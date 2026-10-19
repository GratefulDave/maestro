# Run preflight

`run start` and `run resume` (and `run attend`, which both end in when a deployment enables
attend) run one preflight before any lane is dispatched. For every dependency the run can
reach it does three things, in order:

1. **Check** whether it is present.
2. **Provision** it from its known source when it is missing and has one, then check again.
3. **Refuse** only what is still missing.

Every check runs whatever an earlier one answered, so a refusal is one report listing every
missing item, not the first of them. The refusal is `RUN_PREFLIGHT_REFUSED`; the CLI prints
`{"outcome", "detail", "refused": [...]}` where each refused item carries `item_id`,
`lane_id` (empty for run-wide), `detail`, `remedy` and `human_secret`.

Implementation: `adw_modules/run_preflight.py` (the check engine and the checks),
`maestro.py` `_RunPreflight` (what this deployment and plan can reach), and
`FactoryScheduler.run`, which calls the preflight before its first transition.

## What is recorded

- One `RUN_PREFLIGHT` run artifact per preflight (ledger schema `artifact-factory.v8`):
  every item, and per lane its `needs`, `verified`, `added` and `refused` item ids, plus
  `needs_manifest` (packages installed tree-local because the project manifest lacks them)
  and the issues filed for them. A record only: nothing reads it to move a lane.
- One `steps.jsonl` line per provisioned item and per refused item, and one
  `preflight` line per lane with its counts.

## Boundaries

- A lane's **declared outputs** and its **accepted suite paths** are never needs. The
  preflight never creates, stubs or fetches what a tester or builder produces, and never
  touches an accepted test file. A suite import that a declared output provides is skipped.
- A missing Python package is installed into the lane tree's own `.venv`
  (`uv pip install --python <tree>/.venv/bin/python`). The target repository's manifest is
  never edited: that would be an unreviewed change to the published surface. The package is
  recorded under `needs_manifest`, a deduplicated GitHub issue is filed so a reviewed lane or
  amendment can fix the manifest, and every later tree `provision_tree` provisions in this
  process gets the same install after `provision_argv`.
- No key is ever generated, regenerated, moved or rewritten. Keys are read only.
- Config values, vendors, models and `provision_argv` are never changed.
- No agent pane is created for a check. The one provision step that creates panes is route
  receipt capture, which is how a receipt is minted at all.

## Checklist

### Run-wide

| Item | Check | Provision | Refuse when |
|---|---|---|---|
| `binary:uv` | `uv` on PATH | none | absent: install uv |
| `binary:herdr` | `executables.herdr` on PATH | none | absent |
| `herdr:server` | `herdr workspace list` exits 0 | none | the server is not reachable: open herdr |
| `python:runtime` | running python satisfies the runtime's `requires-python` | none | skipped: the ADW runtime declares no `requires-python` |
| `binary:<route>` | each reachable route's binary (`executables.omp`, `executables.claude`) on PATH | none | absent |
| `omp-profile:<name>` | `~/.omp/profiles/<name>/` exists for each omp route profile | none | absent (human: the profile holds the provider login; `omp --profile <name>`) |
| `key:route-verify:<file>` | each `route_verify_keys` file readable | never | absent (human) |
| `route-receipt:<route>` | `route_receipts.<route>` verifies against the route verify keys and admits the route | when the file is absent: `route_admission.admit_routes` captures it, signed with `keys_dir/route.seed` (read, never created) | unconfigured, no route seed, capture fails, or an existing receipt is invalid (an existing file is never overwritten) |
| `key:reviewer-hmac` | `keys_dir/reviewer-hmac.key` readable | never | absent (human) |
| `attend:planctl` | `attend.planctl` exists and is readable | none: no source the runtime can fetch it from | absent or unset |
| `attend:planroot-dir` | `attend.planctl_root_dir` (default per deployment, outside `~/.maestro`) is not the home directory, does not contain it, is not inside `~/.maestro`, does not overlap `keys_dir` and holds no copy of a key | nothing: set the key, a moved directory cannot be made safe by attend | `ATTEND_PLANROOT_UNSAFE` |
| `attend:plan-ir` | the revision's Plan IR is in attend's planctl root `<planctl_root_dir>/<run>/r<N>/`, parses as JSON, and its `<run>.r<N>.root.json` record names the revision's pinned plan by sha256 | first moves any root an earlier release left under `runtime_state_root/plans/<run>/` into `<planctl_root_dir>` (everything but the pinned plan; the record's `repo_root` follows), else copies the IR found beside the revision's recorded plan artifact (else `attend.plan_ir`), wherever it lives (a hand amendment in `/tmp` included), with every pinned source whose bytes match the IR's sha256, read from the IR's directory and its ancestors, then the run repository | no IR found, unparsable, or a required source no copy matches |
| `aws:planctl-runner` | `aws sts get-caller-identity --profile planctl-runner` exits 0 | never | expired or missing credentials (human); skipped under `run attend --local` |
| `gh:auth` | `gh auth status` exits 0 | never | not logged in (human) |
| `gh:repo-permission` | `gh repo view --json viewerPermission` is TRIAGE, WRITE, MAINTAIN or ADMIN | never | lower (human: grant access) |

Attend items (`attend:*`, `aws:*`, `gh:*`, `attend-tree:*`, and the attend routes'
binaries, profiles and receipts) are checked only when attend is enabled, and reported as
skipped otherwise.

### Per tree

| Item | Check | Provision | Refuse when |
|---|---|---|---|
| `lane-tree` | a worktree at the integration head, prepared by the launch path's own `_prepared_cwd` (refresh, then `provision_argv`) | the tree itself is created for the check and removed afterwards | `provision_argv` fails there |
| `runners` | `FactoryScheduler._assert_runners_usable`: every runner the plan names resolves in a provisioned review-shaped tree | none | a runner does not load; the tree is kept for inspection |
| `attend-tree:operator`, `attend-tree:reviewer` | the real `attend_operator` / `attend_reviewer` dispatch, stopped after tree preparation and before any pane | none | the launch path fails preparing the tree (it must not provision a non-git tree) |

### Per lane (every lane not yet `MERGED`)

| Item | Need determined from | Check | Provision | Refuse when |
|---|---|---|---|---|
| `python:<module>` | Python imports of the accepted suite the lane is graded against, minus stdlib, the tree's own modules, and anything a declared output or suite path provides | `importlib.util.find_spec` in the lane tree's `.venv`, from the gate's `cwd` | `uv pip install` into the tree's `.venv`; recorded as `needs_manifest` and filed as an issue | the install fails or the module is still missing |
| `fixture:<path>` | `spec.obligations.observed_baseline[].path`, unless a declared output or suite path | the path exists in the lane tree | none: the plan says it exists before the lane runs | absent |

Not determined, because the plan schema declares none: environment variables and
tool binaries per lane. A lane with no accepted suite yet (a tests lane before its tester
writes one) has no suite imports.
