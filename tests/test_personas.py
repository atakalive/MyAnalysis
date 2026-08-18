"""ペルソナ定義ストア (data/llm_state/personas.json — llm_bridge/personas.py)。

シードは「ファイル不在の間だけ」提示され、初回書込で実体化する。以後はファイルが
正で、空リストもシードを復活させない。unreadable (破損・version≠1) は読み []・
書き拒否。conftest の autouse fixture が personas_path を tmp へ向けるので、
実ファイルは触らない。
"""
import json

import pytest

import llm_bridge.paths as lb_paths
from llm_bridge.personas import (
    SCHEMA_VERSION,
    SEED_PERSONAS,
    Persona,
    delete_persona,
    get_persona,
    list_personas,
    save_personas,
    upsert_persona,
)

SEED_NAME = SEED_PERSONAS[0].name


def personas_path():
    """Resolve through the module so conftest's monkeypatch applies."""
    return lb_paths.personas_path()


def _read_raw() -> dict:
    return json.loads(personas_path().read_text(encoding="utf-8"))


# ---- absent → シード提示 ----

def test_absent_shows_seed_without_creating_file():
    assert list_personas() == list(SEED_PERSONAS)
    assert get_persona(SEED_NAME) == SEED_PERSONAS[0]
    assert not personas_path().exists()          # 読みは書かない


def test_get_unknown_or_blank_name_returns_none():
    assert get_persona("居ない名前") is None
    assert get_persona("") is None               # "" は「明示的になし」センチネル
    assert get_persona("   ") is None


# ---- 初回書込の実体化 ----

def test_first_upsert_materialises_seed_plus_edit():
    assert upsert_persona("新入り", "テキスト") is True
    raw = _read_raw()
    assert raw["version"] == SCHEMA_VERSION
    assert [p["name"] for p in raw["personas"]] == [SEED_NAME, "新入り"]
    assert list_personas() == [SEED_PERSONAS[0], Persona("新入り", "テキスト")]


def test_editing_seeded_persona_persists():
    assert upsert_persona(SEED_NAME, "改稿した本文") is True
    assert list_personas() == [Persona(SEED_NAME, "改稿した本文")]
    assert get_persona(SEED_NAME).text == "改稿した本文"


def test_upsert_blank_name_refused():
    assert upsert_persona("", "x") is False      # "" はセンチネルとして予約
    assert upsert_persona("   ", "x") is False
    assert not personas_path().exists()


def test_upsert_strips_name():
    assert upsert_persona("  余白  ", "t") is True
    assert get_persona("余白") == Persona("余白", "t")
    assert get_persona("  余白  ") == Persona("余白", "t")


# ---- 削除・復活なし ----

def test_delete_seeded_then_reload_gives_empty_no_resurrection():
    assert delete_persona(SEED_NAME) is True
    assert list_personas() == []                 # 再読込でもシードは復活しない
    assert _read_raw()["personas"] == []
    assert get_persona(SEED_NAME) is None


def test_delete_missing_name_is_noop_success():
    assert delete_persona("居ない名前") is True
    assert not personas_path().exists()          # 実体化の副作用も出さない


def test_empty_list_file_honoured():
    personas_path().write_text(
        json.dumps({"version": 1, "personas": []}), encoding="utf-8"
    )
    assert list_personas() == []
    assert get_persona(SEED_NAME) is None


# ---- unreadable (破損・version 不一致) → 読み []・書き拒否 ----

def test_corrupt_file_reads_empty_refuses_writes_bytes_untouched():
    personas_path().write_bytes(b"{ not json")
    assert list_personas() == []
    assert get_persona(SEED_NAME) is None
    assert save_personas([Persona("x", "y")]) is False
    assert upsert_persona("x", "y") is False
    assert delete_persona(SEED_NAME) is False
    assert personas_path().read_bytes() == b"{ not json"   # 潰していない


@pytest.mark.parametrize("version", [2, 0, "1", True, None])
def test_wrong_or_missing_version_is_unreadable(version):
    """version≠1 は将来スキーマかもしれないので読まず・潰さず (True は bool≠int)。"""
    payload = {"personas": [{"name": "a", "text": "b"}]}
    if version is not None:
        payload["version"] = version
    body = json.dumps(payload, ensure_ascii=False)
    personas_path().write_text(body, encoding="utf-8")
    assert list_personas() == []
    assert upsert_persona("a", "c") is False
    assert save_personas([]) is False
    assert personas_path().read_text(encoding="utf-8") == body


def test_non_list_personas_is_unreadable():
    personas_path().write_text(
        json.dumps({"version": 1, "personas": "junk"}), encoding="utf-8"
    )
    assert list_personas() == []
    assert upsert_persona("a", "b") is False


# ---- 正規化 ----

def test_normalization_skips_junk_and_dedupes_first_wins():
    personas_path().write_text(json.dumps({"version": 1, "personas": [
        "not a dict",
        {"name": "   ", "text": "blank name"},
        {"name": "  甲  ", "text": "first"},
        {"name": "甲", "text": "second"},        # 重複 → 先勝ち
        {"name": "乙", "text": 123},             # 非 str text → ""
        {"text": "no name"},
    ]}, ensure_ascii=False), encoding="utf-8")
    assert list_personas() == [Persona("甲", "first"), Persona("乙", "")]


def test_save_personas_normalises_input():
    assert save_personas([
        Persona("  甲 ", "first"), Persona("甲", "second"), Persona("  ", "x"),
    ]) is True
    assert list_personas() == [Persona("甲", "first")]


# ---- round-trip ----

def test_multiline_japanese_roundtrip_human_readable():
    text = "一行目。すぐ本題に入る。\n二行目：詳細な説明。\n\n三行目（空行の後）。"
    assert upsert_persona("多行", text) is True
    assert get_persona("多行") == Persona("多行", text)
    # ensure_ascii=False — 実ファイルが人間可読 (\uXXXX に潰れない)
    assert "多行" in personas_path().read_text(encoding="utf-8")
