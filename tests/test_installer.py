"""Installer contracts: preservation, offline reuse, process supervision and releases."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from installer import build, core


@pytest.fixture(autouse=True)
def isolated_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MYANALYSIS_SETUP_HOME", str(tmp_path / "state"))


def test_official_python_filter():
    info = dict(implementation="cpython", bits=64, version=[3, 12, 4], gil_disabled=False,
                executable="C:/Python312/python.exe", prefix="C:/Python312", base="C:/Python312")
    assert core.supported_python(info)
    assert not core.supported_python(dict(info, bits=32))
    assert not core.supported_python(dict(info, gil_disabled=True))
    assert not core.supported_python(dict(info, version=[3, 10, 9]))
    if sys.platform == "win32":
        assert not core.supported_python(dict(info, base="C:/Program Files/WindowsApps/Python"))


def test_target_priority_and_old_checkout(tmp_path, monkeypatch):
    fresh, old, source, saved = [tmp_path / name for name in ("fresh", "MyAnalysis", "source", "saved")]
    monkeypatch.setattr(core, "default_target", lambda: fresh)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    old.mkdir()
    assert core.select_target() == old
    fresh.mkdir()
    assert core.select_target() == fresh
    (source / ".git").mkdir(parents=True)
    assert core.select_target(source=source) == source
    saved.mkdir()
    core.write_json(core.state_root() / "selection.json", {"target": str(saved)})
    assert core.select_target(source=source) == saved
    assert core.select_target(tmp_path / "explicit") == tmp_path / "explicit"


def test_mutation_lock_cross_process_and_stale_file(tmp_path):
    target = tmp_path / "app"
    code = "from installer.core import mutation_lock; import sys\ntry:\n with mutation_lock(sys.argv[1]): pass\nexcept Exception:\n sys.exit(7)"
    with core.mutation_lock(target):
        result = subprocess.run([sys.executable, "-c", code, str(target)], timeout=10)
        assert result.returncode == 7
    assert subprocess.run([sys.executable, "-c", code, str(target)], timeout=10).returncode == 0


def test_process_identity_does_not_confuse_reused_pid():
    stamp = core.process_stamp(os.getpid())
    assert stamp not in (None, "unknown")
    assert core.alive({"pid": os.getpid(), "stamp": stamp})
    assert not core.alive({"pid": os.getpid(), "stamp": "different creation time"})


def test_streaming_output_bounded_and_spooled():
    text = core.run([sys.executable, "-c", "import sys;sys.stdout.write('x'*2200000);sys.stderr.write('END')"], timeout=10)
    assert len(text.encode()) <= core.MAX_OUTPUT
    assert text.endswith("END")
    assert max(p.stat().st_size for p in (core.state_root() / "children").glob("*.log")) > 2200000
    core.check_children()


def test_command_timeout_stops_child_before_retry():
    with pytest.raises(core.SetupError, match="Timed out"):
        core.run([sys.executable, "-c", "import time;time.sleep(30)"], timeout=.15)
    core.check_children()


def test_os_installer_not_killed_at_monitor_deadline():
    assert core.run([sys.executable, "-c", "import time;time.sleep(.3);print('finished')"], timeout=.05, os_install=True) == "finished"


def test_owned_clone_staging_and_user_directory_preservation(tmp_path, monkeypatch):
    repo = tmp_path / "app"
    repo.mkdir()
    (repo / "datasets.local.json").write_text("private", encoding="utf-8")
    monkeypatch.setattr(core, "capture", lambda *a, **k: pytest.fail("Do not execute Git on invalid nonempty directory"))
    with pytest.raises(core.SetupError, match="preserved"):
        core.install_checkout(repo, "git")
    assert (repo / "datasets.local.json").read_text() == "private"
    new = tmp_path / "other"
    stage = new.with_name(new.name + ".setup-clone")
    stage.mkdir()
    with pytest.raises(core.SetupError, match="Unowned"):
        core.install_checkout(new, "git")
    assert stage.is_dir()


def test_complete_environment_is_offline_and_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(core.ctypes, "CDLL", lambda *a: None)
    repo = tmp_path / "app"
    repo.mkdir()
    python = core.venv_python(repo)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"existing")
    (repo / ".venv/pyvenv.cfg").write_text("home = " + str(tmp_path), encoding="utf-8")
    (repo / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    protected = repo / "datasets.local.json"
    protected.write_bytes(b"private data")
    probe = dict(missing=[], conflicts=[], installed={"numpy": "1.26.4"})
    monkeypatch.setattr(core, "ensure_git", lambda: ("git", "bash"))
    monkeypatch.setattr(core, "install_checkout", lambda *a: "abc")
    monkeypatch.setattr(core, "python_info", lambda *a: dict(executable=str(python), prefix=str(repo / ".venv"), version=[3, 12], implementation="cpython", bits=64))
    monkeypatch.setattr(core, "capture", lambda argv, *a, **k: json.dumps(probe) if core.DEPENDENCY_PROBE in argv else "OK")
    monkeypatch.setattr(core, "run", lambda *a, **k: pytest.fail("Complete environment must not run pip/downloads"))
    monkeypatch.setattr(core, "wait_for_apps", lambda *a: pytest.fail("Read-only verification must work with app open"))
    core.setup(repo)
    assert protected.read_bytes() == b"private data"
    assert python.read_bytes() == b"existing"
    assert core.read_json(repo / "data/setup/runtime.json")["commit"] == "abc"
    monkeypatch.setattr(core, "python_info", lambda *a: dict(prefix=str(tmp_path / "global-python"), version=[3, 12], implementation="cpython", bits=64))
    with pytest.raises(core.SetupError, match="global Python"):
        core.setup(repo)
    assert protected.read_bytes() == b"private data"


def test_dependency_conflict_never_runs_pip(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "capture", lambda *a, **k: json.dumps(dict(conflicts=["numpy>=2"], missing=[], installed={"numpy": "1.26"})))
    monkeypatch.setattr(core, "run", lambda *a, **k: pytest.fail("Must preserve conflicting environment"))
    with pytest.raises(core.SetupError, match="preserved"):
        core.ensure_dependencies(tmp_path, sys.executable)


def test_resolver_may_not_replace_existing_package(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "capture", lambda *a, **k: json.dumps(dict(conflicts=[], missing=["pandas"], installed={"numpy": "1.26.4"})))
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert "--dry-run" in argv
        core.write_json(tmp_path / "data/setup/resolution.json", {"install": [{"metadata": {"name": "numpy", "version": "2.0"}}]})
    monkeypatch.setattr(core, "run", run)
    with pytest.raises(core.SetupError, match="change an existing"):
        core.ensure_dependencies(tmp_path, sys.executable)
    assert len(calls) == 1
    assert (tmp_path / "data/setup/constraints.txt").read_text() == "numpy==1.26.4"


def test_verified_download_cache_is_offline(tmp_path, monkeypatch):
    data = b"official installer"
    cache = core.state_root() / "downloads"
    cache.mkdir(parents=True)
    (cache / "example.exe").write_bytes(data)
    monkeypatch.setattr(core.urllib.request, "urlopen", lambda *a, **k: pytest.fail("Do not download a verified cache again"))
    asset = dict(url="https://example.com/example.exe", size=len(data), sha256=hashlib.sha256(data).hexdigest())
    assert core.fetch(asset).read_bytes() == data


def test_runtime_paths_are_process_local_and_explicit_bash_wins(tmp_path, monkeypatch):
    git, bash = tmp_path / "git.exe", tmp_path / "bash.exe"
    git.touch(); bash.touch()
    core.write_json(tmp_path / "data/setup/runtime.json", dict(git=str(git), bash=str(bash)))
    monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", "explicit")
    monkeypatch.setenv("PATH", "original")
    core.apply_runtime(tmp_path)
    assert os.environ["CLAUDE_CODE_GIT_BASH_PATH"] == "explicit"
    assert os.environ["PATH"].endswith("original")
    assert str(git.parent) in os.environ["PATH"]


def test_generated_entrypoints_embed_exact_core(tmp_path, monkeypatch):
    build.entrypoints(tmp_path)
    bat = (tmp_path / "setup.bat").read_bytes()
    assert bat.isascii()
    assert b"\n" not in bat.replace(b"\r\n", b"")
    ps = base64.b64decode(bat.rsplit(b"::PAYLOAD:", 1)[1]).decode()
    payload = ps.split("FromBase64String('", 1)[1].split("'", 1)[0]
    expected = (build.ROOT / "installer/core.py").read_text(encoding="utf-8").encode("utf-8")
    assert base64.b64decode(payload) == expected
    shell = (tmp_path / "setup.sh").read_text(encoding="utf-8")
    assert base64.b64encode(expected).decode() in shell
    assert "__CORE_BASE64__" not in shell
    assert (build.ROOT / "setup.bat").read_bytes() == bat, "Regenerate release entry points: python -m installer.build"
    assert (build.ROOT / "setup.sh").read_bytes() == (tmp_path / "setup.sh").read_bytes()
    # A fresh Windows checkout can use CRLF for Python/PowerShell sources.
    # Embedding must produce the same release bytes as the Linux checkout.
    source_root = tmp_path / "crlf-source"
    (source_root / "installer").mkdir(parents=True)
    for name in ("core.py", "windows.ps1", "unix.sh"):
        source = (build.ROOT / "installer" / name).read_text(encoding="utf-8")
        (source_root / "installer" / name).write_bytes(source.replace("\n", "\r\n").encode("utf-8"))
    monkeypatch.setattr(build, "ROOT", source_root)
    build.entrypoints(tmp_path / "crlf-output")
    assert (tmp_path / "crlf-output/setup.bat").read_bytes() == bat
    assert (tmp_path / "crlf-output/setup.sh").read_bytes() == (tmp_path / "setup.sh").read_bytes()


def test_worker_exclusion_prevents_update(tmp_path):
    from common import self_update
    with core.mutation_lock(tmp_path):
        result = self_update.run_update(tmp_path)
    assert result.status == "failed"
    assert "Another setup/update" in result.output


def test_pending_process_blocks_mutations():
    record = dict(pid=os.getpid(), stamp=core.process_stamp(os.getpid()), target="pending OS installer")
    core.write_json(core.state_root() / "children/pending.json", record)
    with pytest.raises(core.SetupError, match="still running"):
        core.check_children()


def test_readiness_nonce_must_match_new_process(monkeypatch):
    import llm_bridge
    class Window:
        current_dataset = None
        def open_dataset_names(self):
            return []
    monkeypatch.setenv("MYANALYSIS_STARTUP_TOKEN", "this-launch")
    assert llm_bridge._setup_readiness(Window(), "this-launch") == {"open": [], "active": None}
    with pytest.raises(LookupError):
        llm_bridge._setup_readiness(Window(), "old-launch")
    assert llm_bridge._setup_readiness(Window()) == {"open": [], "active": None}


def test_mac_package_uses_terminal_and_never_runs_user_setup_as_root(tmp_path, monkeypatch):
    def pkgbuild(argv, **kw):
        assert argv[0] == "pkgbuild"
        tree = Path(argv[argv.index("--root")+1])
        import plistlib
        app = tree / "Applications/MyAnalysis.app/Contents"
        assert plistlib.loads((app / "Info.plist").read_bytes())["LSMinimumSystemVersion"] == "14.4"
        assert "Terminal" in (app / "MacOS/MyAnalysis").read_text()
        assert not (tree / "postinstall").exists()
        assert "__CORE_BASE64__" not in (app / "Resources/setup.sh").read_text()
    monkeypatch.setattr(build.subprocess, "run", pkgbuild)
    build.pkg(tmp_path)


def test_worker_death_never_allows_overlap_with_orphan(tmp_path):
    child_script = "import sys;from installer.core import run;run([sys.executable,'-c','import time;time.sleep(60)'])"
    with (tmp_path / "worker.log").open("wb") as output:
        worker = subprocess.Popen([sys.executable, "-c", child_script], stdout=output, stderr=output)
    deadline = time.monotonic() + 10
    records = []
    try:
        while time.monotonic() < deadline:
            records = list((core.state_root() / "children").glob("*.json"))
            if records:
                break
            time.sleep(.05)
        assert records, "worker did not register its child"
        record = core.read_json(records[0])
        worker.kill(); worker.wait(timeout=10)
        if sys.platform == "win32":
            deadline = time.monotonic() + 5
            while core.alive(record) and time.monotonic() < deadline:
                time.sleep(.05)
            assert not core.alive(record), "Windows job must terminate normal children after worker death"
        else:
            with pytest.raises(core.SetupError, match="still running"):
                core.check_children()
            os.killpg(record["pid"], signal.SIGKILL)
    finally:
        if worker.poll() is None:
            worker.kill(); worker.wait(timeout=10)


def test_missing_dependency_installed_from_wheel_without_changing_existing(tmp_path, monkeypatch):
    import importlib.metadata
    import zipfile
    if importlib.util.find_spec("PySide6") is None:
        pytest.skip("Live dependency probe requires installed application dependencies")
    isolated = subprocess.run([sys.executable, "-I", "-c", core.IMPORT_PROBE + "; import ensurepip"], capture_output=True, timeout=120)
    if isolated.returncode:
        pytest.skip("Live venv test requires ensurepip and application packages visible under -I")
    wheels = tmp_path / "index"
    wheels.mkdir()
    wheel = wheels / "insttest_addition-1.0-py3-none-any.whl"
    dist = "insttest_addition-1.0.dist-info/"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("insttest_addition/__init__.py", "value = 1\n")
        archive.writestr(dist + "METADATA", "Metadata-Version: 2.1\nName: insttest-addition\nVersion: 1.0\nRequires-Dist: numpy>=1\n")
        archive.writestr(dist + "WHEEL", "Wheel-Version: 1.0\nGenerator: installer-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(dist + "RECORD", "")
    repo = tmp_path / "app"
    repo.mkdir()
    (repo / "requirements.txt").write_text("numpy\ninsttest-addition\n", encoding="utf-8")
    subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(repo / ".venv")], check=True, timeout=60, capture_output=True)
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", str(wheels))
    monkeypatch.setenv("PIP_CONFIG_FILE", os.devnull)
    before = importlib.metadata.version("numpy")
    installed = core.ensure_dependencies(repo, core.venv_python(repo))
    assert installed["numpy"] == before
    assert installed["insttest-addition"] == "1.0"
    assert core.capture([str(core.venv_python(repo)), "-I", "-c", "import insttest_addition;print(insttest_addition.value)"]) == "1"


def test_cli_authentication_is_separate_and_acknowledgements_are_per_request(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace
    result = tmp_path / "run.json"
    auth = result.with_suffix(".auth.json")
    answer = auth.with_suffix(".answer.json")
    monkeypatch.setenv("MYANALYSIS_SETUP_RESULT", str(result))
    monkeypatch.setenv("MYANALYSIS_SETUP_TTY", "/dev/pts/test")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(core.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    commands, ids = [], []
    monkeypatch.setattr(core, "run", lambda argv, **kw: commands.append(argv))
    def authenticate():
        deadline = time.monotonic() + 5
        while len(ids) < 2 and time.monotonic() < deadline:
            request = core.read_json(auth)
            if request and request["id"] not in ids:
                ids.append(request["id"])
                core.write_json(answer, {"id": request["id"], "ok": True})
            time.sleep(.02)
    thread = threading.Thread(target=authenticate, daemon=True)
    thread.start()
    core.apt_install(["git"])
    core.apt_install(["python3-venv"])
    thread.join(timeout=5)
    assert len(set(ids)) == 2
    assert all(command[:2] == ["sudo", "-n"] for command in commands)
