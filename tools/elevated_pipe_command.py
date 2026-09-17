"""Run one Helper pipe command elevated and persist its response for verification."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))

from gd_client import PipeClient  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: elevated_pipe_command.py COMMAND")
    command = sys.argv[1]
    result_path = ROOT / "data" / "elevated_pipe_result.json"
    payload: dict[str, str | int] = {"command": command}
    try:
        with PipeClient(timeout_ms=30000) as client:
            payload["response"] = client.command(command)
        exit_code = 0 if str(payload["response"]).startswith("OK\t") else 2
    except BaseException as exc:
        payload["error"] = f"{type(exc).__name__}: {exc}"
        exit_code = 1
    payload["exit_code"] = exit_code
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
