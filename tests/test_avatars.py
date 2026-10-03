import asyncio
import base64
import io
import os

import pytest
from PIL import Image

from qq_group_digest.avatars import AvatarCache


def bitmap(size=(40, 30), *, transparent=False):
    image = Image.new("RGBA" if transparent else "RGB", size, (200, 40, 20, 0) if transparent else "coral")
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


class Clock:
    now = 100_000

    def __call__(self):
        return self.now


class FetchCache(AvatarCache):
    def __init__(self, root, **kwargs):
        super().__init__(root, **kwargs)
        self.calls = []
        self.failure = False

    async def _fetch(self, session, kind, identifier):
        self.calls.append((kind, identifier))
        if self.failure:
            raise OSError("CDN unavailable")
        return bitmap()


async def test_cache_reuses_downloads_and_never_fetches_model_urls(tmp_path):
    cache = FetchCache(tmp_path)
    images = await cache.load({"123456", "https://example.com/face", "000123", "<img>"}, "987654")
    assert set(images) == {"user:123456", "group:987654"}
    assert len(cache.calls) == 2
    assert await cache.user("123456") == images["user:123456"]
    assert await cache.group("987654") == images["group:987654"]
    assert await cache.user("https://q1.qlogo.cn/g?nk=123456") is None
    assert len(cache.calls) == 2


def test_decoded_avatar_is_small_static_and_has_no_metadata(tmp_path):
    cache = AvatarCache(tmp_path)
    encoded = cache._prepare(bitmap((600, 400), transparent=True))
    with Image.open(io.BytesIO(encoded)) as image:
        assert image.size == (160, 160)
        assert image.mode == "RGB"
        assert image.format == "JPEG"
        assert not image.getexif()
        assert image.getpixel((80, 80)) == (255, 255, 255)


@pytest.mark.parametrize(
    "raw", [b"not an image", b"", b"a" * (512 * 1024 + 1)], ids=["invalid", "empty", "oversized"]
)
def test_invalid_or_large_download_is_rejected(tmp_path, raw):
    cache = AvatarCache(tmp_path)
    with pytest.raises((ValueError, OSError)):
        cache._prepare(raw)


def test_large_dimensions_are_rejected_before_loading_pixels(tmp_path):
    cache = AvatarCache(tmp_path)
    with pytest.raises(ValueError, match="dimensions"):
        cache._prepare(bitmap((2049, 1)))


async def test_stale_avatar_survives_failure_and_failure_is_not_repeated(tmp_path):
    clock = Clock()
    cache = FetchCache(tmp_path, clock=clock)
    original = await cache.user("123456")
    clock.now += cache.TTL + 1
    cache.failure = True
    assert await cache.user("123456") == original
    assert len(cache.calls) == 2
    assert await cache.user("123456") == original
    assert len(cache.calls) == 2
    clock.now += cache.NEGATIVE_TTL + 1
    cache.failure = False
    assert await cache.user("123456") == original
    assert len(cache.calls) == 3
    assert not (cache.root / "user_123456.miss").exists()


async def test_failed_first_download_uses_negative_cache(tmp_path):
    cache = FetchCache(tmp_path)
    cache.failure = True
    assert await cache.user("123456") is None
    assert await cache.user("123456") is None
    assert len(cache.calls) == 1


async def test_batch_limits_requests_and_download_concurrency(tmp_path):
    cache = FetchCache(tmp_path)
    active, peak = 0, 0

    async def fetch(session, kind, identifier):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        cache.calls.append((kind, identifier))
        return bitmap()

    cache._fetch = fetch
    result = await cache.load({str(number) for number in range(100_000, 100_100)}, "987654")
    assert len(result) == 65
    assert "group:987654" in result
    assert len(cache.calls) == 65
    assert peak == 4


async def test_batch_timeout_stops_pending_downloads_and_negative_caches_them(tmp_path):
    cache = FetchCache(tmp_path)
    cache.BATCH_SECONDS = 0.02
    cancelled = []

    async def fetch(session, kind, identifier):
        cache.calls.append((kind, identifier))
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled.append(identifier)
            raise

    cache._fetch = fetch
    identifiers = {str(number) for number in range(100_000, 100_008)}
    assert await cache.load(identifiers) == {}
    assert len(cancelled) == 4
    assert len(list(cache.root.glob("*.miss"))) == 8
    assert await cache.load(identifiers) == {}
    assert len(cache.calls) == 4


async def test_caller_cancellation_propagates_without_negative_cache(tmp_path):
    cache = FetchCache(tmp_path)
    started = asyncio.Event()

    async def fetch(session, kind, identifier):
        started.set()
        await asyncio.sleep(100)

    cache._fetch = fetch
    task = asyncio.create_task(cache.user("123456"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(cache.root.glob("*.miss"))


async def test_storage_failure_still_returns_downloaded_avatar(tmp_path):
    cache = FetchCache(tmp_path)

    def fail(*args):
        raise OSError("disk full")

    cache._save = fail
    result = await cache.user("123456")
    assert result.startswith("data:image/jpeg;base64,")
    Image.open(io.BytesIO(base64.b64decode(result.split(",", 1)[1]))).verify()


def test_cleanup_bounds_age_and_number_of_cache_entries(tmp_path):
    clock = Clock()
    cache = AvatarCache(tmp_path, clock=clock)
    cache.MAX_ENTRIES = 2
    image = cache._prepare(bitmap())
    for number, age in [(100_001, 10), (100_002, 20), (100_003, 30), (100_004, cache.RETENTION + 1)]:
        cache._save("user", str(number), image, clock.now - age)
    cache._mark_failure("user", "100003", clock.now - 30)
    cache._cleanup(clock.now)
    assert sorted(path.name for path in cache.root.iterdir()) == ["user_100001.jpg", "user_100002.jpg"]


class Response:
    def __init__(self, chunks, *, status=200, length=None):
        self.status, self.content_length = status, length
        self.chunks = chunks
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk


class Session:
    def __init__(self, response):
        self.response, self.requests = response, []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.response


@pytest.mark.parametrize("kind", ["user", "group"])
async def test_fetch_uses_fixed_https_cdns_and_disables_redirects(tmp_path, kind):
    cache = AvatarCache(tmp_path)
    session = Session(Response([b"image bytes"]))
    assert await cache._fetch(session, kind, "123456") == b"image bytes"
    url, kwargs = session.requests[0]
    assert url == (
        "https://q1.qlogo.cn/g?b=qq&nk=123456&s=100"
        if kind == "user"
        else "https://p.qlogo.cn/gh/123456/123456/100"
    )
    assert kwargs == {"allow_redirects": False}


@pytest.mark.parametrize(
    "response",
    [Response([], status=302), Response([], length=512 * 1024 + 1), Response([b"a" * (512 * 1024), b"b"])],
)
async def test_fetch_rejects_redirects_and_oversized_bodies(tmp_path, response):
    cache = AvatarCache(tmp_path)
    with pytest.raises(ValueError):
        await cache._fetch(Session(response), "user", "123456")


async def test_corrupt_cached_file_is_replaced(tmp_path):
    cache = FetchCache(tmp_path)
    path = cache.root / "user_123456.jpg"
    path.write_bytes(b"bad cache")
    os.utime(path, None)
    assert (await cache.user("123456")).startswith("data:image/jpeg;base64,")
    assert len(cache.calls) == 1
