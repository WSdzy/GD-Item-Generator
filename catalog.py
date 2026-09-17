#!/usr/bin/env python3
"""Build the independent item catalog from Grim Dawn archive indexes."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_GAME_ROOT = Path(
    r"D:\Program Files (x86)\steam\steamapps\common\Grim Dawn"
)
CATALOG_VERSION = 3
DEFAULT_EXTRACTED_DB = Path(__file__).resolve().parent / "data" / "gd_db"
RECORD_RE = re.compile(rb"records/[A-Za-z0-9_./%~()+ -]+?\.dbr")
TAG_COLOR_RE = re.compile(r"\^[A-Za-z0-9]")

GEAR_CATEGORIES = {
    "gearweapons": "weapon",
    "gearaccessories": "accessory",
    "gearhead": "armor",
    "gearshoulders": "armor",
    "geartorso": "armor",
    "gearlegs": "armor",
    "gearhands": "armor",
    "gearfeet": "armor",
    "geararmor": "armor",
    "gearrelic": "relic",
}

EXCLUDED_ITEM_PARTS = {
    "bonusitems",
    "crafting",
    "enchantments",
    "enchants",
    "enemygear",
    "faction",
    "lootaffixes",
    "lootchests",
    "lootsets",
    "loottables",
    "loreobjects",
    "materia",
    "misc",
    "questitems",
    "transmutes",
}


def discover_archives(game_root: Path) -> list[Path]:
    candidates: list[Path] = []
    database = game_root / "database" / "database.arz"
    if database.is_file():
        candidates.append(database)

    for expansion in sorted(game_root.glob("gdx*/database/*.arz")):
        if expansion.is_file():
            candidates.append(expansion)

    unique: dict[str, Path] = {}
    for path in candidates:
        unique[str(path.resolve()).lower()] = path
    return list(unique.values())


def archive_signature(archives: list[Path]) -> list[dict[str, Any]]:
    signature: list[dict[str, Any]] = []
    for archive in archives:
        stat = archive.stat()
        signature.append(
            {
                "path": str(archive.resolve()),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return signature


def discover_localization_files() -> list[Path]:
    roots: list[Path] = []
    configured = os.environ.get("GRIM_DAWN_TEXT_DIR")
    if configured:
        roots.append(Path(configured).expanduser())

    home = Path.home()
    roots.extend(
        [
            home / "OneDrive" / "文档" / "My Games" / "Grim Dawn" / "Settings" / "text_zh",
            home / "OneDrive" / "Documents" / "My Games" / "Grim Dawn" / "Settings" / "text_zh",
            home / "Documents" / "My Games" / "Grim Dawn" / "Settings" / "text_zh",
            home / "我的文档" / "My Games" / "Grim Dawn" / "Settings" / "text_zh",
        ]
    )

    files: dict[str, Path] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.txt"):
            name = path.name.casefold()
            if name == "tags_items.txt" or (
                name.startswith("tags") and "items" in name
            ):
                files[str(path.resolve()).casefold()] = path
    return sorted(files.values(), key=lambda path: str(path).casefold())


def localization_signature(files: list[Path]) -> list[dict[str, Any]]:
    signature: list[dict[str, Any]] = []
    for path in files:
        stat = path.stat()
        signature.append(
            {
                "path": str(path.resolve()),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return signature


def clean_localized_text(text: str) -> str:
    return TAG_COLOR_RE.sub("", text).strip()


def load_localization_tags(files: list[Path]) -> dict[str, str]:
    tags: dict[str, str] = {}
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            tag, _, text = line.partition("=")
            tag = tag.strip()
            if not tag.startswith("tag"):
                continue
            tags[tag] = clean_localized_text(text)
    return tags


def read_record_name_tag(dbr_root: Path, record_path: str, kind: str) -> str:
    local_path = dbr_root / record_path
    try:
        text = local_path.read_bytes().decode("utf-8", errors="ignore")
    except OSError:
        return ""

    field = "itemNameTag" if kind == "base" else "lootRandomizerName"
    match = re.search(
        rf"(?im)^{re.escape(field)}\s*,\s*([^,\r\n]*)\s*,",
        text,
    )
    return match.group(1).strip() if match else ""


def read_record_metadata(dbr_root: Path, record_path: str) -> dict[str, object]:
    """Read the gameplay-facing fields needed to present a safe item choice."""
    try:
        text = (dbr_root / record_path).read_bytes().decode("utf-8", errors="ignore")
    except OSError:
        return {}

    def value(field: str) -> str:
        match = re.search(rf"(?im)^{re.escape(field)}\s*,\s*([^,\r\n]*)", text)
        return match.group(1).strip() if match else ""

    def number(field: str) -> int | None:
        raw = value(field)
        try:
            return int(float(raw)) if raw else None
        except ValueError:
            return None

    return {
        "item_level": number("itemLevel"),
        "level_requirement": number("levelRequirement"),
        "item_classification": value("itemClassification"),
        "roll_jitter": number("lootRandomizerJitter"),
    }


def scan_record_paths(archive: Path) -> set[str]:
    data = archive.read_bytes()
    paths: set[str] = set()
    for match in RECORD_RE.finditer(data):
        try:
            paths.add(match.group(0).decode("ascii"))
        except UnicodeDecodeError:
            continue
    return paths


def record_label(path: str) -> str:
    stem = path.rsplit("/", 1)[-1][:-4]
    label = stem.replace("_", " ").replace("-", " ")
    label = re.sub(r"\s+", " ", label).strip()
    return label


def record_category(path: str) -> tuple[str, str]:
    parts = path.split("/")
    for category, group in GEAR_CATEGORIES.items():
        if category not in parts:
            continue
        index = parts.index(category)
        subcategory = parts[index + 1] if index + 1 < len(parts) - 1 else ""
        return group, subcategory
    return "other", ""


def classify_record(path: str) -> str | None:
    if "/lootaffixes/prefix/" in path:
        if "/prefixtables/" in path or path.endswith("/aa000_base_blank.dbr"):
            return None
        return "prefix"

    if "/lootaffixes/suffix/" in path:
        if "/suffixtables/" in path or path.endswith("/a000_base_blank.dbr"):
            return None
        return "suffix"

    if not path.startswith("records/items/"):
        return None

    parts = set(path.split("/"))
    if parts & EXCLUDED_ITEM_PARTS:
        return None
    if not parts & set(GEAR_CATEGORIES):
        return None
    return "base"


def build_catalog(
    game_root: Path,
    extracted_db: Path = DEFAULT_EXTRACTED_DB,
    localization_files: list[Path] | None = None,
) -> dict[str, Any]:
    archives = discover_archives(game_root)
    if not archives:
        raise FileNotFoundError(f"no Grim Dawn .arz archives under {game_root}")

    if localization_files is None:
        localization_files = discover_localization_files()
    localization_tags = load_localization_tags(localization_files)

    records_by_path: dict[str, dict[str, Any]] = {}
    counts = {"base": 0, "prefix": 0, "suffix": 0}
    for archive in archives:
        for path in scan_record_paths(archive):
            kind = classify_record(path)
            if kind is None:
                continue
            category, subcategory = record_category(path)
            records_by_path[path] = {
                "path": path,
                "kind": kind,
                "label": record_label(path),
                "category": category,
                "subcategory": subcategory,
                "source": archive.name,
            }

    localized_counts = {"base": 0, "prefix": 0, "suffix": 0}
    for record in records_by_path.values():
        name_tag = read_record_name_tag(extracted_db, record["path"], record["kind"])
        display_name = localization_tags.get(name_tag, "")
        record["name_tag"] = name_tag
        record["display_name"] = display_name
        record.update(read_record_metadata(extracted_db, record["path"]))
        if display_name:
            localized_counts[record["kind"]] += 1

    records = sorted(
        records_by_path.values(),
        key=lambda record: (
            record["kind"],
            record["category"],
            record["subcategory"],
            (record["display_name"] or record["label"]).casefold(),
            record["label"].lower(),
            record["path"],
        ),
    )
    for record in records:
        counts[record["kind"]] += 1

    return {
        "version": CATALOG_VERSION,
        "game_root": str(game_root.resolve()),
        "extracted_db": str(extracted_db.resolve()),
        "generated_at": int(time.time()),
        "signature": archive_signature(archives),
        "localization_signature": localization_signature(localization_files),
        "localization": {
            "files": [str(path.resolve()) for path in localization_files],
            "tag_count": len(localization_tags),
            "localized_counts": localized_counts,
        },
        "counts": counts,
        "records": records,
    }


def cache_is_current(cache: dict[str, Any], game_root: Path) -> bool:
    if cache.get("version") != CATALOG_VERSION:
        return False
    if str(game_root.resolve()) != cache.get("game_root"):
        return False
    if str(DEFAULT_EXTRACTED_DB.resolve()) != cache.get("extracted_db"):
        return False
    archives = discover_archives(game_root)
    localization_files = discover_localization_files()
    return (
        cache.get("signature") == archive_signature(archives)
        and cache.get("localization_signature")
        == localization_signature(localization_files)
    )


def load_catalog(
    game_root: Path = DEFAULT_GAME_ROOT,
    cache_path: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    if cache_path is None:
        cache_path = Path(__file__).resolve().parent / "data" / "catalog.json"

    if not force and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = {}
        if cache_is_current(cached, game_root):
            return cached

    catalog = build_catalog(game_root)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(catalog, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return catalog


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--game-root",
        type=Path,
        default=Path(os.environ.get("GRIM_DAWN_PATH", DEFAULT_GAME_ROOT)),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "data" / "catalog.json",
    )
    parser.add_argument("--force", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        catalog = load_catalog(args.game_root, args.output, args.force)
    except (OSError, ValueError) as exc:
        print(f"ERR\t{exc}", file=sys.stderr)
        return 1

    counts = catalog["counts"]
    localized_counts = catalog.get("localization", {}).get("localized_counts", {})
    print(
        "OK\t"
        f"base={counts['base']}\t"
        f"prefix={counts['prefix']}\t"
        f"suffix={counts['suffix']}\t"
        f"localized_base={localized_counts.get('base', 0)}\t"
        f"localized_prefix={localized_counts.get('prefix', 0)}\t"
        f"localized_suffix={localized_counts.get('suffix', 0)}\t"
        f"output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
