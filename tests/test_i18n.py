"""Pure-Python tests for common.i18n (no Qt)."""
import pytest


@pytest.fixture(autouse=True)
def _restore_i18n():
    import common.i18n as i18n
    saved = (i18n._active, dict(i18n._catalogs))
    yield
    i18n._active, i18n._catalogs = saved[0], saved[1]


def _write_catalogs(tmp_path, files: dict[str, str]):
    d = tmp_path / "i18n"
    d.mkdir()
    for stem, body in files.items():
        (d / f"{stem}.toml").write_text(body, encoding="utf-8")
    return d


def _patch_dir(monkeypatch, d):
    import common.i18n as i18n
    # patch where it's used: i18n.py's bound name
    monkeypatch.setattr(i18n, "i18n_dir", lambda: d)


def test_translation_active_language(tmp_path, monkeypatch):
    import common.i18n as i18n
    d = _write_catalogs(tmp_path, {
        "en": '"greet" = "Hello {name}"\n"_lang.name" = "English"\n',
        "ja": '"greet" = "こんにちは {name}"\n"_lang.name" = "日本語"\n',
    })
    _patch_dir(monkeypatch, d)
    i18n.init_language()
    i18n._active = "ja"
    assert i18n.tr("greet", name="X") == "こんにちは X"
    i18n._active = "en"
    assert i18n.tr("greet", name="X") == "Hello X"


def test_fallback_to_en_then_key(tmp_path, monkeypatch):
    import common.i18n as i18n
    d = _write_catalogs(tmp_path, {
        "en": '"only_en" = "EN only"\n',
        "ja": '"other" = "他"\n',
    })
    _patch_dir(monkeypatch, d)
    i18n._load_catalogs()
    i18n._active = "ja"
    # missing in ja → en value
    assert i18n.tr("only_en") == "EN only"
    # missing in both → key
    assert i18n.tr("nonexistent.key") == "nonexistent.key"


def test_empty_string_value_does_not_leak_to_base(tmp_path, monkeypatch):
    import common.i18n as i18n
    d = _write_catalogs(tmp_path, {
        "en": '"k" = "EN"\n',
        "ja": '"k" = ""\n',
    })
    _patch_dir(monkeypatch, d)
    i18n._load_catalogs()
    i18n._active = "ja"
    # active has "" → adopt it, do NOT leak to en
    assert i18n.tr("k") == ""


def test_never_raise_on_bad_interpolation(tmp_path, monkeypatch):
    import common.i18n as i18n
    d = _write_catalogs(tmp_path, {
        "en": '"need" = "Hi {name}"\n"attr" = "Val {x.attr}"\n',
    })
    _patch_dir(monkeypatch, d)
    i18n._load_catalogs()
    i18n._active = "en"
    # missing kwargs → untouched template, no raise
    assert i18n.tr("need") == "Hi {name}"
    # attribute access on incompatible kwargs → absorbed, returns template
    assert i18n.tr("attr", x=123) == "Val {x.attr}"


def test_never_raise_when_dir_missing(tmp_path, monkeypatch):
    import common.i18n as i18n
    _patch_dir(monkeypatch, tmp_path / "does_not_exist")
    i18n._load_catalogs()
    assert i18n.tr("anything") == "anything"
    assert i18n.available_languages() == []


def test_never_raise_on_non_utf8_catalog(tmp_path, monkeypatch):
    import common.i18n as i18n
    import llm_bridge.paths as lp
    d = tmp_path / "i18n"
    d.mkdir()
    (d / "en.toml").write_text('"k" = "EN"\n', encoding="utf-8")
    # non-UTF-8 bytes (Shift_JIS) → tomllib raises UnicodeDecodeError on load;
    # _load_catalogs must absorb it and treat the file as an empty catalog.
    (d / "ja.toml").write_bytes('"k" = "日本語"\n'.encode("shift_jis"))
    _patch_dir(monkeypatch, d)
    # init_language() reads ui_prefs → keep it hermetic (no real data/ access).
    monkeypatch.setattr(lp, "ui_prefs_path", lambda: tmp_path / "ui_prefs.json")
    i18n._load_catalogs()               # must not raise
    i18n._active = "ja"
    assert i18n.tr("k") == "EN"          # broken ja → {} → fall back to en
    assert i18n.available_languages() == ["en", "ja"]  # file still listed
    i18n.init_language()                # must not raise with the bad file present
    assert i18n.tr("k") == "EN"


@pytest.mark.parametrize("ja_body", [
    '"good" = "OK"\n',                                  # all-str catalog
    '"good" = "OK"\n"k.bad" = 123\n[section]\nx = "y"\n',  # mixed bad types
])
def test_non_str_values_are_dropped(tmp_path, monkeypatch, ja_body):
    import common.i18n as i18n
    d = _write_catalogs(tmp_path, {
        "en": '"good" = "OK_EN"\n"k.bad" = "fallback"\n',
        "ja": ja_body,
    })
    _patch_dir(monkeypatch, d)
    i18n._load_catalogs()
    i18n._active = "ja"
    assert i18n.tr("good") == "OK"
    # non-str / table values dropped → fall back to en, then key; never raises
    assert isinstance(i18n.tr("k.bad"), str)
    assert i18n.tr("k.bad") == "fallback"
    # a table-only key not in en → key
    assert i18n.tr("section") == "section"


def test_set_language_persists_preserving_siblings(tmp_path, monkeypatch):
    import json
    import common.i18n as i18n
    import llm_bridge.paths as lp
    d = _write_catalogs(tmp_path, {
        "en": '"k" = "EN"\n', "ja": '"k" = "JA"\n',
    })
    _patch_dir(monkeypatch, d)
    prefs = tmp_path / "ui_prefs.json"
    prefs.write_text(json.dumps({"chat_zoom": 3}), encoding="utf-8")
    monkeypatch.setattr(lp, "ui_prefs_path", lambda: prefs)
    i18n._load_catalogs()
    i18n.set_language("ja")
    data = json.loads(prefs.read_text(encoding="utf-8"))
    assert data == {"chat_zoom": 3, "language": "ja"}
    assert lp.read_ui_pref("language", None) == "ja"


def test_set_language_unknown_is_ignored(tmp_path, monkeypatch):
    import common.i18n as i18n
    import llm_bridge.paths as lp
    d = _write_catalogs(tmp_path, {
        "en": '"k" = "EN"\n', "ja": '"k" = "JA"\n',
    })
    _patch_dir(monkeypatch, d)
    monkeypatch.setattr(lp, "ui_prefs_path", lambda: tmp_path / "ui_prefs.json")
    i18n._load_catalogs()
    i18n._active = "ja"
    i18n.set_language("zz")  # no such catalog
    assert i18n.current_language() == "ja"  # unchanged


def test_init_language_reads_pref(tmp_path, monkeypatch):
    import json
    import common.i18n as i18n
    import llm_bridge.paths as lp
    d = _write_catalogs(tmp_path, {
        "en": '"k" = "EN"\n', "ja": '"k" = "JA"\n',
    })
    _patch_dir(monkeypatch, d)
    prefs = tmp_path / "ui_prefs.json"
    prefs.write_text(json.dumps({"language": "ja"}), encoding="utf-8")
    monkeypatch.setattr(lp, "ui_prefs_path", lambda: prefs)
    i18n.init_language()
    assert i18n.current_language() == "ja"


def test_init_language_defaults_to_en_when_unset(tmp_path, monkeypatch):
    import common.i18n as i18n
    import llm_bridge.paths as lp
    d = _write_catalogs(tmp_path, {
        "en": '"k" = "EN"\n', "ja": '"k" = "JA"\n',
    })
    _patch_dir(monkeypatch, d)
    monkeypatch.setattr(lp, "ui_prefs_path", lambda: tmp_path / "ui_prefs.json")
    i18n.init_language()
    assert i18n.current_language() == "en"
