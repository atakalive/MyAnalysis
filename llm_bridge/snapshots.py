"""Per-tab view snapshot. PNG of the entire AnalysisTab QWidget."""
from pathlib import Path
from collections.abc import Callable
from common.paths import state_dir


def writer(name: str) -> Callable[[object], None]:
    """Return a writer; pass an AnalysisTab (or any QWidget). PNG goes to current_view.png.

    Implementation: tab.grab() returns a QPixmap; save as PNG.
    Raises OSError if the save fails (disk full, permissions, etc.).
    """
    target = state_dir(name) / "current_view.png"
    def _write(tab) -> None:
        pixmap = tab.grab()
        if not pixmap.save(str(target), "PNG"):
            raise OSError(f"failed to save snapshot to {target}")
    return _write


def path(name: str) -> Path:
    return state_dir(name) / "current_view.png"
