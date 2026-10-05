"""Minimal protobuf fields for the public NapCat send_packet album command.

Based on QunAlbum.AddAlbum in LLOneBot. No generated runtime or private QQ
session access is required; unknown protobuf fields are skipped safely.
"""

from __future__ import annotations

import secrets
from datetime import datetime


def varint(value):
    if not isinstance(value, int) or value < 0:
        raise ValueError("invalid varint")
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    if isinstance(value, str):
        value = value.encode("utf-8")
    return varint((number << 3) | 2) + varint(len(value)) + value


def message(*fields):
    return b"".join(field(number, value) for number, value in fields)


def create_request(group, name):
    now = datetime.now()
    session = now.strftime("_%m%d%H%M%S") + f"{now.microsecond // 1000:03}_{secrets.randbelow(90000) + 10000}"
    info = message((2, str(group)), (3, name), (4, ""), (5, 0))
    header = message((1, "fc-appid"), (2, "100"))
    return message(
        (1, secrets.randbelow(0x7FFFFFFE) + 1),
        (2, b""),
        (3, b""),
        (4, message((1, info))),
        (5, session),
        (10, header),
    )


def decode(raw):
    """Decode scalar/length-delimited fields; reject truncation and overflows."""
    fields, position = {}, 0

    def read_varint():
        nonlocal position
        value = 0
        for shift in range(0, 70, 7):
            if position >= len(raw):
                raise ValueError("truncated protobuf")
            byte = raw[position]
            position += 1
            if shift == 63 and byte > 1:
                raise ValueError("protobuf overflow")
            value |= (byte & 127) << shift
            if not byte & 128:
                return value
        raise ValueError("invalid protobuf")

    while position < len(raw):
        key = read_varint()
        number, wire = key >> 3, key & 7
        if not number:
            raise ValueError("invalid protobuf field")
        if wire == 0:
            value = read_varint()
        elif wire in (1, 2, 5):
            length = read_varint() if wire == 2 else (8 if wire == 1 else 4)
            end = position + length
            if end > len(raw):
                raise ValueError("truncated protobuf field")
            value, position = raw[position:end], end
        else:
            raise ValueError("unsupported protobuf wire")
        fields.setdefault(number, []).append(value)
    return fields


def created_album(raw):
    """Return explicit server code and returned album metadata."""
    data = decode(raw)
    code = data.get(2, [0])[0]  # protobuf omits the successful zero value
    if not isinstance(code, int):
        raise ValueError("invalid result code")
    if code:
        return code, {}
    body = decode(data[4][0])
    info = decode(body[1][0])
    return code, {
        key: info.get(index, [b""])[0].decode("utf-8")
        for key, index in (("album_id", 1), ("group_id", 2), ("name", 3))
    }
