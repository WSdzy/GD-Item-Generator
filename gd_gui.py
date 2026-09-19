#!/usr/bin/env python3
"""Desktop item generator for the independent Grim Dawn helper."""

from __future__ import annotations

import ctypes
import json
import queue
import random
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parent
CLIENT_DIR = ROOT / "client"
if str(CLIENT_DIR) not in sys.path:
    sys.path.insert(0, str(CLIENT_DIR))

import catalog as catalog_module  # noqa: E402
from gd_client import (  # noqa: E402
    PipeClient,
    create_affixed_command,
)


MAX_VISIBLE_RECORDS = 2500
MAX_UINT32 = 0xFFFFFFFF
ALL_CATEGORIES = "全部"
NO_TARGET_ATTRIBUTE = "不指定"
AFFIX_STATS_PATH = ROOT / "data" / "zygd_items.json"
MATERIALS_PATH = ROOT / "data" / "materials.json"
USER_SETTINGS_PATH = ROOT / "data" / "settings.local.json"
# These are deliberately phrased as the Chinese terms used in the bundled
# affix index.  Selecting at most two keeps the recommendation meaningful:
# one offensive goal plus one defensive goal is the common case.
TARGET_ATTRIBUTES = (
    "混乱伤害", "物理伤害", "穿刺伤害", "火焰伤害", "冰冷伤害", "闪电伤害",
    "酸性伤害", "毒伤", "活力伤害", "以太伤害", "元素伤害", "流血伤害", "创伤伤害",
    "混乱抗性", "以太抗性", "元素抗性", "火焰抗性", "冰冷抗性", "闪电抗性",
    "酸抗性", "毒抗性", "穿刺抗性", "流血抗性", "虚化抗性", "晕眩抗性",
)
TaskCallback = Callable[[Any, Optional[BaseException]], None]


def record_display_name(record: Dict[str, Any]) -> str:
    display_name = str(record.get("display_name") or "").strip()
    if display_name:
        return display_name
    return str(record.get("label") or "").strip()


def load_user_settings(path: Path = USER_SETTINGS_PATH) -> Dict[str, str]:
    """Load machine-specific options without making a checkout non-portable."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    game_root = payload.get("game_root")
    return {"game_root": str(game_root)} if isinstance(game_root, str) else {}


def save_user_settings(settings: Dict[str, str], path: Path = USER_SETTINGS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def game_executable(game_root: str) -> Path | None:
    root = Path(game_root).expanduser() if game_root else None
    if not root:
        return None
    # Steam's current x64 installation commonly keeps the executable in an
    # x64 subdirectory, while older installations place it at the root.
    for executable in (root / "Grim Dawn.exe", root / "x64" / "Grim Dawn.exe"):
        if executable.is_file():
            return executable
    return None


def is_grim_dawn_running() -> bool:
    """Check the process list without relying on a particular installation path."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Grim Dawn.exe", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "grim dawn.exe" in result.stdout.casefold()


def filter_records(
    records: Iterable[Dict[str, Any]],
    kind: str,
    query: str = "",
    category: str = "",
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    tokens = [token.casefold() for token in query.split() if token.strip()]
    wanted_category = "" if category in ("", ALL_CATEGORIES) else category.casefold()
    matches: List[Dict[str, Any]] = []

    for record in records:
        if record.get("kind") != kind:
            continue
        if wanted_category and str(record.get("category", "")).casefold() != wanted_category:
            continue

        haystack = " ".join(
            str(record.get(field, ""))
            for field in (
                "display_name",
                "name_tag",
                "label",
                "path",
                "category",
                "subcategory",
                "source",
                "item_level",
                "level_requirement",
                "item_classification",
            )
        ).casefold()
        if tokens and not all(token in haystack for token in tokens):
            continue

        matches.append(record)
        if limit is not None and len(matches) >= limit:
            break

    return matches


def parse_count(text: str) -> int:
    try:
        value = int(text.strip(), 10)
    except (AttributeError, ValueError) as exc:
        raise ValueError("数量必须是整数。") from exc
    if value < 1 or value > 1000:
        raise ValueError("数量必须在 1 到 1000 之间。")
    return value


def parse_seed(text: str) -> int:
    try:
        value = int(text.strip(), 0)
    except (AttributeError, ValueError) as exc:
        raise ValueError("种子必须是十进制或十六进制整数。") from exc
    if value < 0 or value > MAX_UINT32:
        raise ValueError("种子必须在 0 到 4294967295 之间。")
    return value


def record_level(record: Dict[str, Any]) -> int:
    value = record.get("level_requirement") or record.get("item_level") or 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def validate_affix_selection(
    base: Dict[str, Any], prefix: Dict[str, Any], suffix: Dict[str, Any]
) -> None:
    """Reject combinations the local database cannot represent legitimately."""
    selected = [record for record in (prefix, suffix) if record]
    if not selected:
        return
    classification = str(base.get("item_classification") or "").casefold()
    if classification not in {"magical", "rare"}:
        raise ValueError("史诗/传奇装备的特殊属性已固化在物品记录中，不能外挂随机前后缀。")
    base_level = record_level(base)
    for affix in selected:
        affix_level = record_level(affix)
        if base_level and affix_level and affix_level > base_level:
            raise ValueError(
                f"词缀等级 Lv {affix_level} 高于装备等级 Lv {base_level}。"
            )


def random_seed() -> int:
    return random.SystemRandom().randrange(0, MAX_UINT32 + 1)


def load_affix_stat_texts(path: Path = AFFIX_STATS_PATH) -> Dict[str, str]:
    """Return the local, display-oriented stat text keyed by affix record."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        str(entry.get("path")): str(entry.get("stats") or "")
        for entry in payload.get("affixes", [])
        if entry.get("path") and entry.get("stats")
    }


def load_material_records(path: Path = MATERIALS_PATH) -> List[Dict[str, Any]]:
    """Load bundled fixed-value material and enchantment records."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [
        record for record in payload.get("records", [])
        if isinstance(record, dict)
        and record.get("kind") == "material"
        and record.get("path")
    ]


def score_affix_targets(stat_text: str, targets: Sequence[str]) -> Tuple[int, float]:
    """Score a documented affix text for up to two user-selected targets."""
    matched = 0
    score = 0.0
    for target in targets:
        if not target or target not in stat_text:
            continue
        matched += 1
        # Attribute texts consistently put the numeric value immediately
        # before the stat name (for example '+40% 混乱伤害').  Sum every such
        # value, which also handles an affix that lists the target twice.
        values = re.findall(
            rf"([+-]?\d+(?:\.\d+)?)\s*%?\s*{re.escape(target)}",
            stat_text,
        )
        score += sum(float(value) for value in values) or 1.0
    return matched, score


def choose_targeted_affix(
    records: Iterable[Dict[str, Any]],
    stat_texts: Dict[str, str],
    base: Dict[str, Any],
    targets: Sequence[str],
) -> Tuple[Optional[Dict[str, Any]], str, Tuple[int, float]]:
    """Choose the highest documented, level-legal affix for the target stats."""
    base_level = record_level(base)
    best: Optional[Dict[str, Any]] = None
    best_text = ""
    best_score = (0, 0.0)
    for record in records:
        path = str(record.get("path") or "")
        text = stat_texts.get(path, "")
        if not text:
            continue
        affix_level = record_level(record)
        if base_level and affix_level and affix_level > base_level:
            continue
        score = score_affix_targets(text, targets)
        # Prefer covering both requested goals, then the larger documented
        # value, then the highest eligible affix tier.
        rank = (score[0], score[1], affix_level)
        if score[0] and (best is None or rank > (best_score[0], best_score[1], record_level(best))):
            best = record
            best_text = text
            best_score = score
    return best, best_text, best_score


def pipe_error_text(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return str(exc) or "Helper 响应超时。"
    if isinstance(exc, OSError):
        code = getattr(exc, "winerror", None)
        if code is None:
            code = exc.errno
        if code == 2:
            return "Helper 未运行：找不到命令管道。"
        if code == 231:
            return "Helper 管道正忙，请稍后重试。"
        if code == 121:
            return "等待 Helper 管道超时。"
        if code == 1223:
            return "管理员权限请求已取消。"
        if code:
            return f"Helper 连接失败：WinError {code}。"
    return str(exc) or exc.__class__.__name__


class ShellExecuteInfoW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", wintypes.LPVOID),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HANDLE),
        ("dwHotKey", wintypes.DWORD),
        ("hIcon", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def run_injector_elevated(injector: Path) -> int:
    if not injector.is_file():
        raise FileNotFoundError(f"找不到注入器：{injector}")

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    shell_execute = shell32.ShellExecuteExW
    shell_execute.argtypes = [ctypes.POINTER(ShellExecuteInfoW)]
    shell_execute.restype = wintypes.BOOL

    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait_for_single_object.restype = wintypes.DWORD

    get_exit_code_process = kernel32.GetExitCodeProcess
    get_exit_code_process.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_exit_code_process.restype = wintypes.BOOL

    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    info = ShellExecuteInfoW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040 | 0x00000100  # SEE_MASK_NOCLOSEPROCESS | NOASYNC
    info.lpVerb = "runas"
    info.lpFile = str(injector)
    info.lpDirectory = str(injector.parent)
    info.nShow = 1

    if not shell_execute(ctypes.byref(info)):
        raise OSError(ctypes.get_last_error(), "ShellExecuteExW failed")
    if not info.hProcess:
        raise RuntimeError("注入器启动后没有返回进程句柄。")

    try:
        wait_result = wait_for_single_object(info.hProcess, 30000)
        if wait_result != 0:
            raise RuntimeError("等待注入器退出超时。")
        exit_code = wintypes.DWORD()
        if not get_exit_code_process(info.hProcess, ctypes.byref(exit_code)):
            raise OSError(ctypes.get_last_error(), "GetExitCodeProcess failed")
        return int(exit_code.value)
    finally:
        close_handle(info.hProcess)


class RecordPicker(ttk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        kind: str,
        on_change: Callable[[], None],
        include_categories: bool = False,
        allow_clear: bool = False,
    ) -> None:
        super().__init__(parent)
        self.kind = kind
        self.on_change = on_change
        self.records: List[Dict[str, Any]] = []
        self.records_by_path: Dict[str, Dict[str, Any]] = {}
        self.stat_texts: Dict[str, str] = {}
        self.selected_path = ""
        self.row_paths: Dict[str, str] = {}
        self.filter_after_id: Optional[str] = None

        self.query_var = tk.StringVar()
        self.category_var = tk.StringVar(value=ALL_CATEGORIES)
        self.count_var = tk.StringVar(value="0 项")
        self.selected_var = tk.StringVar(value="未选择")

        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        search_row = ttk.Frame(self)
        search_row.grid(row=0, column=0, sticky="ew")
        search_row.columnconfigure(0, weight=1)
        self.search_entry = ttk.Entry(search_row, textvariable=self.query_var)
        self.search_entry.grid(row=0, column=0, sticky="ew")
        if allow_clear:
            ttk.Button(
                search_row,
                text="清除选择",
                command=self.clear_selection,
            ).grid(row=0, column=1, padx=(6, 0))

        filter_row = ttk.Frame(self)
        filter_row.grid(row=1, column=0, sticky="ew", pady=(6, 6))
        filter_row.columnconfigure(1, weight=1)
        if include_categories:
            ttk.Label(filter_row, text="分类").grid(row=0, column=0, padx=(0, 6))
            self.category_box = ttk.Combobox(
                filter_row,
                textvariable=self.category_var,
                values=(ALL_CATEGORIES,),
                state="readonly",
                width=14,
            )
            self.category_box.grid(row=0, column=1, sticky="w")
        ttk.Label(filter_row, textvariable=self.count_var).grid(
            row=0,
            column=2,
            sticky="e",
        )

        self.tree = ttk.Treeview(
            self,
            columns=("label", "level", "category", "path"),
            show="headings",
            selectmode="browse",
            height=14,
        )
        self.tree.heading("label", text="名称")
        self.tree.heading("level", text="使用等级")
        self.tree.heading("category", text="分类")
        self.tree.heading("path", text="记录路径")
        self.tree.column("label", width=210, minwidth=110, stretch=False)
        self.tree.column("level", width=80, minwidth=70, stretch=False)
        self.tree.column("category", width=90, minwidth=70, stretch=False)
        self.tree.column("path", width=420, minwidth=180, stretch=True)
        self.tree.grid(row=3, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.tree.yview)
        scrollbar.grid(row=3, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)

        selected_label = ttk.Label(
            self,
            textvariable=self.selected_var,
            anchor="w",
            relief=tk.SUNKEN,
            padding=(6, 5),
            wraplength=720,
        )
        selected_label.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        self.query_var.trace_add("write", self._schedule_filter)
        self.category_var.trace_add("write", self._schedule_filter)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", lambda _event: self._on_tree_select())

    def set_records(self, records: Sequence[Dict[str, Any]]) -> None:
        self.records = [record for record in records if record.get("kind") == self.kind]
        self.records_by_path = {
            str(record.get("path", "")): record
            for record in self.records
            if record.get("path")
        }
        if hasattr(self, "category_box"):
            categories = sorted(
                {
                    str(record.get("category", ""))
                    for record in self.records
                    if record.get("category")
                }
            )
            self.category_box.configure(values=(ALL_CATEGORIES, *categories))
        self._refresh_tree()

    def _schedule_filter(self, *_: object) -> None:
        if self.filter_after_id is not None:
            self.after_cancel(self.filter_after_id)
        self.filter_after_id = self.after(120, self._refresh_tree)

    def _refresh_tree(self) -> None:
        self.filter_after_id = None
        matches = filter_records(
            self.records,
            self.kind,
            self.query_var.get(),
            self.category_var.get(),
            MAX_VISIBLE_RECORDS + 1,
        )
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        self.row_paths.clear()

        for index, record in enumerate(matches[:MAX_VISIBLE_RECORDS]):
            item_id = str(index)
            path = str(record.get("path", ""))
            self.row_paths[item_id] = path
            self.tree.insert(
                "",
                tk.END,
                iid=item_id,
                values=(
                    record_display_name(record),
                    self._level_text(record),
                    record.get("category", ""),
                    path,
                ),
            )

        if len(matches) > MAX_VISIBLE_RECORDS:
            self.count_var.set(f"显示前 {MAX_VISIBLE_RECORDS} 项，请继续搜索")
        else:
            self.count_var.set(f"{len(matches)} 项")

        if self.selected_path:
            for item_id, path in self.row_paths.items():
                if path == self.selected_path:
                    self.tree.selection_set(item_id)
                    self.tree.see(item_id)
                    break

    def _on_tree_select(self, _event: Optional[tk.Event] = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        path = self.row_paths.get(selection[0], "")
        if not path:
            return
        self.selected_path = path
        record = self.records_by_path.get(path, {})
        display_name = record_display_name(record) or path
        name_tag = str(record.get("name_tag") or "")
        rarity = str(record.get("item_classification") or "")
        details = f"{display_name} | {self._level_text(record)}"
        if rarity:
            details += f" | {rarity}"
        details += f" | {path}"
        if name_tag:
            details = f"{display_name} | {name_tag} | {self._level_text(record)} | {path}"
        stats = self.stat_texts.get(path, "")
        if stats:
            details += f"\n加成：{stats}"
        self.selected_var.set(details)
        self.on_change()

    @staticmethod
    def _level_text(record: Dict[str, Any]) -> str:
        level = record.get("level_requirement") or record.get("item_level")
        return f"Lv {level}" if level is not None else "—"

    def clear_selection(self) -> None:
        self.selected_path = ""
        self.selected_var.set("不使用")
        self.tree.selection_remove(self.tree.selection())
        self.on_change()

    def select_path(self, path: str) -> None:
        """Select a known record programmatically, preserving normal UI state."""
        if path not in self.records_by_path:
            raise ValueError(f"目录中没有记录：{path}")
        self.selected_path = path
        record = self.records_by_path[path]
        display_name = record_display_name(record) or path
        details = f"{display_name} | {self._level_text(record)} | {path}"
        if self.stat_texts.get(path):
            details += f"\n加成：{self.stat_texts[path]}"
        self.selected_var.set(details)
        self._refresh_tree()
        self.on_change()

    def set_stat_texts(self, stat_texts: Dict[str, str]) -> None:
        """Attach optional local stat details for selected affix records."""
        self.stat_texts = dict(stat_texts)

    def focus_search(self) -> None:
        self.search_entry.focus_set()


class TrainerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Grim Dawn 独立物品生成器")
        self.root.geometry("1240x820")
        self.root.minsize(1024, 700)
        # Ensure the launcher is visible even when an earlier monitor layout
        # placed its fixed-size window outside the current desktop bounds.
        self.root.after_idle(lambda: self.root.state("zoomed"))

        self.result_queue: "queue.Queue[Tuple[str, Any, Optional[BaseException], Optional[TaskCallback]]]" = queue.Queue()
        self.task_running = False
        self.generation_running = False
        self.stop_event = threading.Event()
        self.catalog: Optional[Dict[str, Any]] = None
        self.material_records: List[Dict[str, Any]] = []
        self.user_settings = load_user_settings()

        self.status_var = tk.StringVar(value="状态：未检测")
        self.game_path_var = tk.StringVar(
            value=self.user_settings.get("game_root", "未配置（已运行的游戏可直接注入）")
        )
        self.base_path_var = tk.StringVar(value="未选择")
        self.material_path_var = tk.StringVar(value="未选择")
        self.prefix_path_var = tk.StringVar(value="不使用")
        self.suffix_path_var = tk.StringVar(value="不使用")
        self.seed_var = tk.StringVar(value=str(random_seed()))
        self.count_var = tk.StringVar(value="1")
        self.material_count_var = tk.StringVar(value="1")
        self.target_one_var = tk.StringVar(value=NO_TARGET_ATTRIBUTE)
        self.target_two_var = tk.StringVar(value=NO_TARGET_ATTRIBUTE)
        self.target_result_var = tk.StringVar(
            value="未选择目标属性；可手动选择词缀，或选择至多两项后自动推荐。"
        )
        self.affix_stat_texts: Dict[str, str] = {}

        self._configure_style()
        self._build_ui()
        self._update_game_status()
        self.root.after(80, self._poll_results)
        self._append_log("正在读取物品目录...")
        self.submit_task("加载物品目录", self._load_catalog, self._on_catalog_loaded)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Path.TLabel", font=("Consolas", 9))
        style.configure("Action.TButton", padding=(12, 7))

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = ttk.Frame(self.root, padding=(14, 12, 14, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        ttk.Label(
            header,
            text="Grim Dawn 独立物品生成器",
            style="Title.TLabel",
        ).grid(row=0, column=0, sticky="w")

        self.status_label = ttk.Label(header, textvariable=self.status_var)
        self.status_label.grid(row=0, column=1, sticky="e", padx=12)

        self.check_button = ttk.Button(
            header,
            text="检查连接",
            command=self.check_connection,
        )
        self.check_button.grid(row=0, column=2, padx=(0, 6))

        self.inject_button = ttk.Button(
            header,
            text="注入 Helper",
            command=self.inject_helper,
        )
        self.inject_button.grid(row=0, column=3, padx=(0, 6))

        self.unload_button = ttk.Button(
            header,
            text="卸载 Helper",
            command=self.unload_helper,
        )
        self.unload_button.grid(row=0, column=4)

        ttk.Label(header, text="游戏目录").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(
            header,
            textvariable=self.game_path_var,
            style="Path.TLabel",
            anchor="w",
        ).grid(row=1, column=1, sticky="ew", padx=12, pady=(8, 0))
        self.choose_game_button = ttk.Button(
            header,
            text="选择游戏目录",
            command=self.choose_game_directory,
        )
        self.choose_game_button.grid(row=1, column=2, columnspan=2, sticky="ew", padx=(0, 6), pady=(8, 0))
        self.launch_game_button = ttk.Button(
            header,
            text="启动游戏",
            command=self.launch_game,
        )
        self.launch_game_button.grid(row=1, column=4, sticky="ew", pady=(8, 0))

        body = ttk.Frame(self.root, padding=(14, 0, 14, 8))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        paned = ttk.Panedwindow(body, orient=tk.HORIZONTAL)
        paned.grid(row=0, column=0, sticky="nsew")

        base_frame = ttk.LabelFrame(paned, text="物品", padding=8)
        base_notebook = ttk.Notebook(base_frame)
        base_notebook.pack(fill=tk.BOTH, expand=True)
        base_tab = ttk.Frame(base_notebook, padding=6)
        self.base_picker = RecordPicker(
            base_tab,
            kind="base",
            on_change=self._on_selection_changed,
            include_categories=True,
        )
        self.base_picker.pack(fill=tk.BOTH, expand=True)
        base_notebook.add(base_tab, text="装备")

        material_tab = ttk.Frame(base_notebook, padding=6)
        self.material_picker = RecordPicker(
            material_tab,
            kind="material",
            on_change=self._on_selection_changed,
            include_categories=True,
        )
        self.material_picker.pack(fill=tk.BOTH, expand=True)
        base_notebook.add(material_tab, text="材料")
        paned.add(base_frame, weight=1)

        affix_frame = ttk.LabelFrame(paned, text="前后词缀", padding=8)
        notebook = ttk.Notebook(affix_frame)
        notebook.pack(fill=tk.BOTH, expand=True)

        prefix_tab = ttk.Frame(notebook, padding=6)
        self.prefix_picker = RecordPicker(
            prefix_tab,
            kind="prefix",
            on_change=self._on_selection_changed,
            allow_clear=True,
        )
        self.prefix_picker.pack(fill=tk.BOTH, expand=True)
        notebook.add(prefix_tab, text="前缀")

        suffix_tab = ttk.Frame(notebook, padding=6)
        self.suffix_picker = RecordPicker(
            suffix_tab,
            kind="suffix",
            on_change=self._on_selection_changed,
            allow_clear=True,
        )
        self.suffix_picker.pack(fill=tk.BOTH, expand=True)
        notebook.add(suffix_tab, text="后缀")
        paned.add(affix_frame, weight=1)

        generator = ttk.LabelFrame(self.root, text="生成", padding=(12, 8))
        generator.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 14))
        generator.columnconfigure(1, weight=1)

        self._add_path_row(generator, 0, "基础", self.base_path_var)
        self._add_path_row(generator, 1, "前缀", self.prefix_path_var)
        self._add_path_row(generator, 2, "后缀", self.suffix_path_var)
        self._add_path_row(generator, 3, "材料", self.material_path_var)

        options = ttk.Frame(generator)
        options.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(7, 7))
        options.columnconfigure(1, weight=1)

        ttk.Label(options, text="种子（紫装/传奇也适用）").grid(row=0, column=0, padx=(0, 6))
        ttk.Entry(options, textvariable=self.seed_var, width=18).grid(
            row=0,
            column=1,
            sticky="w",
        )
        ttk.Button(
            options,
            text="随机",
            command=lambda: self.seed_var.set(str(random_seed())),
        ).grid(row=0, column=2, padx=(6, 18))

        ttk.Label(options, text="数量").grid(row=0, column=3, padx=(0, 6))
        ttk.Spinbox(
            options,
            from_=1,
            to=1000,
            textvariable=self.count_var,
            width=8,
        ).grid(row=0, column=4, sticky="w")

        # Keep the recommender inside the existing compact options area.
        # The log occupies generator row 5, so it must not share that row.
        target_frame = ttk.LabelFrame(options, text="词条导向（最高词缀等级）", padding=(6, 3))
        target_frame.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(5, 0))
        target_frame.columnconfigure(4, weight=1)
        target_values = (NO_TARGET_ATTRIBUTE, *TARGET_ATTRIBUTES)
        ttk.Label(target_frame, text="目标 1").grid(row=0, column=0, padx=(0, 5))
        ttk.Combobox(
            target_frame, textvariable=self.target_one_var, values=target_values,
            state="readonly", width=14,
        ).grid(row=0, column=1, sticky="w")
        ttk.Label(target_frame, text="目标 2").grid(row=0, column=2, padx=(12, 5))
        ttk.Combobox(
            target_frame, textvariable=self.target_two_var, values=target_values,
            state="readonly", width=14,
        ).grid(row=0, column=3, sticky="w")
        self.auto_affix_button = ttk.Button(
            target_frame,
            text="自动推荐",
            command=self.apply_targeted_affixes,
        )
        self.auto_affix_button.grid(row=0, column=4, padx=(12, 0))

        action_row = ttk.Frame(generator)
        action_row.grid(row=5, column=0, columnspan=2, sticky="ew")
        action_row.columnconfigure(8, weight=1)
        self.create_base_button = ttk.Button(
            action_row,
            text="生成无前后缀物品",
            command=self.create_base,
            style="Action.TButton",
            state=tk.DISABLED,
        )
        self.create_base_button.grid(row=0, column=0, padx=(0, 8))
        self.create_affixed_button = ttk.Button(
            action_row,
            text="生成带词缀物品",
            command=self.create_affixed,
            style="Action.TButton",
            state=tk.DISABLED,
        )
        self.create_affixed_button.grid(row=0, column=1, padx=(0, 8))
        ttk.Label(action_row, text="材料数量").grid(row=0, column=2, padx=(0, 5))
        self.material_count_box = ttk.Combobox(
            action_row,
            textvariable=self.material_count_var,
            values=("1", "100", "1000"),
            state="readonly",
            width=6,
        )
        self.material_count_box.grid(row=0, column=3, padx=(0, 6))
        self.create_material_button = ttk.Button(
            action_row,
            text="生成材料",
            command=self.create_material,
            style="Action.TButton",
            state=tk.DISABLED,
        )
        self.create_material_button.grid(row=0, column=4, padx=(0, 8))
        self.stop_button = ttk.Button(
            action_row,
            text="停止生成",
            command=self.stop_generation,
            state=tk.DISABLED,
        )
        self.stop_button.grid(row=0, column=5, padx=(0, 8))
        ttk.Button(
            action_row,
            text="清空日志",
            command=self._clear_log,
        ).grid(row=0, column=6)

        self.progress = ttk.Progressbar(
            action_row,
            mode="determinate",
            maximum=100,
            value=0,
            length=180,
        )
        self.progress.grid(row=0, column=8, sticky="e")

        log_frame = ttk.Frame(generator)
        log_frame.grid(row=6, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            log_frame,
            height=7,
            wrap=tk.WORD,
            font=("Consolas", 9),
            state=tk.DISABLED,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(
            log_frame,
            orient=tk.VERTICAL,
            command=self.log_text.yview,
        )
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

    def _add_path_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
    ) -> None:
        ttk.Label(parent, text=label, width=5).grid(
            row=row,
            column=0,
            sticky="nw",
            padx=(0, 6),
            pady=2,
        )
        ttk.Label(
            parent,
            textvariable=variable,
            style="Path.TLabel",
            anchor="w",
            relief=tk.SUNKEN,
            padding=(5, 4),
            wraplength=1080,
        ).grid(row=row, column=1, sticky="ew", pady=2)

    def _load_catalog(self) -> Dict[str, Any]:
        return catalog_module.load_catalog()

    def _configured_game_root(self) -> str:
        return self.user_settings.get("game_root", "")

    def _update_game_status(self) -> None:
        if is_grim_dawn_running():
            self._append_log("已检测到 Grim Dawn 正在运行；无需配置游戏目录即可注入。")
        elif game_executable(self._configured_game_root()):
            self._append_log("游戏尚未运行；可点击“启动游戏”，再注入 Helper。")
        else:
            self._append_log("游戏尚未运行；请先启动游戏，或通过“选择游戏目录”配置安装位置。")

    def choose_game_directory(self) -> None:
        selected = filedialog.askdirectory(
            parent=self.root,
            title="选择包含 Grim Dawn.exe 的游戏目录",
            initialdir=self._configured_game_root() or str(ROOT),
        )
        if not selected:
            return
        executable = game_executable(selected)
        if executable is None:
            messagebox.showerror(
                "目录不正确",
                "所选目录中没有 Grim Dawn.exe。请直接选择游戏安装目录。",
                parent=self.root,
            )
            return
        self.user_settings["game_root"] = str(executable.parent)
        save_user_settings(self.user_settings)
        self.game_path_var.set(str(executable.parent))
        self._append_log(f"已保存本机游戏目录：{executable.parent}")

    def launch_game(self) -> None:
        executable = game_executable(self._configured_game_root())
        if executable is None:
            messagebox.showwarning(
                "尚未配置游戏目录",
                "请先选择包含 Grim Dawn.exe 的游戏目录，或自行启动游戏后再注入。",
                parent=self.root,
            )
            return
        if is_grim_dawn_running():
            self._append_log("Grim Dawn 已在运行。")
            return
        try:
            subprocess.Popen([str(executable)], cwd=str(executable.parent))
        except OSError as exc:
            messagebox.showerror("启动游戏失败", str(exc), parent=self.root)
            return
        self._append_log("已请求启动 Grim Dawn；进入角色后再点击“注入 Helper”。")

    def _on_catalog_loaded(
        self,
        result: Any,
        error: Optional[BaseException],
    ) -> None:
        if error is not None:
            self._append_log(f"目录加载失败：{error}")
            messagebox.showerror("目录加载失败", str(error), parent=self.root)
            return

        self.catalog = result
        records = list(result.get("records", []))
        self.base_picker.set_records(records)
        self.material_records = load_material_records()
        self.material_picker.set_records(self.material_records)
        self.prefix_picker.set_records(records)
        self.suffix_picker.set_records(records)
        self.affix_stat_texts = load_affix_stat_texts()
        self.prefix_picker.set_stat_texts(self.affix_stat_texts)
        self.suffix_picker.set_stat_texts(self.affix_stat_texts)
        counts = result.get("counts", {})
        localized_counts = result.get("localization", {}).get("localized_counts", {})
        self._append_log(
            "目录已加载："
            f"基础 {counts.get('base', 0)}，"
            f"前缀 {counts.get('prefix', 0)}，"
            f"后缀 {counts.get('suffix', 0)}；"
            f"中文名 {localized_counts.get('base', 0)} / "
            f"{localized_counts.get('prefix', 0)} / "
            f"{localized_counts.get('suffix', 0)}。"
        )
        if self.affix_stat_texts:
            self._append_log(f"词条属性索引已加载：{len(self.affix_stat_texts)} 条。")
        else:
            self._append_log("未找到词条属性索引；仍可手动选择词缀。")
        self._append_log(f"材料目录已加载：{len(self.material_records)} 项。")
        self._on_selection_changed()
        self.check_connection()

    def submit_task(
        self,
        label: str,
        function: Callable[[], Any],
        callback: Callable[[Any, Optional[BaseException]], None],
    ) -> bool:
        if self.task_running:
            self._append_log("已有操作正在执行，请稍候。")
            return False

        self.task_running = True
        self._set_busy(True)
        self._append_log(f"{label}...")

        def worker() -> None:
            try:
                result = function()
            except BaseException as exc:
                self.result_queue.put((label, None, exc, callback))
            else:
                self.result_queue.put((label, result, None, callback))

        threading.Thread(target=worker, daemon=True).start()
        return True

    def _poll_results(self) -> None:
        try:
            while True:
                try:
                    label, result, error, callback = self.result_queue.get_nowait()
                except queue.Empty:
                    break

                if label == "__progress__":
                    self._on_generation_progress(result)
                    continue

                self.task_running = False
                self.generation_running = False
                self._set_busy(False)
                # 额外确保状态一致性
                if self.generation_running:
                    self.generation_running = False
                    self._set_busy(False)
                if callback is None:
                    continue
                try:
                    callback(result, error)
                except BaseException as exc:
                    self._append_log(f"界面回调失败：{exc}")
        finally:
            self.root.after(80, self._poll_results)

    def _default_task_callback(
        self,
        label: str,
        result: Any,
        error: Optional[BaseException],
    ) -> None:
        if error is not None:
            self._append_log(f"{label}失败：{pipe_error_text(error)}")
        else:
            self._append_log(f"{label}：{result}")

    def _set_busy(self, busy: bool) -> None:
        self.progress.stop()

        for button in (
            self.check_button,
            self.inject_button,
            self.unload_button,
            self.choose_game_button,
            self.launch_game_button,
        ):
            button.configure(state=tk.DISABLED if busy else tk.NORMAL)

        can_create = not busy and self.catalog is not None
        self.create_base_button.configure(
            state=tk.NORMAL if can_create and self.base_picker.selected_path else tk.DISABLED
        )
        self.create_affixed_button.configure(
            state=tk.NORMAL if can_create and self.base_picker.selected_path else tk.DISABLED
        )
        self.create_material_button.configure(
            state=tk.NORMAL if can_create and self.material_picker.selected_path else tk.DISABLED
        )
        self.material_count_box.configure(state="readonly" if not busy else tk.DISABLED)
        self.auto_affix_button.configure(
            state=tk.NORMAL if can_create and self.base_picker.selected_path else tk.DISABLED
        )
        self.stop_button.configure(
            state=(
                tk.NORMAL
                if busy and self.generation_running
                else tk.DISABLED
            )
        )

    def _on_selection_changed(self) -> None:
        self.base_path_var.set(self.base_picker.selected_path or "未选择")
        self.material_path_var.set(self.material_picker.selected_path or "未选择")
        self.prefix_path_var.set(self.prefix_picker.selected_path or "不使用")
        self.suffix_path_var.set(self.suffix_picker.selected_path or "不使用")
        if hasattr(self, "create_base_button"):
            self._set_busy(self.task_running)

    def _set_status(self, text: str, color: str) -> None:
        self.status_var.set(f"状态：{text}")
        self.status_label.configure(foreground=color)

    def _ping_helper(self, timeout_ms: int = 1500) -> str:
        with PipeClient(timeout_ms=timeout_ms) as client:
            return client.command("ping")

    def check_connection(self) -> None:
        self.submit_task("检查连接", self._ping_helper, self._callback_检查连接)

    def _callback_检查连接(
        self,
        result: Any,
        error: Optional[BaseException],
    ) -> None:
        if error is not None:
            self._set_status("离线", "#a33a32")
            self._append_log(f"检查连接失败：{pipe_error_text(error)}")
            return

        response = str(result)
        self._append_log(response)
        if response.startswith("OK\tpong"):
            state = response.partition("state=")[2] or "unknown"
            self._set_status(f"在线 · {state}", "#226b45")
        else:
            self._set_status("异常", "#a33a32")

    def inject_helper(self) -> None:
        # Do not block injection based on tasklist.  A game launched with
        # elevated privileges can be invisible to the normal GUI process;
        # the injector itself is elevated and is the authoritative check.
        if not is_grim_dawn_running():
            self._append_log(
                "未能以普通权限确认游戏进程；仍将交由管理员注入器检测。"
            )
        injector = ROOT / "bin" / "gd_injector.exe"
        if not injector.is_file():
            messagebox.showerror(
                "缺少注入器",
                f"找不到 {injector}",
                parent=self.root,
            )
            return

        def action() -> Tuple[int, str]:
            exit_code = run_injector_elevated(injector)
            time.sleep(0.8)
            try:
                response = self._ping_helper(2500)
            except OSError as exc:
                response = f"ERR\t{pipe_error_text(exc)}"
            return exit_code, response

        self.submit_task("注入 Helper", action, self._callback_注入_Helper)

    def _callback_注入_Helper(
        self,
        result: Any,
        error: Optional[BaseException],
    ) -> None:
        if error is not None:
            self._set_status("离线", "#a33a32")
            self._append_log(f"注入失败：{pipe_error_text(error)}")
            messagebox.showerror(
                "注入失败",
                pipe_error_text(error),
                parent=self.root,
            )
            return

        exit_code, response = result
        self._append_log(f"注入器退出码：{exit_code}")
        self._append_log(response)
        if response.startswith("OK\tpong"):
            state = response.partition("state=")[2] or "unknown"
            self._set_status(f"在线 · {state}", "#226b45")
        else:
            self._set_status("注入后未连接", "#a33a32")
            messagebox.showerror(
                "Helper 未连接",
                response,
                parent=self.root,
            )

    def unload_helper(self) -> None:
        def action() -> str:
            with PipeClient(timeout_ms=2500) as client:
                return client.command("shutdown")

        self.submit_task("卸载 Helper", action, self._callback_卸载_Helper)

    def _callback_卸载_Helper(
        self,
        result: Any,
        error: Optional[BaseException],
    ) -> None:
        if error is not None:
            self._set_status("离线", "#a33a32")
            self._append_log(f"卸载失败：{pipe_error_text(error)}")
            return
        self._append_log(str(result))
        self._set_status("已卸载", "#555555")

    def _command(
        self,
        command: str,
        timeout_ms: int = 30000,
    ) -> str:
        with PipeClient(timeout_ms=timeout_ms) as client:
            return client.command(command)

    def submit_generation(
        self,
        label: str,
        command_factory: Callable[[int], str],
        callback: TaskCallback,
        total: int,
    ) -> bool:
        if self.task_running:
            self._append_log("已有操作正在执行，请稍候。")
            return False

        self.stop_event.clear()
        self.generation_running = True
        self.task_running = True
        self.progress.configure(maximum=total, value=0)
        self._set_busy(True)
        self._append_log(f"{label}：0 / {total}...")

        def worker() -> None:
            completed = 0
            last_response = ""
            try:
                for index in range(total):
                    if self.stop_event.is_set():
                        break

                    response = self._command(command_factory(index), timeout_ms=30000)
                    last_response = response
                    if not response.startswith("OK\t"):
                        result = (completed, total, False, response, True)
                        self.result_queue.put((label, result, None, callback))
                        return

                    completed += 1
                    self.result_queue.put(
                        (
                            "__progress__",
                            (completed, total),
                            None,
                            None,
                        )
                    )

                stopped = self.stop_event.is_set()
                result = (completed, total, stopped, last_response, False)
                self.result_queue.put((label, result, None, callback))
            except BaseException as exc:
                result = (completed, total, False, last_response, False)
                self.result_queue.put((label, result, exc, callback))

        threading.Thread(target=worker, daemon=True).start()
        return True

    def _on_generation_progress(self, result: Any) -> None:
        completed, total = result
        self.progress.configure(maximum=total, value=completed)
        self._set_status(f"生成中 · {completed}/{total}", "#1f5f8b")

    def stop_generation(self) -> None:
        if not self.generation_running:
            return
        self.stop_event.set()
        self.stop_button.configure(state=tk.DISABLED)
        self._append_log("已请求停止，当前单件完成后停止。")

    def apply_targeted_affixes(self) -> None:
        base_path = self.base_picker.selected_path
        if not base_path:
            messagebox.showwarning("未选择物品", "请先选择基础物品。", parent=self.root)
            return
        targets = [
            value for value in (self.target_one_var.get(), self.target_two_var.get())
            if value and value != NO_TARGET_ATTRIBUTE
        ]
        targets = list(dict.fromkeys(targets))
        if not targets:
            messagebox.showwarning("未选择目标", "请至少选择一项目标属性。", parent=self.root)
            return
        base = self.base_picker.records_by_path.get(base_path, {})
        classification = str(base.get("item_classification") or "").casefold()
        if classification not in {"magical", "rare"}:
            messagebox.showwarning(
                "不适用",
                "只有魔法/稀有装备可自动附加随机前后缀；史诗和传奇装备请使用无前后缀生成。",
                parent=self.root,
            )
            return
        if not self.affix_stat_texts:
            messagebox.showerror("缺少属性索引", "无法读取词条属性数据。", parent=self.root)
            return

        prefix, prefix_text, prefix_score = choose_targeted_affix(
            self.prefix_picker.records, self.affix_stat_texts, base, targets
        )
        suffix, suffix_text, suffix_score = choose_targeted_affix(
            self.suffix_picker.records, self.affix_stat_texts, base, targets
        )
        if prefix is None and suffix is None:
            self.target_result_var.set(
                f"未找到适用于 Lv {record_level(base) or '—'} 的目标词缀：{'、'.join(targets)}"
            )
            return
        if prefix is not None:
            self.prefix_picker.select_path(str(prefix["path"]))
        else:
            self.prefix_picker.clear_selection()
        if suffix is not None:
            self.suffix_picker.select_path(str(suffix["path"]))
        else:
            self.suffix_picker.clear_selection()

        chosen = []
        if prefix is not None:
            chosen.append(
                f"前缀 {record_display_name(prefix)}（匹配 {prefix_score[0]} 项）：{prefix_text}"
            )
        if suffix is not None:
            chosen.append(
                f"后缀 {record_display_name(suffix)}（匹配 {suffix_score[0]} 项）：{suffix_text}"
            )
        summary = "；".join(chosen)
        self.target_result_var.set("已自动选择：" + summary)
        self._append_log("词条导向推荐：" + summary)

    def create_base(self) -> None:
        base = self.base_picker.selected_path
        if not base:
            messagebox.showwarning("未选择物品", "请先选择基础物品。", parent=self.root)
            return
        try:
            seed = parse_seed(self.seed_var.get())
            count = parse_count(self.count_var.get())
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc), parent=self.root)
            return

        self.submit_generation(
            "生成无前后缀物品",
            # Do not use create_base: the legacy Helper acknowledges that
            # void debug call even when the game created nothing.  Creating
            # an item with empty affix records uses the verified player-give
            # route and returns an item pointer.
            lambda _index: create_affixed_command(base, "", "", seed, 1),
            self._callback_生成基础物品,
            count,
        )

    def _callback_生成基础物品(
        self,
        result: Any,
        error: Optional[BaseException],
    ) -> None:
        self._handle_generation_result("生成无前后缀物品", result, error)

    def create_affixed(self) -> None:
        base = self.base_picker.selected_path
        if not base:
            messagebox.showwarning("未选择物品", "请先选择基础物品。", parent=self.root)
            return

        try:
            seed = parse_seed(self.seed_var.get())
            count = parse_count(self.count_var.get())
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc), parent=self.root)
            return

        prefix = self.prefix_picker.selected_path
        suffix = self.suffix_picker.selected_path
        try:
            validate_affix_selection(
                self.base_picker.records_by_path.get(base, {}),
                self.prefix_picker.records_by_path.get(prefix, {}),
                self.suffix_picker.records_by_path.get(suffix, {}),
            )
        except ValueError as exc:
            messagebox.showerror("词缀组合不合法", str(exc), parent=self.root)
            return
        self.submit_generation(
            "生成带词缀物品",
            lambda _index: create_affixed_command(base, prefix, suffix, seed, 1),
            self._callback_生成带词缀物品,
            count,
        )

    def create_material(self) -> None:
        material = self.material_picker.selected_path
        if not material:
            messagebox.showwarning("未选择材料", "请先在“材料”页选择材料。", parent=self.root)
            return
        try:
            count = int(self.material_count_var.get(), 10)
        except ValueError:
            count = 0
        if count not in {1, 100, 1000}:
            messagebox.showerror("参数错误", "材料数量必须为 1、100 或 1000。", parent=self.root)
            return
        record = self.material_picker.records_by_path.get(material, {})
        name = record_display_name(record) or material
        self.submit_generation(
            f"生成材料：{name}",
            # Materials are fixed DBR records.  Empty affix fields preserve
            # their native component/enchantment behavior.
            lambda _index: create_affixed_command(material, "", "", 0, count),
            self._callback_生成材料,
            1,
        )

    def _callback_生成材料(
        self,
        result: Any,
        error: Optional[BaseException],
    ) -> None:
        self._handle_generation_result("生成材料", result, error)

    def _callback_生成带词缀物品(
        self,
        result: Any,
        error: Optional[BaseException],
    ) -> None:
        self._handle_generation_result("生成带词缀物品", result, error)

    def _handle_generation_result(
        self,
        label: str,
        result: Any,
        error: Optional[BaseException],
    ) -> None:
        completed, total, stopped, last_response, failed_response = result
        if error is not None:
            text = pipe_error_text(error)
            self.progress.configure(value=completed)
            self._append_log(
                f"{label}失败：已完成 {completed} / {total}，{text}"
            )
            self._set_status("请求失败", "#a33a32")
            return

        if failed_response:
            self.progress.configure(value=completed)
            self._append_log(
                f"{label}中止：已完成 {completed} / {total}，{last_response}"
            )
            self._set_status("游戏返回错误", "#a33a32")
            return

        self.progress.configure(value=completed)
        if stopped:
            self._append_log(f"{label}已停止：已完成 {completed} / {total}。")
            self._set_status("已停止", "#8a5a18")
            return

        if last_response:
            self._append_log(last_response)
        self._append_log(f"{label}完成：{completed} / {total}。")
        self._set_status("生成成功", "#226b45")

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)


def main() -> int:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"ERR\tcannot start GUI: {exc}", file=sys.stderr)
        return 1
    TrainerApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
