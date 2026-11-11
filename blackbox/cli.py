"""Command line interface.

    blackbox run.jsonl                      # report to stdout
    blackbox run.jsonl --html report.html   # self-contained page
    blackbox run.jsonl --db run.duckdb      # queryable event store
    blackbox run.jsonl --verify             # prove the findings replay exactly
    blackbox --replay run.duckdb            # re-derive from a stored run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

from blackbox.adapters.registry import ADAPTERS, NoAdapterError
from blackbox.analysis import analyze
from blackbox.detectors.base import Config
from blackbox.pricing import PRICING_AS_OF
from blackbox.report import html as html_report
from blackbox.report import text as text_report

VERSION = "0.1.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blackbox",
        description=(
            "Structured analytics for one agent run. Drop in a trace file; get cost "
            "attribution, failure clusters and a deterministic replay. No SDK, no "
            "vendor, no model calls."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"adapters: {', '.join(m.NAME for m in ADAPTERS)} · prices as of {PRICING_AS_OF}",
    )
    parser.add_argument("trace", nargs="?", help="path to an agent trace file (.jsonl)")
    parser.add_argument("--replay", metavar="DB", help="re-derive the report from a stored run")
    parser.add_argument("--adapter", help="force an adapter instead of sniffing")

    out = parser.add_argument_group("output")
    out.add_argument("--html", metavar="PATH", help="write a self-contained HTML report")
    out.add_argument("--json", metavar="PATH", help="write the canonical payload as JSON")
    out.add_argument("--db", metavar="PATH", help="write the DuckDB event store")
    out.add_argument("-q", "--quiet", action="store_true", help="suppress the stdout report")
    out.add_argument(
        "--max-findings",
        metavar="N",
        default="25",
        help="findings to print: a number or 'all' (default 25)",
    )

    thresholds = parser.add_argument_group("thresholds")
    thresholds.add_argument("--stall-seconds", type=float, default=Config.stall_seconds)
    thresholds.add_argument("--idle-seconds", type=float, default=Config.idle_seconds)
    thresholds.add_argument("--retry-min-repeats", type=int, default=Config.retry_min_repeats)
    thresholds.add_argument("--retry-window", type=int, default=Config.retry_window)
    thresholds.add_argument("--thrash-min-edits", type=int, default=Config.thrash_min_edits)
    thresholds.add_argument("--error-cluster-min", type=int, default=Config.error_cluster_min)

    parser.add_argument(
        "--verify",
        action="store_true",
        help="store the run, replay it, and check the findings are byte-identical",
    )
    parser.add_argument("--version", action="version", version=f"blackbox {VERSION}")
    return parser


def _max_findings(raw: str) -> int | None:
    if raw.lower() == "all":
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        raise SystemExit(f"--max-findings expects a number or 'all', got {raw!r}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.trace and not args.replay:
        parser.print_help()
        return 2
    if args.trace and args.replay:
        print("error: pass a trace file or --replay, not both", file=sys.stderr)
        return 2

    config = Config(
        stall_seconds=args.stall_seconds,
        idle_seconds=args.idle_seconds,
        retry_min_repeats=args.retry_min_repeats,
        retry_window=args.retry_window,
        thrash_min_edits=args.thrash_min_edits,
        error_cluster_min=args.error_cluster_min,
    )

    try:
        if args.replay:
            from blackbox import replay as replay_mod

            if not os.path.exists(args.replay):
                print(f"error: no such store: {args.replay}", file=sys.stderr)
                return 1
            analysis = replay_mod.replay(args.replay)
        else:
            if not os.path.exists(args.trace):
                print(f"error: no such file: {args.trace}", file=sys.stderr)
                return 1
            analysis = analyze(args.trace, config=config, adapter=args.adapter)
    except NoAdapterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(text_report.render(analysis, max_findings=_max_findings(args.max_findings)))

    if args.db:
        from blackbox import store

        store.write(args.db, analysis)
        print(f"wrote event store  {args.db}")

    if args.html:
        with open(args.html, "w", encoding="utf-8") as fh:
            fh.write(html_report.render(analysis))
        print(f"wrote html report  {args.html}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(analysis.payload(), fh, indent=2, sort_keys=True, default=str)
            fh.write("\n")
        print(f"wrote json payload {args.json}")

    if args.verify:
        return _verify(analysis)

    return 0


def _verify(analysis) -> int:
    """Store, replay and compare -- the determinism claim, checked on real input."""
    from blackbox import replay as replay_mod
    from blackbox import store

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "verify.duckdb")
        store.write(db, analysis)
        again = replay_mod.replay(db)

        original = analysis.digest()
        replayed = again.digest()
        if original == replayed:
            print(
                f"verify OK   {len(analysis.findings)} findings replayed byte-identical "
                f"from the event store, with no model calls\n"
                f"            digest {original}"
            )
            return 0

        print("verify FAILED   replay did not reproduce the original payload", file=sys.stderr)
        print(f"  original {original}", file=sys.stderr)
        print(f"  replayed {replayed}", file=sys.stderr)
        return 1
