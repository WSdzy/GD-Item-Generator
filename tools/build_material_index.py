#!/usr/bin/env python3
"""Build the portable material index from extracted Grim Dawn database records.

The resulting ``data/materials.json`` is portable.  The large database
extractions used to create it stay outside the repository.
"""

from __future__ import annotations

import json
import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_DIR = ROOT / "data" / "localization_zh"
OUTPUT = ROOT / "data" / "materials.json"


def fields(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        key, separator, remainder = line.partition(",")
        if not separator:
            continue
        values[key] = remainder.partition(",")[0].strip()
    return values


def localization_tags() -> dict[str, str]:
    tags: dict[str, str] = {}
    for path in TEXT_DIR.rglob("tags*items.txt"):
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            tag, separator, text = line.partition("=")
            if separator and tag.startswith("tag"):
                # UI formatting follows a trailing " ^color" sequence.
                tags[tag.strip()] = text.split(" ^", 1)[0].strip()
    return tags


def number(value: str) -> int | None:
    try:
        return int(float(value)) if value else None
    except ValueError:
        return None


def category_for(record_path: str, display_name: str) -> str:
    """Classify materials using the game record and its localized title.

    Grim Dawn stores runes together with augments beneath ``items/enchants``.
    Their Chinese titles consistently identify them as 铭文 or 符文, so they
    must not be presented as augments in the UI.
    """
    if "/materia/" in record_path:
        return "镶嵌物"
    if "铭文" in display_name or "符文" in display_name:
        return "符文"
    return "附魔"


def parse_source_argument(value: str) -> tuple[str, Path]:
    source, separator, raw_path = value.partition("=")
    if not separator or not source or not raw_path:
        raise argparse.ArgumentTypeError("格式应为 来源名=解包目录")
    path = Path(raw_path)
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"目录不存在：{path}")
    return source, path


def relevant_record_paths(source_dir: Path):
    for dbr in source_dir.rglob("*.dbr"):
        relative = dbr.relative_to(source_dir).as_posix().lower()
        if relative.startswith("records/items/materia/") or relative.startswith(
            "records/items/enchants/"
        ):
            yield relative, dbr


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        action="append",
        type=parse_source_argument,
        required=True,
        metavar="NAME=DIR",
        help="一个完整解包目录；可重复指定，后面的同路径记录会覆盖前面的记录。",
    )
    args = parser.parse_args()
    tags = localization_tags()
    records_by_path: dict[str, dict[str, object]] = {}

    for source_name, source_dir in args.source:
        for record_path, dbr in relevant_record_paths(source_dir):
            record_fields = fields(dbr)
            name_tag = record_fields.get("description", "")
            display_name = tags.get(name_tag, "")
            if not display_name:
                continue
            records_by_path[record_path] = {
                "path": record_path,
                "kind": "material",
                "category": category_for(record_path, display_name),
                "display_name": display_name,
                "name_tag": name_tag,
                "label": record_path.rsplit("/", 1)[-1][:-4].replace("_", " "),
                "item_classification": record_fields.get("itemClassification", ""),
                "item_level": number(record_fields.get("itemLevel", "")),
                "level_requirement": number(record_fields.get("levelRequirement", "")),
                "source": source_name,
            }

    # Records verified before the bulk probe, including the user's examples.
    for record_path, category, name_tag, rarity, item_level, level, source in (
        (
            "records/items/materia/compa_scalyhide.dbr",
            "镶嵌物",
            "tagCompA031Name",
            "Common",
            11,
            7,
            "database.arz",
        ),
        (
            "records/items/enchants/b122a_enchant.dbr",
            "附魔",
            "tagGDX1EnchantB122A",
            "Rare",
            90,
            90,
            "GDX1.arz",
        ),
    ):
        records_by_path.setdefault(
            record_path,
            {
                "path": record_path,
                "kind": "material",
                "category": category_for(record_path, tags[name_tag]),
                "display_name": tags[name_tag],
                "name_tag": name_tag,
                "label": record_path.rsplit("/", 1)[-1][:-4].replace("_", " "),
                "item_classification": rarity,
                "item_level": item_level,
                "level_requirement": level,
                "source": source,
            },
        )

    records = sorted(
        records_by_path.values(),
        key=lambda record: (
            str(record["category"]),
            str(record["display_name"]),
            str(record["path"]),
        ),
    )
    OUTPUT.write_text(
        json.dumps({"version": 2, "records": records}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    counts = {category: sum(1 for record in records if record["category"] == category)
              for category in ("镶嵌物", "符文", "附魔")}
    print(f"materials={len(records)} {counts}")


if __name__ == "__main__":
    main()
