"""Per-tab view snapshot. PNG of the entire AnalysisTab QWidget."""
from pathlib import Path
from collections.abc import Callable
import dataset_config


def writer(dataset: str, name: str) -> Callable[[object], None]:
    """Return a writer; pass an AnalysisTab (or any QWidget). PNG goes to current_view.png.

    Implementation: tab.grab() returns a QPixmap; save as PNG.
    Raises OSError if the save fails (disk full, permissions, etc.).
    """
    target = dataset_config.state_dir(dataset, name, create=True) / "current_view.png"
    def _write(tab) -> None:
        pixmap = tab.grab()
        if not pixmap.save(str(target), "PNG"):
            raise OSError(f"failed to save snapshot to {target}")
    return _write


def path(dataset: str, name: str) -> Path:
    return dataset_config.state_dir(dataset, name, create=False) / "current_view.png"
