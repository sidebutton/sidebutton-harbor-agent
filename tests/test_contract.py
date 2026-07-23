"""Contract + API-drift smoke: the adapter resolves, instantiates, and still
lines up with the Harbor ``ClaudeCode`` base it depends on.

If Harbor renames/moves the hooks this adapter reuses, these tests fail loudly
(rather than the adapter silently misbehaving inside a container)."""

from __future__ import annotations

import importlib
from pathlib import Path

from harbor.agents.installed.base import BaseInstalledAgent
from harbor.agents.installed.claude_code import ClaudeCode

from sidebutton_harbor_agent import SidebuttonAgent


def test_import_path_resolves_to_class() -> None:
    module_name, _, class_name = "sidebutton_harbor_agent:SidebuttonAgent".partition(":")
    resolved = getattr(importlib.import_module(module_name), class_name)
    assert resolved is SidebuttonAgent
    assert SidebuttonAgent.import_path() == "sidebutton_harbor_agent:SidebuttonAgent"


def test_identity(tmp_path: Path) -> None:
    assert SidebuttonAgent.name() == "sidebutton"
    agent = SidebuttonAgent(logs_dir=tmp_path, model_name="anthropic/claude-opus-4-8")
    assert agent.version() == "0.1.0+cli.1.5.1"


def test_is_a_harbor_installed_agent() -> None:
    assert issubclass(SidebuttonAgent, ClaudeCode)
    assert issubclass(SidebuttonAgent, BaseInstalledAgent)


def test_atif_trajectory_support_preserved() -> None:
    # Subclassing ClaudeCode is what keeps ATIF trajectory emission (mandatory
    # for submission). A regression here means the inheritance broke.
    assert SidebuttonAgent.SUPPORTS_ATIF is True


def test_inherited_hooks_still_exist(tmp_path: Path) -> None:
    agent = SidebuttonAgent(logs_dir=tmp_path, model_name="anthropic/claude-opus-4-8")
    for hook in (
        "build_cli_flags",
        "_build_register_skills_command",
        "_build_register_mcp_servers_command",
        "_resolved_env_vars",
        "render_instruction",
    ):
        assert hasattr(agent, hook), f"Harbor API drift: missing {hook!r}"


def test_effort_and_append_flags_still_mapped() -> None:
    by_kwarg = {flag.kwarg: flag for flag in SidebuttonAgent.CLI_FLAGS}
    assert by_kwarg["reasoning_effort"].cli == "--effort"
    assert "high" in (by_kwarg["reasoning_effort"].choices or [])
    # The verify-loop rides the instruction, but confirm the base still models
    # the append-system-prompt flag we deliberately do *not* use for it.
    assert "append_system_prompt" in by_kwarg


def test_verify_loop_appended_to_instruction(tmp_path: Path) -> None:
    agent = SidebuttonAgent(logs_dir=tmp_path, model_name="anthropic/claude-opus-4-8")
    rendered = agent.render_instruction("Fix the failing test in app/foo.py")
    assert "Fix the failing test in app/foo.py" in rendered
    assert "Before you finish" in rendered  # from config/CLAUDE.md


def test_verify_loop_can_be_disabled(tmp_path: Path) -> None:
    agent = SidebuttonAgent(
        logs_dir=tmp_path,
        model_name="anthropic/claude-opus-4-8",
        verify_loop=False,
    )
    assert agent.render_instruction("do X") == "do X"


def test_verify_loop_disabled_via_string_kwarg(tmp_path: Path) -> None:
    # --agent-kwarg values arrive as strings; "false" must disable, not read truthy.
    agent = SidebuttonAgent(
        logs_dir=tmp_path,
        model_name="anthropic/claude-opus-4-8",
        verify_loop="false",
    )
    assert agent.render_instruction("do X") == "do X"
