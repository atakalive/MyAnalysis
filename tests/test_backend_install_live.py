"""Opt-in Windows smoke tests. Downloads official artifacts, never sends a turn.

Set MYANALYSIS_TEST_BACKEND_DOWNLOADS=1 to run. All writable state is isolated.
"""
import os
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("MYANALYSIS_TEST_BACKEND_DOWNLOADS") != "1",
    reason="opt-in Windows artifact installation test",
)


@pytest.mark.parametrize("engine", ["claude-cli", "codex", "pi"])
def test_real_install_via_gui_without_system_node(tmp_path, monkeypatch, engine):
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from common.i18n import tr
    from gui.backend_status_window import BackendStatusWindow
    from llm_backend import model_catalog, preflight, runtime
    import llm_backend

    home = tmp_path / "isolated user"
    home.mkdir()
    root = home / "backend install 日本語"
    monkeypatch.setenv(runtime.ROOT_ENV, str(root))
    for name in ("HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / "codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "claude"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(home / "pi"))
    # A system Node setup must not influence the application's private Node.
    monkeypatch.setenv("NODE_OPTIONS", "--require nonexistent-system-node-module.js")
    monkeypatch.setenv("NODE_PATH", str(home / "outside-node-modules"))
    for key in list(os.environ):
        if key.endswith(("API_KEY", "AUTH_TOKEN", "OAUTH_TOKEN")) or key in ("CLAUDE_CODE_BIN", "CODEX_BIN"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PATH", str(Path(os.environ["SystemRoot"]) / "System32"))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setattr(llm_backend, "backend_config", lambda: {})
    monkeypatch.setattr(model_catalog, "fetch_models", lambda eid: model_catalog.ModelList(eid, "missing"))
    app = QApplication.instance() or QApplication([])
    window = BackendStatusWindow(MagicMock())

    def wait_idle(seconds):
        loop = QEventLoop()
        timer = QTimer()
        timer.timeout.connect(lambda: loop.quit() if not window._busy() else None)
        timer.start(25)
        QTimer.singleShot(int(seconds * 1000), loop.quit)
        if window._busy():
            loop.exec()
        timer.stop()
        assert not window._busy(), window._log.toPlainText()[-5000:]

    try:
        wait_idle(90)
        assert window._status[engine].binary_state == "missing"
        button = window._rows[engine]["action"]
        assert not button.isHidden() and button.isEnabled()
        assert button.text() == tr("backend.status.btn.install")
        button.click()
        wait_idle(900)
        assert window._status[engine].binary_state == "ok", window._log.toPlainText()[-5000:]
        assert window._status[engine].version
        assert window._status[engine].login
        assert window._rows[engine]["action"].text() == tr("backend.status.btn.update")
        assert runtime.managed_command(engine)
        command = runtime.expand_command([window._status[engine].binary])
        help_args = ["exec", "--help"] if engine == "codex" else ["--help"]
        help_result = subprocess.run(
            [*command, *help_args], env=runtime.environment(command),
            stdin=subprocess.DEVNULL, capture_output=True, timeout=45,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert help_result.returncode == 0, help_result.stderr.decode("utf-8", "replace")
        required_flag = {"pi": "--no-context-files", "codex": "--json", "claude-cli": "--input-format"}[engine]
        assert required_flag in help_result.stdout.decode("utf-8", "replace")

        # A real pythonw process exercises console-free startup and probe workers.
        result_path = tmp_path / "pythonw-result.json"
        program = """
import json, sys
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import llm_backend
llm_backend.backend_config = lambda: {}
from gui.backend_status_window import BackendStatusWindow
app = QApplication([])
window = BackendStatusWindow(SimpleNamespace())
def finish():
    if window._probe is not None:
        return
    state = window._status[sys.argv[1]]
    Path(sys.argv[2]).write_text(json.dumps({
        'state': state.binary_state, 'version': state.version,
        'installer': state.install, 'login': state.login,
    }), encoding='utf-8')
    window.close()
    app.quit()
timer = QTimer()
timer.timeout.connect(finish)
timer.start(25)
QTimer.singleShot(60000, app.quit)
app.exec()
"""
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        result = subprocess.run(
            [str(pythonw), "-c", program, engine, str(result_path)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=75,
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        observed = json.loads(result_path.read_text(encoding="utf-8"))
        assert observed["state"] == "ok" and observed["version"]
        assert observed["login"]
        assert Path(observed["installer"][0]).name.lower() == "python.exe"
        print(window._log.toPlainText())
    except Exception:
        print(window._log.toPlainText(), flush=True)
        print({name: getattr(window, name) for name in (
            "_probe", "_cmd", "_models", "_install_eid", "_models_after_install",
            "_models_after_install_seen", "_install_probe_done")}, flush=True)
        raise
    finally:
        window.close()
        app.processEvents()
