"""Qt-side hot-reload driver: controller, `reload` verb, 開発 menu, manifest.

Wraps the Qt-free core (``devtools.hotreload``) with the four escalation tiers:

  Tier 1 (patch / default) — in-place superreload of changed repo modules.
  Tier 2 (tab)             — single sandbox build of one analysis.py, then swap.
  Tier 3 (app)             — blue-green: flush state, purge repo modules, rebuild
                             the ToolWindow in-process from fresh code.
  Tier 4 (restart)         — relaunch the process with --resume-session.

`install_hotreload(window)` registers the `reload` window verb and adds the
開発(&D) menu. The agent edits repo code then runs
`python -m llm_bridge window reload [scope=tab|app|restart] [target=<tab>] --wait`.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import time
import traceback
from datetime import datetime

from PySide6.QtCore import QByteArray, QObject, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from devtools import hotreload


def _m(name: str):
    """Resolve a repo module from sys.modules at call time.

    devtools is excluded from purge_project_modules, so after a Tier 3 rebuild
    top-level import references would be stale. Resolving at call time ensures
    we always use the current (post-rebuild) module objects.
    """
    return sys.modules[name]


# Durable references to live main windows. After a Tier 3 rebuild the old
# main()-scope local still points at the OLD window, so the rebuilt window has
# no Python reference and would be GC'd. This set anchors it. devtools is
# excluded from purge, so the set survives a blue-green rebuild.
_live_windows: set = set()


# ---------------------------------------------------------------------------
# Reload context (passed to module __on_reload__ hooks)
# ---------------------------------------------------------------------------


class ReloadContext:
    def __init__(self, window, old=None):
        self.window = window
        self.old = old


# ---------------------------------------------------------------------------
# View (viewbox / splitter) capture + restore — best-effort, panel duck-typed
# ---------------------------------------------------------------------------


def _capture_view(tab) -> dict:
    out: dict = {"panels": {}, "splitter": None}
    try:
        out["splitter"] = list(tab._splitter.sizes())
    except Exception:
        pass
    for key, panel in getattr(tab, "_panels", {}).items():
        try:
            if hasattr(panel, "_plot"):
                out["panels"][key] = ["plot", panel._plot.getViewBox().viewRange()]
            elif hasattr(panel, "_view") and hasattr(panel._view, "getView"):
                out["panels"][key] = ["image", panel._view.getView().viewRange()]
        except Exception:
            continue
    return out


def _restore_view(tab, view: dict) -> None:
    try:
        sizes = view.get("splitter")
        if sizes:
            tab._splitter.setSizes(sizes)
    except Exception:
        pass
    for key, entry in (view.get("panels") or {}).items():
        panel = getattr(tab, "_panels", {}).get(key)
        if panel is None:
            continue
        try:
            kind, rng = entry
            xr, yr = rng[0], rng[1]
            if kind == "plot":
                panel._plot.getViewBox().setRange(xRange=xr, yRange=yr, padding=0)
            elif kind == "image":
                panel._view.getView().setRange(xRange=xr, yRange=yr, padding=0)
        except Exception:
            continue


# ---------------------------------------------------------------------------
# Manifest (Tier 3/4)
# ---------------------------------------------------------------------------


def _hex(qba: QByteArray) -> str:
    return bytes(qba.toHex().data()).decode("ascii")


def write_manifest(window) -> None:
    """Persist enough UI state to restore a rebuilt/restarted window."""
    datasets: list[str] = []
    for tab in window.tabs():
        spec = getattr(tab, "session_spec", None)
        if spec and spec.get("dataset") and spec["dataset"] not in datasets:
            datasets.append(spec["dataset"])
    active = window.active_tab()
    cw = window.chat_widget()
    # view_state_by_dataset is nested {dataset: {tab_name: view}} keyed by a name
    # distinct from flat view_state so restore disambiguates by KEY, not by an
    # isinstance heuristic (a flat value {panels,splitter} and a nested value
    # {name: view} are both dicts). dataset-less tabs ((empty)/inferred viewer,
    # session_spec=None) are excluded — json would coerce a None key to "null".
    view_by_ds: dict[str, dict] = {}
    for tab in window.tabs():
        spec = getattr(tab, "session_spec", None)
        ds = spec.get("dataset") if isinstance(spec, dict) else None
        if ds is None:
            continue
        view_by_ds.setdefault(ds, {})[tab.name] = _capture_view(tab)
    data = {
        "datasets": datasets,
        "active_tab": active.name if active is not None else None,
        "active_dataset": getattr(window, "current_dataset", None),
        "geometry": _hex(window.saveGeometry()),
        "window_state": _hex(window.saveState()),
        "chat_draft": cw.input_draft() if hasattr(cw, "input_draft") else "",
        "chat_active_id": (
            cw.active_session_id() if hasattr(cw, "active_session_id") else None
        ),
        "view_state_by_dataset": view_by_ds,
    }
    _m("llm_bridge.paths").reload_manifest_path().write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def consume_manifest(window) -> bool:
    """Restore window/tabs/chat from the reload manifest, then delete it.

    Returns True if a manifest was consumed. Absent / corrupt / wrong-typed
    manifest → ordinary startup (no restore). The manifest file is always
    removed so a stale one can't drive an unintended restore on the next plain
    launch.
    """
    path = _m("llm_bridge.paths").reload_manifest_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return False
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        path.unlink(missing_ok=True)
        return False
    if not isinstance(data, dict):
        path.unlink(missing_ok=True)
        return False
    try:
        _restore_from_manifest(window, data)
    finally:
        path.unlink(missing_ok=True)
    return True


def _restore_from_manifest(window, data: dict) -> None:
    geo = data.get("geometry") or ""
    if geo:
        try:
            window.restoreGeometry(QByteArray.fromHex(bytes(geo, "ascii")))
        except Exception:
            pass
    for ds in data.get("datasets", []) or []:
        try:
            window.dispatch_command("open-dataset", name=ds)
        except Exception:
            continue
    ws = data.get("window_state") or ""
    if ws:
        try:
            window.restoreState(QByteArray.fromHex(bytes(ws, "ascii")))
        except Exception:
            pass
    # Bring the saved active dataset group to the front BEFORE focusing its
    # active tab (else set_active_tab can't surface the right group). Uses
    # set_active_dataset (= _select_dataset_group), not note_current_dataset.
    active_ds = data.get("active_dataset")
    if active_ds is not None:
        try:
            window.set_active_dataset(active_ds)
        except Exception:
            pass
    active = data.get("active_tab")
    if active:
        try:
            window.set_active_tab(active, dataset=active_ds)
        except Exception:
            try:
                window.set_active_tab(active)
            except Exception:
                pass
    # view_state: new nested form (keyed by dataset) wins; fall back to the old
    # flat form for a mid-migration manifest. Key name disambiguates — no
    # isinstance guessing.
    view_by_ds = data.get("view_state_by_dataset")
    flat_view = data.get("view_state", {}) or {}
    for tab in window.tabs():
        spec = getattr(tab, "session_spec", None)
        ds = spec.get("dataset") if isinstance(spec, dict) else None
        v = None
        if isinstance(view_by_ds, dict) and ds is not None:
            v = (view_by_ds.get(ds) or {}).get(tab.name)
        if v is None:
            v = flat_view.get(tab.name)
        if v:
            _restore_view(tab, v)
    cw = window.chat_widget()
    if cw is not None:
        draft = data.get("chat_draft") or ""
        if draft and hasattr(cw, "set_input_draft"):
            try:
                cw.set_input_draft(draft)
            except Exception:
                pass
        cid = data.get("chat_active_id")
        if cid and hasattr(cw, "set_active_session_by_id"):
            try:
                cw.set_active_session_by_id(cid)
            except Exception:
                pass
    window.clear_session_dirty()


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class HotReloadController(QObject):
    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self._reloader = hotreload.HotReloader()

    def note_analysis_opened(self, dataset: str, name: str) -> None:
        """解析タブを開いた時点の SHA を clean baseline として登録する。"""
        self._reloader.mark_analysis_clean(dataset, name)

    # -- guards --

    def _busy(self) -> str | None:
        cw = self._window.chat_widget()
        if cw is not None and getattr(cw, "is_busy", lambda: False)():
            return "reload-busy:a chat turn is streaming — retry when idle"
        if QApplication.activeModalWidget() is not None:
            return "reload-busy:a modal dialog is open — close it and retry"
        return None

    # -- Tier 1 --

    def do_reload(self) -> str:
        busy = self._busy()
        if busy:
            return busy
        report = self._reloader.reload(ReloadContext(self._window))
        return report.summary()

    # -- Tier 2 --

    def reload_tab(self, name: str) -> str:
        busy = self._busy()
        if busy:
            return busy
        import dataset_config

        window = self._window
        # Resolve by (name, current dataset) so a same-named analysis in another
        # dataset is neither reloaded nor closed by mistake. find_tab prefers the
        # current dataset and raises on an unresolved ambiguity.
        finder = getattr(window, "find_tab", None)
        old_tab = None
        if finder is not None:
            try:
                old_tab = finder(name, None)
            except LookupError:
                return (
                    f"reload-tab-error:tab {name!r} is open in multiple datasets; "
                    f"switch to the target dataset first"
                )
        if old_tab is None:
            old_tab = next((t for t in window.tabs() if t.name == name), None)
        if old_tab is None:
            return f"reload-tab-error:no open tab named {name!r}"
        old_spec = getattr(old_tab, "session_spec", None) or {}
        # 同名の figure/viewer タブを解析タブとして reload しない（kind 一致を要求）。
        if old_spec.get("kind") != "analysis":
            return f"reload-tab-error:tab {name!r} is not an analysis tab"
        dataset = old_spec.get("dataset") or getattr(old_tab, "dataset", None)
        if not dataset:
            return f"reload-tab-error:cannot resolve dataset for tab {name!r}"

        try:
            af = dataset_config.analysis_file(dataset, name)
        except (ValueError, KeyError, RuntimeError) as e:
            return f"reload-tab-error:{e}"
        if not af.is_file():
            return f"reload-tab-error:no analysis named {name!r}"
        try:
            compile(af.read_text(encoding="utf-8"), str(af), "exec")
        except SyntaxError as e:
            return f"reload-tab-error:syntax {e.filename}:{e.lineno}: {e.msg}"

        captured_state = _m("llm_bridge.state").read(dataset, name)
        view = _capture_view(old_tab)
        sandbox = QWidget()
        try:
            new_tab, mod = _m("llm_bridge")._build_analysis(sandbox, dataset, name)
        except Exception as e:  # noqa: BLE001 — keep old tab on any build failure
            sandbox.deleteLater()
            _m("llm_bridge.state").writer(dataset, name)(captured_state)
            return f"reload-tab-error:build failed: {e!r} (old tab retained)"
        if new_tab.name != name:
            sandbox.deleteLater()
            _m("llm_bridge.state").writer(dataset, name)(captured_state)
            return (
                f"reload-tab-error:tab name mismatch: expected {name!r}, "
                f"got {new_tab.name!r} (old tab retained)"
            )
        if not _m("llm_bridge")._sync_new_tab_state(
            new_tab, mod, dataset, name, captured_state
        ):
            sandbox.deleteLater()
            _m("llm_bridge.state").writer(dataset, name)(captured_state)
            return (
                "reload-tab-error:apply_state / refresh-state failed (old tab retained)"
            )
        # All good — swap the old tab for the new one (dataset-scoped so a
        # same-named tab in another dataset is untouched).
        new_tab.setParent(None)
        sandbox.deleteLater()
        window.close_tab(name, dataset=dataset)
        window.add_tab(new_tab)
        window.set_active_tab(name, dataset=dataset)
        _restore_view(new_tab, view)
        self._reloader.mark_analysis_clean(dataset, name)
        return f"reloaded-tab:{name}"

    # -- Tier 3 --

    def reload_app(self) -> str:
        busy = self._busy()
        if busy:
            return busy
        # Called from inside the command watcher's _drain slot (the watcher we
        # are about to destroy). Defer to a clean event-loop turn and return
        # immediately; correlate the eventual result via the command id.
        cmd_id = getattr(self._window, "_active_command_id", None)
        QTimer.singleShot(0, lambda: self._do_reload_app(cmd_id))
        return "reload-scheduled"

    def _do_reload_app(self, cmd_id) -> None:
        old_window = self._window
        app = QApplication.instance()
        new_window = None
        saved_modules = {
            k: v for k, v in sys.modules.items() if hotreload.is_project_module(k)
        }
        try:
            # 1. syntax guard across all changed modules.
            self._reloader.scan()
            errs: list[str] = []
            for rec in self._reloader.changed_modules():
                try:
                    compile(rec.path.read_text(encoding="utf-8"), str(rec.path), "exec")
                except SyntaxError as e:
                    errs.append(f"{e.filename}:{e.lineno}: {e.msg}")
            if errs:
                self._log_reload_result(
                    cmd_id, "failed", 3, error="syntax: " + "; ".join(errs)
                )
                QMessageBox.critical(
                    old_window,
                    _m("common.i18n").tr("dev.abort.syntax.title"),
                    "\n".join(errs),
                )
                return

            # 2. flush all state (abort if any dataset fails to save).
            _saved, failed = _m("llm_bridge.session").save_all(old_window)
            if failed:
                self._log_reload_result(
                    cmd_id, "failed", 3, error=f"save_all failed: {failed}"
                )
                tr = _m("common.i18n").tr
                QMessageBox.critical(
                    old_window,
                    tr("dev.abort.title"),
                    tr("dev.save_failed.body", datasets=", ".join(failed)),
                )
                return

            # 3. manifest.
            write_manifest(old_window)

            # 4. rebuild epoch (same clock domain as file mtimes; 2s margin).
            rebuild_start_ts = time.time() - 2.0

            # 6. stop the old command watcher (release the directory handle).
            self._teardown_watchers(old_window)
            # Stop the old meeting relay (idempotent) so its QThread/QTimer don't
            # outlive the blue-green rebuild (Issue #42).
            _relay = getattr(old_window, "_meeting_relay", None)
            if _relay is not None:
                _relay.stop()

            # 6b. re-dock any floated tabs before purge (#56). getattr-guarded:
            # the first patch reload has no _close_all_floats on the old window.
            _caf = getattr(old_window, "_close_all_floats", None)
            if _caf:
                _caf()

            # 7. purge repo modules.
            hotreload.purge_project_modules()

            # 8. rebuild from fresh code.
            tool = importlib.import_module("tool")
            new_window = tool.create_main_window(
                app, watcher_resume_after=rebuild_start_ts, resume_session=True
            )
            new_window.show()

            # 9. retire the old window (new window is anchored in _live_windows
            #    via its own install_hotreload call inside create_main_window).
            _live_windows.discard(old_window)
            old_window.hide()
            old_window.deleteLater()

            # 10. success result.
            self._log_reload_result(cmd_id, "ok", 3)
        except Exception:  # noqa: BLE001 — recover the old window on any failure
            tb = traceback.format_exc()
            if new_window is not None:
                try:
                    new_window.hide()
                    new_window.deleteLater()
                except Exception:
                    pass
            hotreload.purge_project_modules()
            sys.modules.update(saved_modules)
            _m("llm_bridge.paths").reload_manifest_path().unlink(missing_ok=True)
            self._recover_old_watcher(old_window)
            # _close_all_floats latched _shutting_down=True; clear it so floats on
            # the recovered old window re-dock normally afterward (#56).
            if getattr(old_window, "_shutting_down", False):
                old_window._shutting_down = False
            try:
                old_window.show()
            except Exception:
                pass
            QMessageBox.critical(
                old_window, _m("common.i18n").tr("dev.rebuild_failed.title"), tb
            )
            self._log_reload_result(cmd_id, "failed", 3, error=tb)

    # -- Tier 4 --

    def restart(self) -> str:
        busy = self._busy()
        if busy:
            return busy
        cmd_id = getattr(self._window, "_active_command_id", None)
        QTimer.singleShot(0, lambda: self._do_restart(cmd_id))
        return "reload-scheduled"

    def _do_restart(self, cmd_id) -> None:
        from common.paths import repo_root

        window = self._window
        _saved, failed = _m("llm_bridge.session").save_all(window)
        if failed:
            self._log_reload_result(
                cmd_id, "failed", 4, error=f"save_all failed: {failed}"
            )
            tr = _m("common.i18n").tr
            QMessageBox.critical(
                window,
                tr("dev.restart_abort.title"),
                tr("dev.save_failed.body", datasets=", ".join(failed)),
            )
            return
        write_manifest(window)
        self._teardown_watchers(window)
        tool_path = str(repo_root() / "tool.py")
        kwargs: dict = {}
        if sys.platform == "win32":
            # windowless 起動（run.bat の pythonw）は再起動も窓なしに保つ。
            # pythonw に CREATE_NEW_CONSOLE を渡すと使われない空コンソールが
            # 残ってしまうため、pythonw 起動時は CREATE_NO_WINDOW を使う。
            # debug 起動（python.exe）は従来どおり新コンソールを開く。
            if sys.executable.lower().endswith("pythonw.exe"):
                flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            else:
                flag = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            kwargs["creationflags"] = flag
        subprocess.Popen([sys.executable, tool_path, "--resume-session"], **kwargs)
        window._close_all_floats()  # re-dock floats (#56); after Popen, before clear_dirty
        window.clear_session_dirty()  # avoid the closeEvent save prompt
        QApplication.quit()

    # -- helpers --

    def _teardown_watchers(self, window) -> None:
        for w in list(getattr(window, "_bridge_watchers", []) or []):
            try:
                w.directoryChanged.disconnect()
            except Exception:
                pass
            try:
                w.removePaths(w.directories())
            except Exception:
                pass
            try:
                w.deleteLater()
            except Exception:
                pass
        window._bridge_watchers = []

    def _recover_old_watcher(self, window) -> None:
        # _teardown_watchers cleared the list (incl. the dead command watcher);
        # regenerate a command watcher so the old window keeps accepting CLI.
        try:
            cmd_watcher = _m("llm_bridge.commands").start_watcher(window)
            window._bridge_watchers = [cmd_watcher]
        except Exception:
            window._bridge_watchers = []

    def _log_reload_result(
        self, cmd_id, status: str, tier: int, error: str | None = None
    ) -> None:
        now = datetime.now().isoformat(timespec="milliseconds")
        entry = {
            "id": cmd_id,
            "ts": now,
            "verb": "reload-result",
            "status": status,
            "tier": tier,
            "target": None,
            "args": {},
            "completed_at": now,
        }
        if error is not None:
            entry["error"] = error
        _m("llm_bridge.commands")._append_log(entry)


# ---------------------------------------------------------------------------
# Install (verb + menu)
# ---------------------------------------------------------------------------


def install_hotreload(window) -> HotReloadController:
    """Register the `reload` verb + 開発 menu. Returns the controller.

    The controller is parented to ``window`` so it lives as long as the window
    (and isn't GC'd through loss of the Python reference).
    """
    controller = HotReloadController(window)
    _live_windows.add(window)

    def _reload(scope: str = "patch", target: str | None = None) -> str:
        if scope == "patch":
            return controller.do_reload()
        if scope == "tab":
            if not target:
                return "reload-error:scope=tab requires target=<tab name>"
            return controller.reload_tab(target)
        if scope == "app":
            return controller.reload_app()
        if scope == "restart":
            return controller.restart()
        return f"reload-error:unknown scope {scope!r} (use patch|tab|app|restart)"

    window.register_command("reload", _reload)
    _install_menu(window, controller)
    return controller


def _install_menu(window, controller: HotReloadController) -> None:
    # tr resolved at call time: common.i18n is purged on a Tier 3 rebuild (it
    # holds mutable _active/_catalogs) while this module persists, so a module-
    # level `from common.i18n import tr` would go stale. _m() always returns the
    # current module — and `tr(...)` stays a Name call so the catalog test sees
    # the keys.
    tr = _m("common.i18n").tr
    menu = window.menuBar().addMenu(tr("menu.dev"))
    a_patch = menu.addAction(tr("menu.dev.reload"))
    a_patch.setShortcut(QKeySequence("Ctrl+F5"))
    a_patch.triggered.connect(lambda: _menu_patch(window, controller))
    a_app = menu.addAction(tr("menu.dev.rebuild"))
    a_app.triggered.connect(controller.reload_app)
    a_restart = menu.addAction(tr("menu.dev.restart"))
    a_restart.triggered.connect(controller.restart)

    def _retranslate() -> None:
        tr = _m("common.i18n").tr
        menu.setTitle(tr("menu.dev"))
        a_patch.setText(tr("menu.dev.reload"))
        a_app.setText(tr("menu.dev.rebuild"))
        a_restart.setText(tr("menu.dev.restart"))

    if hasattr(window, "register_retranslate_hook"):
        window.register_retranslate_hook(_retranslate)


def _menu_patch(window, controller: HotReloadController) -> None:
    msg = controller.do_reload()
    first = msg.splitlines()[0] if msg else "done"
    window.statusBar().showMessage(f"reload: {first}", 5000)
    if "scope=app recommended" in msg or "SYNTAX ERROR" in msg or "FAILED" in msg:
        QMessageBox.information(window, _m("common.i18n").tr("dev.reload.title"), msg)
