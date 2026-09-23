"""Inspect image containers and readable metadata, never compressed pixel bytes."""
import re
import struct
import zlib

from package_checks import MAX_BYTES
from package_checks_content import scan_literal, scan_values, scan_xml
from package_image_metadata import exif_metadata, icc_metadata


def inflate(data):
    decoder = zlib.decompressobj()
    value = decoder.decompress(data, MAX_BYTES + 1)
    if len(value) > MAX_BYTES or not decoder.eof or decoder.unused_data:
        raise ValueError("Invalid or oversized compressed metadata")
    return value


def pixel_bytes(header):
    width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", header)
    depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
    if (not width or not height or depth not in depths.get(color, set())
            or compression or filtering or interlace not in {0, 1}):
        raise ValueError("Invalid PNG pixel format")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    passes = [(0, 0, 1, 1)] if not interlace else [
        (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
        (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]
    size = 0
    for left, top, dx, dy in passes:
        w, h = max(0, (width - left + dx - 1) // dx), max(0, (height - top + dy - 1) // dy)
        if w and h:
            size += h * (1 + (w * channels * depth + 7) // 8)
    if size > MAX_BYTES:
        raise ValueError("PNG pixels exceed audit limit")
    return size


def png_metadata(data, name, errors):
    offset, seen = 8, set()
    metadata_size, decoded_size, expected_size = 0, 0, 0
    decoder = zlib.decompressobj()
    while offset + 12 <= len(data):
        size, kind = struct.unpack_from(">I4s", data, offset)
        end = offset + 12 + size
        if end > len(data):
            raise ValueError("Truncated PNG chunk")
        value = data[offset + 8:end - 4]
        if zlib.crc32(kind + value) != int.from_bytes(data[end - 4:end], "big"):
            raise ValueError("Invalid PNG checksum")
        if not seen and kind != b"IHDR":
            raise ValueError("Missing PNG header")
        if kind == b"IHDR":
            if seen or size != 13:
                raise ValueError("Invalid PNG header")
            expected_size = pixel_bytes(value)
        elif kind == b"IEND":
            if (size or b"IDAT" not in seen or end != len(data) or not decoder.eof
                    or decoded_size != expected_size):
                raise ValueError("Invalid PNG ending")
            return
        elif kind == b"IDAT":
            while value:
                decoded_size += len(decoder.decompress(value, 1024 * 1024))
                if decoded_size > expected_size or decoder.unused_data:
                    raise ValueError("Invalid PNG pixel stream")
                value = decoder.unconsumed_tail
        elif kind in {b"tEXt", b"zTXt", b"iTXt"}:
            keyword, value = value.split(b"\0", 1)
            keyword = keyword.decode("latin1")
            if not 1 <= len(keyword) <= 79:
                raise ValueError("Invalid PNG text keyword")
            if kind == b"zTXt":
                if value[:1] != b"\0":
                    raise ValueError("Unsupported PNG text compression")
                text = inflate(value[1:]).decode("latin1")
            elif kind == b"iTXt":
                if len(value) < 2 or value[0] not in {0, 1} or value[1] != 0:
                    raise ValueError("Invalid PNG international text")
                language, translated, raw = value[2:].split(b"\0", 2)
                scan_literal(language.decode("ascii"), name, errors)
                scan_literal(translated.decode("utf-8"), name, errors)
                text = (inflate(raw) if value[0] else raw).decode("utf-8")
            else:
                text = value.decode("latin1")
            metadata_size += len(text)
            if metadata_size > MAX_BYTES:
                raise ValueError("PNG metadata exceeds audit limit")
            scan_values({keyword: text}, name, errors)
            if keyword == "XML:com.adobe.xmp":
                scan_xml(text, name, errors)
        elif kind == b"eXIf":
            exif_metadata(value, name, errors)
        elif kind == b"iCCP":
            profile_name, value = value.split(b"\0", 1)
            scan_literal(profile_name.decode("latin1"), name, errors)
            if value[:1] != b"\0":
                raise ValueError("Unsupported PNG profile compression")
            icc_metadata(inflate(value[1:]), name, errors)
        elif kind == b"sPLT":
            label, palette = value.split(b"\0", 1)
            scan_literal(label.decode("latin1"), name, errors)
            if not palette or palette[0] not in {8, 16} or (
                    len(palette) - 1) % (6 if palette[0] == 8 else 10):
                raise ValueError("Invalid PNG named palette")
        elif kind in {b"gAMA", b"cHRM", b"sRGB", b"pHYs", b"tIME", b"caNv",
                      b"cICP", b"mDCV", b"cLLI"}:
            lengths = {b"gAMA": 4, b"cHRM": 32, b"sRGB": 1, b"pHYs": 9, b"tIME": 7,
                       b"caNv": 16, b"cICP": 4, b"mDCV": 24, b"cLLI": 8}
            if size != lengths[kind]:
                raise ValueError("Invalid PNG numeric metadata")
        elif kind not in {b"PLTE", b"tRNS", b"sBIT", b"bKGD", b"hIST"}:
            raise ValueError("Unsupported PNG chunk")
        seen.add(kind)
        offset = end
    raise ValueError("Missing PNG ending")


def jpeg_metadata(data, name, errors):
    offset, scanned, profiles, profile_count = 2, False, {}, 0
    while offset < len(data):
        if data[offset] != 255:
            raise ValueError("Invalid JPEG marker")
        while offset < len(data) and data[offset] == 255:
            offset += 1
        if offset >= len(data):
            break
        marker, offset = data[offset], offset + 1
        if marker == 217:
            if not scanned or offset != len(data):
                raise ValueError("Invalid JPEG ending")
            if profiles:
                if set(profiles) != set(range(1, profile_count + 1)):
                    raise ValueError("Incomplete JPEG ICC profile")
                icc_metadata(b"".join(profiles[i] for i in sorted(profiles)), name, errors)
            return
        if marker in {0, 216} or 208 <= marker <= 215:
            raise ValueError("Unexpected JPEG marker")
        if offset + 2 > len(data):
            break
        size = int.from_bytes(data[offset:offset + 2], "big")
        if size < 2 or offset + size > len(data):
            raise ValueError("Truncated JPEG segment")
        value, offset = data[offset + 2:offset + size], offset + size
        if marker == 254:
            scan_literal(value.decode("latin1"), name, errors)
        elif marker == 225 and value.startswith(b"Exif\0\0"):
            exif_metadata(value, name, errors)
        elif marker == 225 and value.startswith(b"http://ns.adobe.com/xap/1.0/\0"):
            scan_xml(value.split(b"\0", 1)[1], name, errors)
        elif marker == 226 and value.startswith(b"ICC_PROFILE\0") and len(value) >= 14:
            sequence, count = value[12:14]
            if (not 1 <= sequence <= count or sequence in profiles
                    or profile_count and profile_count != count):
                raise ValueError("Invalid JPEG ICC sequence")
            profiles[sequence], profile_count = value[14:], count
        elif marker == 224 and value.startswith(b"JFIF\0"):
            if len(value) < 14 or len(value) != 14 + 3 * value[12] * value[13]:
                raise ValueError("Invalid JPEG thumbnail")
        elif marker == 238 and value.startswith(b"Adobe") and len(value) == 12:
            pass
        elif 224 <= marker <= 239:
            raise ValueError("Unsupported JPEG metadata")
        if marker == 218:
            scanned = True
            # Entropy-coded samples have stuffed FF bytes and restart markers.
            match = re.search(b"\xff(?![\x00\xd0-\xd7])", data[offset:])
            if not match:
                raise ValueError("Truncated JPEG scan")
            offset += match.start()
    raise ValueError("Missing JPEG ending")


def inspect_image(data, name, errors):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        png_metadata(data, name, errors)
    elif data.startswith(b"\xff\xd8\xff"):
        jpeg_metadata(data, name, errors)
    else:
        raise ValueError("Unsupported image encoding")
