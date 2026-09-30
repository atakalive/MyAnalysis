"""Chat session persistence — Qt-free core.

A chat session (a conversation thread in the chat dock) is persisted per dataset
at `<work_dir>/chat_sessions/<id>.json` so it syncs with the data and follows it
across PCs/repos, mirroring `session.py`'s per-dataset session.json.

IMPORTANT: this module must stay Qt-free and free of config/dataset_config
imports. It is imported from `llm_bridge/session.py` (which the CLI imports too),
so it imports only the standard library, the Qt-free llm_backend.base, and
(lazily, inside write_session_file) common.paths. Serialization is testable
headless. work_dir resolution is the caller's responsibility — every function
here takes a `pathlib.Path` work_dir / target path.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from llm_backend.base import Message

_log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_DEFAULT_TITLE = "新しいチャット"


# NOTE: __slots__ を付けないこと — hot-reload 中の旧インスタンスへの動的属性追加
# (archived 等) が壊れる。session_to_dict の getattr 防御と setattr 経路が
# __dict__ 動的属性を前提にしている。
@dataclass
class ChatSession:
    id: str
    title: str
    messages: list[Message]
    dataset: str | None
    backend_name: str
    # Native resume token (claude --resume / pi --session / codex exec resume).
    # Deliberately NOT persisted here, like `draft` below: the native session it
    # points at lives on THIS PC (~/.claude, ~/.pi/agent/sessions, ~/.codex), so
    # syncing it would hand a nonexistent id to --resume on another machine.
    # Its persistence layer is the PC-local data/llm_state/backend_sessions.json
    # (llm_bridge.paths.read/write_backend_session); this field is just the
    # in-process working value, refilled from that store each turn.
    backend_session_id: str | None
    created: float
    updated: float
    order: float = 0.0  # explicit tab position; persisted, lower = leftmost
    tool_display: str | None = None  # per-session display override; None = follow default
    archived: bool = False
    # Per-session engine override. `engine` (an engines.ENGINES id) is the sentinel:
    # None = follow the global backend/model selection, and the other two are then
    # ignored. Empty model/provider mean "that engine's configured default", so
    # switching engine alone is expressible. Validated only as str here — the id is
    # resolved against the catalog at use time so that round-tripping through an
    # older build (or a PC lacking that engine) cannot silently erase the choice.
    engine: str | None = None
    engine_model: str | None = None
    engine_provider: str | None = None
    # 3 状態: None=全体設定に従う / ""=明示的にペルソナなし / 非空=ペルソナ名。
    # engine 同様 str 検証のみ・解決は使用時。未解決名は「なし」に degrade し
    # フィールドは書き換えない（ストアは PC ローカル、別 PC でも選択を保持）。
    persona: str | None = None
    # "chat" = 普通の会話 / "search" = チャット検索の結果タブ（Issue #108）。検索結果は
    # 全ての検索・verb の対象から外す。search_spec は検索の条件（続きの質問に指示を
    # 付け直すのに使う）で、kind="search" のときだけ意味を持つ。
    kind: str = "chat"
    search_spec: dict | None = None
    # Per-tab unsent composer text. Deliberately NOT persisted: session_to_dict
    # omits it and session_from_dict never reads it, so a draft lives only as
    # long as the process. Kept on the session (not a side dict) so deleting a
    # session drops its draft with it.
    draft: str = ""


_SEARCH_SCOPES = ("dataset", "all", "unbound")


def normalize_search_spec(v) -> dict | None:
    """検索条件 dict を正規化する。不正なら None（Issue #108）。

    scope の 3 値は llm_bridge.chat_search.SCOPES と同じ（chat_store は Qt 非依存かつ
    chat_search を import しないのでここに直書きする）。
    """
    if not isinstance(v, dict):
        return None
    scope = v.get("scope")
    if scope not in _SEARCH_SCOPES:
        return None
    inc, ai = v.get("include_archived"), v.get("ai")
    if not isinstance(inc, bool) or not isinstance(ai, bool):
        return None
    ds = v.get("dataset")
    if scope == "dataset":
        if not isinstance(ds, str) or not ds:
            return None
    else:
        ds = None
    return {"scope": scope, "dataset": ds, "include_archived": inc, "ai": ai}


# ----- Message ⇄ dict -----


def message_to_dict(m: Message) -> dict:
    """Serialize a Message to a plain dict. Omits None fields for compactness."""
    d: dict = {"role": m.role}
    if m.content is not None:
        d["content"] = m.content
    if m.tool_calls is not None:
        d["tool_calls"] = m.tool_calls
    if m.tool_call_id is not None:
        d["tool_call_id"] = m.tool_call_id
    return d


def message_from_dict(d: dict) -> Message:
    """Build a Message from a dict. Uses dict.get() throughout so missing keys
    do not raise KeyError; a missing `role` yields None → Message.__post_init__
    raises ValueError, caught upstream."""
    return Message(
        role=d.get("role"),
        content=d.get("content"),
        tool_calls=d.get("tool_calls"),
        tool_call_id=d.get("tool_call_id"),
    )


# ----- ChatSession ⇄ dict -----


def session_to_dict(sess: ChatSession) -> dict:
    kind = getattr(sess, "kind", "chat")
    return {
        "version": SCHEMA_VERSION,
        "id": sess.id,
        "title": sess.title,
        "messages": [message_to_dict(m) for m in sess.messages],
        "dataset": sess.dataset,
        "backend_name": sess.backend_name,
        # backend_session_id is intentionally absent — see the field's comment.
        "created": sess.created,
        "updated": sess.updated,
        "order": sess.order,
        # getattr for every optional field: a hot-reload `patch` leaves live
        # instances without newly-added attributes, and an AttributeError here
        # would escape write_session_file and fail the chat save.
        "tool_display": getattr(sess, "tool_display", None),
        "archived": bool(getattr(sess, "archived", False)),
        "engine": getattr(sess, "engine", None),
        "engine_model": getattr(sess, "engine_model", None),
        "engine_provider": getattr(sess, "engine_provider", None),
        "persona": getattr(sess, "persona", None),
        "kind": kind,
        "search_spec": (normalize_search_spec(getattr(sess, "search_spec", None))
                        if kind == "search" else None),
    }


def session_from_dict(data: dict) -> ChatSession:
    """Build a ChatSession from a dict, validating the schema version.

    Raises ValueError on unsupported version or malformed messages, TypeError if
    `messages` is not a list. Callers (read_session_file) catch broadly.
    """
    if data.get("version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported chat session version {data.get('version')!r}")
    raw_messages = data.get("messages")
    if not isinstance(raw_messages, list):
        raise TypeError("chat session 'messages' must be a list")
    messages = [message_from_dict(m) for m in raw_messages]
    tool_display = data.get("tool_display")
    if tool_display not in (None, "full", "compact", "hidden"):
        tool_display = None
    # persona は 3 状態のため _opt_str を通さない: blank を None に潰すと
    # 「明示的にペルソナなし」("") が「全体設定に従う」(None) に化ける。
    raw = data.get("persona")
    persona = raw.strip() if isinstance(raw, str) else None
    kind = "search" if data.get("kind") == "search" else "chat"
    search_spec = normalize_search_spec(data.get("search_spec")) if kind == "search" else None

    def _opt_str(key):
        """Accept any str; anything else (or missing) → None.

        Deliberately NOT whitelisted against engines.ENGINES: chat_store must stay
        importable without llm_backend (`python -m llm_bridge` runs without PySide6
        and shouldn't drag the backend catalog in), and whitelisting here would
        delete a newer build's engine id whenever the file round-trips through an
        older one. The id is resolved — and degraded to the global default — at use.
        """
        v = data.get(key)
        return v if isinstance(v, str) and v.strip() else None

    return ChatSession(
        id=data.get("id"),
        title=data.get("title", _DEFAULT_TITLE),
        messages=messages,
        dataset=data.get("dataset"),
        backend_name=data.get("backend_name"),
        # Never read back: a token in an old file (or one synced from another PC)
        # points at a native session that does not exist here. The PC-local store
        # is the only source; a missing token just means "replay full history".
        backend_session_id=None,
        created=data.get("created"),
        updated=data.get("updated"),
        order=data.get("order", 0.0),
        tool_display=tool_display,
        archived=bool(data.get("archived", False)),
        engine=_opt_str("engine"),
        engine_model=_opt_str("engine_model"),
        engine_provider=_opt_str("engine_provider"),
        persona=persona,
        kind=kind,
        search_spec=search_spec,
    )


# 指紋に入れないフィールド: order は並び位置として別に判定し、dataset はファイルの
# 置き場所が真実（merge_dataset_sessions が付け直す）なので内容に数えない。
_FINGERPRINT_EXCLUDE = ("order", "dataset")


def content_fingerprint(sess: ChatSession) -> str:
    """永続される内容の指紋（order と dataset を除く）。保存時の変化判定に使う（Issue #106 A-3）。

    updated は PC 間で一意な改訂番号ではない（時計が遅れた 2 台がどちらも
    max(time, T + 1e-3) で同じ値を作り得る）ので、内容の同一性は updated ではなく
    この指紋で見る。session_from_dict を一度通して正規化してから数えるので、
    ディスクから読んだ版と手元の版で表記ゆれ（空白だけのエンジン指定など）が出ない。
    """
    d = session_to_dict(session_from_dict(session_to_dict(sess)))
    for k in _FINGERPRINT_EXCLUDE:
        d.pop(k, None)
    text = json.dumps(d, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ----- factory -----


def new_session(
    backend_name: str,
    system_prompt: str,
    dataset: str | None = None,
    title: str = _DEFAULT_TITLE,
    kind: str = "chat",
    search_spec: dict | None = None,
) -> ChatSession:
    """Mint a fresh ChatSession seeded with a single system Message."""
    now = time.time()
    return ChatSession(
        id=uuid.uuid4().hex,
        title=title,
        messages=[Message(role="system", content=system_prompt)],
        dataset=dataset,
        backend_name=backend_name,
        backend_session_id=None,
        created=now,
        updated=now,
        kind=kind,
        search_spec=search_spec,
    )


def fork_session(src: ChatSession, cut: int, *, title: str) -> ChatSession:
    """src.messages[:cut] を deep-copy した独立の新セッションを mint する。

    非破壊: src は一切変更しない。backend_session_id は必ず None（分岐先は
    サーバ側会話状態を引き継がない）。`title` は呼び出し側が決めた最終文字列を
    そのまま採用する — この関数は Qt-free / i18n-free なので tr() や suffix 付与は
    行わない（センチネル _DEFAULT_TITLE を渡せば据え置き、それ以外はその文字列）。
    検索結果タブ（kind="search"）から分岐した先は普通の会話（kind="chat"・
    search_spec=None）になり、以後は検索対象に入る（Issue #108）。
    """
    now = time.time()
    return ChatSession(
        id=uuid.uuid4().hex,
        title=title,
        messages=copy.deepcopy(src.messages[:cut]),
        dataset=src.dataset,
        backend_name=src.backend_name,
        backend_session_id=None,
        created=now,
        updated=now,
        order=src.order,          # 暫定。保存時に list 位置から再採番される
        tool_display=getattr(src, "tool_display", None),
        # 分岐先も同じエンジンで続けるのが期待値（分岐して既定に戻られると困る）。
        engine=getattr(src, "engine", None),
        engine_model=getattr(src, "engine_model", None),
        engine_provider=getattr(src, "engine_provider", None),
        # 口調（ペルソナ）も同様に引き継ぐ。
        persona=getattr(src, "persona", None),
        kind="chat",
        search_spec=None,
    )


# ----- per-file IO -----


def read_session_file(path: Path) -> ChatSession | None:
    """Read one chat session file, falling back to its `.bak` sidecar. None on
    total failure.

    Pure (security-free) deserialization boundary: a single corrupt file must
    localize to a single missing session, never abort startup. Catches every
    exception (FileNotFoundError/OSError/JSONDecodeError/ValueError plus the
    KeyError/TypeError/AttributeError that key-missing valid JSON or unknown
    versions can induce). If the primary is missing/corrupt on the synced mount,
    the durable `.bak` copy (written by write_session_file) is tried next.
    """
    for p in (path, path.with_name(path.name + ".bak")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return session_from_dict(data)
        except Exception:
            continue   # primary が 0byte/破損/欠落なら .bak（durable コピー）へフォールバック
    return None


def read_session_file_status(work_dir: Path, id: str) -> tuple[str, ChatSession | None]:
    """<work_dir>/chat_sessions/<id>.json を primary と .bak の新しい方で読む（書かない）。never raise.

    ('ok', sess) / ('absent', None) = 両コピーとも無い /
    ('unreadable', None) = どちらかは在るが読めない（読取の OSError・同期途中を含む）、
    または中身が ChatSession にならない。
    """
    from common.paths import durable_read_json
    status, data = durable_read_json(work_dir / "chat_sessions" / f"{id}.json")
    if status == "absent":
        return ("absent", None)
    if status in ("ok", "recovered"):
        try:
            return ("ok", session_from_dict(data))
        except Exception:
            return ("unreadable", None)
    return ("unreadable", None)


def write_session_file(work_dir: Path, sess: ChatSession) -> None:
    """Durably write <work_dir>/chat_sessions/<id>.json (+ .bak, tmp + replace).

    This is the only function that creates chat_sessions/ (write path). Chat
    history is irreplaceable and lives on the synced mount, so it is written with
    a `.bak` sidecar (durable_write_json) — if the primary is evicted with a
    failed upload, read_session_file/load_dataset_sessions recover from `.bak`.
    書込・検証の失敗（`MountWriteError` を含む OSError など）は送出する。呼び出し側
    （`session._save_chat_sessions`）がデータセットの失敗として扱う。
    """
    from common.paths import durable_write_json
    target_dir = work_dir / "chat_sessions"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{sess.id}.json"
    durable_write_json(target, session_to_dict(sess))


def load_dataset_sessions(work_dir: Path) -> list[ChatSession]:
    """Load all chat sessions under <work_dir>/chat_sessions/.

    Read-only path: never mkdir. A missing directory → glob yields nothing → [].
    Corrupt files are dropped (read_session_file → None). Sorted by explicit
    `order` ascending (the user-controlled tab position), with `updated`
    descending as a tiebreaker so legacy files lacking `order` (all default 0.0)
    retain the historical most-recent-first ordering.
    """
    target_dir = work_dir / "chat_sessions"
    by_id: dict[str, ChatSession] = {}
    order: list[str] = []

    def _take(sess: ChatSession | None) -> None:
        if sess is not None and sess.id not in by_id:
            by_id[sess.id] = sess
            order.append(sess.id)

    for path in target_dir.glob("*.json"):        # '*.json' は '<id>.json.bak' に一致しない
        _take(read_session_file(path))
    # primary が evict/欠落した分を .bak から回収（primary が読めた分は read_session_file
    # 内で既に .bak も見るので、ここは primary が glob に現れないケースの保険）。
    for bak in target_dir.glob("*.json.bak"):
        primary = bak.with_name(bak.name[:-4])    # 末尾 ".bak" を除去 → '<id>.json'
        if not primary.exists():
            _take(read_session_file(primary))     # primary absent → .bak にフォールバック
    sessions = [by_id[i] for i in order]
    sessions.sort(key=lambda s: (s.order, -(s.updated or 0.0)))
    return sessions


def delete_session_file(work_dir: Path, id: str) -> bool:
    """Best-effort delete of <work_dir>/chat_sessions/<id>.json AND its .bak.

    Both copies must go — otherwise load_dataset_sessions would resurrect the
    deleted session from the durable .bak. Returns whether both are absent after
    the call: True if already gone / unlink succeeded, False if an OSError
    prevented deletion of either.
    """
    target = work_dir / "chat_sessions" / f"{id}.json"
    ok = True
    for p in (target, target.with_name(target.name + ".bak")):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            _log.warning("delete_session_file: failed to delete %r", p.name, exc_info=True)
            ok = False
    return ok


# ----- merge -----


def merge_sessions(
    existing: list[ChatSession], incoming: list[ChatSession],
    *, prefer_incoming: bool = False,
) -> list[ChatSession]:
    """Merge `incoming` onto `existing` by id (pure, non-destructive).

    Same id → replace ONLY when incoming.updated > existing.updated (strict);
    tie or older keeps `existing`. New id → appended. Returns a new list.

    prefer_incoming=True: same id → always replace (updated は比べない。保存時に
    他の PC の版を取り込む経路 — Issue #106 A-3)。位置は既存の要素の位置のまま。
    """
    by_id: dict[str, ChatSession] = {}
    order: list[str] = []
    for s in existing:
        if s.id not in by_id:
            order.append(s.id)
        by_id[s.id] = s
    for s in incoming:
        cur = by_id.get(s.id)
        if cur is None:
            by_id[s.id] = s
            order.append(s.id)
        elif prefer_incoming or (s.updated or 0.0) > (cur.updated or 0.0):
            by_id[s.id] = s
    return [by_id[i] for i in order]
