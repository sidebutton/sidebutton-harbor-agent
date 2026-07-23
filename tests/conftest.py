"""Shared fixtures and a container-free fake environment.

Tests run offline (no Docker, no network). ``src`` is placed on ``sys.path`` so
the suite runs with a plain ``pytest`` even without an editable install.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize host env vars that would make config assembly non-deterministic."""
    for var in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "SIDEBUTTON_CLI_VERSION",
        "SIDEBUTTON_PACKS_DIR",
        "MAX_THINKING_TOKENS",
        "CLAUDE_CODE_EFFORT_LEVEL",
        "CLAUDE_CODE_MAX_TURNS",
    ):
        monkeypatch.delenv(var, raising=False)


class ExecResult:
    """Duck-typed stand-in for Harbor's ExecResult."""

    def __init__(self, return_code: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.return_code = return_code
        self.stdout = stdout
        self.stderr = stderr


class FakeEnvironment:
    """Records commands and uploads; every command "succeeds" (return code 0),
    so ``install()`` short-circuits the real npm/apt work and we can assert on
    the branch that was taken."""

    def __init__(self) -> None:
        self.exec_calls: list[dict[str, object]] = []
        self.uploads: list[tuple[str, str]] = []

    async def exec(
        self,
        command: str,
        user: str | int | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> ExecResult:
        self.exec_calls.append({"command": command, "user": user})
        stdout = "/home/agent" if "$HOME" in command else ""
        return ExecResult(return_code=0, stdout=stdout, stderr="")

    async def upload_dir(self, source_dir: str | Path, target_dir: str) -> None:
        self.uploads.append((str(source_dir), str(target_dir)))

    def commands(self) -> list[str]:
        return [str(call["command"]) for call in self.exec_calls]


@pytest.fixture
def fake_env() -> FakeEnvironment:
    return FakeEnvironment()
