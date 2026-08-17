<!-- Parent: ../AGENTS.md -->

# From a source document to an executable plan

Maestro executes `maestro-plan.v1`. It does not read audits, interview transcripts, architecture
decks, or HTML specifications, and there is no converter that turns one into a plan. Every such
document enters the pipeline the same way: as a hash-pinned `source_artifacts` entry inside a
`plan-contract.v1` Plan IR authored by a planning skill. That IR — with its bound rendered view and
an independent PASS review receipt — is the single input Maestro accepts.

```
source documents ──► plan-contract.v1 Plan IR  ──► maestro-plan.v1 ──► run
   (audit HTML,       + extensions.maestro          (projection)
    interview,        + bound HTML view
    arch deck)        + PASS review receipt
```

Because there is exactly one entry format, "support a new kind of input document" never means
building a new adapter. It means citing that document as a source in a plan.

## The projection boundary

`plan_contract_ingress.author_from_plan_contract` is an authoring boundary, not a lossy converter.
It verifies the receipt against the exact IR bytes, projects the IR onto a Maestro draft, and
refuses anything it cannot execute:

| Contract element | Becomes |
| --- | --- |
| `lanes[]` | plan nodes |
| `lane.depends_on` | node `needs` |
| `lane.execution_context` | the node gate's `cwd` |
| `verifiers[]` | node gates (`runner`, `argv`, `min_cases`) |
| `source_artifacts[]` | observed evidence |
| `extensions.maestro.outputs` | produced evidence |
| `extensions.maestro.integration_gate` | the merge policy's integration gate |

Two consequences follow from the receipt being bound to the IR bytes:

1. `extensions.maestro` MUST be present before the plan is rendered and reviewed. Adding it to an
   approved IR changes the bytes and the projection refuses with `RECEIPT_IR_MISMATCH`.
2. Repairing a plan means re-rendering, re-validating, and obtaining a fresh receipt — never
   editing an approved IR in place.

An `architecture` plan is refused outright (`ARCHITECTURE_NOT_EXECUTABLE`). It is the anchor a
brownfield plan pins, not something that runs.

## Single repository

Run these in the repository the plan will change.

1. `/arch-review <master-spec>` — author or refresh the approved architecture IR. It produces
   `.maestro/<stem>.plan.json`, `.maestro/<stem>.html`, and `.maestro/<stem>.plan-review.json`.
   Skip only when an approved architecture IR already exists.
2. `/plan-brownfield "<what to build>" <master-spec>` — author one executable work package per
   reviewable unit, each carrying `extensions.maestro`. Place the IR, its bound HTML view, and its
   review receipt under `.maestro/` — `.maestro/<name>.plan.json`, `.maestro/<name>.html`,
   `.maestro/<name>.plan-review.json` — alongside `.maestro/plans/`, where Maestro projects the
   finished plan.
3. `planctl render` → `planctl validate` → `planctl mutate`, then an independent reviewer runs
   `planctl review` and `planctl validate --require-approved`, all with `--repo-root .` so source
   paths in `source_artifacts` resolve from the repository root rather than from `.maestro/`, the
   IR's own directory. Without `--repo-root`, a source cited as `docs/AUDIT.md` would have to be
   written `../docs/AUDIT.md` to reach it from inside `.maestro/`, and Maestro refuses any path
   that escapes with `..` — the two tools would only agree on paths at the repository root.
   `--repo-root .` removes that constraint, which is what lets the IR live in `.maestro/` at all.
   `validate` reproduces every ingress refusal, so a plan that passes here projects.
4. Project and check:

   ```bash
   maestro plan author <plan-name> \
     --from-plan-contract .maestro/<name>.plan.json \
     --plan-contract-receipt .maestro/<name>.plan-review.json \
     --plan-contract-rendered .maestro/<name>.html
   maestro plan validate <plan-name>
   ```

5. `maestro plan finalize <plan-name>` — required before the plan can run, and before it can
   participate in a workspace.

## Choosing a verifier command

Every lane declares one verifier — the command that proves the lane's work happened. Maestro does
not merely run it; it counts how many test cases actually executed and checks that count against
the lane's `min_cases` floor. Counting is why the runner set is closed: Maestro has to parse the
runner's report, so `Gate.runner` is `Literal["pytest", "vitest"]` and nothing else projects.

| Verifying | Command shape |
| --- | --- |
| Python | `pytest <targets>` or `python3 -m pytest <targets>` |
| JavaScript / TypeScript, including React and Next.js | `npx vitest run <targets>` or `vitest <targets>` |

A shell script, a Makefile target, a migration applied with `psql`, or a `curl` health check proves
nothing countable and is refused with `maestro.command`. That is not a gap to work around — it is
the reason the gate means something. Work of that kind is verified by asserting its *effect* from a
test the runner can count:

```python
# tests/test_mdl_schema.py — verifier command: pytest tests/test_mdl_schema.py
def test_mdl_cases_carries_a_docket_number():
    assert "docket_number" in columns_of("mdl_cases")
```

### Practical rules

- **Pass the real argv, never a script alias.** `npm test` and `make test` are refused even when
  they ultimately invoke vitest, because Maestro has to see the selector to know what the gate
  actually covers. Write `npx vitest run src/ingest.test.ts`.
- **Choose vitest over Jest when setting up a new JavaScript or TypeScript repository.** Adding
  tests to such a repository is ordinary project work — `npm i -D vitest @testing-library/react
  jsdom` — and needs no change to Maestro. Choosing Jest does: `Gate.runner` would have to accept
  it and the executor would have to parse its report.
- **Playwright and Cypress cannot currently be gates** for the same reason. End-to-end coverage
  either waits on that change or is asserted through a countable test.
- **One verifier per lane.** The projection gives each node exactly one gate, so a lane binding
  zero or several verifiers is refused with `maestro.lane_gate`. When a lane genuinely needs two
  independent checks, either widen one command's selector to cover both or split the lane.

## Several repositories

There is no multi-repository Plan IR. Each repository runs the whole single-repository sequence to
a PASS finalization receipt; the repositories are then bound by one `maestro-workspace.v1` manifest
that declares participation and cross-repository ordering and nothing else.

```bash
maestro workspace author --from workspace-draft.json --out workspace.json --root .
maestro workspace validate --manifest-file workspace.json
```

`workspace author` fills each writer's `plan_digest` and `base_commit` from its stored plan and
refuses a writer whose finalization receipt is missing, not PASS, or bound to different plan bytes.
A manifest therefore cannot be authored ahead of the work it coordinates. See the `plan-workspace`
skill for the draft shape and the full refusal list.

## Which skill authors what

| Input | Skill | Emits | Executable |
| --- | --- | --- | --- |
| a mapped codebase, a master spec | `arch-review` | `plan_kind: architecture` | no — anchor only |
| an approved architecture IR plus a change request | `plan-brownfield` | `plan_kind: brownfield` | yes |
| a greenfield request | `planf3` | `plan_kind: implementation` | yes |
| finalized plans in several repositories | `plan-workspace` | `maestro-workspace.v1` | yes |

The shared contract, the `extensions.maestro` shape, and every diagnostic that mirrors an ingress
refusal are documented in the `plan-contract` skill.
