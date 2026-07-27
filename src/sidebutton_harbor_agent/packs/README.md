# Bundled skill packs

This directory holds the SideButton skill packs the adapter loads into the task
container. Each pack is a **subdirectory**; loose files like this README and
`EXPORT.json` are ignored by the loader (`SidebuttonAgent.pack_skill_dirs`) —
though note `_stage_packs` uploads the *whole* directory, which is why the drift
guard refuses anything here it did not export.

**Claude Code registers a skill folder by its `SKILL.md`.** The packs authored in
the account repo are `skill-pack.json` + `_skill.md` + module dirs, so as
exported they stage but are not expected to register as skills. The export is a
verbatim frozen mirror by contract (plan §5.3), so any layout transform belongs
in adapter staging, not here — it needs its own ticket and a smoke run before the
primed arm.

**Empty by design for the cold arm.** When this directory contains no pack
subdirectories, the adapter runs the base agent with no packs (a clean no-op —
see `SidebuttonAgent.has_packs`).

**Generated, not hand-edited.** The primed arm's packs (`sb-tb-*`) are authored
in the benchmark account's pack repo and mirrored here by
`sidebutton-harbor-agent-export-packs` at a pinned commit, recorded in
`EXPORT.json` alongside a sha256 per file. They are bit-identical to the authored
source, metadata included — so an edit made here is drift, not an improvement:
change the pack in the account repo and re-export. CI enforces this
(`sidebutton-harbor-agent-check-packs`); see the README's *Pack export & drift
guard* section.

Packs carry **domain-general competency only** (toolchain eras, idioms, debugging
routines) — never task-specific knowledge and nothing keyed to a task id.
