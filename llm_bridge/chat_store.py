"""Chat session persistence — Qt-free core.

A chat session (a conversation thread in the chat dock) is persisted per dataset
at `<work_dir>/chat_sessions/<id>.json` so it syncs with the data and follows it
across PCs/repos, mirroring `session.py`'s per-dataset session.json.

IMPORTANT: this module must stay Qt-free and free of config/dataset_config
imports. It is imported from `llm_bridge/session.py` (which the CLI imports too),
so it stays standard-library only. Serialization is testable headless. work_dir
resolution is the caller's responsibility — every function here takes a
`pathlib.Path` work_dir / target path.
"""

from __future__ import annotations

import copy
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
    backend_session_id: str | None
    created: float
    updated: float
    order: float = 0.0  # explicit tab position; persisted, lower = leftmost
    tool_display: str | None = None  # per-session display override; None = follow default
    archived: bool = False
    # Per-tab unsent composer text. Deliberately NOT persisted: session_to_dict
    # omits it and session_from_dict never reads it, so a draft lives only as
    # long as the process. Kept on the session (not a side dict) so deleting a
    # session drops its draft with it.
    draft: str = ""


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
    return {
        "version": SCHEMA_VERSION,
        "id": sess.id,
        "title": sess.title,
        "messages": [message_to_dict(m) for m in sess.messages],
        "dataset": sess.dataset,
        "backend_name": sess.backend_name,
        "backend_session_id": sess.backend_session_id,
        "created": sess.created,
        "updated": sess.updated,
        "order": sess.order,
        "tool_display": sess.tool_display,
        "archived": bool(getattr(sess, "archived", False)),
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
    return ChatSession(
        id=data.get("id"),
        title=data.get("title", _DEFAULT_TITLE),
        messages=messages,
        dataset=data.get("dataset"),
        backend_name=data.get("backend_name"),
        backend_session_id=data.get("backend_session_id"),
        created=data.get("created"),
        updated=data.get("updated"),
        order=data.get("order", 0.0),
        tool_display=tool_display,
        archived=bool(data.get("archived", False)),
    )


# ----- factory -----


def new_session(
    backend_name: str,
    system_prompt: str,
    dataset: str | None = None,
    title: str = _DEFAULT_TITLE,
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
    )


def fork_session(src: ChatSession, cut: int, *, title: str) -> ChatSession:
    """src.messages[:cut] を deep-copy した独立の新セッションを mint する。

    非破壊: src は一切変更しない。backend_session_id は必ず None（分岐先は
    サーバ側会話状態を引き継がない）。`title` は呼び出し側が決めた最終文字列を
    そのまま採用する — この関数は Qt-free / i18n-free なので tr() や suffix 付与は
    行わない（センチネル _DEFAULT_TITLE を渡せば据え置き、それ以外はその文字列）。
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
        tool_display=src.tool_display,
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


def write_session_file(work_dir: Path, sess: ChatSession) -> None:
    """Durably write <work_dir>/chat_sessions/<id>.json (+ .bak, tmp + replace).

    This is the only function that creates chat_sessions/ (write path). Chat
    history is irreplaceable and lives on the synced mount, so it is written with
    a `.bak` sidecar (durable_write_json) — if the primary is evicted with a
    failed upload, read_session_file/load_dataset_sessions recover from `.bak`.
    Best-effort: OSError is swallowed.
    """
    from common.paths import durable_write_json
    try:
        target_dir = work_dir / "chat_sessions"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{sess.id}.json"
        durable_write_json(target, session_to_dict(sess))
    except OSError:
        _log.warning("write_session_file: failed to write %r", sess.id, exc_info=True)


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
    existing: list[ChatSession], incoming: list[ChatSession]
) -> list[ChatSession]:
    """Merge `incoming` onto `existing` by id (pure, non-destructive).

    Same id → replace ONLY when incoming.updated > existing.updated (strict);
    tie or older keeps `existing`. New id → appended. Returns a new list.
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
        elif (s.updated or 0.0) > (cur.updated or 0.0):
            by_id[s.id] = s
    return [by_id[i] for i in order]
