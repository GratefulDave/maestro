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

Current implementation authority is `.claude/skills/sssf/templates/adws/` **in this repo**.

Deployed copies exist elsewhere — notably `lexgenius/adws/` — and **there is no sync
mechanism between them: no script, no test**. Every fix must be applied by hand to each copy.
Land changes here first, mirror deliberately, and state which copies were touched.

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
