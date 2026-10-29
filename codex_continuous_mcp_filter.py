#!/usr/bin/env python3
"""Small stdio MCP proxy that removes image data and clips huge text fields.

Use this only for stdio MCP servers whose responses are otherwise too large
for the parent context. The proxy preserves JSON-RPC framing and forwards
requests unchanged. It sanitizes server-to-client JSON messages before they
reach the MCP client. Remote HTTP MCP servers cannot be intercepted by this
stdio proxy and must be handled by their own gateway or a tool-specific hook.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from typing import Any, BinaryIO

DEFAULT_MAX_TEXT = 6_000
IMAGE_DATA = re.compile(r"^data:image/[^;]+;base64[:,]", re.IGNORECASE)
DOM_KEYS = re.compile(r"(?:dom|html|snapshot|accessibility|page[_-]?content|outer[_-]?html|inner[_-]?html)", re.IGNORECASE)


def _limit() -> int:
    try:
        value = int(os.environ.get("CODEX_CONTINUOUS_MCP_MAX_TEXT", str(DEFAULT_MAX_TEXT)))
    except ValueError:
        value = DEFAULT_MAX_TEXT
    return max(512, value)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n[… MCP text clipped at {limit:,} chars …]"


def _sanitize(value: Any, key: str = "") -> Any:
    if isinstance(value, str):
        if IMAGE_DATA.match(value):
            encoded = value.split(",", 1)[1] if "," in value else ""
            return f"[image data omitted: {len(encoded):,} base64 chars]"
        return _clip(value, _limit() if DOM_KEYS.search(key) else _limit())
    if isinstance(value, list):
        return [_sanitize(item, key) for item in value]
    if isinstance(value, dict):
        return {str(name): _sanitize(item, str(name)) for name, item in value.items()}
    return value


def sanitize_message(payload: bytes) -> bytes:
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload
    sanitized = _sanitize(decoded)
    return json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _read_frame(stream: BinaryIO) -> tuple[bytes, str] | None:
    first = stream.readline()
    if not first:
        return None
    if first.lower().startswith(b"content-length:"):
        headers = [first]
        while True:
            line = stream.readline()
            if not line:
                return None
            headers.append(line)
            if line in {b"\r\n", b"\n"}:
                break
        length = None
        for header in headers:
            name, _, value = header.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value.strip())
                break
        if length is None or length < 0:
            return None
        payload = stream.read(length)
        if len(payload) != length:
            return None
        return payload, "content-length"
    return first.rstrip(b"\r\n"), "line"


def _write_frame(stream: BinaryIO, payload: bytes, framing: str) -> None:
    if framing == "content-length":
        stream.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii"))
        stream.write(payload)
    else:
        stream.write(payload + b"\n")
    stream.flush()


def _forward_requests(source: BinaryIO, target: BinaryIO) -> None:
    try:
        while True:
            chunk = source.read(65536)
            if not chunk:
                break
            target.write(chunk)
            target.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            target.close()
        except OSError:
            pass


def run_proxy(command: str, args: list[str]) -> int:
    process = subprocess.Popen(
        [command, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    thread = threading.Thread(
        target=_forward_requests,
        args=(sys.stdin.buffer, process.stdin),
        daemon=True,
    )
    thread.start()
    try:
        while True:
            frame = _read_frame(process.stdout)
            if frame is None:
                break
            payload, framing = frame
            _write_frame(sys.stdout.buffer, sanitize_message(payload), framing)
    except (BrokenPipeError, OSError):
        pass
    finally:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        process.wait()
    return process.returncode or 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", required=True)
    parser.add_argument("--args-json", default="[]")
    args = parser.parse_args(argv)
    try:
        child_args = json.loads(args.args_json)
        if not isinstance(child_args, list) or not all(isinstance(item, str) for item in child_args):
            raise ValueError("--args-json must be a JSON string array")
        return run_proxy(args.command, child_args)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"MCP RLM proxy failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
