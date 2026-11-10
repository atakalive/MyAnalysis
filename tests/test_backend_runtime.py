"""All callers share the private command and preserve explicit overrides."""
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_backend import runtime


def managed(engine):
    root = runtime.engine_dir(engine)
    release = root / "releases" / "1.2.3-test"
    release.mkdir(parents=True)
    if engine == "pi":
        paths = ["runtime/node.exe" if os.name == "nt" else "runtime/bin/node", "agent/cli.js"]
    else:
        paths = [engine + (".exe" if os.name == "nt" else "")]
    for name in paths:
        p = release / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"stub")
        p.chmod(0o755)
    (root / "current.json").write_text(json.dumps({"release": "releases/1.2.3-test", "version": "1.2.3", "argv": paths}))
    return tuple(str(release / p) for p in paths)


@pytest.mark.parametrize("engine", ["claude", "codex", "pi"])
def test_discovery_uses_current_release_without_a_path_entry(engine):
    command = managed(engine)
    assert runtime.managed_command(engine) == command
    assert runtime.managed_binary(engine) == command[-1]


def test_pi_expands_once_and_preserves_multiline_arguments():
    node, entry = managed("pi")
    argv = [entry, "--append-system-prompt", "first\nsecond", "--session", "session-id"]
    expected = [node, *argv]
    assert runtime.expand_command(argv) == expected
    assert runtime.expand_command(expected) == expected
    assert runtime.environment(expected)["PATH"].split(os.pathsep)[0] == str(Path(node).parent)


def test_pi_running_command_keeps_its_original_node_after_update():
    node, entry = managed("pi")
    root = runtime.engine_dir("pi")
    pointer = root / "current.json"
    data = json.loads(pointer.read_text())
    shutil.copytree(root / data["release"], root / "releases" / "2.0.0-next")
    data["release"] = "releases/2.0.0-next"
    pointer.write_text(json.dumps(data))
    assert runtime.managed_binary("pi") != entry
    assert runtime.expand_command([entry, "--version"]) == [node, entry, "--version"]


def test_custom_pi_does_not_silently_use_the_managed_copy(monkeypatch):
    managed("pi")
    monkeypatch.setattr(runtime.shutil, "which", lambda command: None)
    assert runtime.which_pi("custom-pi") is None
    assert runtime.which_pi() == runtime.managed_binary("pi")


def test_codex_managed_copy_precedes_path_and_extension(monkeypatch):
    from llm_backend.codex import CodexBackend
    managed("codex")
    monkeypatch.setattr("llm_backend.codex._path_binary", lambda name: "/external/codex")
    monkeypatch.delenv("CODEX_BIN", raising=False)
    assert CodexBackend()._discover_binary_info().source == "managed"
    assert CodexBackend({"bin": "/explicit/codex"})._discover_binary() == "/explicit/codex"


def test_claude_explicit_cli_and_default_can_use_managed_copy(monkeypatch):
    from llm_backend.claude_code import ClaudeCodeBackend
    path = managed("claude")[0]
    monkeypatch.delenv("CLAUDE_CODE_BIN", raising=False)
    monkeypatch.setattr(ClaudeCodeBackend, "_host_platform_suffix", staticmethod(lambda: "nonexistent"))
    assert ClaudeCodeBackend()._discover_binary() == path
    assert ClaudeCodeBackend({"bin": "claude"})._discover_binary() == path
    assert ClaudeCodeBackend({"bin": "/custom/claude"})._discover_binary() == "/custom/claude"


@pytest.mark.parametrize("platform_name,machine,target", [
    ("win32", "AMD64", "win32-x64"), ("win32", "ARM64", "win32-arm64"),
    ("darwin", "arm64", "darwin-arm64"), ("linux", "x86_64", "linux-x64"),
    ("freebsd", "amd64", None), ("linux", "riscv64", None),
])
def test_distribution_target_mapping(monkeypatch, platform_name, machine, target):
    monkeypatch.setattr(runtime, "sys", SimpleNamespace(platform=platform_name))
    monkeypatch.setattr(runtime.platform, "machine", lambda: machine)
    monkeypatch.setattr(runtime.platform, "libc_ver", lambda: ("glibc", "2.31"))
    assert runtime.host_target() == target


def test_windowless_python_uses_its_console_sibling_for_install_logs(tmp_path, monkeypatch):
    import sys
    console = tmp_path / "python.exe"
    console.touch()
    monkeypatch.setattr(runtime, "sys", SimpleNamespace(
        platform=sys.platform, executable=str(tmp_path / "pythonw.exe")))
    command = runtime.install_command("claude-cli")
    assert command[0] == str(console)
    assert command[1:] == ("-u", "-m", "llm_backend.install", "claude")


def test_private_node_does_not_inherit_other_node_setup(monkeypatch):
    command = managed("pi")
    monkeypatch.setenv("NODE_OPTIONS", "--require outside.js")
    monkeypatch.setenv("NODE_PATH", "/outside/modules")
    env = runtime.environment(command)
    assert "NODE_OPTIONS" not in env and "NODE_PATH" not in env
    external = runtime.environment(["external-pi"])
    assert external["NODE_OPTIONS"] == "--require outside.js"


def test_musl_distribution_support_is_engine_specific(monkeypatch):
    monkeypatch.setattr(runtime, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(runtime.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(runtime.platform, "libc_ver", lambda: ("musl", "1.2"))
    assert runtime.host_target() == "linux-x64-musl"
    assert runtime.supported("claude") and runtime.supported("codex")
    assert not runtime.supported("pi")
