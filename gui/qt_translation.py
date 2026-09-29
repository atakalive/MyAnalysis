"""Qt 標準ボタン等の翻訳（qtbase_<lang>.qm）を QApplication に載せ替える。"""
from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QLibraryInfo, QTranslator

_APP_ATTR = "_myanalysis_qtbase_translator"


def install_qt_translator(app: QCoreApplication, lang: str) -> bool:
    """app に載っている前回の translator を外し、lang 用の qtbase を載せる。

    en は Qt の原文なので何も載せない（外すだけ）。.qm が無い言語も外すだけ。
    translator は親無しで作り、参照は app の属性だけに持つ（app を親にすると
    外しても app の子として残り、切替のたびに増える）。外した translator は
    属性の参照が切れた時点で解放される。
    app の属性に持つ理由: ホットリロード Tier 3 は gui.* をパージして再 import
    するが、QApplication は同じインスタンスなので、モジュール変数では前回の
    translator を外せない。
    戻り値は translator を載せたかどうか。never raise。
    """
    try:
        old = getattr(app, _APP_ATTR, None)
        if old is not None:
            app.removeTranslator(old)
            setattr(app, _APP_ATTR, None)
        if lang == "en":
            return False
        t = QTranslator()
        path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        if not t.load(f"qtbase_{lang}", path):
            return False
        if not app.installTranslator(t):
            return False
        setattr(app, _APP_ATTR, t)
        return True
    except Exception:
        return False
