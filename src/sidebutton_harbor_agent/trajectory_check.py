"""Check that the verify-before-done loop visibly executed in an ATIF trajectory.

This is the AC3 evidence step of the verify-loop deliverable (B3): a smoke run
only proves the loop works if the trajectory *shows* the self-review turn — the
agent re-stating the task's criteria and checking them against real output
**after** its last edit and **before** it reports done. Eyeballing a 200-step
trajectory for that is unreliable, so this reduces it to one command:

    $ sidebutton-harbor-agent-check-trajectory jobs/<job>/<trial>/agent/trajectory.json
    $ sidebutton-harbor-agent-check-trajectory jobs/<job>          # every trial

Host-side only. It reads a trajectory that harbor already wrote; it never ships
into a task container, never touches the verifier, the dataset, or the run, and
has no effect on a reward. It is submission QA on our own output.

The verdict is a *signal*, not a grade: the two signals below are keyword and
tool-call heuristics over free-form agent text, so the command always prints the
excerpt it matched. Attach the excerpt as the evidence; use the exit code to
catch the obvious miss (agent edited, then declared done without running
anything).

Two properties the heuristics are deliberately built around, because getting
either wrong inverts the verdict on a real run:

* **A step is a turn, not an action.** Harbor bundles every ``tool_use`` of one
  LLM inference into a single ATIF step (RFC-0001), so the common "fix it and
  re-run the check" turn is *one* step holding both. Mutation and verification
  are therefore resolved per tool call, in call order, not per step.
* **Shell text is matched, not parsed.** An over-match narrows the window and
  fails a healthy trajectory; a missed form widens it and passes a lazy one.
  Neither direction is safe, which is why the excerpt is always printed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Task names used by the documented smoke run. Single source of truth: the
#: README smoke block is checked against this list (``tests/test_docs.py``) so a
#: task id that does not exist in the dataset cannot drift into the docs again.
#: Verified present in ``terminal-bench/terminal-bench-2-1`` (89 tasks) — re-check
#: with ``harbor datasets download terminal-bench/terminal-bench-2-1`` after a
#: dataset bump. Chosen for criteria-dense instructions (so a self-review turn
#: has something explicit to enumerate) and small images; ``modernize-scientific-stack``
#: is failure-shaped, which exercises the reproduce-before-fix pillar.
SMOKE_TASK_NAMES = (
    "openssl-selfsigned-cert",
    "regex-log",
    "modernize-scientific-stack",
)

#: Canonical Harbor dataset id (``org/name``). The bare name ``terminal-bench-2-1``
#: is *not* resolvable — harbor reads a slash-less ``--dataset`` as a legacy
#: registry name and fails with "Dataset not found".
DATASET_ID = "terminal-bench/terminal-bench-2-1"

#: Tool calls that change the container. A step using one of these is an edit,
#: never verification (matched case-insensitively against ``function_name``).
MUTATION_TOOLS = frozenset(
    {
        "write",
        "edit",
        "multiedit",
        "notebookedit",
        "create",
        "patch",
        "apply_patch",
        "str_replace_editor",
    }
)

#: Tools that return output but observe nothing about the container: they report
#: on the agent's own bookkeeping or the network. Running one after an edit is
#: *not* "run the thing and read the output", so they never satisfy the
#: verification signal. ``TodoWrite`` in particular closes almost every Claude
#: Code task, and counting it would pass the exact miss this tool exists to catch.
NON_OBSERVING_TOOLS = frozenset(
    {
        "todowrite",
        "todoread",
        "exitplanmode",
        "websearch",
        "webfetch",
    }
)

#: Shell fragments that make a command a mutation rather than an observation.
#: Deliberately short and readable — this cannot be a shell parser, and it errs
#: in both directions: a missed form widens the self-review window (a lenient
#: verdict), an over-match narrows it (a false FAIL). Both are why the command
#: always prints the excerpt and the docs call the verdict a signal.
_MUTATING_SHELL_RE = re.compile(
    r"""(?:\btee\b
      | \bsed\s+-i
      | \b(?:mv|cp|rm|rmdir|mkdir|touch|chmod|chown|ln|truncate|dd)\b
      | \bpatch\b
      | \bgit\s+(?:commit|apply|am|checkout|switch|merge|cherry-pick|reset|revert|stash)\b
      | \b(?:pip|pip3|npm|apt-get|apk|dnf|yum)\s+(?:install|i|add)\b
      | \b(?:curl|wget)\b[^|;&]*\s-(?:o|O|-output)\b
      | \bopenssl\b[^|;&]*\s-(?:out|keyout)\b
      | \b(?:unzip|gunzip|bunzip2)\b
      | \bmake\b[^|;&]*\binstall\b
      | >>?)""",
    re.VERBOSE,
)

#: File-descriptor duplication (``2>&1``, ``>&2``, ``1>&-``). Plumbing, not a
#: write — but the ``>>?`` branch above would read it as a redirect, which turned
#: `pytest -q 2>&1 | tail` (the most common verification idiom there is) into an
#: "edit" and reported a healthy self-review turn as a FAIL. Stripped first.
#: The digit/``-`` requirement keeps the rare `cmd >& file` form a mutation.
_FD_DUP_RE = re.compile(r"\d*>&\s*(?:\d+|-)")

#: Redirect targets that do not count as mutating the solution (scratch output).
_SCRATCH_REDIRECT_RE = re.compile(r">>?\s*(?:/dev/null|/tmp/|\S*\.log\b)")

#: Cues that an agent turn is enumerating what the task asked for, rather than
#: narrating an edit. Matched case-insensitively against message + reasoning as
#: whole words, so ``requirements.txt`` (dependency chatter, not a criteria
#: restatement) no longer counts as a self-review.
CRITERIA_CUES = (
    "acceptance crit",
    "criterion",
    "criteria",
    "requirement",
    "checklist",
    "re-read",
    "reread",
    "re-reading",
    "the task asked",
    "the task states",
    "the instruction",
    "each item",
    "every item",
)

#: ``CRITERIA_CUES`` as one word-boundary alternation. The trailing guard stops a
#: cue from matching the start of a filename (``requirements.txt``,
#: ``criteria.json``) while still allowing normal prose punctuation.
_CRITERIA_CUE_RE = re.compile(
    r"\b(?:{})\w*\b(?!\.\w)".format("|".join(re.escape(cue) for cue in CRITERIA_CUES)),
    re.IGNORECASE,
)


@dataclass
class TrajectoryReport:
    """Per-trajectory verdict with the step ids and text that produced it."""

    path: Path
    steps: int = 0
    final_step_id: int = 0
    last_mutation_step_id: int = 0
    verification_step_ids: list[int] = field(default_factory=list)
    criteria_step_ids: list[int] = field(default_factory=list)
    criteria_step_ids_any: list[int] = field(default_factory=list)
    excerpt: str = ""
    error: str = ""

    @property
    def self_review_turn_present(self) -> bool:
        """True when, after its last edit, the agent both re-stated the task's
        criteria and ran something whose output it observed."""
        return (
            not self.error
            and bool(self.verification_step_ids)
            and bool(self.criteria_step_ids)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "self_review_turn_present": self.self_review_turn_present,
            "steps": self.steps,
            "final_step_id": self.final_step_id,
            "last_mutation_step_id": self.last_mutation_step_id,
            "verification_step_ids": self.verification_step_ids,
            "criteria_step_ids": self.criteria_step_ids,
            "criteria_step_ids_any": self.criteria_step_ids_any,
            "excerpt": self.excerpt,
            "error": self.error,
        }

    def render_human(self, excerpt_chars: int = 600) -> str:
        if self.error:
            return f"FAIL {self.path}\n  error: {self.error}"
        verdict = "PASS" if self.self_review_turn_present else "FAIL"
        lines = [
            f"{verdict} {self.path}",
            f"  steps: {self.steps} (last step_id {self.final_step_id})",
            f"  last edit at step: {self.last_mutation_step_id or '(none detected)'}",
            f"  ran + observed after last edit: {self.verification_step_ids or '(none)'}",
            f"  restated task criteria after last edit: {self.criteria_step_ids or '(none)'}",
            # An iterative loop ("if a check fails, fix it and verify again") pushes
            # the window past its own opening enumeration; point at that turn too so
            # the operator can lift the fullest excerpt from the trajectory.
            f"  restated task criteria anywhere: {self.criteria_step_ids_any or '(none)'}",
        ]
        if self.excerpt and excerpt_chars > 0:
            body = self.excerpt[:excerpt_chars]
            suffix = "…" if len(self.excerpt) > excerpt_chars else ""
            lines.append("  self-review excerpt:")
            lines.extend(f"    | {line}" for line in f"{body}{suffix}".splitlines())
        return "\n".join(lines)


def _step_text(step: dict[str, Any]) -> str:
    """Flatten a step's agent-authored prose (message + reasoning) to plain text."""
    parts: list[str] = []
    message = step.get("message")
    if isinstance(message, str):
        parts.append(message)
    elif isinstance(message, list):  # ATIF-v1.6 multimodal ContentPart array
        parts.extend(p.get("text") or "" for p in message if isinstance(p, dict))
    reasoning = step.get("reasoning_content")
    if isinstance(reasoning, str):
        parts.append(reasoning)
    return "\n".join(p for p in parts if p)


def _tool_calls(step: dict[str, Any]) -> list[dict[str, Any]]:
    calls = step.get("tool_calls") or []
    return [c for c in calls if isinstance(c, dict)]


def _command_text(call: dict[str, Any]) -> str:
    """Best-effort shell text of a tool call, across agents' argument names."""
    args = call.get("arguments") or {}
    if not isinstance(args, dict):
        return ""
    values = [args.get(k) for k in ("command", "cmd", "script", "code", "input")]
    return "\n".join(v for v in values if isinstance(v, str))


def _tool_name(call: dict[str, Any]) -> str:
    return (call.get("function_name") or "").strip().lower()


def _call_is_mutation(call: dict[str, Any]) -> bool:
    """One tool call changes the container."""
    if _tool_name(call) in MUTATION_TOOLS:
        return True
    command = _command_text(call)
    if not command:
        return False
    scrubbed = _SCRATCH_REDIRECT_RE.sub("", _FD_DUP_RE.sub("", command))
    return bool(_MUTATING_SHELL_RE.search(scrubbed))


def _last_mutation_index(step: dict[str, Any]) -> int:
    """Index of the step's last mutating tool call, or -1 if it has none."""
    return max(
        (i for i, call in enumerate(_tool_calls(step)) if _call_is_mutation(call)),
        default=-1,
    )


def _is_mutation(step: dict[str, Any]) -> bool:
    return _last_mutation_index(step) >= 0


def _observed_call_ids(step: dict[str, Any]) -> set[str]:
    """Ids of the calls the environment actually answered.

    ``content`` is compared against ``None``, not truthiness: harbor only emits a
    result when the tool returned, so ``""`` means "ran and printed nothing" —
    which is success for ``diff``, ``cmp``, ``grep -q`` and ``test``, i.e. the
    most rigorous checks an agent can run.
    """
    results = ((step.get("observation") or {}).get("results")) or []
    return {
        r.get("source_call_id")
        for r in results
        if isinstance(r, dict) and r.get("content") is not None
    }


def _observed_output(step: dict[str, Any]) -> bool:
    """The step ran a check against the container *after* its own last edit and
    the environment's result came back.

    This is the "run the thing, don't assert" signal. Two things it deliberately
    does not accept: a claim of success with no observed output, and a tool that
    reports only on the agent's own state (:data:`NON_OBSERVING_TOOLS`).

    Resolution is per tool call, not per step, because harbor bundles every
    ``tool_use`` of one LLM turn into a single ATIF step (RFC-0001: "``step`` ==
    one turn; ``tool_calls`` is multi-valued"). A turn that batches an edit with
    the re-run of the check is the most common Claude Code shape there is, so the
    check that follows the edit *within* the same step has to count.
    """
    observed = _observed_call_ids(step)
    calls = _tool_calls(step)
    for call in calls[_last_mutation_index(step) + 1 :]:
        name = _tool_name(call)
        if name in MUTATION_TOOLS or name in NON_OBSERVING_TOOLS:
            continue
        if call.get("tool_call_id") in observed:
            return True
    return False


def _mentions_criteria(text: str) -> bool:
    return bool(_CRITERIA_CUE_RE.search(text))


def analyze_trajectory(path: Path) -> TrajectoryReport:
    """Read one ATIF trajectory and decide whether the self-review turn is visible."""
    report = TrajectoryReport(path=path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        report.error = f"unreadable ATIF trajectory: {exc}"
        return report
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list) or not steps:
        report.error = "no steps in trajectory (not an ATIF document?)"
        return report

    steps = [s for s in steps if isinstance(s, dict)]
    if not steps:
        report.error = "no object-shaped steps in trajectory (not an ATIF document?)"
        return report
    report.steps = len(steps)
    report.final_step_id = max((s.get("step_id") or 0) for s in steps)
    report.last_mutation_step_id = max(
        (s.get("step_id") or 0 for s in steps if _is_mutation(s)), default=0
    )

    # The self-review window: agent turns from the last edit onward. A check that
    # ran *before* the final edit does not verify the code that shipped — but the
    # editing turn itself is in the window, because a single ATIF step can carry
    # the edit and the re-run of the check (see :func:`_observed_output`).
    excerpts: list[str] = []
    for step in steps:
        step_id = step.get("step_id") or 0
        if step.get("source") != "agent":
            continue
        text = _step_text(step)
        mentions_criteria = _mentions_criteria(text)
        if mentions_criteria:
            report.criteria_step_ids_any.append(step_id)
        if step_id < report.last_mutation_step_id:
            continue
        verified = _observed_output(step)
        if verified:
            report.verification_step_ids.append(step_id)
        # On the editing turn itself, only prose that accompanies a real
        # post-edit check is a self-review; otherwise it is narration of the edit.
        if mentions_criteria and (step_id > report.last_mutation_step_id or verified):
            report.criteria_step_ids.append(step_id)
            if text.strip():
                excerpts.append(text.strip())
    report.excerpt = "\n\n".join(excerpts)
    return report


def find_trajectories(target: Path) -> list[Path]:
    """Resolve a CLI argument to trajectory files.

    Accepts a trajectory file directly, or any directory above one — harbor
    writes ``<trial>/agent/trajectory.json``, so a whole job directory works.
    """
    if target.is_file():
        return [target]
    if not target.is_dir():
        return []
    # ``**/trajectory.json`` already covers ``<trial>/agent/trajectory.json``;
    # globbing both walked the whole job tree twice for the same result.
    return sorted(target.glob("**/trajectory.json"))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sidebutton-harbor-agent-check-trajectory",
        description=(
            "Check an ATIF trajectory for the verify-before-done self-review turn "
            "(ran checks against real output, after the last edit, before done). "
            "Prints the matching excerpt as smoke evidence."
        ),
    )
    parser.add_argument(
        "target",
        nargs="+",
        help="trajectory.json file(s), or a trial/job directory to search.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit the reports as JSON."
    )
    parser.add_argument(
        "--excerpt-chars",
        type=int,
        default=600,
        help="Truncate each printed excerpt (default: 600; 0 or less = no excerpt).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # Every target is resolved before anything is reported, so one errored trial
    # with no trajectory cannot silently swallow the trials named after it.
    paths: list[Path] = []
    missing: list[str] = []
    for raw in args.target:
        resolved = find_trajectories(Path(raw))
        if not resolved:
            missing.append(raw)
        paths.extend(resolved)

    for raw in missing:
        print(f"no trajectory.json found under: {raw}", file=sys.stderr)
    if not paths:
        return 2

    reports = [analyze_trajectory(p) for p in paths]

    if args.json:
        print(json.dumps([r.to_dict() for r in reports], indent=2))
    else:
        for report in reports:
            print(report.render_human(excerpt_chars=args.excerpt_chars))

    passed = sum(1 for r in reports if r.self_review_turn_present)
    summary = f"\n{passed}/{len(reports)} trajectory(ies) show a self-review turn."
    print(summary, file=sys.stderr if args.json else sys.stdout)
    return 0 if passed == len(reports) and not missing else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
