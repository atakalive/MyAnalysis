"""Chat search history — Qt-free store (Issue #108).

`<work_dir>/chat_search_history.json`（同期ドライブ上＝どの PC でも同じ履歴。
`chat_sessions/` と同じ層）に検索の履歴を durable（primary + `.bak`）で残す。
PC 間ロックは取れない前提（session.json と同じ last-writer-wins）。

work_dir の解決は呼び出し側の責務。common.paths は関数内で遅延 import する
（chat_store と同じ）。
"""

from __future__ import annotations

import math
import time
import uuid
from datetime import datetime
from pathlib import Path

HISTORY_FILE = "chat_search_history.json"
_VERSION = 1


def history_path(work_dir) -> Path:
    return Path(work_dir) / HISTORY_FILE


def valid_entry(e) -> bool:
    """UI と読み戻しが参照する全キーを型まで検査する。"""
    if not isinstance(e, dict):
        return False
    if not isinstance(e.get("id"), str) or not e["id"]:
        return False
    ts = e.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return False
    try:
        # 巨大な整数では isfinite 自体が OverflowError。有限でも 1e20 や年 10000 以降は
        # 日時にできず、履歴の一覧（datetime.fromtimestamp）で落ちるので、ここで弾く。
        if not math.isfinite(ts):
            return False
        datetime.fromtimestamp(ts)
    except (OverflowError, OSError, ValueError):
        return False
    if e.get("mode") not in ("text", "ai"):
        return False
    if not isinstance(e.get("query"), str) or not isinstance(e.get("hint"), str):
        return False
    scope = e.get("scope")
    if scope not in ("dataset", "all"):
        return False
    ds = e.get("datasets")
    if not isinstance(ds, list) or not all(isinstance(x, str) for x in ds):
        return False
    if scope == "dataset" and not ds:
        return False
    if not isinstance(e.get("include_archived"), bool):
        return False
    n = e.get("n_hits")
    if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 0):
        return False
    if not isinstance(e.get("session_id"), str) or not e["session_id"]:
        return False
    return True


def load_history(work_dir) -> tuple[list[dict], str]:
    """(entries, status)。status は durable_read_json の値（形が違えば 'unreadable'）。never raise."""
    try:
        from common.paths import durable_read_json
        status, data = durable_read_json(history_path(work_dir))
    except Exception:
        return ([], "unreadable")
    if status == "absent":
        return ([], "absent")
    if status not in ("ok", "recovered"):
        return ([], "unreadable")
    if not isinstance(data, dict) or data.get("version") != _VERSION \
            or not isinstance(data.get("entries"), list):
        return ([], "unreadable")
    return ([e for e in data["entries"] if valid_entry(e)], status)


def _has_content(path: Path) -> bool:
    """primary か .bak に守るべき中身があるか（llm_bridge.paths._preserve_unreadable の 2 コピー版）。"""
    from common.paths import bak_path
    for p in (path, bak_path(path)):
        try:
            if p.stat().st_size > 0:
                return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False


def append_history(work_dir, entry: dict, *, max_entries: int = 500) -> bool:
    """entry を末尾に追記（上限 max_entries で古い順に落とす）。never raise."""
    try:
        from common.paths import durable_write_json
        path = history_path(work_dir)
        entries, status = load_history(work_dir)
        if status == "unreadable":
            if _has_content(path):
                return False
            entries = []
        entries = (entries + [entry])[-max_entries:]
        durable_write_json(path, {"version": _VERSION, "entries": entries})
        return True
    except (OSError, ValueError, TypeError):
        return False


def delete_history_entry(work_dir, entry_id) -> bool:
    try:
        from common.paths import durable_write_json
        entries, status = load_history(work_dir)
        if status not in ("ok", "recovered"):
            return False
        kept = [e for e in entries if e.get("id") != entry_id]
        if len(kept) == len(entries):
            return False
        durable_write_json(history_path(work_dir), {"version": _VERSION, "entries": kept})
        return True
    except Exception:
        return False


def new_entry(req, *, datasets, n_hits, session_id) -> dict:
    if req.scope not in ("dataset", "all"):
        raise ValueError(f"a search with scope={req.scope!r} is not recorded")
    return {
        "id": uuid.uuid4().hex,
        "ts": time.time(),
        "mode": "ai" if req.ai else "text",
        "query": req.query,
        "hint": req.hint,
        "scope": req.scope,
        "datasets": list(datasets),
        "include_archived": bool(req.include_archived),
        "n_hits": n_hits,
        "session_id": session_id,
    }
