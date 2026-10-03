import base64
import io

import pytest
from PIL import Image

from qq_group_digest.config import DigestError
from qq_group_digest.images import image_files, local_image_path, prepare_payload

_buffer = io.BytesIO()
Image.new("RGB", (1, 1), "white").save(_buffer, "PNG")
PNG = _buffer.getvalue()


def image_payload(path):
    return {"message": [{"type": "image", "data": {"file": path.resolve().as_uri(), "summary": "[日报]"}}]}


async def test_image_preparation_uses_bytes_and_preserves_persisted_uri(tmp_path):
    path = tmp_path / "海报 with space.png"
    path.write_bytes(PNG)
    persisted = image_payload(path)
    wire = await prepare_payload(persisted)
    assert wire is not persisted and wire["message"] is not persisted["message"]
    image = wire["message"][0]["data"]
    assert image["summary"] == "[日报]"
    assert image["file"].startswith("base64://")
    assert base64.b64decode(image["file"].removeprefix("base64://")) == PNG
    assert persisted["message"][0]["data"]["file"] == path.resolve().as_uri()
    assert local_image_path(path.resolve().as_uri()) == path.resolve()
    assert image_files(persisted) == [path.resolve().as_uri()]


async def test_text_payload_does_not_copy_or_touch_files():
    payload = {"message": [{"type": "text", "data": {"text": "信息"}}]}
    assert await prepare_payload(payload) is payload
    assert image_files(payload) == []


@pytest.mark.parametrize(
    "uri", ["https://example.org/a.png", "file://remote-host/share/a.png", "a.png", "base64://x"]
)
def test_image_path_rejects_remote_hosts_and_non_file_schemes(uri):
    with pytest.raises(DigestError, match="地址无效"):
        local_image_path(uri)


async def test_missing_image_is_reported_before_a_send_is_attempted(tmp_path):
    with pytest.raises(DigestError, match="无法读取"):
        await prepare_payload(image_payload(tmp_path / "missing.png"))


async def test_corrupted_png_stops_before_upload(tmp_path):
    path = tmp_path / "corrupted.png"
    path.write_bytes(PNG[:24])
    with pytest.raises(DigestError, match="损坏"):
        await prepare_payload(image_payload(path))


@pytest.mark.parametrize("oversized", [False, True])
async def test_image_preparation_rejects_invalid_or_oversized_image(tmp_path, oversized):
    path = tmp_path / "bad.png"
    with path.open("wb") as stream:
        stream.write(PNG if oversized else b"not an image")
        if oversized:
            stream.truncate(12 * 1024 * 1024 + 1)
    with pytest.raises(DigestError, match="格式或大小"):
        await prepare_payload(image_payload(path))


async def test_mixed_payload_keeps_text_and_converts_each_image_independently(tmp_path):
    paths = [tmp_path / "a.png", tmp_path / "b.png"]
    for path in paths:
        path.write_bytes(PNG)
    payload = {"message": [{"type": "text", "data": {"text": "说明"}}]}
    payload["message"].extend(image_payload(path)["message"][0] for path in paths)
    wire = await prepare_payload(payload)
    assert wire["message"][0] == payload["message"][0]
    assert len(image_files(wire)) == 2
    assert all(value.startswith("base64://") for value in image_files(wire))
