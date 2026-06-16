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


# ----- per-file IO -----


def read_session_file(path: Path) -> ChatSession | None:
    """Read one chat session file. Returns None on ANY problem.

    Pure (security-free) deserialization boundary: a single corrupt file must
    localize to a single missing session, never abort startup. Catches every
    exception (FileNotFoundError/OSError/JSONDecodeError/ValueError plus the
    KeyError/TypeError/AttributeError that key-missing valid JSON or unknown
    versions can induce).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return session_from_dict(data)
    except Exception:
        return None


def write_session_file(work_dir: Path, sess: ChatSession) -> None:
    """Atomically write <work_dir>/chat_sessions/<id>.json (tmp + replace).

    This is the only function that creates chat_sessions/ (write path).
    Best-effort: OSError is swallowed. The tmp file is always cleaned up.
    """
    try:
        target_dir = work_dir / "chat_sessions"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{sess.id}.json"
        tmp = target.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(session_to_dict(sess), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
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
    sessions: list[ChatSession] = []
    for path in target_dir.glob("*.json"):
        sess = read_session_file(path)
        if sess is not None:
            sessions.append(sess)
    sessions.sort(key=lambda s: (s.order, -(s.updated or 0.0)))
    return sessions


def delete_session_file(work_dir: Path, id: str) -> bool:
    """Best-effort delete of <work_dir>/chat_sessions/<id>.json.

    Returns whether the file is absent after the call: True if it was already
    gone or unlink succeeded, False if an OSError prevented deletion.
    """
    target = work_dir / "chat_sessions" / f"{id}.json"
    try:
        target.unlink(missing_ok=True)
        return True
    except OSError:
        _log.warning("delete_session_file: failed to delete %r", id, exc_info=True)
        return False


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
