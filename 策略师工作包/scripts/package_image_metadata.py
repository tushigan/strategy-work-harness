"""Read typed EXIF and ICC text without scanning their binary image/profile data."""
import struct

from package_checks import MAX_BYTES
from package_checks_content import scan_literal, scan_xml


def icc_metadata(data, name, errors):
    if (len(data) < 132 or int.from_bytes(data[:4], "big") != len(data)
            or data[36:40] != b"acsp"):
        raise ValueError("Invalid ICC profile")
    count = int.from_bytes(data[128:132], "big")
    if count > 4096 or 132 + 12 * count > len(data):
        raise ValueError("Invalid ICC table")
    for index in range(count):
        _, offset, size = struct.unpack_from(">4sII", data, 132 + 12 * index)
        item = data[offset:offset + size]
        if offset < 128 or len(item) != size or size < 8:
            raise ValueError("Invalid ICC tag")
        kind = item[:4]
        if kind == b"text":
            scan_literal(item[8:].rstrip(b"\0").decode("ascii"), name, errors)
        elif kind == b"desc":
            if len(item) < 12:
                raise ValueError("Invalid ICC description")
            size = int.from_bytes(item[8:12], "big")
            if 12 + size > len(item):
                raise ValueError("Invalid ICC description")
            scan_literal(item[12:12 + size].rstrip(b"\0").decode("ascii"), name, errors)
            rest = item[12 + size:]
            if len(rest) >= 8:
                length = int.from_bytes(rest[4:8], "big") * 2
                if 8 + length > len(rest):
                    raise ValueError("Invalid ICC Unicode description")
                scan_literal(rest[8:8 + length].decode("utf-16-be").rstrip("\0"), name, errors)
        elif kind == b"mluc":
            if len(item) < 16:
                raise ValueError("Invalid ICC localized text")
            count, record_size = struct.unpack_from(">II", item, 8)
            if record_size != 12 or count > 4096 or 16 + count * record_size > len(item):
                raise ValueError("Invalid ICC localized table")
            for index in range(count):
                size, offset = struct.unpack_from(">II", item, 20 + index * record_size)
                if offset + size > len(item):
                    raise ValueError("Invalid ICC localized value")
                scan_literal(item[offset:offset + size].decode("utf-16-be"), name, errors)
        elif kind not in {b"XYZ ", b"curv", b"para", b"sf32", b"mft1", b"mft2", b"mAB ",
                          b"mBA ", b"sig ", b"view", b"meas", b"chrm", b"dtim", b"ui32"}:
            raise ValueError("Unsupported ICC metadata")


def exif_metadata(data, name, errors):
    if data.startswith(b"Exif\0\0"):
        data = data[6:]
    if len(data) < 8 or data[:4] not in {b"II*\0", b"MM\0*"}:
        raise ValueError("Invalid EXIF header")
    endian = "<" if data[:2] == b"II" else ">"

    def integer(offset, width):
        if offset < 0 or offset + width > len(data):
            raise ValueError("Truncated EXIF")
        return int.from_bytes(data[offset:offset + width], "little" if endian == "<" else "big")

    pending, seen = [integer(4, 4)], set()
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
    while pending:
        offset = pending.pop()
        if not offset:
            continue
        if offset in seen or len(seen) >= 64:
            raise ValueError("Cyclic EXIF")
        seen.add(offset)
        count = integer(offset, 2)
        if count > 4096 or offset + 2 + count * 12 + 4 > len(data):
            raise ValueError("Invalid EXIF directory")
        for index in range(count):
            field = offset + 2 + index * 12
            tag, kind, count_values = struct.unpack_from(endian + "HHI", data, field)
            size = sizes.get(kind, 0) * count_values
            if kind not in sizes or size > MAX_BYTES:
                raise ValueError("Unsupported EXIF value")
            start = field + 8 if size <= 4 else integer(field + 8, 4)
            if start + size > len(data):
                raise ValueError("Truncated EXIF value")
            value = data[start:start + size]
            if tag in {330, 34665, 34853, 40965} and kind in {4, 13}:
                pending.extend(integer(start + j * 4, 4) for j in range(count_values))
            elif kind == 2:
                scan_literal(value.rstrip(b"\0").decode("latin1"), name, errors)
            elif tag in {40091, 40092, 40093, 40094, 40095}:
                scan_literal(value.decode("utf-16-le").rstrip("\0"), name, errors)
            elif tag == 37510:
                encodings = {b"ASCII\0\0\0": "ascii", b"UNICODE\0": "utf-16-le" if endian == "<"
                             else "utf-16-be", b"JIS\0\0\0\0\0": "shift_jis"}
                encoding = encodings.get(value[:8])
                if not encoding:
                    raise ValueError("Unsupported EXIF comment encoding")
                scan_literal(value[8:].decode(encoding).rstrip("\0"), name, errors)
            elif tag == 700:
                scan_xml(value, name, errors)
            elif tag == 34675:
                icc_metadata(value, name, errors)
            elif kind == 7 and tag not in {36864, 37121, 40960}:
                raise ValueError("Unsupported opaque EXIF metadata")
        pending.append(integer(offset + 2 + count * 12, 4))
