"""AC1: config assembly + param plumbing (model / effort / API key / env)."""

from __future__ import annotations

from pathlib import Path

from sidebutton_harbor_agent import SidebuttonAgent


def _agent(tmp_path: Path, **kwargs) -> SidebuttonAgent:
    return SidebuttonAgent(logs_dir=tmp_path, **kwargs)


def test_model_maps_to_anthropic_model_stripping_provider(tmp_path: Path) -> None:
    inv = _agent(tmp_path, model_name="anthropic/claude-opus-4-8").build_invocation()
    # Official Anthropic API path strips the "provider/" prefix.
    assert inv.env["ANTHROPIC_MODEL"] == "claude-opus-4-8"
    assert inv.model == "anthropic/claude-opus-4-8"


def test_bare_model_name_passthrough(tmp_path: Path) -> None:
    inv = _agent(tmp_path, model_name="claude-opus-4-8").build_invocation()
    assert inv.env["ANTHROPIC_MODEL"] == "claude-opus-4-8"


def test_effort_maps_to_effort_flag(tmp_path: Path) -> None:
    inv = _agent(
        tmp_path, model_name="anthropic/claude-opus-4-8", reasoning_effort="high"
    ).build_invocation()
    assert "--effort high" in inv.cli_flags
    assert inv.command.startswith("claude --verbose --output-format=stream-json")
    assert inv.command.endswith("--print")


def test_effort_absent_when_unset(tmp_path: Path) -> None:
    inv = _agent(tmp_path, model_name="anthropic/claude-opus-4-8").build_invocation()
    assert "--effort" not in inv.cli_flags


def test_api_key_passthrough_via_extra_env(tmp_path: Path) -> None:
    inv = _agent(
        tmp_path,
        model_name="anthropic/claude-opus-4-8",
        extra_env={"ANTHROPIC_API_KEY": "sk-test-123"},
    ).build_invocation()
    assert inv.env["ANTHROPIC_API_KEY"] == "sk-test-123"


def test_max_thinking_tokens_env_var(tmp_path: Path) -> None:
    inv = _agent(
        tmp_path, model_name="anthropic/claude-opus-4-8", max_thinking_tokens=4096
    ).build_invocation()
    assert inv.env["MAX_THINKING_TOKENS"] == "4096"


def test_permission_mode_defaults_to_bypass(tmp_path: Path) -> None:
    # Inherited ClaudeCode default — required so the CLI runs non-interactively
    # inside the container.
    inv = _agent(tmp_path, model_name="anthropic/claude-opus-4-8").build_invocation()
    assert "--permission-mode=bypassPermissions" in inv.cli_flags


def test_no_verifier_timeout_or_resource_overrides(tmp_path: Path) -> None:
    """Fairness: the adapter must emit no verifier/timeout/resource overrides."""
    inv = _agent(
        tmp_path, model_name="anthropic/claude-opus-4-8", reasoning_effort="max"
    ).build_invocation()
    assert inv.validate() == []
    blob = f"{inv.command} {inv.cli_flags} {' '.join(inv.setup_commands)}"
    for forbidden in ("--verifier", "--timeout", "--cpus", "--memory", "--gpus"):
        assert forbidden not in blob


def test_safety_env_constants_present(tmp_path: Path) -> None:
    inv = _agent(tmp_path, model_name="anthropic/claude-opus-4-8").build_invocation()
    assert inv.env["IS_SANDBOX"] == "1"
    assert inv.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
