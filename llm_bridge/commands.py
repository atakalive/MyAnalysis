"""Command queue (LLM → GUI). All tiers share data/llm_state/commands/."""
import json
import logging
import os
import re
import sys
import time
import traceback
import uuid
from datetime import datetime
from common.filelock import exclusive_lock
from common.paths import atomic_write_text
from llm_bridge.paths import (
    commands_queue_dir, command_log_path, command_results_dir,
    rotated_command_log_path,
)

_log = logging.getLogger(__name__)

# 結果に招待の秘密（トークン・#token= リンク）を含む window verb。
_SECRET_RESULT_VERBS: frozenset[str] = frozenset(
    {"meeting-start", "meeting-token", "meeting-lan-link"}
)
_REDACTED = "<redacted>"
_ID_RE = re.compile(r"[0-9a-f]{32}")   # submit() の uuid4().hex
_RESULT_MAX_AGE_SEC = 3600.0
_LOG_ROTATE_BYTES = 1 << 20   # 1 MiB。追記の前に判定するので各世代は「1 MiB 未満 + 最後の 1 エントリ」まで育つ


def submit(tier: str, target: str | None, verb: str, args: dict) -> str:
    """Write a command JSON to the queue atomically. Returns the command id."""
    if tier not in ("window", "tab"):
        raise ValueError(f"unknown tier: {tier!r}")
    now = datetime.now()
    cmd_id = uuid.uuid4().hex
    ts = now.isoformat(timespec="milliseconds")
    payload = {
        "id": cmd_id,
        "ts": ts,
        "tier": tier,
        "target": target,
        "verb": verb,
        "args": args,
    }
    qd = commands_queue_dir()
    fname = f"{now:%Y%m%d_%H%M%S_%f}_{cmd_id[:8]}.json"
    target_path = qd / fname
    tmp = target_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target_path)
    return cmd_id


def _log_sig(path) -> tuple[int, int, int, int] | None:
    """path の世代シグネチャ。無い・読めないときは None。
    .1 は追記されず回転で丸ごと置き換わるだけなので、これが変われば回転が起きている。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _scan_from(path, start: int, cmd_id: str) -> tuple[dict | None, int]:
    """path の start 以降の「改行で終わる行」だけを読み、id == cmd_id のエントリを探す。
    返り値は (見つかったエントリ or None, 次に読む位置)。ファイルが無い・読めないときは
    (None, start)。開いたファイルの大きさが start より小さければ 0 から読む。"""
    try:
        with open(path, "rb") as f:
            if os.fstat(f.fileno()).st_size < start:
                start = 0
            f.seek(start)
            chunk = f.read()
    except OSError:
        return None, start
    last_nl = chunk.rfind(b"\n")
    if last_nl == -1:
        return None, start
    for line in chunk[: last_nl + 1].decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("id") == cmd_id:
            return entry, start + last_nl + 1
    return None, start + last_nl + 1


def _resolve_result(entry: dict, cmd_id: str) -> dict:
    """秘密の結果（result_redacted is True）なら results/<id>.json から本体を戻し、
    ファイルを消す。results/ を用意できない・読めない・壊れている・id が不正なら
    entry をそのまま返す（wait_for の呼び出し元へ例外を出さない）。"""
    if entry.get("result_redacted") is not True:
        return entry
    if not isinstance(cmd_id, str) or not _ID_RE.fullmatch(cmd_id):
        return entry
    try:
        # command_results_dir() は mkdir するので OSError を投げ得る。読み取りと同じ try に入れる
        p = command_results_dir() / f"{cmd_id}.json"
        result = json.loads(p.read_text(encoding="utf-8"))["result"]
    except (OSError, ValueError, KeyError, TypeError):
        return entry
    entry["result"] = result
    entry.pop("result_redacted")
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass
    return entry


def wait_for(cmd_id: str, timeout: float = 30.0, poll: float = 0.1) -> dict | None:
    """Block until command_log.jsonl contains an entry with id=cmd_id, or timeout.

    Returns the log entry or None on timeout. Tracks byte offset across polls so
    that long log files don't get re-scanned from the start each iteration.
    Only advances the offset past complete lines (terminated by newline) to avoid
    losing partially-written entries.

    Rotation: _append_log moves the log to command_log.jsonl.1 once it reaches
    _LOG_ROTATE_BYTES. Rotation is detected by a change of the .1 generation
    signature (not by the main log shrinking); on detection .1 is scanned from
    the start and the new main log is re-read from offset 0. .1 is also scanned
    once on the first poll (rotation between submit and the first read).
    Guarantee: if the log rotates twice between two consecutive polls (>= 1 MiB
    written in one poll interval), the generation holding the entry may be gone
    and this returns None on timeout — it never returns a wrong entry.

    Secret results (meeting-* verbs) are logged as "<redacted>" with
    result_redacted=True; the real result is read back from
    data/llm_state/results/<id>.json (then deleted) and returned in `result`.

    Note: a stale entry (GUI was offline when submitted) is a valid completion
    — callers must check `status` on the returned dict.
    """
    log = command_log_path()
    old = rotated_command_log_path()
    deadline = time.monotonic() + timeout
    offset = 0
    old_sig = _log_sig(old)
    first = True
    while time.monotonic() < deadline:
        sig = _log_sig(old)
        if sig != old_sig:
            # 回転が起きた。さっきまで読んでいた本体は .1 になった（2 回回っていれば消えた）。
            # .1 を頭から探し、新しい本体は 0 から読み直す。
            old_sig = sig
            offset = 0
            entry, _ = _scan_from(old, 0, cmd_id)
            if entry is not None:
                return _resolve_result(entry, cmd_id)
        entry, offset = _scan_from(log, offset, cmd_id)
        if entry is not None:
            return _resolve_result(entry, cmd_id)
        if first:
            # submit から最初の読み取りまでの間に回っていた場合に備え、.1 も 1 回だけ見る
            first = False
            entry, _ = _scan_from(old, 0, cmd_id)
            if entry is not None:
                return _resolve_result(entry, cmd_id)
        time.sleep(poll)
    return None


# ---- GUI-side dispatcher ----

def _find_tab(window, name: str, dataset: str | None = None):
    """Find a tab by (name, dataset) and focus it, using only public ToolWindow API.

    Returns the tab, or None if none matches. On an ambiguous bare name (same
    name in multiple datasets, no dataset= given), window.find_tab raises
    LookupError, which propagates to the caller as an error result.

    By design this focuses the target tab (via set_active_tab) before returning.
    Routing a command to a tab and surfacing that tab to the user are the same
    user-facing operation, so coupling them is intentional, not accidental. The
    focus carries the RESOLVED dataset so a same-named tab in the active dataset
    is never surfaced instead.
    """
    finder = getattr(window, "find_tab", None)
    if finder is not None:
        tab = finder(name, dataset)
        if tab is None:
            return None
        resolved_ds = dataset
        if resolved_ds is None:
            spec = getattr(tab, "session_spec", None) or {}
            resolved_ds = spec.get("dataset")
        try:
            window.set_active_tab(name, dataset=resolved_ds)
        except TypeError:
            window.set_active_tab(name)
        return tab
    # Legacy fallback (windows without the group model).
    if not window.set_active_tab(name):
        return None
    return window.active_tab()


def _append_log(entry: dict) -> None:
    """Append a single JSON line to command_log.jsonl under advisory lock.

    The lock serialises concurrent writers (e.g. multiple GUI callbacks or
    NFS where O_APPEND atomicity is not guaranteed).
    """
    log = command_log_path()
    lock_path = log.with_suffix(".jsonl.lock")
    with exclusive_lock(lock_path):
        _rotate_if_needed(log)
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _rotate_if_needed(log) -> None:
    """ロック内で呼ぶ。本体が _LOG_ROTATE_BYTES 以上なら .1 に回す（.1 は上書き）。
    失敗（無い・Windows で他プロセスが開いている等）は今回は回さず、次の追記で再試行する。"""
    try:
        if log.stat().st_size < _LOG_ROTATE_BYTES:
            return
        os.replace(log, rotated_command_log_path())
    except OSError:
        return


def _prune_results() -> None:
    """results/ の *.json のうち _RESULT_MAX_AGE_SEC より古いものを消す。never raise。"""
    try:
        d = command_results_dir()
        cutoff = time.time() - _RESULT_MAX_AGE_SEC
        files = list(d.glob("*.json"))
    except OSError:
        return
    for f in files:
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
        except OSError:
            continue


def _write_result(cmd_id: str, result) -> None:
    """秘密を含む結果を results/<id>.json に置く（wait_for が読んで消す）。"""
    _prune_results()
    atomic_write_text(
        command_results_dir() / f"{cmd_id}.json",
        json.dumps({"result": result}, ensure_ascii=False),
    )


def _execute(window, payload: dict) -> None:
    tier = payload.get("tier")
    verb = payload.get("verb")
    # Copy so the tab-tier `args.pop("dataset", ...)` below mutates a local dict,
    # not payload["args"] (which the audit log re-reads at the end; a pop there
    # would drop `dataset` from the logged args).
    args = dict(payload.get("args") or {})
    target = payload.get("target")
    status = "ok"
    error = None
    result_repr = None
    tb = None
    try:
        if tier == "window":
            dispatcher = window
        elif tier == "tab":
            # `dataset=` is an optional ambiguity resolver for the tab address —
            # pop it so it is not forwarded to the tab verb (snapshot/set-split
            # etc. take no dataset). Non-tab verbs keep their args intact.
            ds = args.pop("dataset", None)
            tab = _find_tab(window, target, ds) if target else None
            if tab is None:
                raise LookupError(f"no tab named {target!r}")
            dispatcher = tab
        else:
            raise ValueError(f"unknown tier: {tier!r}")
        if not dispatcher.has_command(verb):
            status = "rejected"
            error = f"unknown verb: {verb!r}"
        else:
            # Expose the executing command id so the `reload` verb (Tier 3/4)
            # can correlate its deferred `reload-result` log entry with the
            # command the agent is --wait'ing on.
            setattr(window, "_active_command_id", payload.get("id"))
            result = dispatcher.dispatch_command(verb, **args)
            result_repr = _summarize(result)
    except Exception as e:
        status = "error"
        error = f"{type(e).__name__}: {e}"
        tb = traceback.format_exc(limit=-10)
        _log.warning("command %s %s failed", tier, verb, exc_info=True)
    extra: dict = {}
    if tier == "window" and verb in _SECRET_RESULT_VERBS and status == "ok":
        # 別ファイルの書き込みはログ追記より前（ログの行が見えた時点でファイルが在る）。
        # 書けなければ結果は捨てる — 秘密をログに書くより CLI に届かない方を選ぶ。
        cmd_id = payload.get("id")
        redacted_to_file = False
        if isinstance(cmd_id, str) and _ID_RE.fullmatch(cmd_id):
            try:
                _write_result(cmd_id, result_repr)
                redacted_to_file = True
            except OSError:
                redacted_to_file = False
        result_repr = _REDACTED
        extra["result_redacted"] = redacted_to_file
    _append_log({
        **{k: payload.get(k) for k in ("id", "ts", "tier", "target", "verb", "args")},
        "status": status,
        "error": error,
        "result": result_repr,
        **extra,
        "traceback": tb,
        "completed_at": datetime.now().isoformat(timespec="milliseconds"),
    })


def _summarize(value) -> object:
    """Reduce arbitrary return values to a JSON-safe summary.

    JSON-encodable values pass through unchanged; everything else gets repr'd.
    Lists/dicts of primitives stay as real arrays/objects in the log (not
    stringified); non-trivial objects (widgets, numpy arrays, ...) become strings.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def start_watcher(window, *, resume_after: float | None = None) -> object:
    """Watch commands_queue_dir, dispatch each new file, then delete it.

    Returns the QFileSystemWatcher (caller must hold reference).

    Startup behavior: pre-existing committed queue files (`*.json`) are
    logged as `status: "stale"` and deleted, NOT executed. Orphan
    intermediates (`*.json.tmp`, `*.json.processing`) are silently
    deleted. A previous-session crash that left 50 pending commands does
    not result in 50 commands running on the next launch. See
    `_drain_stale()` below for the per-category rationale.

    ``resume_after`` (wall-clock epoch, ``time.time()`` domain): committed
    `*.json` whose mtime is ``>= resume_after`` are NOT treated as stale —
    they were queued during a Tier 3 rebuild and must still run. After
    `_drain_stale`, an explicit `_drain()` processes them (QFileSystemWatcher
    does not fire directoryChanged for files that already existed).
    """
    from PySide6.QtCore import QFileSystemWatcher
    _prune_results()   # --wait 無しで残った秘密の結果を掃除（GUI 起動時）
    qd = commands_queue_dir()
    watcher = QFileSystemWatcher([str(qd)])

    def _drain():
        for f in sorted(qd.glob("*.json")):
            processing = f.with_suffix(".json.processing")
            try:
                f.replace(processing)
            except OSError:
                continue
            try:
                payload = json.loads(processing.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                parts = f.stem.rsplit("_", 1)
                id_hint = parts[-1] if len(parts) >= 2 else None
                _append_log({
                    "id": id_hint,
                    "ts": datetime.now().isoformat(timespec="milliseconds"),
                    "tier": None, "target": None, "verb": None, "args": None,
                    "status": "malformed",
                    "error": f"could not parse {f.name}: {e!r}",
                    "result": None,
                    "completed_at": datetime.now().isoformat(timespec="milliseconds"),
                })
            else:
                _execute(window, payload)
            try:
                processing.unlink()
            except OSError as e:
                print(f"warning: could not remove {processing}: {e}", file=sys.stderr)

    def _drain_stale():
        """On startup: log pre-existing committed queue items as stale, delete them.

        Three orphan categories handled separately:
        - *.json (committed but unprocessed): log `status: "stale"` then delete.
          The caller may have been --wait'ing on these.
        - *.json.processing (previous GUI crashed mid-drain): always delete.
          Whether the handler ran before the crash is undetermined — if it did,
          there's already a status:ok/error entry from that session.
        - *.json.tmp: skip if mtime < 5 seconds ago (may be an in-progress
          CLI submit between write_text and replace). Delete if older.
        """
        for f in sorted(qd.glob("*.json")):
            if resume_after is not None:
                try:
                    if f.stat().st_mtime >= resume_after:
                        continue  # queued during a rebuild — keep for _drain()
                except OSError:
                    pass
            try:
                payload = json.loads(f.read_text(encoding="utf-8"))
                base = {k: payload.get(k) for k in ("id", "ts", "tier", "target", "verb", "args")}
            except (OSError, json.JSONDecodeError):
                parts = f.stem.rsplit("_", 1)
                id_hint = parts[-1] if len(parts) >= 2 else None
                base = {"id": id_hint, "ts": None, "tier": None,
                        "target": None, "verb": None, "args": None}
            _append_log({
                **base,
                "status": "stale",
                "error": "GUI was not running when this command was queued",
                "result": None,
                "completed_at": datetime.now().isoformat(timespec="milliseconds"),
            })
            try:
                f.unlink()
            except OSError as e:
                print(f"warning: could not remove stale {f}: {e}", file=sys.stderr)
        for f in list(qd.glob("*.json.processing")):
            try:
                f.unlink()
            except OSError as e:
                print(f"warning: could not remove orphan {f}: {e}", file=sys.stderr)
        now = time.time()
        for f in list(qd.glob("*.json.tmp")):
            try:
                if now - f.stat().st_mtime < 5:
                    continue
                f.unlink()
            except OSError as e:
                print(f"warning: could not remove orphan {f}: {e}", file=sys.stderr)

    watcher.directoryChanged.connect(lambda _: _drain())
    _drain_stale()
    if resume_after is not None:
        # Files skipped by _drain_stale (queued during the rebuild) won't trigger
        # directoryChanged since they already exist — process them explicitly.
        _drain()
    return watcher
