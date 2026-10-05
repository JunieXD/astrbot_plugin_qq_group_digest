import asyncio

import pytest

from qq_group_digest.album_transport import AlbumRejected, AlbumTransport, AlbumUncertain, url_identity
from qq_group_digest.album_wire import create_request, created_album, decode, message
from qq_group_digest.config import DigestError


class Adapter:
    account = "123456789"

    def __init__(self, pages=(), result=None):
        self.pages = iter(pages)
        self.calls = []
        self.result = result

    async def read(self, action, **params):
        self.calls.append((action, params))
        result = next(self.pages)
        if isinstance(result, Exception):
            raise result
        return result

    async def transport(self, action, **params):
        self.calls.append((action, params))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def album_page(albums=(), *, cursor="", more=False):
    return {"album_list": list(albums), "attach_info": cursor, "has_more": more}


def media_page(media=(), *, cursor="", more=False):
    return {"result": 0, "media_list": list(media), "next_attach_info": cursor, "next_has_more": more}


def picture(pid="photo", *, title="daily.png", description="时间段", batch="123", uploader="123456789"):
    return {
        "image": {
            "name": title,
            "lloc": pid,
            "default_url": {"url": "https://qungz.photo.store.qq.com/qun-qungz/album/photo/400?x=1"},
        },
        "desc": description,
        "batch_id": batch,
        "uploader": uploader,
    }


async def test_find_album_completes_pagination():
    adapter = Adapter(
        [
            album_page([{"album_id": "target", "name": "日报"}], cursor="second", more=True),
            album_page([{"album_id": "other", "name": "其它"}]),
        ]
    )
    assert await AlbumTransport(adapter).find_album("123456789", "日报") == "target"
    assert adapter.calls[1][1]["attach_info"] == "second"


async def test_find_album_failed_page_does_not_mean_absent():
    adapter = Adapter([album_page(cursor="second", more=True), DigestError("读取失败")])
    with pytest.raises(DigestError, match="读取失败"):
        await AlbumTransport(adapter).find_album("123456789", "日报")


async def test_find_album_requires_valid_terminal_page():
    with pytest.raises(DigestError, match="分页状态"):
        await AlbumTransport(Adapter([{"album_list": []}])).find_album("123456789", "日报")


async def test_find_album_repeated_cursor_stops():
    page = album_page(cursor="again", more=True)
    with pytest.raises(DigestError, match="游标重复"):
        await AlbumTransport(Adapter([page, page])).find_album("123456789", "日报")


async def test_find_album_duplicate_names_are_not_arbitrarily_selected():
    page = album_page([{"album_id": "one", "name": "日报"}, {"album_id": "two", "name": "日报"}])
    with pytest.raises(DigestError, match="多个同名"):
        await AlbumTransport(Adapter([page])).find_album("123456789", "日报")


def test_create_request_contains_only_public_album_protocol():
    fields = decode(create_request("123456789", "鼠群日报"))
    info = decode(decode(fields[4][0])[1][0])
    assert info[2] == [b"123456789"]
    assert info[3] == ["鼠群日报".encode()]
    assert decode(fields[10][0]) == {1: [b"fc-appid"], 2: [b"100"]}


async def test_create_receipt_uses_server_album_identifier():
    result = message((4, message((1, message((1, "album"), (2, "123456789"), (3, "日报")))))).hex()
    adapter = Adapter(result=result)
    assert await AlbumTransport(adapter).create_album("123456789", "日报") == "album"
    assert adapter.calls[0][0] == "send_packet"
    assert adapter.calls[0][1]["rsp"] is True


async def test_create_known_rejection_is_not_unknown():
    with pytest.raises(DigestError) as exc:
        await AlbumTransport(Adapter(result=message((2, 17)).hex())).create_album("123456789", "日报")
    assert isinstance(exc.value, AlbumRejected)


@pytest.mark.parametrize("result", [TimeoutError(), "garbage", "", "00"])
async def test_create_unknown_response_is_not_retried(result):
    adapter = Adapter(result=result)
    with pytest.raises(AlbumUncertain) as exc:
        await AlbumTransport(adapter).create_album("123456789", "日报")
    assert exc.value.receipt == {"operation": "create", "group_id": "123456789", "name": "日报"}
    assert len(adapter.calls) == 1


def test_protobuf_rejects_truncated_bytes():
    with pytest.raises(ValueError):
        decode(b"\x22\xff\xff")
    with pytest.raises((ValueError, KeyError)):
        created_album(b"\x22\x02\x0a\x05")


async def test_credentials_require_the_bound_account_and_keep_secrets_out_of_errors():
    adapter = Adapter([{"cookies": "uin=o999999999; skey=SECRET; p_skey=OTHER"}])
    with pytest.raises(DigestError) as exc:
        await AlbumTransport(adapter)._credentials()
    assert "SECRET" not in str(exc.value) and "OTHER" not in str(exc.value)


async def test_credentials_use_pskey_gtk():
    adapter = Adapter([{"cookies": "uin=o123456789; skey=secret; p_skey=psecret"}])
    uin, _, pskey, gtk = await AlbumTransport(adapter)._credentials()
    assert uin == adapter.account and pskey == "psecret"
    result = 5381
    for c in pskey:
        result = (result * 33 + ord(c)) & 0xFFFFFFFF
    assert gtk == str(result & 0x7FFFFFFF)
    assert adapter.calls[0] == ("get_cookies", {"domain": "qzone.qq.com"})


class UploadTransport(AlbumTransport):
    def __init__(self, adapter, results):
        super().__init__(adapter)
        self.results, self.posts = iter(results), []

    async def _post(self, session, endpoint, gtk, cookie, **kwargs):
        self.posts.append((endpoint, kwargs))
        result = next(self.results)
        if isinstance(result, BaseException):
            raise result
        return result


async def test_upload_uses_server_chunk_size_description_and_no_feeds(tmp_path):
    path = tmp_path / "daily.png"
    path.write_bytes(b"X" * 2500)
    adapter = Adapter([{"cookies": "uin=o123456789; skey=secret; p_skey=psecret"}])
    transport = UploadTransport(
        adapter,
        [
            {"session": "session", "slice_size": 1024},
            {},
            {},
            {"biz": {"sPhotoID": "PHOTO", "sBURL": "https://photo.example/photo"}},
        ],
    )
    result = await transport.upload("123456789", "album", "日报", path, "2026年10月4日—10月5日")
    control = transport.posts[0][1]["payload"]["control_req"][0]
    assert control["biz_req"]["sPicDesc"] == "2026年10月4日—10月5日"
    assert control["biz_req"]["iNeedFeeds"] == 0
    assert [p[1]["params"]["offset"] for p in transport.posts[1:]] == [0, 1024, 2048]
    assert [p[1]["params"]["end"] for p in transport.posts[1:]] == [1024, 2048, 2500]
    assert result["photo_id"] == "PHOTO" and result["url"] == "https://photo.example/photo"
    assert "secret" not in repr(result)


async def test_upload_timeout_preserves_reconciliation_identifiers(tmp_path):
    path = tmp_path / "daily.png"
    path.write_bytes(b"X" * 2500)
    adapter = Adapter([{"cookies": "uin=o123456789; skey=secret; p_skey=psecret"}])
    transport = UploadTransport(
        adapter,
        [
            {"session": "session", "slice_size": 1024},
            {},
            TimeoutError(),
        ],
    )
    with pytest.raises(AlbumUncertain) as exc:
        await transport.upload("123456789", "album", "日报", path, "时间段")
    assert len(transport.posts) == 3
    assert exc.value.receipt["title"] == "daily.png"
    assert exc.value.receipt["description"] == "时间段"
    assert "secret" not in repr(exc.value.receipt)


async def test_upload_invalid_local_file_has_not_started_a_write(tmp_path):
    transport = UploadTransport(Adapter(), [])
    with pytest.raises(DigestError) as exc:
        await transport.upload("123456789", "album", "日报", tmp_path / "missing.png", "时间段")
    assert not isinstance(exc.value, AlbumUncertain)
    assert not transport.posts


async def test_confirm_matches_the_identifier_on_later_page():
    expected = picture("mine")
    adapter = Adapter(
        [
            media_page([picture("someone-else")], cursor="second", more=True),
            media_page([expected]),
        ]
    )
    assert await AlbumTransport(adapter).confirm("123456789", "album", {"photo_id": "mine"}) == expected


async def test_confirm_matches_exact_server_url_without_thumbnail_size():
    expected = picture()
    adapter = Adapter([media_page([expected])])
    result = await AlbumTransport(adapter).confirm(
        "123456789",
        "album",
        {
            "photo_id": "different-wire-id",
            "url": "https://qungz.photo.store.qq.com/qun-qungz/album/photo/0?x=2",
        },
    )
    assert result == expected


async def test_confirm_missing_identity_is_not_guessed_from_latest_photo():
    with pytest.raises(DigestError, match="可靠标识"):
        await AlbumTransport(Adapter([media_page([picture()])])).confirm("123456789", "album", {})


async def test_confirm_unknown_write_requires_exact_title_description_batch_and_owner():
    expected = picture()
    adapter = Adapter([media_page([picture(batch="456"), expected])])
    result = await AlbumTransport(adapter).confirm(
        "123456789",
        "album",
        {
            "title": "daily.png",
            "description": "时间段",
            "batch_id": "123",
            "uploader": "123456789",
        },
    )
    assert result == expected


async def test_confirm_read_failure_does_not_prove_absence():
    with pytest.raises(DigestError, match="查询失败"):
        await AlbumTransport(Adapter([{"result": 9, "media_list": []}])).confirm(
            "123456789", "album", {"photo_id": "mine"}
        )


async def test_confirm_duplicate_identity_is_not_arbitrarily_selected():
    with pytest.raises(DigestError, match="多个匹配"):
        await AlbumTransport(Adapter([media_page([picture(), picture()])])).confirm(
            "123456789", "album", {"photo_id": "photo"}
        )


def test_photo_url_identity_preserves_album_and_photo_but_not_size():
    assert (
        url_identity("https://qungz.photo.store.qq.com/qun-qungz/album/photo/400?x=1")
        == "/qun-qungz/album/photo"
    )


async def test_create_submission_is_saved_before_write_and_cancellation_propagates():
    adapter = Adapter(result=asyncio.CancelledError())
    saved = []

    async def on_submit(receipt):
        assert not adapter.calls
        saved.append(receipt)

    with pytest.raises(asyncio.CancelledError):
        await AlbumTransport(adapter).create_album("123456789", "日报", on_submit=on_submit)
    assert saved[0]["operation"] == "create"
    assert len(adapter.calls) == 1


async def test_submission_callback_failure_prevents_any_write(tmp_path):
    path = tmp_path / "daily.png"
    path.write_bytes(b"X" * 100)
    adapter = Adapter([{"cookies": "uin=o123456789; skey=secret; p_skey=psecret"}])
    transport = UploadTransport(adapter, [])

    async def on_submit(receipt):
        raise OSError("storage unavailable")

    with pytest.raises(OSError, match="storage unavailable"):
        await transport.upload("123456789", "album", "日报", path, "时间段", on_submit=on_submit)
    assert not transport.posts


async def test_upload_submission_contains_identity_before_http_and_cancellation_propagates(tmp_path):
    path = tmp_path / "daily.png"
    path.write_bytes(b"X" * 100)
    adapter = Adapter([{"cookies": "uin=o123456789; skey=secret; p_skey=psecret"}])
    transport = UploadTransport(adapter, [asyncio.CancelledError()])
    saved = []

    async def on_submit(receipt):
        assert not transport.posts
        saved.append(receipt)

    with pytest.raises(asyncio.CancelledError):
        await transport.upload("123456789", "album", "日报", path, "时间段", on_submit=on_submit)
    assert saved[0]["title"] == "daily.png"
    assert saved[0]["batch_id"] and saved[0]["uploader"]


async def test_upload_multipart_matches_browser_form_data_for_legacy_qzone_parser(tmp_path):
    path = tmp_path / "daily.png"
    path.write_bytes(b"X" * 2500)
    adapter = Adapter([{"cookies": "uin=o123456789; skey=secret; p_skey=psecret"}])
    transport = UploadTransport(
        adapter,
        [
            {"session": "session", "slice_size": 1024},
            {},
            {},
            {"biz": {"sPhotoID": "PHOTO"}},
        ],
    )
    await transport.upload("123456789", "album", "日报", path, "时间段")
    written = bytearray()

    class Writer:
        async def write(self, chunk):
            written.extend(chunk)

    await transport.posts[1][1]["form"].write(Writer())
    raw = bytes(written)
    assert b"Content-Type: text/plain" not in raw
    assert raw.count(b"Content-Type: application/octet-stream") == 1
    assert raw.index(b'name="offset"') < raw.index(b'name="data"') < raw.index(b'name="checksum"')


def test_final_upload_url_matches_photo_url_despite_trailing_raw_image_token():
    assert url_identity("http://qungz.photo.store.qq.com/qun-qungz/album/photo/OATmDvI!/") == url_identity(
        "https://qungz.photo.store.qq.com/qun-qungz/album/photo/400?ek=1"
    )


async def test_confirm_uses_exact_returned_photo_id_inside_image_url_when_lloc_is_encoded():
    expected = picture("encoded-wire-lloc")
    adapter = Adapter([media_page([expected])])
    assert await AlbumTransport(adapter).confirm("123456789", "album", {"photo_id": "photo"}) == expected
