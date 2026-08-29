# Rule: diagnosing a maestro lane that will not advance

Scope: this repository and the ADW runtime it ships. MUST, not advisory.

## Rule

- **Execute the harness's own measurement by hand before writing any fix.** Run it in the
  attempt worktree, under the real binary, for **every runner the plan can name** —
  `pytest` and `vitest` both, not just the one you suspect.
- **The first sufficient explanation is not the explanation.** A refusal string names a
  measurement that returned zero; it does not say why. Two independent causes can produce a
  byte-identical verdict. Keep looking until you have run the measurement, not until you
  have found a story that fits it.
- **Never hand the operator a `run start` / `run resume` line until the fix it depends on
  has been executed against the real binary in the real worktree.** A command on screen is a
  command they will run.
- **Check the scheduler's start time against the file mtime** before claiming a running run
  picks up a patch. Python binds modules at import; a fix applied after `run start` does
  nothing to that process.

## Where to run it

Attempt worktrees survive the run, provisioned, with `node_modules` in place:

```
~/.maestro/<install>/runs/<run_id>/worktrees/<run_id>-<node_id>-a<N>/
```

The verdict that sent the node back is in the ledger, not in pane text:

```bash
sqlite3 ~/.maestro/<install>/lifecycle.sqlite3 \
  "select id, node_id, reason, detail_json from transitions
   where run_id='<run_id>' order by id;"
```

`detail_json` carries the real reason. The `attempts` table does not.

## Evidence rules

- **A green suite proves nothing about a code path no test executes.** Confirm coverage
  exists before trusting the suite about a subject: `grep -l <subject> tests/*.py`.
- **A stubbed `subprocess.run` cannot observe an argv parser.** It replays the stdout you
  scripted. Any claim about how an external tool *reads* its arguments must be tested by
  running that tool.
- **A measurement must never mutate its subject.** Prove a harness command aimed at a
  candidate's own files is read-only by comparing bytes before and after.

## Why this exists

On 2026-08-27/28 `lane-wp6-tests` retried until it was stopped by hand, four runs, zero
nodes merged. Two independent bugs produced the identical verdict
`TESTS_NO_NEW_CASES: no new collected case versus the parent commit`:

1. `_prove_tests_red_at_parent` discarded the node and collected with pytest whatever the
   gate declared; pytest collects nothing from a `.test.ts` file (`407d7d3`).
2. `VitestCaseRunner.collect` built `vitest list --json <paths>`; vitest's `--json` takes an
   optional value, so it **overwrote the tester's committed test file** with its own JSON,
   printed nothing, exited 0 (`5273342`).

The first was shipped as the whole answer and the run failed again identically. Fifteen
seconds running `vitest list` in the a1 worktree would have named both at once.
