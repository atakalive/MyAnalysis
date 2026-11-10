"""Private backend installation: filesystem transactions and untrusted archives."""
from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import zipfile
from pathlib import Path

import pytest

from llm_backend import install, runtime


def _fake_claude(stage, target, log, cancelled):
    entry = stage / ("claude.exe" if target.startswith("win32") else "claude")
    entry.write_bytes(b"executable")
    entry.chmod(0o755)
    return "1.2.3", [entry]


@pytest.fixture
def fake_install(monkeypatch):
    monkeypatch.setattr(install, "_claude", _fake_claude)
    monkeypatch.setattr(install, "_run", lambda *a, **kw: "1.2.3")


def test_install_activates_only_verified_files(fake_install):
    result = install.install("claude", log=lambda _: None)
    command = runtime.managed_command("claude")
    assert command and os.path.isfile(command[0])
    assert result["version"] == "1.2.3"
    assert runtime.environment(command)["DISABLE_UPDATES"] == "1"
    assert not list(runtime.engine_dir("claude").glob("staging-*"))


@pytest.mark.parametrize("failure", ["download", "version", "activate", "cancel"])
def test_failed_update_keeps_the_previous_release(fake_install, monkeypatch, failure):
    install.install("claude", log=lambda _: None)
    root = runtime.engine_dir("claude")
    original = (root / "current.json").read_bytes()
    previous = runtime.managed_command("claude")
    if failure == "download":
        def fail(*a, **kw):
            raise OSError("offline")
        monkeypatch.setattr(install, "_claude", fail)
    elif failure == "version":
        monkeypatch.setattr(install, "_run", lambda *a, **kw: "wrong version")
    elif failure == "activate":
        def fail(*a, **kw):
            raise PermissionError("locked pointer")
        monkeypatch.setattr(install.os, "replace", fail)
    with pytest.raises((install.InstallError, OSError)):
        install.install("claude", log=lambda _: None, cancelled=lambda: failure == "cancel")
    assert (root / "current.json").read_bytes() == original
    assert runtime.managed_command("claude") == previous
    assert os.path.isfile(previous[0])


def test_new_install_cannot_be_observed_before_activation(fake_install, monkeypatch):
    def verify(*a, **kw):
        assert runtime.managed_command("claude") is None
        return "1.2.3"
    monkeypatch.setattr(install, "_run", verify)
    install.install("claude", log=lambda _: None)
    assert runtime.managed_command("claude")


def test_successful_update_keeps_the_old_executable_immutable(fake_install):
    install.install("claude", log=lambda _: None)
    previous = runtime.managed_command("claude")
    old_bytes = Path(previous[0]).read_bytes()
    install.install("claude", log=lambda _: None)
    assert runtime.managed_command("claude") != previous
    assert Path(previous[0]).read_bytes() == old_bytes


def test_same_engine_is_locked_across_installations(fake_install):
    from common.filelock import try_exclusive_lock
    root = runtime.engine_dir("claude")
    root.mkdir(parents=True)
    with try_exclusive_lock(root / "install.lock") as acquired:
        assert acquired
        with pytest.raises(install.InstallError, match="Another installation"):
            install.install("claude", log=lambda _: None)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/absolute", "a/../../escape", "a\\..\\escape"])
def test_zip_cannot_escape_the_staging_directory(tmp_path, name):
    archive = tmp_path / "test.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(name, "bad")
    with pytest.raises((install.InstallError, ValueError)):
        install._extract(archive, tmp_path / "out", lambda: False)
    assert not (tmp_path / "escape").exists()


def test_tar_cannot_follow_an_escaping_link(tmp_path):
    archive = tmp_path / "test.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        tf.addfile(link)
        member = tarfile.TarInfo("link/evil")
        member.size = 4
        tf.addfile(member, io.BytesIO(b"evil"))
    with pytest.raises((install.InstallError, ValueError, OSError)):
        install._extract(archive, tmp_path / "out", lambda: False)
    assert not (tmp_path / "outside").exists()


def test_tar_accepts_internal_relative_links_used_by_posix_node(tmp_path):
    archive = tmp_path / "node.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        source = tarfile.TarInfo("node/lib/cli.js")
        source.size = 4
        source.mode = 0o755
        tf.addfile(source, io.BytesIO(b"node"))
        link = tarfile.TarInfo("node/bin/npm")
        link.type = tarfile.SYMTYPE
        link.linkname = "../lib/cli.js"
        tf.addfile(link)
    out = tmp_path / "out"
    install._extract(archive, out, lambda: False)
    assert (out / "node/bin/npm").read_bytes() == b"node"
    if os.name != "nt":
        assert (out / "node/bin/npm").is_symlink()
        assert (out / "node/lib/cli.js").stat().st_mode & 0o100


def test_download_checks_integrity_and_cancellation(tmp_path, monkeypatch):
    class Response(io.BytesIO):
        url = "https://example.test/package"
    monkeypatch.setattr(install, "_open", lambda url: Response(b"payload"))
    checksum = "sha256:" + hashlib.sha256(b"payload").hexdigest()
    path = tmp_path / "download"
    install._download("https://example.test/package", path, checksum, lambda _: None, lambda: False)
    assert path.read_bytes() == b"payload"
    with pytest.raises(install.InstallError, match="Checksum"):
        install._download("https://example.test/package", path, "sha256:bad", lambda _: None, lambda: False)
    with pytest.raises(install.InstallError, match="cancelled"):
        install._download("https://example.test/package", path, checksum, lambda _: None, lambda: True)


def test_release_manifest_cannot_escape_install_root(fake_install):
    install.install("claude", log=lambda _: None)
    pointer = runtime.engine_dir("claude") / "current.json"
    data = json.loads(pointer.read_text())
    data["argv"] = ["../../outside.exe"]
    pointer.write_text(json.dumps(data))
    assert runtime.managed_command("claude") is None


def test_private_npm_configuration_does_not_inherit_global_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("NPM_CONFIG_PREFIX", "outside")
    monkeypatch.setenv("npm_config_cache", "outside-cache")
    monkeypatch.setenv("NODE_OPTIONS", "--require outside.js")
    env = install._npm_env(tmp_path, tmp_path / "node.exe")
    assert "NPM_CONFIG_PREFIX" not in env
    assert "NODE_OPTIONS" not in env
    assert env["npm_config_cache"] == str(tmp_path / "npm-cache")
    assert env["npm_config_global"] == "false"
    assert env["npm_config_userconfig"] != env["npm_config_globalconfig"]
    assert os.path.isfile(env["npm_config_userconfig"])
    assert os.path.isfile(env["npm_config_globalconfig"])


def test_private_pi_verification_ignores_system_node_options(monkeypatch):
    monkeypatch.setenv("NODE_OPTIONS", "--require missing-system-module.js")
    monkeypatch.setenv("NODE_PATH", "/unrelated/modules")

    def prepare(stage, target, log, cancelled):
        node = stage / "runtime" / ("node.exe" if os.name == "nt" else "bin/node")
        entry = stage / "agent" / "cli.js"
        for path in (node, entry):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"stub")
            path.chmod(0o755)
        return "1.2.3", [node, entry]

    def verify(argv, cwd, env, *args, **kwargs):
        assert "NODE_OPTIONS" not in env and "NODE_PATH" not in env
        return "1.2.3"

    monkeypatch.setattr(install, "_pi", prepare)
    monkeypatch.setattr(install, "_run", verify)
    install.install("pi", log=lambda _: None)
    assert runtime.managed_command("pi")
