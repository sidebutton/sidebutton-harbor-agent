<!--
  PLACEHOLDER verify-before-done loop.

  The adapter appends this file's contents to the task instruction the agent
  receives (see SidebuttonAgent.render_instruction). The real, tuned content is
  authored in B3 / SCRUM-1836; this skeleton exists so the injection mechanism
  is wired and testable now. Keep it domain-general — no task-specific or
  benchmark-keyed guidance (campaign fairness).
-->

## Before you finish

Before you report the task as done, verify your own work:

1. **Re-read the task.** List the acceptance criteria the instruction states or
   implies. For each, point to the concrete change or output that satisfies it.
2. **Exercise it.** Run the tests, build, or command the task is about and read
   the actual output — do not assume success. If nothing runs the change, drive
   the affected path directly and observe the result.
3. **Check the obvious failure modes.** Empty/edge inputs, error paths, and the
   exact file/paths the task named.
4. **If a check fails, fix it and re-verify** rather than reporting done.

Only conclude once every acceptance criterion has been checked against observed
behavior.
