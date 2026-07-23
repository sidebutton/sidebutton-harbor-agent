"""Harbor ``InstalledAgent`` adapter for the SideButton runtime.

The public ``sidebutton`` npm CLI is a *workflow / skill-pack* tool, not an
autonomous coder. The "SideButton runtime" that competes on Terminal-Bench is
therefore a base coding agent (Claude Code) **plus** SideButton skill packs
**plus** a verify-before-done loop. This adapter models exactly that by
subclassing Harbor's :class:`~harbor.agents.installed.claude_code.ClaudeCode`,
which gives us ATIF trajectory emission, error classification, and the
model / effort / API-key plumbing for free.

On top of the inherited base it:

* installs the public ``sidebutton`` CLI inside the task container,
* loads any skill packs present under ``packs/`` by flattening them into Claude
  Code's skills directory (empty ``packs/`` -> clean no-op: the "cold arm"),
* appends the verify-loop guidance (``config/CLAUDE.md``) to the task
  instruction the agent receives.

It deliberately sets **no** verifier, timeout, or resource overrides so that
runs stay on stock settings (campaign fairness requirement).
"""

from __future__ import annotations

import importlib.resources
import os
import shlex
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, override

from harbor.agents.installed.claude_code import ClaudeCode
from harbor.environments.base import BaseEnvironment
from harbor.utils.env import parse_bool_env_value

#: Adapter package version. Distinct from the underlying Claude Code CLI version
#: and from the pinned SideButton CLI version -- all three feed ``version()``.
ADAPTER_VERSION = "0.1.0"

#: Default pinned SideButton CLI (public npm). Overridable per run via the
#: ``sidebutton_cli_version`` kwarg or ``SIDEBUTTON_CLI_VERSION`` env var so a
#: submission records exactly which CLI produced it (reproducibility).
DEFAULT_SIDEBUTTON_CLI_VERSION = "1.5.1"

#: Placeholder instruction used by the dry-run builder when no real task
#: instruction is available (no container is involved).
INSTRUCTION_PLACEHOLDER = "<INSTRUCTION>"

#: Command fragments that would constitute a verifier / timeout / resource
#: override. The adapter must emit none of these (fairness): the dry-run
#: validator and the unit tests assert their absence in the generated command.
FORBIDDEN_OVERRIDE_TOKENS = (
    "--verifier",
    "--timeout",
    "--setup-timeout",
    "--agent-timeout",
    "--cpus",
    "--memory",
    "--gpus",
    "--resource",
)


@dataclass
class InContainerInvocation:
    """A pure, no-container description of what the adapter would run.

    Produced by :meth:`SidebuttonAgent.build_invocation` so that the dry-run
    entrypoint and the unit tests can assert the generated command line and
    config without Docker (mirrors the stable subset of ``ClaudeCode.run()``).
    """

    agent: str
    version: str
    model: str | None
    env: dict[str, str]
    cli_flags: str
    command: str
    setup_commands: list[str] = field(default_factory=list)
    packs: list[str] = field(default_factory=list)
    rendered_instruction: str = ""

    def validate(self) -> list[str]:
        """Return a list of human-readable problems (empty == valid)."""
        problems: list[str] = []

        if not self.command.startswith("claude "):
            problems.append(
                f"in-container command must invoke the claude CLI, got: {self.command!r}"
            )
        if "--print" not in self.command:
            problems.append("claude invocation must be non-interactive (missing --print)")
        if self.model and "ANTHROPIC_MODEL" not in self.env:
            problems.append(
                f"model {self.model!r} was not mapped to ANTHROPIC_MODEL in the env"
            )
        # Fairness: no verifier / timeout / resource overrides anywhere.
        haystack = f"{self.command} {self.cli_flags} {' '.join(self.setup_commands)}"
        for token in FORBIDDEN_OVERRIDE_TOKENS:
            if token in haystack:
                problems.append(f"forbidden override token present: {token}")
        return problems

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def render_human(self) -> str:
        setup_lines = [f"  $ {c}" for c in self.setup_commands] or ["  (none)"]
        lines = [
            f"agent:   {self.agent}",
            f"version: {self.version}",
            f"model:   {self.model}",
            f"packs:   {self.packs or '(none — cold arm)'}",
            "",
            "env (in-container):",
            *(f"  {k}={v}" for k, v in sorted(self.env.items())),
            "",
            "setup commands:",
            *setup_lines,
            "",
            "agent command:",
            f"  $ {self.command}",
        ]
        return "\n".join(lines)


class SidebuttonAgent(ClaudeCode):
    """SideButton runtime as a Harbor InstalledAgent.

    Import path: ``sidebutton_harbor_agent:SidebuttonAgent``.
    """

    #: Stable adapter id. ``AgentInfo.name`` is a free-form ``str`` in Harbor and
    #: the run/trajectory path never coerces it back to the built-in ``AgentName``
    #: enum, so a custom name is safe and keeps SideButton runs distinct from
    #: vanilla claude-code in the ``(agent, version, model, effort)`` submission key.
    AGENT_NAME = "sidebutton"

    #: npm package that provides the SideButton CLI.
    SIDEBUTTON_NPM_PACKAGE = "sidebutton"

    def __init__(
        self,
        logs_dir: Path,
        *args: Any,
        packs_dir: str | os.PathLike[str] | None = None,
        sidebutton_cli_version: str | None = None,
        verify_loop: bool | str = True,
        verify_loop_path: str | os.PathLike[str] | None = None,
        **kwargs: Any,
    ) -> None:
        self._packs_dir = self._resolve_packs_dir(packs_dir)
        self._cli_version = (
            sidebutton_cli_version
            or os.environ.get("SIDEBUTTON_CLI_VERSION")
            or DEFAULT_SIDEBUTTON_CLI_VERSION
        )
        # ``--agent-kwarg`` values arrive as strings, so coerce truthiness robustly.
        self._verify_loop_enabled = (
            parse_bool_env_value(verify_loop, name="verify_loop", default=True)
            if isinstance(verify_loop, str)
            else bool(verify_loop)
        )
        self._verify_loop_text = (
            self._load_verify_loop_text(verify_loop_path)
            if self._verify_loop_enabled
            else ""
        )
        super().__init__(logs_dir, *args, **kwargs)

    # ------------------------------------------------------------------ identity
    @staticmethod
    @override
    def name() -> str:
        return SidebuttonAgent.AGENT_NAME

    @classmethod
    @override
    def import_path(cls) -> str:
        # Canonical public path (the class is re-exported from the package root),
        # independent of the defining submodule.
        return "sidebutton_harbor_agent:SidebuttonAgent"

    @override
    def version(self) -> str:
        # Adapter identity + the CLI pin so a submission is reproducible. Kept
        # deterministic (no runtime detection that could vary between runs).
        return f"{ADAPTER_VERSION}+cli.{self._cli_version}"

    # --------------------------------------------------------------- verify loop
    @override
    def render_instruction(self, instruction: str) -> str:
        """Append the verify-before-done guidance to the task instruction.

        Delivered through the instruction (not ``--append-system-prompt``, whose
        value is inserted unquoted into the shell command and cannot carry
        multi-line content, and not a ``CLAUDE.md`` file, whose discovery is
        unreliable once ``CLAUDE_CONFIG_DIR`` is overridden by the base ``run()``).
        Real content lands in B3 / SCRUM-1836; this wires the mechanism.
        """
        rendered = super().render_instruction(instruction)
        if self._verify_loop_enabled and self._verify_loop_text:
            return f"{rendered}\n\n{self._verify_loop_text}"
        return rendered

    # ------------------------------------------------------------------- install
    @override
    async def install(self, environment: BaseEnvironment) -> None:
        # Base coder: Claude Code via the inherited installer (brings curl/procps).
        await super().install(environment)

        # Required deliverable: the public SideButton CLI (public npm) inside the
        # container. Ensure node/npm exist (mirrors the base installer's own
        # package-manager detection), then install globally as root so the npm
        # prefix is always writable.
        await self.exec_as_root(
            environment,
            command=(
                "if ! command -v npm >/dev/null 2>&1; then "
                "if command -v apk >/dev/null 2>&1; then apk add --no-cache nodejs npm; "
                "elif command -v apt-get >/dev/null 2>&1; then "
                "apt-get update && apt-get install -y nodejs npm; "
                "elif command -v dnf >/dev/null 2>&1; then dnf install -y nodejs npm; "
                "elif command -v yum >/dev/null 2>&1; then yum install -y nodejs npm; "
                "fi; fi"
            ),
            env={"DEBIAN_FRONTEND": "noninteractive"},
        )
        spec = (
            f"{self.SIDEBUTTON_NPM_PACKAGE}@{self._cli_version}"
            if self._cli_version
            else self.SIDEBUTTON_NPM_PACKAGE
        )
        await self.exec_as_root(
            environment, command=f"npm install -g {shlex.quote(spec)}"
        )

        # Skill packs (flatten -> Claude Code skills). Empty for the cold arm.
        if self.has_packs():
            await self._stage_packs(environment)
        else:
            self.logger.info(
                "SideButton cold arm: no packs under %s; running the base agent.",
                self._packs_dir,
            )

    async def _stage_packs(self, environment: BaseEnvironment) -> None:
        """Upload packs into ``~/.claude/skills`` so the inherited ``run()`` setup
        copies them into the active skills dir.

        Best-effort: a pack-layer failure must **never** fail the trial (that
        would turn a robustness bug into a reward-0), so any error degrades the
        run to the base agent.
        """
        try:
            home_result = await environment.exec(command='printf %s "$HOME"')
            home = (home_result.stdout or "").strip() or "/root"
            target = f"{home}/.claude/skills"
            await environment.exec(command=f"mkdir -p {shlex.quote(target)}")
            await environment.upload_dir(self._packs_dir, target)
            self.logger.info(
                "Staged %d SideButton pack(s) into %s.",
                len(self.pack_skill_dirs()),
                target,
            )
        except Exception as exc:  # noqa: BLE001 — deliberate degrade-to-base
            self.logger.warning(
                "SideButton pack staging failed (%s); degrading to the base agent.",
                exc,
            )

    # --------------------------------------------------------------------- packs
    def _resolve_packs_dir(
        self, packs_dir: str | os.PathLike[str] | None
    ) -> Path | None:
        override = packs_dir or os.environ.get("SIDEBUTTON_PACKS_DIR")
        if override:
            return Path(override)
        try:
            packaged = importlib.resources.files("sidebutton_harbor_agent").joinpath(
                "packs"
            )
            return Path(str(packaged))
        except (ModuleNotFoundError, FileNotFoundError, TypeError):
            return None

    def has_packs(self) -> bool:
        return len(self.pack_skill_dirs()) > 0

    def pack_skill_dirs(self) -> list[Path]:
        """Skill packs are subdirectories of ``packs_dir``; files (README,
        .gitkeep) and hidden entries are ignored."""
        if not self._packs_dir or not self._packs_dir.is_dir():
            return []
        return sorted(
            p
            for p in self._packs_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )

    def _load_verify_loop_text(
        self, verify_loop_path: str | os.PathLike[str] | None
    ) -> str:
        try:
            if verify_loop_path:
                return Path(verify_loop_path).read_text(encoding="utf-8").strip()
            packaged = importlib.resources.files("sidebutton_harbor_agent").joinpath(
                "config", "CLAUDE.md"
            )
            return packaged.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, ModuleNotFoundError, OSError):
            return ""

    # ------------------------------------------------------------- dry-run seam
    def build_invocation(
        self, instruction: str = INSTRUCTION_PLACEHOLDER
    ) -> InContainerInvocation:
        """Assemble the in-container invocation **without** a container.

        Mirrors the stable subset of ``ClaudeCode.run()`` (env + CLI flags +
        the ``claude`` command) so the dry-run entrypoint and the unit tests can
        assert config assembly and param plumbing offline.
        """
        cli_flags = self.build_cli_flags()
        command = " ".join(
            part
            for part in (
                "claude --verbose --output-format=stream-json",
                cli_flags,
                "--print",
            )
            if part
        )
        setup_commands = [
            cmd
            for cmd in (
                self._build_register_skills_command(),
                self._build_register_memory_command(),
                self._build_register_mcp_servers_command(),
            )
            if cmd
        ]
        return InContainerInvocation(
            agent=self.name(),
            version=self.version(),
            model=self.model_name,
            env=self._build_agent_env(),
            cli_flags=cli_flags,
            command=command,
            setup_commands=setup_commands,
            packs=[p.name for p in self.pack_skill_dirs()],
            rendered_instruction=self.render_instruction(instruction),
        )

    def _build_agent_env(self) -> dict[str, str]:
        """Resolve the in-container environment for the official-Anthropic-API
        path (the campaign's default). Mirrors ``ClaudeCode.run()``; the Bedrock
        and forced-OAuth branches are intentionally out of scope for the pure
        builder and are handled by the inherited ``run()`` at execution time.
        """
        env: dict[str, str] = {}

        api_key = self._get_env("ANTHROPIC_API_KEY") or self._get_env(
            "ANTHROPIC_AUTH_TOKEN"
        )
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key

        base_url = os.environ.get("ANTHROPIC_BASE_URL")
        if base_url:
            env["ANTHROPIC_BASE_URL"] = base_url

        if self.model_name:
            # Keep the full name for a custom base URL; strip the provider prefix
            # for the official Anthropic API.
            env["ANTHROPIC_MODEL"] = (
                self.model_name if base_url else self.model_name.split("/")[-1]
            )
        elif os.environ.get("ANTHROPIC_MODEL"):
            env["ANTHROPIC_MODEL"] = os.environ["ANTHROPIC_MODEL"]

        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        env["IS_SANDBOX"] = "1"
        # Declarative env vars (e.g. MAX_THINKING_TOKENS) resolved by the base.
        env.update(self._resolved_env_vars)
        return env
