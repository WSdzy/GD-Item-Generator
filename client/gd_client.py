#!/usr/bin/env python3
"""Small command-line client for the independent Grim Dawn helper."""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import wintypes


PIPE_NAME = r"\\.\pipe\GDIndependentTrainer"
ERROR_PIPE_BUSY = 231
ERROR_BROKEN_PIPE = 109
ERROR_PIPE_NOT_CONNECTED = 233
ERROR_NO_DATA = 232
MAX_RESPONSE_BYTES = 1024 * 1024


class PipeTimeoutError(TimeoutError):
    pass


class PipeClient:
    def __init__(self, timeout_ms: int = 20000) -> None:
        if timeout_ms < 1:
            raise ValueError("timeout_ms must be positive")
        self.timeout_ms = timeout_ms
        self.handle: int | None = None

    def __enter__(self) -> "PipeClient":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE

        wait_named_pipe = kernel32.WaitNamedPipeW
        wait_named_pipe.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        wait_named_pipe.restype = wintypes.BOOL

        if not wait_named_pipe(PIPE_NAME, self.timeout_ms):
            raise OSError(ctypes.get_last_error(), "WaitNamedPipeW failed")

        handle = create_file(
            PIPE_NAME,
            0xC0000000,  # GENERIC_READ | GENERIC_WRITE
            0,
            None,
            3,  # OPEN_EXISTING
            0,
            None,
        )
        if handle == wintypes.HANDLE(-1).value:
            raise OSError(ctypes.get_last_error(), "CreateFileW failed")
        self.handle = handle
        return self

    def __exit__(self, *_: object) -> None:
        if self.handle is not None:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self.handle)
            self.handle = None

    def command(self, line: str) -> str:
        if self.handle is None:
            raise RuntimeError("pipe is not connected")
        if "\n" in line or "\r" in line:
            raise ValueError("command must be one line")

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        write_file = kernel32.WriteFile
        write_file.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        write_file.restype = wintypes.BOOL

        read_file = kernel32.ReadFile
        read_file.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        read_file.restype = wintypes.BOOL

        peek_named_pipe = kernel32.PeekNamedPipe
        peek_named_pipe.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        peek_named_pipe.restype = wintypes.BOOL

        payload = (line + "\n").encode("utf-8")
        written_total = 0
        while written_total < len(payload):
            written = wintypes.DWORD()
            if not write_file(
                self.handle,
                payload[written_total:],
                len(payload) - written_total,
                ctypes.byref(written),
                None,
            ):
                raise OSError(ctypes.get_last_error(), "WriteFile failed")
            if written.value == 0:
                raise EOFError("helper closed the pipe while writing")
            written_total += written.value

        response = bytearray()
        deadline = time.monotonic() + (self.timeout_ms / 1000.0)
        while True:
            available = wintypes.DWORD()
            if not peek_named_pipe(
                self.handle,
                None,
                0,
                None,
                ctypes.byref(available),
                None,
            ):
                code = ctypes.get_last_error()
                if code in (ERROR_BROKEN_PIPE, ERROR_PIPE_NOT_CONNECTED, ERROR_NO_DATA):
                    raise EOFError("helper closed the pipe")
                raise OSError(code, "PeekNamedPipe failed")

            if available.value == 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PipeTimeoutError(
                        f"helper response timeout after {self.timeout_ms} ms"
                    )
                time.sleep(min(0.01, remaining))
                continue

            read_size = min(int(available.value), 4096)
            chunk = ctypes.create_string_buffer(read_size)
            read = wintypes.DWORD()
            if not read_file(
                self.handle,
                chunk,
                read_size,
                ctypes.byref(read),
                None,
            ):
                raise OSError(ctypes.get_last_error(), "ReadFile failed")
            if read.value == 0:
                raise EOFError("helper closed the pipe")

            data = chunk.raw[: read.value]
            newline = data.find(b"\n")
            if newline >= 0:
                response.extend(data[:newline])
                return response.decode("utf-8", errors="replace").rstrip("\r")

            response.extend(data)
            if len(response) > MAX_RESPONSE_BYTES:
                raise ValueError("helper response is too large")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout-ms", type=int, default=20000)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("ping")
    subparsers.add_parser("shutdown")

    base = subparsers.add_parser("create-base")
    base.add_argument("record_path")
    base.add_argument("count", nargs="?", type=int, default=1)

    affixed = subparsers.add_parser("create-affixed")
    affixed.add_argument("base")
    affixed.add_argument("prefix")
    affixed.add_argument("suffix")
    affixed.add_argument("seed", type=lambda value: int(value, 0))
    affixed.add_argument("count", nargs="?", type=int, default=1)

    return parser


def create_base_command(record_path: str, count: int = 1) -> str:
    return f"create_base\t{record_path}\t{count}"


def create_affixed_command(
    base: str,
    prefix: str,
    suffix: str,
    seed: int,
    count: int = 1,
) -> str:
    return f"create_affixed\t{base}\t{prefix}\t{suffix}\t{seed}\t{count}"


def command_line(args: argparse.Namespace) -> str:
    if args.command == "create-base":
        return create_base_command(args.record_path, args.count)
    if args.command == "create-affixed":
        return create_affixed_command(
            args.base,
            args.prefix,
            args.suffix,
            args.seed,
            args.count,
        )
    return args.command


def main() -> int:
    args = build_parser().parse_args()
    try:
        with PipeClient(args.timeout_ms) as client:
            response = client.command(command_line(args))
    except OSError as exc:
        print(f"ERR\t{exc}", file=sys.stderr)
        return 1

    print(response)
    return 0 if response.startswith("OK\t") else 2


if __name__ == "__main__":
    raise SystemExit(main())
