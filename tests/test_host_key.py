"""この PC の登録簿のキー（Issue #122）: common.host_key と、config・CLI の照合と書込。

conftest の autouse が非 macOS の経路に固定し、本物の scutil を禁じている。macOS の経路は
ここで _is_darwin と _scutil を差し替えて試す。
"""

from __future__ import annotations

import socket
import sys
from unittest.mock import patch

import pytest

import config
from common import host_key


class _FakeScutil:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.calls = 0

    def __call__(self, key: str) -> str | None:
        self.calls += 1
        return self.values.get(key)


@pytest.fixture
def mac(monkeypatch):
    """macOS の経路にして、scutil の値と gethostname を差し替える関数を返す。"""
    monkeypatch.setattr(host_key, "_is_darwin", lambda: True)
    holder: dict = {}

    def _setup(values: dict[str, str], hostname: str) -> _FakeScutil:
        fake = _FakeScutil(values)
        monkeypatch.setattr(host_key, "_scutil", fake)
        monkeypatch.setattr(socket, "gethostname", lambda: hostname)
        host_key.cache_clear()
        holder["fake"] = fake
        return fake

    yield _setup
    host_key.cache_clear()


def test_non_darwin_uses_gethostname_without_cache(monkeypatch):
    def _forbid(key):
        raise AssertionError("scutil must not be called")

    monkeypatch.setattr(host_key, "_scutil", _forbid)
    monkeypatch.setattr(socket, "gethostname", lambda: "pc-a")
    assert host_key.host_key() == "PC-A"
    monkeypatch.setattr(socket, "gethostname", lambda: "pc-b")
    assert host_key.host_key() == "PC-B"
    assert host_key.host_keys() == ["PC-B"]


def test_darwin_hostname_first(mac):
    mac({"HostName": "lab-mac", "LocalHostName": "Lab-Mac-2"}, "lab-mac")
    assert host_key.host_key() == "LAB-MAC"
    assert host_key.host_keys() == ["LAB-MAC", "LAB-MAC-2", "LAB-MAC-2.LOCAL"]


def test_darwin_without_hostname(mac):
    mac({"LocalHostName": "Mats-MBP"}, "dhcp-10-0-0-5.example.com")
    assert host_key.host_keys() == ["MATS-MBP", "MATS-MBP.LOCAL", "DHCP-10-0-0-5.EXAMPLE.COM"]


def test_darwin_scutil_fails(mac):
    mac({}, "Mats-MBP.local")
    assert host_key.host_key() == "MATS-MBP"
    assert host_key.host_keys() == ["MATS-MBP", "MATS-MBP.LOCAL"]


def test_darwin_invalid_hostname_is_skipped(mac):
    mac({"HostName": "bad name", "LocalHostName": "Mats-MBP"}, "x.local")
    assert host_key.host_key() == "MATS-MBP"
    assert host_key.host_keys()[0] == "MATS-MBP"
    assert "BAD NAME" not in host_key.host_keys()


def test_darwin_names_are_cached(mac):
    fake = mac({"HostName": "lab-mac", "LocalHostName": "Lab-Mac-2"}, "lab-mac")
    host_key.host_key()
    host_key.host_key()
    assert fake.calls == 2
    host_key.cache_clear()
    host_key.host_key()
    assert fake.calls == 4


def test_host_path_and_lookup(mac, tmp_path):
    mac({"LocalHostName": "Mats-MBP"}, "Mats-MBP.local")
    assert config.host_path({"MATS-MBP.LOCAL": "/a", "MATS-MBP": "/b"}) == ("MATS-MBP", "/b")
    assert config.host_path({"MATS-MBP.LOCAL": "/a"}) == ("MATS-MBP.LOCAL", "/a")
    assert config.host_path({"OTHER": "/c"}) is None

    d = tmp_path / "data"
    d.mkdir()
    config.DATASETS["ds"] = {"MATS-MBP.LOCAL": str(d)}
    assert config.get_dataset_dir("ds") == d

    res = config.register_dataset("ds2", str(d))
    assert res["host"] == "MATS-MBP"
    assert config.read_registry()["ds2"] == {"MATS-MBP": str(d)}


def _run_register(args_list):
    from llm_bridge import commands
    from llm_bridge.__main__ import main

    submitted = []

    def fake_submit(tier, target, verb, kwargs):
        submitted.append((tier, target, verb, kwargs))
        return "cmd-1"

    with patch.object(commands, "submit", fake_submit), \
         patch.object(commands, "wait_for", lambda cmd_id, timeout=None: None), \
         patch.object(sys, "argv", ["llm_bridge"] + args_list):
        rc = main()
    return rc, submitted


def test_cli_register_alias_is_another_host(mac, tmp_path):
    mac({"LocalHostName": "Mac"}, "Mac.local")
    assert host_key.host_keys() == ["MAC", "MAC.LOCAL"]
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    config.write_registry({"ds": {"MAC": str(old)}})
    config.reload_datasets()

    rc, submitted = _run_register(["register-dataset", "ds", str(new), "--host", "MAC.LOCAL"])
    assert rc == 0
    assert submitted == []
    assert not (old / "myanalysis.toml").exists()
    assert not (new / "myanalysis.toml").exists()

    rc, submitted = _run_register(["register-dataset", "ds", str(new), "--host", "MAC"])
    assert rc == 0
    assert len(submitted) == 1
