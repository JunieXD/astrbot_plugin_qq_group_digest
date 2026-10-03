"""Keep persisted images small; send local bytes even to a remote NapCat host."""

import asyncio
import base64
import io
import warnings
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from PIL import Image

from .config import DigestError

MAX_IMAGE_BYTES = 12 * 1024 * 1024


def local_image_path(uri):
    parts = urlsplit(uri)
    if parts.scheme != "file" or parts.netloc not in ("", "localhost"):
        raise DigestError("海报文件地址无效。")
    return Path(url2pathname(parts.path))


def image_files(payload):
    return [s.get("data", {}).get("file", "") for s in payload.get("message", []) if s.get("type") == "image"]


def _prepare(payload):
    result = deepcopy(payload)
    for segment in result.get("message", []):
        if segment.get("type") != "image":
            continue
        path = local_image_path(segment["data"]["file"])
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_IMAGE_BYTES + 1)
        except OSError as exc:
            raise DigestError("海报文件无法读取，已暂停发送；请重试生成这一期摘要。") from exc
        if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > MAX_IMAGE_BYTES:
            raise DigestError("海报文件格式或大小不符合发送要求。")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    width, height = image.size
                    if (
                        image.format != "PNG"
                        or min(width, height) < 1
                        or max(width, height) > 16000
                        or width * height > 20000000
                    ):
                        raise ValueError("invalid poster dimensions")
                    image.verify()
        except (
            OSError,
            ValueError,
            SyntaxError,
            Image.DecompressionBombWarning,
            Image.DecompressionBombError,
        ) as exc:
            raise DigestError("海报文件已损坏或尺寸异常，已暂停发送。") from exc
        segment["data"]["file"] = "base64://" + base64.b64encode(data).decode("ascii")
    return result


async def prepare_payload(payload):
    return await asyncio.to_thread(_prepare, payload) if image_files(payload) else payload
