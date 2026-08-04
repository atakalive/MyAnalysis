"""LLM bridge specific paths. Lives parallel to common.paths but owns its own dir."""
import json
import time
from pathlib import Path
from common.paths import repo_root


def global_state_dir() -> Path:
    """Return data/llm_state/, creating it if missing."""
    p = repo_root() / "data" / "llm_state"
    p.mkdir(parents=True, exist_ok=True)
    return p


def commands_queue_dir() -> Path:
    """Return data/llm_state/commands/, creating it if missing."""
    p = global_state_dir() / "commands"
    p.mkdir(parents=True, exist_ok=True)
    return p


def command_log_path() -> Path:
    """Return data/llm_state/command_log.jsonl (file may not exist yet)."""
    return global_state_dir() / "command_log.jsonl"


def active_state_path() -> Path:
    """Return data/llm_state/active.json (file may not exist yet)."""
    return global_state_dir() / "active.json"


def ui_prefs_path() -> Path:
    """Return data/llm_state/ui_prefs.json (file may not exist yet)."""
    return global_state_dir() / "ui_prefs.json"


def read_ui_pref(key: str, default=None):
    """ui_prefs.json から key を読む。欠損/破損/型不一致/非 dict は default。never raise。"""
    try:
        data = json.loads(ui_prefs_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return default
    if not isinstance(data, dict):
        return default
    return data.get(key, default)


def update_ui_pref(key: str, value) -> None:
    """ui_prefs.json の key を value に更新。兄弟キー保持・atomic(temp+replace)・never raise。

    破損/transient で読めない（unreadable）ときは書込を skip する: 兄弟キーを巻き添えで
    失わないため（absent なら新規 {} から書いてよい）。"""
    from common.paths import read_json_classified
    try:
        path = ui_prefs_path()
        status, data = read_json_classified(path)
        if status == "unreadable":
            return                                 # 破損/transient → 兄弟キーを潰さない
        data = data if status == "ok" else {}      # absent → 新規
        data[key] = value
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except (OSError, ValueError, TypeError):        # 非 JSON-serializable value の TypeError も吸収（never-raise 厳守）
        pass


def recent_datasets_path() -> Path:
    """Return data/llm_state/recent_datasets.json (file may not exist yet).

    PC-local MRU (most-recently-opened) order for the dataset picker. Kept out of
    the synced meta.json so open-order does not mix across PCs.
    """
    return global_state_dir() / "recent_datasets.json"


def read_recent_datasets() -> dict:
    """Read the MRU map {name: epoch_float}. never raise; corrupt → fresh {}.

    Values are type-normalized: only str keys mapped to non-bool int/float epochs
    survive, so a hand-edited/corrupt entry can't poison the picker's sort.
    """
    try:
        data = json.loads(recent_datasets_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: v for k, v in data.items()
        if isinstance(k, str) and isinstance(v, (int, float))
        and not isinstance(v, bool)
    }


def note_recent_dataset(name: str) -> None:
    """Record that `name` was just opened (MRU). atomic(temp+replace)·never raise.

    Distinct from session.note_dataset (tracks empty-tabs persistence) and
    window.note_current_dataset (pushes the current dataset to chat) — this only
    stamps the PC-local recent_datasets.json used for the picker's default sort.
    """
    from common.paths import read_json_classified
    try:
        path = recent_datasets_path()
        status, raw = read_json_classified(path)
        if status == "unreadable":
            return                                 # 破損/transient → MRU を巻き添えで潰さない
        raw = raw if status == "ok" else {}
        data = {                                   # read_recent_datasets と同じ型正規化
            k: v for k, v in raw.items()
            if isinstance(k, str) and isinstance(v, (int, float))
            and not isinstance(v, bool)
        }
        data[name] = time.time()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except (OSError, ValueError, TypeError):
        pass


def last_window_path() -> Path:
    """Return data/llm_state/last_window.json (file may not exist yet).

    PC-local workspace record: which datasets were open together + the active
    one, so File → "前回のセッションを復元" can re-open them. A second PC-local
    record next to recent_datasets.json (MRU) — MRU is open-order history,
    last_window is the co-open set. Not synced across PCs (per-dataset tab
    contents in session.json carry the syncable state).
    """
    return global_state_dir() / "last_window.json"


def reload_manifest_path() -> Path:
    """Return data/llm_state/reload_manifest.json (file may not exist yet).

    Written by hot-reload Tier 3/4 and consumed (then deleted) by the rebuilt
    window / restarted process.
    """
    return global_state_dir() / "reload_manifest.json"


# ----- native resume tokens (PC-local; Issue: chat 復帰の PC 非依存化) -----
#
# claude/pi/codex はネイティブセッションを PC ローカルに持つ (~/.claude,
# ~/.pi/agent/sessions, ~/.codex)。その resume token を同期側の
# chat_sessions/<id>.json に書くと、別 PC で存在しない token を --resume に渡して
# しまう。README の設計原則「ローカルには何も残さない / どの PC でも再開」を守るには、
# 同期させるのは会話履歴 (messages) だけで、token は machine 状態としてここに置く。
# recent_datasets.json (MRU) / last_window.json (workspace) と同じ PC-local 層。


def backend_sessions_path() -> Path:
    """Return data/llm_state/backend_sessions.json (file may not exist yet).

    ``{chat session id: {"engine": str|None, "backend": str|None, "token": str}}``
    ``engine`` はエンジン ID (claude-vscode / claude-cli / pi / codex / ...)。
    ``backend`` 名だけでは claude-vscode と claude-cli を区別できない
    (ClaudeCodeBackend.name は両者で "claude-code") ため、両方を持つ。
    """
    return global_state_dir() / "backend_sessions.json"


def _read_backend_sessions() -> dict:
    """全エントリを型正規化して返す。never raise; 破損/欠損 → {}。"""
    try:
        data = json.loads(backend_sessions_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for sid, rec in data.items():
        # token が非空 str のエントリだけ生かす。engine/backend は str 以外を捨てる
        # (手編集・旧形式で壊れていても resume に使わないだけで済むように)。
        if not isinstance(sid, str) or not isinstance(rec, dict):
            continue
        token = rec.get("token")
        if not isinstance(token, str) or not token:
            continue
        ts = rec.get("updated")
        out[sid] = {
            "engine": rec["engine"] if isinstance(rec.get("engine"), str) else None,
            "backend": rec["backend"] if isinstance(rec.get("backend"), str) else None,
            "token": token,
            "updated": ts if isinstance(ts, (int, float)) and not isinstance(ts, bool)
            else 0.0,
        }
    return out


def _write_backend_sessions(data: dict) -> None:
    """atomic(temp+replace)・never raise。破損時は書込 skip (兄弟エントリを守る)。"""
    from common.paths import read_json_classified
    try:
        path = backend_sessions_path()
        status, _ = read_json_classified(path)
        if status == "unreadable":
            return                                 # 破損/transient → 巻き添えで潰さない
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except (OSError, ValueError, TypeError):
        pass


def read_backend_session(session_id: str) -> dict | None:
    """``{"engine","backend","token"}`` を返す。無ければ None。never raise。"""
    if not session_id:
        return None
    return _read_backend_sessions().get(session_id)


BACKEND_SESSION_TTL = 90 * 24 * 3600.0     # 90 日


def _prune_expired(data: dict, now: float) -> dict:
    """TTL 超過のエントリを落とす。

    セッション ID 突合で孤児を掃除することは**できない**: 開いていないデータセットの
    チャットは self._sessions に載らないので、「今 GUI に無い ID」を消すと閉じている
    データセットの token を巻き添えにする。代わりに年齢で切る — ネイティブセッション側
    (~/.claude 等) も履歴をローテートするので、古い token はどのみち解決できない。
    """
    return {
        k: v for k, v in data.items()
        if now - float(v.get("updated") or 0.0) < BACKEND_SESSION_TTL
    }


def write_backend_session(
    session_id: str, engine: str | None, backend: str | None, token: str | None
) -> None:
    """resume token を記録する。``token`` が空/None ならエントリを削除。never raise。

    書込のたびに TTL 超過分を掃除するので、ストアは放っておいても膨らまない。
    """
    if not session_id:
        return
    now = time.time()
    data = _read_backend_sessions()
    before = len(data)
    data = _prune_expired(data, now)
    if not token:
        dropped = data.pop(session_id, None) is not None
        if not dropped and len(data) == before:
            return                                 # 変化なし → 書かない
    else:
        data[session_id] = {
            "engine": engine, "backend": backend, "token": token, "updated": now,
        }
    _write_backend_sessions(data)


def drop_backend_session(session_id: str) -> None:
    """1 セッション分の token を捨てる (失敗ターン後・セッション削除・エンジン変更時)。

    このストアは ChatSession の side record なので、``draft`` のように「セッションを
    消せば一緒に消える」保証が無い。削除経路から明示的に呼ぶこと。
    """
    write_backend_session(session_id, None, None, None)
