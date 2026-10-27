"""ヘルプ → バージョン情報… のダイアログ（AboutDialog）。開くたびに作る。pjdeck の about_dialog と同じ形。

バージョンは**プロセスを起動したときの HEAD**（動いているコード）。tool.py が重い import の前に
``.git`` から読み（git は実行しない）、``set_startup_head`` で QApplication の property に置く
（開発 → アプリ再構築（Tier 3）でも変わらない。更新で HEAD が進んでも、再起動するまでは前の版）。
日付はそのコミットの committer の日時（``common.git.read_commit_time``。記録された時差のまま）。

上から「MyAnalysis」、``version_line`` の行とその右の [コピー]（不具合の報告に貼る用。コピーするのは
``version_text`` だけで、作者の行は入れない。押しても閉じない。押すと文字を「コピーしました」に変え、
ボタンは初めから両方の文字が入る幅にして行を動かさない）、作者（GitHub の ``AUTHOR``。名前は
``AUTHOR_URL`` へのリンク）、[閉じる]。Enter は [閉じる]（``setDefault(True)``。ボタン箱をダイアログに
入れてから呼ぶ。入れる前に呼ぶと既定として登録されない）、[コピー] は ``setAutoDefault(False)``、
最初のフォーカスは [閉じる]。Esc でも閉じる。リンクの色は palette に頼らず ``_LINK_COLOR`` を span で
付ける。``setOpenExternalLinks(True)`` は C++ の中で開いて差し替えられないので使わず、
``linkActivated`` で受ける。ブラウザを開く ``_open_url`` とクリップボードに書く ``_set_clipboard`` は
テストが差し替える（メソッドの中からモジュールの名前で呼ぶ）。
"""
from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from common.i18n import tr

_log = logging.getLogger(__name__)

PRODUCT = "MyAnalysis"
AUTHOR = "atakalive"
AUTHOR_URL = "https://github.com/atakalive"
# リンクの色は palette に頼らない（暗いテーマの palette の Link は環境で変わる）。地 #2b2b2b に対して約 6.5:1。
_LINK_COLOR = "#6cb4ff"
_START_HEAD_PROPERTY = "myanalysis_start_head"


def set_startup_head(app, head: str | None) -> None:
    """起動時の HEAD を QApplication に置く（tool.py の main から）。分からなければ None。"""
    app.setProperty(_START_HEAD_PROPERTY, head or "")


def startup_head() -> str | None:
    """起動時の HEAD（set_startup_head で置いたもの）。置いていない・分からなければ None。"""
    app = QApplication.instance()
    if app is None:
        return None
    value = app.property(_START_HEAD_PROPERTY)
    return value if isinstance(value, str) and value else None


def version_label(head: str | None, committed: datetime | None = None) -> str:
    """版の表し方。コミットの日付（committed。記録された時差で 2026.10.27 の形）と先頭 7 桁。
    日付が分からなければ先頭 7 桁だけ、HEAD が分からなければ「不明」。更新の知らせでも使う。"""
    if not head:
        return tr("about.version_unknown")
    if committed is None:
        return head[:7]
    return f"{committed.year}.{committed.month:02d}.{committed.day:02d} ({head[:7]})"


def version_line(head: str | None, committed: datetime | None = None) -> str:
    """「バージョン: …」の行。"""
    return tr("about.version", version=version_label(head, committed))


def version_text(head: str | None, committed: datetime | None = None) -> str:
    """バージョン情報の本文（[コピー] で入れる文）。1 行目は製品名、2 行目は version_line。"""
    return f"{PRODUCT}\n{version_line(head, committed)}"


def _open_url(url: str) -> None:
    """ブラウザで開く（テストが差し替える）。"""
    if not QDesktopServices.openUrl(QUrl(url)):
        _log.warning("could not open %s in the browser", url)


def _set_clipboard(text: str) -> None:
    """クリップボードに入れる（テストが差し替える）。"""
    QApplication.clipboard().setText(text)


class AboutDialog(QDialog):
    def __init__(self, head: str | None, committed: datetime | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("about.title"))
        self._head = head
        self._committed = committed
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(PRODUCT))

        row = QHBoxLayout()
        self._version = QLabel(version_line(head, committed))
        self._version.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(self._version)
        self._copy_btn = QPushButton(tr("about.copy"))
        self._copy_btn.setAutoDefault(False)     # Enter は [閉じる] に渡す（フォーカスが [コピー] にあっても）
        # 文字が「コピーしました」に変わっても行を動かさないよう、初めから広い方の幅にする
        widths = []
        for text in (tr("about.copied"), tr("about.copy")):
            self._copy_btn.setText(text)
            widths.append(self._copy_btn.sizeHint().width())
        self._copy_btn.setMinimumWidth(max(widths))
        self._copy_btn.clicked.connect(self._copy)
        row.addWidget(self._copy_btn)
        row.addStretch(1)
        layout.addLayout(row)

        # 値は定数なので escape しない
        link = f'<a href="{AUTHOR_URL}"><span style="color:{_LINK_COLOR}">{AUTHOR}</span></a>'
        self._author = QLabel(tr("about.author", name=link))
        self._author.setTextFormat(Qt.TextFormat.RichText)
        self._author.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
        self._author.setToolTip(AUTHOR_URL)
        # setOpenExternalLinks(True) にすると C++ の中で開き、テストで差し替えられない
        self._author.linkActivated.connect(self._open_link)
        layout.addWidget(self._author)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._close_btn = buttons.button(QDialogButtonBox.StandardButton.Close)
        self._close_btn.setText(tr("about.close"))
        # ダイアログに入れてから（setDefault は親の QDialog を探して登録する。入れる前だと登録されず、
        # フォーカスが [閉じる] から離れたときに既定が外れ、[コピー] の上の Return で閉じなくなる）
        self._close_btn.setDefault(True)
        # 既定のボタンがあれば QDialog は表示のときにそこへフォーカスを移すが、その特例に頼らず置く
        # （既定もフォーカスも無いと、先に作った [コピー] に付き、Space でコピーになる）
        self._close_btn.setFocus()

    def copy_text(self) -> str:
        """[コピー] で入れる文（version_text。作者の行は入れない）。"""
        return version_text(self._head, self._committed)

    def _copy(self) -> None:
        _set_clipboard(self.copy_text())
        self._copy_btn.setText(tr("about.copied"))

    def _open_link(self, url: str) -> None:
        _open_url(url)
