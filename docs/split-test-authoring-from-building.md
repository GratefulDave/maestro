# Splitting test authoring from building

**Status: design note. Recommend, do not implement.** Nothing here is built. It changes plan
shape, so building it forces a re-ship, and a re-ship orphans the paused run
`run-c8910572828c4f5bb5c60c0582dd4be5`. Written 2026-08-20, beside the Phase 1 change recorded as
`MAESTRO_architecture.md` §19 M35.

---

## 1. The defect this addresses, and what Phase 1 already did about it

A Maestro agent node declares the paths it may write, and for every lane in
`cmo-consolidation-l-r7` those paths are **both** the production module and the test file the
node's own gate counts:

```json
{ "node_id": "lane-p5-gap-policy",
  "outputs": ["src/lexgenius_pipeline/ingestion/judicial/cmo/gap_policy.py",
              "tests/unit/ingestion/test_cmo_gap_policy.py"],
  "gate": { "runner": "pytest",
            "argv": ["tests/unit/ingestion/test_cmo_gap_policy.py"],
            "min_cases": 9 } }
```

The thing being satisfied is written by the thing satisfying it. `min_cases` counts cases, not
assertions, so nine tests that carry their own copy of the production logic satisfy every check
the runtime has: the pre-node gate is red because nothing exists yet (§7.4), the post-node gate is
green because the tests never needed the module, the permission check passes because both paths
are declared (§8.3), and the reachability refusal is silent because the test file references the
symbols the production file defines.

Phase 1 closed that with a count rather than a judge: after the post-node gate passes, revert
every path the attempt wrote that the gate's own argv does not select, re-run the gate, and
require it to fail (§7.4). It works on an already-shipped plan because it reads only `node.outputs`
and `node.gate`.

**What Phase 1 does not do is make the hole structurally unreachable.** It detects a hollow test
after the agent has written one, costs an attempt and a gate run to detect it, and — as §7.4
records — has *no subject at all* for a node every one of whose written paths its gate selects.
The proposal below removes the ability to write a hollow test rather than detecting it.

---

## 2. The proposal

One lane becomes two nodes.

| | spec node | build node |
|---|---|---|
| `node_id` | `lane-p5-gap-policy-spec` | `lane-p5-gap-policy` |
| `needs` | — | `["lane-p5-gap-policy-spec"]` |
| `outputs` | `tests/unit/ingestion/test_cmo_gap_policy.py` | `src/…/cmo/gap_policy.py` |
| `instruction` | the requirement, as the thing to witness | the requirement, as the thing to build |
| gate | the selector **collects `min_cases` and every case fails** | `pytest tests/unit/…` , `min_cases: 9` (unchanged) |

The build node **cannot write the test file**, and nothing new enforces that: §8.3's permission
check already refuses a measured inventory delta that is not a subset of the node's declared
outputs, and an agent node's clause-4 failure is already SEMANTIC with the offending paths named
in the retry prompt (§7.5). The write-scope mechanism exists and is load-bearing today; this
proposal only gives it a boundary worth drawing.

Three things follow that are worth more than the headline.

**The build node's pre-node gate becomes a real witness.** Today it is red because the test file
does not exist — a fact about an absent file, not about absent behaviour. After the split the
spec node has merged, so the test file is present at the build node's base and the pre-node gate
is red *because the behaviour is missing*. §7.4 already claims that reading ("a red pre-gate is red
for the intended reason"); this is what would make it true.

**The reviewer's diff surface halves.** §19 records `lane-p5-gap-policy` as nine behaviours behind
one gate, with `diff.introduces_no_obvious_defect` reading a diff no claim bounds. Two nodes are
two diffs, each with its own instruction.

**Phase 1's falsification check keeps its job and gets cheaper to reason about.** The build node
still writes only production code, so the revert set is exactly its output and the check is a
strict statement about it. It is not made redundant: a build node can still ship helpers nothing
executes.

---

## 3. What it does to the plan schema

### 3.1 A new node kind, and therefore a new schema version

`NodeKind` is `{agent, code}` today. The spec node is neither.

It is not a `code` node: its acceptance is not an exit code, its command is not deterministic, and
it launches an agent in a pane.

It is not an `agent` node either, and the reason is the gate. §7.3 clause 3 adjudicates an agent
node's post-node gate under §10.2's counting rule — `passed >= min_cases >= 1`, `failed == 0`,
`skipped < collected`, `errored == 0`. A spec node's post-node gate must satisfy the **opposite**
predicate: the selector collects at least `min_cases`, and every collected case fails or errors,
because the production code it describes does not exist yet. Running a spec node as an `agent`
node would make its own success condition unreachable — the same shape §7.3 records for an
unscoped `VERIFIED` making a code node unable to merge.

So: `NodeKind.SPEC`, with its own `VERIFIED` predicate:

1. the agent's terminal envelope parses (§10.1);
2. the **pre-node** run of the selector collects **zero** cases — the file is absent, so there is
   nothing yet to fail;
3. the **post-node** run collects `>= min_cases` and `failed + errored == collected` — the tests
   exist, they run, and they all say the behaviour is missing;
4. the worktree delta passes §8.3's two-conjunct permission check.

Clause 3 is where the design earns its keep and also where it is most exposed. A test file of
`assert False` satisfies it. That is the residual, stated in §6 below rather than argued away.

§6.3 is unambiguous about the cost of the enum member: *"A shipped version's model class is frozen
forever — no added, removed, or re-defaulted field. A new field means a new version string and a
new class. There is no upgrade function."* Widening `kind` is a change to the frozen class, so it
is **`maestro-plan.v3`**.

### 3.2 What v3 does *not* cost

There is no upgrade function, which is the point of the registry: a shipped `maestro-plan.v2` file
under v3 code dispatches to the frozen v2 model with v2 obligations and keeps running. Existing
plans are not invalidated by the version bump itself.

This is worth stating precisely because the last bump did have a cost: §104 made a
`maestro-plan.v1` plan **unrunnable** at run start (`RUN_PLAN_SCHEMA_VERSION_UNRUNNABLE`), which is
a deliberate refusal rather than a consequence of the registry. A v3 bump should not carry one.
`maestro-plan.v2` should stay runnable.

### 3.3 Two projections, not one

`plan_contract_ingress` projects a contract IR onto plan nodes. Today one lane yields one node.
The split makes it yield two, which means the IR needs to say which requirement's witness is
authored separately — or the projection needs a rule, applied uniformly, that every lane with a
gate whose selector is inside its own outputs becomes a spec/build pair. The uniform rule is the
better one: it is a predicate over the plan the projection already holds, it needs no new IR
field, and a per-lane opt-in is a switch someone forgets.

---

## 4. Evidence-chain scoping (§1.1 item 4)

§1.1 item 4 requires every merged node to carry a complete evidence chain **scoped to its node
kind**, and §7.3 records what happens when that scoping is dropped. A third kind means extending
the scoping, not reusing another kind's chain.

| element | agent | code | **spec (proposed)** |
|---|---|---|---|
| typed envelope | ✔ | — | ✔ |
| attempt row | ✔ | ✔ | ✔ |
| pane correlation | ✔ | — | ✔ |
| worktree path | ✔ | ✔ | ✔ |
| output commit SHA | ✔ | ✔ | ✔ |
| pre-node gate FAILED | ✔ | — | **replaced**: pre-node selector collected **zero** |
| post-node gate PASSED | ✔ | — | **replaced**: collected `>= min_cases`, all failing |
| recorded zero exit | — | ✔ | — |
| declared-expectation result | — | ✔ | — |
| permission check over the measured delta | ✔ | ✔ | ✔ |
| git ancestry proof | ✔ | ✔ | ✔ |
| §7.4 post-work falsification | ✔ | — | **not applicable** — see §6 |

The repair chain (§7.5) applies unchanged: a spec node that fails its own post-node predicate
after committing has a proven output commit, so the next attempt repairs it.

The chain rows are two new gate-result shapes, not two new tables. `GateResult` already records
`counts`; what is new is a second adjudicator beside `adjudicate_counts` reading the same parsed
counts under the inverted rule. One parser, two rules — which is the shape §10.2 already
prescribes and the reason it insists on one parser.

---

## 5. Re-ship cost

**No shipped plan can be migrated in place.** §6.3's rule is that a shipped plan is re-projected
under a new name, never patched, and #104 made the same point structurally.

For `cmo-consolidation-l-r7` specifically:

- Five agent lanes become ten nodes. Each spec node needs an instruction derived from the same
  requirement and a gate declaring the inverted threshold; each build node keeps its existing gate
  verbatim and loses one declared output.
- The plan is re-projected as **r8**, minting a new digest. That is not free: the plan receipt
  must be re-signed, `integration/cmo-consolidation-l-r5` is still the base commit, and the
  cross-run budget count starts from zero — which §7.5 calls the correct cost, because the debt
  belongs to the plan bytes.
- **The paused run is orphaned.** `run-c8910572828c4f5bb5c60c0582dd4be5` is a run of the r7
  digest. It cannot be resumed onto r8, and its two spent attempts are not transferable. This is
  the whole reason Phase 1 was built to work on the shipped bytes and this was not.
- Wall-clock: ten nodes rather than five, each with its own worktree, pane, and three gate runs.
  Depth increases by one for every lane, so the DAG's critical path lengthens even though the
  lanes still run concurrently.

**Recommended sequencing.** Do not migrate r7. Land the split for the *next* plan authored after
it, so the first plan to carry spec nodes is one authored with them rather than retrofitted. Phase
1's falsification check covers r7 in the meantime, which is what it was built for.

---

## 6. Residuals, stated rather than argued away

**R1 — the spec node's own hollow shape.** Nothing in the proposed predicate stops a spec node
shipping nine `assert False` cases. They collect, they all fail, and the build node then has to
make nine meaningless assertions pass. The split moves the hole rather than closing it: it can no
longer be filled *after* seeing the production code, which is the half that mattered in the
observed failures, but it is still a hole. Phase 1's post-work falsification cannot help here,
because a spec node's only written path is the one its gate selects — the "no subject" case §7.4
reports and does not refuse. Closing R1 needs a different count and this note does not have one.

**R2 — the split's cost is paid on every lane, and the hole was found on one.** Nine of the twelve
historical rejections of `lane-p5-gap-policy` were not about hollow tests at all. A change that
doubles node count across every plan to close a defect measured on one lane should be adopted
with that ratio in view.

**R3 — the projection rule needs a measured case.** §3.3 proposes "every lane whose gate selector
is inside its own outputs becomes a pair." Every lane in r7 matches. No lane that should *not*
match has been observed, which means the rule's negative case is untested.

---

## 7. Recommendation

Adopt, for plans authored after r7, and not before:

1. **Do not implement any of this until r7 has run** under Phase 1's falsification check. That run
   is the measurement that says whether the structural fix is needed at all — if the check refuses
   nothing across five lanes, the hollow-test shape was rarer than the ledger suggested.
2. If it is needed: `maestro-plan.v3` with `NodeKind.SPEC`, its own four-clause `VERIFIED`
   predicate, its own scoped evidence chain, and the uniform projection rule — **as one change**,
   because a partial version bump is the invariant-loss shape §3.6 B15 names.
3. Keep `maestro-plan.v2` runnable. The registry gives that for free and only a deliberate refusal
   takes it away.
4. Do not implement R1's answer speculatively. If a spec node ships `assert False` in production,
   that failure will name the count that refuses it; inventing one now would be a check with no
   measured case behind it, which is what §16.3 exists to record instead.
