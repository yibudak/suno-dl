from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _project_metadata() -> dict:
    tomllib = pytest.importorskip("tomllib", reason="tomllib needs Python 3.11+")
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def test_module_version_matches_pyproject():
    import suno_dl

    assert suno_dl.__version__ == _project_metadata()["project"]["version"]


def test_entry_point_is_exported():
    import suno_dl

    assert callable(suno_dl.main)


def test_readme_is_present_and_mentions_the_command():
    text = (ROOT / "README.md").read_text()
    assert "suno-dl" in text


def test_no_credentials_are_committed():
    """A crude guard: no long JWT-looking blobs inside the tracked source."""
    pattern = re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.")
    for path in [ROOT / "suno_dl.py", ROOT / "README.md"]:
        assert not pattern.search(path.read_text()), f"possible token in {path.name}"
