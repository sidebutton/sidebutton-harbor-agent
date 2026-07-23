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
| `src/sidebutton_harbor_agent/packs/` | Bundled skill packs (`sb-tb-*`). Empty for the cold arm; populated at a pinned commit by the pack-export tickets. |
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
version: 0.1.0+cli.1.5.1
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

## Operator smoke run (AC3 — manual, not agent-run)

Run one Terminal-Bench task end-to-end on local Docker to confirm the adapter completes a trial
and produces an ATIF trajectory. **Prerequisites:** Docker running, `harbor` installed, and
`ANTHROPIC_API_KEY` exported.

```bash
export ANTHROPIC_API_KEY=sk-…

harbor run \
  --agent sidebutton_harbor_agent:SidebuttonAgent \
  --dataset terminal-bench-2-1 \
  --include-task-name hello-world \
  --model anthropic/claude-opus-4-8 \
  --agent-kwarg reasoning_effort=high \
  -k 1
```

`--agent-import-path` is the deprecated spelling of `--agent`; both resolve the same import path.

**Expect:** the run reaches a verifier reward for the task, and an ATIF `trajectory_path` is written
under the run's trial directory (inherited from the Claude Code base — that trajectory is what the
leaderboard submission uploads). Pick any quick task with `--include-task-name`.

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
  export commit is recorded per benchmark arm, so any run is re-creatable.

## Development

```bash
pip install -e ".[dev]"
ruff check .
pytest -q
```

CI (ruff + pytest on Python 3.12 & 3.13 + the dry-run smoke) is defined in
[`ci/ci.yml`](ci/ci.yml). Move it to `.github/workflows/ci.yml` to activate it —
it is parked outside `.github/workflows/` only because the automation account
that opened the adapter PR lacks the GitHub `workflow` token scope.

## License

Apache-2.0 — see [LICENSE](LICENSE).
