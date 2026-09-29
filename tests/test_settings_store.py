"""Tests for llm_backend.settings_store — targeted, comment-preserving TOML edits.

Fixtures are built from the committed example files (models.toml is NOT in the
tree). All writes go to tmp_path; the real config.toml / models.toml are never
touched.
"""
import tomllib
from pathlib import Path

import pytest

from llm_backend.settings_store import _apply_one, set_toml_keys

CONFIG_SAMPLE = (
    "# LLM backend configuration.\n"
    "\n"
    "[backend]\n"
    'name = "pi"          # "claude" | "pi" | "openai" | "mock"\n'
    "\n"
    "[claude_code]\n"
    'bin = ""             # auto-detect\n'
    'permission_mode = "bypassPermissions"\n'
)

MODELS_SAMPLE = (
    "[claude_code]\n"
    'model    = "opus"     # --model\n'
    "\n"
    "[pi]\n"
    'provider = "openai-codex"  # --provider\n'
    'model    = "gpt-5.5"       # --model\n'
)


def _write(p: Path, text: str) -> Path:
    # newline="" is required: Path.write_text defaults to newline=None, which
    # translates "\n" → os.linesep, so on Windows an "LF fixture" would land on
    # disk as CRLF and test_lf_preserved would assert against a CRLF input.
    p.write_text(text, encoding="utf-8", newline="")
    return p


def test_value_replace_preserves_comment_and_padding(tmp_path):
    p = _write(tmp_path / "config.toml", CONFIG_SAMPLE)
    set_toml_keys(p, {"backend": {"name": "claude"}})
    text = p.read_text(encoding="utf-8")
    # comment + alignment padding preserved verbatim, only the value changed.
    assert 'name = "claude"          # "claude" | "pi" | "openai" | "mock"' in text
    # unrelated lines untouched.
    assert 'permission_mode = "bypassPermissions"' in text
    assert tomllib.loads(text)["backend"]["name"] == "claude"


def test_key_insert_at_section_end(tmp_path):
    p = _write(tmp_path / "config.toml", CONFIG_SAMPLE)
    set_toml_keys(p, {"claude_code": {"model": "sonnet"}})
    lines = p.read_text(encoding="utf-8").splitlines()
    # inserted after permission_mode (last non-blank line of the section).
    i = lines.index('permission_mode = "bypassPermissions"')
    assert lines[i + 1] == 'model = "sonnet"'


def test_insert_before_trailing_blank_lines(tmp_path):
    p = _write(
        tmp_path / "c.toml",
        "[s]\n" 'a = "1"\n' "\n" "\n",
    )
    set_toml_keys(p, {"s": {"b": "2"}})
    lines = p.read_text(encoding="utf-8").splitlines()
    assert lines == ["[s]", 'a = "1"', 'b = "2"', "", ""]


def test_append_missing_section(tmp_path):
    p = _write(tmp_path / "config.toml", CONFIG_SAMPLE)
    set_toml_keys(p, {"pi": {"provider": "openai-codex"}})
    parsed = tomllib.loads(p.read_text(encoding="utf-8"))
    assert parsed["pi"]["provider"] == "openai-codex"
    assert "[pi]" in p.read_text(encoding="utf-8")


def test_minimal_generate_when_file_absent(tmp_path):
    p = tmp_path / "models.toml"
    assert not p.exists()
    set_toml_keys(p, {"claude_code": {"model": "sonnet"}})
    assert p.read_text(encoding="utf-8") == '[claude_code]\nmodel = "sonnet"\n'


def test_commented_out_line_untouched_and_not_counted(tmp_path):
    p = _write(tmp_path / "c.toml", '[s]\n# k = "old"\n')
    set_toml_keys(p, {"s": {"k": "new"}})
    text = p.read_text(encoding="utf-8")
    assert '# k = "old"' in text          # comment preserved
    assert 'k = "new"' in text            # active key inserted, not editing comment
    assert tomllib.loads(text)["s"]["k"] == "new"


def test_lf_preserved(tmp_path):
    p = _write(tmp_path / "c.toml", '[s]\nk = "a"\n')
    set_toml_keys(p, {"s": {"k": "b"}})
    raw = p.read_bytes()
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")


def test_crlf_preserved(tmp_path):
    p = tmp_path / "c.toml"
    p.write_bytes(b'[s]\r\nk = "a"\r\n')
    set_toml_keys(p, {"s": {"k": "b"}})
    raw = p.read_bytes()
    assert b"\r\n" in raw
    assert b'k = "b"' in raw
    assert tomllib.loads(p.read_text(encoding="utf-8"))["s"]["k"] == "b"


def test_no_trailing_newline_preserved(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[s]\nk = "a"', encoding="utf-8")   # no trailing newline
    set_toml_keys(p, {"s": {"k": "b"}})
    assert not p.read_text(encoding="utf-8").endswith("\n")


def test_bool_formatting(tmp_path):
    p = _write(tmp_path / "c.toml", "[s]\n")
    set_toml_keys(p, {"s": {"flag": True, "off": False}})
    parsed = tomllib.loads(p.read_text(encoding="utf-8"))
    assert parsed["s"]["flag"] is True
    assert parsed["s"]["off"] is False


def test_string_with_hash_roundtrips(tmp_path):
    p = _write(tmp_path / "c.toml", "[s]\n")
    set_toml_keys(p, {"s": {"k": "a#b"}})
    text = p.read_text(encoding="utf-8")
    # the '#' lives inside quotes → not parsed as an inline comment.
    assert tomllib.loads(text)["s"]["k"] == "a#b"


def test_replace_value_with_inline_comment_and_hash(tmp_path):
    p = _write(tmp_path / "c.toml", '[s]\nk = "old"   # note\n')
    set_toml_keys(p, {"s": {"k": "a#b"}})
    text = p.read_text(encoding="utf-8")
    assert "# note" in text
    assert tomllib.loads(text)["s"]["k"] == "a#b"


def test_single_line_array_is_rewritable(tmp_path):
    """Single-line arrays are a supported shape (the dialog's choice lists)."""
    p = _write(tmp_path / "c.toml", "[s]\nk = [1, 2]\n")
    set_toml_keys(p, {"s": {"k": "x"}})
    assert tomllib.loads(p.read_text(encoding="utf-8"))["s"]["k"] == "x"


def test_write_string_list(tmp_path):
    p = _write(tmp_path / "c.toml", '[s]\nk = "old"   # note\n')
    set_toml_keys(p, {"s": {"k": ["a", "b"]}})
    text = p.read_text(encoding="utf-8")
    assert "# note" in text                       # inline comment preserved
    assert 'k = ["a", "b"]' in text               # single line, JSON-quoted
    assert tomllib.loads(text)["s"]["k"] == ["a", "b"]


def test_write_empty_list_is_distinct_from_absent(tmp_path):
    """An empty list must persist as [] — 'user cleared it' ≠ 'never set'."""
    p = _write(tmp_path / "c.toml", "[s]\n")
    set_toml_keys(p, {"s": {"k": []}})
    assert tomllib.loads(p.read_text(encoding="utf-8"))["s"]["k"] == []


def test_list_round_trip_replaces_existing_list(tmp_path):
    p = _write(tmp_path / "c.toml", '[s]\nk = ["a", "b"]\n')
    set_toml_keys(p, {"s": {"k": ["c"]}})
    assert tomllib.loads(p.read_text(encoding="utf-8"))["s"]["k"] == ["c"]


def test_non_str_list_element_rejected_before_touching_file(tmp_path):
    orig = '[s]\nk = "a"\n'
    p = _write(tmp_path / "c.toml", orig)
    with pytest.raises(TypeError):
        set_toml_keys(p, {"s": {"k": ["ok", 3]}})
    assert p.read_text(encoding="utf-8") == orig


def test_multiline_array_refused_unchanged(tmp_path):
    """A multiline array has no closing bracket on the key line → fail-closed."""
    orig = '[s]\nk = [\n  "a",\n  "b",\n]\n'
    p = _write(tmp_path / "c.toml", orig)
    with pytest.raises(RuntimeError):
        set_toml_keys(p, {"s": {"k": "x"}})
    assert p.read_text(encoding="utf-8") == orig


def test_nested_array_refused_unchanged(tmp_path):
    """Inner brackets are outside the supported shape → fail-closed."""
    orig = "[s]\nk = [[1], [2]]\n"
    p = _write(tmp_path / "c.toml", orig)
    with pytest.raises(RuntimeError):
        set_toml_keys(p, {"s": {"k": "x"}})
    assert p.read_text(encoding="utf-8") == orig


def test_multiline_value_refused_unchanged(tmp_path):
    orig = '[s]\nk = """\nmany\n"""\n'
    p = _write(tmp_path / "c.toml", orig)
    with pytest.raises(RuntimeError):
        set_toml_keys(p, {"s": {"k": "x"}})
    assert p.read_text(encoding="utf-8") == orig


def test_bare_numeric_value_refused_unchanged(tmp_path):
    orig = "[s]\nk = 42\n"
    p = _write(tmp_path / "c.toml", orig)
    with pytest.raises(RuntimeError):
        set_toml_keys(p, {"s": {"k": "x"}})
    assert p.read_text(encoding="utf-8") == orig


def test_duplicate_active_key_refused():
    # A whole-file duplicate is unparseable TOML (→ self-heal), so exercise the
    # ambiguity guard at the unit level with an already-split line list.
    lines = ["[s]", 'k = "a"', 'k = "b"']
    with pytest.raises(RuntimeError):
        _apply_one(lines, "s", "k", '"x"', Path("dummy.toml"))


def test_non_str_bool_value_typeerror(tmp_path):
    p = _write(tmp_path / "c.toml", "[s]\n")
    with pytest.raises(TypeError):
        set_toml_keys(p, {"s": {"k": [1]}})
    with pytest.raises(TypeError):
        set_toml_keys(p, {"s": {"k": 5}})


def test_corrupt_file_self_heals_and_backs_up(tmp_path):
    p = tmp_path / "c.toml"
    corrupt = "[s]\nthis is not = = valid toml [[[\n"
    p.write_text(corrupt, encoding="utf-8")
    set_toml_keys(p, {"s": {"k": "v"}})
    # regenerated minimally with the requested key...
    assert tomllib.loads(p.read_text(encoding="utf-8"))["s"]["k"] == "v"
    # ...and the corrupt original preserved in a single .bak.
    bak = p.with_suffix(p.suffix + ".bak")
    assert bak.read_text(encoding="utf-8") == corrupt


def test_models_sample_targeted_edit(tmp_path):
    p = _write(tmp_path / "models.toml", MODELS_SAMPLE)
    set_toml_keys(p, {"pi": {"model": "gpt-6"}})
    text = p.read_text(encoding="utf-8")
    # only [pi].model changed; [pi].provider and [claude_code].model untouched.
    parsed = tomllib.loads(text)
    assert parsed["pi"]["model"] == "gpt-6"
    assert parsed["pi"]["provider"] == "openai-codex"
    assert parsed["claude_code"]["model"] == "opus"
    assert "# --provider" in text


@pytest.mark.parametrize("initial", [
    '[a]\nk = "v"\n',          # LF
    '[a]\r\nk = "v"\r\n',      # CRLF
    None,                      # absent
    "[x\n",                    # broken TOML
])
def test_set_toml_keys_returns_before_and_after(tmp_path, initial):
    p = tmp_path / "m.toml"
    if initial is not None:
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(initial)
    r = set_toml_keys(p, {"a": {"k": "w"}})
    with open(p, encoding="utf-8", newline="") as f:
        assert r.after == f.read()
    assert r.before == initial
    if initial == "[x\n":
        with open(p.with_suffix(".toml.bak"), encoding="utf-8", newline="") as f:
            assert f.read() == initial
