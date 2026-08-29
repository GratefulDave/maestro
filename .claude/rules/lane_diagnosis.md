# Rule: diagnosing a maestro lane that will not advance

Scope: this repository and the ADW runtime it ships. MUST, not advisory.

## Rule

- **Execute the harness's own measurement by hand before writing any fix.** Run it under
  the real binary, for **every runner the plan can name** — `pytest` and `vitest` both,
  not just the one you suspect. Recreate a fresh worktree from the immutable input
  commit/artifact. Do not adopt a previous pane, process, or dirty tree.
- **The first sufficient explanation is not the explanation.** A refusal string names a
  measurement that returned zero; it does not say why. Two independent causes can produce a
  byte-identical verdict. Keep looking until you have run the measurement, not until you
  have found a story that fits it.
- **Never hand the operator a `run start` / `run resume` / `run amend` / `run status` line
  until the fix it depends on has been executed against the real binary in the real
  worktree.** A command on screen is a command they will run. Those four verbs are the
  frozen operator surface.
- **Check the scheduler's start time against the file mtime** before claiming a running run
  picks up a patch. Python binds modules at import; a fix applied after `run start` does
  nothing to that process.

## Where to run it

Durable state lives under the deployment's absolute `runtime_state_root` (mode `0700`,
outside the target repository). Do not treat template-source trees or `~/.maestro/...`
attempt directories as live authority.

```
<runtime_state_root>/lifecycle.sqlite3
<runtime_state_root>/worktrees/   # ephemeral; recreate from immutable input, never adopt
```

The stage that names the next work is `lane_state.stage`. The verdict that sent a lane
back is in the immutable artifact (`TEST_REVIEW` / `CODE_REVIEW` / `USER_WAIT` /
`PLAN_AMENDMENT`), not pane text and not an attempts table:

```bash
sqlite3 <runtime_state_root>/lifecycle.sqlite3 \
  "select lane_id, stage from lane_state where run_id='<run_id>' order by lane_id;"
sqlite3 <runtime_state_root>/lifecycle.sqlite3 \
  "select sequence, lane_id, artifact_kind, input_digest, artifact_ref
   from lane_artifacts where run_id='<run_id>' order by sequence;"
```

`lane_state.stage` plus those artifacts are workflow authority. Git candidate /
integration / publication refs identify immutable bytes; they do not encode stage.

## Evidence rules

- **A green suite proves nothing about a code path no test executes.** Confirm coverage
  exists before trusting the suite about a subject: `grep -l <subject> tests/*.py`.
- **A stubbed `subprocess.run` cannot observe an argv parser.** It replays the stdout you
  scripted. Any claim about how an external tool *reads* its arguments must be tested by
  running that tool.
- **A measurement must never mutate its subject.** Prove a harness command aimed at a
  candidate's own files is read-only by comparing bytes before and after.

## Why this exists

Historical incident (2026-08-27/28, pre-artifact-factory runtime): `lane-wp6-tests`
retried until it was stopped by hand, four runs, zero nodes merged. Two independent bugs
produced the identical verdict
`TESTS_NO_NEW_CASES: no new collected case versus the parent commit`:

1. `_prove_tests_red_at_parent` discarded the node and collected with pytest whatever the
   gate declared; pytest collects nothing from a `.test.ts` file (`407d7d3`).
2. `VitestCaseRunner.collect` built `vitest list --json <paths>`; vitest's `--json` takes an
   optional value, so it **overwrote the tester's committed test file** with its own JSON,
   printed nothing, exited 0 (`5273342`).

The first was shipped as the whole answer and the run failed again identically. Fifteen
seconds running `vitest list` in the then-current a1 worktree would have named both at once.
That worktree-survival layout is not current factory authority; diagnose today's runs from
`lane_state.stage` and immutable artifacts under `runtime_state_root`.
