# sidebutton-harbor-agent

SideButton agent adapter for the [Harbor](https://github.com/harbor-framework/harbor) harness,
targeting the [Terminal-Bench 2.1](https://github.com/harbor-framework/terminal-bench-2-1)
leaderboard.

Import path (Harbor `--agent`): **`sidebutton_harbor_agent:SidebuttonAgent`**

## What it is

The public `sidebutton` npm CLI is a *workflow / skill-pack* tool, not an autonomous coder. The
"SideButton runtime" that competes on Terminal-Bench is therefore **a base coding agent (Claude
Code) + SideButton skill packs + a verify-before-done loop**. This adapter models exactly that by
subclassing Harbor's `ClaudeCode` installed agent, which means it inherits ATIF trajectory
emission, provider error classification, and the model / effort / API-key plumbing unchanged.

On top of the base agent it:

- **installs the public SideButton CLI** (`npm i -g sidebutton@<pin>`) inside the task container;
- **feeds the task's `instruction.md`** to the agent (inherited) and **appends the verify-loop
  guidance** (`config/CLAUDE.md`) to it;
- **loads any skill packs** present under `packs/` by flattening them into Claude Code's skills
  directory. When `packs/` has no packs (the *cold arm*) this is a clean no-op;
- sets **no** verifier, timeout, or resource overrides — runs stay on stock settings.

| Component | Purpose |
|---|---|
| `src/sidebutton_harbor_agent/agent.py` | `SidebuttonAgent(ClaudeCode)` — the adapter. |
| `src/sidebutton_harbor_agent/dryrun.py` | `sidebutton-harbor-agent-dryrun` — prints & validates the in-container command line, no container. |
| `src/sidebutton_harbor_agent/trajectory_check.py` | `sidebutton-harbor-agent-check-trajectory` — host-side check that the verify loop visibly ran in a trial's ATIF trajectory (see [Smoke run](#smoke-run-ac3--needs-docker-not-runnable-in-a-container-less-agent-vm)). |
| `src/sidebutton_harbor_agent/pack_export.py` | `sidebutton-harbor-agent-export-packs` — one-way export of the `sb-tb-*` packs from the account pack repo at a pinned commit (see [Pack export & drift guard](#pack-export--drift-guard)). |
| `src/sidebutton_harbor_agent/pack_check.py` | `sidebutton-harbor-agent-check-packs` — drift guard: `packs/` must still be a clean export of the commit recorded in `packs/EXPORT.json`. |
| `src/sidebutton_harbor_agent/packs/` | Bundled skill packs (`sb-tb-*`) + `EXPORT.json` provenance. Empty for the cold arm; populated at a pinned commit by the export tool. |
| `src/sidebutton_harbor_agent/config/CLAUDE.md` | The verify-before-done loop appended to every task instruction: enumerate the stated acceptance criteria and check each against real behavior, reproduce-before-fix for bug-shaped tasks, and "hidden tests exist — your own verification is the only signal". Domain-general and transparent for trajectory review. |
| `docs/` | Campaign operator docs — per-arm parameter schema + operator runbook (see [Running a benchmark arm](#running-a-benchmark-arm)). |

## Running a benchmark arm

An *arm* is one clone of the Test epic carrying a parameter block that drives a single `harbor run`.
The durable definition of an arm — the parameter schema and the operator runbook — lives under `docs/`:

| Doc | Purpose |
|---|---|
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Operator runbook: author the 89-task epic, clone per arm, fill + validate the parameter block, `harbor run` per arm type, record results, gate, and submit. Executable after the epic B2 bring-up. |
| [`docs/arm-params.schema.json`](docs/arm-params.schema.json) | JSON Schema (draft 2020-12) for the per-arm parameter block: 15 fields, `cold ⇒ no packs` / `primed ⇒ packs` rule, and a `#/$defs/submission` profile for the all × ≥5 × public submission arm. |
| [`docs/arm-params.example.json`](docs/arm-params.example.json) · [`.cold.`](docs/arm-params.cold.example.json) · [`.submission.`](docs/arm-params.submission.example.json) | Reference parameter blocks (primed / cold / submission) doubling as validation fixtures. |

Validate a parameter block:

```bash
check-jsonschema --schemafile docs/arm-params.schema.json arm.json
# submission arm additionally:
check-jsonschema --schemafile docs/arm-params.submission.schema.json arm.json
```

## Install

```bash
pip install "git+https://github.com/sidebutton/sidebutton-harbor-agent"
# or, from a checkout:
pip install -e ".[dev]"
```

Requires Python ≥ 3.12 and `harbor >= 0.20, < 0.21` (installed automatically). Everything a
leaderboard maintainer needs to re-run a submission is public and ships in this package: the CLI is
public npm, the packs live in `packs/`, and the verify loop is `config/CLAUDE.md`.

## Parameters (Terminal-Bench §10.1 clone-param block)

Parameters map 1:1 to the base agent — no custom parsing:

| §10.1 param | How to pass | In-container effect |
|---|---|---|
| backend model id | `--model anthropic/claude-opus-4-8` | `ANTHROPIC_MODEL` (provider prefix stripped for the official API) |
| reasoning effort | `--agent-kwarg reasoning_effort=high` | `claude … --effort high` |
| API key | host `ANTHROPIC_API_KEY`, or `--agent-env ANTHROPIC_API_KEY=…` | passed through to the CLI |
| priming (cold vs primed) | populate / empty `packs/` (or `--agent-kwarg packs_dir=…`) | packs flattened into Claude Code skills, or no-op |

Adapter-specific `--agent-kwarg`s: `packs_dir`, `sidebutton_cli_version`, `verify_loop`
(`true`/`false`), `verify_loop_path`.

## Dry run (no container) — AC2

Inspect and validate exactly what would run in the container:

```bash
sidebutton-harbor-agent-dryrun --model anthropic/claude-opus-4-8 --effort high
```

```text
agent:   sidebutton
version: 0.2.0+cli.1.5.1
model:   anthropic/claude-opus-4-8
packs:   (none — cold arm)

env (in-container):
  ANTHROPIC_MODEL=claude-opus-4-8
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
  IS_SANDBOX=1

setup commands:
  (none)

agent command:
  $ claude --verbose --output-format=stream-json --effort high --permission-mode=bypassPermissions --print

dry-run OK — invocation is valid (no overrides, model & effort wired).
```

`--json` emits the same as machine-readable JSON (status line on stderr, so stdout stays pure).
A non-zero exit means the invocation failed validation (e.g. an override token was present).

## Smoke run (AC3 — needs Docker; not runnable in a container-less agent VM)

Run 2–3 Terminal-Bench tasks end-to-end on local Docker to confirm the adapter completes a trial,
produces an ATIF trajectory, **and that the verify-before-done loop visibly executed in it**.
**Prerequisites:** Docker running, `harbor` installed, and `ANTHROPIC_API_KEY` exported (a literal
key — an OAuth-only credential does not reach the in-container CLI).

```bash
export ANTHROPIC_API_KEY=sk-…

harbor run \
  --agent sidebutton_harbor_agent:SidebuttonAgent \
  --dataset terminal-bench/terminal-bench-2-1 \
  --include-task-name openssl-selfsigned-cert \
  --include-task-name regex-log \
  --include-task-name modernize-scientific-stack \
  --model anthropic/claude-opus-4-8 \
  --agent-kwarg reasoning_effort=high \
  -k 1 --n-concurrent 1
```

Notes on the invocation:

- **`--dataset` needs the `org/name` id.** A slash-less name is read as a *legacy registry* dataset
  and fails with `Dataset 'terminal-bench-2-1' (version: 'None') not found`.
- **The task ids are dataset-verified** (`SMOKE_TASK_NAMES` in
  [`trajectory_check.py`](src/sidebutton_harbor_agent/trajectory_check.py), pinned by
  `tests/test_docs.py`). They are criteria-dense — each instruction states requirements the
  self-review turn can be seen enumerating — and `modernize-scientific-stack` is failure-shaped, so
  it also exercises the reproduce-before-fix pillar. To substitute one, confirm it exists with
  `harbor datasets download terminal-bench/terminal-bench-2-1` **and** update `SMOKE_TASK_NAMES` —
  the README and that constant are pinned to each other, so changing only one fails the test.
- One run over three repeated `--include-task-name` flags (the RUNBOOK's subset-iteration form) puts
  all trials under one job directory, so the check below covers them in one pass. `--n-concurrent 1`
  serializes the image pulls for a small VM; raise it if disk and CPU allow.
- Leave `verify_loop` at its default (**on**): `verify_loop=false` is the cold/ablation arm and would
  invalidate the smoke. Keep timeouts and resources at stock (fairness), and `--upload` off.
- `--agent-import-path` is the deprecated spelling of `--agent`; both resolve the same import path.

**Expect:** each run reaches a verifier reward, and an ATIF trajectory is written to
`<trial>/agent/trajectory.json` (inherited from the Claude Code base — that trajectory is what a
leaderboard submission uploads).

Then check that the loop actually ran, per trial or across the whole job directory:

```bash
sidebutton-harbor-agent-check-trajectory jobs/<job-name>
```

```text
PASS jobs/<job-name>/openssl-selfsigned-cert.1/agent/trajectory.json
  steps: 11 (last step_id 11)
  last edit at step: 9
  ran + observed after last edit: [10]
  restated task criteria after last edit: [10, 11]
  restated task criteria anywhere: [5, 6, 7, 8, 9, 10, 11]
  self-review excerpt:
    | Re-checking the criterion I just fixed.
    |
    | All six acceptance criteria are now checked against observed output. Done.
FAIL jobs/<job-name>/regex-log.1/agent/trajectory.json
  steps: 4 (last step_id 4)
  last edit at step: 3
  ran + observed after last edit: (none)
  restated task criteria after last edit: (none)
  restated task criteria anywhere: (none)

1/2 trajectory(ies) show a self-review turn.
```

It reports the self-review turn — the agent restating the task's criteria **and** running checks
whose output it observed, *after* its last edit — and prints the matching excerpt to attach as AC3
evidence. `FAIL` above is the failure mode this deliverable exists to prevent: the last action was an
edit and the agent declared success without running anything. Exit code is non-zero when any
trajectory looks like that. Because the loop is iterative ("if a check fails, fix it and verify
again"), the *last* edit is often a fix the self-review itself found — hence the two criteria lines:
the verdict uses the post-edit window, while `anywhere` points at the opening enumeration so you can
lift the fullest excerpt.

The two signals are keyword and tool-call heuristics over free-form agent prose, so treat the verdict
as a signal and the excerpt as the evidence. The tool is host-side submission QA: it reads a
trajectory harbor already wrote, never enters a container, and cannot affect a reward. `--json` emits
the same reports machine-readably.

## Pack export & drift guard

The `sb-tb-*` skill packs are **authored** in the benchmark account's private pack repo and
**published** here as a frozen export pinned to one commit of it. A leaderboard maintainer re-runs a
submission from public sources only, so nothing may point a task container at that private registry —
the packs have to ship inside this repo. The flow is strictly one-way: this repo never writes back.

```bash
# 1. Export (operator, from a read-only checkout of the account pack repo)
sidebutton-harbor-agent-export-packs --source ../pack-repo --commit <sha>

# 2. Verify, then commit packs/ + packs/EXPORT.json
sidebutton-harbor-agent-check-packs
```

The export reads the **committed** tree (`git archive` at the pinned commit), so a dirty checkout
cannot leak uncommitted content into a public repo, and it selects exactly the top-level `sb-tb-*`
directories — the generator scripts, `index.json` and any seeded example pack stay behind. Writes are
mirror-shaped: pack subdirectories are replaced wholesale (a pack dropped upstream disappears here
too) while loose files like this repo's `packs/README.md` are preserved. Exported packs are
**bit-identical to the authored source**, metadata included, which is why re-exporting the same
commit is byte-for-byte reproducible.

`packs/EXPORT.json` records the provenance an arm's parameter block needs — `source_commit` is the
§10.1 `pack_repo_commit` — plus a sha256 per exported file:

```json
{
  "schema": 1,
  "source_repo": "https://git.sidebutton.com/<account>.git",
  "source_commit": "<full 40-hex sha>",
  "source_commit_date": "2026-07-27T12:00:00+00:00",
  "export_date": "2026-07-27T16:40:00Z",
  "packs": ["sb-tb-algo", "sb-tb-build", "..."],
  "files": { "sb-tb-algo/_skill.md": "<sha256>", "...": "..." }
}
```

`EXPORT.json` is a loose file, so the adapter's loader ignores it (only subdirectories are packs).
Set `SOURCE_DATE_EPOCH` to pin `export_date` when reproducing an export byte-for-byte.

### Drift guard modes

`sidebutton-harbor-agent-check-packs` runs in CI (the `packs` job in [`ci/ci.yml`](ci/ci.yml)) and
degrades cleanly, because the private-repo fetch is credential-gated. It always prints which mode ran.

| Mode | Needs | Catches |
|---|---|---|
| **offline** (default) | nothing | hand-edited, added or deleted files under `packs/`; pack list ≠ manifest; missing or malformed manifest; a short (ambiguous) `source_commit` |
| **full** (`--source <checkout>` or `--fetch`) | a checkout, or `SB_PACK_REPO_TOKEN` (+ `SB_PACK_REPO_URL`) | everything above **plus** a *coordinated* edit where the file and its recorded hash were changed together, and packs re-synced from a newer commit without moving the pin |

Offline mode cannot see a coordinated file+hash tamper — its hashes are self-referential by
construction. That is what the credentialed mode is for; configure the secret where the full check
matters. The cold state (no packs, no manifest) is valid and passes.

`--fetch` clones the pack repo read-only into a temp dir using `SB_PACK_REPO_TOKEN`, passed via
`GIT_ASKPASS` so it never reaches the command line or the clone's config, and redacted from output.
Credentials are also stripped from any URL recorded in the manifest. The tool shells out to `git`
(present on GitHub runners).

**Cold arm, once packs are bundled.** `has_packs()` is a property of the packs directory, so after an
export the default is *primed*. A cold arm then passes an explicit empty directory:

```bash
harbor run --agent sidebutton_harbor_agent:SidebuttonAgent --agent-kwarg packs_dir=/tmp/no-packs …
```

## Fairness & reproducibility

- **Public everything.** SideButton CLI is public npm; packs ship in this repo (never a private
  registry); the verify loop is `config/CLAUDE.md` in-tree and transparent for trajectory review.
- **No overrides.** The adapter sets no verifier, timeout, or resource overrides; the dry-run
  validator and the unit tests assert their absence. Runs use stock timeouts and resources.
- **Domain-general packs only.** Packs carry competency (toolchain eras, idioms, debugging
  routines), never task-specific knowledge or anything keyed to a task id; pack discovery never
  reads the benchmark dataset or its oracle solutions.
- **Robustness.** A pack-layer failure degrades to the base agent rather than erroring the trial —
  a flaky layer must never cost a reward.
- **Pinned & recorded.** `version()` reports `<adapter>+cli.<sidebutton-cli-version>`; the packs'
  export commit is recorded in `packs/EXPORT.json` **and** per benchmark arm, and CI fails on drift
  between the two, so any run is re-creatable.

## Development

```bash
pip install -e ".[dev]"
ruff check .
pytest -q
```

CI (ruff + pytest on Python 3.12 & 3.13 + the dry-run smoke + the
[pack drift guard](#drift-guard-modes)) is defined in
[`ci/ci.yml`](ci/ci.yml). Move it to `.github/workflows/ci.yml` to activate it —
it is parked outside `.github/workflows/` only because the automation account
that opened the adapter PR lacks the GitHub `workflow` token scope.

## License

Apache-2.0 — see [LICENSE](LICENSE).
