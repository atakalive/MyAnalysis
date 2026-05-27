"""Command queue (LLM → GUI). All tiers share data/llm_state/commands/."""
import fcntl
import json
import sys
import time
import uuid
from datetime import datetime
from llm_bridge.paths import commands_queue_dir, command_log_path


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


def wait_for(cmd_id: str, timeout: float = 30.0, poll: float = 0.1) -> dict | None:
    """Block until command_log.jsonl contains an entry with id=cmd_id, or timeout.

    Returns the log entry or None on timeout. Tracks byte offset across polls so
    that long log files don't get re-scanned from the start each iteration.
    Only advances the offset past complete lines (terminated by newline) to avoid
    losing partially-written entries.
    Note: a stale entry (GUI was offline when submitted) is a valid completion
    — callers must check `status` on the returned dict.
    """
    log = command_log_path()
    deadline = time.monotonic() + timeout
    offset = 0
    while time.monotonic() < deadline:
        if log.exists():
            try:
                with open(log, "rb") as f:
                    f.seek(offset)
                    chunk = f.read()
                last_nl = chunk.rfind(b"\n")
                if last_nl == -1:
                    time.sleep(poll)
                    continue
                complete = chunk[:last_nl + 1]
                offset += last_nl + 1
                for line in complete.decode("utf-8", errors="replace").splitlines():
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("id") == cmd_id:
                        return entry
            except OSError:
                pass
        time.sleep(poll)
    return None


# ---- GUI-side dispatcher ----

def _find_tab(window, name: str):
    """Find a tab by name and focus it, using only public ToolWindow API.

    Returns the AnalysisTab, or None if no tab with that name exists.

    By design this focuses the target tab (via set_active_tab) before returning.
    Routing a command to a tab and surfacing that tab to the user are the same
    user-facing operation, so coupling them is intentional, not accidental.
    """
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
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            with open(log, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _execute(window, payload: dict) -> None:
    tier = payload.get("tier")
    verb = payload.get("verb")
    args = payload.get("args") or {}
    target = payload.get("target")
    status = "ok"
    error = None
    result_repr = None
    try:
        if tier == "window":
            dispatcher = window
        elif tier == "tab":
            tab = _find_tab(window, target) if target else None
            if tab is None:
                raise LookupError(f"no tab named {target!r}")
            dispatcher = tab
        else:
            raise ValueError(f"unknown tier: {tier!r}")
        # Separate verb lookup from handler execution. dispatch_command does
        # `self._command_handlers[verb](**kwargs)` so a missing verb and a
        # handler-internal KeyError are indistinguishable. Check existence
        # first via _command_handlers to classify cleanly.
        if verb not in dispatcher._command_handlers:
            status = "rejected"
            error = f"unknown verb: {verb!r}"
        else:
            result = dispatcher.dispatch_command(verb, **args)
            result_repr = _summarize(result)
    except Exception as e:
        status = "error"
        error = repr(e)
    _append_log({
        **{k: payload.get(k) for k in ("id", "ts", "tier", "target", "verb", "args")},
        "status": status,
        "error": error,
        "result": result_repr,
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


def start_watcher(window) -> object:
    """Watch commands_queue_dir, dispatch each new file, then delete it.

    Returns the QFileSystemWatcher (caller must hold reference).

    Startup behavior: pre-existing committed queue files (`*.json`) are
    logged as `status: "stale"` and deleted, NOT executed. Orphan
    intermediates (`*.json.tmp`, `*.json.processing`) are silently
    deleted. A previous-session crash that left 50 pending commands does
    not result in 50 commands running on the next launch. See
    `_drain_stale()` below for the per-category rationale.
    """
    from PySide6.QtCore import QFileSystemWatcher
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
    return watcher
