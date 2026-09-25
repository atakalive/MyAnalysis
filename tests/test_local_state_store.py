"""data/llm_state の PC ローカル JSON: 0 バイト固着の自己修復と doctor 検出。

実測で backend_sessions.json が 0 バイトのまま固着していた。`read_json_classified`
は 0 バイトを 'unreadable' と分類し、writer 側は 'unreadable' なら「兄弟エントリを
守るため書かない」で skip する — この 2 つが噛み合うと**一度 0 バイト化したファイルには
二度と書けない**。結果 resume token が全エンジンで一度も保存されず、`--resume` が
使われないまま毎回全履歴 replay になっていた。

0 バイトは「守るべき破損データ」ではなく「守る中身が無い」。その境界をここで固定する。
中身のある破損ファイルは従来どおり守る（潰さない）ことも同時に固定する。

conftest の autouse fixture が各 path を tmp へ向けるので実ファイルは触らない。
"""
import json

import pytest

import llm_bridge.paths as lb_paths
from llm_bridge.doctor import check_local_state
from llm_bridge.paths import (
    note_recent_dataset,
    read_backend_session,
    read_recent_datasets,
    read_ui_pref,
    update_ui_pref,
    write_backend_session,
)

CORRUPT = b"{ not json"


# ---- 0 バイトは書き直す / 中身のある破損は守る ----

def test_zero_byte_ui_prefs_is_rewritten():
    lb_paths.ui_prefs_path().write_bytes(b"")
    update_ui_pref("lang", "ja")
    assert read_ui_pref("lang") == "ja"


def test_corrupt_ui_prefs_is_preserved():
    lb_paths.ui_prefs_path().write_bytes(CORRUPT)
    update_ui_pref("lang", "ja")
    assert lb_paths.ui_prefs_path().read_bytes() == CORRUPT   # 潰していない


def test_zero_byte_recent_datasets_is_rewritten():
    lb_paths.recent_datasets_path().write_bytes(b"")
    note_recent_dataset("ds1")
    assert "ds1" in read_recent_datasets()


def test_corrupt_recent_datasets_is_preserved():
    lb_paths.recent_datasets_path().write_bytes(CORRUPT)
    note_recent_dataset("ds1")
    assert lb_paths.recent_datasets_path().read_bytes() == CORRUPT


def test_zero_byte_backend_sessions_is_rewritten():
    """これが実際に起きていた事故そのもの（従来は永久に書けなかった）。"""
    lb_paths.backend_sessions_path().write_bytes(b"")
    write_backend_session("s1", "claude-cli", "claude-code", "tok-1")
    rec = read_backend_session("s1")
    assert rec is not None and rec["token"] == "tok-1"


def test_corrupt_backend_sessions_is_preserved():
    lb_paths.backend_sessions_path().write_bytes(CORRUPT)
    write_backend_session("s1", "claude-cli", "claude-code", "tok-1")
    assert lb_paths.backend_sessions_path().read_bytes() == CORRUPT


def test_unstattable_unreadable_is_preserved(monkeypatch):
    """サイズが取れない＝判定不能なら守る側に倒す（0 バイト扱いにしない）。"""
    path = lb_paths.ui_prefs_path()
    path.write_bytes(CORRUPT)
    real_stat = type(path).stat

    def boom(self, *a, **kw):
        if self == path:
            raise OSError("stat failed")
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(type(path), "stat", boom)
    assert lb_paths._preserve_unreadable(path, "unreadable") is True


# ---- doctor.check_local_state ----

@pytest.fixture
def state_dir(monkeypatch, tmp_path):
    d = tmp_path / "llm_state"
    d.mkdir()
    monkeypatch.setattr(lb_paths, "global_state_dir", lambda: d)
    return d


def test_check_local_state_clean_when_absent(state_dir):
    assert check_local_state() == ([], [])


def test_check_local_state_reports_zero_byte(state_dir):
    (state_dir / "backend_sessions.json").write_bytes(b"")
    issues, notes = check_local_state()
    assert notes == []
    assert any("backend_sessions.json" in i and "0 バイト" in i for i in issues)


def test_check_local_state_repair_deletes_zero_byte(state_dir):
    p = state_dir / "ui_prefs.json"
    p.write_bytes(b"")
    issues, notes = check_local_state(repair=True)
    assert issues == []
    assert any("ui_prefs.json" in n for n in notes)
    assert not p.exists()


def test_check_local_state_reports_corrupt(state_dir):
    (state_dir / "active.json").write_bytes(CORRUPT)
    issues, _ = check_local_state()
    assert any("active.json" in i and "読めない" in i for i in issues)


def test_check_local_state_accepts_valid(state_dir):
    (state_dir / "ui_prefs.json").write_text(json.dumps({"lang": "ja"}))
    assert check_local_state() == ([], [])


def test_check_local_state_ignores_queue_and_locks(state_dir):
    """commands/ のキューと *.lock は空が正常。誤検出してはならない。"""
    (state_dir / "commands").mkdir()
    (state_dir / "commands" / "queued.json").write_bytes(b"")
    (state_dir / "command_log.jsonl.lock").write_bytes(b"")
    (state_dir / "backend_sessions.json.tmp.abc").write_bytes(b"")   # mkstemp の残骸
    assert check_local_state() == ([], [])


def test_check_local_state_checks_personas_bak(state_dir):
    """personas.json は durable (primary + .bak) なので .bak も見る。"""
    (state_dir / "personas.json").write_text(
        json.dumps({"version": 1, "personas": []})
    )
    (state_dir / "personas.json.bak").write_bytes(b"")
    issues, _ = check_local_state()
    assert any("personas.json.bak" in i for i in issues)
