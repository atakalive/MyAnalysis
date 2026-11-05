"""Chat search dialog (Ctrl+F) and result helpers — Issue #108.

検索ウィンドウ（語・対象・アーカイブ・検索方法・検索履歴）と、一致検索・埋め込み検索の結果
Markdown、埋め込み検索のジョブ、検索履歴の記録。検索そのものは Qt 非依存の
llm_bridge.chat_search と llm_bridge.chat_index（埋め込み検索。Issue #128）。

このモジュールは gui.chat を import しない（gui.chat が関数内でこちらを import する）。
テストが差し替えるので ai_search_engine / current_engine_id / current_model は
モジュール属性として持つ。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSignalBlocker, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from common.i18n import tr
from llm_backend.engines import current_engine_id, current_model, engine_by_id, engine_label
from llm_bridge import chat_search as core
from llm_bridge import chat_search_history as history
from llm_bridge.chat_search import EMBEDDING_MODES, SearchRequest

_MAX_MD_HITS = 100
_RELATED_SNIPPET = 160   # 埋め込み検索の 1 行の抜粋の長さ（一致検索の抜粋と同じくらい）
RECALL_BUDGET_S = 20.0   # Ctrl+F の埋め込み検索が先に行う索引の差分更新の上限（秒）
_HISTORY_COLS = 6
_COL_DATASET = 5
_RELATED_K = 30          # AI 検索の初回に同梱する関連箇所の候補数（Issue #127）

_log = logging.getLogger(__name__)


# ----- engine for AI search -----


def ai_search_engine():
    """(engine, model, provider) of [chat_search]; (None, "", "") = follow the global."""
    try:
        from llm_backend.engines import chat_search_selection
        return chat_search_selection()
    except Exception:
        return (None, "", "")


def ai_engine_label_text() -> str:
    try:
        eng, model, _ = ai_search_engine()
        if eng is not None:
            return tr("chat.search.dialog.ai_engine", engine=engine_label(eng),
                      model=model.strip() or current_model(eng) or "-",
                      mark=tr("chat.engine.mark_override"))
        eid = current_engine_id()
        if eid is None:
            return tr("chat.search.dialog.ai_engine", engine=tr("chat.engine.none"),
                      model="-", mark=tr("chat.engine.mark_default"))
        e = engine_by_id(eid)
        return tr("chat.search.dialog.ai_engine",
                  engine=engine_label(e) if e is not None else "-",
                  model=(current_model(e) or "-") if e is not None else "-",
                  mark=tr("chat.engine.mark_default"))
    except Exception:
        return tr("chat.search.dialog.ai_engine", engine="-", model="-",
                  mark=tr("chat.engine.mark_default"))


def related_hits_for_ai(query, datasets, include_archived) -> list | None:
    """AI 検索の初回に同梱する関連箇所（Issue #127。埋め込み検索の上位 _RELATED_K 件）。never raise。

    索引は更新せず、status() も呼ばない（同期ドライブの全 DS の走査を GUI スレッドでしない）。
    使えない・「AI 検索に関連箇所を渡す」がオフ・範囲が空なら []、検索の失敗は None
    （呼び出し側が知らせる）。初回だけ GUI スレッドでモデルの読み込み（1 秒ほど）が起きる。"""
    try:
        from llm_bridge import chat_index
        if not datasets or not chat_index.is_ready() or not chat_index.load_prefs().in_ai_search:
            return []
        hits, _warnings = chat_index.ChatIndex.open().search(
            query, datasets=list(datasets), include_archived=include_archived, k=_RELATED_K)
        return list(hits)
    except Exception:  # noqa: BLE001 — never raise（AI 検索は関連箇所なしで始める）
        _log.debug("related hits for AI search failed", exc_info=True)
        return None


# ----- result Markdown -----


def search_datasets(req, ctx) -> list[str]:
    """実際に検索する範囲のデータセット。結果の見出し・AI 検索の表示行・履歴の datasets で共用する。

    scope="dataset" は [req.dataset]。"all" は ctx.open_datasets（切替後に取った ctx を渡す）。
    ctx.open_datasets が None（window 不明）のときは core が DS を絞らずに検索するので、
    検索範囲に入るチャットのデータセット（名前順。同期ファイル由来の str 以外の値は数えない）。
    "unbound" は []。未所属のチャットはどの範囲でも検索に入るが、ここには数えない。"""
    if req.scope == "dataset":
        return [req.dataset] if req.dataset is not None else []
    if req.scope == "all":
        if ctx.open_datasets is None:
            return sorted({s.dataset for s in core.scope_sessions(
                ctx, scope="all", include_archived=req.include_archived)
                if isinstance(s.dataset, str)})
        return list(ctx.open_datasets)
    return []           # "unbound" と "registered"（登録済みの全データセット。名前を並べない）


def target_text(req, datasets: list[str]) -> str | None:
    """見出しと AI 検索の表示行に出す対象（選択した範囲の略記）。unbound は None。

    ダイアログの選択肢と同じく、未所属のチャットも検索に含むことは書かない。"""
    if req.scope == "dataset" and req.dataset is not None:
        return req.dataset
    if req.scope == "all":
        return ", ".join(datasets) if datasets else tr("chat.search.scope.all_short")
    if req.scope == "registered":
        return tr("chat.search.scope.registered_short")
    return None


_MD_SPECIAL = set("\\`*_[]<>#~|")


def _md_escape(text) -> str:
    return "".join("\\" + ch if ch in _MD_SPECIAL else ch for ch in str(text))


def hits_to_markdown(hits, req, *, datasets: list[str], when: float) -> str:
    """標準検索の結果（hits は全件）を Markdown にする。各 hit は chatsearch: リンク。

    1 行目は対象・実行日時・件数（0 件ならこの 1 行だけ）。"""
    target = target_text(req, datasets)
    if target is None:
        head = tr("chat.search.result.header_unbound", time=core.format_ts(when), n=len(hits))
    else:
        head = tr("chat.search.result.header", target=_md_escape(target),
                  time=core.format_ts(when), n=len(hits))
    if req.include_archived:
        head += tr("chat.search.result.archived_included")
    if not hits:
        return head
    return "\n".join([head, "", *_hit_lines(hits, req)])


def _role_text(role) -> str:
    return tr("chat.role.user") if role == "user" else tr("chat.role.assistant")


def _hit_lines(hits, req) -> list[str]:
    """一致検索のヒットの行（最大 _MAX_MD_HITS 行と「ほか n 件」）。"""
    lines = []
    for i, h in enumerate(hits[:_MAX_MD_HITS], 1):
        name = _md_escape(core._collapse(h.title))
        if req.scope in ("all", "registered") and h.dataset is not None:
            name = f"{_md_escape(h.dataset)} / {name}"
        if h.archived:
            name += _md_escape(tr("chat.search.result.archived_mark"))
        when = core.format_ts(h.updated)
        link = core.make_search_link(h.session_id, h.msg_index, req.query)
        lines.append(f"{i}. [{name}]({link}) · {_role_text(h.role)} · {when} — "
                     f"{_md_escape(h.snippet)}")
    if len(hits) > _MAX_MD_HITS:
        lines += ["", tr("chat.search.result.more", n=len(hits) - _MAX_MD_HITS)]
    return lines


# ----- embedding search (Issue #128) -----


def embedding_ready() -> bool:
    """埋め込み検索・無料検索を出すか（llm_bridge.chat_index.is_ready）。never raise。

    機能がオフ（既定）なら ui_prefs を 1 回読むだけで、索引のモジュールを import しない
    （gui.chat._chat_recall_ready と同じ流儀。このモジュールは gui.chat を import しない）。"""
    try:
        if _read_pref("chat_recall_enabled", False) is not True:   # chat_index.PREF_ENABLED
            return False
        from llm_bridge import chat_index
        return bool(chat_index.is_ready())
    except Exception:  # noqa: BLE001 — never raise（使えない扱い）
        _log.debug("chat recall readiness check failed", exc_info=True)
        return False


def _embed_model():
    """選択中の埋め込みモデル（catalog の EmbedModel）。読めなければ None。never raise。"""
    try:
        from llm_bridge import chat_index
        return chat_index.readiness().model
    except Exception:  # noqa: BLE001
        _log.debug("chat recall model lookup failed", exc_info=True)
        return None


def embed_model_text() -> str | None:
    """ダイアログに出す埋め込みモデルの行（表示名と次元）。None なら出さない。"""
    m = _embed_model()
    if m is None:
        return None
    return tr("chat.search.dialog.embed_model", model=tr(m.label_key), dims=m.dims)


def embed_model_id() -> str:
    """結果の見出しに出すモデル（catalog の id。同期されるタブの本文に残すので訳さない）。"""
    m = _embed_model()
    return m.id if m is not None else "-"


def _embedding_header(text_hits, req, *, datasets, when, model) -> str:
    target = target_text(req, datasets)
    t = core.format_ts(when)
    if text_hits is not None:
        if target is None:
            head = tr("chat.search.result.header_unbound", time=t, n=len(text_hits))
        else:
            head = tr("chat.search.result.header", target=_md_escape(target), time=t,
                      n=len(text_hits))
    else:
        if target is None:
            target = tr("chat.search.scope.registered_short")
        head = tr("chat.search.result.header_semantic", target=_md_escape(target), time=t,
                  model=_md_escape(model))
    if req.include_archived:
        head += tr("chat.search.result.archived_included")
    return head


def embedding_markdown(text_hits, req, *, datasets: list[str], when: float, model: str,
                       related: str | None = None) -> str:
    """埋め込み検索（text_hits=None）・無料検索の結果タブの本文。

    無料検索は一致検索の節（hits_to_markdown と同じ行。0 件なら節ごと省く）と関連の節、
    埋め込み検索は関連の行だけ（節見出しなし）。related が None なら「検索中…」。
    完了時もこの関数で全体を組み直す（一致の節は同じ入力から同じ文字列になる）。"""
    lines = [_embedding_header(text_hits, req, datasets=datasets, when=when, model=model), ""]
    if text_hits is not None:
        if text_hits:
            lines += ["### " + tr("chat.search.section.exact"), "", *_hit_lines(text_hits, req),
                      ""]
        lines += ["### " + tr("chat.search.section.related", model=_md_escape(model)), ""]
    lines.append(related if related is not None else tr("chat.search.related.running"))
    return "\n".join(lines)


def shown_related(hits, exclude=frozenset()) -> list:
    """関連の節に出すヒット（exclude の (完全な sid, idx) を除き、最大 _MAX_MD_HITS 件）。"""
    return [h for h in hits if (h.sid, h.idx) not in exclude][:_MAX_MD_HITS]


def related_section_markdown(hits, req, *, exclude=frozenset(), open_datasets=None) -> str:
    """埋め込み検索のヒット（chat_index.RecallHit）の行。exclude（(完全な sid, idx)）は除く。

    リンクにはヒットのデータセットを ?ds= で載せる（閉じているデータセットのヒットを案内する
    ため）。open_datasets（開いているデータセットの list）に無いデータセットのヒットには
    「開く」の opendataset: リンクを付ける。None（分からない）なら付けない。"""
    lines: list[str] = []
    for h in shown_related(hits, exclude):
        ds = h.dataset if isinstance(h.dataset, str) and h.dataset else None
        name = _md_escape(core._collapse(h.title))
        if ds is not None:
            name = f"{_md_escape(ds)} / {name}"
        if h.archived:
            name += _md_escape(tr("chat.search.result.archived_mark"))
        link = core.make_search_link(h.sid, h.idx, req.query, dataset=ds)
        line = (f"{len(lines) + 1}. [{name}]({link}) · {_role_text(h.role)} · "
                f"{core.format_ts(h.updated)} · {h.score:.4f} — "
                f"{_md_escape(core._clip(h.snippet, _RELATED_SNIPPET))}")
        if ds is not None and isinstance(open_datasets, (list, tuple)) \
                and ds not in open_datasets:
            line += (f" · [{_md_escape(tr('chat.search.result.open_dataset'))}]"
                     f"({core.make_open_dataset_link(ds)})")
        lines.append(line)
    if not lines:
        return tr("chat.search.related.none")
    return "\n".join(lines)


def run_recall(query: str, datasets, include_archived: bool):
    """RecallJob の中身（worker スレッド）。索引を差分更新してから検索する。

    更新は待たない（ほかの処理が索引を更新中ならそのまま今の索引で検索し updated_index=False）
    うえで RECALL_BUDGET_S で打ち切り、間に合わなかった分は stale で返す。"""
    from llm_bridge import chat_index
    return chat_index.ChatIndex.open().recall(
        query, datasets=datasets, include_archived=include_archived, k=_MAX_MD_HITS,
        update=True, blocking=False, budget_s=RECALL_BUDGET_S)


def recall_note(result) -> str | None:
    """埋め込み検索の完了時に status bar に出す 1 文（無ければ None）。

    優先順: 索引をほかで更新中 → 届かないデータセット → 索引の遅れ → そのほかの警告。"""
    if not getattr(result, "updated_index", True):
        return tr("chat.search.related.index_busy")
    warns = [(getattr(w, "code", ""), getattr(w, "dataset", None))
             for w in getattr(result, "warnings", None) or ()]
    unreachable = list(dict.fromkeys(ds for code, ds in warns if code == "unreachable" and ds))
    if unreachable:
        return tr("chat.search.related.unreachable", datasets=", ".join(unreachable))
    stale = getattr(result, "stale", None) or {}
    if stale:
        return tr("chat.search.related.stale", datasets=", ".join(sorted(stale)))
    # locked は updated_index、budget は stale で示す
    other = list(dict.fromkeys(f"{code} ({ds})" if ds else str(code) for code, ds in warns
                               if code not in ("locked", "budget", "unreachable")))
    if other:
        return tr("chat.search.related.warning", detail=", ".join(other))
    return None


class RecallJob(QObject):
    """埋め込み検索を 1 回、デーモンスレッドで走らせる（Issue #128）。

    結果は GUI スレッドで ``finished``（("ok", RecallResult) か ("error", 文字列)）に出す。
    worker スレッドは自分の ``_result`` を emit するだけで、受け手は GUI スレッドに属する
    この QObject の Slot なので、配送は queued になる（``gui/math_settings_dialog.py`` の
    Signal → bound method と同じ形）。親（ChatWidget）ごと消えていれば emit の RuntimeError を
    握り、配送待ちの分は Qt が捨てる。ChatWidget に Signal を足さない（scope=app を要さない）。
    テストは ``_spawn`` を差し替えて同期にする。"""

    finished = Signal(object)
    _result = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn
        self._result.connect(self._deliver)

    def run(self) -> None:
        try:
            payload = ("ok", self._fn())
        except Exception as e:  # noqa: BLE001 — 結果のタブに「検索できませんでした」を出す
            _log.warning("chat recall failed", exc_info=True)
            payload = ("error", f"{type(e).__name__}: {e}")
        try:
            self._result.emit(payload)
        except RuntimeError:       # 先に親（ChatWidget）ごと消えた
            pass

    @Slot(object)
    def _deliver(self, payload) -> None:
        self.finished.emit(payload)


def _spawn(job: RecallJob) -> None:
    threading.Thread(target=job.run, name="myanalysis-chat-recall", daemon=True).start()


# ----- history -----


def history_work_dir(ds, *, create: bool) -> Path | None:
    if ds is None:
        return None
    try:
        import dataset_config
        return dataset_config.get_work_dir(ds, create=create)
    except Exception:
        return None


def record_search(req, ctx, *, dataset, n_hits, session_id, ts=None, datasets=None) -> bool:
    """datasets を渡せばそれを記録する（埋め込み検索の完了時に、開始時の範囲で。ctx は None 可）。"""
    if req.scope == "unbound" or dataset is None:
        return False
    wd = history_work_dir(dataset, create=True)
    if wd is None:
        return False
    try:
        entry = history.new_entry(
            req, datasets=search_datasets(req, ctx) if datasets is None else datasets,
            n_hits=n_hits, session_id=session_id, ts=ts)
    except ValueError:
        return False
    return history.append_history(wd, entry)


# ----- ui_prefs -----


def _read_pref(key, default):
    try:
        from llm_bridge.paths import read_ui_pref
        return read_ui_pref(key, default)
    except Exception:
        return default


def _write_pref(key, value) -> None:
    try:
        from llm_bridge.paths import update_ui_pref
        update_ui_pref(key, value)
    except Exception:
        pass


def _load_bool(key) -> bool:
    v = _read_pref(key, False)
    return v if isinstance(v, bool) else False


def load_scope_pref() -> str:
    v = _read_pref("chat_search_scope", "dataset")
    return v if v in ("dataset", "all", "registered") else "dataset"


def load_archived_pref() -> bool:
    return _load_bool("chat_search_archived")


def load_ai_pref() -> bool:
    return _load_bool("chat_search_ai")


def load_mode_pref(items) -> str:
    """前回の検索方法（items のどれか）。

    chat_search_mode（使える状態のときだけ書く。Issue #128）が items にあり、新旧両方の
    ビルドが書く chat_search_ai と食い違わなければそれ。それ以外は chat_search_ai から
    （旧ビルドと同じ読み方。機能がオフなら埋め込み検索・無料検索は一致検索に倒れる）。"""
    ai = load_ai_pref()
    m = _read_pref("chat_search_mode", None)
    if isinstance(m, str) and m in items and (m == "ai") == ai:
        return m
    return "ai" if ai else "text"


def load_history_open_pref() -> bool:
    return _load_bool("chat_search_history_open")


# ----- dialog -----


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def _mode_label(mode: str) -> str:
    if mode == "semantic":
        return tr("chat.search.mode.semantic")
    if mode == "hybrid":
        return tr("chat.search.mode.hybrid")
    if mode == "ai":
        return tr("chat.search.mode.ai")
    return tr("chat.search.mode.text")


def _mode_hint(mode: str) -> str:
    if mode == "semantic":
        return tr("chat.search.dialog.semantic_hint")
    if mode == "hybrid":
        return tr("chat.search.dialog.hybrid_hint")
    if mode == "ai":
        return tr("chat.search.dialog.ai_hint")
    return ""


def _history_mode_label(mode: str) -> str:
    """検索履歴の「種別」列（埋め込み・無料は短い名前）。"""
    if mode == "semantic":
        return tr("chat.search.history.mode_semantic")
    if mode == "hybrid":
        return tr("chat.search.history.mode_hybrid")
    if mode == "ai":
        return tr("chat.search.mode.ai")
    return tr("chat.search.mode.text")


class ChatSearchDialog(QDialog):
    def __init__(self, chat_widget, parent=None):
        super().__init__(parent)
        self._chat = chat_widget
        self._jump_sid: str | None = None
        self._history_rows: list[tuple[str, dict]] = []
        self._applying_entry = False
        self.setWindowTitle(tr("chat.search.dialog.title"))
        self.setSizeGripEnabled(True)

        ctx = chat_widget.search_context()
        self._current = ctx.current_dataset
        open_ds = list(ctx.open_datasets or [])
        if self._current is not None and self._current not in open_ds:
            open_ds.insert(0, self._current)
        self._open = open_ds

        layout = QVBoxLayout(self)

        self._active_label = QLabel("")
        f = self._active_label.font()
        f.setBold(True)
        self._active_label.setFont(f)
        if self._current is not None:
            self._active_label.setText(
                tr("chat.search.dialog.active_dataset", dataset=self._current))
        elif not open_ds:
            self._active_label.setText(tr("chat.search.dialog.no_dataset"))
        else:
            self._active_label.hide()
        layout.addWidget(self._active_label)

        # ----- query pages -----
        self._mode_stack = QStackedWidget()
        page0 = QWidget()
        f0 = QFormLayout(page0)
        f0.setContentsMargins(0, 0, 0, 0)
        self._query_edit = QLineEdit()
        f0.addRow(tr("chat.search.dialog.query"), self._query_edit)
        page1 = QWidget()
        f1 = QFormLayout(page1)
        f1.setContentsMargins(0, 0, 0, 0)
        self._ai_query_edit = QPlainTextEdit()
        self._ai_query_edit.setPlaceholderText(tr("chat.search.dialog.ai_query_placeholder"))
        self._ai_query_edit.setTabChangesFocus(True)
        lh = self._ai_query_edit.fontMetrics().lineSpacing()
        self._ai_query_edit.setFixedHeight(lh * 5 + 12)
        self._ai_query_edit.installEventFilter(self)
        f1.addRow(tr("chat.search.dialog.ai_query"), self._ai_query_edit)
        self._hint_edit = QLineEdit()
        self._hint_edit.setPlaceholderText(tr("chat.search.dialog.hint_placeholder"))
        f1.addRow(tr("chat.search.dialog.hint"), self._hint_edit)
        self._mode_stack.addWidget(page0)
        self._mode_stack.addWidget(page1)
        layout.addWidget(self._mode_stack)

        # ----- options -----
        # 検索方法（Issue #128）。埋め込み検索・無料検索は関連検索が使える状態のときだけ出す
        # （開いた時点で 1 回だけ判定する。使えなければ一致検索と AI 検索の 2 択）。
        self._embed_ready = embedding_ready()
        self._mode_items: list[str] = (["text", "semantic", "hybrid", "ai"] if self._embed_ready
                                       else ["text", "ai"])
        self._mode_combo = QComboBox()
        for i, m in enumerate(self._mode_items):
            self._mode_combo.addItem(_mode_label(m))
            hint = _mode_hint(m)
            if hint:
                self._mode_combo.setItemData(i, hint, Qt.ItemDataRole.ToolTipRole)
        self._mode_combo.setCurrentIndex(self._mode_items.index(load_mode_pref(self._mode_items)))
        self._prev_mode = self._mode()

        self._form = form = QFormLayout()
        self._scope_combo = QComboBox()
        # 選択肢はコンボの index と対応するリストで持つ（itemData に tuple を入れると
        # PySide6 6.10.1 は list で返し、比較・isinstance が外れる）。項目は
        # _fill_scope_combo でだけ作る（検索方法で「登録済みの全データセット」が出入りする）。
        self._scope_items: list[tuple[str, str | None]] = []
        self._scope_row_visible = True
        form.addRow(tr("chat.search.dialog.scope"), self._scope_combo)
        self._switch_note = QLabel("")
        self._switch_note.setWordWrap(True)
        form.addRow("", self._switch_note)
        self._fill_scope_combo(None)
        self._archived_check = QCheckBox(tr("chat.search.dialog.archived"))
        self._archived_check.setChecked(load_archived_pref())
        form.addRow("", self._archived_check)
        form.addRow(tr("chat.search.dialog.mode"), self._mode_combo)
        self._engine_label = QLabel(ai_engine_label_text())
        form.addRow("", self._engine_label)
        self._embed_label = QLabel("")
        self._embed_in_ai = False
        if self._embed_ready:          # オフなら索引のモジュールに触れない
            self._embed_label.setText(embed_model_text() or "")
            try:
                from llm_bridge import chat_index
                self._embed_in_ai = bool(chat_index.load_prefs().in_ai_search)
            except Exception:  # noqa: BLE001
                self._embed_in_ai = False
        form.addRow("", self._embed_label)
        layout.addLayout(form)
        self._mode_stack.setCurrentIndex(1 if self._mode() == "ai" else 0)
        self._update_mode_labels()

        # ----- history (collapsed by default) -----
        self._history_toggle = QToolButton()
        self._history_toggle.setCheckable(True)
        self._history_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._history_toggle.setAutoRaise(True)
        layout.addWidget(self._history_toggle)
        self._history_panel = QWidget()
        hl = QVBoxLayout(self._history_panel)
        hl.setContentsMargins(0, 0, 0, 0)
        self._history_tree = QTreeWidget()
        self._history_tree.setColumnCount(_HISTORY_COLS)
        self._history_tree.setHeaderLabels([
            tr("chat.search.history.col.time"), tr("chat.search.history.col.mode"),
            tr("chat.search.history.col.query"), tr("chat.search.history.col.scope"),
            tr("chat.search.history.col.hits"), tr("chat.search.history.col.dataset"),
        ])
        self._history_tree.setRootIsDecorated(False)
        self._history_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._history_tree.setMinimumHeight(
            self._history_tree.fontMetrics().lineSpacing() * 8 + 24)
        hl.addWidget(self._history_tree)
        self._history_delete_btn = QPushButton(tr("chat.search.history.delete"))
        self._history_delete_btn.setEnabled(False)
        hl.addWidget(self._history_delete_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        self._history_note = QLabel("")
        self._history_note.setWordWrap(True)
        self._history_note.hide()
        hl.addWidget(self._history_note)
        layout.addWidget(self._history_panel)
        opened = load_history_open_pref()
        self._history_toggle.setChecked(opened)     # connect より前（初期化で保存しない）
        self._history_panel.setVisible(opened)
        self._history_toggle.setArrowType(
            Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow)
        self._update_scope_row()
        if not open_ds:
            # 検索履歴は常に空なので出さない（no_dataset の注記だけ残す）。対象の行は、
            # 開いているデータセットが無くても埋め込み検索なら「登録済みの全データセット」を出す。
            self._history_toggle.hide()
            self._history_panel.hide()

        # ----- buttons -----
        self._buttons = QDialogButtonBox()
        self._search_btn = self._buttons.addButton(
            tr("chat.search.btn.search"), QDialogButtonBox.ButtonRole.AcceptRole)
        self._search_btn.setDefault(True)
        self._buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        # ----- signals -----
        self._query_edit.textChanged.connect(self._update_search_enabled)
        self._ai_query_edit.textChanged.connect(self._update_search_enabled)
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self._scope_combo.currentIndexChanged.connect(self._on_scope_changed)
        self._history_toggle.toggled.connect(self._on_history_toggled)
        self._history_tree.currentItemChanged.connect(self._on_history_current)
        self._history_tree.itemDoubleClicked.connect(self._on_history_double)
        self._history_delete_btn.clicked.connect(self._on_history_delete)

        self._update_switch_note()
        self._reload_history()
        self._update_search_enabled()
        QTimer.singleShot(0, self._focus_main_input)

    # ----- helpers -----

    def _mode(self) -> str:
        i = self._mode_combo.currentIndex()
        return self._mode_items[i] if 0 <= i < len(self._mode_items) else "text"

    def _set_mode(self, mode: str) -> None:
        if mode in self._mode_items:
            self._mode_combo.setCurrentIndex(self._mode_items.index(mode))

    def _focus_main_input(self) -> None:
        w = self._ai_query_edit if self._mode() == "ai" else self._query_edit
        w.setFocus()

    def _main_text(self) -> str:
        if self._mode() == "ai":
            return self._ai_query_edit.toPlainText().strip()
        return self._query_edit.text().strip()

    def _update_search_enabled(self, *_) -> None:
        self._search_btn.setEnabled(bool(self._main_text()))

    def _scope_data(self) -> tuple[str, str | None]:
        i = self._scope_combo.currentIndex()
        return self._scope_items[i] if 0 <= i < len(self._scope_items) else ("none", None)

    def _scope_index(self, data: tuple[str, str | None]) -> int:
        try:
            return self._scope_items.index(data)
        except ValueError:
            return -1

    def _wanted_scope_items(self) -> list[tuple[str, str | None]]:
        items: list[tuple[str, str | None]] = [("dataset", ds) for ds in self._open]
        if self._open:
            items.append(("all", None))
        if self._mode() in EMBEDDING_MODES:
            items.append(("registered", None))
        return items

    def _scope_label(self, kind: str, ds) -> str:
        if kind == "dataset":
            return tr("chat.search.scope.dataset", dataset=ds)
        if kind == "all":
            return tr("chat.search.scope.all_named", datasets=", ".join(self._open))
        return tr("chat.search.scope.registered")

    def _choose_scope(self, keep) -> int:
        """作り直した範囲のどれを選ぶか。keep（直前の選択）→ pref → 前面のデータセット。"""
        if keep is not None:
            i = self._scope_index(keep)
            if i >= 0:
                return i
            if keep[0] == "registered":           # 一致検索・AI 検索に戻した
                i = self._scope_index(("all", None))
                if i >= 0:
                    return i
        pref = load_scope_pref()
        if pref in ("all", "registered"):
            i = self._scope_index((pref, None))
            if i >= 0:
                return i
        if self._open:
            # 項目に無い "registered"（機能がオフ）も前面のデータセット（旧ビルドと同じ読み方）
            front = self._current if self._current is not None else self._open[0]
            return self._open.index(front)
        return 0 if self._scope_items else -1

    def _fill_scope_combo(self, keep) -> None:
        with QSignalBlocker(self._scope_combo):
            self._scope_combo.clear()
            self._scope_items = self._wanted_scope_items()
            for kind, ds in self._scope_items:
                self._scope_combo.addItem(self._scope_label(kind, ds))
            self._scope_combo.setCurrentIndex(self._choose_scope(keep))

    def _update_scope_row(self) -> None:
        """対象の行は選べる項目があるときだけ出す。"""
        visible = bool(self._scope_items)
        if visible != self._scope_row_visible:
            self._scope_row_visible = visible
            self._form.setRowVisible(self._scope_combo, visible)
            self._form.setRowVisible(self._switch_note, visible)
        self._update_switch_note()

    def _rebuild_scope_items(self) -> None:
        """検索方法が変わったときに範囲を作り直す（項目が同じなら何もしない）。"""
        if self._wanted_scope_items() == self._scope_items:
            return
        before = self._scope_data()
        self._fill_scope_combo(before)
        self._update_scope_row()
        if self._scope_data() != before and not self._applying_entry:
            self._reload_history()

    def _update_switch_note(self) -> None:
        kind, ds = self._scope_data()
        target = None
        if kind == "dataset" and ds != self._current:
            target = ds
        elif kind == "all" and self._current is None and self._open:
            target = self._open[0]
        text = ""
        if target is not None:
            text = tr("chat.search.dialog.will_switch", dataset=target)
        elif kind == "registered" and self._mode() == "hybrid":
            text = tr("chat.search.scope.registered_note")
        self._switch_note.setText(text)
        self._switch_note.setVisible(bool(text))

    def _update_mode_labels(self) -> None:
        mode = self._mode()
        self._form.setRowVisible(self._engine_label, mode == "ai")
        show_embed = self._embed_ready and bool(self._embed_label.text()) and (
            mode in EMBEDDING_MODES or (mode == "ai" and self._embed_in_ai))
        self._form.setRowVisible(self._embed_label, show_embed)
        self._mode_combo.setToolTip(_mode_hint(mode))

    def _set_note(self, text: str) -> None:
        self._history_note.setText(text)
        self._history_note.setVisible(bool(text))

    def _on_mode_changed(self, *_) -> None:
        mode, prev = self._mode(), self._prev_mode
        self._prev_mode = mode
        self._mode_stack.setCurrentIndex(1 if mode == "ai" else 0)
        # 語の欄は一致・埋め込み・無料で共用（1 行）、AI は複数行。跨ぐときだけ写す。
        if mode == "ai" and prev != "ai":
            t = self._query_edit.text().strip()
            if t:
                self._ai_query_edit.setPlainText(t)
        elif prev == "ai" and mode != "ai":
            body = self._ai_query_edit.toPlainText()
            if body.strip():
                self._query_edit.setText(" ".join(body.split()))
        self._update_mode_labels()
        self._rebuild_scope_items()
        self._update_switch_note()       # 範囲が同じでも注記は検索方法で変わる（無料検索）
        self._update_search_enabled()

    def _on_scope_changed(self, *_) -> None:
        self._update_switch_note()
        if not self._applying_entry:
            self._reload_history()

    # ----- history -----

    def _history_datasets(self) -> list[str]:
        kind, ds = self._scope_data()
        if kind == "dataset":
            return [ds]
        if kind in ("all", "registered"):
            return list(self._open)
        return []

    def _reload_history(self) -> None:
        kind, _ = self._scope_data()
        rows: list[tuple[str, dict]] = []
        bad = False
        for ds in self._history_datasets():
            wd = history_work_dir(ds, create=False)
            if wd is None:
                bad = True
                continue
            entries, status = history.load_history(wd)
            if status == "unreadable":
                bad = True
            rows.extend((ds, e) for e in entries)
        rows.sort(key=lambda r: -r[1]["ts"])
        self._history_rows = rows
        self._history_tree.clear()
        for i, (ds, e) in enumerate(rows):
            if history.entry_registered(e):
                scope_text = tr("chat.search.scope.registered_short")
            elif e["scope"] == "all":
                scope_text = tr("chat.search.scope.all_short")
            else:
                scope_text = e["datasets"][0]
            item = QTreeWidgetItem([
                core.format_ts(e["ts"]),
                _history_mode_label(history.entry_mode(e)),
                _first_line(e["query"]),
                scope_text,
                str(e["n_hits"]) if isinstance(e["n_hits"], int) else "—",
                ds,
            ])
            tip = e["query"]
            if e["hint"]:
                tip += "\n" + tr("chat.search.dialog.hint") + " " + e["hint"]
            item.setToolTip(2, tip)
            item.setData(0, Qt.ItemDataRole.UserRole, i)
            self._history_tree.addTopLevelItem(item)
        self._history_tree.setColumnHidden(_COL_DATASET, kind not in ("all", "registered"))
        self._history_toggle.setText(tr("chat.search.history.title_n", n=len(rows)))
        self._history_delete_btn.setEnabled(False)
        self._set_note(tr("chat.search.history.unreadable") if bad else "")

    def _row_of(self, item) -> tuple[str, dict] | None:
        if item is None:
            return None
        i = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(i, int) or not 0 <= i < len(self._history_rows):
            return None
        return self._history_rows[i]

    def _on_history_toggled(self, on: bool) -> None:
        self._history_panel.setVisible(on)
        self._history_toggle.setArrowType(
            Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        _write_pref("chat_search_history_open", bool(on))
        self.adjustSize()

    def _on_history_current(self, item, _prev=None) -> None:
        row = self._row_of(item)
        self._history_delete_btn.setEnabled(row is not None)
        if row is not None:
            self._apply_entry(*row)

    def _apply_entry(self, ds: str, entry: dict) -> None:
        self._applying_entry = True
        try:
            mode = history.entry_mode(entry)
            if mode not in self._mode_items:      # 埋め込み検索が使えないときは一致検索に倒す
                mode = "text"
            self._set_mode(mode)
            if mode == "ai":
                self._ai_query_edit.setPlainText(entry["query"])
                self._hint_edit.setText(entry["hint"])
            else:
                self._query_edit.setText(entry["query"])
            reg = self._scope_index(("registered", None)) if history.entry_registered(entry) \
                else -1
            if reg >= 0:
                self._scope_combo.setCurrentIndex(reg)
            elif entry["scope"] == "all":
                i = self._scope_index(("all", None))
                if i >= 0:
                    self._scope_combo.setCurrentIndex(i)
            elif entry["datasets"]:
                target = entry["datasets"][0]
                i = self._scope_index(("dataset", target))
                if i >= 0:
                    self._scope_combo.setCurrentIndex(i)
                else:
                    self._set_note(tr("chat.search.history.dataset_not_open", dataset=target))
            self._archived_check.setChecked(entry["include_archived"])
        finally:
            self._applying_entry = False
        self._update_search_enabled()

    def _on_history_double(self, item, _col=0) -> None:
        row = self._row_of(item)
        if row is None:
            return
        sess = core.resolve_session(self._chat.search_context(), row[1]["session_id"])
        if sess is None:
            self._set_note(tr("chat.search.history.no_results_tab"))
            return
        self._jump_sid = sess.id
        self.accept()

    def _on_history_delete(self) -> None:
        row = self._row_of(self._history_tree.currentItem())
        if row is None:
            return
        ds, entry = row
        wd = history_work_dir(ds, create=False)
        ok = wd is not None and history.delete_history_entry(wd, entry["id"])
        self._reload_history()
        if not ok:
            self._set_note(tr("chat.search.history.write_failed"))

    # ----- keys / result -----

    def eventFilter(self, obj, ev) -> bool:
        if obj is self._ai_query_edit and ev.type() == QEvent.Type.KeyPress \
                and ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                and (ev.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.accept()
            return True
        return super().eventFilter(obj, ev)

    def accept(self) -> None:
        if self._jump_sid is None:
            if not self._main_text():
                return
            kind, _ = self._scope_data()
            _write_pref("chat_search_scope", kind if kind in ("all", "registered") else "dataset")
            _write_pref("chat_search_archived", self._archived_check.isChecked())
            mode = self._mode()
            _write_pref("chat_search_ai", mode == "ai")
            if self._embed_ready:    # オフのときは書かない（ui_prefs を旧ビルドと同じに保つ）
                _write_pref("chat_search_mode", mode)
        super().accept()

    def request(self) -> SearchRequest | None:
        if self._jump_sid is not None:
            return None
        mode = self._mode()
        if mode == "ai":
            query = self._ai_query_edit.toPlainText().strip()
            hint = self._hint_edit.text().strip()
        else:
            query = self._query_edit.text().strip()
            hint = ""
        kind, ds = self._scope_data()
        if kind == "dataset":
            scope, dataset = "dataset", ds
        elif kind in ("all", "registered"):
            scope, dataset = kind, None
        else:
            scope, dataset = "unbound", None
        return SearchRequest(query=query, scope=scope, dataset=dataset,
                             include_archived=self._archived_check.isChecked(),
                             ai=(mode == "ai"), hint=hint, mode=mode)

    def jump_session_id(self) -> str | None:
        return self._jump_sid
