"""The documented smoke command must stay runnable.

The AC3 smoke block previously shipped ``--dataset terminal-bench-2-1
--include-task-name hello-world``: harbor rejects the slash-less dataset name
("Dataset 'terminal-bench-2-1' (version: 'None') not found") and no task named
``hello-world`` exists in the 89-task dataset, so running the block verbatim
matched nothing. Nothing caught it because the command is prose. These tests pin
the README against the code's own constants.
"""

from __future__ import annotations

import re
from pathlib import Path

from sidebutton_harbor_agent.trajectory_check import DATASET_ID, SMOKE_TASK_NAMES

_README = Path(__file__).resolve().parent.parent / "README.md"


def _readme() -> str:
    return _README.read_text(encoding="utf-8")


def test_documented_dataset_is_the_resolvable_org_name_form() -> None:
    """Every ``--dataset`` in the docs uses the ``org/name`` id harbor resolves."""
    datasets = re.findall(r"--dataset\s+(\S+)", _readme())

    assert datasets, "README no longer documents a --dataset invocation"
    for dataset in datasets:
        assert dataset == DATASET_ID, (
            f"--dataset {dataset!r} is not resolvable; harbor reads a slash-less "
            f"name as a legacy registry dataset. Use {DATASET_ID!r}."
        )


def _documented_task_names() -> list[str]:
    """Task ids the README tells an operator to run — from the ``for task in …``
    list and from any literal ``--include-task-name`` (shell vars skipped)."""
    readme = _readme()
    names = [
        name
        for match in re.findall(r"^\s*for task in ([^;\n]+); do", readme, re.MULTILINE)
        for name in match.split()
    ]
    names += [
        name.strip("\"'")
        for name in re.findall(r"--include-task-name\s+(\S+)", readme)
        if "$" not in name
    ]
    return names


def test_documented_smoke_tasks_exist_in_the_dataset() -> None:
    """``--include-task-name`` values come from the verified set, not an invented id."""
    task_names = _documented_task_names()

    assert task_names, "README no longer documents the smoke tasks to run"
    for name in task_names:
        assert name in SMOKE_TASK_NAMES, (
            f"task id {name!r} is not in the dataset-verified SMOKE_TASK_NAMES; "
            "confirm it with `harbor datasets download` before documenting it."
        )
    assert set(task_names) == set(SMOKE_TASK_NAMES), (
        "README and SMOKE_TASK_NAMES disagree on which tasks the smoke covers"
    )


def test_readme_documents_the_self_review_evidence_requirement() -> None:
    """AC3 is about the *visible self-review turn*, not just a finished trial —
    the smoke section has to tell the runner to capture it."""
    readme = _readme().lower()

    assert "self-review" in readme
    assert "sidebutton-harbor-agent-check-trajectory" in readme
