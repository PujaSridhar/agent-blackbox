"""Adapter for OpenAI Codex CLI rollouts (``~/.codex/sessions/**/rollout-*.jsonl``).

Written to answer a fair challenge: a canonical model designed against one
vendor's logs has not been shown to be canonical. This adapter exists to test
that claim against a format that differs from Claude Code's in every surface
detail -- and it required no change to the graph, the cost engine, or any
detector.

What differs, and how it maps:

* **No parent pointers.** Codex rollouts are a flat sequence, so the causal
  spine is rebuilt from ``call_id`` and from turn order rather than read off
  the rows.
* **Usage is cumulative, not per-turn.** ``token_count`` events carry a
  running ``total_token_usage``. Summing the per-request ``last_token_usage``
  double counts -- verified against this corpus, where it overstated output
  tokens by about 5% -- so each turn's usage is the delta between consecutive
  cumulative readings.
* **Input tokens include cached ones.** OpenAI reports ``input_tokens`` as the
  total and ``cached_input_tokens`` as the share served from cache, so fresh
  input is the difference. There is no separate cache-write charge, so those
  tiers stay zero rather than being invented.
* **One patch call edits many files.** ``apply_patch`` is normalised into the
  canonical multi-file operation list, which is why ``thrash`` and
  ``contradiction`` work on Codex traces unchanged.
* **Compaction is recorded.** Codex writes a row when it summarises and clears
  the context. That boundary is kept, because a tool result dumped before one
  stops being re-sent after it -- which is the difference between an estimate
  and a guess in :mod:`blackbox.detectors.context_bloat`.
"""

from __future__ import annotations

import json
import re
from typing import Any

from blackbox.adapters._linking import link_results_to_calls
from blackbox.adapters.base import Trace
from blackbox.events import (
    ASSISTANT_MESSAGE,
    META,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    Event,
    Usage,
    parse_ts,
)
from blackbox.toolinfo import EDIT, MULTI_FILE_KEY, WRITE

NAME = "codex"

_ROW_TYPES = {"session_meta", "response_item", "event_msg", "turn_context", "compacted"}

# Marks a point where the context window was summarised and cleared.
# Anything carried before it stops being re-sent afterwards.
COMPACTION_FLAG = "compaction"

_EXIT = re.compile(r"process exited with code (\d+)", re.I)
_PATCH_OP = re.compile(r"^\*\*\*\s+(Add|Update|Delete)\s+File:\s*(.+?)\s*$", re.M)

_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def sniff(path: str) -> bool:
    """True when the first usable line looks like a Codex rollout row."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for _ in range(20):
                line = fh.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    return False
                if not isinstance(row, dict):
                    return False
                return row.get("type") in _ROW_TYPES and isinstance(row.get("payload"), dict)
    except OSError:
        return False
    return False


def _delta_usage(current: dict[str, Any], previous: dict[str, int]) -> tuple[Usage, dict[str, int]]:
    """Per-turn usage from two cumulative readings.

    A decrease would mean the counter restarted; the reading is then taken as
    the delta rather than producing a negative, which would silently subtract
    from the run's total.
    """
    latest = {field: int(current.get(field) or 0) for field in _USAGE_FIELDS}
    delta = {}
    for field, value in latest.items():
        previous_value = previous.get(field, 0)
        delta[field] = value - previous_value if value >= previous_value else value

    cached = delta["cached_input_tokens"]
    total_input = delta["input_tokens"]
    return (
        Usage(
            # OpenAI counts cached tokens inside input_tokens, so fresh input
            # is the remainder. There is no cache-write charge to record.
            input_tokens=max(total_input - cached, 0),
            output_tokens=delta["output_tokens"],
            cache_read_tokens=cached,
            thinking_tokens=delta["reasoning_output_tokens"],
        ),
        latest,
    )


def _patch_files(patch: str) -> list[dict[str, str]]:
    """The files an ``apply_patch`` call touches, with how it touches each."""
    operations = []
    for verb, path in _PATCH_OP.findall(patch or ""):
        # Adding or deleting replaces the file wholesale, so the agent knows
        # the result either way; updating edits content it must already know.
        operations.append({"path": path.strip(), "op": EDIT if verb == "Update" else WRITE})
    return operations


def _result_ok(output: str) -> bool | None:
    """Whether a tool result reports success, or None when it does not say."""
    if not output:
        return None
    head = output[:800]
    match = _EXIT.search(head)
    if match:
        return match.group(1) == "0"
    lowered = head.lower()
    if "failed for" in lowered or lowered.startswith("error") or '"error"' in lowered:
        return False
    if "success." in lowered:
        return True
    return None


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def parse(path: str) -> Trace:
    events: list[Event] = []
    seq = 0
    skipped = 0
    model: str | None = None
    cwd: str | None = None
    session_id: str | None = None
    cli_version: str | None = None
    previous_usage: dict[str, int] = {}
    last_turn_uid: str | None = None

    def add(event: Event) -> Event:
        nonlocal seq
        events.append(event)
        seq += 1
        return event

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(row, dict):
                skipped += 1
                continue

            payload = row.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            row_type = str(row.get("type") or "unknown")
            sub_type = str(payload.get("type") or "")
            ts = parse_ts(row.get("timestamp"))

            if row_type == "session_meta":
                session_id = session_id or payload.get("id")
                cwd = cwd or payload.get("cwd")
                cli_version = cli_version or payload.get("cli_version")
                model = payload.get("model") or model
                add(Event(uid=f"line{lineno}", seq=seq, kind=META, ts=ts,
                          raw_type=row_type, source_line=lineno))
                continue

            if row_type == "turn_context":
                # The model can change between turns; later turns bill at the
                # new rate, so this is tracked rather than read once.
                model = payload.get("model") or model
                cwd = cwd or payload.get("cwd")
                add(Event(uid=f"line{lineno}", seq=seq, kind=META, ts=ts,
                          raw_type=row_type, source_line=lineno))
                continue

            if row_type == "compacted" or (
                row_type == "event_msg" and sub_type == "context_compacted"
            ):
                add(Event(uid=f"line{lineno}", seq=seq, kind=META, ts=ts,
                          flags=(COMPACTION_FLAG,), raw_type="compacted",
                          source_line=lineno))
                continue

            if row_type == "event_msg":
                if sub_type == "token_count":
                    info = payload.get("info")
                    if isinstance(info, dict):
                        total = info.get("total_token_usage")
                        if isinstance(total, dict):
                            usage, previous_usage = _delta_usage(total, previous_usage)
                            if not usage.is_empty():
                                turn = add(
                                    Event(
                                        uid=f"line{lineno}",
                                        seq=seq,
                                        kind=ASSISTANT_MESSAGE,
                                        ts=ts,
                                        model=model,
                                        usage=usage,
                                        raw_type="token_count",
                                        source_line=lineno,
                                    )
                                )
                                last_turn_uid = turn.uid
                                continue
                add(Event(uid=f"line{lineno}", seq=seq, kind=META, ts=ts,
                          text=_text_of(payload.get("message"))[:2000],
                          raw_type=f"{row_type}/{sub_type}" if sub_type else row_type,
                          source_line=lineno))
                continue

            if row_type != "response_item":
                skipped += 1
                continue

            if sub_type == "message":
                role = payload.get("role")
                text = _text_of(payload.get("content"))
                kind = USER_MESSAGE if role == "user" else META
                add(Event(uid=f"line{lineno}", seq=seq, kind=kind, ts=ts, text=text[:4000],
                          raw_type=f"message/{role}", source_line=lineno))
                continue

            if sub_type in ("function_call", "custom_tool_call", "web_search_call",
                            "tool_search_call"):
                name = payload.get("name") or sub_type
                call_id = payload.get("call_id") or payload.get("id")

                tool_input: dict[str, Any] = {}
                raw_args = payload.get("arguments")
                if isinstance(raw_args, str):
                    try:
                        parsed = json.loads(raw_args)
                        if isinstance(parsed, dict):
                            tool_input = parsed
                    except json.JSONDecodeError:
                        tool_input = {"arguments": raw_args}
                raw_input = payload.get("input")
                if isinstance(raw_input, str):
                    tool_input = {"input": raw_input}
                    if name == "apply_patch":
                        files = _patch_files(raw_input)
                        if files:
                            tool_input[MULTI_FILE_KEY] = files
                if isinstance(payload.get("query"), str):
                    tool_input.setdefault("query", payload["query"])

                add(Event(uid=f"line{lineno}", seq=seq, kind=TOOL_CALL, parent_uid=last_turn_uid,
                          ts=ts, model=model, tool_name=name, tool_use_id=call_id,
                          tool_input=tool_input, raw_type=sub_type, source_line=lineno))
                continue

            if sub_type in ("function_call_output", "custom_tool_call_output",
                            "tool_search_output"):
                output = payload.get("output")
                if not isinstance(output, str):
                    output = json.dumps(output, sort_keys=True) if output is not None else ""
                add(Event(uid=f"line{lineno}", seq=seq, kind=TOOL_RESULT,
                          parent_uid=last_turn_uid, ts=ts,
                          tool_use_id=payload.get("call_id"), tool_ok=_result_ok(output),
                          tool_output=output, raw_type=sub_type, source_line=lineno))
                continue

            add(Event(uid=f"line{lineno}", seq=seq, kind=META, ts=ts,
                      raw_type=sub_type or row_type, source_line=lineno))

    events = link_results_to_calls(events)

    return Trace(
        events=events,
        source=path,
        adapter=NAME,
        skipped_lines=skipped,
        meta={
            "session_id": session_id,
            "cwd": cwd,
            "client_versions": [cli_version] if cli_version else [],
        },
    )
