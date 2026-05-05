"""filters.md hot-reload."""

from __future__ import annotations

import os
import time
from pathlib import Path

from aggregator.filters import FiltersFile


def test_initial_load(tmp_path: Path) -> None:
    p = tmp_path / "filters.md"
    p.write_text("rule one\n", encoding="utf-8")
    f = FiltersFile(p)
    assert "rule one" in f.text


def test_reload_on_mtime_change(tmp_path: Path) -> None:
    p = tmp_path / "filters.md"
    p.write_text("rule one\n", encoding="utf-8")
    f = FiltersFile(p)
    # Bump mtime so the change is detectable on filesystems with 1s mtime resolution.
    new_time = time.time() + 5
    p.write_text("rule two\n", encoding="utf-8")
    os.utime(p, (new_time, new_time))
    assert f.reload_if_changed() is True
    assert "rule two" in f.text
    # Second call without further changes is a no-op.
    assert f.reload_if_changed() is False


def test_handles_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "filters.md"  # never created
    f = FiltersFile(p)
    assert f.text == ""
    assert f.reload_if_changed() is False


def test_handles_file_deletion(tmp_path: Path) -> None:
    p = tmp_path / "filters.md"
    p.write_text("rule one\n", encoding="utf-8")
    f = FiltersFile(p)
    p.unlink()
    assert f.reload_if_changed() is True
    assert f.text == ""
