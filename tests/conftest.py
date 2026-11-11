from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic.jsonl"


@pytest.fixture(scope="session")
def fixture_path() -> str:
    """The synthetic trace, regenerated if missing so the suite is self-contained."""
    if not FIXTURE.exists():
        from tests.fixtures import make_fixture

        make_fixture.write(str(FIXTURE))
    return str(FIXTURE)


@pytest.fixture(scope="session")
def analysis(fixture_path):
    from blackbox.analysis import analyze

    return analyze(fixture_path)
