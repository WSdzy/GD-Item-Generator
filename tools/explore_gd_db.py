"""Inspect the layout of zyGD's embedded item database."""

import re
from pathlib import Path


DLL = Path(r"C:\Users\13645\AppData\Local\Temp\zygd_rebuilt.dll")


def decode_cn(raw: bytes) -> str:
    return raw.decode("gb18030", errors="replace")


def main() -> None:
    data = DLL.read_bytes()
    marker = b"records/items/"
    starts = [m.start() for m in re.finditer(re.escape(marker), data)]
    paths = []
    for off in starts:
        end = data.find(b"\x00", off)
        paths.append((off, data[off:end].decode("latin1")))

    # Group consecutive path occurrences that clearly belong to one entry.
    groups = []
    for off, path in paths:
        if not groups or off - groups[-1][-1][0] > 160:
            groups.append([])
        groups[-1].append((off, path))

    print("path occurrences:", len(paths), "groups:", len(groups))
    from collections import Counter

    print("group sizes:", Counter(len(g) for g in groups).most_common(12))

    # Inspect the exact bytes around a known item name token.
    base = 1410608
    nxt = data.find(b"\x00", base)
    print("first path:", data[base:nxt])
    nxt2 = data.find(b"\x00", nxt + 1)
    print("second path:", data[nxt + 1 : nxt2])
    nxt3 = data.find(b"\x00", nxt2 + 1)
    tok = data[nxt2 + 1 : nxt3]
    print("name bytes:", tok.hex(), "len", len(tok))
    for enc in ("utf-8", "gb18030"):
        try:
            print(enc, repr(tok.decode(enc, errors="strict")))
        except Exception as exc:
            print(enc, "ERR", exc)

    named = 0
    english = 0
    samples = []
    for g in groups:
        last_off = g[-1][0]
        window = data[last_off : last_off + 768]
        tokens = [t.decode("gb18030", errors="replace") for t in window.split(b"\x00")]
        cjk = next((t for t in tokens if re.search(r"[\u4e00-\u9fff]", t)), "")
        en = next(
            (
                t
                for t in tokens
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9' \-]+", t) and len(t) > 2
            ),
            "",
        )
        if cjk:
            named += 1
        if en:
            english += 1
        if len(samples) < 25:
            samples.append((g[0][1], [p for _, p in g], cjk, en, tokens[:12]))

    print("groups with CJK name:", named, "groups with EN name:", english)
    for path, gpaths, cjk, en, toks in samples:
        print("---")
        print("paths:", len(gpaths), gpaths[:3], "..." if len(gpaths) > 3 else "")
        print("cjk:", repr(cjk), "en:", repr(en))
        print("tokens:", toks)


if __name__ == "__main__":
    main()
