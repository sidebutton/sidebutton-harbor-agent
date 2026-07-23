# sidebutton-harbor-agent

SideButton agent adapter for the [Harbor](https://github.com/harbor-framework/harbor) harness, targeting the
[Terminal-Bench 2.1](https://github.com/harbor-framework/terminal-bench-2-1) leaderboard.

**Status: scaffolding.** The adapter implementation, bundled skill packs, and verify-loop config land here as
the benchmark campaign's build phase executes. Nothing in this repo is runnable yet.

## What this repo will contain

| Component | Purpose |
|---|---|
| `sidebutton_harbor_agent/` | Harbor `InstalledAgent` adapter (import path `sidebutton_harbor_agent:SidebuttonAgent`). Installs the public SideButton CLI inside the task container, feeds it the task's `instruction.md`, and passes model / effort / API-key parameters. |
| `packs/` | Domain-general skill packs (`sb-tb-*`), exported at a pinned commit from the authoring repo. The export commit is recorded per benchmark arm so any run is re-creatable. |
| `config/CLAUDE.md` | The in-container verify-before-done loop: self-review against the task's acceptance criteria before finishing. |
| `docs/` | Campaign operator docs (available now) — see below. |

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

## Reproducibility & fairness

Everything a leaderboard maintainer needs to re-run a submission is public:

- The SideButton CLI is public npm; the skill packs ship **in this repo** (never from a private registry).
- Packs carry **domain-general competency only** (toolchain eras, idioms, debugging routines) — no
  task-specific knowledge, nothing keyed to a task id, and pack discovery never reads the benchmark
  dataset repo or its oracle solutions.
- Runs use stock timeouts, resources, and verifiers; the verify loop is transparent for trajectory review.

## License

Apache-2.0 — see [LICENSE](LICENSE).
