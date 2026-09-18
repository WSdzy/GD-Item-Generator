from __future__ import annotations

import sys
import json
import tempfile
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

    def test_bundled_localization_and_portable_cache(self) -> None:
        bundled = catalog.PROJECT_LOCALIZATION_DIR / "tags_items.txt"
        self.assertTrue(bundled.is_file())
        self.assertIn(bundled, catalog.discover_localization_files())

        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "catalog.json"
            cache_path.write_text(
                json.dumps({"version": 3, "records": [{"path": "records/test.dbr"}]}),
                encoding="utf-8",
            )
            loaded = catalog.load_catalog(
                Path(temporary) / "no-game-installed", cache_path
            )
        self.assertEqual(loaded["records"][0]["path"], "records/test.dbr")

    def test_affix_validation_blocks_legendary_and_high_level_affixes(self) -> None:
        with self.assertRaises(ValueError):
            gd_gui.validate_affix_selection(
                {"item_classification": "Legendary", "level_requirement": 94},
                {"level_requirement": 1},
                {},
            )

    def test_targeted_affix_prefers_matching_highest_legal_tier(self) -> None:
        base = {"item_classification": "Rare", "level_requirement": 50}
        records = [
            {"path": "prefix/low", "level_requirement": 20},
            {"path": "prefix/high", "level_requirement": 50},
            {"path": "prefix/too-high", "level_requirement": 70},
        ]
        chosen, text, score = gd_gui.choose_targeted_affix(
            records,
            {
                "prefix/low": "+20% 混乱伤害",
                "prefix/high": "+40% 混乱伤害 18% 混乱抗性",
                "prefix/too-high": "+99% 混乱伤害",
            },
            base,
            ["混乱伤害", "混乱抗性"],
        )
        self.assertEqual(chosen["path"], "prefix/high")
        self.assertEqual(text, "+40% 混乱伤害 18% 混乱抗性")
        self.assertEqual(score, (2, 58.0))

    def test_targeted_affix_score_uses_only_selected_stats(self) -> None:
        self.assertEqual(
            gd_gui.score_affix_targets("+40% 混乱伤害 22% 虚化抗性", ["混乱伤害"]),
            (1, 40.0),
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

    def test_game_path_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "Grim Dawn.exe"
            executable.write_bytes(b"")
            self.assertEqual(gd_gui.game_executable(str(root)), executable)
            settings_path = root / "settings.json"
            gd_gui.save_user_settings({"game_root": str(root)}, settings_path)
            self.assertEqual(
                gd_gui.load_user_settings(settings_path), {"game_root": str(root)}
            )


if __name__ == "__main__":
    unittest.main()
