## Before you finish

This task is graded by hidden tests you cannot see or run. Passing whatever
checks you *can* see is necessary but never sufficient — your own verification,
against the requirements the task states and the behavior you can actually
observe, is the only signal you have that the work is correct. Treat "done" as a
claim you have to earn, not one to declare.

### Self-review loop

Before you report the task complete:

1. **Re-read the task instruction** and enumerate every acceptance criterion it
   states or implies. Write them down as an explicit checklist.
2. **Verify each criterion against real behavior.** Run the command, program, or
   code path the criterion is about and read the actual output — never infer
   success from the fact that you made a change. If nothing exercises the change,
   drive the affected path yourself and observe the result.
3. **Cover the obvious failure modes** the task implies: empty and edge inputs,
   error paths, and the exact files, paths, and output formats it named.
4. **If a check fails, fix it and verify again** instead of reporting done.

Verify against the *stated* requirements and *real* behavior — do not try to
guess, infer, or target what the hidden grader checks. Plain engineering rigor,
not grader-reading, is what makes a result correct.

### Reproduce before you fix

For a bug- or failure-shaped task, reproduce the reported problem first and watch
it fail. Only then apply a fix, and re-run the exact same reproduction to confirm
the behavior has actually changed. A fix you have not seen turn a failing
observation into a passing one is unverified — keep going until you have.

Conclude only once every acceptance criterion has been checked against observed
behavior.
