#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch CCTV8K Y_JCE EPG from jacc.ysp.cctv.cn and save it as XMLTV.

The request layout is ported from the Android code in this repository:
com.lizongying.mytv.jce.JceRequestBodyConverter and b.java.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import ssl
import struct
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, TypeVar


JACC_URL = "https://jacc.ysp.cctv.cn/"
CCTV8K_PID = "600156816"
CCTV8K_CHANNEL_ID = "CCTV8K"
CCTV8K_CHANNEL_NAME = "CCTV8K 超高清"
CMD_TV_TIME_SHIFT_PROGRAM = 24897
DEFAULT_GUID = "093e7e5989684fd986c44f07542d8dc8"
USER_AGENT = "CCTVVideo/2.9.0 (iPad; iOS 17.2; Scale/2.00)"
CN_TZ = timezone(timedelta(hours=8))


TYPE_BYTE = 0
TYPE_SHORT = 1
TYPE_INT = 2
TYPE_LONG = 3
TYPE_FLOAT = 4
TYPE_DOUBLE = 5
TYPE_STRING1 = 6
TYPE_STRING4 = 7
TYPE_MAP = 8
TYPE_LIST = 9
TYPE_STRUCT_BEGIN = 10
TYPE_STRUCT_END = 11
TYPE_ZERO = 12
TYPE_SIMPLE_LIST = 13

T = TypeVar("T")


@dataclass
class TVProgram:
    program_id: str
    start_time_stamp: int
    epg_time: str
    name: str
    copy_right: int
    time_shift_flag: int
    duration: int
    pic_screen_flag: int
    video_screen_flag: int


@dataclass
class TVTimeShiftProgramResponse:
    errcode: int
    errmsg: str
    programs: list[TVProgram]
    time_shift_flag: int


class JceDecodeError(RuntimeError):
    pass


class JceWriter:
    def __init__(self) -> None:
        self.buf = bytearray()

    def to_bytes(self) -> bytes:
        return bytes(self.buf)

    def write_head(self, type_: int, tag: int) -> None:
        if tag < 15:
            self.buf.append((tag << 4) | type_)
        elif tag < 256:
            self.buf.append(0xF0 | type_)
            self.buf.append(tag)
        else:
            raise ValueError(f"JCE tag is too large: {tag}")

    def write_byte(self, value: int, tag: int) -> None:
        if value == 0:
            self.write_head(TYPE_ZERO, tag)
            return
        self.write_head(TYPE_BYTE, tag)
        self.buf.extend(struct.pack(">b", value))

    def write_short(self, value: int, tag: int) -> None:
        if -128 <= value <= 127:
            self.write_byte(value, tag)
            return
        self.write_head(TYPE_SHORT, tag)
        self.buf.extend(struct.pack(">h", value))

    def write_int(self, value: int, tag: int) -> None:
        if -32768 <= value <= 32767:
            self.write_short(value, tag)
            return
        self.write_head(TYPE_INT, tag)
        self.buf.extend(struct.pack(">i", value))

    def write_long(self, value: int, tag: int) -> None:
        if -2147483648 <= value <= 2147483647:
            self.write_int(value, tag)
            return
        self.write_head(TYPE_LONG, tag)
        self.buf.extend(struct.pack(">q", value))

    def write_string(self, value: str, tag: int) -> None:
        data = value.encode("utf-8")
        if len(data) <= 255:
            self.write_head(TYPE_STRING1, tag)
            self.buf.append(len(data))
        else:
            self.write_head(TYPE_STRING4, tag)
            self.buf.extend(struct.pack(">i", len(data)))
        self.buf.extend(data)

    def write_bytes(self, value: bytes, tag: int) -> None:
        self.write_head(TYPE_SIMPLE_LIST, tag)
        self.write_head(TYPE_BYTE, 0)
        self.write_int(len(value), 0)
        self.buf.extend(value)

    def write_struct(self, tag: int, writer: Callable[["JceWriter"], None]) -> None:
        self.write_head(TYPE_STRUCT_BEGIN, tag)
        writer(self)
        self.write_head(TYPE_STRUCT_END, 0)


class JceReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def _require(self, size: int) -> None:
        if self.pos + size > len(self.data):
            raise JceDecodeError("unexpected end of JCE data")

    def _read(self, size: int) -> bytes:
        self._require(size)
        value = self.data[self.pos : self.pos + size]
        self.pos += size
        return value

    def _read_head_at(self, pos: int) -> tuple[int, int, int]:
        if pos >= len(self.data):
            raise JceDecodeError("missing JCE head")
        head = self.data[pos]
        type_ = head & 0x0F
        tag = (head & 0xF0) >> 4
        if tag == 15:
            if pos + 1 >= len(self.data):
                raise JceDecodeError("missing extended JCE tag")
            tag = self.data[pos + 1]
            return type_, tag, 2
        return type_, tag, 1

    def read_head(self) -> tuple[int, int]:
        type_, tag, size = self._read_head_at(self.pos)
        self.pos += size
        return type_, tag

    def skip_to_tag(self, target_tag: int) -> bool:
        while True:
            try:
                type_, tag, head_size = self._read_head_at(self.pos)
            except JceDecodeError:
                return False
            if type_ == TYPE_STRUCT_END:
                return False
            if target_tag <= tag:
                return target_tag == tag
            self.pos += head_size
            self.skip_field_by_type(type_)

    def skip_field(self) -> None:
        type_, _ = self.read_head()
        self.skip_field_by_type(type_)

    def skip_field_by_type(self, type_: int) -> None:
        if type_ == TYPE_BYTE:
            self.pos += 1
        elif type_ == TYPE_SHORT:
            self.pos += 2
        elif type_ in (TYPE_INT, TYPE_FLOAT):
            self.pos += 4
        elif type_ in (TYPE_LONG, TYPE_DOUBLE):
            self.pos += 8
        elif type_ == TYPE_STRING1:
            size = self._read(1)[0]
            self.pos += size
        elif type_ == TYPE_STRING4:
            size = struct.unpack(">i", self._read(4))[0]
            self.pos += size
        elif type_ == TYPE_MAP:
            size = self.read_int(0, required=True)
            for _ in range(size * 2):
                self.skip_field()
        elif type_ == TYPE_LIST:
            size = self.read_int(0, required=True)
            for _ in range(size):
                self.skip_field()
        elif type_ == TYPE_STRUCT_BEGIN:
            self.skip_to_struct_end()
        elif type_ in (TYPE_STRUCT_END, TYPE_ZERO):
            return
        elif type_ == TYPE_SIMPLE_LIST:
            inner_type, _ = self.read_head()
            if inner_type != TYPE_BYTE:
                raise JceDecodeError(f"invalid simple list inner type: {inner_type}")
            size = self.read_int(0, required=True)
            self.pos += size
        else:
            raise JceDecodeError(f"invalid JCE type: {type_}")
        if self.pos > len(self.data):
            raise JceDecodeError("JCE field exceeds buffer")

    def skip_to_struct_end(self) -> None:
        while True:
            type_, _ = self.read_head()
            self.skip_field_by_type(type_)
            if type_ == TYPE_STRUCT_END:
                return

    def read_int(self, tag: int, required: bool = False, default: int = 0) -> int:
        if not self.skip_to_tag(tag):
            if required:
                raise JceDecodeError(f"required int field tag {tag} not found")
            return default
        type_, _ = self.read_head()
        if type_ == TYPE_BYTE:
            return struct.unpack(">b", self._read(1))[0]
        if type_ == TYPE_SHORT:
            return struct.unpack(">h", self._read(2))[0]
        if type_ == TYPE_INT:
            return struct.unpack(">i", self._read(4))[0]
        if type_ == TYPE_ZERO:
            return 0
        raise JceDecodeError(f"type mismatch for int tag {tag}: {type_}")

    def read_long(self, tag: int, required: bool = False, default: int = 0) -> int:
        if not self.skip_to_tag(tag):
            if required:
                raise JceDecodeError(f"required long field tag {tag} not found")
            return default
        type_, _ = self.read_head()
        if type_ == TYPE_BYTE:
            return struct.unpack(">b", self._read(1))[0]
        if type_ == TYPE_SHORT:
            return struct.unpack(">h", self._read(2))[0]
        if type_ == TYPE_INT:
            return struct.unpack(">i", self._read(4))[0]
        if type_ == TYPE_LONG:
            return struct.unpack(">q", self._read(8))[0]
        if type_ == TYPE_ZERO:
            return 0
        raise JceDecodeError(f"type mismatch for long tag {tag}: {type_}")

    def read_string(
        self, tag: int, required: bool = False, default: Optional[str] = None
    ) -> Optional[str]:
        if not self.skip_to_tag(tag):
            if required:
                raise JceDecodeError(f"required string field tag {tag} not found")
            return default
        type_, _ = self.read_head()
        if type_ == TYPE_STRING1:
            size = self._read(1)[0]
        elif type_ == TYPE_STRING4:
            size = struct.unpack(">i", self._read(4))[0]
        else:
            raise JceDecodeError(f"type mismatch for string tag {tag}: {type_}")
        return self._read(size).decode("utf-8", errors="replace")

    def read_bytes(
        self, tag: int, required: bool = False, default: Optional[bytes] = None
    ) -> Optional[bytes]:
        if not self.skip_to_tag(tag):
            if required:
                raise JceDecodeError(f"required bytes field tag {tag} not found")
            return default
        type_, _ = self.read_head()
        if type_ == TYPE_SIMPLE_LIST:
            inner_type, _ = self.read_head()
            if inner_type != TYPE_BYTE:
                raise JceDecodeError(f"invalid simple list inner type: {inner_type}")
            size = self.read_int(0, required=True)
            return self._read(size)
        if type_ == TYPE_LIST:
            size = self.read_int(0, required=True)
            return bytes(self.read_int(0, required=True) & 0xFF for _ in range(size))
        raise JceDecodeError(f"type mismatch for bytes tag {tag}: {type_}")

    def read_struct(
        self, tag: int, parser: Callable[["JceReader"], T], required: bool = False
    ) -> Optional[T]:
        if not self.skip_to_tag(tag):
            if required:
                raise JceDecodeError(f"required struct field tag {tag} not found")
            return None
        type_, _ = self.read_head()
        if type_ != TYPE_STRUCT_BEGIN:
            raise JceDecodeError(f"type mismatch for struct tag {tag}: {type_}")
        value = parser(self)
        self.skip_to_struct_end()
        return value

    def read_struct_list(
        self, tag: int, parser: Callable[["JceReader"], T], required: bool = False
    ) -> list[T]:
        if not self.skip_to_tag(tag):
            if required:
                raise JceDecodeError(f"required list field tag {tag} not found")
            return []
        type_, _ = self.read_head()
        if type_ != TYPE_LIST:
            raise JceDecodeError(f"type mismatch for list tag {tag}: {type_}")
        size = self.read_int(0, required=True)
        items: list[T] = []
        for _ in range(size):
            item = self.read_struct(0, parser, required=True)
            if item is not None:
                items.append(item)
        return items


def write_tv_time_shift_program_request(writer: JceWriter, pid: str) -> None:
    writer.write_string(pid, 0)


def write_business_extent(writer: JceWriter) -> None:
    writer.write_int(1, 0)


def write_business_head(writer: JceWriter) -> None:
    writer.write_int(0, 0)


def write_qua(writer: JceWriter) -> None:
    writer.write_string("2.9.0.23399", 0)
    writer.write_string("23399", 1)
    writer.write_int(1640, 2)
    writer.write_int(2360, 3)
    writer.write_int(5, 4)
    writer.write_string("sysver=ios17.2&device=iPad&modify_time=&lang=zh_CN", 5)
    writer.write_int(1, 6)
    writer.write_int(0, 7)
    writer.write_int(0, 8)
    writer.write_string("50001", 9)
    writer.write_string("", 10)
    writer.write_string("", 11)
    writer.write_string("", 12)
    writer.write_string("78d6ac7572afb5461e9a590f089f893d2abc0010119007", 13)
    writer.write_string("", 14)
    writer.write_string("A41CD6B6-6A56-46DC-9AE0-4ECF2E9118DF", 16)
    writer.write_string("", 17)
    writer.write_string("", 18)
    writer.write_string("5EE46760-11B1-5FE7-949A-FF232DAE1823", 20)
    writer.write_string("iPad Pro (12.9 inch) 3G", 21)
    writer.write_int(1, 22)
    writer.write_int(0, 23)
    writer.write_int(1, 24)
    writer.write_int(0, 25)
    writer.write_int(0, 26)
    writer.write_string("", 27)
    writer.write_string("", 28)
    writer.write_string("1c0fc6ed8a53584ae722b915200014317601", 29)


def write_request_head(writer: JceWriter, request_id: int, cmd_id: int, guid: str) -> None:
    writer.write_int(request_id, 0)
    writer.write_int(cmd_id, 1)
    writer.write_struct(2, write_qua)
    writer.write_string("1200013", 3)
    writer.write_string(guid, 4)
    writer.write_int(0, 8)
    writer.write_int(0, 9)
    writer.write_int(0, 10)
    writer.write_struct(12, write_business_extent)


def make_request_command(pid: str, request_id: int, cmd_id: int, guid: str) -> bytes:
    body_writer = JceWriter()
    write_tv_time_shift_program_request(body_writer, pid)

    command_writer = JceWriter()
    command_writer.write_struct(
        0, lambda w: write_request_head(w, request_id=request_id, cmd_id=cmd_id, guid=guid)
    )
    command_writer.write_bytes(body_writer.to_bytes(), 1)
    command_writer.write_struct(2, write_business_head)
    return command_writer.to_bytes()


def wrap_unified_protocol(jce_body: bytes, request_id: int, cmd_id: int, guid: str) -> bytes:
    inner_len = len(jce_body) + 17
    inner = bytearray()
    inner.append(38)
    inner.extend(struct.pack(">i", inner_len))
    inner.append(1)
    inner.extend(b"\x00" * 10)
    inner.extend(jce_body)
    inner.append(40)

    compressed_inner = gzip.compress(bytes(inner))

    out = bytearray()
    out.append(19)
    out.extend(struct.pack(">i", 0))
    out.extend(struct.pack(">h", 2))
    out.extend(struct.pack(">H", 0xFF01))
    out.extend(struct.pack(">H", cmd_id))
    out.extend(struct.pack(">h", 0))
    out.extend(struct.pack(">q", request_id))
    out.extend(struct.pack(">i", 531))
    out.extend(struct.pack(">i", 0))
    out.extend(struct.pack(">q", 0))
    out.extend(guid.encode("utf-8")[:32].ljust(32, b"\x00"))
    out.append(0)
    out.extend(struct.pack(">i", 0))
    out.extend(b"\x00" * 6)
    out.append(0)
    out.extend(struct.pack(">h", 0))
    out.extend(struct.pack(">h", 0))
    out.extend(struct.pack(">i", len(inner)))
    out.extend(compressed_inner)
    out.append(3)
    struct.pack_into(">i", out, 1, len(out))
    return bytes(out)


def unwrap_unified_protocol(data: bytes) -> bytes:
    if len(data) < 90:
        raise JceDecodeError(f"response is too short: {len(data)} bytes")
    pos = 0
    version = data[pos]
    pos += 1
    total_len = struct.unpack_from(">i", data, pos)[0]
    pos += 4
    pos += 2
    magic = struct.unpack_from(">H", data, pos)[0]
    pos += 2
    cmd_id = struct.unpack_from(">H", data, pos)[0]
    pos += 2
    error_code = struct.unpack_from(">H", data, pos)[0]
    pos += 2
    pos += 8
    flags = struct.unpack_from(">i", data, pos)[0]
    pos += 4

    if version != 19 or total_len != len(data):
        raise JceDecodeError("invalid unified protocol response header")
    if magic != 0xFF01:
        raise JceDecodeError(f"invalid unified protocol magic: 0x{magic:04x}")
    if error_code != 0:
        raise JceDecodeError(f"server returned unified protocol error: {error_code}")
    if cmd_id != CMD_TV_TIME_SHIFT_PROGRAM:
        raise JceDecodeError(f"unexpected response cmd id: {cmd_id}")

    compressed = bool(flags & 2)
    if compressed and not (flags & 16):
        raise JceDecodeError("response says gzip is used but gzip flag is missing")

    pos += 4
    pos += 8
    pos += 32
    pos += 1
    pos += 10
    pos += 1
    extension_len = struct.unpack_from(">H", data, pos)[0]
    pos += 2 + extension_len
    business_head_len = struct.unpack_from(">H", data, pos)[0]
    pos += 2 + business_head_len
    inner_len = struct.unpack_from(">i", data, pos)[0]
    pos += 4

    if data[-1] != 3:
        raise JceDecodeError("invalid unified protocol tail")
    payload = data[pos:-1]
    inner = gzip.decompress(payload) if compressed else payload

    if len(inner) < 17 or inner[0] != 38 or inner[-1] != 40:
        raise JceDecodeError("invalid inner protocol wrapper")
    if inner_len != len(inner):
        raise JceDecodeError(
            f"inner length mismatch: header={inner_len}, actual={len(inner)}"
        )
    return inner[16:-1]


def parse_response_head(reader: JceReader) -> dict[str, int | str]:
    return {
        "request_id": reader.read_int(0, required=True),
        "cmd_id": reader.read_int(1, required=True),
        "err_code": reader.read_int(2, required=True),
        "user_id": reader.read_string(3, required=True, default="") or "",
    }


def parse_response_command(data: bytes) -> bytes:
    reader = JceReader(data)
    head = reader.read_struct(0, parse_response_head, required=True)
    body = reader.read_bytes(1, required=True)
    if head is None or body is None:
        raise JceDecodeError("response command is missing head or body")
    if int(head["err_code"]) != 0:
        raise JceDecodeError(f"server returned command error: {head['err_code']}")
    if int(head["cmd_id"]) != CMD_TV_TIME_SHIFT_PROGRAM:
        raise JceDecodeError(f"unexpected command body cmd id: {head['cmd_id']}")
    return body


def parse_tv_program(reader: JceReader) -> TVProgram:
    return TVProgram(
        program_id=reader.read_string(0, default="") or "",
        start_time_stamp=reader.read_long(1, default=0),
        epg_time=reader.read_string(2, default="") or "",
        name=reader.read_string(3, default="") or "",
        copy_right=reader.read_int(4, default=0),
        time_shift_flag=reader.read_int(5, default=0),
        duration=reader.read_int(6, default=0),
        pic_screen_flag=reader.read_int(7, default=0),
        video_screen_flag=reader.read_int(8, default=0),
    )


def parse_tv_time_shift_program_response(data: bytes) -> TVTimeShiftProgramResponse:
    reader = JceReader(data)
    return TVTimeShiftProgramResponse(
        errcode=reader.read_int(0, required=True),
        errmsg=reader.read_string(1, default="") or "",
        programs=reader.read_struct_list(2, parse_tv_program),
        time_shift_flag=reader.read_int(3, default=0),
    )


def build_request(pid: str, request_id: int = 1, guid: str = DEFAULT_GUID) -> bytes:
    jce_body = make_request_command(
        pid=pid,
        request_id=request_id,
        cmd_id=CMD_TV_TIME_SHIFT_PROGRAM,
        guid=guid,
    )
    return wrap_unified_protocol(
        jce_body=jce_body,
        request_id=request_id,
        cmd_id=CMD_TV_TIME_SHIFT_PROGRAM,
        guid=guid,
    )


def fetch_epg(
    pid: str,
    url: str = JACC_URL,
    timeout: float = 15.0,
    insecure: bool = False,
) -> TVTimeShiftProgramResponse:
    body = build_request(pid)
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/octet-stream",
            "User-Agent": USER_AGENT,
        },
    )
    context = ssl._create_unverified_context() if insecure else None
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        response_data = response.read()

    command_data = unwrap_unified_protocol(response_data)
    tv_response_body = parse_response_command(command_data)
    parsed = parse_tv_time_shift_program_response(tv_response_body)
    if parsed.errcode != 0:
        raise JceDecodeError(f"EPG API returned {parsed.errcode}: {parsed.errmsg}")
    return parsed


def normalize_timestamp(value: int) -> int:
    if value > 10_000_000_000:
        return value // 1000
    return value


def xmltv_time(timestamp: int) -> str:
    dt = datetime.fromtimestamp(normalize_timestamp(timestamp), CN_TZ)
    return dt.strftime("%Y%m%d%H%M%S %z")


def iter_program_stops(programs: list[TVProgram]) -> Iterable[tuple[TVProgram, Optional[int]]]:
    ordered = sorted(
        (program for program in programs if program.start_time_stamp > 0),
        key=lambda program: normalize_timestamp(program.start_time_stamp),
    )
    for index, program in enumerate(ordered):
        start = normalize_timestamp(program.start_time_stamp)
        stop: Optional[int] = None
        if program.duration > 0:
            stop = start + program.duration
        elif index + 1 < len(ordered):
            next_start = normalize_timestamp(ordered[index + 1].start_time_stamp)
            if next_start > start:
                stop = next_start
        yield program, stop


def write_xmltv(
    programs: list[TVProgram],
    output: Path,
    channel_id: str = CCTV8K_CHANNEL_ID,
    channel_name: str = CCTV8K_CHANNEL_NAME,
) -> None:
    tv = ET.Element("tv", {"generator-info-name": "my-tv Y_JCE EPG"})
    channel = ET.SubElement(tv, "channel", {"id": channel_id})
    ET.SubElement(channel, "display-name", {"lang": "zh"}).text = channel_name

    for program, stop in iter_program_stops(programs):
        attrs = {
            "start": xmltv_time(program.start_time_stamp),
            "channel": channel_id,
        }
        if stop is not None:
            attrs["stop"] = xmltv_time(stop)
        item = ET.SubElement(tv, "programme", attrs)
        ET.SubElement(item, "title", {"lang": "zh"}).text = program.name or program.epg_time
        if program.epg_time:
            ET.SubElement(item, "sub-title", {"lang": "zh"}).text = program.epg_time
        if program.program_id:
            ET.SubElement(item, "episode-num", {"system": "dd_progid"}).text = (
                program.program_id
            )

    if hasattr(ET, "indent"):
        ET.indent(tv, space="  ")
    tree = ET.ElementTree(tv)
    output.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output, encoding="utf-8", xml_declaration=True)


async def get_epgs_ysp_jce(channel: dict) -> dict:
    epgs = []
    msg = ''
    success = 1
    channel_id = channel['id']
    pid = str(channel.get('id0') or CCTV8K_PID)
    try:
        response = await asyncio.to_thread(fetch_epg, pid)
        for program, stop in iter_program_stops(response.programs):
            if stop is None:
                continue
            title = program.name or program.epg_time
            epg = {
                'channel_id': channel_id,
                'starttime': datetime.fromtimestamp(
                    normalize_timestamp(program.start_time_stamp), CN_TZ
                ),
                'endtime': datetime.fromtimestamp(stop, CN_TZ),
                'title': title,
                'desc': program.epg_time if program.name else '',
            }
            epgs.append(epg)
    except Exception as e:
        success = 0
        spidername = Path(__file__).stem
        msg = 'spider-%s-%s' % (spidername, e)
    ret = {
        'success': success,
        'epgs': epgs,
        'msg': msg,
        'ban': 0
    }
    return ret


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch CCTV8K Y_JCE EPG and save it as XMLTV."
    )
    parser.add_argument("--pid", default=CCTV8K_PID, help="YSP pid, default: CCTV8K")
    parser.add_argument("--url", default=JACC_URL, help="Y_JCE endpoint URL")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("cctv8k_epg.xml"),
        help="XMLTV output path",
    )
    parser.add_argument("--channel-id", default=CCTV8K_CHANNEL_ID)
    parser.add_argument("--channel-name", default=CCTV8K_CHANNEL_NAME)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification, useful if local certificates are broken.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        response = fetch_epg(
            pid=args.pid,
            url=args.url,
            timeout=args.timeout,
            insecure=args.insecure,
        )
        write_xmltv(
            response.programs,
            args.output,
            channel_id=args.channel_id,
            channel_name=args.channel_name,
        )
    except (JceDecodeError, OSError, urllib.error.URLError) as exc:
        print(f"Failed to fetch CCTV8K EPG: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {len(response.programs)} programmes to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
