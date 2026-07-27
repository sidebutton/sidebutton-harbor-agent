"""Export the ``sb-tb-*`` skill packs from the account pack repo into ``packs/``.

The account pack repo is the **source of truth** for the packs; this public
adapter repo carries a *frozen export* pinned to one commit of it (plan §5.3).
Nothing points a task container at the private registry: a leaderboard
maintainer re-runs the submission from public sources only, so the packs have to
ship inside this repo. Without tooling the two repos drift silently — the named
risk in plan §13 — hence this exporter plus the drift guard in
:mod:`sidebutton_harbor_agent.pack_check`.

    $ sidebutton-harbor-agent-export-packs --source ../pack-repo --commit 4ea7705

**One-way, by construction.** Every git invocation goes through :func:`_git`,
which refuses any subcommand outside :data:`READ_ONLY_GIT_SUBCOMMANDS` — there is
no code path from this repo back to the account repo. The source is read at a
*commit* (``git archive``), never from the working tree, so a dirty checkout
cannot leak uncommitted content into a public export.

**Reproducible.** Same source commit -> byte-identical ``packs/``: content comes
from the pinned tree, every git call pins the end-of-line settings so the
operator's own git configuration cannot change the exported bytes, the manifest
serializes deterministically (fixed key order, sorted lists), and ``export_date``
honours ``SOURCE_DATE_EPOCH``.

**Atomic.** The new content is extracted and verified in a temp dir before
``dest`` is touched, so a failure anywhere leaves the previous export intact.

The written manifest (``packs/EXPORT.json``) records what an arm's parameter
block needs — ``source_commit`` is the §10.1 ``pack_repo_commit`` — plus a
``files`` map of sha256 digests, which is what lets the credential-less half of
the drift guard still turn red on a hand-edited pack.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Manifest file written next to the exported packs. A loose file, so the
#: adapter's loader ignores it (``SidebuttonAgent.pack_skill_dirs`` takes
#: subdirectories only).
MANIFEST_NAME = "EXPORT.json"

#: Manifest format version. Bump when a field changes meaning; the checker
#: refuses a manifest it does not understand rather than validating it loosely.
MANIFEST_SCHEMA = 1

#: Packs to export, as a glob over the source repo's top-level directories.
#: Deliberately a pattern and not a hardcoded list of the eight §7 packs: the
#: manifest records whatever was actually exported, and the drift guard pins it
#: from then on.
PACK_GLOB = "sb-tb-*"

#: The only git subcommands this repo may ever run. Enforced in :func:`_git`, so
#: the "one-way" rule (§5.3) is a property of the code, not a convention:
#: ``push``/``commit``/``send-pack`` and friends raise before reaching git.
#: ``clone``/``fetch`` write **into a local temp dir only** — they still only
#: *read* the remote (used by the checker's credentialed mode).
READ_ONLY_GIT_SUBCOMMANDS = frozenset(
    {"archive", "clone", "config", "fetch", "ls-tree", "rev-parse", "show"}
)

#: Git config forced on every invocation, so an export is a function of the
#: pinned commit alone and not of the operator's ``~/.gitconfig``. Without
#: ``core.autocrlf``/``core.eol`` pinned, ``git archive`` applies the same
#: end-of-line conversion a checkout would: an operator running with
#: ``autocrlf=true`` exports CRLF and gets 48 different sha256s for the same
#: commit, and the credentialed CI re-export then reports drift nobody can
#: reproduce. That is AC1 ("same source commit -> byte-identical output"), so it
#: is pinned here rather than left to the environment.
DETERMINISTIC_GIT_CONFIG = ("core.autocrlf=false", "core.eol=lf", "core.quotepath=false")

#: Loose files whose presence marks a directory as an export destination.
#: ``export_packs`` deletes every pack subdirectory of its ``--dest``, so a
#: mistyped path would otherwise destroy an unrelated tree; see
#: :func:`_assert_mirrorable`.
MIRROR_SENTINELS = frozenset({MANIFEST_NAME, "README.md"})


class PackExportError(RuntimeError):
    """A problem an operator can act on (bad source, unknown sha, no packs)."""


@dataclass
class ExportResult:
    """What an export produced — the manifest plus where it landed."""

    dest: Path
    manifest: dict[str, Any]

    @property
    def packs(self) -> list[str]:
        return list(self.manifest["packs"])

    def render_human(self) -> str:
        manifest = self.manifest
        lines = [
            f"dest:          {self.dest}",
            f"source_repo:   {manifest['source_repo']}",
            f"source_commit: {manifest['source_commit']}",
            f"commit_date:   {manifest['source_commit_date']}",
            f"export_date:   {manifest['export_date']}",
            f"packs ({len(self.packs)}):",
            *(f"  - {name}" for name in self.packs),
            f"files:         {len(manifest['files'])} (sha256 recorded in {MANIFEST_NAME})",
        ]
        return "\n".join(lines)


# --------------------------------------------------------------------- git seam
def _git(
    args: list[str],
    *,
    cwd: Path | None = None,
    binary: bool = False,
    env: dict[str, str] | None = None,
) -> Any:
    """Run one **read-only** git command and return its stdout.

    Raises :class:`PackExportError` on a non-zero exit (with git's own stderr,
    which is what an operator needs), and refuses outright to run a subcommand
    that could write to the source repo.
    """
    subcommand = args[0]
    if subcommand not in READ_ONLY_GIT_SUBCOMMANDS:
        raise PackExportError(
            f"refusing to run 'git {subcommand}': the export path is read-only "
            f"(allowed: {', '.join(sorted(READ_ONLY_GIT_SUBCOMMANDS))})"
        )
    if subcommand == "config" and not any(a.startswith("--get") for a in args):
        # `git config <key> <value>` writes. Only the read forms are allowed.
        raise PackExportError("refusing to run 'git config' in a write form")

    # Fixed argv, never a shell string: nothing here is interpolated by a shell.
    config = [flag for setting in DETERMINISTIC_GIT_CONFIG for flag in ("-c", setting)]
    completed = subprocess.run(
        ["git", *config, *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        check=False,
        env={**os.environ, **env} if env else None,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace").strip()
        raise PackExportError(f"git {subcommand} failed: {stderr or 'no stderr'}")
    return completed.stdout if binary else completed.stdout.decode("utf-8", "replace")


# ------------------------------------------------------------------ source read
def resolve_commit(source: Path, ref: str) -> str:
    """Resolve ``ref`` (branch, tag, short sha, ``HEAD``) to a full 40-hex sha."""
    if not source.is_dir():
        raise PackExportError(f"source is not a directory: {source}")
    try:
        _git(["rev-parse", "--git-dir"], cwd=source)
    except PackExportError as exc:
        raise PackExportError(f"source is not a git repository: {source}") from exc
    # ``^{commit}`` makes a tag or tree resolve to (or fail as) a commit.
    sha = _git(["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"], cwd=source)
    return sha.strip()


def commit_date(source: Path, sha: str) -> str:
    """Committer date of ``sha`` in ISO-8601 — a property of the commit, so it
    stays identical across re-exports (unlike ``export_date``)."""
    return _git(["show", "-s", "--format=%cI", sha], cwd=source).strip()


def source_repo_url(source: Path) -> str:
    """Best-effort ``origin`` URL of the source checkout, credentials stripped.

    The recorded URL lands in a **public** repo, so an authenticated remote
    (``https://x-access-token:<token>@host/12.git``) must never be written
    verbatim — :func:`scrub_url` removes the userinfo.
    """
    try:
        url = _git(["config", "--get", "remote.origin.url"], cwd=source).strip()
    except PackExportError:
        return ""
    return scrub_url(url)


def split_userinfo(url: str) -> tuple[str, str, str]:
    """Split ``scheme://userinfo@host/path`` into ``(scheme, userinfo, rest)``.

    Deliberately *not* :func:`urllib.parse.urlsplit`: git ends the authority at
    the first ``/`` and treats everything before the last ``@`` as userinfo,
    while ``urlsplit`` also stops at ``?`` and ``#``. For a credential
    containing one of those (``https://u:p?w@host/12.git``) ``urlsplit`` puts no
    ``@`` in ``netloc`` at all, and a scrubber built on it hands the secret
    straight through into a public manifest. Parsing the authority the way git
    does closes that.

    ``userinfo`` is ``""`` when there is none; ``rest`` keeps the leading
    ``/``. A URL with no ``scheme://`` (scp-style ``git@host:path``) yields
    ``("", "", url)`` — its ``user`` is a host account name, not a credential.
    """
    scheme, sep, remainder = url.partition("://")
    if not sep:
        return "", "", url
    authority, slash, path = remainder.partition("/")
    userinfo, at, host = authority.rpartition("@")
    return scheme, userinfo if at else "", host + slash + path


def scrub_url(url: str) -> str:
    """Strip any ``user[:password]@`` userinfo from a URL.

    Guards the one place a credential could otherwise be committed to a public
    repo: the manifest's ``source_repo``.
    """
    scheme, userinfo, rest = split_userinfo(url)
    if not userinfo:
        return url
    return f"{scheme}://{rest}"


def discover_packs(source: Path, sha: str, glob: str = PACK_GLOB) -> list[str]:
    """Top-level directories of the pinned tree matching ``glob``, sorted.

    Everything else in the source repo — the generator scripts, ``index.json``,
    ``FAIRNESS.md``, ``scripts/``, the seeded example pack — is not a TB pack and
    is never exported.
    """
    # ``-z`` because git C-quotes non-ASCII names in its default output
    # (``"sb-tb-caf\303\251"``), which would silently fail the glob and drop a
    # pack from the export without a word.
    listing = _git(["ls-tree", "-d", "--name-only", "-z", "--full-tree", sha], cwd=source)
    names = [name for name in listing.split("\0") if name]
    return sorted(name for name in names if fnmatch.fnmatch(name, glob))


def tree_files(source: Path, sha: str, packs: list[str]) -> set[str]:
    """Every **blob** path under ``packs`` in the pinned tree.

    Used to prove the archive we extracted is the whole tree: ``git archive``
    honours ``export-ignore`` in ``.gitattributes``, so a file present in the
    source could otherwise be dropped from a "frozen mirror" silently.
    """
    listing = _git(["ls-tree", "-r", "-z", "--full-tree", sha, "--", *packs], cwd=source)
    paths: set[str] = set()
    for record in listing.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        fields = meta.split()
        # <mode> <type> <oid>. Gitlinks (submodules) are not files to export.
        if len(fields) >= 2 and fields[1] == "blob":
            paths.add(path)
    return paths


def extract_packs(source: Path, sha: str, packs: list[str], dest: Path) -> None:
    """Extract the pinned pack trees into ``dest`` via ``git archive``.

    ``git archive <sha> -- <paths>`` reads the **committed** tree, so a dirty or
    stale working tree in the operator's checkout cannot reach the export.
    """
    raw = _git(["archive", "--format=tar", sha, "--", *packs], cwd=source, binary=True)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
        # A pack is content, not a filesystem graph. Refusing links keeps the
        # invariant the drift guard depends on -- *every* file under packs/ is a
        # regular file with a recorded sha256 -- and keeps a symlink out of a
        # directory that gets uploaded into a task container.
        links = [m.name for m in tar.getmembers() if m.issym() or m.islnk()]
        if links:
            raise PackExportError(
                "refusing to export symlinks/hardlinks (packs must be plain files): "
                + ", ".join(sorted(links)[:5])
            )
        # ``data`` filter (3.12+) rejects absolute paths, ``..`` escapes and links
        # pointing outside the destination — an untrusted-archive guard we get for
        # free, and cheap insurance on a path that writes into a repo.
        tar.extractall(path=dest, filter="data")


# ---------------------------------------------------------------------- manifest
def hash_files(dest: Path, packs: list[str]) -> dict[str, str]:
    """sha256 of every file under the exported pack dirs, keyed by POSIX-relative
    path and sorted, so the manifest is stable across platforms."""
    digests: dict[str, str] = {}
    for pack in packs:
        for path in sorted((dest / pack).rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            digests[path.relative_to(dest).as_posix()] = digest
    return dict(sorted(digests.items()))


def export_timestamp() -> str:
    """Export time, honouring ``SOURCE_DATE_EPOCH`` so a reproducibility check can
    pin it and compare the manifest byte-for-byte."""
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if not epoch:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        seconds = int(epoch)
    except ValueError as exc:
        raise PackExportError(f"SOURCE_DATE_EPOCH is not an integer: {epoch!r}") from exc
    try:
        return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError, OSError) as exc:
        # An integer that is not a representable date — same operator mistake,
        # so it deserves the same message rather than a traceback.
        raise PackExportError(f"SOURCE_DATE_EPOCH is out of range: {epoch!r}") from exc


def build_manifest(
    *,
    source_repo: str,
    source_commit: str,
    source_commit_date: str,
    export_date: str,
    packs: list[str],
    files: dict[str, str],
    pack_glob: str = PACK_GLOB,
) -> dict[str, Any]:
    """Assemble the manifest with a fixed key order (part of byte-identity)."""
    return {
        "schema": MANIFEST_SCHEMA,
        "source_repo": source_repo,
        "source_commit": source_commit,
        "source_commit_date": source_commit_date,
        "export_date": export_date,
        # Recorded so the drift guard re-exports with the same selection an
        # operator used; otherwise a non-default --pack-glob reads as drift.
        "pack_glob": pack_glob,
        "packs": sorted(packs),
        "files": files,
    }


def serialize_manifest(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, indent=2) + "\n"


def read_manifest(dest: Path) -> dict[str, Any] | None:
    """Parse ``dest/EXPORT.json``; ``None`` when absent (the cold state)."""
    path = dest / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        # Not a JSONDecodeError: without this the drift guard dies with a
        # traceback instead of reporting a FAIL an operator can read.
        raise PackExportError(f"{path} is not valid UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PackExportError(f"{path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        # An unreadable manifest is an operator problem like any other -- it must
        # reach the CLI as a clean "check failed", not as a traceback.
        raise PackExportError(f"{path} could not be read: {exc}") from exc


def pack_dirs(dest: Path) -> list[str]:
    """Pack directories actually present in ``dest``.

    Mirrors ``SidebuttonAgent.pack_skill_dirs``: every non-hidden subdirectory is
    a pack the adapter would stage — so the drift guard must see them all, not
    only the ones matching :data:`PACK_GLOB`.

    Symlinks are excluded on purpose. A link is never a pack (the export refuses
    to write one), and treating it as one would make every walker built on this
    function follow it *out* of ``packs/`` — the drift guard would then enumerate
    whatever ``sb-tb-x -> /etc`` points at. The checker reports links separately,
    so excluding them here loses no coverage.
    """
    if not dest.is_dir():
        return []
    return sorted(
        p.name
        for p in dest.iterdir()
        if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
    )


def stale_export_entries(dest: Path) -> list[Path]:
    """Entries a previous export owns, which a new one replaces wholesale.

    Every directory under ``dest`` is pack content — *including* a hidden or
    symlinked one. :func:`pack_dirs` deliberately hides those from the adapter's
    view (it mirrors ``pack_skill_dirs``), but a mirror write still has to clear
    them: leaving one behind emits a freshly exported tree that this repo's own
    drift guard then rejects, and ``_stage_packs`` uploads the whole directory
    into the task container regardless of what the loader sees.

    Regular files are left alone: they are adapter-owned (``README.md``) or
    rewritten by this export (the manifest).
    """
    return [p for p in sorted(dest.iterdir()) if p.is_symlink() or p.is_dir()]


# ------------------------------------------------------------------------ export
def default_dest() -> Path:
    """The bundled ``packs/`` directory that ships in the wheel."""
    return Path(__file__).resolve().parent / "packs"


def _assert_mirrorable(dest: Path) -> None:
    """Refuse to mirror into a directory that is not an export destination.

    The mirror write removes *every* pack subdirectory of ``dest`` — including
    ones the export could not have produced, which is deliberate (a hand-added
    dir is drift). That makes a mistyped ``--dest`` destructive: pointed one
    level too high it would delete ``config/`` and every other subpackage. An
    absent or empty directory is fine, and so is one already carrying the
    adapter's loose files, which is what ``packs/`` always looks like.
    """
    if dest.exists() and not dest.is_dir():
        raise PackExportError(f"destination exists and is not a directory: {dest}")
    if not dest.is_dir():
        return
    entries = list(dest.iterdir())
    if not entries or any(entry.name in MIRROR_SENTINELS for entry in entries):
        return
    raise PackExportError(
        f"refusing to mirror into {dest}: it is not empty and holds neither "
        f"{MANIFEST_NAME} nor README.md, so it does not look like a packs "
        "directory — check --dest (the bundled one is "
        "src/sidebutton_harbor_agent/packs)"
    )


def export_packs(
    *,
    source: Path,
    ref: str = "HEAD",
    dest: Path | None = None,
    repo_url: str | None = None,
    glob: str = PACK_GLOB,
) -> ExportResult:
    """Mirror the pinned ``sb-tb-*`` packs from ``source`` into ``dest``.

    Mirror semantics: every **directory** in ``dest`` is replaced (so a pack
    deleted upstream disappears here too, and a hidden or symlinked one a
    hand-edit left behind does not survive a re-export), while loose files —
    ``README.md``, and the manifest we are about to rewrite — are preserved.

    All-or-nothing: the export is staged in full and swapped in only once it is
    complete, so a failure leaves ``dest`` exactly as it was.
    """
    dest = dest or default_dest()
    _assert_mirrorable(dest)
    sha = resolve_commit(source, ref)
    packs = discover_packs(source, sha, glob)
    if not packs:
        raise PackExportError(
            f"no directories matching {glob!r} at commit {sha[:12]} in {source} — "
            "wrong repo or wrong commit?"
        )

    recorded_url = repo_url if repo_url is not None else source_repo_url(source)
    if not recorded_url:
        # Writing an empty source_repo would produce a manifest this repo's own
        # drift guard rejects; fail here, where the fix is obvious.
        raise PackExportError(
            f"could not determine the source repo URL from {source} "
            "(no 'origin' remote) — pass --repo-url explicitly"
        )
    # Everything that can fail is resolved before a single byte of dest changes:
    # a half-export is worse than none.
    date = export_timestamp()
    expected = tree_files(source, sha, packs)
    committed = commit_date(source, sha)

    dest.mkdir(parents=True, exist_ok=True)
    # Stage the complete export beside dest, then swap it in. Extraction refuses
    # links and the mirror-faithfulness check below can reject the archive —
    # doing either after clearing dest would leave packs/ emptied, with a stale
    # manifest still describing the packs that are no longer there.
    with tempfile.TemporaryDirectory(prefix=".sb-pack-export-", dir=dest.parent) as tmp:
        staging = Path(tmp) / "packs"
        staging.mkdir()
        extract_packs(source, sha, packs, staging)

        files = hash_files(staging, packs)
        if set(files) != expected:
            missing = sorted(expected - set(files))
            raise PackExportError(
                "export is not a faithful mirror of the tree — "
                f"{len(missing)} file(s) in the source are missing from the archive "
                f"(a .gitattributes export-ignore?): {', '.join(missing[:5])}"
            )

        manifest = build_manifest(
            source_repo=scrub_url(recorded_url),
            source_commit=sha,
            source_commit_date=committed,
            export_date=date,
            packs=packs,
            pack_glob=glob,
            files=files,
        )
        (staging / MANIFEST_NAME).write_text(serialize_manifest(manifest), encoding="utf-8")

        for stale in stale_export_entries(dest):
            # rmtree refuses a symlink, and following one would delete outside
            # packs/ — unlink the link itself and leave its target alone.
            if stale.is_symlink():
                stale.unlink()
            else:
                shutil.rmtree(stale)
        for entry in sorted(staging.iterdir()):
            shutil.move(str(entry), str(dest / entry.name))
    return ExportResult(dest=dest, manifest=manifest)


# --------------------------------------------------------------------------- CLI
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sidebutton-harbor-agent-export-packs",
        description=(
            "Export the sb-tb-* skill packs from a checkout of the account pack "
            "repo into this repo's packs/, pinned to one commit and recorded in "
            f"packs/{MANIFEST_NAME}. Read-only on the source; never writes back."
        ),
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Path to a checkout of the account pack repo (read-only).",
    )
    parser.add_argument(
        "--commit",
        default="HEAD",
        help="Commit/branch/tag to export (default: HEAD). Recorded in full.",
    )
    parser.add_argument(
        "--dest",
        default=None,
        help="Destination packs dir (default: the bundled src/.../packs/).",
    )
    parser.add_argument(
        "--repo-url",
        default=None,
        help="URL recorded as source_repo (default: the source's origin, credentials stripped).",
    )
    parser.add_argument(
        "--pack-glob",
        default=PACK_GLOB,
        help=f"Glob selecting pack dirs in the source (default: {PACK_GLOB}).",
    )
    parser.add_argument("--json", action="store_true", help="Emit the manifest as JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = export_packs(
            source=Path(args.source),
            ref=args.commit,
            dest=Path(args.dest) if args.dest else None,
            repo_url=args.repo_url,
            glob=args.pack_glob,
        )
    except PackExportError as exc:
        print(f"export failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result.manifest, indent=2))
    else:
        print(result.render_human())
    print(
        f"\nexported {len(result.packs)} pack(s) at {result.manifest['source_commit'][:12]} — "
        f"record it as pack_repo_commit in the arm's param block.",
        file=sys.stderr if args.json else sys.stdout,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
