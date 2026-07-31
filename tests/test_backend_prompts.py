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

    マウント上ではエージェントの Write/Edit が PreToolUse hook で機械的に拒否される
    ので、これが載っていないとエージェントはメモもレポートも書けなくなる。
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
    assert MOUNT_SAFE_EDITS in _SYSTEM_PROMPT
