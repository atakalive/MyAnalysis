"""Issue #65: claude backend system-prompt flag toggle (headless, no Qt/claude)."""
from llm_backend.claude_code import _system_prompt_args, ClaudeCodeBackend, _SYSTEM_PROMPT


def test_system_prompt_args_provider_default():
    assert _system_prompt_args(True) == ["--append-system-prompt", _SYSTEM_PROMPT]


def test_system_prompt_args_replace():
    assert _system_prompt_args(False) == ["--system-prompt", _SYSTEM_PROMPT]


def test_default_uses_provider_prompt():
    assert ClaudeCodeBackend({})._use_provider_system_prompt is True


def test_config_disables_provider_prompt():
    b = ClaudeCodeBackend({"use_provider_system_prompt": False})
    assert b._use_provider_system_prompt is False


def test_setter_flips_attribute():
    b = ClaudeCodeBackend({})
    b.set_use_provider_system_prompt(False)
    assert b._use_provider_system_prompt is False
