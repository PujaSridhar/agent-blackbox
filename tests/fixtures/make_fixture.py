"""Build the synthetic trace the detector tests run against.

Hand-written rather than copied from a real session: every detector needs a
case planted for it, and a fixture nobody can read is a fixture nobody trusts.
Run this module to regenerate ``synthetic.jsonl``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

T0 = datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc)


def _usage(out=400, read=20_000, write_1h=5_000, write_5m=0, thinking=0):
    return {
        "input_tokens": 3,
        "output_tokens": out,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": write_1h + write_5m,
        "cache_creation": {
            "ephemeral_1h_input_tokens": write_1h,
            "ephemeral_5m_input_tokens": write_5m,
        },
        "output_tokens_details": {"thinking_tokens": thinking},
    }


class Builder:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.clock = T0
        self.counter = 0

    def _uid(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}-{self.counter:04d}"

    def tick(self, seconds: float) -> None:
        self.clock += timedelta(seconds=seconds)

    def user(self, text: str) -> str:
        uid = self._uid("u")
        self.rows.append(
            {
                "type": "user",
                "uuid": uid,
                "parentUuid": None,
                "sessionId": "synthetic",
                "cwd": "/repo",
                "gitBranch": "main",
                "version": "test",
                "timestamp": self.clock.isoformat().replace("+00:00", "Z"),
                "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            }
        )
        return uid

    def call(
        self,
        parent: str,
        tool: str,
        tool_input: dict,
        *,
        model: str = "claude-opus-5-5",
        sidechain: bool = False,
        usage: dict | None = None,
    ) -> tuple[str, str]:
        """Emit an assistant row carrying one tool_use block."""
        uid = self._uid("a")
        tool_use_id = self._uid("tu")
        self.rows.append(
            {
                "type": "assistant",
                "uuid": uid,
                "parentUuid": parent,
                "sessionId": "synthetic",
                "isSidechain": sidechain,
                "timestamp": self.clock.isoformat().replace("+00:00", "Z"),
                "message": {
                    "role": "assistant",
                    "model": model,
                    "usage": usage or _usage(),
                    "content": [
                        {"type": "tool_use", "id": tool_use_id, "name": tool, "input": tool_input}
                    ],
                },
            }
        )
        return uid, tool_use_id

    def result(
        self,
        parent: str,
        tool_use_id: str,
        output: str,
        *,
        is_error: bool = False,
        sidechain: bool = False,
        interrupted: bool = False,
    ) -> str:
        uid = self._uid("r")
        row = {
            "type": "user",
            "uuid": uid,
            "parentUuid": parent,
            "sessionId": "synthetic",
            "isSidechain": sidechain,
            "timestamp": self.clock.isoformat().replace("+00:00", "Z"),
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "is_error": is_error,
                        "content": output,
                    }
                ],
            },
        }
        if interrupted:
            row["toolUseResult"] = {"interrupted": True, "stdout": "", "stderr": ""}
        self.rows.append(row)
        return uid

    def step(self, parent: str, tool: str, tool_input: dict, output: str, **kw) -> str:
        """A call plus its result, advancing the clock by ``gap`` seconds."""
        gap = kw.pop("gap", 2.0)
        is_error = kw.pop("is_error", False)
        interrupted = kw.pop("interrupted", False)
        sidechain = kw.pop("sidechain", False)
        call_uid, tool_use_id = self.call(parent, tool, tool_input, sidechain=sidechain, **kw)
        self.tick(gap)
        return self.result(
            call_uid,
            tool_use_id,
            output,
            is_error=is_error,
            sidechain=sidechain,
            interrupted=interrupted,
        )


def build() -> list[dict]:
    b = Builder()
    root = b.user("fix the auth bug")

    # retry loop: the same Bash command four times in a row
    cursor = root
    for _ in range(4):
        cursor = b.step(cursor, "Bash", {"command": "bun test", "description": "run tests"}, "1 failing")
        b.tick(3)

    # stall: a long-running but plausible tool call
    cursor = b.step(cursor, "Bash", {"command": "pip install -r requirements.txt"}, "ok", gap=240)

    # idle gap: far longer than any tool runs
    cursor = b.step(cursor, "Bash", {"command": "echo waiting"}, "waiting", gap=7200)

    # interrupted call -- long, but must never be reported
    cursor = b.step(cursor, "Bash", {"command": "sleep 9999"}, "", gap=3600, interrupted=True)

    # error cluster: three identical failures with volatile detail
    for n in range(3):
        cursor = b.step(
            cursor,
            "Edit",
            {"file_path": f"/repo/src/mod{n}.py", "old_string": "a", "new_string": "b"},
            f"<tool_use_error>File has not been read yet. Read /repo/src/mod{n}.py first.</tool_use_error>",
            is_error=True,
        )

    # stale read: same file read twice, contents differ, no write between
    cursor = b.step(cursor, "Read", {"file_path": "/repo/src/auth.py"}, "version one")
    cursor = b.step(cursor, "Read", {"file_path": "/repo/src/auth.py"}, "version two, changed underneath")

    # blind edit: written without ever being read
    cursor = b.step(
        cursor, "Edit", {"file_path": "/repo/src/untouched.py", "old_string": "x", "new_string": "y"}, "ok"
    )

    # thrash: one file written five times
    for n in range(5):
        cursor = b.step(
            cursor,
            "Write",
            {"file_path": "/repo/src/thrashed.py", "content": f"attempt {n}"},
            "ok",
        )

    # a subagent branch with its own spend, on a different model
    sub_parent = cursor
    for n in range(3):
        sub_parent = b.step(
            sub_parent,
            "Read",
            {"file_path": f"/repo/docs/page{n}.md"},
            f"doc {n}",
            sidechain=True,
            model="claude-haiku-5-5",
            usage=_usage(out=120, read=4_000, write_1h=0, write_5m=1_000),
        )

    # an unpriced model, so the "cost is a floor" path is exercised
    b.step(
        cursor,
        "Bash",
        {"command": "echo done"},
        "done",
        model="some-future-model-9",
        usage=_usage(out=50, read=100, write_1h=0),
    )

    # a client bookkeeping row the adapter must tolerate, not crash on
    b.rows.append({"type": "custom-title", "sessionId": "synthetic", "customTitle": "synthetic run"})
    b.rows.append({"type": "brand-new-row-type", "sessionId": "synthetic", "whatever": 1})
    return b.rows


def write(path: str | None = None) -> str:
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "synthetic.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for row in build():
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    return path


if __name__ == "__main__":
    print(write())
