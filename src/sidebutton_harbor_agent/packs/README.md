# Bundled skill packs

This directory holds the SideButton skill packs the adapter loads into the task
container. Each pack is a **subdirectory** (a Claude Code skill: a folder with a
`SKILL.md`); loose files like this README are ignored by the loader.

**Empty by design for the cold arm.** When this directory contains no pack
subdirectories, the adapter runs the base agent with no packs (a clean no-op —
see `SidebuttonAgent.has_packs`). The primed arm's packs (`sb-tb-*`) are exported
here at a pinned commit by the pack-export tickets (T3 / T4); the export commit
is recorded per benchmark arm so any run is re-creatable.

Packs carry **domain-general competency only** (toolchain eras, idioms, debugging
routines) — never task-specific knowledge and nothing keyed to a task id.
