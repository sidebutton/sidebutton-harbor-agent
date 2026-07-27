"""AC3: the drift guard — red on hand-edited ``packs/``, green on a clean export.

Two modes, both covered offline: credential-less manifest consistency (what CI
runs by default) and the full re-export byte-diff (what CI runs where the
operator configured a read credential; driven here through ``--source``, which
shares the whole comparison path with ``--fetch``).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from conftest import TB_PACK_NAMES, commit_all, make_pack_source

from sidebutton_harbor_agent.pack_check import check, check_offline, main
from sidebutton_harbor_agent.pack_export import MANIFEST_NAME, export_packs


@pytest.fixture
def exported(tmp_path: Path, pack_source) -> Path:
    """A clean export of the fixture pack repo."""
    dest = tmp_path / "packs"
    dest.mkdir()
    (dest / "README.md").write_text("# Bundled skill packs\n", encoding="utf-8")
    export_packs(source=pack_source.path, dest=dest)
    return dest


def _load(dest: Path) -> dict:
    return json.loads((dest / MANIFEST_NAME).read_text(encoding="utf-8"))


def _save(dest: Path, manifest: dict) -> None:
    (dest / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _problems(report) -> str:
    return " | ".join(report.problems)


# ------------------------------------------------------------------ offline OK
def test_offline_green_on_a_clean_export(exported: Path) -> None:
    report = check_offline(exported)
    assert report.ok, _problems(report)
    assert report.state == "exported"
    assert report.packs == list(TB_PACK_NAMES)
    assert len(report.source_commit) == 40


def test_offline_green_on_the_cold_state(tmp_path: Path) -> None:
    """The repo ships with an empty ``packs/`` until the first export lands."""
    cold = tmp_path / "packs"
    cold.mkdir()
    (cold / "README.md").write_text("# Bundled skill packs\n", encoding="utf-8")

    report = check_offline(cold)
    assert report.ok, _problems(report)
    assert report.state == "cold (no packs exported)"


def test_offline_reports_the_mode_it_ran(exported: Path) -> None:
    assert "offline" in check_offline(exported).mode


# ----------------------------------------------------------------- offline red
def test_offline_red_on_an_edited_pack_file(exported: Path) -> None:
    target = exported / "sb-tb-ml" / "toolchain" / "_skill.md"
    target.write_text(target.read_text(encoding="utf-8") + "\nhand-edited\n", encoding="utf-8")

    report = check_offline(exported)
    assert not report.ok
    assert "content drift" in _problems(report)
    assert "sb-tb-ml/toolchain/_skill.md" in _problems(report)


def test_offline_red_on_an_added_file(exported: Path) -> None:
    (exported / "sb-tb-git" / "EXTRA.md").write_text("snuck in\n", encoding="utf-8")

    report = check_offline(exported)
    assert not report.ok
    assert "untracked file" in _problems(report)


def test_offline_red_on_a_deleted_file(exported: Path) -> None:
    (exported / "sb-tb-sec" / "idioms" / "_skill.md").unlink()

    report = check_offline(exported)
    assert not report.ok
    assert "missing file" in _problems(report)


def test_offline_red_on_an_added_pack_dir(exported: Path) -> None:
    (exported / "sb-tb-extra").mkdir()
    (exported / "sb-tb-extra" / "SKILL.md").write_text("not exported\n", encoding="utf-8")

    report = check_offline(exported)
    assert not report.ok
    assert "pack list drift" in _problems(report)


def test_offline_red_on_a_deleted_pack_dir(exported: Path) -> None:
    for path in sorted((exported / "sb-tb-algo").rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    (exported / "sb-tb-algo").rmdir()

    report = check_offline(exported)
    assert not report.ok
    assert "pack list drift" in _problems(report)


def test_offline_red_on_packs_without_a_manifest(exported: Path) -> None:
    (exported / MANIFEST_NAME).unlink()

    report = check_offline(exported)
    assert not report.ok
    assert "no EXPORT.json" in _problems(report)


def test_offline_red_on_a_manifest_without_packs(exported: Path) -> None:
    import shutil

    for name in TB_PACK_NAMES:
        shutil.rmtree(exported / name)

    report = check_offline(exported)
    assert not report.ok
    assert "no pack directories" in _problems(report)


def test_offline_red_on_a_short_sha(exported: Path) -> None:
    """§10.1's ``pack_repo_commit`` has to pin exactly — a short sha is ambiguous."""
    manifest = _load(exported)
    manifest["source_commit"] = manifest["source_commit"][:7]
    _save(exported, manifest)

    report = check_offline(exported)
    assert not report.ok
    assert "not a full 40-hex sha" in _problems(report)


def test_offline_red_on_an_unknown_schema(exported: Path) -> None:
    manifest = _load(exported)
    manifest["schema"] = 99
    _save(exported, manifest)

    assert "unsupported" in _problems(check_offline(exported))


def test_offline_red_on_a_missing_field(exported: Path) -> None:
    manifest = _load(exported)
    del manifest["source_repo"]
    _save(exported, manifest)

    assert "source_repo" in _problems(check_offline(exported))


def test_offline_red_on_unparseable_json(exported: Path) -> None:
    (exported / MANIFEST_NAME).write_text("{not json", encoding="utf-8")
    assert "not valid JSON" in _problems(check_offline(exported))


def test_offline_red_on_a_path_outside_the_pack_list(exported: Path) -> None:
    """Only pack content is tracked — a loose file cannot be smuggled in."""
    manifest = _load(exported)
    manifest["files"]["README.md"] = "0" * 64
    _save(exported, manifest)

    assert "outside the pack list" in _problems(check_offline(exported))


@pytest.mark.parametrize("rel", ["../escape.md", "/etc/passwd", "sb-tb-ml/../../escape.md"])
def test_offline_red_on_an_unsafe_recorded_path(exported: Path, rel: str) -> None:
    """The checker reads recorded paths off disk, so it never trusts them."""
    manifest = _load(exported)
    manifest["files"][rel] = "0" * 64
    _save(exported, manifest)

    assert "unsafe recorded path" in _problems(check_offline(exported))


def test_offline_red_on_a_missing_packs_dir(tmp_path: Path) -> None:
    assert "does not exist" in _problems(check_offline(tmp_path / "absent"))


# ------------------------------------------ what the hash map alone cannot see
def test_offline_red_on_a_symlink_inside_a_pack(exported: Path) -> None:
    """A link is never hashed and never byte-compared, but ``_stage_packs``
    still uploads it into the task container — so it has to be caught here."""
    (exported / "sb-tb-ml" / "notes.md").symlink_to("../../../../etc/passwd")

    report = check_offline(exported)
    assert not report.ok
    assert "unexpected link under packs/: sb-tb-ml/notes.md" in _problems(report)


def test_offline_red_on_a_symlinked_pack_dir(exported: Path) -> None:
    (exported / "sb-tb-linked").symlink_to("/etc")

    report = check_offline(exported)
    assert not report.ok
    assert "unexpected link under packs/" in _problems(report)


def test_offline_red_on_an_unexported_loose_file(exported: Path) -> None:
    """packs/ is uploaded wholesale — unreviewed text must not ride along."""
    (exported / "EXTRA-PROMPT.md").write_text("hand-added guidance\n", encoding="utf-8")

    report = check_offline(exported)
    assert not report.ok
    assert "unexpected loose file under packs/: EXTRA-PROMPT.md" in _problems(report)


def test_offline_red_on_a_hidden_pack_dir(exported: Path) -> None:
    """The loader skips dot-dirs; the upload does not."""
    hidden = exported / ".hidden-pack"
    hidden.mkdir()
    (hidden / "SKILL.md").write_text("invisible to pack_skill_dirs\n", encoding="utf-8")

    report = check_offline(exported)
    assert not report.ok
    assert "hidden directory under packs/: .hidden-pack" in _problems(report)


def test_offline_red_on_junk_in_a_cold_packs_dir(tmp_path: Path) -> None:
    """The cold state is not a free pass — it is still shipped as-is."""
    cold = tmp_path / "packs"
    cold.mkdir()
    (cold / "sneaked.md").write_text("hand-added\n", encoding="utf-8")

    assert "unexpected loose file" in _problems(check_offline(cold))


def test_offline_allows_the_adapter_owned_readme(exported: Path) -> None:
    assert (exported / "README.md").is_file()
    assert check_offline(exported).ok


# -------------------------------------------------------------------- full mode
def test_full_green_on_a_clean_export(exported: Path, pack_source) -> None:
    report = check(exported, source=pack_source.path)
    assert report.ok, _problems(report)
    assert report.mode.startswith("full")


def test_full_red_on_a_hand_edit(exported: Path, pack_source) -> None:
    target = exported / "sb-tb-sys" / "debugging" / "_skill.md"
    target.write_text("rewritten\n", encoding="utf-8")

    report = check(exported, source=pack_source.path)
    assert not report.ok
    assert "sb-tb-sys/debugging/_skill.md" in _problems(report)


def test_full_catches_a_coordinated_file_and_hash_tamper(exported: Path, pack_source) -> None:
    """The offline mode's hashes are self-referential; only full mode sees this."""
    target = exported / "sb-tb-data" / "_skill.md"
    target.write_text("tampered content\n", encoding="utf-8")
    manifest = _load(exported)
    manifest["files"]["sb-tb-data/_skill.md"] = hashlib.sha256(target.read_bytes()).hexdigest()
    _save(exported, manifest)

    assert check_offline(exported).ok, "offline cannot see a coordinated tamper (documented)"
    report = check(exported, source=pack_source.path)
    assert not report.ok
    assert "content differs from the recorded commit" in _problems(report)


def test_full_red_when_the_source_lacks_the_recorded_commit(exported: Path, tmp_path: Path):
    """Pointed at the wrong repo, the check fails loudly instead of passing."""
    other = make_pack_source(tmp_path / "other-repo", names=("sb-tb-unrelated",))

    report = check(exported, source=other.path)
    assert not report.ok
    assert "could not re-export" in _problems(report)


def test_full_red_when_packs_moved_on_but_the_commit_pin_did_not(
    exported: Path, pack_source
) -> None:
    """packs/ re-synced from a newer commit, hashes refreshed, ``source_commit``
    left stale — internally consistent, so only the full mode can see it."""
    (pack_source.path / "sb-tb-ml" / "_skill.md").write_text("revised\n", encoding="utf-8")
    commit_all(pack_source.path, "feat(packs): revise ml")
    target = exported / "sb-tb-ml" / "_skill.md"
    target.write_text("revised\n", encoding="utf-8")
    manifest = _load(exported)
    manifest["files"]["sb-tb-ml/_skill.md"] = hashlib.sha256(target.read_bytes()).hexdigest()
    _save(exported, manifest)

    assert check_offline(exported).ok, "offline sees a self-consistent manifest"
    report = check(exported, source=pack_source.path)
    assert not report.ok
    assert "content differs from the recorded commit" in _problems(report)


def test_full_ignores_export_date_drift(exported: Path, pack_source, monkeypatch) -> None:
    """Re-exporting the same commit tomorrow is provenance, not drift."""
    manifest = _load(exported)
    manifest["export_date"] = "2030-01-01T00:00:00Z"
    _save(exported, manifest)

    report = check(exported, source=pack_source.path)
    assert report.ok, _problems(report)


def test_full_tolerates_a_differently_named_source_remote(
    exported: Path, tmp_path: Path, pack_source
) -> None:
    """An operator's mirror or local clone is not drift — content is what matters.

    The recorded ``source_repo`` is carried into the re-export precisely so a
    checkout whose ``origin`` differs does not read as a mismatch.
    """
    mirror = make_pack_source(tmp_path / "mirror", origin="https://mirror.invalid/12.git")
    assert mirror.commit == pack_source.commit, "same content+metadata -> same sha"

    report = check(exported, source=mirror.path)
    assert report.ok, _problems(report)


def test_full_falls_back_to_offline_on_the_cold_state(tmp_path: Path, pack_source) -> None:
    cold = tmp_path / "packs"
    cold.mkdir()

    report = check(cold, source=pack_source.path)
    assert report.ok, _problems(report)
    assert "nothing exported yet" in report.mode


def test_full_is_skipped_and_said_so_when_offline_already_failed(
    exported: Path, pack_source
) -> None:
    (exported / "sb-tb-ml" / "_skill.md").write_text("edited\n", encoding="utf-8")

    report = check(exported, source=pack_source.path)
    assert not report.ok
    assert "offline stage already failed" in report.mode


# ---------------------------------------------------------------------- CLI
def test_cli_green_exit_zero(exported: Path, capsys) -> None:
    code = main(["--packs-dir", str(exported)])
    assert code == 0
    assert "PACK DRIFT CHECK: PASS" in capsys.readouterr().out


def test_cli_red_exit_one(exported: Path, capsys) -> None:
    (exported / "sb-tb-build" / "_skill.md").write_text("edited\n", encoding="utf-8")

    code = main(["--packs-dir", str(exported)])
    out = capsys.readouterr().out
    assert code == 1
    assert "PACK DRIFT CHECK: FAIL" in out
    assert "content drift" in out


def test_cli_json_report(exported: Path, capsys) -> None:
    code = main(["--packs-dir", str(exported), "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert payload["ok"] is True
    assert payload["packs"] == list(TB_PACK_NAMES)
    assert "PACK DRIFT CHECK: PASS" in captured.err


def test_cli_source_and_fetch_are_mutually_exclusive(exported: Path, capsys) -> None:
    code = main(["--packs-dir", str(exported), "--source", ".", "--fetch"])
    assert code == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_cli_full_mode_via_source(exported: Path, pack_source, capsys) -> None:
    code = main(["--packs-dir", str(exported), "--source", str(pack_source.path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "full (re-export" in out
