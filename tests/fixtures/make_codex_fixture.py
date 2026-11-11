"""Build the synthetic Codex rollout the adapter tests run against.

Hand-written for the same reason as the Claude Code fixture: every behaviour
worth pinning gets a case planted for it, including the two that are easy to
get wrong — cumulative token counts that must be differenced rather than
summed, and a patch call that touches several files at once.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

T0 = datetime(2026, 5, 2, 10, 0, 0, tzinfo=timezone.utc)
CWD = "/repo/codex-project"


class Builder:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.clock = T0
        self.calls = 0
        # Cumulative counters, exactly as Codex reports them.
        self.total = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
        }

    def _row(self, row_type: str, payload: dict) -> None:
        self.rows.append(
            {
                "timestamp": self.clock.isoformat().replace("+00:00", "Z"),
                "type": row_type,
                "payload": payload,
            }
        )

    def tick(self, seconds: float) -> None:
        self.clock += timedelta(seconds=seconds)

    def session(self, model: str = "gpt-5.4") -> None:
        self._row("session_meta", {"id": "synthetic-codex", "cwd": CWD,
                                   "cli_version": "0.0.0-test", "originator": "test"})
        self._row("turn_context", {"cwd": CWD, "model": model})

    def turn(self, *, inp: int, cached: int, out: int, reasoning: int = 0,
             repeat_reading: bool = False) -> None:
        """Advance the cumulative counters and emit a token_count row.

        ``repeat_reading`` emits the *same* cumulative totals a second time,
        which is what makes naive summing of per-request usage over-count.
        """
        self.total["input_tokens"] += inp
        self.total["cached_input_tokens"] += cached
        self.total["output_tokens"] += out
        self.total["reasoning_output_tokens"] += reasoning
        payload = {
            "type": "token_count",
            "info": {
                "total_token_usage": dict(self.total),
                "last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                     "output_tokens": out,
                                     "reasoning_output_tokens": reasoning},
                "model_context_window": 258400,
            },
        }
        self._row("event_msg", payload)
        if repeat_reading:
            self._row("event_msg", json.loads(json.dumps(payload)))

    def user(self, text: str) -> None:
        self._row("response_item", {"type": "message", "role": "user",
                                    "content": [{"type": "input_text", "text": text}]})

    def exec_command(self, cmd: str, output: str, exit_code: int = 0, gap: float = 2.0) -> None:
        self.calls += 1
        call_id = f"call_{self.calls:04d}"
        self._row("response_item", {
            "type": "function_call", "name": "exec_command", "call_id": call_id,
            "arguments": json.dumps({"cmd": cmd, "workdir": CWD, "max_output_tokens": 200}),
        })
        self.tick(gap)
        body = f"Command: /bin/zsh -lc {cmd}\nProcess exited with code {exit_code}\nOutput:\n{output}\n"
        self._row("response_item", {"type": "function_call_output", "call_id": call_id,
                                    "output": body})

    def apply_patch(self, operations: list[tuple[str, str]], gap: float = 1.0,
                    tag: str = "") -> None:
        """``operations`` is a list of (verb, path) with verb Add/Update/Delete.

        ``tag`` varies the patch body, so repeated edits to one file are
        thrash without also being an identical-input retry loop.
        """
        self.calls += 1
        call_id = f"call_{self.calls:04d}"
        body = "*** Begin Patch\n"
        for verb, path in operations:
            body += f"*** {verb} File: {path}\n+placeholder {tag}\n"
        body += "*** End Patch\n"
        self._row("response_item", {"type": "custom_tool_call", "status": "completed",
                                    "call_id": call_id, "name": "apply_patch", "input": body})
        self.tick(gap)
        touched = "\n".join(f"{verb[0]} {path}" for verb, path in operations)
        self._row("response_item", {
            "type": "custom_tool_call_output", "call_id": call_id,
            "output": json.dumps({"output": f"Success. Updated the following files:\n{touched}"}),
        })

    def compaction(self) -> None:
        self._row("compacted", {"message": "context summarised"})


def build() -> list[dict]:
    b = Builder()
    b.session()
    b.user("build the terminal view")
    b.turn(inp=12_000, cached=9_000, out=400, reasoning=40)

    # A real retry loop: the same build command four times running.
    for _ in range(4):
        b.exec_command("npm run build", "error TS2345: type mismatch", exit_code=1)
        b.tick(3)
    b.turn(inp=8_000, cached=7_500, out=300, repeat_reading=True)

    # One patch touching three files at once: two created, one edited in place.
    b.apply_patch([
        ("Add", "/repo/codex-project/src/terminal.js"),
        ("Add", "/repo/codex-project/src/helpers.js"),
        ("Update", "/repo/codex-project/package.json"),
    ])

    # Dense rewrites of a file the patch created: thrash, not a blind edit.
    for n in range(4):
        b.apply_patch([("Update", "/repo/codex-project/src/terminal.js")], tag=f"rev{n}")
    b.turn(inp=6_000, cached=5_000, out=250)

    # Read through the shell, then patched: seen, so not a blind edit.
    b.exec_command("sed -n '1,200p' src/components/Viewer.jsx", "export const Viewer = () => null")
    b.apply_patch([("Update", "/repo/codex-project/src/components/Viewer.jsx")], tag="seen")

    # A large command output, then a compaction that ends its reach.
    b.exec_command("cat build/verbose.log", "LOG LINE padding here\n" * 1200)
    # Unique filler, so the only retry loop in the fixture is the planted one.
    for n in range(6):
        b.exec_command(f"echo before {n}", "ok", gap=1)
        b.turn(inp=2_000, cached=1_800, out=80)
    b.compaction()
    for n in range(6):
        b.exec_command(f"echo after {n}", "ok", gap=1)
        b.turn(inp=2_000, cached=1_800, out=80)

    return b.rows


def write(path: str | None = None) -> str:
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "codex.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for row in build():
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    return path


if __name__ == "__main__":
    print(write())
