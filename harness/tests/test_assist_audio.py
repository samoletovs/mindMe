"""Only manufactured containers/packets; no recordings or personal audio."""

import struct
import random

import pytest

from assist_audio import audio_duration
from task_service import TaskError


def wav(samples=800, rate=8000, *, channels=1, bits=8, codec=1):
    align = channels * bits // 8
    fmt = struct.pack("<HHIIHH", codec, channels, rate, rate * align, align, bits)
    payload = b"\x00" * (samples * align)
    body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(payload)) + payload
    if len(payload) & 1:
        body += b"\x00"
    return b"RIFF" + struct.pack("<I", len(body)) + body


def mp3(frames=4, *, version=3, rate_index=0, bitrate_index=9):
    header = (0x7FF << 21 | version << 19 | 1 << 17 | 1 << 16 |
              bitrate_index << 12 | rate_index << 10)
    rate = (44100, 48000, 32000)[rate_index] // {3: 1, 2: 2, 0: 4}[version]
    bitrate = (128000 if version == 3 else 80000)
    size = (144 if version == 3 else 72) * bitrate // rate
    return (header.to_bytes(4, "big") + b"\x00" * (size - 4)) * frames


OPUS_HEAD = b"OpusHead\x01\x01" + struct.pack("<HIhB", 312, 48000, 0, 0)
OPUS_TAGS = b"OpusTags" + struct.pack("<II", 0, 0)
OPUS_PACKET = b"\xf8\xff\xfe"  # Manufactured one-frame 20 ms packet.


def ogg_crc(data):
    crc = 0
    for value in data:
        crc ^= value << 24
        for _ in range(8):
            crc = ((crc << 1) ^ (0x04C11DB7 if crc & 0x80000000 else 0)) & 0xFFFFFFFF
    return crc


def ogg_page(sequence, packets, granule, flags=0, *, serial=7):
    laces, body = [], b""
    for packet in packets:
        laces.extend([255] * (len(packet) // 255) + [len(packet) % 255])
        body += packet
    header = b"OggS\x00" + bytes([flags]) + struct.pack("<QII", granule, serial, sequence)
    header += b"\x00" * 4 + bytes([len(laces)]) + bytes(laces)
    page = header + body
    return page[:22] + struct.pack("<I", ogg_crc(page)) + page[26:]


def ogg(frames=5, *, final_granule=None):
    result = ogg_page(0, [OPUS_HEAD], 0, 2) + ogg_page(1, [OPUS_TAGS], 0)
    sequence = 2
    for start in range(0, frames, 100):
        count = min(100, frames - start)
        last = start + count == frames
        granule = (start + count) * 960
        if last and final_granule is not None:
            granule = final_granule
        result += ogg_page(sequence, [OPUS_PACKET] * count, granule, 4 if last else 0)
        sequence += 1
    return result


def vint(value):
    for width in range(1, 9):
        if value < (1 << (7 * width)) - 1:
            return ((1 << (7 * width)) | value).to_bytes(width, "big")
    raise ValueError


def ebml(kind, payload, *, unknown=False):
    identifier = kind.to_bytes((kind.bit_length() + 7) // 8, "big")
    return identifier + (b"\xff" if unknown else vint(len(payload))) + payload


def integer(kind, value):
    return ebml(kind, value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big"))


def webm(timestamps=(0, 20, 40), *, duration=None, unknown=True, lace=0,
         group_duration=None):
    header = ebml(0x1A45DFA3, ebml(0x4282, b"webm"))
    info = integer(0x2AD7B1, 1_000_000)
    if duration is not None:
        info += ebml(0x4489, struct.pack(">d", duration))
    audio = ebml(0xB5, struct.pack(">d", 48000)) + integer(0x9F, 1)
    track = (integer(0xD7, 1) + integer(0x83, 2) + ebml(0x86, b"A_OPUS") +
             ebml(0x63A2, OPUS_HEAD) + ebml(0xE1, audio))
    segment = ebml(0x1549A966, info) + ebml(0x1654AE6B, ebml(0xAE, track))
    for timestamp in timestamps:
        packet = OPUS_PACKET
        flags = 0x80 | lace << 1
        if lace == 1:
            packet = b"\x01\x03" + OPUS_PACKET * 2
        elif lace == 2:
            packet = b"\x01" + OPUS_PACKET * 2
        elif lace == 3:
            packet = b"\x01" + vint(3) + OPUS_PACKET * 2
        block = b"\x81\x00\x00" + bytes([flags]) + packet
        encoded_block = ebml(0xA3, block)
        if group_duration is not None:
            encoded_block = ebml(0xA0, ebml(0xA1, block) + integer(0x9B, group_duration))
        cluster = integer(0xE7, timestamp) + encoded_block
        segment += ebml(0x1F43B675, cluster, unknown=unknown)
    return header + ebml(0x18538067, segment, unknown=unknown)


def box(kind, payload):
    return struct.pack(">I", len(payload) + 8) + kind + payload


def fullbox(kind, payload):
    return box(kind, b"\x00" * 4 + payload)


def descriptor(tag, payload):
    assert len(payload) < 128
    return bytes([tag, len(payload)]) + payload


def mp4(samples=5, *, delta=1024, declared=None, offset_shift=0, edits=False,
        fragmented=False, trex_duration=0, trex_size=0):
    ftyp = box(b"ftyp", b"M4A \x00\x00\x00\x00isom")
    mdat = box(b"mdat", b"\x00" * samples * 4)
    asc = b"\x11\x88"  # AAC-LC, 48 kHz, mono, 1024 samples/frame.
    decoder = b"\x40\x15" + b"\x00" * 11 + descriptor(5, asc)
    es = b"\x00\x01\x00" + descriptor(4, decoder) + descriptor(6, b"\x02")
    esds = fullbox(b"esds", descriptor(3, es))
    entry = b"\x00" * 6 + b"\x00\x01" + b"\x00" * 8
    entry += struct.pack(">HHHHI", 1, 16, 0, 0, 48000 << 16) + esds
    stsd = fullbox(b"stsd", struct.pack(">I", 1) + box(b"mp4a", entry))
    stts = fullbox(b"stts", struct.pack(">III", 1, samples, delta))
    stsz = fullbox(b"stsz", struct.pack(">II", 4, samples))
    stsc = fullbox(b"stsc", struct.pack(">IIII", 1, 1, samples, 1))
    stco = fullbox(b"stco", struct.pack(">II", 1, len(ftyp) + 8 + offset_shift))
    if fragmented:
        stts = fullbox(b"stts", b"\x00" * 4)
        stsc = fullbox(b"stsc", b"\x00" * 4)
        stco = fullbox(b"stco", b"\x00" * 4)
        stsz = fullbox(b"stsz", b"\x00" * 8)
    stbl = box(b"stbl", stsd + stts + stsz + stsc + stco)
    dref = fullbox(b"dref", struct.pack(">I", 1) + box(b"url ", b"\x00\x00\x00\x01"))
    minf = box(b"minf", fullbox(b"smhd", b"\x00" * 4) + box(b"dinf", dref) + stbl)
    duration = samples * delta if declared is None else declared
    mdhd = fullbox(b"mdhd", struct.pack(">IIIII", 0, 0, 48000, duration, 0))
    hdlr = fullbox(b"hdlr", b"\x00" * 4 + b"soun" + b"\x00" * 12)
    matrix = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    tkhd = b"\x00\x00\x00\x03" + struct.pack(">IIIII", 0, 0, 1, 0, duration)
    tkhd += b"\x00" * 12 + b"\x01\x00\x00\x00" + matrix + b"\x00" * 8
    mvhd = struct.pack(">IIIII", 0, 0, 48000, duration, 0x10000)
    mvhd += b"\x01\x00" + b"\x00" * 10 + matrix + b"\x00" * 24 + struct.pack(">I", 2)
    trak = box(b"trak", box(b"tkhd", tkhd) + box(b"mdia", mdhd + hdlr + minf) +
               (box(b"edts", b"") if edits else b""))
    mvex = b""
    if fragmented:
        trex = fullbox(b"trex", struct.pack(">IIIII", 1, 1, trex_duration, trex_size, 0))
        mvex = box(b"mvex", trex)
        mdat = b""
    return ftyp + mdat + box(b"moov", fullbox(b"mvhd", mvhd) + trak + mvex)


def flagged_box(kind, flags, payload, version=0):
    return box(kind, bytes([version]) + flags.to_bytes(3, "big") + payload)


def fragment(sequence=1, decode=0, count=5, duration=1024, size=4, *,
             timing="trun", sizing="trun", base="moof", absolute_start=0,
             track=1, tfdt_version=1, offset_shift=0, split=False,
             composition=0, explicit_composition=False):
    tfhd_flags = 0x020000 if base == "moof" else 0
    header = struct.pack(">I", track)
    if base == "absolute":
        tfhd_flags |= 1
        header += struct.pack(">Q", absolute_start)
    if timing == "tfhd":
        tfhd_flags |= 8
        header += struct.pack(">I", duration)
    if sizing == "tfhd":
        tfhd_flags |= 16
        header += struct.pack(">I", size)
    tfhd = flagged_box(b"tfhd", tfhd_flags, header)
    tfdt = flagged_box(b"tfdt", 0, struct.pack(">Q" if tfdt_version else ">I", decode),
                      tfdt_version)
    mfhd = fullbox(b"mfhd", struct.pack(">I", sequence))

    def runs(offset):
        result = b""
        counts = [count] if not split else [count // 2, count - count // 2]
        for index, n in enumerate(counts):
            flags = 1 if index == 0 else 0
            fields = struct.pack(">I", n)
            if index == 0:
                fields += struct.pack(">i", offset)
            sample = b""
            if timing == "trun":
                flags |= 0x100
                sample += struct.pack(">I", duration)
            if sizing == "trun":
                flags |= 0x200
                sample += struct.pack(">I", size)
            if explicit_composition:
                flags |= 0x800
                sample += struct.pack(">I", composition)
            result += flagged_box(b"trun", flags, fields + sample * n)
        return result

    provisional = box(b"moof", mfhd + box(b"traf", tfhd + tfdt + runs(0)))
    offset = len(provisional) + 8 + offset_shift
    moof = box(b"moof", mfhd + box(b"traf", tfhd + tfdt + runs(offset)))
    return moof + box(b"mdat", b"\x00" * (count * max(size, 1)))


def fmp4(count=5, duration=1024, size=4, *, timing="trun", sizing="trun",
         decode=0, base="moof", **kwargs):
    init = mp4(samples=0, fragmented=True,
               trex_duration=duration if timing == "trex" else 0,
               trex_size=size if sizing == "trex" else 0)
    return init + fragment(count=count, duration=duration, size=size,
                           timing=timing, sizing=sizing, decode=decode, base=base,
                           absolute_start=len(init), **kwargs)


def invalid(audio, mime, code="assist_audio_invalid"):
    with pytest.raises(TaskError, match=f"^{code}$"):
        audio_duration(audio, mime)


@pytest.mark.parametrize("audio,mime,seconds", [
    (wav(), "audio/wav", .1),
    (wav(channels=2, bits=16), "audio/wav", .1),
    (wav(codec=3, bits=32), "audio/wav", .1),
    (mp3(), "audio/mpeg", 4 * 1152 / 44100),
    (mp3(version=2), "audio/mpeg", 4 * 576 / 22050),
    (mp3(version=0), "audio/mpeg", 4 * 576 / 11025),
    (ogg(), "audio/ogg", .1),
    (webm(), "audio/webm", .06),
    (webm(unknown=False), "audio/webm", .06),
    (mp4(), "audio/mp4", 5 * 1024 / 48000),
], ids=["wav", "wav-stereo", "wav-float", "mp3-v1", "mp3-v2", "mp3-v25",
        "ogg", "webm-live", "webm-sized", "mp4"])
def test_supported_synthetic_audio(audio, mime, seconds):
    assert audio_duration(audio, mime) == pytest.approx(seconds)


@pytest.mark.parametrize("mime", ["audio/wav", "audio/mpeg", "audio/ogg", "audio/webm", "audio/mp4"])
def test_empty_and_arbitrary_content_fail_closed(mime):
    invalid(b"", mime)
    invalid(b"not an audio recording", mime)


@pytest.mark.parametrize("maker,mime", [
    (wav, "audio/wav"), (mp3, "audio/mpeg"), (ogg, "audio/ogg"),
    (webm, "audio/webm"), (mp4, "audio/mp4"),
])
def test_truncation_and_trailing_garbage(maker, mime):
    original = maker()
    for size in (1, 3, 8, 10, len(original) // 2 + 1, len(original) - 1):
        invalid(original[:size], mime)
    invalid(original + b"\x00", mime)


@pytest.mark.parametrize("audio,mime", [
    (wav(samples=960001), "audio/wav"),
    (mp3(frames=4594), "audio/mpeg"),
    (ogg(frames=6001), "audio/ogg"),
    (webm(timestamps=(120000,)), "audio/webm"),
    (webm(timestamps=(0,), duration=120001), "audio/webm"),
    (mp4(samples=5626), "audio/mp4"),
], ids=["wav", "mp3", "ogg", "webm-end", "webm-declared", "mp4"])
def test_over_120_seconds_is_rejected(audio, mime):
    invalid(audio, mime, "assist_audio_too_long")


def test_boundary_and_request_limits():
    assert audio_duration(wav(samples=960000), "audio/wav") == 120
    assert audio_duration(ogg(frames=6000), "audio/ogg") == 120
    assert audio_duration(webm(timestamps=(119980,)), "audio/webm") == 120
    sixty_ms_packets = webm(timestamps=range(0, 120000, 60)).replace(
        OPUS_PACKET, b"\x18\xff\xfe")
    assert audio_duration(sixty_ms_packets, "audio/webm") == 120
    assert audio_duration(mp4(samples=5625), "audio/mp4") == 120
    invalid(b"\x00" * (2 * 1024 * 1024 + 1), "audio/wav")
    invalid(wav(), "audio/webm")
    invalid(wav(), "audio/wav; codecs=pcm")
    invalid(wav(), "audio/x-wav")
    invalid(bytearray(wav()), "audio/wav")
    invalid(wav(), None)


@pytest.mark.parametrize("offset,replacement", [
    (20, b"\x02\x00"),  # Unsupported WAV codec.
    (22, b"\x00\x00"),  # No channels.
    (24, b"\x00" * 4),  # No sample rate.
    (28, b"\x01\x00\x00\x00"),  # Byte rate inconsistent with format.
    (32, b"\x02\x00"),  # Alignment inconsistent with channels/bits.
    (34, b"\x07\x00"),  # Invalid sample width.
])
def test_wav_format_consistency(offset, replacement):
    audio = wav()
    invalid(audio[:offset] + replacement + audio[offset + len(replacement):], "audio/wav")


def test_wav_partial_sample_and_duplicate_chunk():
    audio = wav(bits=16)
    broken = audio[:-1]
    broken = broken[:4] + struct.pack("<I", len(broken) - 8) + broken[8:40]
    broken += struct.pack("<I", 1599) + audio[44:-1] + b"\x00"
    broken = broken[:4] + struct.pack("<I", len(broken) - 8) + broken[8:]
    invalid(broken, "audio/wav")
    body = audio[8:] + audio[12:36]
    invalid(b"RIFF" + struct.pack("<I", len(body)) + body, "audio/wav")


def test_mp3_id3_and_variable_bitrate():
    tag = b"ID3\x04\x00\x00\x00\x00\x00\x03abc"
    assert audio_duration(tag + mp3(), "audio/mpeg") == pytest.approx(4 * 1152 / 44100)
    invalid(b"ID3\x04\x00\x00\xff\x00\x00\x03" + mp3(), "audio/mpeg")
    invalid(mp3() + mp3(rate_index=1), "audio/mpeg")
    header = bytearray(mp3())
    header[2] &= 0x0F  # Free bitrate cannot be bounded from this header.
    invalid(bytes(header), "audio/mpeg")
    header = bytearray(mp3())
    header[4:6] = b"\xff\x80"  # First frame references missing reservoir.
    invalid(bytes(header), "audio/mpeg")


def test_ogg_integrity_and_stream_consistency():
    audio = ogg()
    invalid(audio[:22] + b"\x00" * 4 + audio[26:], "audio/ogg")
    invalid(ogg_page(0, [OPUS_HEAD], 0, 2) +
            ogg_page(2, [OPUS_TAGS], 0) +
            ogg_page(3, [OPUS_PACKET], 960, 4), "audio/ogg")
    invalid(ogg_page(0, [OPUS_HEAD], 0, 2) +
            ogg_page(1, [OPUS_TAGS], 0, serial=8) +
            ogg_page(2, [OPUS_PACKET], 960, 4), "audio/ogg")
    invalid(ogg(final_granule=4801), "audio/ogg")
    invalid(ogg(final_granule=3839), "audio/ogg")
    invalid(ogg() + ogg(), "audio/ogg")
    # Valid end trimming is not used to reduce the conservative packet bound.
    assert audio_duration(ogg(final_granule=4488), "audio/ogg") == .1


@pytest.mark.parametrize("packet", [b"", b"\xfb\x00", b"\xfb\x31", b"\xf9\x00",
                                  b"\xfa\xff", b"\xfb\x41\xff", b"\xf8" + b"a" * 1276])
def test_malformed_opus_packets(packet):
    audio = (ogg_page(0, [OPUS_HEAD], 0, 2) + ogg_page(1, [OPUS_TAGS], 0) +
             ogg_page(2, [packet], 960, 4))
    invalid(audio, "audio/ogg")


@pytest.mark.parametrize("lace", [1, 2, 3])
def test_webm_lacing_includes_every_packet_end(lace):
    assert audio_duration(webm(timestamps=(0, 40), lace=lace), "audio/webm") == .08


def test_webm_does_not_trust_missing_or_short_duration():
    invalid(webm(timestamps=(0, 119981), duration=1), "audio/webm", "assist_audio_too_long")
    assert audio_duration(webm(timestamps=(0, 0, 0)), "audio/webm") == .06
    invalid(webm(timestamps=(20, 0)), "audio/webm")
    invalid(webm(duration=float("nan")), "audio/webm")
    audio = webm()
    invalid(audio.replace(b"A_OPUS", b"A_FAKE"), "audio/webm")
    invalid(audio.replace(struct.pack(">d", 48000), struct.pack(">d", 8000)), "audio/webm")


def test_mp4_sample_tables_not_only_header_duration():
    # Understated timing cannot hide the AAC sample count.
    invalid(mp4(samples=5626, delta=1), "audio/mp4", "assist_audio_too_long")
    invalid(mp4(declared=1), "audio/mp4")
    invalid(mp4(offset_shift=1), "audio/mp4")
    invalid(mp4(edits=True), "audio/mp4")
    invalid(mp4() + box(b"moof", b""), "audio/mp4")
    audio = mp4()
    invalid(audio.replace(b"\x11\x88", b"\x2b\x88"), "audio/mp4")  # Unsupported AAC object.
    invalid(audio.replace(b"soun", b"vide"), "audio/mp4")
    invalid(audio.replace(b"mp4a", b"enca"), "audio/mp4")
    invalid(audio.replace(b"mvhd", b"free"), "audio/mp4")
    invalid(audio.replace(b"tkhd", b"free"), "audio/mp4")


def test_mp4_sample_count_and_data_coverage():
    audio = mp4()
    where = audio.index(b"stsz") + 12
    invalid(audio[:where] + struct.pack(">I", 6) + audio[where + 4:], "audio/mp4")
    where = audio.index(b"stco") + 8
    invalid(audio[:where] + struct.pack(">I", 0xFFFFFFFF) + audio[where + 4:], "audio/mp4")
    invalid(audio.replace(b"moov", b"moof"), "audio/mp4")


@pytest.mark.parametrize("timing", ["trun", "tfhd", "trex"])
@pytest.mark.parametrize("sizing", ["trun", "tfhd", "trex"])
def test_fragmented_mp4_default_precedence(timing, sizing):
    assert audio_duration(fmp4(timing=timing, sizing=sizing), "audio/mp4") == 5 * 1024 / 48000


@pytest.mark.parametrize("timing,sizing", [("trun", "trun"), ("tfhd", "tfhd")])
def test_fragment_explicit_defaults_override_trex(timing, sizing):
    audio = bytearray(fmp4(timing=timing, sizing=sizing))
    start = audio.index(b"trex") + 16
    audio[start:start + 8] = struct.pack(">II", 48000 * 121, 100000)
    assert audio_duration(bytes(audio), "audio/mp4") == 5 * 1024 / 48000


@pytest.mark.parametrize("base", ["moof", "implicit", "absolute"])
@pytest.mark.parametrize("tfdt_version", [0, 1])
def test_fragmented_mp4_base_offsets_and_decode_width(base, tfdt_version):
    assert audio_duration(fmp4(base=base, tfdt_version=tfdt_version), "audio/mp4") == \
        5 * 1024 / 48000


def test_safari_shaped_init_and_multiple_timed_fragments():
    # ftyp/moov(mvex/trex, empty stbl), moof(mfhd/traf(tfhd/tfdt/trun)), mdat.
    # Synthetic AAC payloads only; this is a wire-shape test, not a device recording.
    init = mp4(samples=0, fragmented=True)
    audio = init + fragment(count=48) + fragment(sequence=2, decode=48 * 1024, count=48)
    assert audio_duration(audio, "audio/mp4") == 96 * 1024 / 48000
    assert audio_duration(fmp4(split=True), "audio/mp4") == 5 * 1024 / 48000
    assert audio_duration(fmp4(explicit_composition=True), "audio/mp4") == 5 * 1024 / 48000


def test_fragmented_mp4_exact_boundary_and_timeline_end():
    assert audio_duration(fmp4(count=5625), "audio/mp4") == 120
    invalid(fmp4(count=5626), "audio/mp4", "assist_audio_too_long")
    assert audio_duration(fmp4(count=1, decode=120 * 48000 - 1024), "audio/mp4") == 120
    invalid(fmp4(count=1, decode=120 * 48000 - 1023), "audio/mp4", "assist_audio_too_long")
    invalid(fmp4(count=5626, duration=1), "audio/mp4", "assist_audio_too_long")
    invalid(fmp4(count=1, duration=120 * 48000 + 1), "audio/mp4", "assist_audio_too_long")


@pytest.mark.parametrize("kwargs", [
    {"timing": "unknown"}, {"sizing": "unknown"}, {"duration": 0}, {"size": 0},
    {"offset_shift": 1}, {"offset_shift": -1}, {"track": 2},
    {"explicit_composition": True, "composition": 1},
])
def test_fragmented_mp4_unknown_or_inconsistent_samples(kwargs):
    invalid(fmp4(**kwargs), "audio/mp4")


def test_fragmented_mp4_truncated_garbage_and_unreferenced_media():
    audio = fmp4()
    for end in (1, 8, len(audio) // 2, len(audio) - 1, len(audio) - 8):
        invalid(audio[:end], "audio/mp4")
    invalid(audio + b"garbage", "audio/mp4")
    invalid(audio + box(b"mdat", b"\x00" * 4), "audio/mp4")
    invalid(audio.replace(b"mvex", b"free"), "audio/mp4")
    invalid(audio.replace(b"tfdt", b"free"), "audio/mp4")
    invalid(audio.replace(b"trex", b"free"), "audio/mp4")
    invalid(audio.replace(b"trun", b"free"), "audio/mp4")
    invalid(audio.replace(b"tfhd", b"free"), "audio/mp4")


def test_fragmented_mp4_sequence_and_overlapping_decode_timeline():
    init = mp4(samples=0, fragmented=True)
    invalid(init + fragment() + fragment(sequence=1, decode=5120), "audio/mp4")
    invalid(init + fragment() + fragment(sequence=2, decode=5119), "audio/mp4")
    assert audio_duration(init + fragment() + fragment(sequence=2, decode=6000),
                          "audio/mp4") == 11120 / 48000


def test_fragment_init_unknown_duration_headers_still_require_sample_timing():
    audio = bytearray(fmp4())
    for kind, offset in ((b"mvhd", 20), (b"tkhd", 24), (b"mdhd", 20)):
        start = audio.index(kind) + offset
        audio[start:start + 4] = b"\xff" * 4
    assert audio_duration(bytes(audio), "audio/mp4") == 5 * 1024 / 48000
    invalid(bytes(audio).replace(b"tfdt", b"free"), "audio/mp4")


def test_fragment_sample_count_and_flag_budgets():
    audio = fmp4()
    start = audio.index(b"trun") + 8
    invalid(audio[:start] + struct.pack(">I", 100001) + audio[start + 4:], "audio/mp4")
    start = audio.index(b"tfhd") + 4
    invalid(audio[:start] + b"\x00\x01\x00\x00" + audio[start + 4:], "audio/mp4")
    start = audio.index(b"trun") + 4
    invalid(audio[:start] + b"\x00\x10\x03\x01" + audio[start + 4:], "audio/mp4")


@pytest.mark.parametrize("packet,samples", [
    (b"\xf8", 960),  # A zero-length Opus frame is legal DTX.
    (b"\xf9\xff\xfe", 1920),
    (b"\xfa\x01\xff\xfe", 1920),
    (b"\xfb\x02\xff\xfe", 1920),
    (b"\xfb\x82\x01\xff\xfe", 1920),
    (b"\xfb\x42\x01\xff\xfe\x00", 1920),
    (b"\x98\xff", 960),  # Hybrid 20 ms.
    (b"\x18\xff", 2880),  # SILK 60 ms.
    (b"\x80\xff", 120),  # CELT 2.5 ms.
])
def test_opus_framing_modes(packet, samples):
    # Four packets clear the OpusHead pre-skip even for 2.5 ms frames.
    audio = (ogg_page(0, [OPUS_HEAD], 0, 2) + ogg_page(1, [OPUS_TAGS], 0) +
             ogg_page(2, [packet] * 4, samples * 4, 4))
    assert audio_duration(audio, "audio/ogg") == samples * 4 / 48000


def test_webm_block_group_duration_cannot_hide_packet_end():
    assert audio_duration(webm(timestamps=(0,), group_duration=1), "audio/webm") == .02
    assert audio_duration(webm(timestamps=(0,), group_duration=40), "audio/webm") == .04


@pytest.mark.parametrize("maker,mime", [
    (wav, "audio/wav"), (mp3, "audio/mpeg"), (ogg, "audio/ogg"),
    (webm, "audio/webm"), (mp4, "audio/mp4"), (fmp4, "audio/mp4"),
])
def test_mutated_synthetic_containers_never_leak_parser_exceptions(maker, mime):
    original = maker()
    rng = random.Random(17)
    for _ in range(100):
        audio = bytearray(original)
        for _ in range(rng.randrange(1, 5)):
            audio[rng.randrange(len(audio))] = rng.randrange(256)
        try:
            seconds = audio_duration(bytes(audio), mime)
        except TaskError as error:
            assert str(error) in ("assist_audio_invalid", "assist_audio_too_long")
        else:
            assert 0 < seconds <= 120
