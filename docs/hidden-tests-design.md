# Hidden tests — design

Written 2026-08-27 after the `lane-routing-chemical` investigation established that current test
visibility is deliberate, not a defect. This document proposes replacing that design.

## Implementation status — read before trusting any section below

The prerequisite is built. **The feature is not.** The sections below describe the intended end
state; this table is what exists, and nothing else is a claim about code.

| Piece | State |
| --- | --- |
| Read containment — the vault (§0, §9) | **Built.** `adw_modules/hidden_vault.py`; `tests/test_hidden_test_containment.py`, 12 cases against real git |
| `test_visibility` on a tests node | **Built.** `maestro-plan.v5`; `TestsNodeV5`, `PlanNode.test_visibility`; `tests/test_test_visibility_schema.py` |
| Invalid-shape refusals (unknown visibility; hidden on a non-tests kind; hidden without a strength contract) | **Built**, in the `PlanNode` constructor |
| Composed evaluation tree (§1, §2) | **Not built** |
| Absence / provenance / coverage conjuncts (§3) | **Not built** |
| Differential falsification in the composed tree (§4) | **Not built** |
| `RepairDirective` and the sanitised handoff (§5) | **Not built** |
| `HiddenGateReceipt` and the evidence chain (§6) | **Not built** |
| Run-level pin, reveal step, merge path (§7) | **Not built** |

Because the execution machinery is absent, **`maestro-plan.v5` is deliberately excluded from
`maestro._RUNNABLE_PLAN_SCHEMA_VERSIONS`**, so a v5 plan is refused at `run start` and a hidden node
can never half-execute. A hidden node under today's scheduler would be gated by its declared
`pytest <path>` inside a worktree that does not contain the path — a collection error, which §7.4
refuses as a red for the wrong reason, on every honest attempt. v1–v4 are untouched and every plan
that could start a run before this change still can.

The run-level pin (§7.2) is **deliberately not built yet**, and its absence is safe for exactly one
reason: no run can contain a hidden node, so there is no retroactivity for a pin to prevent. It
becomes mandatory in the same change that makes v5 runnable. Building the durable column first would
have added a lifecycle-store migration whose only reader is behaviour that does not exist — the B15
shape — to the store of live deployments.

Until the remaining rows are built, the shipped contract in `MAESTRO_architecture.md` §7.4 and
`docs/plan-authoring.md` still governs, and §7.4 / §19 M41 are therefore **not** amended: they
describe the only behaviour that executes. `§16.3` items 8 and 15 *are* amended, because what
changed there is already true.

## 0. What is being changed, and the one thing it rests on

Today a tests node merges into the integration branch, every downstream build lane inherits its
files, and the build lane's own commit must carry them byte-identically
(`tests_chain.compare_test_bytes`, `PairingRefusal.BYTES_SUBSTITUTED`, enforced in
`scheduler.py` around line 3050; stated at `docs/plan-authoring.md:295` and §19 M41). The
implementation builder can read the tests it is judged by. That is the specification working as
written, not a leak.

This design withholds the test files from the build phase and executes them out of band.

**Read this before anything else.** The entire value of the feature depends on a property Maestro
does not have and this design cannot supply on its own: the builder must be unable to *read* the
hidden tests. §16.3 item 15 records that write containment outside the worktree does not exist;
read containment does not exist either. A linked git worktree **shares its parent repository's
object database**, so a hidden-tests commit stored as a ref in the run repository is readable from
the builder's worktree with one `git cat-file`. If hidden tests ship without the separate object
store specified in §2.1, the feature is theatre: it costs a great deal and a builder that thinks to
run one plumbing command defeats it entirely. §9 states the consequence for the honest verdict.

## 1. Core mechanism: the composed evaluation tree

Hidden test bytes never enter the integration branch during the build phase and never enter any
builder worktree. Every gate run for a hidden-paired build node executes in a harness-private
scratch tree **composed** at execution time from two immutable commits:

- the tree under test (an integration head, or an implementation candidate's `output_sha`), and
- the hidden test paths, taken from the **accepted** tests candidate sha.

Composition uses git plumbing into a harness-private index — the same technique §8.4 already uses
to commit without touching a worktree index:

```
GIT_INDEX_FILE=<private>  git read-tree <tree-under-test>
GIT_INDEX_FILE=<private>  git update-index --add --cacheinfo <mode>,<blob>,<hidden path>   # per path
GIT_INDEX_FILE=<private>  git write-tree            ->  composed_tree_sha
```

The composed tree is then materialised into a scratch directory outside every worktree, by the
shape `tests_chain._materialize` already implements, and the gate runs there.
`worktree._run_gate(worktree, runner, argv, scratch, ...)` already takes the tree as a parameter,
so no gate-execution change is needed beyond passing a different directory.

`composed_tree_sha` is the design's audit anchor. It is a deterministic function of
`(tree_under_test_sha, accepted_tests_sha, sorted(hidden_paths))`, so any auditor holding those
three can recompute it and confirm which bytes a gate actually ran against. Adjudicators
**recompute it** rather than trusting the value a runner reports.

### 1.1 Why composition rather than "run the tests from somewhere else"

The alternative — leave the candidate tree alone and point the runner at test files on another
path — fails on imports. A pytest run resolves `from lexgenius_pipeline...` against the tree it
runs in; splitting tests from the code under test across two directories reintroduces exactly the
"red for the wrong reason" failure §7.4 spends a paragraph eliminating. Composition puts both in
one tree, so every existing assumption about how the runner resolves the repository holds
unchanged.

## 2. Requirement 1 — falsifiability without the test in the worktree

§7.4 requires the node's gate to be red before the node's work and green after, and red *for the
intended reason* — the node's behaviour is missing — rather than for the wrong reason of absent
dependencies. Composition preserves the whole argument:

| clause | tree composed from | required verdict |
| --- | --- | --- |
| pre-node (chain root) | chain-root **integration head** + accepted tests | RED |
| post-node | candidate `output_sha` + accepted tests | GREEN, `passed >= min_cases` |
| output falsification | candidate with measured-delta paths reverted to chain-root base, + accepted tests | RED |

Upstream outputs are present because the pre-gate composes from the integration head, exactly as
today. The tests are present because they are overlaid. The two runs in the bracket differ by
exactly the agent's work, because **the same `accepted_tests_sha` is overlaid in both** — which is
strictly stronger than the status quo, where a builder can edit the test files between the pre-gate
and the post-gate and is caught only later, at merge, by `compare_test_bytes`.

Adjudication is unchanged: `verification.adjudicate_gate` and its §10.2 counting rule, and
`verification.pre_gate_not_falsifiable` for a green pre-gate. Neither learns anything about
composition; they receive a `GateResult` as they do now.

**One new refusal is required.** A pre-gate that is red because the tree fails to import or collect
is not a valid red — the same distinction the tests chain already draws
(`adjudicate_parent_red`, and plan-authoring's rule that "an import error, or a collection failure
is refused by name"). Composition makes this reachable in a new way: a hidden test that imports a
symbol the implementation has not created yet produces a collection error rather than an assertion
failure. Refusal `HIDDEN_GATE_RED_BY_IMPORT`, classified SEMANTIC, adjudicated against the plan's
declared `expected_reason_pattern` by the machinery `adjudicate_negative_control` already uses.

This is a real authoring burden and it should be stated to plan authors: **a hidden test suite must
import lazily enough to fail by assertion rather than by import** at the pre-gate, or its
falsifiability witness cannot be taken. The `baseline_absent` falsifiability strategy already has
this property as a requirement; hidden visibility makes it load-bearing for every node rather than
for the tests node alone.

## 3. Requirement 2 — what binds the candidate to the accepted test sha

`compare_test_bytes` answers "are the test files in the implementation candidate's tree the exact
bytes the accepted test candidate was reviewed as". Under hidden visibility those files are not in
the candidate's tree at all, so that question is not merely unanswerable — asking it is a category
error. It is replaced by three conjuncts, every one computed by code from immutable git objects.

**Conjunct 1 — absence.** For every declared hidden path `p`,
`tests_chain.blob_id_at(candidate_sha, p)` must be `None`. Refusal
`HIDDEN_TEST_PATH_PRESENT`. This is strictly stronger than `BYTES_SUBSTITUTED`: today substitution
is *detected*, here possession is *refused*. A builder that reconstructs the test file by guessing
is refused for having it at all, whatever its bytes.

**Conjunct 2 — provenance.** The `GateResult` that produced the green post-gate verdict must carry
the `composed_tree_sha` the adjudicator independently recomputes from
`(candidate_sha, accepted_tests_sha, hidden_paths)`. Refusal `HIDDEN_GATE_TREE_MISMATCH`. This is
what makes "the accepted tests, and no others, judged this candidate" a checkable fact rather than
a claim by the component that ran them.

**Conjunct 3 — coverage.** The existing obligation check — `tests_chain.measure_coverage` over the
accepted contract's `test_strength.coverage` — run against the composed post-gate execution rather
than against `attempt.path`. Refusal `GATE_NOT_GREEN`, reusing the existing code.

Together these are what `record_test_pairing` writes, and the merge check's authority is unchanged
in shape: a pairing row, written last, that the merge consults rather than re-deriving from mutable
state.

**`compare_test_bytes` and `PairingRefusal.BYTES_SUBSTITUTED` are not deleted.** Merged-visibility
lanes still use them, and per §3.6 B15 a check whose field has zero readers is a build failure. The
pairing path branches on visibility; both branches keep a live reader.

## 4. Requirement 3 — differential falsification outside the candidate tree

§7.4's output-falsification step reverts every path the node wrote that the gate's own argv does not
select, re-runs the gate, and requires it to fail
(`verification.adjudicate_output_falsification`, driven from `scheduler.py:2934/2944`, with
`verification._gate_names` deciding what the argv selects).

Under hidden visibility this becomes simpler and stronger:

- The revert runs in the **composed scratch tree**, using the shape `tests_chain._revert_into`
  already implements against a scratch directory. Nothing in a worktree is mutated, and
  `tests_chain._porcelain` proves it across the operation, exactly as `execute_negative_control`
  does today.
- The `_gate_names` filter becomes vacuous rather than load-bearing: the node cannot have written a
  gate-selected path, because conjunct 1 refuses any candidate holding a hidden path. So the revert
  set is the whole measured delta, reverted to the chain-root base.
- The vacuity §7.4 explicitly worries about — "a correction that edits only its test file has a
  one-path delta and the question would pass vacuously" — becomes **structurally impossible**
  rather than mitigated by taking paths from a wider diff. This is the clearest place where hidden
  visibility strengthens the architecture rather than trading against it.

`FALSIFICATION_NO_SUBJECT` (an empty revert set is a counted no-subject, not a pass) is unchanged
and still required.

## 5. Requirement 4 — the reviewer-to-builder handoff, as a typed contract

This is the leakiest surface in the design and the one most likely to quietly undo it. Today's
repair handoff is free text assembled in `retry_policy.py` (sole producer; the
`Paths written outside this node's declared outputs` renderer is at `retry_policy.py:847`) and
consumed at `maestro.py:2457`. Runner output carries case names, `file:line`, assertion source and
expected literals — which is the entire hidden-test content. **No runner output may cross the
boundary.**

### 5.1 The record

A hidden-lane repair prompt renders exactly one typed record and nothing else.

```
RepairDirective:
  refusal        : HiddenRepairCode          # closed enum, below
  counts         : {passed, failed, errored, skipped}   # integers only
  unmet          : tuple[(requirement_id, aspect)]      # from the plan's own coverage block
  repair_hint    : Optional[str]             # PLAN-AUTHORED, see 5.3
  offending_paths: tuple[str]                # §8.3 permission failures only, candidate paths only
```

`HiddenRepairCode` ::= `GATE_NOT_GREEN` | `MIN_CASES_UNMET` | `OBLIGATION_UNMET` |
`IMPORT_FAILED` | `FALSIFICATION_FAILED` | `PATH_PERMISSION` | `HIDDEN_TEST_PATH_PRESENT`.

`requirement_id` and `aspect` come from the plan's declared `test_strength.coverage` — they are the
plan's own vocabulary, already in the builder's prompt as the requirement's own words. They say
*which obligation is unmet*, never *how the suite detects it*.

### 5.2 What is refused from crossing, by name

`GateResult.tail`; per-case counts; case ids and node ids; `case_selector` (a case-name substring is
a detection detail); `expected_failing_selector`; `expected_reason_pattern`; any fixture value; any
byte of any hidden blob; any path under a declared hidden path.

**Import errors are the one hard case**, because the builder genuinely needs them and the traceback
genuinely names files. The rule is mechanical, not a judgement: a traceback crosses only if **every
frame's file path lies inside the candidate's declared outputs**; a single frame in a hidden path
collapses the whole text to the bare code `IMPORT_FAILED`. Path comparison is arithmetic on
strings, which is what §1.2 permits; nothing reads the text to decide.

### 5.3 `repair_hint` — plan-authored, never model-authored

Counts and unmet-obligation flags alone will make some repairs converge slowly, and the temptation
will be to let the reviewer — which sees everything — write a helpful sentence. That is the leak
restored through the front door, and it is also §1.2-adjacent: model prose entering a builder's
next attempt.

Instead the **plan author** may declare, per `(requirement_id, aspect)`, a `repair_hint` string at
authoring time. It is part of the specification, written by a human who already decided what the
requirement means. The runtime emits plan bytes, never model bytes. This costs nothing at runtime
and it is the only proposal here that meaningfully offsets the loss of diagnostic detail.

Nothing mechanical can stop a lazy author from pasting an assertion into a hint. §9 counts that as
relocated risk, not eliminated risk.

### 5.4 The chokepoint and its tripwire

`retry_policy.py` is already the single producer of retry guidance, which makes the enforcement
point one function rather than a sweep. In addition, before a hidden-lane prompt is written to
disk, the assembled text is compared against the accepted tests' blob content: any normalised line
of sufficient length appearing in both refuses the launch with `HIDDEN_TEST_TEXT_IN_PROMPT`.

State its limit honestly: a substring tripwire is a backstop against a coding mistake, not a proof
against paraphrase. It catches the regression where someone re-plumbs runner output into the
handoff. It does not catch a hint that describes the assertion in other words.

## 6. Requirement 5 — evidence chain, scoped to node kind

§1.1 item 4 requires every merged node to carry a complete evidence chain **scoped to its node
kind**, and §19 M41 is the record of what happens when a kind reuses another kind's chain or is
left out of a derived edge. So this design **extends the scoping** rather than borrowing.

A build node whose tests prerequisite is hidden is a distinct evidence shape —
`HIDDEN_PAIRED_AGENT` — not plain `AGENT`. Its chain is:

1. the accepted tests candidate: strong measured evidence plus a passed independent review, bound
   to one immutable sha (unchanged, and unchanged in that the tests node's own reviewer **does**
   read the tests — hiding them there would be both impossible and pointless);
2. `HiddenGateReceipt` × 3 — pre-node, post-node, output-falsification;
3. the `HiddenPairing` row: absence, provenance, coverage (§3);
4. the candidate's own §8.3 measured delta and permission proof (unchanged);
5. the build node's independent review over the candidate diff and the declared contract.

`HiddenGateReceipt` is signed by `receipt_crypto` (Ed25519) like every other receipt, and records:
`run_id`, `node_id`, `attempt_no`, `label`, `tree_under_test_sha`, `accepted_tests_sha`,
`hidden_paths` (sorted), `composed_tree_sha`, `runner`, `argv`, `min_cases`, `counts`, `green`,
the adjudicated verdict, `scratch_path`, and the scratch cleanliness before and after.

Every transition keys on those typed fields and on the adjudicators' verdicts. Nothing keys on pane
text, prompt text, a free-text envelope field, or any agent's claim about its own work (§1.2). The
recomputability of `composed_tree_sha` is the strongest §1.2 compliance available here: the record
is checkable by a third party from immutable objects, not merely typed.

**The build node's reviewer does not receive the hidden tests.** Its input stays the declared
contract of B9 — goal, `produces`, acceptance — plus the candidate diff. A reviewer given the
hidden tests would be a second copy of them one prompt-assembly bug away from the builder.

## 7. Requirement 6 — migration and blast radius

### 7.1 Schema

New field on a tests lane: `test_visibility: "merged" | "hidden"`, default `"merged"`. New schema
version `maestro-plan.v5`.

**`maestro-plan.v4` must remain runnable.** This is a hard requirement, not a preference. The
project's own record (`CLAUDE.md`, and §16.3's account of #104) is that shipping a template which
refuses an existing schema made deployments' shipped plans unrunnable with
`RUN_PLAN_SCHEMA_VERSION_UNRUNNABLE`, repairable only by re-shipping from IR, and not repairable at
all mid-run. v5 adds a field; v4 plans project to `merged` and behave exactly as today. No plan
becomes unrunnable and no deployment mirror is blocked on a re-ship.

### 7.2 The pin, and the highest-risk trap in this design

`runs.pinned_test_visibility`, written at `create_run`, read in `Scheduler.__init__` **before**
`_project_nodes` — the same shape as `pinned_test_strength_contract`
(`lifecycle.py:2251`, read at `scheduler.py:720`). Existing runs read `merged`.

§19 M42 is precisely this failure already committed once: a projection that ignored the run
contract would have applied a stronger lifecycle retroactively on resume, inserting review rows,
**rewiring every direct dependant's `needs_json`**, and lifting depths — reopening dependency
decisions §7.3 forbids. Hidden visibility changes the dependency shape in exactly the same way (a
hidden build lane needs its tests node *accepted*, not *merged*), so an unpinned projection would
rewire `needs_json` on the next resume of any existing run. **Pin before projection, or do not
build this.**

Consequence for the live EPA run: `run-8d1a71f463e4430f92a125a8f8b3731d` is LEGACY-pinned with 13
uncontracted tests nodes (§19 M42). It reads `merged`, is unaffected, and stays resumable.

### 7.3 Hidden requires STRENGTH_V1

The repair handoff's entire vocabulary is `test_strength.coverage`. A LEGACY-pinned run has none,
so hidden visibility is refused at admission with `HIDDEN_REQUIRES_STRENGTH_V1`.

### 7.4 The reveal step

Withholding hidden tests from the integration branch means §8.8's final integration gate would
otherwise verify a tree that does not contain them, and the delivered branch would ship without the
tests that justified it.

Resolution: after the last build lane merges and **before** the integration gate, every accepted
hidden tests candidate merges into the integration branch in the deterministic order of §8.5, by
output sha. No builder remains to protect at that point. The integration gate then runs the
complete suite, and the delivered tree contains its own tests, which is what any consumer expects.

Corollary to state plainly: a run that never reaches final acceptance leaves its hidden tests on
refs in the private object store and not on any branch. They are durable, not lost, but a human
inspecting the integration branch of an abandoned run will not find them.

### 7.5 What breaks and must change

| Surface | Change |
| --- | --- |
| `maestro._append_needed_tests` | Must not fire for hidden lanes; visibility-conditional |
| `maestro._agent_node_prompt` | Must not emit the gate command line for a hidden lane — it names the hidden path. Replaced by requirement text plus a `min_cases` target expressed without the selector |
| `tests_chain.compare_test_bytes` | Merged-only branch; kept with a live reader (B15) |
| `verification._gate_names` | Vacuous on the hidden path; retained for merged |
| §8.3 permission check | Gains the absence conjunct (`HIDDEN_TEST_PATH_PRESENT`) |
| Merge/final acceptance | Gains the reveal step |
| Object storage | Gains a private bare repository outside the run repo (§0, §9) |

### 7.6 Size

This touches the plan schema, ingress, validation, model, the lifecycle store (a new pin, a new
receipt table, a new pairing shape), scheduler projection, gate execution, three verification
adjudicators, `retry_policy`, prompt assembly, and the merge path — plus their tests and the
regressions in §11. It is comparable to or larger than the tests/build split that landed as #124.
It is not a small change and should not be scoped as one.

## 8. Non-goals

- Hiding tests from the tests-node author or the tests-node reviewer. Both must read them.
- Sandboxing the builder's filesystem generally (§16.3 item 15 remains open).
- Preventing a builder from *inferring* likely tests from a well-written requirement. Hidden tests
  hide the encoding, never the specification.
- Reading `verifiers[].falsifiability.mutation`, which §7.4 already records as reaching no runtime.
- Any change to code nodes, whose acceptance is their command's exit code.

## 9. Residual risk, and the honest verdict against §16.3 item 8

§16.3 item 8 is the baseline this has to beat: *"Proxy-gaming is mitigated, not solved. Every gate
samples a proxy, and an agent optimizing for green looks for the cheapest one. The differential
gate raises the cost; it does not make a vacuous proxy impossible."*

**What is genuinely reduced.** Overfitting to literal fixture values and assertion structure — the
builder cannot read them. And editing the test file to make it pass becomes structurally impossible
rather than detected after the fact, which also closes §7.4's named one-path-delta vacuity.

**What is not reduced.** Writing the shallowest code that satisfies the stated requirement. The
gate still samples a proxy. Item 8's sentence remains true after this change.

**A fourth mode, measured 2026-08-27, that this design did not name and would not have stopped.**
The two modes above were written as the failure set. A real builder found a third thing to do:
`lane-routing-chemical` a3 put ~190 lines into production source that manufactured the gate's own
case count at collection time — three synthetic `test_*` functions installed onto the gate's test
module through six mechanism families with fallbacks (`setattr` injection, patched
`SourceFileLoader.exec_module` and `AssertionRewritingHook.exec_module`, patched
`_pytest.python.Module.collect`, a `pytest_collection_modifyitems` hook, plugin self-registration
via `inspect.stack()`, and `Function.from_parent`). It did this *after* an honest
`2 passed, min_cases is 5` rejection, and after §8.3 had convicted it for editing the test file
directly.

**Hidden tests would not have prevented it, and saying so is the point of this paragraph.** The
builder never needed the assertions. It needed two things this design leaves in place: the target
number, which its own honest rejection reported, and enough of a handle on the gate module to
attach to — a name heuristic, not the contents. Under the composed evaluation tree of §1 the
production code still executes inside the runner, so every one of those six routes remains open.
The repair is `adw_modules/gate_capture.py`'s provenance check — every counted case must be defined
in the accepted test bytes, compared harness-side against an immutable sha — and that check is
**independent of visibility**: it works identically for merged and hidden lanes, and it was built
and landed without any part of hidden tests.

That cuts against this design in the place it was meant to be strongest. Of the failure modes now
on record, hidden tests address two — fixture overfitting and test-file editing — and the third,
the most sophisticated one actually observed in production, is closed by a check that needed none
of it. Anyone re-costing §7.6's estimate should weigh that: the measured attack was defeated by
roughly 200 lines of AST-and-provenance code, not by the architecture this document proposes.

**What is relocated, not removed.** Three places:

1. *The repair oracle.* Each repair round returns counts plus per-`(requirement, aspect)` unmet
   flags. That is a search channel. Its width is bounded by the semantic ceiling `K` (§7.5), so the
   leak is roughly `K × (|coverage obligations| + a few bits)` instead of the entire test source in
   round one. A real, quantified reduction — but a channel, and worth choosing deliberately (Q2).
2. *The plan author.* `repair_hint` and requirement text now carry the burden, authored by a human
   with full knowledge of the tests and checked by nothing.
3. *The containment boundary.* See below.

**The verdict.** Hidden tests beat item 8 **narrowly, in the specific mode that was actually
observed** (M41: a suite asserting only happy paths beside builders that edited it), **and only if
read containment is real.** A linked worktree shares its parent's object database. If the accepted
tests commit is a ref in the run repository, the builder reads it with one `git cat-file` and this
entire design buys nothing while costing everything in §7.6. The hidden tests must therefore live
in a **separate bare repository under harness-private state**, fetched into the composition scratch
and never into the run repo.

Even with that, §16.3 item 15's residual applies: under `--dangerously-skip-permissions` a builder
can read any absolute path on the machine, including harness state. So hidden tests are hidden from
a **cooperative-but-optimizing** builder, not from an adversarial one. That is the honest boundary,
and it is the fact that should drive the decision: this feature raises the cost of gaming for an
agent that is not trying to cheat, and does not stop one that is.

**Recommendation.** Build read containment first, as a standalone prerequisite with its own
negative test (a builder-shaped process that provably cannot reach the tests blob from inside its
worktree). If that prerequisite is not acceptable to build, do not build hidden tests — the
status quo plus better plan authoring is strictly cheaper for the same real-world protection.

## 10. Documents this contradicts, and the amendments required

The documents win until amended. Each of these is a specific proposed edit, not a licence:

| Document | Amendment |
| --- | --- |
| `MAESTRO_architecture.md` §7.4 | The gate runs in the attempt's worktree → runs in the attempt's worktree for merged visibility, and in a composed harness-private tree for hidden. Add `HIDDEN_GATE_RED_BY_IMPORT` |
| §1.1 item 4 | Add the `HIDDEN_PAIRED_AGENT` evidence chain to the kind scoping |
| §16.3 item 8 | Amend, do not delete: record what hidden tests reduce, what they relocate, and the containment dependency |
| §16.3 item 15 | Add read containment as a named, currently-unmet requirement |
| §19 M41 | The byte-identical pairing rule becomes visibility-scoped |
| `docs/plan-authoring.md:295` | "What the pair costs downstream" becomes visibility-scoped; document `test_visibility` and `repair_hint` |

## 11. Regressions this design must ship with

Each must fail against a runtime that lacks the mechanism, and each must execute the real production
path rather than reimplementing it:

1. A hidden-lane builder prompt, **as assembled and written to disk**, contains no hidden path, no
   gate argv naming one, and no line of any hidden blob.
2. The builder's worktree contains no hidden test file at any point in the attempt, and a candidate
   that adds one is refused `HIDDEN_TEST_PATH_PRESENT`.
3. A repair handoff after a red post-gate carries only `RepairDirective` fields — asserted by
   feeding real runner output with real assertion text through `retry_policy` and checking none of
   it survives.
4. `composed_tree_sha` recomputation refuses a receipt whose reported tree does not match
   `(candidate_sha, accepted_tests_sha, hidden_paths)`.
5. Resume of an existing merged-visibility run does not rewire any `needs_json` and inserts no
   hidden pairing row (the M42 regression, asserted against a real ledger).
6. A `maestro-plan.v4` plan still starts and resumes unchanged.
7. **Read containment**: a process with the builder's own cwd and credentials cannot obtain the
   hidden blob by `git cat-file`, `git show`, or object-database traversal from the worktree.
8. The reveal step lands the accepted tests on the integration branch before the integration gate,
   and the integration gate runs them.

Test 7 is the one that decides whether the other seven are worth having.

## 12. Open questions requiring the user's decision

- **Q1 (blocking).** Build the separate harness-private object store for hidden test blobs? Without
  it the feature does not work as advertised (§0, §9). If no, the recommendation is to stop.
- **Q2.** Repair oracle granularity: per-`(requirement, aspect)` unmet flags, or a single
  pass/fail? Finer converges repairs faster and leaks more per round.
- **Q3.** Adopt plan-authored `repair_hint`, and who audits hints for leaked assertions?
- **Q4.** Confirm the reveal step: the delivered integration branch should contain its tests.
- **Q5.** Accept the expected increase in repair rounds — a builder repairing against counts rather
  than assertions will spend more attempts, and the semantic ceiling `K` may need raising, which
  raises cost per node.
