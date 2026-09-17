#!/usr/bin/env python3
"""Extract the embedded item/affix database from the unpacked zyGD DLL."""

import json
import re
import sys
from pathlib import Path


DLL = Path(
    r"C:\Users\13645\AppData\Local\Temp\zygd_rebuilt.dll"
)
OUT = Path(__file__).resolve().parent.parent / "data" / "gd_db.json"


def nul_split(chunk: bytes) -> list[str]:
    return [s.decode("utf-8", errors="replace") for s in chunk.split(b"\x00")]


def main() -> None:
    data = DLL.read_bytes()
    marker = b"records/items/"
    starts = [m.start() for m in re.finditer(re.escape(marker), data)]
    print(f"{len(starts)} record-path hits")

    # Dump a tokenized window around the item path block. Names are likely
    # stored in a separate parallel block, possibly GBK encoded.
    off = 1410608
    end = data.find(b"records/", off + 300)
    if end == -1:
        end = min(len(data), off + 200000)
    tokens = nul_split(data[off:end])
    print("=== item path block tokens ===")
    for i, t in enumerate(tokens[:80]):
        if t:
            print(i, repr(t))
    gbk_window = data[off : off + 40000]
    decoded = gbk_window.decode("gb18030", errors="replace")
    print("=== gb18030 window sample ===")
    print(decoded[:3000].replace("\x00", " | "))

    # Print a handful of raw windows around item records (non-affix) so the
    # field layout can be inspected before the final parse.
    shown = 0
    for off in starts:
        path = data[off : data.find(b"\x00", off)].decode("utf-8", "replace")
        if "lootaffixes" in path:
            continue
        window = data[off : off + 600]
        if shown < 8:
            print("---", off, path)
            print(nul_split(window)[:8])
            shown += 1


if __name__ == "__main__":
    main()
