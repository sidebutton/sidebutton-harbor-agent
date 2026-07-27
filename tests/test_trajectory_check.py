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


def _multi_step(
    step_id: int,
    message: str,
    calls: list[tuple[str, dict[str, Any], str | None]],
) -> Step:
    """One ATIF turn carrying several tool calls, as harbor emits them.

    Harbor bundles every ``tool_use`` of one LLM inference into a single step
    (RFC-0001), so "fix it and re-run the check" arrives as one step with an
    ``Edit`` followed by a ``Bash`` — not as two steps.
    """
    tool_calls = [
        ToolCall(tool_call_id=f"call-{step_id}-{i}", function_name=name, arguments=args)
        for i, (name, args, _) in enumerate(calls)
    ]
    results = [
        ObservationResult(source_call_id=f"call-{step_id}-{i}", content=output)
        for i, (_, _, output) in enumerate(calls)
        if output is not None
    ]
    return Step(
        step_id=step_id,
        source="agent",
        message=message,
        tool_calls=tool_calls,
        observation=Observation(results=results) if results else None,
    )


def test_stderr_redirect_is_not_an_edit(tmp_path: Path) -> None:
    """``2>&1`` is fd plumbing, not a write.

    The bare redirect branch used to match it, so `pytest -q 2>&1 | tail` — the
    most common verification idiom there is — was scored as the last edit and a
    healthy self-review turn was reported as a FAIL.
    """
    path = _write(
        tmp_path,
        _edit(1),
        _step(2, "Re-reading the task: 3 acceptance criteria."),
        _step(
            3,
            "Running the suite.",
            tool="Bash",
            arguments={"command": "python3 -m pytest -q 2>&1 | tail -5"},
            output="3 passed",
        ),
    )
    report = analyze_trajectory(path)

    assert report.last_mutation_step_id == 1
    assert report.verification_step_ids == [3]
    assert report.self_review_turn_present


def test_check_batched_into_the_editing_turn_counts(tmp_path: Path) -> None:
    """A turn that edits and then re-runs the check verifies the shipped code."""
    path = _write(
        tmp_path,
        _step(1, "Reading.", tool="Read", arguments={"file_path": "/app/s.py"}, output="..."),
        _multi_step(
            2,
            "Re-reading each acceptance criterion; fixing the off-by-one and re-running.",
            [
                ("Edit", {"file_path": "/app/s.py"}, "File updated."),
                ("Bash", {"command": "python -m pytest -q"}, "3 passed"),
            ],
        ),
    )
    report = analyze_trajectory(path)

    assert report.last_mutation_step_id == 2
    assert report.verification_step_ids == [2]
    assert report.criteria_step_ids == [2]
    assert report.self_review_turn_present


def test_check_before_the_edit_inside_one_turn_does_not_count(tmp_path: Path) -> None:
    """Call order inside the turn decides: a check that ran *before* the edit in
    the same step never exercised the shipped code."""
    path = _write(
        tmp_path,
        _multi_step(
            1,
            "Checking the criteria, then patching what failed.",
            [
                ("Bash", {"command": "python -m pytest -q"}, "1 failed"),
                ("Edit", {"file_path": "/app/s.py"}, "File updated."),
            ],
        ),
        _step(2, "That should do it. Done."),
    )
    report = analyze_trajectory(path)

    assert report.last_mutation_step_id == 1
    assert report.verification_step_ids == []
    assert not report.self_review_turn_present


def test_agent_bookkeeping_tools_are_not_verification(tmp_path: Path) -> None:
    """The near-miss this tool exists to catch, in its most common disguise:
    ``TodoWrite`` returns output but observes nothing about the container."""
    path = _write(
        tmp_path,
        _edit(1),
        _step(
            2,
            "Marking the checklist done.",
            tool="TodoWrite",
            arguments={"todos": []},
            output="Todos have been modified successfully.",
        ),
        _step(3, "Every requirement is satisfied by the change. Done."),
    )
    report = analyze_trajectory(path)

    assert report.verification_step_ids == []
    assert not report.self_review_turn_present


def test_quiet_check_with_empty_output_counts_as_observed(tmp_path: Path) -> None:
    """Silence is success for ``diff``/``cmp``/``grep -q``/``test`` — harbor only
    emits a result when the tool returned, so ``""`` means "ran and printed
    nothing", not "never ran"."""
    path = _write(
        tmp_path,
        _edit(1),
        _step(2, "Re-reading each requirement."),
        _step(
            3,
            "Comparing against the expected output.",
            tool="Bash",
            arguments={"command": "diff /app/out.txt /app/expected.txt"},
            output="",
        ),
    )
    report = analyze_trajectory(path)

    assert report.verification_step_ids == [3]
    assert report.self_review_turn_present


def test_output_flag_write_is_an_edit(tmp_path: Path) -> None:
    """A file created via an output *flag* rather than a redirect is still an
    edit — otherwise a rewrite after the check widens the window and the shipped
    artifact is never verified. ``openssl -out`` is the shape the flagship smoke
    task (``openssl-selfsigned-cert``) produces."""
    path = _write(
        tmp_path,
        _step(
            1,
            "Checking the 6 acceptance criteria.",
            tool="Bash",
            arguments={"command": "openssl x509 -in /app/cert.pem -noout -subject"},
            output="subject=CN=example.com",
        ),
        _step(
            2,
            "Validity is wrong, regenerating the certificate.",
            tool="Bash",
            arguments={
                "command": (
                    "openssl req -x509 -newkey rsa:4096 -keyout /app/key.pem "
                    "-out /app/cert.pem -nodes -days 365 -subj '/CN=example.com'"
                )
            },
            output="writing new private key to '/app/key.pem'",
        ),
        _step(3, "Done."),
    )
    report = analyze_trajectory(path)

    assert report.last_mutation_step_id == 2
    assert not report.self_review_turn_present


def test_dependency_chatter_is_not_a_criteria_restatement(tmp_path: Path) -> None:
    """Cues match whole words: ``requirements.txt`` is not a self-review, and the
    excerpt attached as AC3 evidence must not be a pip line."""
    path = _write(
        tmp_path,
        _edit(1),
        _step(
            2,
            "Installing dependencies from requirements.txt now.",
            tool="Bash",
            arguments={"command": "cat /app/requirements.txt"},
            output="numpy==2.1.0",
        ),
    )
    report = analyze_trajectory(path)

    assert report.criteria_step_ids == []
    assert not report.self_review_turn_present
    assert report.excerpt == ""


def test_malformed_steps_report_an_error_not_a_traceback(tmp_path: Path) -> None:
    """A steps array holding no objects used to reach ``max()`` on an empty
    sequence, aborting a whole job sweep with a traceback."""
    broken = tmp_path / "trajectory.json"
    broken.write_text(json.dumps({"steps": ["oops", None]}), encoding="utf-8")

    report = analyze_trajectory(broken)

    assert not report.self_review_turn_present
    assert "no object-shaped steps" in report.error


def test_missing_target_does_not_swallow_the_others(tmp_path: Path, capsys) -> None:
    """One errored trial with no trajectory must not hide the trials named after
    it — an operator would read the run as "checked"."""
    good = _write(
        tmp_path,
        _edit(1),
        _step(2, "Re-reading the criteria."),
        _step(3, "Check.", tool="Bash", arguments={"command": "ls /app"}, output="ok"),
        name="good.json",
    )

    exit_code = main([str(tmp_path / "nope"), str(good)])
    captured = capsys.readouterr()

    assert exit_code == 1  # missing evidence never reads as clean
    assert "no trajectory.json found under" in captured.err
    assert "PASS" in captured.out


def test_excerpt_can_be_suppressed(tmp_path: Path, capsys) -> None:
    """``--excerpt-chars 0`` means no excerpt, as the help text promises."""
    path = _write(
        tmp_path,
        _edit(1),
        _step(2, "Re-reading the criteria."),
        _step(3, "Check.", tool="Bash", arguments={"command": "ls /app"}, output="ok"),
    )

    assert main([str(path), "--excerpt-chars", "0"]) == 0
    captured = capsys.readouterr()

    assert "self-review excerpt" not in captured.out
    assert "…" not in captured.out


def test_documented_smoke_tasks_are_a_small_named_set() -> None:
    """AC3 asks for 2-3 sample tasks; the ids are pinned here as the doc's source
    of truth (``tests/test_docs.py`` checks the README against them)."""
    assert 2 <= len(SMOKE_TASK_NAMES) <= 3
    assert len(set(SMOKE_TASK_NAMES)) == len(SMOKE_TASK_NAMES)
    assert DATASET_ID.count("/") == 1, "harbor needs org/name, not a bare dataset name"
