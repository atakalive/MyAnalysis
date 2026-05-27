import pyqtgraph as pg
from PySide6.QtGui import QPalette, QColor
from PySide6.QtWidgets import QApplication

pg.setConfigOption("imageAxisOrder", "row-major")
pg.setConfigOption("background", "#202020")
pg.setConfigOption("foreground", "#dcdcdc")


def apply_dark_theme(app: QApplication) -> None:
    """Fusion style + dark palette。tool.py main() で 1 回呼ぶ。"""
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor("#2b2b2b"))
    pal.setColor(QPalette.ColorRole.WindowText, QColor("#dcdcdc"))
    pal.setColor(QPalette.ColorRole.Base, QColor("#1e1e1e"))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#2b2b2b"))
    pal.setColor(QPalette.ColorRole.Text, QColor("#dcdcdc"))
    pal.setColor(QPalette.ColorRole.Button, QColor("#2b2b2b"))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor("#dcdcdc"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor("#3c3c3c"))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor("#dcdcdc"))
    pal.setColor(QPalette.ColorRole.Highlight, QColor("#3a6dc6"))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ColorGroup.Disabled,
                 QPalette.ColorRole.Text, QColor("#808080"))
    pal.setColor(QPalette.ColorGroup.Disabled,
                 QPalette.ColorRole.ButtonText, QColor("#808080"))
    app.setPalette(pal)
