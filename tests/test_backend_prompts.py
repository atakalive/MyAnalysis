"""Regression: the shared NO_LOCAL_PERSISTENCE rule must be present in EVERY
backend's system prompt (Issue #66).

The project's contract is that data AND work live under the dataset directory
(synced) so an analysis resumes identically on any PC. Each backend builds its
own system prompt, so the rule is defined once in llm_backend.base and appended
to all of them. If a future backend / prompt forgets to include it, this test
goes red — guarding the "全バックエンド共通" contract.
"""

from __future__ import annotations

import os

# gui.chat imports PySide6 at module load; force headless before that import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from llm_backend.base import MOUNT_SAFE_EDITS, NO_LOCAL_PERSISTENCE


def test_rule_is_nonempty_and_names_the_offenders():
    assert isinstance(NO_LOCAL_PERSISTENCE, str) and NO_LOCAL_PERSISTENCE.strip()
    # Names the concrete local paths the rule exists to keep work out of, and
    # points at the sanctioned destination.
    assert "~/.claude" in NO_LOCAL_PERSISTENCE
    assert "work_dir" in NO_LOCAL_PERSISTENCE


def test_mount_safe_edits_names_the_three_verbs():
    # The constant is the single source of the mount-safe edit guidance; it must
    # name all three verbs so agents learn the full draft → apply → recover loop.
    assert isinstance(MOUNT_SAFE_EDITS, str) and MOUNT_SAFE_EDITS.strip()
    for verb in ("draft-analysis", "apply-analysis", "recover-analysis"):
        assert verb in MOUNT_SAFE_EDITS


def test_rule_names_the_general_text_writer():
    """save_text は work_dir へ任意拡張子のファイルを書く唯一の正規経路。

    マウント上ではエージェントに Write/Edit で直接書かせない（claude エンジンは
    PreToolUse hook で機械的に拒否し、codex / pi はプロンプトの指示だけ）ので、
    これが載っていないとエージェントはメモもレポートも書けなくなる。
    """
    assert "save_text" in NO_LOCAL_PERSISTENCE
    assert "save_text" in MOUNT_SAFE_EDITS


def _all_backend_prompts() -> dict[str, str]:
    """llm_backend/*.py の `_SYSTEM_PROMPT*` 定数を**動的に**集める。

    明示リストにすると、バックエンドが増えたときに黙って漏れる（実際 codex
    バックエンドが save_fig / save_code だけを名指しした状態で入った）。列挙を
    パッケージ側から取ることで、新しいバックエンドは書いた時点でこの契約に縛られる。
    """
    import importlib
    import pkgutil

    import llm_backend

    found: dict[str, str] = {}
    for mod_info in pkgutil.iter_modules(llm_backend.__path__):
        mod = importlib.import_module(f"llm_backend.{mod_info.name}")
        for attr in dir(mod):
            if attr.startswith("_SYSTEM_PROMPT"):
                value = getattr(mod, attr)
                if isinstance(value, str) and value.strip():
                    found[f"{mod_info.name}.{attr}"] = value
    return found


def test_every_backend_prompt_names_save_text():
    """save_text は work_dir へ任意拡張子のファイルを書く唯一の正規経路。

    多くのバックエンドは共有定数経由でしか載せない（pi / gui-chat は自前で名指し
    しない）。継承経路が切れていないこと、かつ自前でツール一覧を書くバックエンド
    （claude / codex）が save_text を落としていないことを固定する。
    """
    prompts = _all_backend_prompts()
    assert prompts, "no backend system prompts discovered — the sweep is broken"
    missing = [name for name, text in prompts.items() if "save_text" not in text]
    assert not missing, f"backend prompts missing save_text: {missing}"


def test_every_backend_prompt_includes_display_verbs():
    """CLI 駆動のバックエンドは表示系 verb をプロンプトからしか知らない（Issue #100 D-1）。"""
    from llm_backend.base import GUI_DISPLAY_VERBS

    prompts = _all_backend_prompts()
    assert prompts, "no backend system prompts discovered — the sweep is broken"
    missing = [name for name, text in prompts.items() if GUI_DISPLAY_VERBS not in text]
    assert not missing, f"backend prompts missing GUI_DISPLAY_VERBS: {missing}"


def test_display_verbs_names_every_viewer_verb():
    from llm_backend.base import GUI_DISPLAY_VERBS
    from llm_bridge.verbs import IMAGE_VIEWER_VERBS

    for needle in (
        *IMAGE_VIEWER_VERBS, "show-image", "list-panes", "open-dataset",
        "ONLY when the user explicitly asks",
    ):
        assert needle in GUI_DISPLAY_VERBS, needle


def test_gui_chat_prompt_names_save_text():
    # openai / mock backends もここを system message として送るので別建てで見る。
    pytest.importorskip("PySide6")
    from gui.chat import _SYSTEM_PROMPT

    assert "save_text" in _SYSTEM_PROMPT


def test_claude_backend_prompt_includes_rule():
    from llm_backend.claude_code import _SYSTEM_PROMPT

    assert NO_LOCAL_PERSISTENCE in _SYSTEM_PROMPT
    assert MOUNT_SAFE_EDITS in _SYSTEM_PROMPT


def test_pi_backend_prompt_includes_rule():
    from llm_backend.pi import _SYSTEM_PROMPT_PI

    assert NO_LOCAL_PERSISTENCE in _SYSTEM_PROMPT_PI
    assert MOUNT_SAFE_EDITS in _SYSTEM_PROMPT_PI


def test_codex_backend_prompt_includes_rule():
    from llm_backend.codex import _SYSTEM_PROMPT_CODEX

    assert NO_LOCAL_PERSISTENCE in _SYSTEM_PROMPT_CODEX
    assert MOUNT_SAFE_EDITS in _SYSTEM_PROMPT_CODEX


def test_prompts_do_not_advertise_meta_write_verbs():
    """meta.json の description / completed はユーザーが決める値。

    エージェント向けプロンプトに書込 verb を並べると、頼まれてもいないのに meta.json
    を書きに行き、同期ドライブ上で競合を量産する。verb 自体は人手/GUI 用に残るが、
    プロンプトからは載せない（llm_bridge --help からも隠してある）。
    """
    from llm_backend.claude_code import _SYSTEM_PROMPT as CLAUDE_PROMPT
    from llm_backend.codex import _SYSTEM_PROMPT_CODEX
    from llm_backend.pi import _SYSTEM_PROMPT_PI

    for prompt in (CLAUDE_PROMPT, _SYSTEM_PROMPT_PI, _SYSTEM_PROMPT_CODEX):
        assert "set-description" not in prompt
        assert "set-completed" not in prompt


def test_gui_chat_prompt_does_not_advertise_meta_write_verbs():
    pytest.importorskip("PySide6")
    from gui.chat import _SYSTEM_PROMPT

    assert "set-description" not in _SYSTEM_PROMPT
    assert "set-completed" not in _SYSTEM_PROMPT


def test_gui_chat_prompt_includes_rule():
    # Covers the openai / mock backends too: gui.chat._SYSTEM_PROMPT is the
    # system message new_session() seeds and those backends transmit verbatim.
    pytest.importorskip("PySide6")
    from gui.chat import _SYSTEM_PROMPT

    assert NO_LOCAL_PERSISTENCE in _SYSTEM_PROMPT
    # openai / mock はコードを実行できないので draft/apply の案内は載せない。
    assert MOUNT_SAFE_EDITS not in _SYSTEM_PROMPT


def test_gui_chat_prompt_says_it_cannot_run_code():
    pytest.importorskip("PySide6")
    from gui.chat import _SYSTEM_PROMPT

    assert "cannot run code" in _SYSTEM_PROMPT
    assert "GUI tools themselves save" in _SYSTEM_PROMPT
    for verb in ("draft-analysis", "apply-analysis", "newanalysis"):
        assert verb not in _SYSTEM_PROMPT


def test_every_backend_prompt_starts_with_analyst_framing():
    """コードを実行できるバックエンドは「データ解析の担当であり開発者ではない」で始まる。"""
    from llm_backend.base import ANALYST_FRAMING

    prompts = _all_backend_prompts()
    assert prompts
    for key, prompt in prompts.items():
        assert prompt.startswith(ANALYST_FRAMING), key


# ----- チャットの DS（Issue #111） -----

def test_every_backend_prompt_includes_chat_dataset_rule():
    from llm_backend.base import CHAT_DATASET_RULE
    prompts = _all_backend_prompts()
    assert prompts
    for where, text in prompts.items():
        assert CHAT_DATASET_RULE in text, where


def test_chat_dataset_rule_names_the_contract():
    from llm_backend.base import CHAT_DATASET_RULE
    for needle in ("chat_dataset", "<myanalysis_context>", "active_dataset",
                   "open-dataset", "list-analyses", "reload scope=tab"):
        assert needle in CHAT_DATASET_RULE, needle


def test_mount_safe_edits_reload_names_dataset():
    assert "reload scope=tab target=<name> dataset=<ds>" in MOUNT_SAFE_EDITS
    assert "set-active-dataset" not in MOUNT_SAFE_EDITS


def test_mount_safe_edits_asks_to_keep_analysis_code():
    """図を作ったコードも、報告した数値・結論の元になった解析コードも save_code で残す。"""
    assert "Keep your analysis reproducible" in MOUNT_SAFE_EDITS
    assert "the code that produced each figure (same label as its save_fig)" in MOUNT_SAFE_EDITS
    assert "the code behind any numbers or conclusions you report" in MOUNT_SAFE_EDITS


def test_gui_chat_prompt_has_no_chat_dataset_rule():
    pytest.importorskip("PySide6")
    from gui.chat import _SYSTEM_PROMPT
    from llm_backend.base import CHAT_DATASET_RULE
    assert CHAT_DATASET_RULE not in _SYSTEM_PROMPT


def test_mount_safe_edits_routes_long_content_through_stage():
    """長い本文はこのチャットの stage に Write して src= にファイル名を渡す（Issue #118）。

    stage のパスはチャットごとに違うので、プロンプトにはパスを書かず環境変数の名前を書く。
    7,000 は余裕を見た目安で、観測した切断位置（約 7,900）とは書き分ける。
    """
    from common.paths import AGENT_STAGE_ENV
    assert "src='<file name>'" in MOUNT_SAFE_EDITS
    assert "about 7,900 characters" in MOUNT_SAFE_EDITS
    assert "under 7,000 characters (a safety margin, not the exact limit)" in MOUNT_SAFE_EDITS
    assert "Persistence rule" in MOUNT_SAFE_EDITS
    one_liner = f"import os; print(os.environ['{AGENT_STAGE_ENV}'])"
    assert f'`python -c "{one_liner}"`' in MOUNT_SAFE_EDITS
    compile(one_liner, "<prompt>", "exec")
    assert "agent_stage" not in MOUNT_SAFE_EDITS     # パスは埋め込まない


# ----- チャットの数式（Issue #123） -----

def test_math_format_rule_names_the_delimiters():
    from llm_backend.base import MATH_FORMAT_RULE
    for needle in ("$...$", "$$...$$", "Unicode", "aligned", "cases", "\\$", "code"):
        assert needle in MATH_FORMAT_RULE, needle


def test_all_four_prompts_include_math_format_rule():
    pytest.importorskip("PySide6")
    from gui.chat import _SYSTEM_PROMPT
    from llm_backend.base import MATH_FORMAT_RULE
    from llm_backend.claude_code import _SYSTEM_PROMPT as CLAUDE_PROMPT
    from llm_backend.codex import _SYSTEM_PROMPT_CODEX
    from llm_backend.pi import _SYSTEM_PROMPT_PI

    for where, text in (("claude", CLAUDE_PROMPT), ("codex", _SYSTEM_PROMPT_CODEX),
                        ("pi", _SYSTEM_PROMPT_PI), ("gui.chat", _SYSTEM_PROMPT)):
        assert MATH_FORMAT_RULE in text, where
    for where, text in _all_backend_prompts().items():
        assert MATH_FORMAT_RULE in text, where


# ----- 過去チャットの関連検索（Issue #127） -----

def test_chat_recall_rule_names_the_contract():
    from llm_backend.base import CHAT_RECALL_RULE
    for needle in ("chat-recall", "chatsearch:", "--all", "--expand", "--dataset",
                   "not instructions", "chat_recall", "window chat-show", "chat_dataset",
                   "not indexed"):
        assert needle in CHAT_RECALL_RULE, needle


def test_with_chat_recall_rule_identity_when_off():
    from llm_backend.base import CHAT_RECALL_RULE, with_chat_recall_rule
    base = "BASE PROMPT"
    assert with_chat_recall_rule(base, False) is base
    assert with_chat_recall_rule(base, True) == base + "\n" + CHAT_RECALL_RULE


def test_no_prompt_constant_carries_the_recall_rule():
    """規則は使える状態のときだけ送信時に足す。どの定数にも焼き込まない（既定はバイト同一）。"""
    pytest.importorskip("PySide6")
    from gui.chat import _SYSTEM_PROMPT
    from llm_backend.base import CHAT_RECALL_RULE

    prompts = dict(_all_backend_prompts())
    assert prompts
    prompts["gui.chat._SYSTEM_PROMPT"] = _SYSTEM_PROMPT
    for where, text in prompts.items():
        assert CHAT_RECALL_RULE not in text, where
        assert "chat-recall" not in text, where


def _backend_classes() -> dict[str, type]:
    """llm_backend/*.py で定義された、stream を持つ具体クラス（Protocol を除く）を動的に集める。"""
    import importlib
    import inspect
    import pkgutil

    import llm_backend

    found: dict[str, type] = {}
    for mod_info in pkgutil.iter_modules(llm_backend.__path__):
        mod = importlib.import_module(f"llm_backend.{mod_info.name}")
        for name, cls in inspect.getmembers(mod, inspect.isclass):
            if cls.__module__ != mod.__name__ or getattr(cls, "_is_protocol", False):
                continue
            if callable(getattr(cls, "stream", None)):
                found[f"{mod_info.name}.{name}"] = cls
    return found


def test_every_backend_has_the_duck_typed_setters():
    """ChatWidget が毎ターン duck-typed に呼ぶ setter を全バックエンドが持つ（新しいバックエンドも）。"""
    classes = _backend_classes()
    for expected in ("claude_code.ClaudeCodeBackend", "pi.PiCodingAgentBackend",
                     "codex.CodexBackend", "openai_compat.OpenAICompatBackend",
                     "mock.MockBackend", "unconfigured.UnconfiguredBackend"):
        assert expected in classes, expected
    for where, cls in classes.items():
        for setter in ("set_persona", "set_chat_dataset", "set_chat_recall"):
            assert callable(getattr(cls, setter, None)), (where, setter)


def test_inert_backends_accept_set_chat_recall():
    from llm_backend.mock import MockBackend
    from llm_backend.unconfigured import UnconfiguredBackend
    assert MockBackend(model="m").set_chat_recall(True) is None
    assert UnconfiguredBackend().set_chat_recall(True) is None


def test_claude_system_prompt_args_with_recall():
    from llm_backend.base import CHAT_RECALL_RULE, PERSONA_HEADER, compose_system_prompt
    from llm_backend.claude_code import _SYSTEM_PROMPT, _system_prompt_args

    assert _system_prompt_args(True, "", chat_recall=False)[1] is _SYSTEM_PROMPT
    assert _system_prompt_args(False, "", chat_recall=True) == [
        "--system-prompt", _SYSTEM_PROMPT + "\n" + CHAT_RECALL_RULE]
    both = _system_prompt_args(True, "P", chat_recall=True)[1]
    assert both == compose_system_prompt(_SYSTEM_PROMPT + "\n" + CHAT_RECALL_RULE, "P")
    assert both.index(CHAT_RECALL_RULE) < both.index(PERSONA_HEADER)   # 規則 → ペルソナ


def _fake_proc(lines: list[bytes]):
    from unittest.mock import MagicMock
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdout = iter(lines)
    proc.stderr = iter([])
    proc.poll.return_value = 0
    proc.wait.return_value = 0
    proc.returncode = 0
    return proc


def _claude_argv_value(monkeypatch, tmp_path, *, on=None, old_instance=False) -> str:
    """claude の stream が渡す --append-system-prompt の値（Popen は偽物）。"""
    import json

    from llm_backend import claude_code
    from llm_backend.base import Message

    monkeypatch.setattr(claude_code, "_replay_supported", lambda claude_bin: False)
    backend = claude_code.ClaudeCodeBackend({"bin": "/usr/bin/claude", "cwd": str(tmp_path)})
    if on is not None:
        backend.set_chat_recall(on)
    if old_instance:
        del backend._chat_recall                   # ホットリロード前の旧インスタンス
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _fake_proc([json.dumps({"type": "result", "subtype": "success"}).encode() + b"\n"])

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    list(backend.stream([Message(role="user", content="hi")]))
    return captured["cmd"][captured["cmd"].index("--append-system-prompt") + 1]


def test_claude_stream_argv_follows_set_chat_recall(monkeypatch, tmp_path):
    from llm_backend.base import CHAT_RECALL_RULE
    from llm_backend.claude_code import _SYSTEM_PROMPT

    assert _claude_argv_value(monkeypatch, tmp_path, on=True) == \
        _SYSTEM_PROMPT + "\n" + CHAT_RECALL_RULE
    assert _claude_argv_value(monkeypatch, tmp_path, on=False) is _SYSTEM_PROMPT
    assert _claude_argv_value(monkeypatch, tmp_path, old_instance=True) is _SYSTEM_PROMPT


@pytest.mark.parametrize("on", [True, False])
def test_pi_argv_follows_set_chat_recall(monkeypatch, on):
    import json
    from pathlib import Path

    from llm_backend.base import CHAT_RECALL_RULE, Message
    from llm_backend.pi import _SYSTEM_PROMPT_PI, PiCodingAgentBackend

    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("llm_backend.pi.repo_root", lambda: Path("/repo"))
    backend = PiCodingAgentBackend({})
    backend.set_chat_recall(on)
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _fake_proc([json.dumps({"type": "agent_end"}).encode() + b"\n"])

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    list(backend.stream([Message(role="user", content="hello")]))
    value = captured["cmd"][captured["cmd"].index("--append-system-prompt") + 1]
    if on:
        assert value == _SYSTEM_PROMPT_PI + "\n" + CHAT_RECALL_RULE
    else:
        assert value is _SYSTEM_PROMPT_PI


def test_codex_agents_md_follows_set_chat_recall(monkeypatch, tmp_path):
    import json
    from unittest.mock import MagicMock

    from llm_backend import codex as codex_mod
    from llm_backend.base import CHAT_RECALL_RULE, Message, compose_system_prompt
    from llm_backend.codex import _SYSTEM_PROMPT_CODEX, CodexBackend

    events = [{"type": "thread.started", "thread_id": "th-1"},
              {"type": "turn.completed", "usage": {}}]

    class _Popen:
        def __init__(self, cmd, **kwargs):
            self.stdin = MagicMock()
            self.stdout = iter([(json.dumps(e) + "\n").encode() for e in events])
            self.stderr = iter([])
            self.returncode = 0

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(codex_mod.subprocess, "Popen", _Popen)
    b = CodexBackend({"bin": str(tmp_path / "codex.exe"), "cwd": str(tmp_path)})
    agents = tmp_path / "AGENTS.md"

    b.set_chat_recall(True)
    b.set_persona("P")
    list(b.stream([Message(role="user", content="hi")]))
    assert agents.read_text(encoding="utf-8") == compose_system_prompt(
        _SYSTEM_PROMPT_CODEX + "\n" + CHAT_RECALL_RULE, "P")

    # 無効に戻すと、次のターンで素の定数に書き戻る。
    b.set_chat_recall(False)
    b.set_persona("")
    list(b.stream([Message(role="user", content="next")]))
    assert agents.read_text(encoding="utf-8") == _SYSTEM_PROMPT_CODEX


def _openai():
    from llm_backend.openai_compat import OpenAICompatBackend
    return OpenAICompatBackend(base_url="http://x", api_key="", model="m")


def test_openai_payload_with_recall():
    from llm_backend.base import CHAT_RECALL_RULE, PERSONA_HEADER, Message

    msgs = [Message(role="system", content="SYS"), Message(role="user", content="hi")]
    b = _openai()
    b.set_chat_recall(True)
    assert b._payload_messages(msgs)[0] == {"role": "system",
                                             "content": "SYS\n" + CHAT_RECALL_RULE}
    assert msgs[0].content == "SYS"                      # 保存側の Message は変異しない
    b.set_chat_dataset("dsA")
    b.set_persona("P")
    content = b._payload_messages(msgs)[0]["content"]
    assert content.startswith("SYS")
    assert (content.index("## Chat dataset") < content.index(CHAT_RECALL_RULE)
            < content.index(PERSONA_HEADER))
    # system が無ければ、規則だけの system を先頭に入れる。
    b2 = _openai()
    b2.set_chat_recall(True)
    pl = b2._payload_messages([Message(role="user", content="hi")])
    assert pl[0] == {"role": "system", "content": CHAT_RECALL_RULE}
    assert pl[1] == {"role": "user", "content": "hi"}


def test_openai_payload_without_recall_is_byte_identical(monkeypatch):
    import json

    from llm_backend.base import Message

    msgs = [Message(role="system", content="SYS"), Message(role="user", content="hi")]
    b = _openai()
    b.set_chat_recall(False)
    assert b._payload_messages(msgs) == [m.to_payload() for m in msgs]
    old = _openai()
    del old._chat_recall                                  # ホットリロード前の旧インスタンス
    assert old._payload_messages(msgs) == [m.to_payload() for m in msgs]

    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __iter__(self):
            return iter([b"data: [DONE]\n"])

    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    list(b.stream(msgs))
    assert captured["data"] == json.dumps({
        "model": "m", "messages": [m.to_payload() for m in msgs], "stream": True,
    }).encode("utf-8")
