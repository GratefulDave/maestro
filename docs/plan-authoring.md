<!-- Parent: ../AGENTS.md -->

# From a source document to an executable plan

Maestro is a nine-stage artifact factory. It does not read audits, interview transcripts,
architecture decks, or HTML specifications, and there is no converter that turns one into a plan.
Every such document enters the pipeline the same way: as a hash-pinned `source_artifacts` entry
inside a `plan-contract.v1` Plan IR authored by a planning skill. That IR — with its bound rendered
view and an independent PASS review receipt — is the single input the plan compiler accepts.

```
source documents ──► plan-contract.v1 Plan IR  ──► approved plan revision ──► run
   (audit HTML,       + extensions.maestro            (compiler-admitted)
    interview,        + bound HTML view
    arch deck)        + PASS review receipt
```

Because there is exactly one entry format, "support a new kind of input document" never means
building a new adapter. It means citing that document as a source in a plan.

The shipped operator surface after an approved plan exists is only:

```text
uv run adws/maestro.py run start <approved-plan> --repo <target-worktree-root> --main-ref <ref>
uv run adws/maestro.py run resume <run-id>
uv run adws/maestro.py run amend <approved-plan> --run <run-id>
uv run adws/maestro.py run status <run-id>
```

`run start` must execute from the stamped deployment (`adws/maestro.py`), not from Maestro,
the-library, or any other template source (`RUN_REPOSITORY_MISMATCH`). Ledger, vault, locks,
receipts, copied plans, and ephemeral worktrees live only under the deployment's absolute
`runtime_state_root` (mode `0700`, outside the target repository). Every start, resume, amend, and
status revalidates `runtime_state_fingerprint` before reading or mutating run state.

Authoritative contract: `MAESTRO_architecture.md`.

## The compiler boundary

The plan compiler admits a plan if and only if these objective properties hold. It does not judge
produced-symbol reachability, narrative quality, or other generic semantics.

- Schema and required fields are present and well-typed.
- Every `needs` ID exists in the same plan.
- The dependency graph is acyclic.
- Declared outputs are exact normalized repository-relative POSIX file paths, never directories or
  globs.
- Paths have no absolute, empty, `.`, or `..` components.
- No duplicate, equal, ancestor, or descendant ownership conflicts exist across lanes.
- Each lane declares public acceptance criteria.
- Integration order is deterministic from the DAG.

Runtime path comparison is byte-exact after that normalization. It never follows a candidate
symlink.

An `architecture` plan is refused as executable work (`ARCHITECTURE_NOT_EXECUTABLE`). It is the
anchor a brownfield plan pins, not something that runs.

Repairing a plan means a new approved revision (and, once a run exists, `run amend`), never editing
an approved IR in place.

## Nine-stage lane execution

Exactly these stages exist. The persisted `lane_state.stage` field is the sole durable workflow
authority. Git commits and sealed artifact digests identify immutable inputs and outputs; they do
not independently encode stage.

```text
PLANNED
WRITING_TESTS
REVIEWING_TESTS
TESTS_SEALED
BUILDING
REVIEWING_CODE
READY_TO_MERGE
MERGED
WAITING_FOR_USER
```

Product flow for every ready lane:

1. Materialize `LANE_PLAN` from the approved plan artifact (`PLANNED` → `WRITING_TESTS`).
2. Private test author emits `TEST_DRAFT` (private draft digest/ref plus public behavioral
   contract).
3. Test reviewer emits `TEST_REVIEW`: `PASS` seals; `REVISE` returns to the author with four-key
   actionable findings.
4. `SEALED_TEST_BUNDLE` records vault digest/reference. Private bytes are absent from the run
   repository and from every builder input.
5. Builder emits `BUILDER_OUTPUT` bound to plan revision, integration base SHA, sealed-test digest,
   and an immutable candidate ref/SHA. The builder receives the public contract, architecture
   constraints, allowed paths, prior redacted review, and the sealed digest. It never receives
   private source, fixtures, selectors, expected literals, or vault paths.
6. Code reviewer runs the sealed tests against the candidate and emits `CODE_REVIEW`. `PASS`
   advances to merge; `REVISE` returns to `BUILDING` with redacted findings.
7. `INTEGRATION_MERGE` records `before_sha`, accepted candidate SHA, and `after_sha`. Each accepted
   lane merges exactly once into `refs/maestro/integration/<run-id>`.
8. A dependent lane's first `BUILDING` input includes every needed lane's accepted integration
   artifact.
9. When every lane is `MERGED`, final integration review evaluates the integration commit with all
   sealed tests. `PASS` permits exactly-once publication to `main` with an immutable receipt;
   `REVISE` waits for `run amend`.

`REVISE` is artifact data, not a stage. Review roles are stages of the lane, never synthetic DAG
nodes. Resume after process death recreates the current incomplete stage from its last immutable
input. No live actor, pane, dirty worktree, or process is adopted.

A `REVISE` finding must include violated requirement, observed behavior, required behavior, and
implementation area. It must not include private test source, fixtures, selectors, expected
literals, or vault paths.

## Single repository

Run these in the repository the plan will change.

1. `/arch-review <master-spec>` — author or refresh the approved architecture IR. It produces
   `.maestro/<stem>.plan.json`, `.maestro/<stem>.html`, and `.maestro/<stem>.plan-review.json`.
   Skip only when an approved architecture IR already exists.
2. `/plan-brownfield "<what to build>" <master-spec>` or `/planf3` — author one executable work
   package per reviewable unit, each carrying `extensions.maestro`. Place the IR, its bound HTML
   view, and its review receipt under `.maestro/`.
3. Obtain an independent PASS review receipt bound to the exact IR bytes. Adding
   `extensions.maestro` after that receipt changes the bytes and is refused.
4. `uv run adws/tools/plan_author_cli.py --from-plan-contract <ir> --receipt <receipt> --out <plan> --repo <target-worktree-root>`
   — verifies the receipt against the IR bytes, projects lanes, runs the same objective compiler
   as `run start`, and writes canonical plan bytes once (`PLAN_EXISTS` on a second call).
   Authoring writes a plan file and starts no run, so it is a tool rather than an operator verb;
   the operator surface stays frozen at `run start`, `run resume`, `run amend`, `run status`.
5. `uv run adws/maestro.py run start <approved-plan> --repo <target-worktree-root> --main-ref <ref>`
   — creates the run, initial plan revision, complete DAG projection, and `PLANNED` lanes in one
   transaction, then creates the integration ref. Operator execution is only from the stamped
   `adws/` deployment.
6. `run resume <run-id>` continues the next incomplete stage from the last accepted immutable
   artifact. After an explicit pause it restores the recorded stage/input. After
   `AMENDMENT_REQUIRED` it leaves the lane waiting.
7. `run amend <approved-plan> --run <run-id>` is the only verb that may apply a `PLAN_AMENDMENT`. Named already-merged lanes must
   change `spec_digest` (hence `lane_projection_digest`); `needs`/output changes on merged lanes are
   refused.
8. `run status <run-id>` derives run status from durable rows after revalidating
   `runtime_state_fingerprint`.

There is no `retry`, `skip`, `abandon`, or `attempt salvage` verb. Those mechanisms are withdrawn.

## Public contract a tests lane must declare

A tests lane authors private tests that later judge a builder. What the compiler and builder see is
the **public behavioral contract**, not the test source.

Author public acceptance criteria on the lane. Declared outputs are the implementation files the
paired build lane may write — never the private test paths. The sealed bundle's public payload is
acceptance criteria plus declared outputs plus `sealed_digest`. Private draft bytes stay in the
vault.

**The builder cannot read those tests.** That is the shipped contract, not a future option. Sealed
tests never enter the run repository, the builder worktree, builder refs, rev-list, or fetch paths.
Anyone auditing a builder prompt and finding private test source, fixtures, selectors, or expected
literals is looking at a leak.

A `REVISE` from code review may name the violated public requirement and the implementation area. It
must not quote the private assertion.

### Practical authoring rules

- **Write the public acceptance so it is falsifiable without leaking the encoding.** "Negative
  amounts are refused" is a contract. The private `assert` message is not.
- **Keep private tests out of declared outputs.** A path the builder is allowed to write cannot also
  be the hidden suite.
- **One owner per path.** Exactly one lane may own a path — a second lane declaring it is refused.
  A lane's instruction has to be dischargeable inside that permission.
- **Do not invent a second gate command for the builder.** Code review runs the sealed suite in a
  harness scratch tree composed from the candidate commit plus vault blobs. The builder does not
  receive runner argv that names private paths.

## What a lane's outputs must cover

A lane's `outputs` are not a summary of what it will touch. They are its entire write permission.
Exactly one lane may own a path, and a candidate that touches an undeclared path is refused. Write
the instruction and the outputs together, and ask one question before shipping: **could an agent
satisfy this sentence by changing only these files?**

The compiler does not read an instruction against its outputs. That question belongs to the
authoring round and the independent plan review. The cost of getting it wrong is paid at run time:
code review `REVISE` loops, not a review-budget exhaustion state.

The shape that trips it is an end-to-end behavioural claim over code the lane does not own:

```
# refused — the property is about src/dispatch.py, which another lane owns
lane: enrichment-ordering
outputs: [src/enrichment_gate.py]
instruction: "run enrichment only after binary and identity validation"
```

Two ways to fix it:

- **Give the lane the wiring.** Add the production file to its `outputs`, which means no other lane
  may own it. Use this when the ordering *is* the work.
- **Split it.** One lane produces the module; a downstream lane owns `src/dispatch.py` and calls
  it. Both pass because each instruction stops at the files its lane may write.

A lane whose outputs are all new files is the ordinary greenfield case. What is suspect is a lane
whose *sentence* only comes true once some file it may not write changes.

## Where a lane's seam falls

The section above asks whether an agent could satisfy the instruction by changing only the declared
outputs. There is a second question with the same shape and a different subject: **could the tests
observe the answer through those outputs alone?** A lane can pass the first and fail the second, and
when it does, nothing in the plan says so — the refusal arrives at run time, phrased as a product
failure.

`lane-wp7-gw-issue-build` is the worked example. It owned `commerce.py`, its instruction was
satisfiable by editing `commerce.py`, and it was correct on the first question. Its acceptance posted
an issued token to `/v1/faers/dpa`, whose upstream in the test environment is a `SourceHandler`
stand-in owned by a different lane. That stand-in routes a fixed list of paths and returns 404 for
everything else, so the route under test raised `SOURCE_ERROR` on every attempt. The lane could not
fix the stand-in — it did not own the file — and could not pass without it. Three attempts, one
identical failure, and the lane parked with no candidate.

Two rules keep a seam where the lane can reach it:

- **One adapter is a hypothetical seam; two adapters is a real one.** A stand-in that exists only so
  one lane's tests can run is indirection, not a boundary. If a lane's acceptance depends on
  substituting something, the substitution point and the lane's outputs belong to the same lane.
- **The interface is the test surface.** A lane's declared outputs *are* its interface. An assertion
  that has to travel through a sibling's fixture to reach its subject is describing a module boundary
  that was drawn in a different place than the lane boundary.

The fix is the same pair offered above — give the lane the wiring, or split so the assertion stops at
files its lane may write — applied to the test path rather than the production path. When authoring a
tests lane, trace one acceptance case from its entry point to its subject and name every file it
passes through. Any of them the lane does not own is the seam this section is about.

## What a review rejection costs

A rejection is not a fresh start and it is not a retry budget. `TEST_REVIEW(REVISE)` returns the
lane to `WRITING_TESTS` with the same `LANE_PLAN` and four-key findings. `CODE_REVIEW(REVISE)`
returns the lane to `BUILDING` from the sealed bundle and redacted findings. Neither loop requires
`run amend`.

Two consequences for authoring. First, write the instruction so it reads correctly a second time,
against a tree that already contains a partial answer. Second, if a sibling lane has merged, a
zero-delta or stale-base candidate is handled by `BASE_INVALIDATION` back to `BUILDING` from the
new integration HEAD — not by salvaging a dirty worktree.

Process death restarts the current incomplete stage from its last immutable artifact. `run resume`
does not adopt a live session.

`run status` is how you read this from outside. It derives complete / waiting / executing /
integration-review-pending / publishable from durable rows. Pane text and scheduler liveness are
not authority.

## Several repositories

There is no multi-repository Plan IR in the proven two-lane slice. Each repository is one run
against one `--repo` publication worktree. Cross-repository workspace leases are not workflow
authority.

## Which skill authors what

| Input | Skill | Emits | Executable |
| --- | --- | --- | --- |
| a mapped codebase, a master spec | `arch-review` | `plan_kind: architecture` | no — anchor only |
| an approved architecture IR plus a change request | `plan-brownfield` / `arch-brownfield` | `plan_kind: brownfield` | yes, after compiler admission |
| a greenfield request | `planf3` | `plan_kind: implementation` | yes, after compiler admission |
| interview notes | `deep-interview` | requirements that feed the IR | no — input only |

The shared contract and the `extensions.maestro` shape are documented in the `plan-contract` skill.
Execution after admission is `MAESTRO_architecture.md`.

---

## Historical — plan CLI, schema versions, and merged tests

The remainder records withdrawn operator surface and the pre-factory pairing rule. It is not the
shipped contract. Do not follow it for new plans or runs.

### Historical projection and `maestro-plan.v*`

Ingress used to project Plan IR onto `maestro-plan.v1`/`v2`/`v4` node graphs with per-node gates
(`runner`, `argv`, `min_cases`). `_load_runnable_plan` refused versions outside
`_RUNNABLE_PLAN_SCHEMA_VERSIONS`. `maestro-plan.v5` `test_visibility` was authored and then excluded
from the runnable set so a hidden node could not half-execute. The factory compiler no longer
admits a plan by schema-version allowlist of those projections; it admits objective DAG/schema
properties only.

`plan_contract_ingress.author_from_plan_contract` verified a receipt against IR bytes and projected
lanes. The node/gate shape is withdrawn; the projection now emits
`maestro-plan.artifact-factory.v1` lanes via `tools/plan_author_cli.py`. `depends_on` maps onto `needs`,
`execution_context` onto gate `cwd`, and `verifiers[]` onto `pytest`/`vitest` gates. That gate
object is not a lane stage.

### Historical `plan gate` / `review` / `ship` and bootstrap keys

The plan CLI's shipped surface was `maestro plan gate|review|ship`. `gate` wrapped `planctl render`,
`validate`, and `mutate`. `review` injected a reviewer HMAC key into `planctl review` and
`--require-approved`. `ship` projected IR into a Maestro plan and ran deterministic `plan finalize`
(rubric `maestro-deterministic.v1`), after an earlier pane-launched reviewer was removed as a §1.2
violation.

`maestro bootstrap` minted `maestro.env` and `reviewer-hmac.env` under the state root so authors
could not sign their own review receipts. `REVIEWER_KEY_PRESENT` refused `gate` if the reviewer key
was in the environment. Combined `maestro.env` files had to be rewritten. That key split is not the
factory operator surface. Factory execution uses `run start|resume|amend|status` against
`runtime_state_root`.

`planctl` calls carried `--repo-root .` because IR lived in `.maestro/` while `source_artifacts`
were repo-relative. Maestro refused `..` escapes.

### Historical verifier commands and `test_strength`

Every lane declared one countable verifier. `Gate.runner` was `Literal["pytest", "vitest"]`. Shell
scripts, Make targets, and health checks were refused as `maestro.command`. Authors were told to
pass real argv (`npx vitest run …`), never `npm test`. Playwright/Cypress could not be gates.

A tests lane's verifier carried `test_strength`: `coverage[]` with `requirement_id`, `aspect`
(positive and negative both required), `case_selector` substring, `min_cases`, plus
`falsifiability.strategy` of `baseline_absent` or `controlled_mutation` matching
`expected_reason_pattern`. `maestro run start` refused `RUN_TEST_STRENGTH_CONTRACT_ABSENT`.
`case_selector` existed so lifecycle would not key on an agent's account of coverage.

Those fields named private selectors and expected literals. They must not appear on a builder
prompt or in public `CODE_REVIEW` findings under the factory contract. Public acceptance criteria
replace them as the builder-visible obligation.

### Historical merged-test pairing

A tests lane used to merge into the integration branch before the build lane branched. The build
lane's commit had to carry the accepted test files byte-identically
(`tests_chain.compare_test_bytes`, `PairingRefusal.BYTES_SUBSTITUTED`). **The build lane could read
those tests, and that was then specified as the design rather than a leak.**

That pairing rule is withdrawn. Sealed private tests never merge into the builder's tree. The
factory equivalent is `SEALED_TEST_BUNDLE` in the vault plus absence proofs against the run
repository and builder worktree. Final integration review runs sealed tests against the integration
commit; publication is receipt-backed exactly once onto `main`.

`test_visibility: "merged" | "hidden"` on `maestro-plan.v5` was the migration sketch for that
withdrawal. Hidden visibility is now the only builder-facing behavior; there is no merged-test
compatibility path in the factory slice.

### Historical review budgets and repair chains

A rejection used to return to a retained builder session and worktree, bounded by
`review_ceiling`, `REPAIR_CHAIN_LIMIT`, and semantic spend ceilings. Sibling merges aborted the
repair chain. `maestro run convergence` reported findings per attempt.

Those budgets prevented explicit user continuation and are deleted. Factory repair is
`REVISE` → `WRITING_TESTS` or `BUILDING` from immutable artifacts. Pause is `USER_WAIT`. Amendment
is `run amend`. Status is derived, not a convergence verb.

### Historical workspace CLI

`maestro workspace author` / `workspace validate` bound several repositories by
`maestro-workspace.v1` after each repo had a PASS finalization receipt. Coordinator workspace
leases are not factory workflow authority. The proven slice is exactly two dependent lanes in one
run.
