"""設定 → チャットの関連検索（実験的）…（Issue #126）。

過去のチャットの関連検索（埋め込み索引。``llm_bridge/chat_index.py``・``common/embed_model.py``）の
有効・無効、必要なパッケージ（onnxruntime・tokenizers）の導入、埋め込みモデルの取得・削除、索引の
更新・再構築・削除を扱うモーダルの小さなダイアログ。骨格は ``gui/math_settings_dialog.py`` と同じ:
導入の前に出所（開発元・元のモデル・配布元・ファイルの URL・固定した sha256・ライセンス・pip の
コマンド・保存先・大きさ・通信の有無）を全部見せる。モデルを切り替えても勝手には取得しない。

時間のかかる処理（pip・取得・削除・索引の操作・索引の状況の読み取り）は daemon の ``threading.Thread``
で行う（QThread は使わない。数式のダイアログと同じ理由）。進み具合と結果は Signal で戻し、emit の
RuntimeError（先にダイアログが消えた）は握る。テストは ``_spawn`` を同期の呼び出しに差し替える。

背景の差分更新（``gui/chat_indexer.py``）とは取り合わない: 開いている間は ``ToolWindow`` が背景の更新を
hold し、ダイアログの操作はワーカーの中で ``indexer.wait_idle`` を待ってから始める。索引の操作はロックを
待たない（``blocking=False``）。ロック待ちは中止できず、busy の間はダイアログを閉じられないため。ほかの
プロセス（CLI・ほかの MyAnalysis）が使っていれば、待たずに「使用中」と言う。
"""
from __future__ import annotations

import html
import importlib
import shlex
import subprocess
import sys
import threading
from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QVBoxLayout,
)

from common import embed_model
from common.i18n import tr
from llm_bridge import chat_index, chat_search
from llm_bridge import paths as lb_paths

THREAD_NAME = "myanalysis-embeddings-install"
WAIT_BACKGROUND_S = 60.0       # 背景の更新が止まるのを待つ上限（次のバッチの境目まで）


def _mb(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB"


def _link(url: str) -> str:
    u = html.escape(url, quote=True)
    return f'<a href="{u}">{u}</a>'


def _ts(v) -> str:
    return chat_search.format_ts(v) if v is not None else "—"


def _command_line(argv: list[str]) -> str:
    return subprocess.list2cmdline(argv) if sys.platform == "win32" else shlex.join(argv)


class EmbeddingSettingsDialog(QDialog):
    _progress_sig = Signal(int, int)               # モデルの取得のバイト数
    _index_progress_sig = Signal(str, int, int)    # 索引: (データセット, 埋め込んだ数, 埋め込む数)
    _line_sig = Signal(str)                        # pip の出力の 1 行
    _finished_sig = Signal(str, object)            # (deps / install / uninstall / index / clear / status, 結果)

    def __init__(self, parent=None, *, indexer=None,
                 on_changed: Callable[[], None] | None = None):
        super().__init__(parent)
        self._indexer = indexer
        self._on_changed = on_changed
        self._busy = False
        self._cancel = threading.Event()
        self._index_status = None
        self._index_error: str | None = None
        self._status_gen = 0
        self._status_inflight = False
        self._status_again = False
        self._closed = False
        self.setWindowTitle(tr("chat.recall.dialog.title"))
        self.setModal(True)
        self._progress_sig.connect(self._on_progress)
        self._index_progress_sig.connect(self._on_index_progress)
        self._line_sig.connect(self._on_line)
        self._finished_sig.connect(self._on_finished)

        lay = QVBoxLayout(self)
        self._status = QLabel()
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._status.setWordWrap(True)
        lay.addWidget(self._status)

        self._enable = QCheckBox(tr("chat.recall.enable"))
        self._enable.toggled.connect(self._on_enable_toggled)
        lay.addWidget(self._enable)

        form = QFormLayout()
        self._model = QComboBox()
        for mid in embed_model.CATALOG:
            self._model.addItem(self._model_label(mid), mid)
        self._model.currentIndexChanged.connect(self._on_model_changed)
        form.addRow(tr("chat.recall.model.label"), self._model)
        self._in_ai = QCheckBox(tr("chat.recall.in_ai_search"))
        self._in_ai.toggled.connect(self._on_in_ai_toggled)
        form.addRow("", self._in_ai)
        lay.addLayout(form)

        self._index_line = QLabel()
        self._index_line.setTextFormat(Qt.TextFormat.PlainText)
        self._index_line.setWordWrap(True)
        lay.addWidget(self._index_line)

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
        self._uninstall_btn = QPushButton(tr("chat.recall.btn.uninstall"))
        self._uninstall_btn.clicked.connect(self._on_uninstall)
        self._update_btn = QPushButton(tr("chat.recall.btn.update_index"))
        self._update_btn.clicked.connect(lambda: self._on_index("update"))
        self._rebuild_btn = QPushButton(tr("chat.recall.btn.rebuild_index"))
        self._rebuild_btn.clicked.connect(lambda: self._on_index("rebuild"))
        self._clear_btn = QPushButton(tr("chat.recall.btn.clear_index"))
        self._clear_btn.clicked.connect(self._on_clear)
        self._cancel_btn = QPushButton(tr("chat.recall.btn.cancel"))
        self._cancel_btn.clicked.connect(self._cancel.set)
        self._cancel_btn.setVisible(False)
        self._close_btn = QPushButton(tr("chat.recall.btn.close"))
        self._close_btn.clicked.connect(self.reject)
        for b in (self._install_btn, self._uninstall_btn, self._update_btn, self._rebuild_btn,
                  self._clear_btn, self._cancel_btn):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self._close_btn)
        lay.addLayout(row)

        self._load_prefs_into_widgets()
        self._refresh()
        self._reload_index_status()

    # ---------------------------------------------------------------- 値

    @staticmethod
    def _model_label(mid: str) -> str:
        m = embed_model.CATALOG.get(mid)
        return tr(m.label_key) if m is not None else mid

    def _load_prefs_into_widgets(self) -> None:
        p = chat_index.load_prefs()
        self._model.blockSignals(True)
        i = self._model.findData(p.model_id)
        if i >= 0:
            self._model.setCurrentIndex(i)
        self._model.blockSignals(False)
        for w, val in ((self._enable, p.enabled), (self._in_ai, p.in_ai_search)):
            w.blockSignals(True)
            w.setChecked(val)
            w.blockSignals(False)

    def selected_model(self) -> str:
        return self._model.currentData()

    def _readiness(self, model_id: str | None = None) -> embed_model.Readiness:
        return embed_model.readiness(model_id or self.selected_model())

    def _notify(self) -> None:
        if self._on_changed is not None:
            try:
                self._on_changed()
            except Exception:  # noqa: BLE001 — 背景の更新の要求の失敗で設定を止めない
                pass

    # ---------------------------------------------------------------- 状態の表示

    def status_text(self) -> str:
        return self._status.text()

    def index_text(self) -> str:
        return self._index_line.text()

    def message_text(self) -> str:
        return self._message.text()

    def _refresh(self) -> None:
        sel = self.selected_model()
        r = self._readiness(sel)
        p = chat_index.load_prefs()
        pr = r if p.model_id == sel else self._readiness(p.model_id)
        st = self._index_status
        b = self._busy
        label = self._model_label(sel)
        if r.deps_missing:
            text = tr("chat.recall.status.deps_missing", packages=", ".join(r.deps_missing))
            btn = tr("chat.recall.btn.install_deps")
        elif r.model_state == "missing":
            text = tr("chat.recall.status.model_missing", model=label)
            btn = tr("chat.recall.btn.install_model")
        elif r.model_state == "broken":
            text = tr("chat.recall.status.model_broken", model=label)
            btn = tr("chat.recall.btn.reinstall_model")
        else:
            btn = None
            if st is None or st.model != sel or not st.total_chunks:
                text = tr("chat.recall.status.index_empty", model=label, dims=r.model.dims)
            else:
                text = tr("chat.recall.status.ready", model=label, dims=r.model.dims,
                          chunks=st.total_chunks, updated=_ts(st.last_update))
        self._status.setText(text)
        if btn is None:
            self._install_btn.setVisible(False)
        else:
            self._install_btn.setText(btn)
            self._install_btn.setVisible(True)
        self._install_btn.setEnabled(not b)
        self._uninstall_btn.setVisible(r.model_state in ("installed", "broken"))
        self._uninstall_btn.setEnabled(not b)
        # 有効化: オンにできるのは選択中（pref）のモデルの材料が揃っているときだけ。オフにはいつでもできる
        pref_ok = not pr.deps_missing and pr.model_state == "installed"
        self._enable.setEnabled(not b and (pref_ok or self._enable.isChecked()))
        ready = p.enabled and pref_ok and sel == p.model_id
        self._update_btn.setEnabled(not b and ready)
        self._rebuild_btn.setEnabled(not b and ready)
        self._clear_btn.setEnabled(not b and st is not None and st.total_bytes > 0)
        for w in (self._model, self._in_ai):
            w.setEnabled(not b)
        self._close_btn.setEnabled(not b)
        self._cancel_btn.setVisible(b)
        self._bar.setVisible(b)
        self._index_line.setText(self._index_text(p))
        self._origin.setText(self._origin_html(sel))

    def _index_text(self, p) -> str:
        if self._index_error is not None:
            return tr("chat.recall.index.unavailable", message=self._index_error)
        st = self._index_status
        if st is None:
            return tr("chat.recall.index.loading")
        return tr("chat.recall.index.line", model=self._model_label(st.model),
                  datasets=sum(1 for s in st.datasets if s.chunks), chunks=st.total_chunks,
                  size=_mb(st.total_bytes), updated=_ts(st.last_update),
                  stale=sum(st.stale.values()))

    def _origin_html(self, mid: str) -> str:
        m = embed_model.CATALOG[mid]
        size = _mb(sum(f.size for f in m.files))
        packages = ", ".join(
            f"{html.escape(pip)} ({_link(embed_model.PACKAGE_PAGES[pip])})"
            for _imp, pip in embed_model.REQUIRED_PACKAGES)
        command = html.escape(_command_line(embed_model.pip_install_argv()))
        rows = [
            tr("chat.recall.origin.what", model=html.escape(self._model_label(mid)),
               dims=m.dims, size=size),
            tr("chat.recall.origin.developer", developer=html.escape(m.developer),
               homepage=_link(m.homepage)),
            tr("chat.recall.origin.source", url=_link(m.source)),
            tr("chat.recall.origin.distribution", url=_link(m.distribution)),
            tr("chat.recall.origin.files", urls="<br>".join(_link(f.url) for f in m.files)),
            tr("chat.recall.origin.integrity",
               hashes="".join(f"<br><code>{html.escape(f.name)}: {html.escape(f.sha256)}</code>"
                              for f in m.files)),
            tr("chat.recall.origin.license", license=html.escape(m.license),
               url=_link(m.license_url)),
            tr("chat.recall.origin.packages", packages=packages,
               command=f"<code>{command}</code>"),
            tr("chat.recall.origin.location",
               models=html.escape(str(embed_model.install_root())),
               index=html.escape(str(lb_paths.chat_index_dir()))),
            tr("chat.recall.origin.size", download=size),
            tr("chat.recall.origin.privacy"),
        ]
        return "<b>" + html.escape(tr("chat.recall.origin.heading")) + "</b><br>" + "<br>".join(rows)

    def _reload_index_status(self) -> None:
        """索引の状況を裏で読む（同期ドライブの scandir を含むので GUI スレッドでは読まない）。

        走らせるのは 1 本だけ。走っている間の要求は、終わった後の 1 回にまとめる。"""
        if self._closed:
            return
        if self._status_inflight:
            self._status_again = True
            return
        self._status_inflight = True
        self._status_gen += 1
        gen = self._status_gen

        def work():
            try:
                st, err = chat_index.status(), None
            except Exception as e:  # noqa: BLE001 — 読めないことを見せる（確認中のままにしない）
                st, err = None, f"{type(e).__name__}: {e}"
            self._emit_finished("status", (gen, st, err))
        self._spawn(work)

    # ---------------------------------------------------------------- 変更

    def _on_enable_toggled(self, on: bool) -> None:
        lb_paths.update_ui_pref(chat_index.PREF_ENABLED, bool(on))
        if not on:
            chat_index.release_embedder()          # メモリを返す
        self._notify()
        self._refresh()

    def _on_model_changed(self, _i: int) -> None:
        sel = self.selected_model()
        if self._readiness(sel).model_state != "installed":
            # 入っていないモデルは勝手には取得しない（出所と大きさを見せて [モデルを取得] を押させる）
            self._refresh()
            return
        if sel != chat_index.load_prefs().model_id:
            lb_paths.update_ui_pref(chat_index.PREF_MODEL, sel)
            self._notify()
            self._reload_index_status()
        self._refresh()

    def _on_in_ai_toggled(self, on: bool) -> None:
        lb_paths.update_ui_pref(chat_index.PREF_IN_AI_SEARCH, bool(on))

    # ---------------------------------------------------------------- 導入・削除・索引

    def is_busy(self) -> bool:
        return self._busy

    def _spawn(self, fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, name=THREAD_NAME, daemon=True).start()

    def _set_busy(self, on: bool) -> None:
        self._busy = on
        self._refresh()

    def _background_running(self) -> bool:
        ix = self._indexer
        try:
            return ix is not None and bool(ix.is_running())
        except Exception:  # noqa: BLE001
            return False

    def _start(self, op: str, work: Callable[[], None], message: str = "") -> None:
        if self._background_running():
            message = tr("chat.recall.progress.waiting")
        self._cancel.clear()
        self._message.setText(message)
        self._bar.setRange(0, 0)
        self._set_busy(True)
        self._spawn(work)

    def _wait_background(self) -> bool:
        """（ワーカーで）背景の更新が止まるのを待つ。止まれば True、期限切れか中止なら False。"""
        ix = self._indexer
        if ix is None:
            return True
        try:
            return bool(ix.wait_idle(WAIT_BACKGROUND_S, self._cancel.is_set))
        except Exception:  # noqa: BLE001 — 待てないときは待たずに進む（ロックが守る）
            return True

    def _not_idle(self) -> embed_model.InstallResult:
        return embed_model.InstallResult(False, "cancelled" if self._cancel.is_set() else "locked")

    def _on_install(self) -> None:
        if self._busy:
            return
        r = self._readiness()
        cancel = self._cancel
        if r.deps_missing:
            def work():
                res = embed_model.install_packages(self._emit_line, cancel.is_set)
                self._emit_finished("deps", res)
            self._start("deps", work)
            return
        sel = self.selected_model()
        broken = r.model_state == "broken"

        def work():
            if broken:                       # 壊れたものを置き換える前に、開いていれば手放す
                if not self._wait_background():
                    self._emit_finished("install", (sel, self._not_idle()))
                    return
                chat_index.release_embedder()
            res = embed_model.install(sel, self._emit_progress, cancel.is_set)
            self._emit_finished("install", (sel, res))
        self._start("install", work)

    def _on_uninstall(self) -> None:
        if self._busy:
            return
        sel = self.selected_model()

        def work():
            if not self._wait_background():
                res = self._not_idle()
            else:
                chat_index.release_embedder()      # 先に手放す（開いたままだと Windows では in_use）
                res = embed_model.uninstall(sel)
            self._emit_finished("uninstall", (sel, res))
        self._start("uninstall", work)

    def _on_index(self, kind: str) -> None:
        if self._busy:
            return
        cancel = self._cancel

        def work():
            self._emit_finished("index", (kind, self._index_work(kind, cancel)))
        self._start("index", work, tr("chat.recall.progress.scanning"))

    def _index_work(self, kind: str, cancel: threading.Event) -> embed_model.InstallResult:
        if not self._wait_background():
            return self._not_idle()
        self._emit_index_progress("", 0, 0)
        try:
            idx = chat_index.ChatIndex.open()
            fn = idx.rebuild if kind == "rebuild" else idx.update
            # reload=False: ワーカーから config.DATASETS（GUI と共有の dict）を書き換えない
            rep = fn(progress=self._emit_index_progress, cancel=cancel.is_set, blocking=False,
                     reload=False)
        except chat_index.NotReady as e:
            return embed_model.InstallResult(False, "not_ready", str(e))
        except chat_index.IndexLocked:
            return embed_model.InstallResult(False, "locked")
        except Exception as e:  # noqa: BLE001 — 文で見せる
            return embed_model.InstallResult(False, "io", f"{type(e).__name__}: {e}")
        if not rep.ran:
            return embed_model.InstallResult(False, "locked")
        if rep.cancelled:
            return embed_model.InstallResult(False, "cancelled")
        return embed_model.InstallResult(True)

    def _on_clear(self) -> None:
        if self._busy:
            return

        def work():
            if not self._wait_background():
                res = self._not_idle()
            else:
                try:
                    chat_index.clear(blocking=False)
                    res = embed_model.InstallResult(True)
                except chat_index.IndexLocked:
                    res = embed_model.InstallResult(False, "locked")
                except Exception as e:  # noqa: BLE001
                    res = embed_model.InstallResult(False, "io", f"{type(e).__name__}: {e}")
            self._emit_finished("clear", res)
        self._start("clear", work)

    def _emit_progress(self, done: int, total: int) -> None:
        try:
            self._progress_sig.emit(done, total)
        except RuntimeError:       # 先にダイアログが消えた
            pass

    def _emit_index_progress(self, dataset: str, done: int, total: int) -> None:
        try:
            self._index_progress_sig.emit(str(dataset), int(done), int(total))
        except RuntimeError:
            pass

    def _emit_line(self, line: str) -> None:
        try:
            self._line_sig.emit(line)
        except RuntimeError:
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
        self._message.setText(tr("chat.recall.progress.bytes", done=_mb(done), total=_mb(total)))

    def _on_index_progress(self, dataset: str, done: int, total: int) -> None:
        if total <= 0:
            self._bar.setRange(0, 0)
            self._message.setText(tr("chat.recall.progress.scanning"))
            return
        self._bar.setRange(0, 1000)
        self._bar.setValue(min(1000, int(done * 1000 / total)))
        self._message.setText(tr("chat.recall.progress.chunks", dataset=dataset, done=done,
                                 total=total))

    def _on_line(self, line: str) -> None:
        self._message.setText(tr("chat.recall.progress.pip", line=line))

    def _on_finished(self, what: str, payload) -> None:
        if what == "status":
            gen, st, err = payload
            self._status_inflight = False
            if self._closed or gen != self._status_gen:
                return
            self._index_status, self._index_error = st, err
            self._refresh()                      # busy・進み具合・メッセージには触れない
            if self._status_again:
                self._status_again = False
                self._reload_index_status()
            return
        self._busy = False
        if what == "deps":
            res = payload
            importlib.invalidate_caches()
            if res.ok:
                # 入れたのに import できない（このプロセスの sys.path に無い場所に入った）なら再起動
                self._message.setText(tr("chat.recall.done.deps_restart")
                                      if embed_model.missing_packages()
                                      else tr("chat.recall.done.deps"))
            else:
                self._message.setText(self._error_text(res))
        elif what == "install":
            sel, res = payload
            if res.ok:
                lb_paths.update_ui_pref(chat_index.PREF_MODEL, sel)
                lb_paths.update_ui_pref(chat_index.PREF_ENABLED, True)   # 取得に成功したら自動で有効にする
                self._load_prefs_into_widgets()
                self._message.setText(tr("chat.recall.done.installed"))
                self._notify()
            else:
                self._message.setText(self._error_text(res))
        elif what == "uninstall":
            sel, res = payload
            if res.ok:
                if sel == chat_index.load_prefs().model_id:   # 使っているモデルを消したときだけ無効にする
                    lb_paths.update_ui_pref(chat_index.PREF_ENABLED, False)
                    self._load_prefs_into_widgets()
                self._message.setText(tr("chat.recall.done.uninstalled"))
                self._notify()
            else:
                self._message.setText(self._error_text(res))
        elif what == "index":
            kind, res = payload
            if res.ok:
                self._message.setText(tr("chat.recall.done.index_rebuilt") if kind == "rebuild"
                                      else tr("chat.recall.done.index_updated"))
            else:
                self._message.setText(self._error_text(res))
        elif what == "clear":
            res = payload
            self._message.setText(tr("chat.recall.done.index_cleared") if res.ok
                                  else self._error_text(res))
        self._refresh()
        self._reload_index_status()

    def _error_text(self, res) -> str:
        msg = res.message or ""
        texts = {
            "network": tr("chat.recall.error.network", message=msg),
            "integrity": tr("chat.recall.error.integrity", message=msg),
            "cancelled": tr("chat.recall.error.cancelled"),
            "io": tr("chat.recall.error.io", message=msg),
            "in_use": tr("chat.recall.error.in_use"),
            "pip": tr("chat.recall.error.pip", message=msg),
            "locked": tr("chat.recall.error.locked"),
            "not_ready": tr("chat.recall.error.not_ready"),
        }
        return texts.get(res.error or "io", texts["io"])

    # ---------------------------------------------------------------- 閉じる

    def _mark_closed(self) -> None:
        self._closed = True
        self._status_gen += 1          # 遅れて届いた状況の読み取りを捨てる

    def reject(self) -> None:
        if self._busy:
            return
        self._mark_closed()
        super().reject()

    def done(self, r: int) -> None:
        if self._busy:
            return
        self._mark_closed()
        super().done(r)

    def closeEvent(self, ev) -> None:  # noqa: N802 — Qt の名前
        if self._busy:
            ev.ignore()
            return
        super().closeEvent(ev)
