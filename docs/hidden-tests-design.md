# Hidden tests — design

Written 2026-08-27 after the `lane-routing-chemical` investigation. Updated 2026-08-29 to match the
shipped nine-stage artifact factory in `MAESTRO_architecture.md`. Sections marked **Historical**
preserve the pre-factory design; they are not current-facing claims.

## Shipped contract

Sealed private tests are never visible to builders. That is implemented behavior, not a proposal.

| Piece | State |
| --- | --- |
| Vault object-database isolation (`adw_modules/hidden_vault.py`) | **Shipped.** Private blobs live in a bare repository under `runtime_state_root`, not in the run repository |
| Private test-author / test-reviewer loop | **Shipped.** `TEST_DRAFT` → `TEST_REVIEW` (`PASS`/`REVISE`) → `SEALED_TEST_BUNDLE` |
| Builder exclusion | **Shipped.** Builder input is public contract, architecture constraints, allowed paths, prior redacted review, and sealed digest. No private source, fixtures, selectors, expected literals, or vault paths |
| Code review against sealed tests | **Shipped.** Reviewer runs private tests in harness scratch; public `CODE_REVIEW` is verdict, result summary, and redacted four-key findings |
| Absence from run repo / builder worktree / fetch | **Shipped.** Isolation proofs are part of seal |
| Nine-stage lane authority | **Shipped.** `PLANNED` … `WAITING_FOR_USER`; git commits and sealed digests identify artifacts, not stage |
| Operator verbs | **Shipped.** `run start` / `run resume` / `run amend` / `run status` |
| External runtime state | **Shipped.** Ledger, vault, locks, receipts, and ephemeral worktrees only under `runtime_state_root` |

The factory does **not** merge tests into the integration branch for the builder to inherit. It does
**not** require the builder's commit to carry test bytes. It does **not** use `test_visibility` on a
v5 plan node, composed-tree gate receipts, `RepairDirective` retry prose, or review-budget repair
chains.

Authoritative transitions:

- `WRITING_TESTS` emits `TEST_DRAFT` (private draft digest/reference plus public behavioral
  contract).
- `REVIEWING_TESTS` `PASS` → `TESTS_SEALED`; `REVISE` returns to the test author with actionable
  findings (no private literals).
- `TESTS_SEALED` emits `SEALED_TEST_BUNDLE` (vault digest/reference; private bytes absent from run
  repo and builder input) → `BUILDING`.
- `REVIEWING_CODE` runs sealed tests against the candidate. `PASS` → `READY_TO_MERGE`. `REVISE` →
  `BUILDING` with redacted findings.
- Final integration review, after every lane is `MERGED`, evaluates the integration commit with all
  sealed tests. Publication is exactly-once and receipt-backed.

Test author and test reviewer may read the private draft. The implementation builder may not. The
code reviewer has controlled vault access for the run and must not put private bytes in the public
artifact.

## What authors must still do

- Declare public acceptance criteria and exact file-path outputs. The compiler checks those
  objectively; it does not read test source.
- Keep private test paths out of declared outputs so the builder has no permission — and no
  need — to write them.
- Write `REVISE`-survivable instructions: the builder's next `BUILDING` input is the sealed digest
  plus redacted findings, not the suite.
- Do not put selectors, fixtures, or expected literals in requirement text that will be copied into
  a builder prompt.

Residual risk that remains true after shipping: a cooperative-but-optimizing builder can still
satisfy the stated public requirement as cheaply as possible, and a builder with unconstrained
filesystem access can read harness state. Hidden tests raise the cost of overfitting to assertion
structure; they do not sandbox the machine. Gate-module capture (synthetic `test_*` functions that
inflate case counts) is a separate provenance problem, independent of visibility.

## 0. What changed relative to the 2026-08-27 draft

**Historical specification (withdrawn as current-facing):** a tests node merged into the integration
branch, every downstream build lane inherited its files, and the build lane's own commit had to
carry them byte-identically (`tests_chain.compare_test_bytes`, `PairingRefusal.BYTES_SUBSTITUTED`).
The implementation builder could read the tests it was judged by. Plan-authoring then stated that
as the design rather than a leak.

**Shipped replacement:** withhold test files from the build phase; execute them from the vault
against an immutable candidate SHA; record only digest/reference and public contract on
`SEALED_TEST_BUNDLE`.

Read containment is the load-bearing property. A linked git worktree **shares its parent
repository's object database**, so a hidden-tests commit stored as a ref in the run repository is
readable with one `git cat-file`. The vault is a separate bare repository under harness-private
runtime state. Without that split the rest of this document is theatre.

---

## Historical — composed evaluation tree and pre-factory gate pairing

The sections below are the 2026-08-27 design that assumed merged-visibility lanes, per-node pytest
gates, `retry_policy` repair prose, and `maestro-plan.v5`. They explain why the vault exists and
what was rejected. They are not instructions for the current runtime.

### Historical status table (as of 2026-08-27)

At the time of writing, vault read containment and a v5 `test_visibility` field existed; composed
evaluation, absence/provenance/coverage conjuncts, `RepairDirective`, `HiddenGateReceipt`, and the
run-level pin/reveal/merge path did not. `maestro-plan.v5` was excluded from
`_RUNNABLE_PLAN_SCHEMA_VERSIONS` so a hidden node could not half-execute under the old scheduler.
That allowlist and the dual merged/hidden plan field are not factory admission.

Until the factory cutover, `MAESTRO_architecture.md` §7.4 (historical gate-in-worktree text) and
the then-current `docs/plan-authoring.md` pairing rule still described executing behavior. They no
longer do.

### Historical §1 — composed evaluation tree

Hidden test bytes were not to enter the integration branch during build, nor any builder worktree.
Every gate run for a hidden-paired build node would execute in a harness-private scratch tree
**composed** at execution time from two immutable commits:

- the tree under test (an integration head, or an implementation candidate's `output_sha`), and
- the hidden test paths, taken from the **accepted** tests candidate sha.

Composition used git plumbing into a harness-private index:

```
GIT_INDEX_FILE=<private>  git read-tree <tree-under-test>
GIT_INDEX_FILE=<private>  git update-index --add --cacheinfo <mode>,<blob>,<hidden path>
GIT_INDEX_FILE=<private>  git write-tree            ->  composed_tree_sha
```

`composed_tree_sha` was the audit anchor, a deterministic function of
`(tree_under_test_sha, accepted_tests_sha, sorted(hidden_paths))`. The factory code-review scratch
tree is the descendant of this idea: materialize the candidate, copy vault blobs, run pytest,
discard the tree. Adjudicators of `GateResult` / `verification.adjudicate_gate` are not factory
stage authority.

Why composition rather than "run the tests from somewhere else": pytest import resolution. Splitting
tests from code across two directories reintroduced collection errors the old §7.4 treated as the
wrong kind of red.

### Historical §2 — falsifiability without the test in the worktree

Old §7.4 required the node's gate red before the work and green after, for the intended reason.

| clause | tree composed from | required verdict |
| --- | --- | --- |
| pre-node (chain root) | chain-root **integration head** + accepted tests | RED |
| post-node | candidate `output_sha` + accepted tests | GREEN, `passed >= min_cases` |
| output falsification | candidate with measured-delta paths reverted to chain-root base, + accepted tests | RED |

A new refusal `HIDDEN_GATE_RED_BY_IMPORT` was proposed for collection/import failures at pre-gate.
Authoring burden: a hidden suite must fail by assertion rather than by import at the pre-gate.

Factory code review still requires executed private tests and refuses `PASS` on fail/error (demoted
to observable `REVISE` with redacted findings). It does not keep the three-gate bracket or
`min_cases` as stage authority.

### Historical §3 — binding the candidate to the accepted test sha

`compare_test_bytes` asked whether the implementation candidate's tree held the accepted test bytes.
Under hidden visibility that question is a category error. It was to be replaced by:

1. **Absence.** `blob_id_at(candidate_sha, p)` is `None` for every hidden path. Refusal
   `HIDDEN_TEST_PATH_PRESENT`.
2. **Provenance.** Recompute `composed_tree_sha`. Refusal `HIDDEN_GATE_TREE_MISMATCH`.
3. **Coverage.** Existing `measure_coverage` against the composed post-gate. Refusal
   `GATE_NOT_GREEN`.

`compare_test_bytes` / `BYTES_SUBSTITUTED` were to remain for merged-visibility lanes. The factory
slice has no merged-visibility compatibility path. Seal plus object-absence proofs replace the
pairing row.

### Historical §4 — differential falsification outside the candidate tree

Output-falsification reverted paths the node wrote that the gate argv did not select
(`verification.adjudicate_output_falsification`). Hidden visibility made `_gate_names` vacuous
because conjunct 1 refused any candidate holding a hidden path, closing the one-path-delta vacuity
where a correction that edited only its test file passed the question. `FALSIFICATION_NO_SUBJECT`
stayed. Factory `CODE_REVIEW` does not use that revert bracket.

### Historical §5 — reviewer-to-builder handoff

The leakiest surface. Repair handoff was free text from `retry_policy.py` carrying case names,
`file:line`, assertion source, and expected literals. **No runner output may cross the boundary**
remains a factory rule; the mechanism is redacted `CODE_REVIEW` findings, not `RepairDirective`.

Proposed record:

```
RepairDirective:
  refusal        : HiddenRepairCode
  counts         : {passed, failed, errored, skipped}
  unmet          : tuple[(requirement_id, aspect)]
  repair_hint    : Optional[str]             # PLAN-AUTHORED
  offending_paths: tuple[str]
```

`HiddenRepairCode` ::= `GATE_NOT_GREEN` | `MIN_CASES_UNMET` | `OBLIGATION_UNMET` |
`IMPORT_FAILED` | `FALSIFICATION_FAILED` | `PATH_PERMISSION` | `HIDDEN_TEST_PATH_PRESENT`.

Factory `REVISE` findings are instead: violated requirement, observed behavior, required behavior,
implementation area — never private source, fixtures, selectors, expected literals, or vault paths.

`repair_hint` as plan-authored prose was proposed so the runtime would emit plan bytes, never model
bytes. Substring tripwire `HIDDEN_TEST_TEXT_IN_PROMPT` was a backstop, not a proof against
paraphrase. Retry-policy spend ceilings are deleted factory mechanisms.

### Historical §6 — evidence chain scoped to node kind

A build node whose tests prerequisite was hidden was a distinct evidence shape
`HIDDEN_PAIRED_AGENT`: accepted tests candidate + three `HiddenGateReceipt`s + `HiddenPairing` +
permission proof + independent review over diff and declared contract. Receipts were Ed25519-signed
like other receipts. The build node's reviewer was not to receive hidden tests.

Factory evidence is the lane artifact sequence (`LANE_PLAN`, `TEST_DRAFT`, `TEST_REVIEW`,
`SEALED_TEST_BUNDLE`, `BUILDER_OUTPUT`, `CODE_REVIEW`, `INTEGRATION_MERGE`) plus publication
receipt. No second node kind.

### Historical §7 — migration, pin, reveal

- Schema field `test_visibility: "merged" | "hidden"`, default `"merged"`, schema
  `maestro-plan.v5`. v4 was required to remain runnable (record of #104 /
  `RUN_PLAN_SCHEMA_VERSION_UNRUNNABLE`).
- `runs.pinned_test_visibility` at `create_run`, read before projection, same trap as
  `pinned_test_strength_contract` (§19 M42). Unpinned projection would rewire `needs_json` on
  resume.
- Hidden required `STRENGTH_V1` (`HIDDEN_REQUIRES_STRENGTH_V1`).
- **Reveal step:** after the last build merge and before the old integration gate, merge accepted
  hidden tests onto the integration branch so the delivered tree contained its tests. A run that
  never reached final acceptance left tests only in the private object store.

Factory final review runs sealed tests against the integration commit without merging private
blobs into the builder-visible history as a builder prerequisite. Publication is
`MAIN_PUBLICATION` plus immutable `refs/maestro/publications/<run-id>/<review-input-fingerprint>`.

Surfaces that were to change (`_append_needed_tests`, `_agent_node_prompt` gate argv, merge path,
lifecycle pin column) are listed here as the blast radius of the old design, not as remaining work
on the factory slice.

### Historical §8 — non-goals (still valid)

- Hiding tests from the tests-lane author or tests-lane reviewer. Both must read them.
- Sandboxing the builder's filesystem generally.
- Preventing a builder from *inferring* likely tests from a well-written requirement. Hidden tests
  hide the encoding, never the specification.

### Historical §9 — residual risk versus §16.3 item 8

Proxy-gaming is mitigated, not solved. Hidden tests reduce overfitting to fixture values and make
editing the test file structurally impossible. They do not reduce writing the shallowest code that
satisfies the stated requirement.

**Fourth mode, measured 2026-08-27:** `lane-routing-chemical` manufactured the gate's case count at
collection time with synthetic `test_*` functions. Hidden tests would not have prevented it. The
repair was provenance (`gate_capture.py`): every counted case defined in accepted test bytes. That
check is independent of visibility.

Honest boundary: hidden tests are hidden from a **cooperative-but-optimizing** builder, not from an
adversarial one with `--dangerously-skip-permissions`. Recommendation at the time: build read
containment first as a standalone prerequisite. That prerequisite shipped as the vault.

### Historical §10 — documents this draft contradicted

Proposed amendments to then-current `MAESTRO_architecture.md` §7.4, §1.1 item 4, §16.3 items 8 and
15, §19 M41, and `docs/plan-authoring.md` pairing text. The factory contract replaced those
sections rather than visibility-scoping the old gate.

### Historical §11 — regressions the old design had to ship with

1. Hidden-lane builder prompt contains no hidden path, gate argv, or hidden blob line.
2. Builder worktree contains no hidden test file; adding one is refused.
3. Repair handoff after a red post-gate carries only `RepairDirective` fields.
4. `composed_tree_sha` recomputation refuses a mismatched receipt.
5. Resume of a merged-visibility run does not rewire `needs_json`.
6. A `maestro-plan.v4` plan still starts and resumes.
7. Read containment: builder cwd cannot `git cat-file` the hidden blob.
8. Reveal step lands accepted tests before the integration gate.

Factory equivalents of 1, 2, and 7 are still required and are owned by private-review / vault
isolation. Items 3–6 and 8 describe withdrawn machinery.

### Historical §12 — open questions (resolved for the factory slice)

- **Q1.** Separate harness-private object store? **Yes — shipped as the vault under
  `runtime_state_root`.**
- **Q2.** Repair oracle granularity? **Four-key redacted findings, not per-selector unmet flags.**
- **Q3.** Plan-authored `repair_hint`? **Not a factory field; public acceptance criteria only.**
- **Q4.** Reveal tests onto the delivered branch before an integration gate? **Final review runs
  sealed tests; publication is receipt-backed. Private bytes stay out of builder input.**
- **Q5.** Raise semantic ceiling `K`? **No. Retry budgets are deleted. Continuation is `run resume`
  / `run amend`.**
