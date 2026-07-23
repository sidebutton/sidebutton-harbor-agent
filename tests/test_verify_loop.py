"""B3 / SCRUM-1836: the in-container verify-before-done guidance is present,
domain-general (campaign fairness), and free of developer-only leakage.

These pin the *content* of ``config/CLAUDE.md``; ``test_contract.py`` pins the
*mechanism* that injects it. Together they cover AC1 (loaded by the adapter) and
AC2 (fairness review) in-repo; AC3 is the operator smoke run (see README).
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path

from sidebutton_harbor_agent import SidebuttonAgent

_SENTINEL = "<<<TASK-INSTRUCTION>>>"


def _verify_text(tmp_path: Path) -> str:
    """The verify-loop guidance as actually injected, with the task text peeled off."""
    agent = SidebuttonAgent(logs_dir=tmp_path, model_name="anthropic/claude-opus-4-8")
    rendered = agent.render_instruction(_SENTINEL)
    assert rendered.startswith(_SENTINEL)
    return rendered[len(_SENTINEL) :].strip()


def test_three_content_pillars_present(tmp_path: Path) -> None:
    """The deliverable's three pillars all ship in the injected guidance."""
    text = _verify_text(tmp_path).lower()
    # Pillar 1 — self-review against the task's own acceptance criteria.
    assert "re-read the task" in text
    assert "acceptance criter" in text  # criterion / criteria
    # Pillar 2 — reproduce-before-fix for bug-shaped tasks.
    assert "reproduce before you fix" in text
    # Pillar 3 — hidden tests exist; your own verification is the only signal.
    assert "hidden tests" in text
    assert "only signal" in text


def test_injection_marker_preserved(tmp_path: Path) -> None:
    # test_contract.py pins this heading as the injection marker; guard it here
    # too so a future content rewrite cannot silently break that contract.
    assert "Before you finish" in _verify_text(tmp_path)


#: Tokens that would signal task-specific or benchmark-internal knowledge leaking
#: into the agent-facing guidance (plan §5.5 / §6 fairness lines).
FORBIDDEN_FAIRNESS_TOKENS = (
    "terminal-bench",
    "oracle",
    "tests/",
    "harbor",
    "solution.sh",
    "task.yaml",
)


def test_no_task_specific_or_benchmark_references(tmp_path: Path) -> None:
    """AC2 / §5.5 / §6: domain-general only — no benchmark-repo, oracle solutions,
    grader-dir, or task-keyed references in the agent-facing guidance."""
    text = _verify_text(tmp_path).lower()
    for token in FORBIDDEN_FAIRNESS_TOKENS:
        assert token not in text, f"fairness: forbidden token {token!r} in verify loop"


def test_packaged_config_is_comment_free() -> None:
    """N1: the shipped file carries no HTML comments (dev-only prose that the
    loader would otherwise inject verbatim into the prompt)."""
    packaged = importlib.resources.files("sidebutton_harbor_agent").joinpath(
        "config", "CLAUDE.md"
    )
    assert "<!--" not in packaged.read_text(encoding="utf-8")


def test_sanitizer_strips_html_comments() -> None:
    """N1 unit: the loader's sanitizer removes HTML comments and trims."""
    sanitized = SidebuttonAgent._sanitize_verify_loop_text(
        "<!-- dev only: SECRET note\n  spanning lines -->\n## Before you finish\nBody.\n"
    )
    assert sanitized == "## Before you finish\nBody."
    assert "SECRET" not in sanitized


def test_html_comment_never_reaches_prompt(tmp_path: Path) -> None:
    """N1 integration: a comment in a custom verify-loop file does not leak into
    the rendered instruction."""
    custom = tmp_path / "custom.md"
    custom.write_text(
        "<!-- LEAKME internal note -->\n## Before you finish\nDo the thing.\n",
        encoding="utf-8",
    )
    agent = SidebuttonAgent(
        logs_dir=tmp_path,
        model_name="anthropic/claude-opus-4-8",
        verify_loop_path=str(custom),
    )
    rendered = agent.render_instruction("do X")
    assert "LEAKME" not in rendered
    assert "<!--" not in rendered
    assert "Do the thing." in rendered
