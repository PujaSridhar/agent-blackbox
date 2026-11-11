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


def test_no_arguments_discovers_the_current_directory(capsys, tmp_path, monkeypatch):
    """Nobody remembers a session UUID. With no arguments the tool looks for
    the latest run of the directory you are standing in."""
    monkeypatch.chdir(tmp_path)
    assert main([]) == 1
    err = capsys.readouterr().err
    # The failure must say where it looked, not just that it found nothing.
    assert "Looked in:" in err
    assert "blackbox ls" in err



# --- discovery, listing and corpus scans ----------------------------------

def test_cwd_encoding_matches_the_on_disk_convention():
    from blackbox.discover import encode_cwd

    assert encode_cwd("/Users/me/Documents/my-app") == "-Users-me-Documents-my-app"
    assert encode_cwd("/Users/me/untitled folder") == "-Users-me-untitled-folder"


def test_no_arguments_uses_the_latest_session(tmp_path, monkeypatch, capsys, fixture_path):
    """Plant two sessions for a directory and check the newer one is chosen."""
    import os
    import shutil

    from blackbox import discover

    work = tmp_path / "project"
    work.mkdir()
    root = tmp_path / "projects"
    project_dir = root / discover.encode_cwd(str(work))
    project_dir.mkdir(parents=True)

    older = project_dir / "aaaa.jsonl"
    newer = project_dir / "bbbb.jsonl"
    shutil.copy(fixture_path, older)
    shutil.copy(fixture_path, newer)
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    monkeypatch.setattr(discover, "DEFAULT_ROOT", str(root))
    monkeypatch.chdir(work)

    assert main([]) == 0
    out = capsys.readouterr().out
    assert "bbbb.jsonl" in out
    assert "aaaa.jsonl" not in out


def test_ls_lists_recent_sessions(tmp_path, capsys, fixture_path, monkeypatch):
    import shutil

    from blackbox import discover

    root = tmp_path / "projects"
    (root / "-Users-me-app").mkdir(parents=True)
    shutil.copy(fixture_path, root / "-Users-me-app" / "sess.jsonl")
    monkeypatch.setattr(discover, "DEFAULT_ROOT", str(root))

    assert main(["ls", str(root)]) == 0
    out = capsys.readouterr().out
    assert "sess" in out
    # The label is the working directory the transcript records inside itself,
    # which beats decoding it back out of a lossy directory name.
    assert "/repo" in out


def test_ls_on_an_empty_root_is_an_error(tmp_path, capsys):
    assert main(["ls", str(tmp_path)]) == 1
    assert "no transcripts found" in capsys.readouterr().err


def test_directory_input_aggregates(tmp_path, capsys, fixture_path):
    import shutil

    folder = tmp_path / "runs"
    folder.mkdir()
    shutil.copy(fixture_path, folder / "one.jsonl")
    shutil.copy(fixture_path, folder / "two.jsonl")

    assert main([str(folder)]) == 0
    out = capsys.readouterr().out
    assert "2 run(s)" in out
    assert "SPEND" in out


def test_scan_survives_an_unreadable_file(tmp_path, capsys, fixture_path):
    import shutil

    folder = tmp_path / "runs"
    folder.mkdir()
    shutil.copy(fixture_path, folder / "good.jsonl")
    (folder / "bad.jsonl").write_text("not a trace at all\n")

    assert main(["scan", str(folder)]) == 0
    captured = capsys.readouterr()
    assert "1 run(s)" in captured.out
    assert "could not be read" in captured.out
    assert "skipped bad.jsonl" in captured.err


def test_corpus_json_summary(tmp_path, fixture_path, capsys):
    import json as json_mod
    import shutil

    folder = tmp_path / "runs"
    folder.mkdir()
    shutil.copy(fixture_path, folder / "one.jsonl")
    out = tmp_path / "corpus.json"

    assert main([str(folder), "--quiet", "--json", str(out)]) == 0
    payload = json_mod.loads(out.read_text())
    assert payload["runs"] == 1
    assert payload["total_usd"] > 0
    assert "cache_hit_rate" in payload


def test_discovery_handles_symlinked_paths(tmp_path, monkeypatch, capsys, fixture_path):
    """A path and its resolved form encode differently. The session was
    recorded under one of them; the shell may report the other."""
    import os
    import shutil

    from blackbox import discover

    real = tmp_path / "real_project"
    real.mkdir()
    link = tmp_path / "linked_project"
    os.symlink(real, link)

    root = tmp_path / "projects"
    # Record the session under the RESOLVED path only.
    project_dir = root / discover.encode_cwd(str(real))
    project_dir.mkdir(parents=True)
    shutil.copy(fixture_path, project_dir / "sess.jsonl")

    monkeypatch.setattr(discover, "DEFAULT_ROOT", str(root))
    # ...but stand in the symlinked one.
    assert discover.latest_for_cwd(str(link), str(root)) is not None


# --- the installed package, not just the checkout --------------------------

def test_public_api_is_importable():
    import blackbox

    assert blackbox.__version__
    for name in ("analyze", "replay", "Analysis", "Config", "Finding", "Event"):
        assert hasattr(blackbox, name), name


def test_replay_function_does_not_break_the_replay_module(fixture_path, tmp_path):
    """`blackbox.replay` is the public function and shadows the module of the
    same name. Internal code must still be able to reach the module, or
    --verify breaks for anyone who installs the package."""
    import blackbox
    from blackbox import store
    from blackbox.replay import replay as replay_run

    assert callable(blackbox.replay)

    original = blackbox.analyze(fixture_path)
    db = str(tmp_path / "shadow.duckdb")
    store.write(db, original)

    assert replay_run(db).digest() == original.digest()
    assert blackbox.replay(db).digest() == original.digest()


def test_verify_works_end_to_end(fixture_path, capsys):
    """The exact path that broke when the public API shadowed the module."""
    assert main([fixture_path, "--quiet", "--verify"]) == 0
    assert "verify OK" in capsys.readouterr().out
