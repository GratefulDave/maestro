# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Changed

- `docs/plan-authoring.md`: the single-repository sequence now places the Plan IR, its bound HTML
  view, and its PASS review receipt under `.maestro/` (`.maestro/<name>.plan.json`,
  `.maestro/<name>.html`, `.maestro/<name>.plan-review.json`) instead of at the repository root,
  and instructs `planctl render`/`validate`/`review` to run with `--repo-root .` so
  `source_artifacts` paths resolve from the repository root instead of `..`-relative to the IR's
  own directory. `.maestro/` is now the one directory everything plan-related is read from,
  alongside `.maestro/plans/<name>/maestro-plan.v1` where Maestro projects the finished plan.

### Added

- `docs/plan-authoring.md`: the path from a source document to an executable
  plan. Records that source documents are never converted — they are pinned as
  `source_artifacts` inside a `plan-contract.v1` Plan IR — and that the IR must
  carry `extensions.maestro` before review, because the review receipt is bound
  to the IR bytes. Covers the single-repository sequence and the
  finalize-then-compose rule for `maestro-workspace.v1`.

- `maestro bootstrap`: visible Herdr route admission. Captures first turn,
  continuation, pane cwd, and clean cancellation per configured route; mints
  or reuses Ed25519 key material under the repository state root; writes a
  detached-signature `maestro-route-receipt.v1` only after that capture
  verifies. Existing unsigned or junk files are refused, not overwritten.
- `maestro plan author`: the production writer of canonical `maestro-plan.v1`
  bytes. Reads a conventional draft, fills git-observed hashes, and
  create-once writes `canonicalize(plan)`.
- Named-plan commands fall back to bootstrapped `keys/signing.pub`,
  `keys/signing.seed`, and `keys/route.pub` when the configured environment
  variables are unset.

### Fixed

- Plan finalize and run start still refuse when no signed route receipt
  exists. There is no fixture-copy or hand-signed bypass.
- Herdr admission/launch no longer treat the typed composer prompt as a
  receipt. That false success closed the first pane and started a second
  Claude (launch command printed twice).
- Prompts are submitted through the documented coding-agent contract:
  `agent prompt <target> <text> --wait --until <status> --timeout <ms>`, which
  submits the text and its Enter atomically and fails loudly instead of
  reporting success for text left sitting in the composer. `pane run`, which
  types into the pane's shell rather than the agent composer, is gone from the
  launcher. On `agent_prompt_stalled` the recovery is a single
  `agent send-keys <target> enter` followed by `agent wait`; a second
  `agent prompt` would append to the unsubmitted line and send both halves as
  one garbled turn.
- Agent readiness is `agent wait <name> --until idle --timeout <ms>`. The
  launcher no longer polls the undocumented `agent get` field
  `interactive_ready` or scrapes the visible pane for per-agent startup
  banners.
- Marker detection strips the outbound prompt before scanning, so the prompt's
  own copy of the marker cannot be read as the agent's answer.

### Removed

- Dead launcher helpers `_agent_visible_text` and `wait_for_agent_process`,
  along with `wait_for_coder_ui`, the banner-sentinel readiness poll they
  supported.
