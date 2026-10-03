"""Bounded QQ avatar downloads; rendering never depends on the CDN being available."""

import asyncio
import base64
import io
import os
import re
import tempfile
import time
import warnings
from pathlib import Path

import aiohttp
from PIL import Image, ImageOps

_IDENTIFIER = re.compile(r"[1-9][0-9]{4,19}\Z")


class AvatarCache:
    TTL = 24 * 60 * 60
    NEGATIVE_TTL = 60 * 60
    RETENTION = 30 * 24 * 60 * 60
    MAX_ENTRIES = 500
    MAX_PER_RENDER = 65
    MAX_BYTES = 512 * 1024
    MAX_EDGE = 2048
    SIZE = 160
    CONCURRENCY = 4
    REQUEST_SECONDS = 5
    BATCH_SECONDS = 12

    def __init__(self, root, journal=None, *, clock=time.time):
        self.root = Path(root) / "avatars"
        self.root.mkdir(parents=True, exist_ok=True)
        self.journal, self.clock = journal, clock
        self._lock = asyncio.Lock()

    async def user(self, qq):
        identifier = self._identifier(qq)
        if not identifier:
            return None
        return (await self.load({identifier})).get("user:" + identifier)

    async def group(self, group_id):
        identifier = self._identifier(group_id)
        if not identifier:
            return None
        return (await self.load(set(), identifier)).get("group:" + identifier)

    async def load(self, users, group_id=""):
        """Return available data URLs keyed by ``user:<id>`` / ``group:<id>``.

        A stale avatar is preferable to a missing one. A failed fetch is cached for
        an hour, including batch timeouts, so unavailable faces do not delay every
        poster. Cancellation of the caller propagates without becoming a failure.
        """
        identifiers = sorted({i for value in users if (i := self._identifier(value))})
        group = self._identifier(group_id)
        keys = [("group", group)] if group else []
        keys.extend(("user", identifier) for identifier in identifiers)
        keys = keys[: self.MAX_PER_RENDER]
        if not keys:
            return {}

        async with self._lock:
            results, pending = {}, []
            now = self.clock()
            for kind, identifier in keys:
                cached, fresh, negative = await asyncio.to_thread(self._cached, kind, identifier, now)
                if cached:
                    results[f"{kind}:{identifier}"] = self._data_url(cached)
                if not fresh and not negative:
                    pending.append((kind, identifier))

            completed = set()
            if pending:
                semaphore = asyncio.Semaphore(self.CONCURRENCY)
                timeout = aiohttp.ClientTimeout(total=self.REQUEST_SECONDS)
                async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:

                    async def download(kind, identifier):
                        key = f"{kind}:{identifier}"
                        async with semaphore:
                            try:
                                raw = await self._fetch(session, kind, identifier)
                                image = await asyncio.to_thread(self._prepare, raw)
                                results[key] = self._data_url(image)
                                await asyncio.to_thread(self._save, kind, identifier, image, self.clock())
                            except (
                                aiohttp.ClientError,
                                asyncio.TimeoutError,
                                ValueError,
                                OSError,
                                Image.DecompressionBombError,
                            ):
                                await asyncio.to_thread(self._mark_failure, kind, identifier, self.clock())
                            completed.add(key)

                    jobs = [download(kind, identifier) for kind, identifier in pending]
                    try:
                        await asyncio.wait_for(asyncio.gather(*jobs), self.BATCH_SECONDS)
                    except asyncio.TimeoutError:
                        # wait_for cancels and awaits the outstanding downloads.
                        for kind, identifier in pending:
                            if f"{kind}:{identifier}" not in completed:
                                await asyncio.to_thread(self._mark_failure, kind, identifier, self.clock())
                        self._record("头像下载超时", requested=len(pending))
            try:
                await asyncio.to_thread(self._cleanup, self.clock())
            except OSError:
                self._record("头像缓存清理失败")
            self._record("头像缓存读取", requested=len(keys), available=len(results), fetched=len(pending))
            return results

    @staticmethod
    def _identifier(value):
        identifier = str(value)
        return identifier if _IDENTIFIER.fullmatch(identifier) else ""

    def _paths(self, kind, identifier):
        stem = f"{kind}_{identifier}"
        return self.root / (stem + ".jpg"), self.root / (stem + ".miss")

    def _cached(self, kind, identifier, now):
        image_path, miss_path = self._paths(kind, identifier)
        image, fresh = None, False
        try:
            stat = image_path.stat()
            if 0 < stat.st_size <= self.MAX_BYTES:
                image = image_path.read_bytes()
                if not image.startswith(b"\xff\xd8"):
                    image = None
                fresh = image is not None and max(0, now - stat.st_mtime) < self.TTL
        except OSError:
            pass
        try:
            negative = max(0, now - miss_path.stat().st_mtime) < self.NEGATIVE_TTL
        except OSError:
            negative = False
        return image, fresh, negative

    async def _fetch(self, session, kind, identifier):
        if kind == "user":
            url = f"https://q1.qlogo.cn/g?b=qq&nk={identifier}&s=100"
        else:
            url = f"https://p.qlogo.cn/gh/{identifier}/{identifier}/100"
        async with session.get(url, allow_redirects=False) as response:
            if response.status != 200 or (response.content_length or 0) > self.MAX_BYTES:
                raise ValueError("avatar response rejected")
            body = bytearray()
            async for chunk in response.content.iter_chunked(16 * 1024):
                body.extend(chunk)
                if len(body) > self.MAX_BYTES:
                    raise ValueError("avatar response too large")
            return bytes(body)

    def _prepare(self, raw):
        if not raw or len(raw) > self.MAX_BYTES:
            raise ValueError("invalid avatar size")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            try:
                with Image.open(io.BytesIO(raw)) as image:
                    if min(image.size) < 1 or max(image.size) > self.MAX_EDGE:
                        raise ValueError("invalid avatar dimensions")
                    image.load()
                    image = ImageOps.fit(
                        image.convert("RGBA"), (self.SIZE, self.SIZE), Image.Resampling.LANCZOS
                    )
                    clean = Image.new("RGB", image.size, "white")
                    clean.paste(image, mask=image.getchannel("A"))
                    output = io.BytesIO()
                    clean.save(output, "JPEG", quality=88, optimize=True)
                    return output.getvalue()
            except Image.DecompressionBombWarning as exc:
                raise ValueError("invalid avatar dimensions") from exc

    def _save(self, kind, identifier, image, now):
        image_path, miss_path = self._paths(kind, identifier)
        self._atomic_write(image_path, image, now)
        miss_path.unlink(missing_ok=True)

    def _mark_failure(self, kind, identifier, now):
        _, path = self._paths(kind, identifier)
        try:
            self._atomic_write(path, b"", now)
        except OSError:
            # The renderer can use a placeholder even if local storage is full.
            pass

    def _atomic_write(self, path, contents, now):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, prefix=".avatar-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(contents)
            os.utime(temporary, (now, now))
            os.replace(temporary, path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def _cleanup(self, now):
        entries = {}
        for path in self.root.iterdir():
            if path.name.startswith(".avatar-"):
                try:
                    if now - path.stat().st_mtime > self.TTL:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            if path.suffix not in {".jpg", ".miss"} or not re.fullmatch(r"(?:user|group)_[0-9]+", path.stem):
                continue
            try:
                updated = path.stat().st_mtime
                if now - updated > self.RETENTION:
                    path.unlink(missing_ok=True)
                else:
                    entries.setdefault(path.stem, []).append((updated, path))
            except OSError:
                continue
        oldest = sorted(entries.values(), key=lambda files: max(updated for updated, _ in files))
        for files in oldest[: max(0, len(oldest) - self.MAX_ENTRIES)]:
            for _, path in files:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _data_url(image):
        return "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")

    def _record(self, event, **fields):
        if self.journal:
            self.journal.record(event, **fields)
