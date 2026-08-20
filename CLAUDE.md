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

**There is now one script that copies one to another, and it is the only one you may use.**
`.claude/skills/sssf/templates/adws/tools/runtime_sync.py` — `check <source> <destination>`
answers "are these level?" structurally (which files differ and in which direction, and,
separately, which exist in one copy and not the other, because that is a deletion rather than
an edit); `mirror <source> <destination>` plans and `--apply` writes, with a sha256 assertion
per file. It never deletes, it refuses a destination that looks ahead of the source unless you
pass `--overwrite-ahead`, and it holds `maestro.config.yaml` back whenever either endpoint is
a deployment. `tests/test_template_parity.py` calls the same comparison, so the test's failure
and the mirror's repair are the same definition of level. Do not use `cp`, `rsync`, or
`git apply` — `git apply` reports success and changes nothing on this machine.

Before it existed there was nothing, and the drift was only discovered when something broke: on
2026-08-17 the template copy was found ~750 lines behind in `maestro.py` alone, missing every
plan verb in daily use, and a revert in a consuming repo silently deleted 6009 lines of runtime
from another copy. Nothing *schedules* the new script either. It makes a reconciliation provable
and refusable; it does not make one happen.

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
assuming it is current against a deployment** — run `runtime_sync.py check` against the instance
rather than trusting this file, which can itself go stale. When landing a change, say explicitly which copies
you touched, and mirror deliberately rather than assuming a mirror already happened.

**State as of 2026-08-20, re-derived by running both the tool and the test.**
`tests/test_template_parity.py` **passes** from this repo — both assertions, same-files and
byte-identical-contents — and `runtime_sync.py check` agrees: template and the-library are level
over **201** compared files. That is after five branches landed here today (#104, the
`maestro-plan.v2` bump and the pre-v2 run refusal; #105, the reviewer-key environment split; #106,
the finalization turn clock gated on route liveness; PR #111, a review rejection repairing the
diff it rejected instead of re-implementing the node; PR #112, `run convergence` no longer
reporting a live run as one that already ended) and the-library was brought level by
`runtime_sync.py mirror --apply` in its PR #121. Earlier readings recorded here (189, then 200
files) are superseded rather than wrong: the count moves whenever a test file is added, which is
exactly why it dates a snapshot instead of describing an invariant.

Two qualifications outrank any such snapshot, this one included. **the-library's mirror can be
uncommitted there.** It is committed as of 2026-08-20 — 201 tracked files under
`skills/sssf/templates/adws/`, nothing modified or untracked under that path, and `main` level with
its remote after PR #121 — but it has been uncommitted before, and the check that would catch it is
not the parity test. A green parity run or a `runtime_sync check` says the bytes on disk agree; it
says nothing about whether they are committed, and an agent that resets or checks out in that
repository takes an uncommitted mirror with it. Confirm with `git status` there, not with parity.
That repository also carries unrelated work — 14 modified and 10 untracked files elsewhere in its
tree, identical sets before and after this mirror, verified by diffing the two `git status` outputs.
Stage only the paths the mirror wrote — `git add skills/sssf/templates/adws` — which is what
preserves them; a `git add -A` in that repo would sweep them into a commit claiming to be a
mirror. And the differing set moves while it is being measured, because other lanes
write into this template concurrently: an earlier observation watched
`test_agent_start_busy_retry.py` go from clean to modified minutes apart. Any comparison between
these copies is a reading of a moving tree — run the test, do not trust this paragraph.

Both deployments **track** `adws/` in git — `lexgenius` at 211 files on branch
`chore/mirror-adws-runtime`, `lexgenius-pipeline` at 208 on `parked/cmo-consolidation-l-run`, each
under a commit named "Mirror the ADW runtime from maestro into this deployment". The earlier
reading here recorded `adws/` as
*entirely untracked* in lexgenius-pipeline, which meant git would neither preserve nor notice a
change to it; that hazard is closed for both, and a mirror there is now a reviewable diff rather
than an invisible overwrite.

Re-derived 2026-08-20 with `runtime_sync.py check`, which is the tool to use — not `diff -rq`, which
reports a filename without saying which side is ahead:

| comparison | result |
| --- | --- |
| template ↔ the-library | level over 201 files |
| template ↔ `lexgenius/adws/` | level over 200 files (`maestro.config.yaml` held out) |
| template ↔ `lexgenius-pipeline/adws/` | level over 200 files (`maestro.config.yaml` held out) |

All three were brought level on 2026-08-20, and both deployments' mirrors are committed on their
own branches. The run that had been executing in lexgenius-pipeline is dead — its scheduler was
killed and its panes closed — so that repository was writable again and was mirrored with the
others.

`lexgenius` was the one that needed a decision, and issue #71's per-file question is now answered.
The three files it had genuinely been ahead on are no longer ahead: `adw_modules/deliver.py` and
`tests/test_step10_cli.py` were already byte-identical, and the template's `tests/test_deliver.py`
turned out to be a strict superset of the deployment's — zero lines existed only there, and
`DeliverReleaseSafetyTest`, the class issue #95 records a previous mirror destroying, is present in
the template. Exactly one file was still ahead in the deployment, `tests/test_node_write_scope.py`
by a single line: a `record_stall` keyword argument the template deliberately removed when the
re-dispatch lock test stopped taking a stall recorder. Keeping it would have failed against the
runtime now in that tree, so it was overwritten with `--overwrite-ahead`, which is the only use of
that flag here and was taken after reading the line rather than in place of reading it.

Mirroring a deployment costs something it did not cost before. #104 makes a `maestro-plan.v1` plan
**unrunnable** at run start (`RUN_PLAN_SCHEMA_VERSION_UNRUNNABLE`); the remedy is to re-ship the plan
from its IR, which is cheap but is not nothing, and it cannot be done while that plan is mid-run.
Mirroring this template into a deployment that holds shipped v1 plans refuses those plans until they
are re-shipped, so sequence the mirror against what is running there rather than treating it as a
neutral copy.

`_node_goal` answers `1` in **both** deployments as of 2026-08-20, so no deployment's reviewers are
judging against the placeholder. lexgenius-pipeline was brought level by hand on 2026-08-18, after the divergence had already been paid for by run
`run-0120c32064d144c2aa55c344087e0b0a`, whose every reviewer was told "Make the gate '…' pass over
selector '…', changing only the declared outputs" verbatim while the plan it was running carried the
correct instruction (§19 M13).

Nothing enforces any of this and nothing would notice it drifting again (§16.3 item 50). Do not read
"level" as a state anything maintains, and do not read a differing filename as a cost — `code_review.py`
was reported as differing the whole time it was silently degrading every review in that deployment.
Re-derive the table above before relying on it; it is a dated observation, not an invariant, and the
only invariant here is the parity test between the two template copies. For the divergence with a
known behavioural cost there is a direct check, cheaper than reading a diff and stating what it means:

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
