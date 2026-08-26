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
3. `maestro plan gate <plan-name>` — the author renders, validates, and mutates the IR.
4. `maestro plan review <plan-name>` — a second person, holding the reviewer's key rather than the
   author's, reviews the mutated IR and re-validates it against the approved-receipt requirement:
   `planctl review` followed by `planctl validate --require-approved`. **No model is dispatched.**
   The verb takes the reviewer's configured identity and vendor as strings and signs a receipt
   bound to the IR bytes by `ir_sha256`; it does not contain a semantic reading of `surface` or
   `effects`. Those two fields have no independent reader before a run begins (#31). Whatever
   judgement the review represents is the reviewer's own, made before they run the verb — the verb
   records that it happened and to which exact bytes, and nothing more.
5. `maestro plan ship <plan-name>` — projects the reviewed IR into a `maestro-plan.v2` plan,
   validates the projection, and finalizes it. Finalizing is required before the plan can run, and
   before it can participate in a workspace. **No model is dispatched here either.**
   `maestro plan finalize` runs the deterministic obligations and, if the plan is eligible, writes
   the signed receipt itself — verdict PASS, rubric version `maestro-deterministic.v1`, no cells,
   and a reviewer identity of `(deterministic, plan_validate, <digest>)`, because that is what
   actually judged the bytes. It used to launch a reviewer in a pane and derive the verdict from
   that reviewer's per-cell answers; the prompt it handed over was the matrix of check ids, the
   plan digest, and a path to write to, and it never contained the plan. A lifecycle transition
   caused by a model's answers about a plan it was not shown is what §1.2 forbids, so the reviewer
   was removed rather than repaired. Eligibility is unchanged, and an ineligible plan still exits 2
   with typed blockers and no receipt.

The version the projection emits is `maestro-plan.v2`, and a run refuses anything older. The bump
is not a structural change: v2 carries the same in-plan types, the same fields, and the same
obligations as v1, and `plan_model.PlanV2` subclasses the frozen `Plan` for exactly that reason.
What moved is what the projection *puts in* an agent node's `instruction`. It used to write the
lane's title there and drop `requirements[].text`, so every builder and every reviewer downstream
was handed a summary of the lane's contract instead of the contract itself (§19 M26). A populated
field cannot be audited by its consumers: the fixed projection went on emitting the same
`maestro-plan.v1` the degenerate one emitted, so a plan shipped before the fix and a plan shipped
after it were indistinguishable to a runtime carrying every fix. The version string is the only
channel that difference can travel on.

`_load_runnable_plan` — the one function that turns plan bytes into a plan a run will execute, and
so the point `run start`, `run resume`, and a workspace participant all cross — refuses a plan whose
declared version is outside `_RUNNABLE_PLAN_SCHEMA_VERSIONS` with
`RUN_PLAN_SCHEMA_VERSION_UNRUNNABLE`, naming the plan, the version found, and the remedy. It is an
allowlist rather than a denylist: a version registered later and not added to it refuses rather than
runs. A `maestro-plan.v1` plan stays readable, canonical, validatable, and finalizable; only running
it is refused.

If you hold a plan shipped before the bump, re-ship it. There is no upgrade function and no in-place
edit (§6.3) — the requirement text a v1 plan is missing is not recoverable from the projected plan,
only from the IR it was projected from. Run `maestro plan ship <plan-name>` again against the same
`.maestro/<name>.plan.json` and its existing `.maestro/<name>.plan-review.json`: nothing needs to be
re-approved, because the IR bytes and the receipt bound to them are unchanged. The projection is
simply re-run by code that carries the requirement text, and the plan is re-validated and
re-finalized under a new digest.

`gate`, `review`, and `ship` are the plan CLI's shipped surface — one verb, one argument,
everything else resolved from `maestro.config.yaml`. They landed in commit 6707e50 (PR #8) and are
registered on the `plan` subparser in `maestro.py`. Run them directly; "What each verb runs" below
documents the calls each one wraps, for reading the trace rather than for hand-running the steps.

`gate` and `review` are two separate commands, on purpose, because of who is allowed to hold what.
A review receipt only proves that someone other than the author looked at the plan if the author
is structurally incapable of producing one, which means the HMAC key that signs receipts must never
sit in the author's environment. Maestro owns that key itself rather than handing it to whoever
types the command: `maestro bootstrap` mints it once into the repository's state root, next to the
Ed25519 signing material it already manages, and `maestro plan review <plan-name>` — which takes
the plan name and nothing else — injects the key into the `planctl review` subprocess directly. No
shell ever holds it, no export line exists, and there is no reviewer identity to pass. (An
environment-variable override remains, for a reviewer who deliberately supplies their own key, the
same way the Ed25519 signing key already supports one; the ordinary path never touches it.)

Bootstrap's environment files are split along the same line, because for a while they were not.
`maestro bootstrap` writes two files into `<state-root>/<repo>/keys/`, both 0600: `maestro.env`,
carrying the verify key, the signing seed, and the route verify key — everything author-side work
needs, and nothing that can make a gate refuse — and `reviewer-hmac.env`, carrying the reviewer
binding and nothing else. Source `maestro.env` freely. Nothing in Maestro's supported path reads
`reviewer-hmac.env`: it exists only for driving `planctl review` directly, outside Maestro, as
`/arch-review` does for an architecture IR, and sourcing it in an authoring shell is exactly what
`REVIEWER_KEY_PRESENT` is there to catch. While the two bindings shared one file, an operator who
sourced it to finalize or start a run had the reviewer's key in their shell, `gate` correctly
refused their own plan, and the only way forward was unsetting and re-exporting a variable by hand
between stages — which is where the key got lost. Maestro's own bootstrap was what put it there.

If your state root still holds a combined `maestro.env`, re-run `maestro bootstrap`: it rewrites
that file in place without the reviewer binding and writes `reviewer-hmac.env` beside it. No key is
regenerated and no receipt already signed is invalidated — `provision_keys` reuses the key material
it finds. Re-running does **not** clean a shell that already sourced the old file, so in any shell
that did, `unset PLANCTL_REVIEWER_HMAC_KEY` (or open a new one) before `maestro plan gate`.

`gate` enforces the same boundary from the other side: it refuses outright if that key is present
in its own environment, because a gate command able to see the reviewer's key would no longer prove
that
gating and reviewing happen on two sides of a line neither side can cross. Nobody has to remember to
keep the key separate — Maestro checks for it before either command does anything else.

### What each verb runs

```bash
# maestro plan gate <plan-name>
planctl render <name>.plan.json --repo-root .
planctl validate <name>.plan.json --repo-root .
planctl mutate <name>.plan.json --repo-root .

# maestro plan review <plan-name>  — runs with the reviewer's key, never the author's
planctl review <name>.plan.json --repo-root .
planctl validate <name>.plan.json --repo-root . --require-approved

# maestro plan ship <plan-name>
maestro plan author <plan-name> \
  --from-plan-contract .maestro/<name>.plan.json \
  --plan-contract-receipt .maestro/<name>.plan-review.json \
  --plan-contract-rendered .maestro/<name>.html
maestro plan validate <plan-name>
maestro plan finalize <plan-name>
```

Every `planctl` call carries `--repo-root .` because the IR lives in `.maestro/` while the
`source_artifacts` paths it cites are repo-relative (`docs/AUDIT.md`, not `.maestro/docs/AUDIT.md`).
Without `--repo-root`, planctl resolves those paths relative to the IR's own directory, so that same
source would have to be written `../docs/AUDIT.md` to be reached from inside `.maestro/` — and
Maestro refuses any path that escapes with `..`. `--repo-root .` is what lets the IR live in
`.maestro/` at all; `gate` and `review` pass it on every call so `planctl` and Maestro agree on
where "repo-relative" starts.

`validate` reproduces every ingress refusal a later step would hit, so a plan that passes it inside
`gate` is a plan that projects inside `ship`.

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

## What a tests lane must declare

A `tests` lane writes the tests a later build lane has to make pass. It also declares **what those
tests must prove**, in a field the runtime executes rather than reads.

You author it on the lane's **verifier**, in the plan-contract IR:

```json
"verifiers": [
  {
    "verifier_id": "verify-refund-tests",
    "lane_ids": ["lane-refund-tests"],
    "command": "pytest tests/test_refund.py",
    "min_executed": 2,
    "test_strength": { … the object below … }
  }
]
```

`maestro plan ship` projects it onto the tests node and emits `maestro-plan.v4`. A tests lane
whose verifier declares none is refused at projection with
`UNMAPPABLE_VERIFIERS:<lane>.test_strength`, and a build lane's verifier that declares one is
refused the same way — it would be a field nothing reads. The projected node looks like this:

```json
"test_strength": {
  "coverage": [
    {"requirement_id": "R-refund-01", "aspect": "positive",
     "case_selector": "test_refund_pays_the_balance", "min_cases": 1},
    {"requirement_id": "R-refund-01", "aspect": "negative",
     "case_selector": "test_refund_rejects_a_negative_amount", "min_cases": 1}
  ],
  "falsifiability": {
    "strategy": "baseline_absent",
    "mutation": null,
    "expected_failing_selector": "test_refund",
    "expected_reason_pattern": "refund must be positive"
  }
}
```

`maestro run start` refuses a plan whose tests lanes declare none —
`RUN_TEST_STRENGTH_CONTRACT_ABSENT`. An existing run of an older plan is unaffected and stays
resumable; it is pinned to the contract it was created under. There is no upgrade function and no
in-place edit (§6.3): add the block to the IR and re-ship.

**Why the plan declares the case names.** §1.2 forbids keying a lifecycle transition on an agent's
account of its own work, so the tester is never asked whether it covered a requirement. The plan
says which case ids would prove it did, and code counts them. `case_selector` is a substring of the
case id (`path::case` under pytest, `file::full name` under vitest), because it has to mean the same
thing under both runners and they disagree about `-k` and `--testNamePattern`.

**Every requirement needs a positive and a negative obligation.** A contract that names only happy
paths does not parse. That rule exists because of one measured run: a tests lane reached `MERGED` on
four non-skipped cases and every implementation candidate it existed to gate was independently
rejected — the tests asserted what the code should do and never what it must refuse.

**The falsifiability strategy is executed, not stored.**

| strategy | what runs | when to use it |
| --- | --- | --- |
| `baseline_absent` | the candidate's own cases, in a tree where the implementation does not exist yet | test-first: the paired build lane has not run |
| `controlled_mutation` | the same cases against a scratch copy with `mutation.paths` reverted to the plan's `base_commit` | brownfield: the behaviour already exists and the control must remove it |

The failure must **match** `expected_reason_pattern` — a Python regular expression over the runner's
own reported reason. A random exception, an import error, or a collection failure is refused by
name: it proves the tree does not import, not that the cases discriminate. A `controlled_mutation`
may only revert paths the **paired build lane** declares as outputs, and never the tests lane's own
files; both are refused at admission with `TEST_STRENGTH_COHERENT`.

Nothing is mutated in place. The control materialises the candidate commit into a scratch directory
outside every worktree, reverts there, and discards it; the attempt worktree's cleanliness is
compared across the whole operation and a control that leaves dirt behind is refused.

**What the pair costs downstream.** The build lane that needs this tests lane cannot start until the
tests lane has an *accepted candidate* — strong measured evidence **and** a passed independent
review, both bound to one immutable sha. When it does run, its own commit must carry the accepted
test files byte-identically, and the accepted contract's coverage obligations must be green against
it. The merge verifies that exact pair and refuses anything else.

## What a lane's outputs must cover

A lane's `outputs` are not a summary of what it will touch. They are its entire write permission.
Exactly one lane may own a path — a second lane declaring it is refused with
`SINGLE_OUTPUT_OWNER` — and the attempt's permission check convicts any diff that touches a path
the lane did not declare. Nothing in between exists: a file is yours to write or it is not.

So a lane's instruction has to be dischargeable inside that permission. Write the instruction and
the outputs together, and ask one question before shipping: **could an agent satisfy this sentence
by changing only these files?**

An instruction that fails that question is unsatisfiable from its first attempt, and the failure is
silent. The node reviewer correctly rejects every diff that does not do what the instruction says;
the permission check would correctly reject every diff that does. The lane then spends its whole
review budget alternating between the two before ending `BLOCKED` with `REVIEW_BUDGET_EXHAUSTED` —
with a green gate and a rising case count every attempt, because the work it *was* permitted to do
was fine.

**Nothing downstream will catch this for you.** A plan-finalization reviewer used to be asked it as
a rubric cell, `node.writes_are_sufficient`; that reviewer is gone, and `maestro plan finalize` is
deterministic now, so no check between the authored bytes and the run reads an instruction against
its `outputs`. It is a question for the authoring round and for `maestro plan review`, where a
person holding the reviewer's key reads the IR — and the whole cost of getting it wrong is paid at
run time, one lane's review budget at a time.

The shape that trips it is an end-to-end behavioural claim over code the lane does not own:

```
# refused — the property is about src/dispatch.py, which another lane owns
lane: enrichment-ordering
outputs: [src/enrichment_gate.py, tests/test_enrichment_gate.py]
instruction: "run enrichment only after binary and identity validation"
```

Two ways to fix it, and the choice is a real one:

- **Give the lane the wiring.** Add the production file to its `outputs`, which means no other lane
  may own it. Use this when the ordering *is* the work.
- **Split it, and make the wiring a lane.** One lane produces the module and says so — "add
  `src/enrichment_gate.py` exposing `enrich_after_validation`; wiring it into dispatch belongs to
  `lane-dispatch-wiring`" — and a downstream lane owns `src/dispatch.py` and calls it. Both lanes
  pass, because each instruction stops at the files its lane may write.

A lane whose outputs are all new files is not itself suspect; it is the ordinary case, and most
lanes in a greenfield package look like that. What is suspect is a lane whose *sentence* only comes
true once some file it may not write changes. If nothing in the plan ever wires the new module into
the codebase, that is a different defect and a different check —
`plan.intent_is_accomplished_by_the_graph`, which asks whether the lanes taken together leave
anything load-bearing unowned.

## What a review rejection costs

A rejection is not a fresh start. When code review rejects an attempt, the next attempt branches
from the commit that attempt produced and the builder is asked to change it — the reviewer's
located findings name what to change, and the lane's `instruction` still leads the prompt and still
bounds the work. Consecutive rejections are therefore rounds of refinement on one artifact rather
than independent implementations, and a `review_ceiling` of six buys six rounds rather than six
one-shot guesses.

Two consequences for authoring. First, write the `instruction` so it reads correctly a second time,
against a tree that already contains a partial answer: a sentence that only makes sense as a
green-field brief ("create `src/gap_policy.py`") is weaker on a repair attempt than one that names
the property the file must have. Second, a repair is refused, and the attempt falls back to a fresh
base, whenever a sibling lane has merged since the rejection was measured — the rejected commit no
longer sits on the integration head, and handing the builder a tree missing the sibling's work
would be worse than restarting. Lanes that finish close together therefore lose repair chains to
each other, which is one more reason to keep a phase's lanes genuinely independent.

The chain is bounded: `REPAIR_CHAIN_LIMIT` caps consecutive repairs, a repair whose findings rose
above the rejection it repaired ends the chain, and the number of chains is bounded by
`review_ceiling`, which the loop does not change. Nothing here adds attempts; it changes only what
an attempt the budget had already paid for starts from.

`maestro run convergence` is how you read this from outside. It reports findings per attempt for
each lane, which is the trend a repair chain is supposed to produce, and it distinguishes a run
that ended without converging from one that has not converged *yet*: a live run reports "not
converged yet" with cause `run still in flight`, and a run whose scheduler cannot be found on this
machine reports that it cannot say rather than asserting either answer.

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
