"""Per-tab view snapshot. PNG of the entire AnalysisTab QWidget."""
from pathlib import Path
from collections.abc import Callable

from common.paths import atomic_write_bytes
import dataset_config


def writer(dataset: str, name: str) -> Callable[[object], None]:
    """Return a writer; pass an AnalysisTab (or any QWidget). PNG goes to current_view.png.

    Implementation: tab.grab() returns a QPixmap → PNG bytes via QBuffer → common.paths の
    書込 chokepoint。`QPixmap.save(str(path))` に**パスを渡してはならない**（Issue #96）:
    QFile が WriteOnly|Truncate で開くため、同期マウント上で書込が失敗すると 0 バイトの PNG が
    残り、しかも失敗が検証されない。バイト列を渡せば戦略切替・read-back 検証・リトライが効く。
    （QBuffer 経由の PNG 化は meeting/relay.py に既存の実例がある）

    Raises OSError if the save fails (disk full, permissions, verification failure).
    """
    target = dataset_config.state_dir(dataset, name, create=True) / "current_view.png"

    def _write(tab) -> None:
        from PySide6.QtCore import QBuffer, QByteArray

        pixmap = tab.grab()
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QBuffer.OpenModeFlag.WriteOnly)
        try:
            if not pixmap.save(buf, "PNG"):
                raise OSError(f"failed to encode snapshot PNG for {target}")
        finally:
            buf.close()
        atomic_write_bytes(target, bytes(ba))
    return _write


def path(dataset: str, name: str) -> Path:
    return dataset_config.state_dir(dataset, name, create=False) / "current_view.png"
