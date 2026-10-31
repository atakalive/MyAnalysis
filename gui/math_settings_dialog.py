"""設定 → 数式の描画…（Issue #123）。

数式描画モジュール（MathJax）の導入・更新・削除、有効・無効、見た目（フォント・大きさ・太さ）を扱う
モーダルの小さなダイアログ。導入の前に出所（開発元・ソース・配布元・tarball の URL・固定した sha512・
ライセンス・保存先・大きさ）を全部見せる。

導入は ``gui/updater.py`` の ``SelfUpdater._spawn`` と同じく daemon の ``threading.Thread`` で行う
（QThread は使わない。urllib の読み込みは 1 回ごとのタイムアウトなので所要時間に上限が無く、走っている
QThread を残したまま QApplication が消えると Qt が abort する）。進み具合と結果は Signal で戻し、emit の
RuntimeError（先にダイアログが消えた）は握る。テストは ``_spawn`` を同期の呼び出しに差し替える。
"""
from __future__ import annotations

import html
import threading
from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QVBoxLayout,
)

from common import mathjax_install
from common.i18n import tr
from llm_bridge.paths import update_ui_pref

THREAD_NAME = "myanalysis-mathjax-install"

# 設定のプレビューに描く式（tests/test_mathmd.py の SAMPLE_MD の最初の $$…$$ と同じ。一致はテストが確かめる）
PREVIEW_TEX = (
    "\\hat{\\boldsymbol\\beta} = \\begin{pmatrix} \\hat\\beta_0 \\\\ \\hat\\beta_1 \\end{pmatrix}, \\quad\n"
    "I_{\\rm box} = \\tfrac{1}{2}\\lVert x - \\hat x \\rVert^2"
)

_BG = "#1e1e1e"
_FG = "#dcdcdc"


def _mb(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB"


def _link(url: str) -> str:
    u = html.escape(url, quote=True)
    return f'<a href="{u}">{u}</a>'


class MathSettingsDialog(QDialog):
    _progress_sig = Signal(int, int)
    _finished_sig = Signal(str, object)          # (処理: install / uninstall, InstallResult)

    def __init__(self, parent=None, *, engine=None,
                 on_changed: Callable[[], None] | None = None):
        super().__init__(parent)
        from gui import math_engine
        self._engine = engine if engine is not None else math_engine.engine()
        self._on_changed = on_changed
        self._busy = False
        self._cancel = threading.Event()
        self._last_message = ""
        self.setWindowTitle(tr("math.dialog.title"))
        self.setModal(True)
        self._progress_sig.connect(self._on_progress)
        self._finished_sig.connect(self._on_finished)

        lay = QVBoxLayout(self)
        self._status = QLabel()
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._status.setWordWrap(True)
        lay.addWidget(self._status)

        self._enable = QCheckBox(tr("math.enable"))
        self._enable.toggled.connect(self._on_enable_toggled)
        lay.addWidget(self._enable)

        form = QFormLayout()
        self._font = QComboBox()
        font_labels = {
            "mathjax-fira": tr("math.font.fira"),
            "mathjax-dejavu": tr("math.font.dejavu"),
            "mathjax-stix2": tr("math.font.stix2"),
            "mathjax-newcm": tr("math.font.newcm"),
        }
        for f in mathjax_install.FONTS:
            self._font.addItem(font_labels[f], f)
        self._font.currentIndexChanged.connect(self._on_font_changed)
        form.addRow(tr("math.font.label"), self._font)
        self._scale = QComboBox()
        for s in mathjax_install.SCALES:
            self._scale.addItem(f"{round(s * 100)}%", s)
        self._scale.currentIndexChanged.connect(self._on_scale_changed)
        form.addRow(tr("math.scale.label"), self._scale)
        self._bold = QCheckBox(tr("math.bold"))
        self._bold.toggled.connect(self._on_bold_toggled)
        form.addRow("", self._bold)
        lay.addLayout(form)

        self._preview = QLabel()
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setMinimumHeight(90)
        self._preview.setStyleSheet(f"background:{_BG};color:{_FG};padding:8px")
        lay.addWidget(QLabel(tr("math.preview.label")))
        lay.addWidget(self._preview)

        self._font_note = QLabel()
        self._font_note.setWordWrap(True)
        lay.addWidget(self._font_note)

        self._origin = QLabel()
        self._origin.setTextFormat(Qt.TextFormat.RichText)
        self._origin.setWordWrap(True)
        self._origin.setOpenExternalLinks(True)
        self._origin.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        lay.addWidget(self._origin)

        self._bar = QProgressBar()
        self._bar.setVisible(False)
        lay.addWidget(self._bar)
        self._message = QLabel()
        self._message.setWordWrap(True)
        self._message.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self._message)

        row = QHBoxLayout()
        self._install_btn = QPushButton()
        self._install_btn.clicked.connect(self._on_install)
        self._uninstall_btn = QPushButton(tr("math.btn.uninstall"))
        self._uninstall_btn.clicked.connect(self._on_uninstall)
        self._cancel_btn = QPushButton(tr("math.btn.cancel"))
        self._cancel_btn.clicked.connect(self._cancel.set)
        self._cancel_btn.setVisible(False)
        self._close_btn = QPushButton(tr("math.btn.close"))
        self._close_btn.clicked.connect(self.reject)
        row.addWidget(self._install_btn)
        row.addWidget(self._uninstall_btn)
        row.addWidget(self._cancel_btn)
        row.addStretch(1)
        row.addWidget(self._close_btn)
        lay.addLayout(row)

        self._load_prefs_into_widgets()
        self._engine.webengine_ok()
        self._engine.rendered.connect(self._update_preview)
        self._engine.stateChanged.connect(self._refresh)
        self._refresh()

    # ---------------------------------------------------------------- 値

    def _load_prefs_into_widgets(self) -> None:
        p = self._engine.prefs
        for w, val in ((self._font, p.font), (self._scale, p.scale)):
            w.blockSignals(True)
            for i in range(w.count()):
                d = w.itemData(i)
                if d == val or (isinstance(d, float) and abs(d - val) < 1e-9):
                    w.setCurrentIndex(i)
            w.blockSignals(False)
        self._bold.blockSignals(True)
        self._bold.setChecked(p.bold)
        self._bold.blockSignals(False)
        self._enable.blockSignals(True)
        self._enable.setChecked(p.enabled)
        self._enable.blockSignals(False)

    def selected_font(self) -> str:
        return self._font.currentData()

    def _apply_pref(self, key: str, value) -> None:
        update_ui_pref(key, value)
        self._engine.reload_prefs()
        self._notify()

    def _notify(self) -> None:
        if self._on_changed is not None:
            try:
                self._on_changed()
            except Exception:  # noqa: BLE001 — チャットの描き直しの失敗で設定を止めない
                pass

    # ---------------------------------------------------------------- 状態の表示

    def status_text(self) -> str:
        return self._status.text()

    def _install_state(self) -> mathjax_install.Status:
        return mathjax_install.status()

    def _refresh(self) -> None:
        st = self._install_state()
        eng = self._engine
        font = self.selected_font()
        if eng.state == "unavailable":
            text = (tr("math.status.restart_required") if eng.unavailable_reason == "restart"
                    else tr("math.status.no_webengine"))
        elif st.state == "missing":
            text = tr("math.status.missing")
        elif st.state == "outdated":
            text = tr("math.status.outdated", version=st.version or "")
        elif st.state == "broken":
            text = tr("math.status.broken")
        elif eng.state == "failed" and eng.last_error:
            text = tr("math.status.last_failure", error=eng.last_error)
        else:
            text = tr("math.status.installed", version=mathjax_install.VERSION)
        self._status.setText(text)
        installed = st.state == "installed"
        font_ok = installed and font in st.fonts
        self._enable.setEnabled(installed and not self._busy)
        if st.state == "missing":
            self._install_btn.setText(tr("math.btn.install"))
            self._install_btn.setVisible(True)
        elif st.state == "outdated":
            self._install_btn.setText(tr("math.btn.update"))
            self._install_btn.setVisible(True)
        elif st.state == "broken":
            self._install_btn.setText(tr("math.btn.reinstall"))
            self._install_btn.setVisible(True)
        elif not font_ok:
            self._install_btn.setText(tr("math.btn.install_font"))
            self._install_btn.setVisible(True)
        else:
            self._install_btn.setVisible(False)
        self._install_btn.setEnabled(not self._busy)
        self._uninstall_btn.setVisible(installed or st.state in ("outdated", "broken"))
        self._uninstall_btn.setEnabled(not self._busy)
        for w in (self._font, self._scale, self._bold):
            w.setEnabled(not self._busy)
        self._close_btn.setEnabled(not self._busy)
        self._cancel_btn.setVisible(self._busy)
        self._bar.setVisible(self._busy)
        self._font_note.setText("" if font_ok or not installed else tr("math.font.not_installed"))
        self._origin.setText(self._origin_html(font))
        self._update_preview()

    def _origin_html(self, font: str) -> str:
        mj = mathjax_install.mathjax_package()
        fp = mathjax_install.font_package(font)
        rows = [
            tr("math.origin.what", version=html.escape(mj.version), font=html.escape(fp.name)),
            tr("math.origin.developer", developer=html.escape(mathjax_install.MATHJAX_DEVELOPER),
               sponsors=html.escape(", ".join(mathjax_install.MATHJAX_SPONSORS)),
               homepage=_link(mathjax_install.MATHJAX_HOMEPAGE)),
            tr("math.origin.source", url=_link(mathjax_install.MATHJAX_SOURCE)),
            tr("math.origin.distribution", pages=_link(mj.npm_page) + "<br>" + _link(fp.npm_page)),
            tr("math.origin.tarballs", urls=_link(mj.tarball) + "<br>" + _link(fp.tarball)),
            tr("math.origin.integrity",
               hashes=f"<br><code>{html.escape(mj.name)}: {html.escape(mj.integrity)}</code>"
                      f"<br><code>{html.escape(fp.name)}: {html.escape(fp.integrity)}</code>"),
            tr("math.origin.license", license=html.escape(mathjax_install.MATHJAX_LICENSE),
               url=_link(mathjax_install.LICENSE_URL),
               font_license=html.escape(mathjax_install.FONT_LICENSES[font]),
               origin=html.escape(mathjax_install.FONT_ORIGINS[font]),
               font_source=_link(mathjax_install.FONTS_SOURCE)),
            tr("math.origin.location", path=html.escape(str(mathjax_install.install_root()))),
            tr("math.origin.size", download=_mb(mj.download_size + fp.download_size),
               installed=_mb(mj.extracted_size + fp.extracted_size)),
            tr("math.origin.privacy"),
        ]
        return "<b>" + html.escape(tr("math.origin.heading")) + "</b><br>" + "<br>".join(rows)

    def _update_preview(self) -> None:
        st = self._install_state()
        font = self.selected_font()
        if st.state != "installed" or font not in st.fonts or self._busy:
            self._preview.setPixmap(QPixmap())
            self._preview.setText(tr("math.preview.unavailable"))
            return
        res = self._engine.request(PREVIEW_TEX, True, force=True)
        if res.status == "pending":
            self._preview.setPixmap(QPixmap())
            self._preview.setText(tr("math.preview.rendering"))
            return
        if res.status != "ready" or not res.svg:
            self._preview.setPixmap(QPixmap())
            self._preview.setText(tr("math.preview.error", error=res.error or ""))
            return
        from PySide6.QtGui import QFontInfo, QFontMetrics
        from gui.math_render import _dpr, rasterize
        f = QFont(self.font())
        px = QFontInfo(f).pixelSize() or 13
        prefs = self._engine.prefs
        r = rasterize(res.svg, em_px=px * prefs.scale, color=QColor(_FG).name(), bold=prefs.bold,
                      dpr=_dpr(), x_height=float(QFontMetrics(f).xHeight()))
        if r is None:
            self._preview.setText(tr("math.preview.error", error="svg"))
            return
        self._preview.setText("")
        self._preview.setPixmap(QPixmap.fromImage(r.image))

    # ---------------------------------------------------------------- 変更

    def _on_enable_toggled(self, on: bool) -> None:
        self._apply_pref("math_render", bool(on))
        self._refresh()

    def _on_font_changed(self, _i: int) -> None:
        font = self.selected_font()
        st = self._install_state()
        if st.state == "installed" and font not in st.fonts:
            # 入っていないフォントは勝手には取得しない（出所と大きさを見せて [このフォントを導入] を押させる）
            self._refresh()
            return
        self._apply_pref("math_font", font)
        self._refresh()

    def _on_scale_changed(self, _i: int) -> None:
        self._apply_pref("math_scale", float(self._scale.currentData()))
        self._update_preview()

    def _on_bold_toggled(self, on: bool) -> None:
        self._apply_pref("math_bold", bool(on))
        self._update_preview()

    # ---------------------------------------------------------------- 導入・削除

    def is_busy(self) -> bool:
        return self._busy

    def _spawn(self, fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, name=THREAD_NAME, daemon=True).start()

    def _set_busy(self, on: bool) -> None:
        self._busy = on
        self._refresh()

    def _on_install(self) -> None:
        if self._busy:
            return
        font = self.selected_font()
        self._engine.stop()                      # 更新の前に自分のエンジンを止める
        self._cancel.clear()
        self._message.setText("")
        self._bar.setRange(0, 0)
        self._set_busy(True)
        cancel = self._cancel

        def work():
            res = mathjax_install.install(font, self._emit_progress, cancel.is_set)
            self._emit_finished("install", (font, res))
        self._spawn(work)

    def _on_uninstall(self) -> None:
        if self._busy:
            return
        self._engine.stop()
        self._message.setText("")
        res = mathjax_install.uninstall()
        self._on_finished("uninstall", res)

    def _emit_progress(self, done: int, total: int) -> None:
        try:
            self._progress_sig.emit(done, total)
        except RuntimeError:       # 先にダイアログが消えた
            pass

    def _emit_finished(self, what: str, payload) -> None:
        try:
            self._finished_sig.emit(what, payload)
        except RuntimeError:
            pass

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self._bar.setRange(0, 1000)
            self._bar.setValue(min(1000, int(done * 1000 / total)))
        self._message.setText(tr("math.progress", done=_mb(done), total=_mb(total)))

    def _on_finished(self, what: str, payload) -> None:
        if what == "install":
            font, res = payload
            self._busy = False
            if res.ok:
                update_ui_pref("math_font", font)
                update_ui_pref("math_render", True)    # 導入に成功したら自動で有効にする
                self._engine.reload_prefs()
                self._engine.reset()
                self._load_prefs_into_widgets()
                self._message.setText(tr("math.done.installed"))
                self._notify()
            else:
                self._message.setText(self._error_text(res))
        else:
            res = payload
            if res.ok:
                update_ui_pref("math_render", False)
                self._engine.reload_prefs()
                self._engine.clear_cache()
                self._load_prefs_into_widgets()
                self._message.setText(tr("math.done.uninstalled"))
                self._notify()
            else:
                self._message.setText(self._error_text(res))
        self._refresh()

    def message_text(self) -> str:
        return self._message.text()

    def _error_text(self, res) -> str:
        msg = res.message or ""
        texts = {
            "network": tr("math.error.network", message=msg),
            "integrity": tr("math.error.integrity", message=msg),
            "archive": tr("math.error.archive", message=msg),
            "cancelled": tr("math.error.cancelled"),
            "io": tr("math.error.io", message=msg),
            "in_use": tr("math.error.in_use"),
            "superseded": tr("math.error.superseded"),
        }
        return texts.get(res.error or "io", texts["io"])

    # ---------------------------------------------------------------- 閉じる

    def reject(self) -> None:
        if self._busy:
            return
        self._disconnect_engine()
        super().reject()

    def done(self, r: int) -> None:
        if self._busy:
            return
        self._disconnect_engine()
        super().done(r)

    def closeEvent(self, ev) -> None:  # noqa: N802 — Qt の名前
        if self._busy:
            ev.ignore()
            return
        super().closeEvent(ev)

    def _disconnect_engine(self) -> None:
        if getattr(self, "_disconnected", False):
            return
        self._disconnected = True
        eng = self._engine
        for sig, slot in ((eng.rendered, self._update_preview), (eng.stateChanged, self._refresh)):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        if not eng.prefs.enabled:
            eng.stop()             # プレビューのために起こしたエンジンを残さない
