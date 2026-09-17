from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "client"))

import catalog  # noqa: E402
import gd_gui  # noqa: E402
from gd_client import (  # noqa: E402
    build_parser,
    command_line,
    create_affixed_command,
    create_base_command,
)


class CatalogTests(unittest.TestCase):
    def test_cached_catalog_counts_and_unique_paths(self) -> None:
        loaded = catalog.load_catalog()
        records = loaded["records"]
        paths = [record["path"] for record in records]

        self.assertEqual(loaded["counts"]["base"], 6586)
        self.assertEqual(loaded["counts"]["prefix"], 2781)
        self.assertEqual(loaded["counts"]["suffix"], 3408)
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(len(records), sum(loaded["counts"].values()))

    def test_filter_records_searches_all_fields(self) -> None:
        records = [
            {
                "kind": "base",
                "label": "b303b sword2h",
                "path": "records/items/gearweapons/melee2h/b303b_sword2h.dbr",
                "category": "weapon",
                "subcategory": "melee2h",
                "source": "database.arz",
            },
            {
                "kind": "prefix",
                "label": "b ar020",
                "path": "records/items/lootaffixes/prefix/b_ar020_je_f.dbr",
                "category": "other",
                "subcategory": "",
                "source": "database.arz",
            },
        ]

        self.assertEqual(
            [record["kind"] for record in gd_gui.filter_records(records, "base")],
            ["base"],
        )
        self.assertEqual(
            gd_gui.filter_records(records, "base", "sword weapon")[0]["label"],
            "b303b sword2h",
        )
        self.assertEqual(gd_gui.filter_records(records, "base", "prefix"), [])

    def test_catalog_exposes_level_and_rarity(self) -> None:
        records = catalog.load_catalog()["records"]
        shuroth = next(
            record for record in records
            if record["path"].endswith("gearaccessories/rings/d226_ring.dbr")
        )
        self.assertEqual(shuroth["item_level"], 94)
        self.assertEqual(shuroth["level_requirement"], 94)
        self.assertEqual(shuroth["item_classification"], "Legendary")

    def test_affix_validation_blocks_legendary_and_high_level_affixes(self) -> None:
        with self.assertRaises(ValueError):
            gd_gui.validate_affix_selection(
                {"item_classification": "Legendary", "level_requirement": 94},
                {"level_requirement": 1},
                {},
            )
        with self.assertRaises(ValueError):
            gd_gui.validate_affix_selection(
                {"item_classification": "Rare", "level_requirement": 20},
                {"level_requirement": 26},
                {},
            )


class CommandTests(unittest.TestCase):
    def test_command_helpers(self) -> None:
        self.assertEqual(
            create_base_command("records/items/test.dbr", 2),
            "create_base\trecords/items/test.dbr\t2",
        )
        self.assertEqual(
            create_affixed_command(
                "records/items/test.dbr",
                "records/items/lootaffixes/prefix/p.dbr",
                "records/items/lootaffixes/suffix/s.dbr",
                123,
                3,
            ),
            "create_affixed\t"
            "records/items/test.dbr\t"
            "records/items/lootaffixes/prefix/p.dbr\t"
            "records/items/lootaffixes/suffix/s.dbr\t"
            "123\t3",
        )

    def test_argument_parser_uses_command_helpers(self) -> None:
        args = build_parser().parse_args(
            [
                "create-affixed",
                "records/items/base.dbr",
                "records/items/prefix.dbr",
                "",
                "0x10",
                "4",
            ]
        )
        self.assertEqual(
            command_line(args),
            "create_affixed\t"
            "records/items/base.dbr\t"
            "records/items/prefix.dbr\t"
            "\t16\t4",
        )

    def test_parameter_validation(self) -> None:
        self.assertEqual(gd_gui.parse_count(" 25 "), 25)
        self.assertEqual(gd_gui.parse_seed("0xFFFFFFFF"), 0xFFFFFFFF)
        with self.assertRaises(ValueError):
            gd_gui.parse_count("0")
        with self.assertRaises(ValueError):
            gd_gui.parse_count("1001")
        with self.assertRaises(ValueError):
            gd_gui.parse_seed("-1")
        with self.assertRaises(ValueError):
            gd_gui.parse_seed("4294967296")


class GuiImportTests(unittest.TestCase):
    def test_gui_module_imports_without_starting_tk(self) -> None:
        self.assertTrue(callable(gd_gui.main))
        self.assertTrue(callable(gd_gui.TrainerApp))


if __name__ == "__main__":
    unittest.main()
