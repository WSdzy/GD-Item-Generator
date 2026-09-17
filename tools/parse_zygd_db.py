#!/usr/bin/env python3
"""Parse the item/affix/skill database embedded in the unpacked zyGD DLL.

The rebuilt DLL stores a flat, NUL-terminated UTF-8 string table.  Its layout
is zoned:

  * zone 1 (tokens before the first plain item path): affixes and skills.
    An affix entry is [affix dbr path, Chinese name, stat text].  A skill
    entry is [ascended affix dbr path, skill dbr path, title, optional text].

  * zone 2: item records.  Each item appears as [one or more item dbr paths,
    Chinese name, English name?].  When several Chinese names appear before
    the English name, the last one is the unique item name; the earlier ones
    are base-item labels.  Slot labels and filter labels are interleaved and
    are skipped.

This script only reads the rebuilt DLL; it never executes zyGD itself.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


DLL = Path(r"C:\Users\13645\AppData\Local\Temp\zygd_rebuilt.dll")
OUT = Path(__file__).resolve().parents[1] / "data" / "zygd_items.json"

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
DBR_RE = re.compile(rb"^records/(items|skills)/.+\.dbr$")
ITEM_RE = re.compile(rb"^records/items/")
AFFIX_RE = re.compile(rb"^records/items/lootaffixes/(ascended|prefix|suffix)/")
SKILL_RE = re.compile(rb"^records/skills/itemskillsgdx3/")
AFFIX_STR_RE = re.compile(r"^records/items/lootaffixes/(ascended|prefix|suffix)/")

# Labels that appear between item names and their paths.  They never name an
# item themselves.
SLOT_LABELS = {
    "头盔",
    "肩甲",
    "胸甲",
    "护腕",
    "裤子",
    "护靴",
    "腰带",
    "右手武器",
    "左手武器",
    "双手武器",
    "戒指",
    "项链",
    "圣物",
    "勋章",
    "遗物",
    "副手",
    "盾牌",
}
FILTER_LABELS = {
    "全部",
    "前缀",
    "后缀",
    "专精",
    "宠物",
    "攻击",
    "防御",
    "史诗",
    "神话史诗",
    "传奇",
    "神话传奇",
    "觉醒装备",
}
LABELS = SLOT_LABELS | FILTER_LABELS
ASCII_LABELS = {"Crafted", "Upgraded", "Awakened"}


def tokenize(data: bytes) -> list[tuple[int, bytes]]:
    tokens: list[tuple[int, bytes]] = []
    start: int | None = None
    for i, byte in enumerate(data):
        if byte != 0 and start is None:
            start = i
        elif byte == 0 and start is not None:
            if i - start >= 2:
                tokens.append((start, data[start:i]))
            start = None
    return tokens


def cjk_head(text: str) -> str:
    head = ""
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            head += ch
        else:
            break
    return head


def looks_like_english(text: str) -> bool:
    letters = sum(ch.isalpha() for ch in text)
    if letters < 2:
        return False
    if not text.isprintable():
        return False
    return not text.startswith(("+", "%", "[", "-"))


def normalize_category(path: str) -> str:
    parts = path.split("/")
    if len(parts) >= 4 and parts[1] == "items" and parts[2] in ("upgraded", "awakened"):
        return parts[3]
    if len(parts) >= 3 and parts[1] == "items":
        return parts[2]
    return "other"


def parse_affix_zone(tokens: list[tuple[int, bytes]]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    affixes: list[dict[str, object]] = []
    skills: list[dict[str, object]] = []
    pending_affix = ""  # prefix/suffix path awaiting its meta
    last_ascended = ""  # ascended affix path paired with the next skill path
    skill_path = ""
    meta: list[str] = []

    def flush_affix() -> None:
        nonlocal meta
        if not pending_affix:
            meta = []
            return
        name = ""
        stats = ""
        for text in meta:
            if not name and CJK_RE.search(text) and not re.search(r"[0-9+\-%\[\]]", text):
                name = text
            elif not stats and re.search(r"[0-9+\-%\[\]]", text):
                stats = text
        slot = AFFIX_STR_RE.match(pending_affix).group(1) if AFFIX_STR_RE.match(pending_affix) else ""
        affixes.append({"name": name, "stats": stats, "path": pending_affix, "slot": slot})
        meta = []

    def flush_skill() -> None:
        nonlocal meta, skill_path
        title = ""
        stats = ""
        for text in meta:
            if not title and CJK_RE.search(text):
                title = text
            elif not stats and re.search(r"[0-9+\-%\[\]]", text):
                stats = text
        skills.append({"title": title, "stats": stats, "item_path": last_ascended, "skill_path": skill_path})
        meta = []
        skill_path = ""

    for _, raw in tokens:
        if DBR_RE.match(raw):
            path = raw.decode("utf-8", "replace")
            if skill_path:
                flush_skill()
            else:
                flush_affix()
            if SKILL_RE.match(path.encode()):
                pending_affix = ""
                skill_path = path
            elif AFFIX_STR_RE.match(path):
                match = AFFIX_STR_RE.match(path)
                slot = match.group(1)
                if slot == "ascended":
                    pending_affix = ""
                    last_ascended = path
                else:
                    pending_affix = path
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not text.isprintable() or len(text) < 2:
            continue
        if skill_path:
            meta.append(text)
        elif pending_affix:
            meta.append(text)
    flush_affix()
    return affixes, skills


def parse_item_zone(tokens: list[tuple[int, bytes]]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    last_label = ""

    def flush() -> None:
        nonlocal current
        if current is not None:
            items.append(current)
        current = None

    def new_item() -> dict[str, object]:
        return {"name": "", "en": "", "paths": [], "slots": [], "category": ""}

    for _, raw in tokens:
        if DBR_RE.match(raw) and ITEM_RE.match(raw):
            path = raw.decode("utf-8", "replace")
            if current is not None and (current["name"] or current["en"]):
                flush()
            if current is None:
                current = new_item()
            current["paths"].append(path)
            current["slots"].append(last_label)
            current["category"] = normalize_category(path)
            last_label = ""
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not text.isprintable() or len(text) < 2:
            continue
        head = cjk_head(text)
        if head in LABELS:
            last_label = head
            continue
        if head and head == text and len(head) >= 2:
            if current is None:
                current = new_item()
            elif current["en"]:
                flush()
                current = new_item()
            current["name"] = head
            last_label = ""
            continue
        if text in ASCII_LABELS:
            last_label = text
            continue
        if looks_like_english(text):
            if current is None:
                current = new_item()
            if current["en"] == "":
                current["en"] = text
            last_label = ""
            continue
    flush()

    seen: set[tuple[str, ...]] = set()
    deduped: list[dict[str, object]] = []
    for item in items:
        paths = list(dict.fromkeys(item["paths"]))
        slots = list(dict.fromkeys(item["slots"]))
        item["paths"] = paths
        item["slots"] = slots
        key = (str(item["name"]), tuple(paths))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def main() -> None:
    if not DLL.exists():
        sys.exit(f"missing rebuilt DLL: {DLL}")
    data = DLL.read_bytes()
    tokens = tokenize(data)

    plain = [
        i
        for i, (_, raw) in enumerate(tokens)
        if ITEM_RE.match(raw) and not AFFIX_RE.match(raw)
    ]
    split = plain[0]
    item_end = plain[-1] + 1
    affixes, skills = parse_affix_zone(tokens[:split])
    items = parse_item_zone(tokens[split:item_end])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "items": items,
        "affixes": affixes,
        "skills": skills,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"tokens: {len(tokens)}  split at token {split}")
    print(f"items: {len(items)}  affixes: {len(affixes)}  skills: {len(skills)}")
    no_name = [i for i in items if not i["name"]]
    no_en = [i for i in items if not i["en"]]
    print(f"items without Chinese name: {len(no_name)}  without English name: {len(no_en)}")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
