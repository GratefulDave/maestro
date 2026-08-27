#!/usr/bin/env python3
"""Prove, against real Herdr, that one prompt is offered once and observed once.

The unit tests in `tests/test_prompt_submission_proof.py` settle what
`submit_agent_prompt` concludes from a given set of Herdr answers. They cannot
settle what Herdr actually answers, and the 2026-08-27 incident lived exactly
there: every fake in the suite let `agent wait` succeed, real Herdr raises it
for a composer that has not reached `working`, and the whole family of wrong
conclusions was invisible to a green suite.

So this drives the production functions -- `wait_for_interactive_agent` and
`submit_agent_prompt` -- against a real pane running a real route, and asserts
the two properties the incident violated:

  offered once   `pane send-text` is issued exactly one time. A second offer
                 appends to an unsubmitted line and sends both as one garbled
                 turn, which is why recovery presses Enter instead.
  observed once  the call returns having *proven* consumption from the actor
                 transcript (or recovery Enter), never from the pane revision.
                 Paste-repaint advances that meter whether or not the composer
                 submitted.

Usage:
    python3 tools/prompt_submission_smoke.py --route omp --profile deepseek

Creates its own workspace and closes it again, so it touches no pane a run or
an operator owns. Exit 0 means the seam is sound for that route.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import launcher as lch  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402


class RecordingHerdr:
    """The launcher's own subprocess shim, with every call written down."""

    def __init__(self, binary: str, env: Dict[str, str]):
        self.binary = binary
        self.env = env
        self.calls: List[tuple] = []

    def __call__(self, *args: str, timeout: float = 30.0, **_kw) -> dict:
        self.calls.append(tuple(args[:2]))
        merged = dict(os.environ)
        merged.update(self.env)
        result = subprocess.run(
            [self.binary, *args],
            capture_output=True,
            text=True,
            env=merged,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            refusal = (result.stderr or result.stdout).strip()
            raise lch.HerdrCallError(
                "LAUNCH_REFUSED:{}".format(refusal[-400:]),
                lch.herdr_error_code(refusal),
            )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"result": {"text": result.stdout or ""}}

    def count(self, *verb: str) -> int:
        return sum(1 for call in self.calls if call[: len(verb)] == verb)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--route", default="omp", choices=("omp", "claude"))
    parser.add_argument("--profile", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", default="high")
    parser.add_argument("--herdr", default=shutil.which("herdr") or "herdr")
    parser.add_argument("--omp", default=shutil.which("omp") or "omp")
    parser.add_argument("--claude", default=shutil.which("claude") or "claude")
    parser.add_argument("--timeout-s", type=float, default=60.0)
    parser.add_argument(
        "--cwd",
        default=None,
        help="pane working directory. Defaults to the scratch root, which is "
             "right for omp; the claude route blocks on folder trust in a "
             "directory it has never seen, and production always launches it "
             "in the repository, so pass that here.",
    )
    parser.add_argument(
        "--start-window-s",
        type=float,
        default=120.0,
        help="bounded window for re-offering `agent start` over herdr's typed "
             "survivable codes; wider than production's because this boots a "
             "cold agent in a fresh workspace",
    )
    parser.add_argument(
        "--xdg-cache",
        default=None,
        help="ALSO pass `--env XDG_CACHE_HOME=<path>` into the pane. Maestro "
             "stopped redirecting this on 2026-08-27 because a pane's login "
             "shell reads its credentials through it; passing a fresh empty "
             "directory here reproduces the incident on demand, which is the "
             "live control for that removal.",
    )
    parser.add_argument(
        "--env-keys",
        default=None,
        help="comma-separated subset of the production redirection keys to "
             "apply, for bisecting which one a route cannot start under",
    )
    parser.add_argument(
        "--production-env",
        action="store_true",
        help="create the pane with the same scratch redirection a run uses "
             "(`worktree.launch_env` -> `pane_env_flags`). Production always "
             "does; a smoke that does not is testing a different pane.",
    )
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="maestro-submit-smoke-"))
    session_dir = root / "session"
    session_dir.mkdir()
    prompt_path = root / "agent-prompt.txt"
    # Deliberately trivial and terminal. The smoke is about the submission
    # seam, not about the agent's answer; a prompt that provokes tool use
    # would measure the model instead.
    prompt_path.write_text(
        "Reply with exactly the word ACK and then stop. Do not use any tools.\n",
        encoding="utf-8",
    )

    pane_cwd = Path(args.cwd).resolve() if args.cwd else root
    spec = lch.LaunchSpec(
        correlation_token="smoke-" + uuid.uuid4().hex[:12],
        worktree=pane_cwd,
        prompt_path=prompt_path,
        envelope_path=root / "agent-envelope.json",
        route=args.route,
        model=args.model,
        effort=args.effort,
        profile=args.profile,
        session_dir=session_dir,
    )
    if args.route == "omp":
        route_argv = lch.build_omp_argv(Path(args.omp), spec)
    else:
        route_argv = lch.build_claude_argv(Path(args.claude), spec)

    herdr = RecordingHerdr(args.herdr, {})
    name = lch.agent_name_for(spec.correlation_token)
    workspace_id: Optional[str] = None
    failures: List[str] = []
    try:
        env_flags: List[str] = []
        if args.production_env or args.env_keys:
            full = list(lch.pane_env_flags(wt.launch_env(root / "harness-scratch")))
            if args.env_keys:
                wanted = {k.strip() for k in args.env_keys.split(",") if k.strip()}
                env_flags = []
                for flag, assignment in zip(full[0::2], full[1::2]):
                    if assignment.split("=", 1)[0] in wanted:
                        env_flags.extend([flag, assignment])
            else:
                env_flags = full
        if args.xdg_cache:
            Path(args.xdg_cache).mkdir(parents=True, exist_ok=True)
            env_flags = list(env_flags) + [
                "--env",
                "XDG_CACHE_HOME=" + args.xdg_cache,
            ]
        created = herdr(
            "workspace",
            "create",
            "--cwd",
            str(pane_cwd),
            "--label",
            "maestro-submit-smoke",
            "--no-focus",
            *env_flags,
            timeout=60.0,
        )
        workspace = _payload(created, "workspace")
        workspace_id = str(workspace.get("workspace_id") or "")
        pane_id = _first_pane(herdr, workspace_id)
        if not pane_id:
            raise RuntimeError("SMOKE_NO_PANE: workspace created no readable pane")

        # Production's own bounded re-offer, over herdr's own typed survivable
        # codes (`agent_pane_busy`, `agent_not_ready`). The window is widened
        # here and only here: production splits into an existing tab, while
        # this creates a fresh workspace whose coding agent boots from cold,
        # and losing the smoke to that timing accident would say nothing about
        # the submission seam it exists to measure.
        try:
            lch._start_agent_when_free(
                lambda: herdr(
                    "agent",
                    "start",
                    name,
                    "--kind",
                    args.route,
                    "--pane",
                    pane_id,
                    "--timeout",
                    "180000",
                    "--",
                    *route_argv[1:],
                    timeout=185.0,
                )
            )
        except lch.HerdrCallError as exc:
            # `agent_not_ready` is not a failed start and must not be
            # re-offered. herdr has already registered the agent -- the 2026-08-27
            # re-offer collided `agent_name_taken` with `status=Blocked` on the
            # very agent it was trying to create -- it is simply still booting.
            # The readiness wait below is the thing that answers that, so drop
            # through to it rather than treating a race as a refusal.
            if lch.herdr_error_code(str(exc)) != "agent_not_ready" and (
                exc.code != "agent_not_ready"
            ):
                raise
        lch.wait_for_interactive_agent(herdr, name, args.start_window_s)
        transcript = lch.wait_for_agent_transcript(
            herdr, name, args.start_window_s, launched_cwd=pane_cwd
        )
        if transcript is None:
            raise RuntimeError(
                "SMOKE_NO_TRANSCRIPT: actor transcript path never appeared, "
                "so this run cannot judge the submission seam"
            )
        handle = lch.LaunchHandle(
            spec.correlation_token,
            pane_id,
            name,
            pane_cwd,
            transcript_path=transcript,
        )
        offered_before = herdr.count("pane", "send-text")
        started = time.monotonic()
        lch.submit_agent_prompt(
            herdr,
            pane_id,
            "@{0} ".format(prompt_path.resolve()),
            name,
            timeout_s=args.timeout_s,
            until=("working", "idle"),
            working_proves=True,
            submission_recorded=lch._rising_submission_record(handle, prompt_path),
        )
        elapsed = time.monotonic() - started

        offers = herdr.count("pane", "send-text") - offered_before
        if offers != 1:
            failures.append("prompt offered {0} times, expected exactly 1".format(offers))
        if not lch.prompt_submission_recorded(handle, prompt_path):
            failures.append(
                "actor transcript never recorded the offered prompt; "
                "pane revision is not proof"
            )
        enters = herdr.count("pane", "send-keys") + herdr.count("agent", "send-keys")
        if enters < 1:
            failures.append("no Enter was pressed; send-text alone is not a submit")
        print(
            json.dumps(
                {
                    "route": args.route,
                    "profile": args.profile,
                    "outcome": "SUBMISSION_OBSERVED" if not failures else "SMOKE_FAILED",
                    "prompt_offers": offers,
                    "transcript": str(transcript),
                    "recorded": lch.prompt_submission_recorded(handle, prompt_path),
                    "enter_presses": herdr.count("agent", "send-keys"),
                    "elapsed_s": round(elapsed, 2),
                    "failures": failures,
                },
                sort_keys=True,
            )
        )
    except BaseException as exc:  # noqa: BLE001 - the smoke reports, never raises
        print(
            json.dumps(
                {
                    "route": args.route,
                    "profile": args.profile,
                    "outcome": "SMOKE_FAILED",
                    "error": "{0}: {1}".format(type(exc).__name__, exc),
                    "prompt_offers": herdr.count("agent", "prompt"),
                    "pane_reads": herdr.count("pane", "get"),
                    "enter_presses": herdr.count("agent", "send-keys"),
                },
                sort_keys=True,
            )
        )
        failures.append(str(exc))
    finally:
        if workspace_id:
            try:
                herdr("workspace", "close", workspace_id, timeout=30.0)
            except Exception:
                print("SMOKE_CLEANUP_FAILED: workspace {0}".format(workspace_id))
        shutil.rmtree(root, ignore_errors=True)
    return 1 if failures else 0


def _payload(payload: Any, key: str) -> Dict[str, Any]:
    extracted = lch._extract(payload, key)
    return extracted if isinstance(extracted, dict) else {}


def _first_pane(herdr: RecordingHerdr, workspace_id: str) -> Optional[str]:
    listed = herdr("pane", "list", timeout=30.0)
    panes = (listed or {}).get("result", {}).get("panes")
    if not isinstance(panes, list):
        return None
    for pane in panes:
        if isinstance(pane, dict) and str(pane.get("workspace_id") or "") == workspace_id:
            return str(pane.get("pane_id") or "") or None
    return None


if __name__ == "__main__":
    raise SystemExit(main())
