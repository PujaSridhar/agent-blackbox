"""Command line interface.

    blackbox                                # the latest run in this directory
    blackbox run.jsonl                      # one specific trace
    blackbox ~/.claude/projects/my-app/     # every run for one project
    blackbox ls                             # recent sessions, newest first
    blackbox scan                           # every local run, aggregated
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

from blackbox import discover
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
    parser.add_argument(
        "trace",
        nargs="?",
        help=(
            "a trace file, a directory of traces, or one of: "
            "'ls' (recent sessions), 'scan' (every local run). "
            "Omit it to use the latest run for the current directory."
        ),
    )
    parser.add_argument(
        "root",
        nargs="?",
        help="for 'scan' and 'ls': where to look (default ~/.claude/projects)",
    )
    parser.add_argument("--limit", type=int, default=20, help="rows for 'ls' (default 20)")
    parser.add_argument("--replay", metavar="DB", help="re-derive the report from a stored run")
    parser.add_argument("--adapter", help="force an adapter instead of sniffing")
    parser.add_argument(
        "--prices",
        metavar="FILE",
        help=(
            "JSON of {model: {input, output, cache_read?}} in dollars per million "
            "tokens, for models this build does not ship rates for"
        ),
    )

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
    thresholds.add_argument("--thrash-window", type=int, default=Config.thrash_window)
    thresholds.add_argument("--error-cluster-min", type=int, default=Config.error_cluster_min)
    thresholds.add_argument("--bloat-min-tokens", type=int, default=Config.bloat_min_tokens)
    thresholds.add_argument("--bloat-min-usd", type=float, default=Config.bloat_min_usd)

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

    if args.trace == "ls":
        return _list_sessions(args.root, args.limit)
    if args.trace and args.replay:
        print("error: pass a trace file or --replay, not both", file=sys.stderr)
        return 2

    config = Config(
        stall_seconds=args.stall_seconds,
        idle_seconds=args.idle_seconds,
        retry_min_repeats=args.retry_min_repeats,
        retry_window=args.retry_window,
        thrash_min_edits=args.thrash_min_edits,
        thrash_window=args.thrash_window,
        error_cluster_min=args.error_cluster_min,
        bloat_min_tokens=args.bloat_min_tokens,
        bloat_min_usd=args.bloat_min_usd,
    )

    overrides = {}
    if args.prices:
        try:
            with open(args.prices, "r", encoding="utf-8") as fh:
                overrides = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: could not read --prices {args.prices}: {exc}", file=sys.stderr)
            return 2

    if args.trace == "scan":
        return _scan(args.root, config, args, overrides)

    target = args.trace
    if not target and not args.replay:
        session = discover.latest_for_cwd()
        if session is None:
            print(
                "No agent transcripts found for this directory.\n"
                + "".join(f"  Looked in: {d}\n" for d in discover.candidate_dirs())
                + "  Try 'blackbox ls' to see recent sessions, or pass a trace file.",
                file=sys.stderr,
            )
            return 1
        target = session.path
        print(f"using latest session for this directory: {os.path.basename(target)}")

    if target and os.path.isdir(target):
        return _aggregate(discover.traces_in(target), config, args, title=target, overrides=overrides)

    try:
        if args.replay:
            from blackbox import replay as replay_mod

            if not os.path.exists(args.replay):
                print(f"error: no such store: {args.replay}", file=sys.stderr)
                return 1
            analysis = replay_mod.replay(args.replay)
        else:
            if not os.path.exists(target):
                print(f"error: no such file: {target}", file=sys.stderr)
                return 1
            analysis = analyze(
                target, config=config, adapter=args.adapter, price_overrides=overrides
            )
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


def _list_sessions(root: str | None, limit: int) -> int:
    import datetime

    sessions = discover.recent_sessions(root, limit)
    if not sessions:
        where = root or ", ".join(discover.all_roots()) or "any known location"
        print(f"no transcripts found under {where}", file=sys.stderr)
        return 1
    print(f"{'modified':<17}{'size':>9}  {'session':<14} project")
    for session in sessions:
        when = datetime.datetime.fromtimestamp(session.modified).strftime("%Y-%m-%d %H:%M")
        print(
            f"{when:<17}{session.size / 1024:>8.0f}K  "
            f"{session.session_id[:12]:<14} {session.label}"
        )
    return 0


def _aggregate(
    paths: list[str], config: Config, args, title: str, overrides: dict | None = None
) -> int:
    from blackbox import aggregate
    from blackbox.report import text as text_report_mod

    if not paths:
        print(f"no traces found under {title}", file=sys.stderr)
        return 1

    corpus = aggregate.load(paths, config=config, price_overrides=overrides)
    if not corpus.runs:
        print("no traces could be read", file=sys.stderr)
        return 1

    if not args.quiet:
        print(text_report_mod.render_corpus(corpus, title=title))
    for path, reason in corpus.failed:
        print(f"  skipped {os.path.basename(path)}: {reason}", file=sys.stderr)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(corpus.summary(), fh, indent=2, sort_keys=True, default=str)
            fh.write("\n")
        print(f"wrote json summary {args.json}")
    return 0


def _scan(root: str | None, config: Config, args, overrides: dict) -> int:
    if root:
        roots = [root]
        title = f"every run under {root}"
    else:
        roots = discover.all_roots()
        title = "every run on this machine"
        if not roots:
            print("no transcript directories found", file=sys.stderr)
            return 1
    paths: list[str] = []
    for base in roots:
        paths.extend(discover.traces_in(base))
    return _aggregate(paths, config, args, title=title, overrides=overrides)


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
