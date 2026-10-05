"""QQ group-album transport behind public OneBot/HTTP interfaces.

Writes are never retried here. A missing response after a write is distinguished
from a known rejection so the durable archive queue can reconcile it first.
Cookies stay in the request scope and never enter receipts, logs or errors.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from .album_wire import create_request, created_album
from .config import DigestError, identifier

CREATE_COMMAND = "QunAlbum.trpc.qzone.webapp_qun_media.QunMedia.AddAlbum"
UPLOAD_BASE = "https://h5.qzone.qq.com/webapp/json/sliceUpload/"
MAX_PAGES = 100
MAX_IMAGE_BYTES = 32 * 1024 * 1024


class AlbumRejected(DigestError):
    """The server explicitly rejected a creation; no new album was made."""


class AlbumUncertain(DigestError):
    """A write may have succeeded; verify its receipt before any new write."""

    def __init__(self, message, *, receipt=None):
        super().__init__(message)
        self.receipt = dict(receipt or {})


def checked_dict(value, message):
    if not isinstance(value, dict):
        raise DigestError(message)
    return value


def page_cursor(data, *, media=False):
    more_key, cursor_key = ("next_has_more", "next_attach_info") if media else ("has_more", "attach_info")
    more = data.get(more_key)
    if not isinstance(more, bool):
        raise DigestError("群相册接口没有提供有效的分页状态。")
    cursor = data.get(cursor_key, "")
    if more and (not isinstance(cursor, str) or not cursor):
        raise DigestError("群相册接口缺少下一页游标，已停止查询。")
    return cursor if more else None


def url_identity(value):
    """Compare server-returned photo identity independently of thumbnail size."""
    if not isinstance(value, str) or not value:
        return ""
    parsed = urlsplit(value)
    if parsed.hostname and parsed.hostname.endswith(".photo.store.qq.com"):
        parts = parsed.path.split("/")
        return "/".join(parts[:4]) if len(parts) >= 4 else parsed.path
    return value.split("?", 1)[0].split("#", 1)[0]


def photo_urls(image):
    default = image.get("default_url")
    if isinstance(default, dict) and default.get("url"):
        yield default["url"]
    for item in image.get("photo_url", []):
        nested = item.get("url") if isinstance(item, dict) else None
        if isinstance(nested, dict) and nested.get("url"):
            yield nested["url"]


class AlbumTransport:
    def __init__(self, adapter, journal=None):
        self.adapter, self.journal = adapter, journal

    async def find_album(self, group, name):
        group = identifier(group, "相册目标群")
        cursor, seen, matches = "", set(), set()
        for _ in range(MAX_PAGES):
            data = checked_dict(
                await self.adapter.read("get_qun_album_list", group_id=group, attach_info=cursor),
                "无法读取群相册列表。",
            )
            albums = data.get("album_list")
            if not isinstance(albums, list):
                raise DigestError("群相册接口没有返回有效的相册列表。")
            for album in albums:
                if not isinstance(album, dict):
                    raise DigestError("群相册列表包含无效数据。")
                if album.get("name", album.get("album_name")) == name:
                    aid = album.get("album_id")
                    if not isinstance(aid, str) or not aid:
                        raise DigestError("同名群相册缺少相册 ID。")
                    matches.add(aid)
            cursor = page_cursor(data)
            if cursor is None:
                if len(matches) > 1:
                    raise DigestError("这个群有多个同名相册，请先保留一个或更换相册名称。")
                return next(iter(matches), None)
            if cursor in seen:
                raise DigestError("群相册分页游标重复，已停止查询。")
            seen.add(cursor)
        raise DigestError("群相册页数超过查询上限，无法确认相册是否存在。")

    async def create_album(self, group, name, *, on_submit=None):
        group = identifier(group, "相册目标群")
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise DigestError("群相册名称应为 1～100 字。")
        receipt = {"operation": "create", "group_id": group, "name": name}
        if on_submit is not None:
            await on_submit(dict(receipt))
        try:
            result = await self.adapter.transport(
                "send_packet", cmd=CREATE_COMMAND, data=create_request(group, name).hex(), rsp=True
            )
            code, album = created_album(bytes.fromhex(result))
        except Exception as exc:
            raise AlbumUncertain("群相册创建结果未确认，请先查询相册后再继续。", receipt=receipt) from exc
        if code:
            raise AlbumRejected("QQ 拒绝了创建群相册请求，请检查管理员权限和群相册设置。")
        if not album.get("album_id") or album.get("group_id") != group or album.get("name") != name:
            raise AlbumUncertain("创建响应未提供匹配的群相册标识，已暂停归档。", receipt=receipt)
        return album["album_id"]

    async def _credentials(self):
        data = checked_dict(
            await self.adapter.read("get_cookies", domain="qzone.qq.com"), "无法取得群相册登录票据。"
        )
        raw = data.get("cookies")
        if not isinstance(raw, str) or any(c in raw for c in "\r\n"):
            raise DigestError("群相册登录票据不可用，请重新登录 QQ。")
        cookies = SimpleCookie()
        try:
            cookies.load(raw)
            values = {key: value.value for key, value in cookies.items()}
        except Exception as exc:
            raise DigestError("群相册登录票据格式异常。") from exc
        skey, pskey = values.get("skey", ""), values.get("p_skey", "")
        uin = (values.get("uin") or values.get("p_uin", "")).lstrip("o")
        account = str(getattr(self.adapter, "account", "") or "")
        if not skey or not pskey or not uin.isdigit() or (account and account != uin):
            raise DigestError("群相册登录票据已失效或与机器人不符，请重新登录 QQ。")
        # This is the same p_skey GTK used by NapCat's public get_cookies.
        gtk = 5381
        for char in pskey:
            gtk = (gtk * 33 + ord(char)) & 0xFFFFFFFF
        cookie = f"p_uin=o{uin}; p_skey={pskey}; skey={skey}; uin=o{uin}"
        return uin, cookie, pskey, str(gtk & 0x7FFFFFFF)

    async def _post(self, session, endpoint, gtk, cookie, *, payload=None, form=None, params=None):
        params = dict(params or {}, g_tk=gtk)
        async with session.post(
            UPLOAD_BASE + endpoint,
            params=params,
            headers={"Cookie": cookie},
            json=payload,
            data=form,
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise DigestError("群相册上传服务暂时不可用。")
            try:
                result = await response.json(content_type=None)
            except (ValueError, aiohttp.ClientError) as exc:
                raise DigestError("群相册上传服务返回了无效结果。") from exc
            result = checked_dict(result, "群相册上传服务返回了无效结果。")
            if type(result.get("ret")) is not int or result["ret"] != 0:
                raise DigestError("QQ 群相册上传未成功，请检查登录状态和上传权限。")
            data = checked_dict(result.get("data"), "群相册上传响应缺少数据。")
            if "ret" in data and (type(data["ret"]) is not int or data["ret"] != 0):
                raise DigestError("QQ 群相册上传未成功，请检查登录状态和上传权限。")
            return data

    async def upload(self, group, album_id, name, path, description, *, on_submit=None):
        group = identifier(group, "相册目标群")
        if not isinstance(album_id, str) or not album_id:
            raise DigestError("上传图片缺少群相册 ID。")
        path = Path(path)
        try:
            size = path.stat().st_size
            if not 0 < size <= MAX_IMAGE_BYTES:
                raise ValueError
            contents = await asyncio.to_thread(path.read_bytes)
        except (OSError, ValueError) as exc:
            raise DigestError("日报图片不可读取或超过 32 MiB 上传限制。") from exc
        uin, cookie, pskey, gtk = await self._credentials()
        checksum = hashlib.md5(contents).hexdigest()
        timestamp = int(time.time())
        receipt = {
            "operation": "upload",
            "group_id": group,
            "album_id": album_id,
            "title": path.name,
            "description": description,
            "checksum": checksum,
            "batch_id": str(timestamp),
            "uploader": uin,
            "photo_id": "",
            "url": "",
        }
        control = {
            "uin": uin,
            "token": {"type": 4, "data": pskey, "appid": 5},
            "appid": "qun",
            "checksum": checksum,
            "check_type": 0,
            "file_len": len(contents),
            "env": {"refer": "qzone", "deviceInfo": "h5"},
            "model": 0,
            "biz_req": {
                "sPicTitle": path.name,
                "sPicDesc": description,
                "sAlbumName": name,
                "sAlbumID": album_id,
                "iAlbumTypeID": 0,
                "iBitmap": 0,
                "iUploadType": 0,
                "iUpPicType": 0,
                "iBatchID": timestamp,
                "sPicPath": "",
                "iPicWidth": 0,
                "iPicHight": 0,
                "iWaterType": 0,
                "iDistinctUse": 0,
                "iNeedFeeds": 0,
                "iUploadTime": timestamp,
                "mapExt": {"appid": "qun", "userid": group},
                "stExtendInfo": {"mapParams": {"photo_num": "1", "video_num": "0", "batch_num": "1"}},
            },
            "session": "",
            "asy_upload": 0,
            "cmd": "FileUpload",
        }
        if on_submit is not None:
            await on_submit(dict(receipt))
        try:
            timeout = aiohttp.ClientTimeout(total=25, connect=10)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                return await asyncio.wait_for(
                    self._upload_slices(session, gtk, cookie, control, contents, receipt), timeout=180
                )
        except Exception as exc:
            # Control may immediately reuse an existing checksum or finish a
            # session. Any failure from this point requires reconciliation.
            raise AlbumUncertain("群相册图片上传结果未确认，已暂停自动重试。", receipt=receipt) from exc

    async def _upload_slices(self, session, gtk, cookie, control, contents, receipt):
        data = await self._post(
            session,
            f"FileBatchControl/{receipt['checksum']}",
            gtk,
            cookie,
            payload={"control_req": [control]},
        )
        upload_session, slice_size = data.get("session"), data.get("slice_size", 16384)
        if not isinstance(upload_session, str) or not upload_session:
            raise DigestError("群相册上传响应未提供会话。")
        if type(slice_size) is not int or not 1024 <= slice_size <= 8 * 1024 * 1024:
            raise DigestError("群相册上传返回了异常的分片大小。")
        for seq, offset in enumerate(range(0, len(contents), slice_size)):
            chunk = contents[offset : offset + slice_size]
            end = offset + len(chunk)
            form = aiohttp.MultipartWriter("form-data")
            fields = {
                "uin": receipt["uploader"],
                "appid": "qun",
                "session": upload_session,
                "offset": offset,
                "checksum": "",
                "check_type": 0,
                "retry": 0,
                "seq": seq,
                "end": end,
                "cmd": "FileUpload",
                "slice_size": len(chunk),
                "biz_req.iUploadType": 0,
            }
            for key, value in fields.items():
                part = form.append(str(value))
                # QZone's legacy form parser expects browser FormData text
                # parts without a Content-Type. aiohttp.FormData adds one.
                part.headers.pop("Content-Type", None)
                part.set_content_disposition("form-data", name=key)
                if key == "offset":
                    blob = form.append(chunk, {"Content-Type": "application/octet-stream"})
                    blob.set_content_disposition("form-data", name="data", filename="blob")
            data = await self._post(
                session,
                "FileUpload",
                gtk,
                cookie,
                form=form,
                params={
                    "seq": seq,
                    "retry": 0,
                    "offset": offset,
                    "end": end,
                    "total": len(contents),
                    "type": "form",
                },
            )
            biz = data.get("biz")
            if isinstance(biz, dict):
                receipt["photo_id"] = str(biz.get("sPhotoID") or receipt["photo_id"])
                receipt["url"] = str(biz.get("sBURL") or receipt["url"])
        if not receipt["photo_id"] and not receipt["url"]:
            raise DigestError("群相册上传没有返回可核实的照片标识。")
        return dict(receipt)

    async def confirm(self, group, album_id, receipt):
        """Return matched media or None only after complete, successful paging."""
        group = identifier(group, "相册目标群")
        receipt = checked_dict(receipt, "群相册核验缺少上传凭据。")
        photo_id, image_url = str(receipt.get("photo_id") or ""), url_identity(receipt.get("url"))
        fallback = all(receipt.get(k) for k in ("title", "description", "batch_id", "uploader"))
        if not photo_id and not image_url and not fallback:
            raise DigestError("照片核验缺少可靠标识，不能确定是否已经上传。")
        cursor, seen, matches = "", set(), []
        for _ in range(MAX_PAGES):
            data = checked_dict(
                await self.adapter.read(
                    "get_group_album_media_list", group_id=group, album_id=album_id, attach_info=cursor
                ),
                "无法核验群相册图片。",
            )
            if data.get("result") != 0 or not isinstance(data.get("media_list"), list):
                raise DigestError("群相册图片列表查询失败，不能判断是否已经上传。")
            for media in data["media_list"]:
                if not isinstance(media, dict):
                    raise DigestError("群相册图片列表包含无效数据。")
                image = media.get("image") or {}
                if not isinstance(image, dict):
                    continue
                ids = (media.get("photo_id"), media.get("media_id"), image.get("lloc"), image.get("sloc"))
                urls = tuple(photo_urls(image))
                matched = bool(
                    photo_id
                    and (
                        photo_id in ids
                        or any(url_identity(url).rsplit("/", 1)[-1] == photo_id for url in urls)
                    )
                ) or bool(image_url and any(url_identity(url) == image_url for url in urls))
                if not matched and not photo_id and not image_url and fallback:
                    matched = (
                        image.get("name") == receipt["title"]
                        and media.get("desc") == receipt["description"]
                        and str(media.get("batch_id")) == str(receipt["batch_id"])
                        and str(media.get("uploader")) == str(receipt["uploader"])
                    )
                if matched:
                    matches.append(media)
            cursor = page_cursor(data, media=True)
            if cursor is None:
                if len(matches) > 1:
                    raise DigestError("照片核验发现多个匹配结果，已暂停避免重复上传。")
                return matches[0] if matches else None
            if cursor in seen:
                raise DigestError("群相册图片分页游标重复，已暂停核验。")
            seen.add(cursor)
        raise DigestError("群相册图片页数超过核验上限。")
