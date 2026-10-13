"""Bounded, offline duration checks before paid transcription.

Accepts PCM/float RIFF WAV, framed Layer III MP3, single-stream Ogg Opus,
single-track WebM Opus (including MediaRecorder's unknown-size segments/clusters
and absent Duration), and MP4 AAC-LC with ordinary sample tables or an
initialization movie followed by timed fragments (Safari MediaRecorder shape).
Edit lists, encrypted media, video/multiple tracks, non-Opus WebM/Ogg and other
codecs fail closed. There is no byte-size fallback or audio decoding.

This is a structural/duration validator, not a codec decoder. Packet/sample
counts bound encoded playback independently of untrusted duration metadata.
Opus pre-skip, end trimming and MP3 encoder padding are deliberately not deducted,
so the result can conservatively exceed audible duration by a few milliseconds.
No content, filename or parsing details are logged or included in errors.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Iterator

from task_service import TaskError

_MAX_BYTES = 2 * 1024 * 1024
_MAX_ITEMS = 100_000
_MAX_SECONDS = 120


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError


def _uint(data: memoryview, start: int, size: int, endian: str = "big") -> int:
    _require(size > 0 and start >= 0 and start + size <= len(data))
    return int.from_bytes(data[start:start + size], endian)


def audio_duration(audio: bytes, mime: str) -> float:
    """Return conservative seconds, or stable ``TaskError`` codes.

    Input must be nonempty bytes, at most 2 MiB, and use one of the five exact
    MIME types. The complete container is checked before returning a result;
    duration over 120 seconds raises ``assist_audio_too_long``. All other
    unsupported, inconsistent, truncated or malformed input raises
    ``assist_audio_invalid``. No I/O, subprocesses or third-party parsers.
    """
    parsers = {
        "audio/webm": _webm,
        "audio/ogg": _ogg,
        "audio/mp4": _mp4,
        "audio/mpeg": _mp3,
        "audio/wav": _wav,
    }
    try:
        _require(isinstance(audio, bytes) and 0 < len(audio) <= _MAX_BYTES)
        _require(isinstance(mime, str) and mime in parsers)
        seconds = parsers[mime](memoryview(audio))
        _require(math.isfinite(seconds) and seconds > 0)
    except (ValueError, IndexError, OverflowError, struct.error):
        raise TaskError("assist_audio_invalid") from None
    if seconds > _MAX_SECONDS:
        raise TaskError("assist_audio_too_long")
    return seconds


def _wav(data: memoryview) -> float:
    _require(data[:4] == b"RIFF" and data[8:12] == b"WAVE")
    _require(_uint(data, 4, 4, "little") + 8 == len(data))
    pos, items = 12, 0
    fmt = None
    samples = None
    while pos < len(data):
        items += 1
        _require(items <= _MAX_ITEMS and pos + 8 <= len(data))
        kind = bytes(data[pos:pos + 4])
        size = _uint(data, pos + 4, 4, "little")
        start, end = pos + 8, pos + 8 + size
        _require(end + (size & 1) <= len(data))
        if kind == b"fmt ":
            _require(fmt is None and size in (16, 18))
            fmt = struct.unpack_from("<HHIIHH", data, start)
            if size == 18:
                _require(_uint(data, start + 16, 2, "little") == 0)
        elif kind == b"data":
            _require(samples is None and size > 0)
            samples = size
        pos = end + (size & 1)
    _require(fmt is not None and samples is not None)
    codec, channels, rate, byte_rate, align, bits = fmt
    _require(codec in (1, 3) and channels in (1, 2))
    _require(8000 <= rate <= 192000)
    _require(bits in ((8, 16, 24, 32) if codec == 1 else (32, 64)))
    _require(align == channels * bits // 8 and byte_rate == rate * align)
    _require(samples % align == 0)
    return samples / byte_rate


def _mp3(data: memoryview) -> float:
    pos = 0
    if data[:3] == b"ID3":
        _require(len(data) >= 10 and data[3] in (3, 4) and data[4] != 255)
        flags = data[5]
        _require(flags & (0x1F if data[3] == 3 else 0x0F) == 0)
        size_bytes = data[6:10]
        _require(all(v < 128 for v in size_bytes))
        size = sum(v << (7 * (3 - i)) for i, v in enumerate(size_bytes))
        pos = 10 + size
        if data[3] == 4 and flags & 0x10:
            _require(data[pos:pos + 3] == b"3DI" and data[pos + 3:pos + 10] == data[3:10])
            pos += 10
        _require(pos <= len(data))
    end = len(data)
    if end - pos >= 128 and data[end - 128:end - 125] == b"TAG":
        end -= 128
    samples, frames, reservoir_bytes = 0, 0, 0
    stream = None
    while pos < end:
        header = _uint(data, pos, 4)
        _require(header >> 21 == 0x7FF)
        version, layer = (header >> 19) & 3, (header >> 17) & 3
        bitrate_index, rate_index = (header >> 12) & 15, (header >> 10) & 3
        _require(version in (0, 2, 3) and layer == 1)
        _require(1 <= bitrate_index <= 14 and rate_index < 3)
        _require(header & 3 != 2)
        rate = (44100, 48000, 32000)[rate_index] // {3: 1, 2: 2, 0: 4}[version]
        rates = ((32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
                 if version == 3 else
                 (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160))
        bitrate = rates[bitrate_index - 1] * 1000
        size = (144 if version == 3 else 72) * bitrate // rate + ((header >> 9) & 1)
        channels = 1 if ((header >> 6) & 3) == 3 else 2
        current = version, rate, channels
        _require(stream is None or stream == current)
        stream = current
        side_info = (17 if channels == 1 else 32) if version == 3 else (9 if channels == 1 else 17)
        crc = 0 if header & (1 << 16) else 2
        _require(size > 4 + crc + side_info and pos + size <= end)
        # A reservoir may refer to preceding frames, never nonexistent bytes.
        side_start = pos + 4 + crc
        reservoir = (_uint(data, side_start, 2) >> 7 if version == 3 else data[side_start])
        _require(reservoir <= min(511 if version == 3 else 255, reservoir_bytes))
        reservoir_bytes += size - 4 - crc - side_info
        frames += 1
        _require(frames <= _MAX_ITEMS)
        samples += 1152 if version == 3 else 576
        pos += size
    _require(frames > 0)
    return samples / stream[1]


def _opus_head(packet: memoryview) -> int:
    _require(len(packet) == 19 and packet[:8] == b"OpusHead")
    _require(packet[8] == 1 and packet[9] in (1, 2) and packet[18] == 0)
    return _uint(packet, 10, 2, "little")


def _opus_samples(packet: memoryview) -> int:
    _require(0 < len(packet) <= 65536)
    toc = packet[0]
    config, code = toc >> 3, toc & 3
    per_frame = ((480, 960, 1920, 2880)[config & 3] if config < 12 else
                 (480, 960)[config & 1] if config < 16 else
                 (120, 240, 480, 960)[config & 3])
    pos, end = 1, len(packet)

    def frame_size() -> int:
        nonlocal pos
        _require(pos < end)
        first = packet[pos]
        pos += 1
        if first < 252:
            return first
        _require(pos < end)
        value = first + 4 * packet[pos]
        pos += 1
        return value

    if code == 0:
        count, sizes = 1, [end - pos]
    elif code == 1:
        _require((end - pos) % 2 == 0)
        count, sizes = 2, [(end - pos) // 2] * 2
    elif code == 2:
        first = frame_size()
        count, sizes = 2, [first, end - pos - first]
    else:
        _require(pos < end)
        control = packet[pos]
        pos += 1
        count = control & 63
        _require(1 <= count <= 48 and count * per_frame <= 5760)
        if control & 64:
            padding = 0
            while True:
                _require(pos < end)
                value = packet[pos]
                pos += 1
                padding += 254 if value == 255 else value
                if value != 255:
                    break
            end -= padding
            _require(pos <= end)
        if control & 128:
            sizes = [frame_size() for _ in range(count - 1)]
            sizes.append(end - pos - sum(sizes))
        else:
            _require((end - pos) % count == 0)
            sizes = [(end - pos) // count] * count
    _require(all(0 <= size <= 1275 for size in sizes))
    _require(count * per_frame <= 5760)
    return count * per_frame


def _crc_table() -> tuple[int, ...]:
    table = []
    for value in range(256):
        crc = value << 24
        for _ in range(8):
            crc = ((crc << 1) ^ (0x04C11DB7 if crc & 0x80000000 else 0)) & 0xFFFFFFFF
        table.append(crc)
    return tuple(table)


_OGG_CRC = _crc_table()


def _ogg(data: memoryview) -> float:
    pos, sequence, packets, total, last_samples = 0, 0, 0, 0, 0
    serial = None
    pending = bytearray()
    eos = False
    preskip = 0
    while pos < len(data):
        _require(not eos and sequence < _MAX_ITEMS and pos + 27 <= len(data))
        _require(data[pos:pos + 4] == b"OggS" and data[pos + 4] == 0)
        flags = data[pos + 5]
        _require(flags & ~7 == 0 and bool(flags & 1) == bool(pending))
        _require(bool(flags & 2) == (sequence == 0))
        page_serial = _uint(data, pos + 14, 4, "little")
        _require(serial is None or serial == page_serial)
        serial = page_serial
        _require(_uint(data, pos + 18, 4, "little") == sequence)
        segments = data[pos + 26]
        body = pos + 27 + segments
        _require(segments > 0 and body <= len(data))
        laces = data[pos + 27:body]
        end = body + sum(laces)
        _require(end <= len(data))
        crc = 0
        for index in range(pos, end):
            value = 0 if pos + 22 <= index < pos + 26 else data[index]
            crc = ((crc << 8) & 0xFFFFFFFF) ^ _OGG_CRC[(crc >> 24) ^ value]
        _require(crc == _uint(data, pos + 22, 4, "little"))
        completed = False
        for size in laces:
            pending.extend(data[body:body + size])
            body += size
            _require(len(pending) <= 65536)
            if size == 255:
                continue
            packet = memoryview(bytes(pending))
            pending.clear()
            if packets == 0:
                preskip = _opus_head(packet)
                _require(sequence == 0 and segments == 1)
            elif packets == 1:
                _require(packet[:8] == b"OpusTags" and len(packet) >= 16)
                vendor_size = _uint(packet, 8, 4, "little")
                cursor = 12 + vendor_size
                comments = _uint(packet, cursor, 4, "little")
                cursor += 4
                _require(comments <= _MAX_ITEMS)
                for _ in range(comments):
                    size = _uint(packet, cursor, 4, "little")
                    cursor += 4 + size
                    _require(cursor <= len(packet))
                _require(cursor <= len(packet))
            else:
                last_samples = _opus_samples(packet)
                total += last_samples
            packets += 1
            _require(packets <= _MAX_ITEMS)
            completed = True
        granule = _uint(data, pos + 6, 8, "little")
        eos = bool(flags & 4)
        if eos:
            _require(not pending and packets > 2 and completed)
            _require(max(preskip, total - last_samples) <= granule <= total)
        elif packets <= 2:
            _require(granule == (0 if completed else 0xFFFFFFFFFFFFFFFF))
        else:
            _require(granule == (total if completed else 0xFFFFFFFFFFFFFFFF))
        sequence += 1
        pos = end
    _require(eos and not pending and total > preskip)
    return total / 48000


def _vint(data: memoryview, pos: int, *, identifier: bool = False) -> tuple[int, int, bool]:
    _require(pos < len(data) and data[pos] != 0)
    first = data[pos]
    width = 1
    mask = 0x80
    while not first & mask:
        width += 1
        mask >>= 1
    _require(width <= (4 if identifier else 8) and pos + width <= len(data))
    value = _uint(data, pos, width)
    if not identifier:
        value &= (1 << (7 * width)) - 1
    return value, pos + width, not identifier and value == (1 << (7 * width)) - 1


_SEGMENT_IDS = {0x1549A966, 0x1654AE6B, 0x1F43B675, 0x1C53BB6B,
                0x114D9B74, 0x1254C367, 0x1941A469, 0x1043A770}


def _ebml(data: memoryview, start: int, end: int, *, segment: bool = False
          ) -> Iterator[tuple[int, memoryview]]:
    pos, count = start, 0
    while pos < end:
        kind, cursor, _ = _vint(data, pos, identifier=True)
        size, body, unknown = _vint(data, cursor)
        stop = body + size
        if unknown:
            _require(segment and kind == 0x1F43B675)
            stop = body
            while stop < end:
                child, child_cursor, _ = _vint(data, stop, identifier=True)
                if child in _SEGMENT_IDS:
                    break
                child_size, child_body, child_unknown = _vint(data, child_cursor)
                _require(not child_unknown and child_body + child_size <= end)
                stop = child_body + child_size
        _require(body <= stop <= end)
        count += 1
        _require(count <= _MAX_ITEMS)
        yield kind, data[body:stop]
        pos = stop
    _require(pos == end)


def _fields(data: memoryview) -> dict[int, memoryview]:
    fields = {}
    for kind, payload in _ebml(data, 0, len(data)):
        _require(kind not in fields)
        fields[kind] = payload
    return fields


def _integer(data: memoryview) -> int:
    _require(1 <= len(data) <= 8)
    return int.from_bytes(data, "big")


def _float(data: memoryview) -> float:
    _require(len(data) in (4, 8))
    value = struct.unpack(">f" if len(data) == 4 else ">d", data)[0]
    _require(math.isfinite(value) and value >= 0)
    return value


def _block_packets(block: memoryview) -> tuple[int, int, list[memoryview]]:
    track, pos, unknown = _vint(block, 0)
    _require(not unknown and pos + 3 < len(block))
    timestamp = int.from_bytes(block[pos:pos + 2], "big", signed=True)
    flags = block[pos + 2]
    _require(flags & 0x70 == 0)
    pos += 3
    lacing = (flags >> 1) & 3
    if not lacing:
        return track, timestamp, [block[pos:]]
    count = block[pos] + 1
    pos += 1
    _require(2 <= count <= 256)
    sizes = []
    if lacing == 1:
        for _ in range(count - 1):
            size = 0
            while True:
                _require(pos < len(block))
                value = block[pos]
                pos += 1
                size += value
                if value != 255:
                    break
            sizes.append(size)
    elif lacing == 2:
        _require((len(block) - pos) % count == 0)
        sizes = [(len(block) - pos) // count] * (count - 1)
    else:
        first, pos, unknown = _vint(block, pos)
        _require(not unknown)
        sizes.append(first)
        for _ in range(count - 2):
            old = pos
            delta, pos, unknown = _vint(block, pos)
            _require(not unknown)
            sizes.append(sizes[-1] + delta - ((1 << (7 * (pos - old) - 1)) - 1))
    sizes.append(len(block) - pos - sum(sizes))
    _require(all(size > 0 for size in sizes))
    packets = []
    for size in sizes:
        _require(pos + size <= len(block))
        packets.append(block[pos:pos + size])
        pos += size
    return track, timestamp, packets


def _webm(data: memoryview) -> float:
    kind, pos, _ = _vint(data, 0, identifier=True)
    _require(kind == 0x1A45DFA3)
    size, body, unknown = _vint(data, pos)
    _require(not unknown and body + size < len(data))
    header = _fields(data[body:body + size])
    _require(bytes(header.get(0x4282, b"")) == b"webm")
    kind, pos, _ = _vint(data, body + size, identifier=True)
    _require(kind == 0x18538067)
    size, body, unknown = _vint(data, pos)
    _require(unknown or body + size == len(data))
    info, tracks, clusters = None, None, []
    for kind, payload in _ebml(data, body, len(data), segment=True):
        if kind == 0x1549A966:
            _require(info is None)
            info = _fields(payload)
        elif kind == 0x1654AE6B:
            _require(tracks is None)
            entries = list(_ebml(payload, 0, len(payload)))
            _require(len(entries) == 1 and entries[0][0] == 0xAE)
            tracks = _fields(entries[0][1])
        elif kind == 0x1F43B675:
            clusters.append(payload)
        else:
            _require(kind in _SEGMENT_IDS or kind in (0xEC, 0xBF))
    _require(info is not None and tracks is not None and clusters)
    scale = _integer(info[0x2AD7B1]) if 0x2AD7B1 in info else 1_000_000
    _require(0 < scale <= 1_000_000_000)
    _require(_integer(tracks.get(0x83, memoryview(b""))) == 2)
    _require(bytes(tracks.get(0x86, b"")) == b"A_OPUS")
    _require(0x63A2 in tracks and 0xE1 in tracks and 0xD7 in tracks)
    _opus_head(tracks[0x63A2])
    track = _integer(tracks[0xD7])
    _require(track > 0)
    audio = _fields(tracks[0xE1])
    _require(0xB5 in audio and _float(audio[0xB5]) == 48000)
    channels = _integer(audio[0x9F]) if 0x9F in audio else 1
    _require(channels == tracks[0x63A2][9])
    for forbidden in (0x6D80, 0x23314F, 0x537F):
        _require(forbidden not in tracks)
    if 0xB9 in tracks:
        _require(_integer(tracks[0xB9]) == 1)
    # Integer units avoid rounding an exactly 120-second packet stream upward.
    units_per_second = 48000 * 1_000_000_000
    maximum, total, previous, blocks = 0, 0, -1, 0
    for cluster in clusters:
        timestamp = None
        for kind, payload in _ebml(cluster, 0, len(cluster)):
            if kind == 0xE7:
                _require(timestamp is None)
                timestamp = _integer(payload)
                continue
            if kind in (0xEC, 0xBF, 0xA7, 0xAB):
                continue
            _require(kind in (0xA3, 0xA0) and timestamp is not None)
            declared = 0
            if kind == 0xA0:
                group = _fields(payload)
                _require(0xA1 in group and not any(k in group for k in (0xA6, 0x75A1)))
                if 0x9B in group:
                    declared = _integer(group[0x9B]) * scale * 48000
                payload = group[0xA1]
            block_track, relative, packets = _block_packets(payload)
            _require(block_track == track)
            start = (timestamp + relative) * scale * 48000
            _require(start >= 0 and start >= previous)
            previous = start
            duration = sum(_opus_samples(packet) for packet in packets) * 1_000_000_000
            duration = max(duration, declared)
            total += duration
            maximum = max(maximum, start + duration)
            blocks += len(packets)
            _require(blocks <= _MAX_ITEMS)
        _require(timestamp is not None)
    _require(blocks > 0)
    seconds = max(maximum, total) / units_per_second
    if 0x4489 in info:
        seconds = max(seconds, _float(info[0x4489]) * scale / 1e9)
    return seconds


def _boxes(data: memoryview, start: int = 0, end: int | None = None
           ) -> Iterator[tuple[bytes, int, int]]:
    end = len(data) if end is None else end
    pos, count = start, 0
    while pos < end:
        _require(pos + 8 <= end)
        size = _uint(data, pos, 4)
        kind = bytes(data[pos + 4:pos + 8])
        header = 8
        if size == 1:
            size = _uint(data, pos + 8, 8)
            header = 16
        elif size == 0:
            size = end - pos
        _require(header <= size and pos + size <= end)
        count += 1
        _require(count <= _MAX_ITEMS)
        yield kind, pos + header, pos + size
        pos += size


def _box_fields(data: memoryview) -> dict[bytes, memoryview]:
    fields = {}
    for kind, start, end in _boxes(data):
        _require(kind not in fields)
        fields[kind] = data[start:end]
    return fields


def _fullbox(data: memoryview, version: int = 0) -> memoryview:
    _require(len(data) >= 4 and data[:4] == bytes((version, 0, 0, 0)))
    return data[4:]


def _descriptor(data: memoryview, pos: int) -> tuple[int, memoryview, int]:
    _require(pos < len(data))
    tag = data[pos]
    pos += 1
    size = 0
    for index in range(4):
        _require(pos < len(data))
        value = data[pos]
        pos += 1
        size = (size << 7) | (value & 127)
        if value < 128:
            break
        _require(index < 3)
    _require(pos + size <= len(data))
    return tag, data[pos:pos + size], pos + size


def _aac_config(esds: memoryview, rate: int, channels: int) -> None:
    data = _fullbox(esds)
    tag, es, end = _descriptor(data, 0)
    _require(tag == 3 and end == len(data) and len(es) >= 3 and es[2] == 0)
    tag, config, cursor = _descriptor(es, 3)
    _require(tag == 4 and len(config) >= 13 and config[0] == 0x40 and config[1] == 0x15)
    tag, asc, config_end = _descriptor(config, 13)
    _require(tag == 5 and config_end == len(config) and len(asc) == 2)
    bits = _uint(asc, 0, 2)
    frequency = (bits >> 7) & 15
    rates = (96000, 88200, 64000, 48000, 44100, 32000, 24000,
             22050, 16000, 12000, 11025, 8000, 7350)
    _require(bits >> 11 == 2 and frequency < len(rates))
    _require(rates[frequency] == rate and ((bits >> 3) & 15) == channels and bits & 7 == 0)
    tag, sl, end = _descriptor(es, cursor)
    _require(tag == 6 and bytes(sl) == b"\x02" and end == len(es))


def _mp4(data: memoryview) -> float:
    movie = None
    mdats = []
    fragments = []
    box_start = 0
    ftyp = False
    for kind, start, end in _boxes(data):
        if kind == b"ftyp":
            _require(not ftyp and end - start >= 8 and (end - start) % 4 == 0)
            brands = [bytes(data[start:start + 4])]
            brands += [bytes(data[p:p + 4]) for p in range(start + 8, end, 4)]
            _require(any(brand in (b"M4A ", b"isom", b"iso2", b"iso6", b"mp41",
                                  b"mp42", b"qt  ") for brand in brands))
            ftyp = True
        elif kind == b"moov":
            _require(movie is None and not fragments)
            movie = data[start:end]
        elif kind == b"moof":
            _require(movie is not None)
            fragments.append((box_start, data[start:end]))
            _require(len(fragments) <= _MAX_ITEMS)
        elif kind == b"mdat":
            _require(end > start)
            mdats.append((start, end))
        else:
            _require(kind in (b"free", b"skip", b"wide", b"styp"))
            if kind == b"styp":
                _require(end - start >= 8 and (end - start) % 4 == 0)
        box_start = end
    _require(ftyp and movie is not None and mdats)
    moov = _box_fields(movie)
    _require(b"trak" in moov and b"mvhd" in moov)
    _require(bool(fragments) == (b"mvex" in moov))
    mvhd = moov[b"mvhd"]
    _require(mvhd[0] in (0, 1))
    movie_timing = _fullbox(mvhd, mvhd[0])
    _require(len(movie_timing) == (96 if mvhd[0] == 0 else 108))
    movie_offset = 8 if mvhd[0] == 0 else 16
    movie_scale = _uint(movie_timing, movie_offset, 4)
    movie_duration = _uint(movie_timing, movie_offset + 4, 4 if mvhd[0] == 0 else 8)
    _require(movie_scale > 0)
    track = _box_fields(moov[b"trak"])
    _require(b"edts" not in track and b"mdia" in track and b"tkhd" in track)
    tkhd = track[b"tkhd"]
    _require(len(tkhd) in (84, 96) and tkhd[0] in (0, 1))
    _require(len(tkhd) == (84 if tkhd[0] == 0 else 96))
    _require(_uint(tkhd, 1, 3) & ~7 == 0)
    track_offset = 12 if tkhd[0] == 0 else 20
    track_id = _uint(tkhd, track_offset, 4)
    _require(track_id > 0)
    track_duration = _uint(tkhd, track_offset + 8, 4 if tkhd[0] == 0 else 8)
    _require(tkhd[-8:] == b"\x00" * 8)
    mdia = _box_fields(track[b"mdia"])
    _require(all(key in mdia for key in (b"mdhd", b"hdlr", b"minf")))
    hdlr = _fullbox(mdia[b"hdlr"])
    _require(len(hdlr) >= 20 and hdlr[4:8] == b"soun")
    mdhd = mdia[b"mdhd"]
    _require(mdhd[0] in (0, 1))
    timing = _fullbox(mdhd, mdhd[0])
    offset = 8 if mdhd[0] == 0 else 16
    _require(len(timing) == (20 if mdhd[0] == 0 else 32))
    timescale = _uint(timing, offset, 4)
    declared = _uint(timing, offset + 4, 4 if mdhd[0] == 0 else 8)
    _require(timescale > 0)
    minf = _box_fields(mdia[b"minf"])
    _require(b"stbl" in minf and b"dinf" in minf and b"smhd" in minf)
    _require(bytes(_fullbox(minf[b"smhd"])) == b"\x00" * 4)
    dinf = _box_fields(minf[b"dinf"])
    _require(b"dref" in dinf)
    dref = _fullbox(dinf[b"dref"])
    _require(_uint(dref, 0, 4) == 1)
    refs = list(_boxes(dref, 4))
    _require(len(refs) == 1 and refs[0][0] == b"url ")
    _require(bytes(dref[refs[0][1]:refs[0][2]]) == b"\x00\x00\x00\x01")
    table = _box_fields(minf[b"stbl"])
    _require(all(key in table for key in (b"stsd", b"stts", b"stsc", b"stsz")))
    _require(not any(key in table for key in (b"ctts", b"stz2", b"senc", b"saiz", b"saio")))
    stsd = _fullbox(table[b"stsd"])
    _require(_uint(stsd, 0, 4) == 1)
    entries = list(_boxes(stsd, 4))
    _require(len(entries) == 1 and entries[0][0] == b"mp4a")
    entry = stsd[entries[0][1]:entries[0][2]]
    _require(len(entry) >= 28 and entry[:6] == b"\x00" * 6)
    _require(_uint(entry, 6, 2) == 1 and entry[8:16] == b"\x00" * 8)
    channels = _uint(entry, 16, 2)
    _require(channels in (1, 2) and _uint(entry, 18, 2) == 16)
    fixed_rate = _uint(entry, 24, 4)
    _require(fixed_rate & 65535 == 0)
    rate = fixed_rate >> 16
    children = _box_fields(entry[28:])
    _require(b"esds" in children and b"sinf" not in children)
    _aac_config(children[b"esds"], rate, channels)
    if fragments:
        _empty_fragment_tables(table)
        seconds = _fragment_duration(data, fragments, mdats, moov[b"mvex"],
                                     track_id, timescale, rate)
        # Fragment init headers may explicitly mark duration unknown. Actual
        # trun durations and tfdt timeline are still mandatory and authoritative.
        if declared == (1 << (32 if mdhd[0] == 0 else 64)) - 1:
            declared = 0
        if movie_duration == (1 << (32 if mvhd[0] == 0 else 64)) - 1:
            movie_duration = 0
        if track_duration == (1 << (32 if tkhd[0] == 0 else 64)) - 1:
            track_duration = 0
        extension = _box_fields(moov[b"mvex"])
        if b"mehd" in extension:
            mehd = extension[b"mehd"]
            _require(len(mehd) >= 4 and mehd[0] in (0, 1))
            timing = _fullbox(mehd, mehd[0])
            _require(len(timing) == (4 if mehd[0] == 0 else 8))
            duration = _uint(timing, 0, len(timing))
            if duration != (1 << (8 * len(timing))) - 1:
                seconds = max(seconds, duration / movie_scale)
        return max(seconds, declared / timescale,
                   movie_duration / movie_scale, track_duration / movie_scale)
    sizes_data = _fullbox(table[b"stsz"])
    default_size, count = _uint(sizes_data, 0, 4), _uint(sizes_data, 4, 4)
    _require(0 < count <= _MAX_ITEMS)
    _require(len(sizes_data) == 8 + (0 if default_size else count * 4))
    sizes = ([default_size] * count if default_size else
             [_uint(sizes_data, 8 + i * 4, 4) for i in range(count)])
    _require(all(size > 0 for size in sizes))
    stts = _fullbox(table[b"stts"])
    runs = _uint(stts, 0, 4)
    _require(0 < runs <= count and len(stts) == 4 + runs * 8)
    sample_count, ticks = 0, 0
    for index in range(runs):
        n, delta = _uint(stts, 4 + index * 8, 4), _uint(stts, 8 + index * 8, 4)
        _require(n > 0 and delta > 0)
        sample_count += n
        ticks += n * delta
    _require(sample_count == count and declared == ticks)
    _require((b"stco" in table) != (b"co64" in table))
    wide = b"co64" in table
    offsets_data = _fullbox(table[b"co64" if wide else b"stco"])
    chunks = _uint(offsets_data, 0, 4)
    width = 8 if wide else 4
    _require(0 < chunks <= count and len(offsets_data) == 4 + chunks * width)
    offsets = [_uint(offsets_data, 4 + index * width, width) for index in range(chunks)]
    stsc = _fullbox(table[b"stsc"])
    runs = _uint(stsc, 0, 4)
    _require(0 < runs <= chunks and len(stsc) == 4 + runs * 12)
    mapping = []
    for index in range(runs):
        first = _uint(stsc, 4 + index * 12, 4)
        n = _uint(stsc, 8 + index * 12, 4)
        description = _uint(stsc, 12 + index * 12, 4)
        _require(0 < first <= chunks and 0 < n <= count and description == 1)
        _require((not mapping and first == 1) or (mapping and first > mapping[-1][0]))
        mapping.append((first, n))
    sample, run = 0, 0
    ranges = []
    for index, chunk_offset in enumerate(offsets, 1):
        if run + 1 < len(mapping) and index >= mapping[run + 1][0]:
            run += 1
        n = mapping[run][1]
        _require(sample + n <= count)
        stop = chunk_offset + sum(sizes[sample:sample + n])
        ranges.append((chunk_offset, stop))
        sample += n
    _require(sample == count)
    _media_coverage(ranges, mdats)
    return max(ticks / timescale, count * 1024 / rate,
               movie_duration / movie_scale, track_duration / movie_scale)


def _media_coverage(ranges: list[tuple[int, int]], mdats: list[tuple[int, int]]) -> None:
    _require(bool(ranges) and bool(mdats))
    ranges.sort()
    # Every media byte belongs to exactly one referenced audio chunk.
    cursor, media_index = mdats[0][0], 0
    for start, end in ranges:
        if cursor == mdats[media_index][1] and media_index + 1 < len(mdats):
            media_index += 1
            cursor = mdats[media_index][0]
        _require(start == cursor and end <= mdats[media_index][1])
        cursor = end
    _require(media_index == len(mdats) - 1 and cursor == mdats[-1][1])


def _empty_fragment_tables(table: dict[bytes, memoryview]) -> None:
    for key in (b"stts", b"stsc"):
        _require(bytes(_fullbox(table[key])) == b"\x00" * 4)
    _require(bytes(_fullbox(table[b"stsz"])) == b"\x00" * 8)
    _require((b"stco" in table) != (b"co64" in table))
    _require(bytes(_fullbox(table[b"stco" if b"stco" in table else b"co64"])) == b"\x00" * 4)


def _fragment_header(payload: memoryview, mask: int, versions: tuple[int, ...] = (0,)
                     ) -> tuple[int, memoryview]:
    _require(len(payload) >= 4 and payload[0] in versions)
    flags = _uint(payload, 1, 3)
    _require(flags & ~mask == 0)
    return flags, payload[4:]


def _fragment_duration(
    data: memoryview, fragments: list[tuple[int, memoryview]],
    mdats: list[tuple[int, int]], mvex: memoryview,
    track_id: int, timescale: int, rate: int,
) -> float:
    defaults = _box_fields(mvex)
    _require(b"trex" in defaults and set(defaults) <= {b"trex", b"mehd"})
    trex = _fullbox(defaults[b"trex"])
    _require(len(trex) == 20 and _uint(trex, 0, 4) == track_id)
    _require(_uint(trex, 4, 4) == 1)
    default_duration = _uint(trex, 8, 4)
    default_size = _uint(trex, 12, 4)
    samples, total_ticks, maximum, previous_end = 0, 0, 0, 0
    previous_sequence = None
    ranges = []
    for moof_start, fragment in fragments:
        mfhd = None
        traf = None
        for kind, start, end in _boxes(fragment):
            if kind == b"mfhd":
                _require(mfhd is None)
                mfhd = _fullbox(fragment[start:end])
            elif kind == b"traf":
                _require(traf is None)
                traf = fragment[start:end]
            else:
                _require(kind == b"free")
        _require(mfhd is not None and len(mfhd) == 4 and traf is not None)
        sequence = _uint(mfhd, 0, 4)
        _require(previous_sequence is None or sequence == previous_sequence + 1)
        previous_sequence = sequence
        tfhd, tfdt, runs = None, None, []
        for kind, start, end in _boxes(traf):
            if kind == b"tfhd":
                _require(tfhd is None and not runs)
                tfhd = traf[start:end]
            elif kind == b"tfdt":
                _require(tfdt is None and not runs)
                tfdt = traf[start:end]
            elif kind == b"trun":
                runs.append(traf[start:end])
            else:
                _require(kind == b"free")
        _require(tfhd is not None and tfdt is not None and runs)
        # One track/traf only: the ISO implicit first-traf base is the moof.
        flags, header = _fragment_header(tfhd, 0x02003B)
        _require(_uint(header, 0, 4) == track_id)
        cursor = 4
        base = moof_start
        if flags & 1:
            _require(not flags & 0x020000)
            base = _uint(header, cursor, 8)
            cursor += 8
        if flags & 2:
            _require(_uint(header, cursor, 4) == 1)
            cursor += 4
        duration, size = default_duration, default_size
        if flags & 8:
            duration = _uint(header, cursor, 4)
            cursor += 4
        if flags & 16:
            size = _uint(header, cursor, 4)
            cursor += 4
        if flags & 32:
            _uint(header, cursor, 4)  # Default sample flags do not change duration.
            cursor += 4
        _require(cursor == len(header))
        _, decode_data = _fragment_header(tfdt, 0, (0, 1))
        _require(len(decode_data) == (4 if tfdt[0] == 0 else 8))
        decode = _uint(decode_data, 0, len(decode_data))
        _require(decode >= previous_end)
        ticks = 0
        data_end = None
        for run in runs:
            run_flags, fields = _fragment_header(run, 0x000F05, (0, 1))
            count = _uint(fields, 0, 4)
            _require(0 < count <= _MAX_ITEMS - samples)
            samples += count
            cursor = 4
            if run_flags & 1:
                offset = int.from_bytes(fields[cursor:cursor + 4], "big", signed=True)
                _require(cursor + 4 <= len(fields))
                cursor += 4
                data_start = base + offset
            else:
                data_start = base if data_end is None else data_end
            if run_flags & 4:
                _require(not run_flags & 0x400)
                _uint(fields, cursor, 4)
                cursor += 4
            width = 4 * sum(bool(run_flags & bit) for bit in (0x100, 0x200, 0x400, 0x800))
            _require(len(fields) == cursor + count * width)
            payload_size = 0
            for _ in range(count):
                sample_duration, sample_size = duration, size
                if run_flags & 0x100:
                    sample_duration = _uint(fields, cursor, 4)
                    cursor += 4
                if run_flags & 0x200:
                    sample_size = _uint(fields, cursor, 4)
                    cursor += 4
                if run_flags & 0x400:
                    _uint(fields, cursor, 4)
                    cursor += 4
                if run_flags & 0x800:
                    # AAC has no reordered presentation; unknown shifts fail closed.
                    _require(_uint(fields, cursor, 4) == 0)
                    cursor += 4
                _require(sample_duration > 0 and sample_size > 0)
                ticks += sample_duration
                payload_size += sample_size
            data_end = data_start + payload_size
            _require(0 <= data_start < data_end <= len(data))
            ranges.append((data_start, data_end))
        total_ticks += ticks
        previous_end = decode + ticks
        maximum = max(maximum, previous_end)
    _media_coverage(ranges, mdats)
    return max(maximum / timescale, total_ticks / timescale, samples * 1024 / rate)
