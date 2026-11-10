"""Codex acquisition paths, isolated from installed editors and commands."""
from types import SimpleNamespace

import pytest

from llm_backend import codex
from llm_backend.codex import CodexBackend


@pytest.fixture
def discovery(monkeypatch, tmp_path):
    monkeypatch.setattr(codex.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(codex, "_bundled_target", lambda: "linux-x86_64")
    monkeypatch.setattr(codex.shutil, "which", lambda name: None)
    monkeypatch.delenv("CODEX_BIN", raising=False)
    return tmp_path


def install(home, version="26.9.1", editor=".vscode", target="linux-x86_64"):
    filename = "codex.exe" if target.startswith("windows-") else "codex"
    binary = home / editor / "extensions" / (
        f"openai.chatgpt-{version}-win32-x64"
    ) / "bin" / target / filename
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"stub")
    binary.chmod(0o755)
    return binary


@pytest.mark.parametrize("editor", [
    ".vscode", ".vscode-insiders", ".vscode-server", ".cursor", ".windsurf",
])
def test_extension_only_needs_no_path_command(discovery, editor):
    binary = install(discovery, editor=editor)
    info = CodexBackend()._discover_binary_info()
    assert info.path == str(binary)
    assert info.source == "vscode"


def test_versions_sort_numerically_across_editors(discovery):
    install(discovery, "26.9.999")
    binary = install(discovery, "26.10.2", editor=".cursor")
    assert CodexBackend()._discover_binary() == str(binary)


def test_host_target_uses_binary_directory_not_extension_suffix(discovery):
    right = install(discovery)
    install(discovery, "99.1.1", target="windows-x86_64")
    assert CodexBackend()._discover_binary() == str(right)


def test_incomplete_and_non_executable_versions_are_skipped(discovery, monkeypatch):
    right = install(discovery)
    broken = install(discovery, "99.1.1")
    broken.unlink()
    blocked = install(discovery, "98.1.1")
    monkeypatch.setattr(codex.os, "access", lambda p, mode: p != blocked)
    assert CodexBackend()._discover_binary() == str(right)


def test_unreadable_editor_does_not_hide_other_editors(discovery, monkeypatch):
    binary = install(discovery, editor=".windsurf")
    real_glob = codex.Path.glob

    def glob(path, pattern):
        if path == discovery / ".vscode" / "extensions":
            raise PermissionError("unreadable")
        return real_glob(path, pattern)

    monkeypatch.setattr(codex.Path, "glob", glob)
    assert CodexBackend()._discover_binary() == str(binary)


def test_path_precedes_extension(discovery, monkeypatch):
    install(discovery)
    monkeypatch.setattr(
        codex.shutil, "which", lambda name: "/standalone/codex" if name == "codex" else None
    )
    info = CodexBackend()._discover_binary_info()
    assert info.path == "/standalone/codex"
    assert info.source == "path"


def test_config_precedes_environment_and_path(discovery, monkeypatch):
    install(discovery)
    monkeypatch.setenv("CODEX_BIN", "/environment/codex")
    monkeypatch.setattr(codex.shutil, "which", lambda name: "/path/codex")
    backend = CodexBackend({"bin": "/configured/codex"})
    info = backend._discover_binary_info()
    assert info.path == "/configured/codex"
    assert info.source == "config"
    assert backend._config == {"bin": "/configured/codex"}


def test_environment_precedes_path(discovery, monkeypatch):
    install(discovery)
    monkeypatch.setenv("CODEX_BIN", "/environment/codex")
    info = CodexBackend({"bin": "  "})._discover_binary_info()
    assert info.path == "/environment/codex"
    assert info.source == "env"


@pytest.mark.parametrize("setting", ["config", "env"])
def test_missing_named_override_does_not_fall_back(discovery, monkeypatch, setting):
    install(discovery)
    config = {"bin": "missing-custom-codex"} if setting == "config" else {}
    if setting == "env":
        monkeypatch.setenv("CODEX_BIN", "missing-custom-codex")
    with pytest.raises(RuntimeError, match="Configured Codex executable not found"):
        CodexBackend(config)._discover_binary()


@pytest.mark.parametrize("setting", ["config", "env", "path"])
def test_explicit_extension_path_keeps_its_acquisition_source(
    discovery, monkeypatch, setting
):
    binary = install(discovery / "user with spaces")
    config = {"bin": str(binary)} if setting == "config" else {}
    if setting == "env":
        monkeypatch.setenv("CODEX_BIN", str(binary))
    if setting == "path":
        monkeypatch.setattr(codex.shutil, "which", lambda name: str(binary))
    info = CodexBackend(config)._discover_binary_info()
    assert info.path == str(binary)
    assert info.source == "vscode"


def test_windows_prefers_cmd_to_extensionless_npm_shim(discovery, monkeypatch):
    monkeypatch.setattr(codex, "sys", SimpleNamespace(platform="win32"))
    found = {"codex": "/npm/codex", "codex.cmd": "/npm/codex.cmd"}
    monkeypatch.setattr(codex.shutil, "which", found.get)
    assert CodexBackend()._discover_binary() == "/npm/codex.cmd"


def test_missing_engine_explains_extension_route(discovery):
    with pytest.raises(RuntimeError, match="no separate CLI installation required"):
        CodexBackend()._discover_binary()


@pytest.mark.parametrize("os_name,machine,target", [
    ("win32", "AMD64", "windows-x86_64"),
    ("win32", "ARM64", "windows-aarch64"),
    ("darwin", "x86_64", "macos-x86_64"),
    ("darwin", "arm64", "macos-aarch64"),
    ("linux", "x64", "linux-x86_64"),
    ("linux", "aarch64", "linux-aarch64"),
    ("linux", "riscv64", None),
    ("freebsd", "x86_64", None),
])
def test_target_matches_codex_extension_layout(monkeypatch, os_name, machine, target):
    monkeypatch.setattr(codex, "sys", SimpleNamespace(platform=os_name))
    monkeypatch.setattr(codex.platform, "machine", lambda: machine)
    assert codex._bundled_target() == target
