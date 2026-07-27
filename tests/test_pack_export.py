"""AC1/AC2: the pack export tool — selection, pinning, reproducibility, manifest.

The source fixture (``conftest.make_pack_source``) mirrors the real account pack
repo: eight ``sb-tb-*`` dirs plus the generator scripts, the fairness gate, the
stale ``index.json`` and the seeded ``example`` pack. Everything here runs
offline against a throwaway git repo in ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import TB_PACK_NAMES, commit_all, make_pack_source, write_pack_tree

from sidebutton_harbor_agent.pack_export import (
    MANIFEST_NAME,
    MANIFEST_SCHEMA,
    PackExportError,
    _git,
    export_packs,
    main,
    scrub_url,
)


def _tree(root: Path) -> dict[str, bytes]:
    """Everything under ``root`` as ``relative path -> bytes`` (byte-identity)."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _manifest(dest: Path) -> dict:
    return json.loads((dest / MANIFEST_NAME).read_text(encoding="utf-8"))


# ------------------------------------------------------------------- selection
def test_exports_exactly_the_sb_tb_packs(tmp_path: Path, pack_source) -> None:
    dest = tmp_path / "packs"
    result = export_packs(source=pack_source.path, dest=dest)

    assert result.packs == list(TB_PACK_NAMES)
    assert sorted(p.name for p in dest.iterdir() if p.is_dir()) == list(TB_PACK_NAMES)
    # Repo-root noise never crosses into the public repo.
    for noise in ("example", "scripts", "gen_tb_packs.py", "index.json", "FAIRNESS.md"):
        assert not (dest / noise).exists(), f"{noise} must not be exported"


def test_pack_content_is_a_verbatim_mirror(tmp_path: Path, pack_source) -> None:
    dest = tmp_path / "packs"
    export_packs(source=pack_source.path, dest=dest)

    for name in TB_PACK_NAMES:
        assert _tree(dest / name) == _tree(pack_source.path / name)


def test_zero_matches_is_an_error(tmp_path: Path, pack_source) -> None:
    with pytest.raises(PackExportError, match="no directories matching"):
        export_packs(source=pack_source.path, dest=tmp_path / "packs", glob="sb-nope-*")


# --------------------------------------------------------------------- manifest
def test_manifest_records_the_arm_param_block_fields(tmp_path: Path, pack_source) -> None:
    dest = tmp_path / "packs"
    export_packs(source=pack_source.path, dest=dest)
    manifest = _manifest(dest)

    assert manifest["schema"] == MANIFEST_SCHEMA
    assert manifest["source_repo"] == "https://git.sidebutton.com/12.git"
    # §10.1 pack_repo_commit: full sha, never the ambiguous short form.
    assert manifest["source_commit"] == pack_source.commit
    assert len(manifest["source_commit"]) == 40
    assert manifest["source_commit_date"].startswith("2026-07-27T12:00:00")
    assert manifest["export_date"].endswith("Z")
    assert manifest["packs"] == sorted(TB_PACK_NAMES)
    # 8 packs x 6 files, every one hashed.
    assert len(manifest["files"]) == 48
    assert list(manifest["files"]) == sorted(manifest["files"])
    assert all(len(digest) == 64 for digest in manifest["files"].values())


def test_manifest_is_a_loose_file_the_adapter_ignores(tmp_path: Path, pack_source) -> None:
    """``EXPORT.json`` must not look like a pack to ``pack_skill_dirs``."""
    from sidebutton_harbor_agent import SidebuttonAgent

    dest = tmp_path / "packs"
    export_packs(source=pack_source.path, dest=dest)
    agent = SidebuttonAgent(logs_dir=tmp_path, model_name="m", packs_dir=str(dest))

    assert [p.name for p in agent.pack_skill_dirs()] == list(TB_PACK_NAMES)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://x-access-token:ghp_secret@git.sidebutton.com/12.git",
         "https://git.sidebutton.com/12.git"),
        ("https://user@git.sidebutton.com/12.git", "https://git.sidebutton.com/12.git"),
        ("https://git.sidebutton.com/12.git", "https://git.sidebutton.com/12.git"),
        ("git@github.com:sidebutton/x.git", "git@github.com:sidebutton/x.git"),
    ],
)
def test_scrub_url_strips_credentials(url: str, expected: str) -> None:
    assert scrub_url(url) == expected


def test_a_token_bearing_origin_is_never_recorded(tmp_path: Path) -> None:
    """The manifest ships in a *public* repo: an authenticated clone URL must not."""
    source = make_pack_source(
        tmp_path / "repo", origin="https://x-access-token:ghp_secret@git.sidebutton.com/12.git"
    )
    dest = tmp_path / "packs"
    export_packs(source=source.path, dest=dest)

    assert "ghp_secret" not in (dest / MANIFEST_NAME).read_text(encoding="utf-8")
    assert _manifest(dest)["source_repo"] == "https://git.sidebutton.com/12.git"


def test_repo_url_override_is_recorded(tmp_path: Path, pack_source) -> None:
    dest = tmp_path / "packs"
    export_packs(source=pack_source.path, dest=dest, repo_url="https://elsewhere/12.git")
    assert _manifest(dest)["source_repo"] == "https://elsewhere/12.git"


# -------------------------------------------------------------- reproducibility
def test_same_commit_yields_byte_identical_output(
    tmp_path: Path, pack_source, monkeypatch
) -> None:
    """AC1. With ``SOURCE_DATE_EPOCH`` pinned, even the manifest is identical."""
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1769500800")
    first, second = tmp_path / "a", tmp_path / "b"
    export_packs(source=pack_source.path, dest=first)
    export_packs(source=pack_source.path, dest=second)

    assert _tree(first) == _tree(second)


def test_reexport_over_itself_is_stable(tmp_path: Path, pack_source, monkeypatch) -> None:
    """Exporting twice into the *same* dir is idempotent (the mirror re-run)."""
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1769500800")
    dest = tmp_path / "packs"
    export_packs(source=pack_source.path, dest=dest)
    before = _tree(dest)
    export_packs(source=pack_source.path, dest=dest)

    assert _tree(dest) == before


def test_only_export_date_moves_without_source_date_epoch(
    tmp_path: Path, pack_source
) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    one = export_packs(source=pack_source.path, dest=first).manifest
    two = export_packs(source=pack_source.path, dest=second).manifest

    assert {k: v for k, v in one.items() if k != "export_date"} == {
        k: v for k, v in two.items() if k != "export_date"
    }


def test_invalid_source_date_epoch_errors_before_writing(tmp_path: Path, pack_source, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "not-a-number")
    dest = tmp_path / "packs"
    with pytest.raises(PackExportError, match="SOURCE_DATE_EPOCH"):
        export_packs(source=pack_source.path, dest=dest)
    # Half-written exports are worse than none: nothing landed.
    assert not dest.exists() or not any(dest.iterdir())


# ------------------------------------------------------------------- commit pin
def test_export_honours_the_pinned_commit_not_head(tmp_path: Path, pack_source) -> None:
    (pack_source.path / "sb-tb-ml" / "_skill.md").write_text("CHANGED AT HEAD\n", encoding="utf-8")
    head = commit_all(pack_source.path, "feat(packs): revise ml")
    assert head != pack_source.commit

    dest = tmp_path / "packs"
    export_packs(source=pack_source.path, ref=pack_source.commit, dest=dest)

    assert "CHANGED AT HEAD" not in (dest / "sb-tb-ml" / "_skill.md").read_text(encoding="utf-8")
    assert _manifest(dest)["source_commit"] == pack_source.commit


def test_export_reads_the_commit_not_a_dirty_working_tree(tmp_path: Path, pack_source) -> None:
    """A dirty operator checkout must not leak uncommitted text into a public repo."""
    (pack_source.path / "sb-tb-git" / "_skill.md").write_text("UNCOMMITTED\n", encoding="utf-8")
    (pack_source.path / "sb-tb-scratch").mkdir()
    (pack_source.path / "sb-tb-scratch" / "_skill.md").write_text("WIP\n", encoding="utf-8")

    dest = tmp_path / "packs"
    result = export_packs(source=pack_source.path, dest=dest)

    assert result.packs == list(TB_PACK_NAMES)  # the untracked pack is not exported
    assert "UNCOMMITTED" not in (dest / "sb-tb-git" / "_skill.md").read_text(encoding="utf-8")


def test_short_sha_resolves_to_the_full_sha(tmp_path: Path, pack_source) -> None:
    dest = tmp_path / "packs"
    export_packs(source=pack_source.path, ref=pack_source.commit[:7], dest=dest)
    assert _manifest(dest)["source_commit"] == pack_source.commit


def test_unknown_commit_is_an_error(tmp_path: Path, pack_source) -> None:
    with pytest.raises(PackExportError, match="rev-parse failed"):
        export_packs(source=pack_source.path, ref="0" * 40, dest=tmp_path / "packs")


def test_non_git_source_is_an_error(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    write_pack_tree(plain)
    with pytest.raises(PackExportError, match="not a git repository"):
        export_packs(source=plain, dest=tmp_path / "packs")


def test_missing_source_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(PackExportError, match="not a directory"):
        export_packs(source=tmp_path / "nope", dest=tmp_path / "packs")


# ----------------------------------------------------------- mirror write scope
def test_stale_pack_dirs_are_removed_and_loose_files_preserved(
    tmp_path: Path, pack_source
) -> None:
    dest = tmp_path / "packs"
    dest.mkdir()
    (dest / "README.md").write_text("adapter-owned, must survive\n", encoding="utf-8")
    (dest / "sb-tb-retired").mkdir()
    (dest / "sb-tb-retired" / "SKILL.md").write_text("dropped upstream\n", encoding="utf-8")

    export_packs(source=pack_source.path, dest=dest)

    assert not (dest / "sb-tb-retired").exists()
    assert (dest / "README.md").read_text(encoding="utf-8") == "adapter-owned, must survive\n"


def test_mirror_removes_non_matching_pack_dirs_too(tmp_path: Path, pack_source) -> None:
    """The adapter stages *every* subdir, so a non-``sb-tb-*`` one is drift too."""
    dest = tmp_path / "packs"
    dest.mkdir()
    (dest / "hand-added").mkdir()
    (dest / "hand-added" / "SKILL.md").write_text("not from the source repo\n", encoding="utf-8")

    export_packs(source=pack_source.path, dest=dest)

    assert not (dest / "hand-added").exists()


def test_symlinks_in_the_source_are_refused(tmp_path: Path, pack_source) -> None:
    """Every file under packs/ must be a regular file with a recorded sha256 —
    otherwise a link slips past the drift guard (and into a task container)."""
    (pack_source.path / "sb-tb-ml" / "linked.md").symlink_to("../../FAIRNESS.md")
    commit_all(pack_source.path, "feat(packs): add a link")

    with pytest.raises(PackExportError, match="refusing to export symlinks"):
        export_packs(source=pack_source.path, dest=tmp_path / "packs")


# ---------------------------------------------------------------- one-way rule
@pytest.mark.parametrize("subcommand", ["push", "commit", "send-pack", "update-ref", "tag"])
def test_write_subcommands_are_refused(subcommand: str) -> None:
    """§5.3 is one-way: there is no code path from this repo back to the source."""
    with pytest.raises(PackExportError, match="read-only"):
        _git([subcommand, "origin", "main"])


def test_git_config_write_form_is_refused() -> None:
    with pytest.raises(PackExportError, match="write form"):
        _git(["config", "remote.origin.url", "https://evil/"])


# ----------------------------------------------------------------------- CLI
def test_cli_exports_and_reports(tmp_path: Path, pack_source, capsys) -> None:
    dest = tmp_path / "packs"
    code = main(["--source", str(pack_source.path), "--dest", str(dest)])
    out = capsys.readouterr().out

    assert code == 0
    assert pack_source.commit[:12] in out
    assert "sb-tb-ml" in out
    assert (dest / MANIFEST_NAME).is_file()


def test_cli_json_mode_emits_the_manifest(tmp_path: Path, pack_source, capsys) -> None:
    code = main(
        ["--source", str(pack_source.path), "--dest", str(tmp_path / "packs"), "--json"]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert json.loads(captured.out)["source_commit"] == pack_source.commit
    # Status line stays off stdout so the JSON can be piped.
    assert "exported 8 pack(s)" in captured.err


def test_cli_reports_failure_without_a_traceback(tmp_path: Path, capsys) -> None:
    code = main(["--source", str(tmp_path / "nope"), "--dest", str(tmp_path / "packs")])
    assert code == 1
    assert "export failed" in capsys.readouterr().err
