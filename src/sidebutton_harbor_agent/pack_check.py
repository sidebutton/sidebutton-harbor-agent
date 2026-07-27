"""Drift guard: the bundled ``packs/`` must still be a clean export.

Pairs with :mod:`sidebutton_harbor_agent.pack_export`. The account pack repo is
the source of truth and this repo carries a frozen export of it (plan §5.3);
"exported packs drift from the account pack repo" is a named risk (§13). This is
the CI check that catches it.

    $ sidebutton-harbor-agent-check-packs                    # offline (default)
    $ sidebutton-harbor-agent-check-packs --source ../pack-repo
    $ sidebutton-harbor-agent-check-packs --fetch            # credentialed

The private-repo fetch is credential-gated, so the check **degrades** instead of
failing when no credential is configured:

* **offline** (default) — the manifest parses, records a full sha, and agrees
  with what is on disk: same pack list, same file set, matching sha256 per file.
  Catches the realistic accident (someone edits a bundled pack by hand, or adds
  or deletes a file) with no credential at all.
* **full** (``--source`` / ``--fetch``) — re-exports the manifest's own
  ``source_commit`` and byte-compares. This is the only mode that can catch a
  *coordinated* edit, where the file and its recorded hash were changed
  together; the offline mode's hashes are self-referential by construction.

The mode that ran is always printed, so a green CI log never overstates what was
verified.

``--fetch`` requires the operator to name the remote in ``SB_PACK_REPO_URL``
whenever a token is configured. The manifest's own ``source_repo`` is read out of
a committed file, so using it as the clone target would let a pull request
redirect the read credential — and then certify ``packs/`` against a repo of the
author's choosing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sidebutton_harbor_agent.pack_export import (
    MANIFEST_NAME,
    MANIFEST_SCHEMA,
    PACK_GLOB,
    PackExportError,
    _git,
    default_dest,
    export_packs,
    hash_files,
    pack_dirs,
    read_manifest,
    scrub_url,
    split_userinfo,
)

#: Remote URL for the credentialed mode. **Required whenever a token is set** —
#: see the module docstring. Falls back to the manifest's own ``source_repo``
#: only when there is no credential that a redirect could capture.
ENV_REPO_URL = "SB_PACK_REPO_URL"

#: Read credential for the private pack repo. Supplied by the operator (CI
#: secret / local env) and **never** committed: its presence is what flips the
#: check from offline to full.
ENV_REPO_TOKEN = "SB_PACK_REPO_TOKEN"

#: Username paired with the token in HTTP basic auth. Hosts differ; the common
#: token-as-password convention is the default.
ENV_REPO_USER = "SB_PACK_REPO_USER"

DEFAULT_REPO_USER = "x-access-token"

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: Loose files this repo owns in ``packs/``. Everything else at the top level is
#: neither exported nor adapter-owned — and ``_stage_packs`` uploads the whole
#: directory into the task container, so an unreviewed file left here would ride
#: along without ever passing the export. Hidden directories are caught for the
#: same reason: the loader skips them, the upload does not.
ALLOWED_LOOSE_FILES = frozenset({"README.md", MANIFEST_NAME})

#: Every key a schema-1 manifest may carry. Closed rather than open: ``packs/``
#: is uploaded wholesale into the task container, so an unknown key is a free
#: text channel into it that no other check would look at.
_MANIFEST_KEYS = frozenset(
    {
        "schema",
        "source_repo",
        "source_commit",
        "source_commit_date",
        "export_date",
        "pack_glob",
        "packs",
        "files",
    }
)

#: Manifest keys compared field-wise in full mode — only the ones a re-export
#: *derives*, so a mismatch means something. ``schema``, ``source_repo``,
#: ``source_commit`` and ``pack_glob`` are excluded because they are fed into
#: that re-export as inputs: comparing them would compare each against itself
#: and read as verification that never happens. ``export_date`` is excluded
#: because it is wall-clock provenance, not content — re-exporting the same
#: commit tomorrow is not drift.
_COMPARED_MANIFEST_KEYS = ("source_commit_date", "packs", "files")

#: Permission bits that have no business on a pack file. setuid/setgid/sticky
#: and group/other-write survive into the task container through
#: ``_stage_packs``, and no hash or byte-compare can see a mode.
_FORBIDDEN_MODE_BITS = (
    stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX | stat.S_IWGRP | stat.S_IWOTH
)


@dataclass
class CheckReport:
    """Outcome of one drift check."""

    packs_dir: Path
    mode: str
    state: str = "unknown"
    packs: list[str] = field(default_factory=list)
    source_commit: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "packs_dir": str(self.packs_dir),
            "mode": self.mode,
            "state": self.state,
            "packs": self.packs,
            "source_commit": self.source_commit,
            "problems": self.problems,
            "ok": self.ok,
        }

    def render_human(self) -> str:
        lines = [
            f"packs dir: {self.packs_dir}",
            f"mode:      {self.mode}",
            f"state:     {self.state}",
        ]
        if self.source_commit:
            lines.append(f"commit:    {self.source_commit}")
        if self.packs:
            lines.append(f"packs:     {len(self.packs)} — {', '.join(self.packs)}")
        if self.problems:
            lines.append("problems:")
            lines.extend(f"  - {problem}" for problem in self.problems)
        return "\n".join(lines)


# ------------------------------------------------------------- offline validation
def _validate_manifest_shape(manifest: Any) -> list[str]:
    """Structural validation — the half that needs no credential and no source."""
    if not isinstance(manifest, dict):
        return [f"{MANIFEST_NAME} must contain a JSON object"]

    problems: list[str] = []
    schema = manifest.get("schema")
    if schema != MANIFEST_SCHEMA:
        # Refuse rather than validate an unknown layout loosely: a manifest from
        # a newer exporter may mean something different by the same key.
        return [f"unsupported {MANIFEST_NAME} schema {schema!r} (expected {MANIFEST_SCHEMA})"]

    unknown = sorted(set(manifest) - _MANIFEST_KEYS)
    if unknown:
        problems.append(f"{MANIFEST_NAME}: unknown key(s) {', '.join(unknown)}")

    for key in ("source_repo", "source_commit", "source_commit_date", "export_date", "pack_glob"):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            problems.append(f"{MANIFEST_NAME}: missing or empty {key!r}")

    commit = manifest.get("source_commit")
    if isinstance(commit, str) and not _FULL_SHA_RE.match(commit):
        # A short sha is ambiguous; §10.1's pack_repo_commit has to pin exactly.
        problems.append(
            f"{MANIFEST_NAME}: source_commit {commit!r} is not a full 40-hex sha"
        )

    packs = manifest.get("packs")
    if not isinstance(packs, list) or not all(isinstance(p, str) and p for p in packs):
        problems.append(f"{MANIFEST_NAME}: 'packs' must be a list of names")
    elif packs != sorted(set(packs)):
        problems.append(f"{MANIFEST_NAME}: 'packs' must be sorted and unique")

    files = manifest.get("files")
    if not isinstance(files, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in files.items()
    ):
        problems.append(f"{MANIFEST_NAME}: 'files' must be a path -> sha256 object")
    return problems


def _validate_recorded_paths(files: dict[str, str], packs: list[str]) -> list[str]:
    """Recorded paths must be pack-relative and inside a recorded pack."""
    problems: list[str] = []
    for rel in files:
        if rel.startswith("/") or ".." in Path(rel).parts:
            problems.append(f"{MANIFEST_NAME}: unsafe recorded path {rel!r}")
        elif rel.split("/")[0] not in packs:
            problems.append(f"{MANIFEST_NAME}: recorded path {rel!r} is outside the pack list")
    return problems


def unexpected_entries(packs_dir: Path, packs: list[str]) -> list[str]:
    """Anything under ``packs/`` the export could not have produced.

    ``_stage_packs`` uploads the whole directory into the task container, so the
    guard has to cover everything the sha256 map structurally cannot: a link
    (never hashed, never byte-compared — the export refuses links at the source,
    so one here was added by hand), top-level content that is not a pack, an
    empty directory (``git archive`` emits none), and a permission bit, which no
    content comparison can see.

    ``packs`` must be :func:`pack_dirs` output — real directories only. Passing a
    symlinked entry would make the walk below follow it out of ``packs/``.
    """
    problems: list[str] = []
    for entry in sorted(packs_dir.iterdir()):
        rel = entry.name
        if entry.is_symlink():
            problems.append(f"unexpected link under packs/: {rel}")
        elif entry.is_dir():
            if rel.startswith("."):
                problems.append(f"hidden directory under packs/: {rel}")
        elif rel not in ALLOWED_LOOSE_FILES:
            problems.append(f"unexpected loose file under packs/: {rel}")

    for pack in packs:
        for path in sorted((packs_dir / pack).rglob("*")):
            rel = path.relative_to(packs_dir).as_posix()
            if path.is_symlink():
                problems.append(f"unexpected link under packs/: {rel}")
            elif path.is_dir():
                if not any(path.iterdir()):
                    problems.append(f"empty directory under packs/: {rel}")
            elif not path.is_file():
                problems.append(f"not a regular file under packs/: {rel}")
            else:
                mode = path.stat().st_mode
                if mode & _FORBIDDEN_MODE_BITS:
                    problems.append(f"unsafe permissions ({stat.filemode(mode)}) under packs/: {rel}")
    return problems


def check_offline(packs_dir: Path) -> CheckReport:
    """Validate the bundled export against its own manifest. No source needed."""
    report = CheckReport(packs_dir=packs_dir, mode="offline (manifest consistency)")

    if not packs_dir.is_dir():
        report.state = "missing"
        report.problems.append(f"packs dir does not exist: {packs_dir}")
        return report

    present = pack_dirs(packs_dir)
    report.packs = present
    # Runs before the cold-state shortcut: junk left in an empty packs/ still
    # ships into the container. Reported *alongside* the content comparison
    # below and never instead of it — a stray loose file must not mask a
    # hand-edited pack file underneath, or a reviewer fixes one, re-runs, and
    # only then learns about the other.
    report.problems.extend(unexpected_entries(packs_dir, present))
    try:
        manifest = read_manifest(packs_dir)
    except PackExportError as exc:
        report.state = "invalid"
        report.problems.append(str(exc))
        return report

    # Cold arm: no packs and no manifest is a valid, deliberate state (the repo
    # ships this way until the first export lands).
    if manifest is None and not present:
        report.state = "cold (no packs exported)"
        return report
    if manifest is None:
        report.state = "invalid"
        report.problems.append(
            f"{len(present)} pack dir(s) present but no {MANIFEST_NAME}: "
            "packs must be produced by the export tool, not added by hand"
        )
        return report
    if not present:
        report.state = "invalid"
        report.problems.append(f"{MANIFEST_NAME} present but no pack directories exported")
        return report

    report.state = "exported"
    shape_problems = _validate_manifest_shape(manifest)
    if shape_problems:
        report.problems.extend(shape_problems)
        return report

    report.source_commit = manifest["source_commit"]
    recorded_packs: list[str] = manifest["packs"]
    files: dict[str, str] = manifest["files"]

    # These two genuinely block the per-file comparison: recorded paths are
    # resolved on disk below, and a disagreeing pack list makes "which files
    # should exist" undefined. Everything else is reported cumulatively.
    blocking = _validate_recorded_paths(files, recorded_packs)
    if recorded_packs != present:
        blocking.append(
            f"pack list drift: {MANIFEST_NAME} records {recorded_packs} but "
            f"packs/ contains {present}"
        )
    if blocking:
        report.problems.extend(blocking)
        return report

    actual = hash_files(packs_dir, present)
    for rel, digest in files.items():
        if rel not in actual:
            report.problems.append(f"missing file recorded in {MANIFEST_NAME}: {rel}")
        elif actual[rel] != digest:
            report.problems.append(f"content drift (sha256 mismatch): {rel}")
    for rel in actual:
        if rel not in files:
            report.problems.append(f"untracked file under packs/: {rel}")
    return report


# ---------------------------------------------------------------- full validation
def _tree_snapshot(root: Path, packs: list[str]) -> dict[str, tuple[bytes, bool]]:
    """Every file under ``packs`` as ``relative posix path -> (bytes, is_exec)``.

    The executable bit rides along because ``git archive`` reproduces it from the
    tree (100644 vs 100755) — so it is part of "a clean export of that commit",
    and it is the one attribute of a bundled pack file that a content hash can
    never see.
    """
    snapshot: dict[str, tuple[bytes, bool]] = {}
    for pack in packs:
        for path in sorted((root / pack).rglob("*")):
            if path.is_file() and not path.is_symlink():
                is_exec = bool(path.stat().st_mode & stat.S_IXUSR)
                snapshot[path.relative_to(root).as_posix()] = (path.read_bytes(), is_exec)
    return snapshot


def compare_with_source(packs_dir: Path, manifest: dict[str, Any], source: Path) -> list[str]:
    """Re-export the recorded commit and byte-compare it with what is committed."""
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="sb-pack-drift-") as tmp:
        reference = Path(tmp) / "packs"
        try:
            result = export_packs(
                source=source,
                ref=manifest["source_commit"],
                dest=reference,
                # Carry the recorded URL through, so an operator checkout whose
                # origin differs (mirror, local path) is not reported as drift.
                repo_url=manifest["source_repo"],
                # ...and the recorded selection, so a non-default --pack-glob
                # export is compared against the same set it was made from.
                glob=manifest.get("pack_glob", PACK_GLOB),
            )
        except PackExportError as exc:
            return [f"could not re-export {manifest['source_commit'][:12]} from {source}: {exc}"]

        expected = _tree_snapshot(reference, result.packs)
        actual = _tree_snapshot(packs_dir, pack_dirs(packs_dir))
        for rel in sorted(set(expected) | set(actual)):
            if rel not in actual:
                problems.append(f"missing from packs/: {rel}")
            elif rel not in expected:
                problems.append(f"not present at the recorded commit: {rel}")
            elif expected[rel][0] != actual[rel][0]:
                problems.append(f"content differs from the recorded commit: {rel}")
            elif expected[rel][1] != actual[rel][1]:
                problems.append(f"file mode differs from the recorded commit: {rel}")

        for key in _COMPARED_MANIFEST_KEYS:
            if manifest.get(key) != result.manifest.get(key):
                problems.append(
                    f"{MANIFEST_NAME}: {key} does not match a re-export of "
                    f"{manifest['source_commit'][:12]}"
                )
    return problems


def _askpass_env(workdir: Path, token: str) -> dict[str, str]:
    """A ``GIT_ASKPASS`` helper that reads the token from the environment.

    Keeps the credential out of the command line (visible in ``ps``), out of the
    cloned repo's config, and out of the helper script itself.
    """
    helper = workdir / "askpass.sh"
    helper.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "${ENV_REPO_TOKEN}"\n', encoding="utf-8")
    helper.chmod(0o700)
    return {
        "GIT_ASKPASS": str(helper),
        ENV_REPO_TOKEN: token,
    }


def split_credentials(url: str, token: str, user: str) -> tuple[str, str]:
    """Move any credential embedded in ``url`` out of the URL.

    ``SB_PACK_REPO_URL`` may legitimately arrive as
    ``https://user:token@host/12.git``; passing that straight to ``git clone``
    would put the secret on the command line, where any process can read it out
    of ``ps``. Returns ``(url_without_password, effective_token)`` — the caller
    hands the token to git through ``GIT_ASKPASS`` instead.

    Only the *password* half is relocated. A bare ``https://name@host`` userinfo
    is left alone: it selects the account git authenticates as, and nothing here
    can tell a login name from a token-as-username PAT — reading it as a secret
    would break a checkout that authenticates through a credential helper. The
    credential that matters, ``SB_PACK_REPO_TOKEN``, never reaches argv.

    The authority is parsed the way git parses it rather than with ``urlsplit``
    (see :func:`~sidebutton_harbor_agent.pack_export.split_userinfo`), so a
    password containing ``?`` or ``#`` is still recognised as a password.
    """
    if not url.startswith(("http://", "https://")):
        return url, token
    scheme, userinfo, rest = split_userinfo(url)
    named_user = False
    if userinfo:
        embedded_user, _, embedded_token = userinfo.partition(":")
        user = embedded_user or user
        token = embedded_token or token
        # A username the operator spelled out selects the account git
        # authenticates as; only the *password* half is a secret to relocate.
        named_user = bool(embedded_user)
    netloc = f"{user}@{rest}" if (token or named_user) else rest
    return f"{scheme}://{netloc}", token


def fetch_source(url: str, workdir: Path) -> Path:
    """Bare-clone the pack repo into ``workdir`` using the operator's credential.

    Read-only on the remote (``clone`` is on the allow-list for exactly this
    reason). The token is passed via ``GIT_ASKPASS`` and the clone is thrown away
    with the temp dir.
    """
    target = workdir / "source.git"
    clone_url, token = split_credentials(
        url,
        os.environ.get(ENV_REPO_TOKEN, ""),
        os.environ.get(ENV_REPO_USER) or DEFAULT_REPO_USER,
    )
    if token and clone_url.startswith("http://"):
        raise PackExportError(
            "refusing to send the pack-repo credential over cleartext http:// — "
            f"use an https:// URL in {ENV_REPO_URL}"
        )

    # Never let git fall back to an interactive prompt: in CI that is a hang,
    # not a failure.
    env: dict[str, str] = {"GIT_TERMINAL_PROMPT": "0"}
    if token and clone_url.startswith("https://"):
        env.update(_askpass_env(workdir, token))

    # ``--`` so a URL that looks like an option is never read as one.
    _git(["clone", "--bare", "--quiet", "--", clone_url, str(target)], env=env)
    return target


def check(
    packs_dir: Path | None = None,
    *,
    source: Path | None = None,
    fetch: bool = False,
) -> CheckReport:
    """Run the drift check in the strongest mode the inputs allow."""
    packs_dir = packs_dir or default_dest()
    report = check_offline(packs_dir)
    if not report.ok or report.state != "exported" or not (source or fetch):
        if source or fetch:
            # Say why the stronger check did not run, so a log line can never
            # imply more was verified than actually was. A failure is always the
            # reason when there is one — a cold packs/ dir carrying hand-added
            # junk fails, and reporting that as "nothing exported yet" would be
            # the exact overstatement this string exists to prevent.
            reason = (
                "full check skipped: the offline stage already failed"
                if not report.ok
                else "nothing exported yet"
            )
            report.mode = f"offline (manifest consistency) — {reason}"
        return report

    manifest = read_manifest(packs_dir)
    if manifest is None:  # unreachable: state == "exported" already parsed it
        report.problems.append(f"{MANIFEST_NAME} disappeared while checking {packs_dir}")
        return report

    if source:
        report.mode = f"full (re-export from {source})"
        report.problems.extend(compare_with_source(packs_dir, manifest, source))
        return report

    env_token = os.environ.get(ENV_REPO_TOKEN, "")
    url = os.environ.get(ENV_REPO_URL, "")
    if not url:
        if env_token:
            # ``source_repo`` is repo-controlled data: it is read out of a
            # committed file that any pull request can edit. Falling back to it
            # while a read credential is configured would let a one-line change
            # to packs/EXPORT.json point the CI secret at a host of the author's
            # choosing -- and a "full (fetch ...)" PASS could then be produced
            # against a repo the author also populated. The URL a credential is
            # sent to must come from the operator.
            report.problems.append(
                f"refusing to send {ENV_REPO_TOKEN} to the URL recorded in "
                f"{MANIFEST_NAME} (repo-controlled): set {ENV_REPO_URL} explicitly"
            )
            return report
        # No credential to misdirect, so the manifest's own URL is fine.
        url = manifest["source_repo"]
    if not url:
        report.problems.append(
            f"--fetch needs a source URL: set {ENV_REPO_URL} or record source_repo"
        )
        return report

    # The effective token may come from the env *or* from the URL itself — both
    # have to be redacted from anything this prints.
    _, token = split_credentials(
        url, env_token, os.environ.get(ENV_REPO_USER) or DEFAULT_REPO_USER
    )
    report.mode = f"full (fetch {scrub_url(url)})"
    # git's stderr echoes the remote it failed on, so redact the token *and* the
    # raw userinfo it may have been embedded in.
    secrets = (token, split_userinfo(url)[1])
    with tempfile.TemporaryDirectory(prefix="sb-pack-fetch-") as tmp:
        try:
            clone = fetch_source(url, Path(tmp))
        except PackExportError as exc:
            report.problems.append(_redact(str(exc), *secrets))
            return report
        report.problems.extend(
            _redact(problem, *secrets) for problem in compare_with_source(packs_dir, manifest, clone)
        )
    return report


def _redact(text: str, *secrets: str) -> str:
    """Never let a credential reach a public CI log."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


# --------------------------------------------------------------------------- CLI
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sidebutton-harbor-agent-check-packs",
        description=(
            "Drift guard for the bundled skill packs: verify packs/ is still a "
            f"clean export of the commit recorded in packs/{MANIFEST_NAME}. "
            "Degrades to manifest-consistency validation when no read "
            "credential for the private pack repo is configured."
        ),
    )
    parser.add_argument(
        "--packs-dir",
        default=None,
        help="Packs dir to check (default: the bundled src/.../packs/).",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="Local checkout of the account pack repo — enables the full re-export check.",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help=(
            f"Clone the pack repo ({ENV_REPO_URL}, or the manifest's source_repo) "
            f"using {ENV_REPO_TOKEN} and run the full check."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.source and args.fetch:
        print("--source and --fetch are mutually exclusive", file=sys.stderr)
        return 2

    report = check(
        Path(args.packs_dir) if args.packs_dir else None,
        source=Path(args.source) if args.source else None,
        fetch=args.fetch,
    )

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.render_human())

    verdict = "PACK DRIFT CHECK: PASS" if report.ok else "PACK DRIFT CHECK: FAIL"
    print(f"\n{verdict} — {report.mode}", file=sys.stderr if args.json else sys.stdout)
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
