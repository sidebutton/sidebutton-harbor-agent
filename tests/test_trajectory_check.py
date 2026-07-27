"""AC3 evidence: the trajectory checker finds the self-review turn — and, more
importantly, refuses to find one when the discipline was skipped.

Fixtures are built from harbor's own ATIF models (``Trajectory``/``Step``) rather
than hand-written JSON, so they are schema-valid by construction and a harbor
schema change surfaces here instead of in a smoke run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harbor.models.trajectories.agent import Agent
from harbor.models.trajectories.content import ContentPart
from harbor.models.trajectories.observation import Observation
from harbor.models.trajectories.observation_result import ObservationResult
from harbor.models.trajectories.step import Step
from harbor.models.trajectories.tool_call import ToolCall
from harbor.models.trajectories.trajectory import Trajectory

from sidebutton_harbor_agent.trajectory_check import (
    DATASET_ID,
    SMOKE_TASK_NAMES,
    analyze_trajectory,
    find_trajectories,
    main,
)

_MODEL = "claude-opus-4-8"


def _step(
    step_id: int,
    message: str = "",
    tool: str | None = None,
    arguments: dict[str, Any] | None = None,
    output: str | None = None,
    source: str = "agent",
) -> Step:
    """One ATIF turn: prose, an optional tool call, and its observed output."""
    call_id = f"call-{step_id}"
    tool_calls = (
        [ToolCall(tool_call_id=call_id, function_name=tool, arguments=arguments or {})]
        if tool
        else None
    )
    observation = (
        Observation(results=[ObservationResult(source_call_id=call_id, content=output)])
        if output is not None
        else None
    )
    return Step(
        step_id=step_id,
        source=source,
        message=message,
        tool_calls=tool_calls,
        observation=observation,
    )


def _write(tmp_path: Path, *steps: Step, name: str = "trajectory.json") -> Path:
    trajectory = Trajectory(
        agent=Agent(name="sidebutton", version="0.1.0+cli.1.5.1", model_name=_MODEL),
        steps=list(steps),
    )
    path = tmp_path / name
    path.write_text(json.dumps(trajectory.to_json_dict()), encoding="utf-8")
    return path


def _edit(step_id: int) -> Step:
    return _step(
        step_id,
        "Writing the fix.",
        tool="Edit",
        arguments={"file_path": "/app/solution.py"},
        output="File updated.",
    )


def test_detects_self_review_turn(tmp_path: Path) -> None:
    """The happy path: edit, then restate the criteria and run the checks."""
    path = _write(
        tmp_path,
        _step(1, "Task received.", source="user"),
        _edit(2),
        _step(3, "Re-reading the task: 3 acceptance criteria — key perms, subject, PEM."),
        _step(
            4,
            "Checking criterion 1.",
            tool="Bash",
            arguments={"command": "stat -c %a /app/ssl/server.key"},
            output="600",
        ),
        _step(5, "All criteria verified against real output. Done."),
    )
    report = analyze_trajectory(path)

    assert report.self_review_turn_present
    assert report.last_mutation_step_id == 2
    assert report.verification_step_ids == [4]
    assert report.criteria_step_ids == [3, 4, 5]
    assert "3 acceptance criteria" in report.excerpt


def test_criteria_before_the_last_fix_are_still_reported_as_context(tmp_path: Path) -> None:
    """The loop says "if a check fails, fix it and verify again", so a real
    self-review often opens *before* the last edit. The verdict stays keyed to the
    post-edit window, but the opening enumeration is reported so an operator can
    lift the fullest excerpt from the trajectory."""
    path = _write(
        tmp_path,
        _edit(1),
        _step(2, "Re-reading the task instruction: 6 acceptance criteria to check."),
        _step(3, "Check.", tool="Bash", arguments={"command": "ls /app/ssl"}, output="server.key"),
        _edit(4),  # a fix found by the self-review
        _step(5, "Re-checking the criterion I just fixed."),
        _step(6, "Verify.", tool="Bash", arguments={"command": "cat /app/ssl/x.txt"}, output="ok"),
    )
    report = analyze_trajectory(path)

    assert report.self_review_turn_present
    assert report.last_mutation_step_id == 4
    assert report.criteria_step_ids == [5]
    assert report.criteria_step_ids_any == [2, 5]


def test_no_review_when_agent_edits_then_declares_done(tmp_path: Path) -> None:
    """The near-miss failure mode this deliverable exists to prevent: the last
    action is an edit and the agent reports success without running anything."""
    path = _write(
        tmp_path,
        _step(1, "Task received.", source="user"),
        _step(2, "Reading.", tool="Read", arguments={"file_path": "/app/x.py"}, output="..."),
        _edit(3),
        _step(4, "The fix is complete and should satisfy the task."),
    )
    report = analyze_trajectory(path)

    assert not report.self_review_turn_present
    assert report.last_mutation_step_id == 3
    assert report.verification_step_ids == []


def test_verification_before_the_last_edit_does_not_count(tmp_path: Path) -> None:
    """Checks that ran before the final edit never exercised the shipped code."""
    path = _write(
        tmp_path,
        _step(
            1,
            "Verifying the criteria first.",
            tool="Bash",
            arguments={"command": "pytest -q"},
            output="1 passed",
        ),
        _edit(2),
        _step(3, "Tweaked one more thing; done."),
    )
    report = analyze_trajectory(path)

    assert not report.self_review_turn_present
    assert report.verification_step_ids == []
    assert report.criteria_step_ids == []


def test_asserted_success_without_observed_output_does_not_count(tmp_path: Path) -> None:
    """"Run the thing, don't assert": criteria language alone is not evidence."""
    path = _write(
        tmp_path,
        _edit(1),
        _step(2, "Walking the checklist: every requirement is satisfied by the change."),
    )
    report = analyze_trajectory(path)

    assert not report.self_review_turn_present
    assert report.criteria_step_ids == [2]
    assert report.verification_step_ids == []


def test_scratch_redirect_is_not_treated_as_an_edit(tmp_path: Path) -> None:
    """A verification command that tees its output to /tmp is still verification."""
    path = _write(
        tmp_path,
        _edit(1),
        _step(2, "Re-reading the task requirements before finishing."),
        _step(
            3,
            "Running the check.",
            tool="Bash",
            arguments={"command": "python3 /app/check_cert.py > /tmp/out.log"},
            output="Certificate verification successful",
        ),
    )
    report = analyze_trajectory(path)

    assert report.last_mutation_step_id == 1
    assert report.self_review_turn_present


def test_shell_write_counts_as_an_edit(tmp_path: Path) -> None:
    """A heredoc/redirect into the solution is an edit, not an observation."""
    path = _write(
        tmp_path,
        _step(1, "Re-reading the task requirements."),
        _step(
            2,
            "Writing the answer.",
            tool="Bash",
            arguments={"command": "printf '%s' '\\d{4}' > /app/regex.txt"},
            output="",
        ),
    )
    report = analyze_trajectory(path)

    assert report.last_mutation_step_id == 2
    assert not report.self_review_turn_present


def test_multimodal_message_text_is_read(tmp_path: Path) -> None:
    """ATIF-v1.6 ContentPart arrays carry the prose the cues are matched against."""
    step = _step(2, "")
    step.message = [
        ContentPart(type="text", text="Re-reading each acceptance criterion.")
    ]
    path = _write(
        tmp_path,
        _edit(1),
        step,
        _step(
            3,
            "Check.",
            tool="Bash",
            arguments={"command": "python3 -m pytest -q"},
            output="2 passed",
        ),
    )
    report = analyze_trajectory(path)

    assert report.criteria_step_ids == [2]
    assert report.self_review_turn_present


def test_find_trajectories_walks_a_job_directory(tmp_path: Path) -> None:
    """Point the checker at a whole harbor job dir: ``<trial>/agent/trajectory.json``."""
    for trial in ("regex-log.1", "openssl-selfsigned-cert.1"):
        agent_dir = tmp_path / "job" / trial / "agent"
        agent_dir.mkdir(parents=True)
        _write(agent_dir, _edit(1), _step(2, "Verifying the criteria."))

    found = find_trajectories(tmp_path / "job")

    assert len(found) == 2
    assert all(p.name == "trajectory.json" for p in found)


def test_unreadable_trajectory_reports_an_error_not_a_traceback(tmp_path: Path) -> None:
    broken = tmp_path / "trajectory.json"
    broken.write_text("{not json", encoding="utf-8")

    report = analyze_trajectory(broken)

    assert not report.self_review_turn_present
    assert "unreadable" in report.error
    assert "FAIL" in report.render_human()


def test_cli_exit_codes_and_json(tmp_path: Path, capsys) -> None:
    good = _write(
        tmp_path,
        _edit(1),
        _step(2, "Re-reading the criteria."),
        _step(3, "Check.", tool="Bash", arguments={"command": "ls /app"}, output="ok"),
        name="good.json",
    )
    bad = _write(tmp_path, _edit(1), _step(2, "Done."), name="bad.json")

    assert main([str(good)]) == 0
    assert main([str(bad)]) == 1
    assert main([str(tmp_path / "nope")]) == 2

    capsys.readouterr()  # discard the human-mode output of the calls above
    assert main([str(good), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["self_review_turn_present"] is True
    assert payload[0]["verification_step_ids"] == [3]


def test_documented_smoke_tasks_are_a_small_named_set() -> None:
    """AC3 asks for 2-3 sample tasks; the ids are pinned here as the doc's source
    of truth (``tests/test_docs.py`` checks the README against them)."""
    assert 2 <= len(SMOKE_TASK_NAMES) <= 3
    assert len(set(SMOKE_TASK_NAMES)) == len(SMOKE_TASK_NAMES)
    assert DATASET_ID.count("/") == 1, "harbor needs org/name, not a bare dataset name"
