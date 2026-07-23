"""AC1: skill-pack loading — cold arm (empty) vs packs present, and install()."""

from __future__ import annotations

import asyncio
from pathlib import Path

from sidebutton_harbor_agent import SidebuttonAgent


def _make_packs(root: Path, names: list[str]) -> Path:
    packs = root / "packs"
    packs.mkdir()
    (packs / "README.md").write_text("loose files are ignored\n")
    for name in names:
        pack = packs / name
        pack.mkdir()
        (pack / "SKILL.md").write_text(f"# {name}\n")
    return packs


def test_cold_arm_bundled_packs_is_noop(tmp_path: Path) -> None:
    # The bundled packs/ dir ships empty (only README) -> no packs.
    agent = SidebuttonAgent(logs_dir=tmp_path, model_name="anthropic/claude-opus-4-8")
    assert agent.has_packs() is False
    assert agent.pack_skill_dirs() == []
    assert agent.build_invocation().packs == []


def test_packs_present_are_discovered(tmp_path: Path) -> None:
    packs = _make_packs(tmp_path, ["sb-tb-python", "sb-tb-git"])
    agent = SidebuttonAgent(
        logs_dir=tmp_path,
        model_name="anthropic/claude-opus-4-8",
        packs_dir=str(packs),
    )
    assert agent.has_packs() is True
    assert [p.name for p in agent.pack_skill_dirs()] == ["sb-tb-git", "sb-tb-python"]
    assert agent.build_invocation().packs == ["sb-tb-git", "sb-tb-python"]


def test_packs_dir_env_override(tmp_path: Path, monkeypatch) -> None:
    packs = _make_packs(tmp_path, ["sb-tb-node"])
    monkeypatch.setenv("SIDEBUTTON_PACKS_DIR", str(packs))
    agent = SidebuttonAgent(logs_dir=tmp_path, model_name="anthropic/claude-opus-4-8")
    assert agent.has_packs() is True


def test_install_installs_sidebutton_cli(tmp_path: Path, fake_env) -> None:
    agent = SidebuttonAgent(logs_dir=tmp_path, model_name="anthropic/claude-opus-4-8")
    asyncio.run(agent.install(fake_env))
    npm_installs = [c for c in fake_env.commands() if "npm install -g" in c]
    assert any("sidebutton@1.5.1" in c for c in npm_installs)


def test_install_cold_arm_uploads_nothing(tmp_path: Path, fake_env) -> None:
    agent = SidebuttonAgent(logs_dir=tmp_path, model_name="anthropic/claude-opus-4-8")
    asyncio.run(agent.install(fake_env))
    assert fake_env.uploads == []


def test_install_stages_packs_when_present(tmp_path: Path, fake_env) -> None:
    packs = _make_packs(tmp_path, ["sb-tb-python"])
    agent = SidebuttonAgent(
        logs_dir=tmp_path,
        model_name="anthropic/claude-opus-4-8",
        packs_dir=str(packs),
    )
    asyncio.run(agent.install(fake_env))
    assert len(fake_env.uploads) == 1
    source, target = fake_env.uploads[0]
    assert source == str(packs)
    # Uploaded where the inherited run() setup copies from into the skills dir.
    assert target.endswith("/.claude/skills")


def test_cli_version_override(tmp_path: Path, fake_env) -> None:
    agent = SidebuttonAgent(
        logs_dir=tmp_path,
        model_name="anthropic/claude-opus-4-8",
        sidebutton_cli_version="1.6.0",
    )
    asyncio.run(agent.install(fake_env))
    assert any("sidebutton@1.6.0" in c for c in fake_env.commands())
    assert agent.version() == "0.1.0+cli.1.6.0"


def test_pack_staging_failure_degrades_cleanly(tmp_path: Path, fake_env) -> None:
    """A pack-layer failure must never propagate out of install() (reward-0 guard)."""
    packs = _make_packs(tmp_path, ["sb-tb-python"])
    agent = SidebuttonAgent(
        logs_dir=tmp_path,
        model_name="anthropic/claude-opus-4-8",
        packs_dir=str(packs),
    )

    async def boom(source_dir, target_dir):
        raise RuntimeError("network is down")

    fake_env.upload_dir = boom
    # Must not raise despite upload_dir blowing up...
    asyncio.run(agent.install(fake_env))
    # ...and the CLI install still happened before the pack layer failed.
    assert any("npm install -g" in c for c in fake_env.commands())
