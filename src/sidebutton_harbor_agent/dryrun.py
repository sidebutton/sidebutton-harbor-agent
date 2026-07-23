"""Dry-run entrypoint: print and validate the in-container command line.

No Docker, no network, no container. Instantiates :class:`SidebuttonAgent` with
the given parameters and prints the exact ``claude`` invocation, resolved env,
and skills/MCP setup commands it would run, then validates them (model mapped,
non-interactive, no verifier/timeout/resource overrides). Exits non-zero if the
invocation is invalid so CI can assert on it.

    $ sidebutton-harbor-agent-dryrun --model anthropic/claude-opus-4-8 --effort high
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from sidebutton_harbor_agent.agent import SidebuttonAgent


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sidebutton-harbor-agent-dryrun",
        description=(
            "Print and validate the in-container command line the SideButton "
            "adapter would run for the given parameters (no container)."
        ),
    )
    parser.add_argument(
        "--model",
        default="anthropic/claude-opus-4-8",
        help="Backend model id (Harbor --model). Default: anthropic/claude-opus-4-8",
    )
    parser.add_argument(
        "--effort",
        default=None,
        help="Reasoning effort: low|medium|high|xhigh|max|ultracode (-> --effort).",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="ANTHROPIC_API_KEY to demonstrate env pass-through (optional).",
    )
    parser.add_argument(
        "--max-turns", type=int, default=None, help="Optional --max-turns cap."
    )
    parser.add_argument(
        "--packs-dir",
        default=None,
        help="Override the skill-packs directory (default: bundled packs/).",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit the invocation as JSON."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    extra_env = {"ANTHROPIC_API_KEY": args.api_key} if args.api_key else None
    kwargs: dict[str, object] = {"model_name": args.model}
    if extra_env:
        kwargs["extra_env"] = extra_env
    if args.effort:
        kwargs["reasoning_effort"] = args.effort
    if args.max_turns is not None:
        kwargs["max_turns"] = args.max_turns
    if args.packs_dir:
        kwargs["packs_dir"] = args.packs_dir

    with tempfile.TemporaryDirectory(prefix="sb-harbor-dryrun-") as tmp:
        agent = SidebuttonAgent(logs_dir=Path(tmp), **kwargs)
        invocation = agent.build_invocation()

    # Keep stdout pure in --json mode (it is meant to be piped/parsed); route the
    # status line to stderr there and to stdout in human mode.
    if args.json:
        print(json.dumps(invocation.to_dict(), indent=2))
    else:
        print(invocation.render_human())

    problems = invocation.validate()
    if problems:
        print("\nVALIDATION FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(
        "\ndry-run OK — invocation is valid (no overrides, model & effort wired).",
        file=sys.stderr if args.json else sys.stdout,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
