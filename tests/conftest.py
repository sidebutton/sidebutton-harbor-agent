"""Shared fixtures and a container-free fake environment.

Tests run offline (no Docker, no network). ``src`` is placed on ``sys.path`` so
the suite runs with a plain ``pytest`` even without an editable install.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

#: The eight domain packs authored in the account pack repo (plan §7 / T3).
TB_PACK_NAMES = (
    "sb-tb-algo",
    "sb-tb-build",
    "sb-tb-data",
    "sb-tb-git",
    "sb-tb-ml",
    "sb-tb-sci",
    "sb-tb-sec",
    "sb-tb-sys",
)

#: Files inside one pack, mirroring the real T3 layout (6 per pack).
_PACK_FILES = (
    "_skill.md",
    "toolchain/_skill.md",
    "idioms/_skill.md",
    "debugging/_skill.md",
    "_roles/sd.md",
)

#: Repo-root content that is **not** a TB pack and must never be exported: the
#: deterministic generator pair, the fairness gate, the stale seeded index, and
#: the seeded ``example`` pack that still occupies index.json.
_SOURCE_NOISE = {
    "gen_tb_packs.py": "# deterministic generator\n",
    "tb_pack_data.py": "DOMAINS = {}\n",
    "FAIRNESS.md": "# Fairness\n",
    "README.md": "# tb-packs\n",
    "scripts/check-fairness.sh": "#!/bin/sh\nexit 0\n",
    "index.json": '{"packs": ["example.com"]}\n',
    "example/skill-pack.json": '{"name": "example.com"}\n',
}


@dataclass
class PackSource:
    """A throwaway git repo shaped like the account pack repo."""

    path: Path
    commit: str


def _run_git(args: list[str], cwd: Path) -> str:
    """Test-side git (needs write subcommands the export path forbids)."""
    stamp = "2026-07-27T12:00:00+00:00"
    env = {
        "GIT_AUTHOR_NAME": "T3",
        "GIT_AUTHOR_EMAIL": "t3@example.invalid",
        "GIT_COMMITTER_NAME": "T3",
        "GIT_COMMITTER_EMAIL": "t3@example.invalid",
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_DATE": stamp,
        "GIT_CONFIG_GLOBAL": str(cwd / ".gitconfig-none"),
        "GIT_CONFIG_SYSTEM": str(cwd / ".gitconfig-none"),
        "HOME": str(cwd),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def write_pack_tree(root: Path, names: tuple[str, ...] = TB_PACK_NAMES) -> None:
    """Write the pack dirs plus the repo-root noise that must not be exported."""
    for rel, text in _SOURCE_NOISE.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for name in names:
        pack = root / name
        pack.mkdir(parents=True, exist_ok=True)
        (pack / "skill-pack.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "version": "0.1.0",
                    "domain": name,
                    "roles": ["sd"],
                    "private": True,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        for rel in _PACK_FILES:
            path = pack / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {name} / {rel}\n\nDomain-general competency.\n", encoding="utf-8")


def commit_all(root: Path, message: str) -> str:
    _run_git(["add", "-A"], cwd=root)
    _run_git(["commit", "-q", "-m", message], cwd=root)
    return _run_git(["rev-parse", "HEAD"], cwd=root)


def make_pack_source(
    root: Path,
    names: tuple[str, ...] = TB_PACK_NAMES,
    origin: str = "https://git.sidebutton.com/12.git",
) -> PackSource:
    """A git repo mirroring the account pack repo at T3's commit."""
    root.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "-q", "-b", "main"], cwd=root)
    if origin:
        _run_git(["remote", "add", "origin", origin], cwd=root)
    write_pack_tree(root, names)
    return PackSource(path=root, commit=commit_all(root, "feat(packs): scaffold sb-tb-* packs"))


@pytest.fixture
def pack_source(tmp_path: Path) -> PackSource:
    return make_pack_source(tmp_path / "pack-repo")


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
        # Pack export/drift guard: a host that happens to configure a pack repo
        # or a reproducibility epoch must not steer the suite.
        "SB_PACK_REPO_URL",
        "SB_PACK_REPO_TOKEN",
        "SB_PACK_REPO_USER",
        "SOURCE_DATE_EPOCH",
        "GIT_CONFIG_GLOBAL",
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
