"""Locate pointer arrays that reference zyGD's embedded string table.

The unpacked payload keeps pointers to its original load address, so instead
of exact string VAs we look for a constant base delta shared by many qwords.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path


DLL = Path(r"C:\Users\13645\AppData\Local\Temp\zygd_rebuilt.dll")


def pe_layout(data: bytes) -> tuple[int, int, int, int, int]:
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    image_base = struct.unpack_from("<Q", data, opt + 24)[0]
    if magic != 0x20B:
        raise SystemExit(f"unsupported optional header magic {magic:#x}")
    sec = opt + 112
    name = data[sec : sec + 8].rstrip(b"\x00")
    vsize, va, rsize, roff = struct.unpack_from("<IIII", data, sec + 8)
    return image_base, va, vsize, roff, rsize


def va_to_off(data: bytes, va: int) -> int | None:
    image_base, sec_va, sec_vsize, sec_roff, sec_rsize = pe_layout(data)
    rva = va - image_base
    if rva < sec_va or rva >= sec_va + sec_vsize:
        return None
    off = sec_roff + (rva - sec_va)
    if off < 0 or off >= len(data):
        return None
    return off


def off_to_va(data: bytes, off: int) -> int:
    image_base, sec_va, _, sec_roff, _ = pe_layout(data)
    return image_base + sec_va + (off - sec_roff)


def main() -> None:
    data = DLL.read_bytes()
    image_base, sec_va, _, sec_roff, _ = pe_layout(data)
    print(f"image base {image_base:#x}, section va {sec_va:#x}, raw {sec_roff:#x}")

    string_vars: list[int] = []
    start: int | None = None
    for i, byte in enumerate(data):
        if byte != 0 and start is None:
            start = i
        elif byte == 0 and start is not None:
            if 2 <= i - start <= 512:
                string_vars.append(off_to_va(data, start))
            start = None
    string_vars.sort()
    print("printable string tokens:", len(string_vars))
    var_set = set(string_vars)

    hits: list[int] = []
    for off in range(0, len(data) - 8, 8):
        val = struct.unpack_from("<Q", data, off)[0]
        if not (0x7FF000000000 <= val <= 0x800000000000):
            continue
        if val in var_set:
            hits.append(off)

    print("qword pointer hits:", len(hits))
    runs: list[tuple[int, int]] = []
    for off in hits:
        if runs and off - runs[-1][1] <= 8:
            runs[-1] = (runs[-1][0], off + 8)
        else:
            runs.append((off, off + 8))
    for start, end in runs:
        if end - start < 24:
            continue
        print(f"run {start:#x}-{end:#x} len {end-start}")
        for off in range(start, end, 8):
            val = struct.unpack_from("<Q", data, off)[0]
            soff = va_to_off(data, val)
            if soff is None:
                print(f"  +{off-start:#04x}: {val:#x} -> out of range")
                continue
            text = data[soff : data.find(b"\x00", soff)][:110]
            try:
                text = text.decode("utf-8")
            except UnicodeDecodeError:
                text = repr(text)
            print(f"  +{off-start:#04x}: {val:#x} -> {soff:#x} {text!r}")


if __name__ == "__main__":
    sys.exit(main())
