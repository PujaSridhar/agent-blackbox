from __future__ import annotations

import json
import os

from blackbox.cli import main


def test_report_to_stdout(fixture_path, capsys):
    assert main([fixture_path]) == 0
    out = capsys.readouterr().out
    assert "AGENT BLACK BOX" in out
    assert "COST" in out
    assert "FINDINGS" in out


def test_verify_round_trips(fixture_path, capsys):
    assert main([fixture_path, "--quiet", "--verify"]) == 0
    assert "verify OK" in capsys.readouterr().out


def test_json_payload_is_written(fixture_path, tmp_path, capsys):
    out = tmp_path / "payload.json"
    assert main([fixture_path, "--quiet", "--json", str(out)]) == 0
    payload = json.loads(out.read_text())
    assert payload["adapter"] == "claude_code"
    assert payload["summary"]["tool_calls"] > 0
    assert payload["findings"]


def test_html_report_is_self_contained(fixture_path, tmp_path, capsys):
    out = tmp_path / "report.html"
    assert main([fixture_path, "--quiet", "--html", str(out)]) == 0
    page = out.read_text()
    assert page.startswith("<!doctype html>")
    # No scripts and no outbound requests: it must render from a USB stick.
    assert "<script" not in page
    assert "http://" not in page and "https://" not in page
    assert page.count("<div") == page.count("</div>")


def test_db_then_replay(fixture_path, tmp_path, capsys):
    db = tmp_path / "run.duckdb"
    assert main([fixture_path, "--quiet", "--db", str(db)]) == 0
    capsys.readouterr()
    assert main(["--replay", str(db)]) == 0
    assert "AGENT BLACK BOX" in capsys.readouterr().out


def test_thresholds_change_the_findings(fixture_path, capsys):
    main([fixture_path, "--stall-seconds", "1", "--max-findings", "all"])
    loose = capsys.readouterr().out
    main([fixture_path, "--stall-seconds", "100000", "--max-findings", "all"])
    strict = capsys.readouterr().out
    assert loose.count("stall") > strict.count("stall")


def test_missing_file_is_an_error(capsys):
    assert main(["/nonexistent/trace.jsonl"]) == 1
    assert "no such file" in capsys.readouterr().err


def test_unrecognised_format_is_rejected(tmp_path, capsys):
    bogus = tmp_path / "notatrace.jsonl"
    bogus.write_text("this is not json\n")
    assert main([str(bogus)]) == 1
    assert "no adapter recognised" in capsys.readouterr().err


def test_trace_and_replay_are_mutually_exclusive(fixture_path, tmp_path, capsys):
    assert main([fixture_path, "--replay", str(tmp_path / "x.duckdb")]) == 2


def test_no_arguments_prints_help(capsys):
    assert main([]) == 2
    assert "usage:" in capsys.readouterr().out
