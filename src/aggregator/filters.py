"""Hot-reloading filters.md loader.

The aggregator polls mtime on this file every ~5s; if it has changed, the next Claude
call uses the updated text. No restart required.
"""

from __future__ import annotations

from pathlib import Path


class FiltersFile:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._text: str = ""
        self._mtime: float | None = None
        self.reload_if_changed()

    def reload_if_changed(self) -> bool:
        """Re-read the file if its mtime has changed. Returns True if reloaded."""
        if not self.path.exists():
            if self._text != "":
                self._text = ""
                self._mtime = None
                return True
            return False
        mtime = self.path.stat().st_mtime
        if mtime == self._mtime:
            return False
        self._text = self.path.read_text(encoding="utf-8")
        self._mtime = mtime
        return True

    @property
    def text(self) -> str:
        return self._text
