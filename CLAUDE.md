# maestro — read this before doing anything

Maestro is a **dependency-DAG software factory**: lanes plan, build, and review, each node
attempt isolated in its own git worktree, each agent node launched in a visible Herdr pane,
with deterministic merge, typed envelopes, gates, and a SQLite lifecycle store.

## Mandatory reading, before writing any code or spawning any agent

| Document | What it binds |
|---|---|
| `MAESTRO_architecture.md` | The acceptance predicate (§1), the failure predicate (§1.2), node kinds and their evidence chains (§7.3), and §3.5–§3.6 — a corpus of real incidents with the design lessons already extracted |
| `docs/plan-authoring.md` | The only supported path from a source document to an executable plan |
| `AGENTS.md` | Repository layout and where the implementation actually lives |

Do not design a feature this project has already specified. Check these first; if a brief
you were given contradicts them, the documents win — say so rather than following the brief.

## Two rules that invalidate work if broken

1. **§1.2** — a run FAILS its acceptance predicate if *any* lifecycle transition is caused by
   pane text, prompt text, a free-text envelope field, or an agent's claim about its own work.
   Transitions key on typed records and signed receipts, never on model prose.
2. **§1.1 item 4** — every merged node carries a complete evidence chain **scoped to its node
   kind**. A new node kind means extending that scoping, not reusing another kind's chain.

## Where the implementation lives

The ADW runtime exists in three copies:

| Copy | Role |
| --- | --- |
| `.claude/skills/sssf/templates/adws/` (this repo) | The template. Where the factory ships from. |
| `lexgenius/adws/` | A deployed instance, and in practice where fixes have landed first. |
| `the-library/skills/sssf/templates/adws/` | The install source for `skills/sssf`. |

**There is still no script that copies one to another.** They drift silently, and until
2026-08-17 the drift was only discovered when something broke. That day the template copy was
found to be ~750 lines behind in `maestro.py` alone, missing every plan verb in daily use, and a
revert in a consuming repo silently deleted 6009 lines of runtime from another copy.

The two **template** copies are now held together by a test rather than by discipline:
`.claude/skills/sssf/templates/adws/tests/test_template_parity.py` compares this repo's template
against the-library's file by file and fails naming the files that differ and in which direction.
It is mirrored into the-library along with the runtime, and re-exported into that repo's own
suite by `the-library/tests/test_sssf_adws_copy_parity.py`, so both repositories check the
invariant from their own side. A file present in one copy and not the other fails, which is the
6009-line loss mode. It skips only when the peer repository is not checked out at all; a peer that
is checked out with its runtime directory missing fails.

`lexgenius/adws/` is **not** covered by that test. A deployed instance carries its own
`maestro.config.yaml` and legitimately runs ahead of the template, so it is reconciled by hand.
Treat this repo's template as authoritative for what the factory *ships*, but **verify before
assuming it is current against a deployment** — `diff -rq` against the instance rather than
trusting this file, which can itself go stale. When landing a change, say explicitly which copies
you touched, and mirror deliberately rather than assuming a mirror already happened.

**State as of 2026-08-18.** The two template copies were reconciled that day and are identical
for every tracked file — `tests/test_template_parity.py` passes from this repo, and its mirror
passes from the-library. `lexgenius-pipeline/adws/` **was** brought level by hand later the same
day: `diff -rq` against the template reports exactly one differing tracked file,
`maestro.config.yaml`, which is deployment-specific by design, plus deployment-only directories
and a stray `maestro.py.orig`. What that reconciliation cost is measured rather than assumed,
because it was made after the divergence had already been paid for. Beforehand the deployment
differed in twelve modules and seventeen test modules, was missing `handoff_budget.py` outright,
and — the divergence that had a cost — its `code_review.py` carried **no `_node_goal`
function**, so the M1 reviewer-contract fix had never reached it, and run
`run-0120c32064d144c2aa55c344087e0b0a` had every reviewer told "Make the gate '…' pass over
selector '…', changing only the declared outputs" — verbatim, while the plan it was running
carried the correct instruction. See §19 M13. Nothing enforces the level state and nothing would
notice it drifting again (§16.3 item 50), so re-derive it rather than trusting this paragraph.
`lexgenius/adws/` was not touched and is far behind: its most recent `adws/` commit is the
reviewer-report fix, so it is missing every launcher, worktree, run-state, and reviewer-contract
fix that landed after it — `adw_modules/code_review.py` does not exist there at all — and it
holds files the template does not (`adw_data/`, `adw_sssf_config/`, a stray `worktree.py.orig`).

Do not read "reconciled" as covering `lexgenius/adws/`, do not read it as a state anything
maintains, and do not read a `diff -rq` filename as a cost — `code_review.py` was reported as differing the whole time it was silently degrading
every review in that deployment. Re-derive this paragraph with `diff -rq` before relying on it;
it is a dated observation, not an invariant, and the only invariant here is the parity test
between the two template copies. For the one divergence with a known behavioural cost there is a
direct check, which is cheaper than reading a diff and states what it means:

```bash
grep -c "def _node_goal" <deployment>/adws/adw_modules/code_review.py   # 0 = reviewers are judging against a placeholder
```

`maestro.config.yaml` is deliberately *not* copied from a deployment. It is deployment-specific:
its lane vendors, models, and concurrency name a particular installation. The template's copy
carries the same schema keys as the deployments, with template-shaped values, and the parity test
does compare it between the two template copies.

The visualizer (`.claude/skills/sssf/apps/visualizer/`) exists only in this repo — no copies,
no ambiguity.

## Reviewer design — settled, do not re-derive

`MAESTRO_architecture.md` §3.6 (Family B) records these as observed production failures:

- No actor signs off on its own output; review is cross-vendor over the merged surface (B12).
- A verdict carries structured findings bound to locations, and FAIL is structurally
  impossible without a located ERROR finding — **enforced from v1**, because a field added
  later is optional forever (B8).
- The reviewer's input is a declared contract: goal, `produces`, acceptance (B9).
- Size-check every handoff against the reviewer's context window before dispatch and fail
  closed; an overflowing reviewer fabricates a verdict about a different workflow (B13).
- Re-review of byte-identical input is impossible or explicitly recorded, with an operator
  escape (B10).
- Detect quiescence-after-liveness rather than imposing a wall clock, and arm detection only
  after `working` or `blocked` is first observed (B14).
- Never gate progress on a zero-finding LLM sweep with restart-on-any-finding — it has no
  bounded termination. Bound the loop or accept graded findings (A9).
- If a check's field has zero readers, that is a build failure (B15).

§19 records where Maestro itself broke these. B9's declared contract was degenerate in
production until 2026-08-18 — a projection silently dropped the node's `instruction`, so every
agent-node reviewer was told "make the gate pass" and nothing else (§19 M1) — and B13's size
check sat on one launch path instead of the chokepoint every route crosses (§19 M6). A lesson in
the list above is not a property of the code; read §19 beside it.

## Verifiers

`Gate.runner` is `Literal["pytest", "vitest"]` and nothing else projects, because Maestro
counts executed cases against `min_cases`. A shell script, Makefile target, `psql` migration,
or `curl` check proves nothing countable and is refused with `maestro.command`. Verify such
work by asserting its effect from a test the runner can count.

Counting note: a repo whose `pytest.ini` sets `-v` cancels `-q`, so every collection count
must pass `-o addopts=` or it silently returns zero.

## Herdr panes

Close every pane when its work is done — `herdr pane close <pane_id>`, positional; `--pane`
fails. `kill_reviewer` does not reliably close panes. Reviewer panes are not named
`maestro-*`; identify them by agent kind, repo cwd, and title.
