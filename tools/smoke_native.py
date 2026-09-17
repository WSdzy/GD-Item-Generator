#!/usr/bin/env python3
"""Load the helper in a harmless host process and test its pipe protocol."""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))

from gd_client import PipeClient  # noqa: E402


def main() -> int:
    helper = ROOT / "bin" / "gd_helper.dll"
    if not helper.exists():
        raise SystemExit(f"missing helper: {helper}")

    module = ctypes.WinDLL(str(helper))
    time.sleep(0.2)

    with PipeClient(timeout_ms=5000) as client:
        ping = client.command("ping")
        create = client.command(
            "create_base\t"
            "records/items/gearweapons/melee2h/b303b_sword2h.dbr\t1"
        )
        shutdown = client.command("shutdown")

    print(ping)
    print(create)
    print(shutdown)

    # Keep a reference to the module until the helper thread has exited.
    del module
    ok = (
        ping.startswith("OK\tpong")
        and create == "ERR\tgame_thread_not_hooked"
        and shutdown == "OK\tshutdown"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
