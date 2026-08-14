"""The Run object: config + adw_id + agent_map + tracer + console, bound once.

`run.phase(PhaseParams(...))` is the ONE phase primitive — a context manager
for all three kinds (engineer, agent, code). Success must be earned: every
phase defaults to fail; only a clean exit flips it (agent phases additionally
require a parsed envelope + green gates, enforced inside ph.call).
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import agents, git_helper
from .console import Console
from .data_types import AgentCall, EnvelopeBase, EventRecord, Phase, PhaseParams
from .utils import ensure_dir, now_iso

# One agent_map.json per run, and under a DAG several nodes write it at once.
# The lock is module-level because the file is shared by every Run in the
# process, so a per-instance lock would guard nothing.
_AGENT_MAP_WRITE = threading.Lock()


class PhaseHandle:
    def __init__(self, run: "Run", phase: Phase):
        self.run = run
        self.phase = phase

    def log(self, **payload) -> None:
        self.run.tracer.event(EventRecord(adw_id=self.run.adw_id,
                                          phase_id=self.phase.phase_id,
                                          type="log", name=self.phase.params.name,
                                          payload=payload))
        self.run.console.note(", ".join(f"{k}: {v}" for k, v in payload.items()))
        if self.phase.params.kind == "engineer" and "input" in payload:
            self.run.tracer.session_request(self.run.adw_id, str(payload["input"]))

    def call(self, call: AgentCall) -> EnvelopeBase:
        if self.phase.params.kind != "agent":
            raise RuntimeError("ph.call() is only valid inside an agent phase")
        return agents.execute(self.run, self.phase, call)


class Run:
    def __init__(self, cfg, adw_id: str, tracer, engineer: str,
                 node_id: str = "", dag_attempt_no: int = 0):
        """Bind one unit of work to its run, and — when there is one — its node.

        `node_id` and `dag_attempt_no` are what make two units of work that
        share an adw_id distinguishable. A DAG scheduler runs several nodes
        against one run at once, and every one of them opens phases, writes
        agent sessions, and resumes coding agents; without a node identity
        those writes are indistinguishable and the later one silently replaces
        the earlier.

        The default is deliberately empty rather than generated. A classic ADW
        is not a DAG node — it is one script opening its phases one at a time
        in one thread — so inventing an identity for it would change the phase
        ids that already exist in installed databases and on operators'
        terminals for no gain. Empty means "no node", and every identity below
        keeps its historical shape when it sees one.
        """
        self.cfg = cfg
        self.adw_id = adw_id
        self.tracer = tracer
        self.console = Console(tracer, adw_id)
        self.engineer = engineer
        self.node_id = node_id
        self.dag_attempt_no = dag_attempt_no
        self.phases: list[Phase] = []
        self.tokens = 0
        self.cost = 0.0
        self._seq = tracer.max_phase_seq(adw_id)   # a joined run continues the sequence
        self.repo_root = git_helper.repo_root()    # where every agent is spawned to work
        self.session_dir = ensure_dir(Path(cfg.defaults.data_dir) / "sessions" / adw_id)
        self.context_handoff_dir = ensure_dir(self.session_dir / "context_handoff")
        self._agent_map_path = self.session_dir / "agent_map.json"
        self.agent_map: dict = (json.loads(self._agent_map_path.read_text())
                                if self._agent_map_path.exists() else {})

    # ── identity (what distinguishes this unit of work from a concurrent one)
    def scoped_key(self, name: str) -> str:
        """Qualify a name with this run's node and attempt, when it has one.

        One function so that the phase id, the agent map key, and anything
        later keyed the same way cannot drift apart. A run with no node hands
        the name straight back, which is what keeps every existing key in an
        installed factory exactly as it was.
        """
        if not self.node_id:
            return name
        return f"{name}@{self.node_id}#{self.dag_attempt_no}"

    # ── agent map (adw_id -> per-agent coding-agent session ids) ────────────
    def save_agent_map(self, agent: str, entry: dict) -> None:
        """Record this node's session for an agent without erasing anyone else's.

        Every node in a run shares one agent_map.json, and the base wrote it
        by serialising the copy it loaded when the Run was built. A node that
        started before a sibling therefore held a map without the sibling's
        entry, and writing its own dropped the sibling's — the widened
        database key would have kept both rows while the file that decides
        whether to resume a session kept one. So the file is re-read under the
        write, this node's own key is the only one it changes, and the
        replacement is atomic: a reader sees the old map or the new one, never
        a half-written one.

        The lock makes that read-modify-write indivisible between threads,
        which is the case a DAG scheduler creates (§7.2 runs nodes on a thread
        pool in one process). Two separate ADW *processes* writing the same
        run's map concurrently are still a race; no ADW does that today, and
        closing it would need file locking rather than a mutex.
        """
        with _AGENT_MAP_WRITE:
            on_disk = (json.loads(self._agent_map_path.read_text())
                       if self._agent_map_path.exists() else {})
            on_disk[self.scoped_key(agent)] = entry
            staged = self._agent_map_path.with_suffix(".json.tmp")
            staged.write_text(json.dumps(on_disk, indent=2))
            os.replace(staged, self._agent_map_path)
            self.agent_map = on_disk

    def agent_map_entry(self, agent: str) -> dict | None:
        """This node's session record for an agent, or None to start fresh.

        Two nodes holding the same roster role must not resume one another's
        coding-agent session — that is one context window with two live agents
        appending to it. Returning None here is the honest answer for a node
        that has not run this agent yet, and a fresh session is the correct
        consequence; §10.4 states the cost as losing session continuity across
        nodes that share a role.
        """
        return self.agent_map.get(self.scoped_key(agent))

    # ── usage (run totals mirror what the tracer accumulates in sqlite) ─────
    def add_usage(self, tokens: int, cost: float) -> None:
        self.tokens += tokens
        self.cost += cost
        self.tracer.session_add_usage(self.adw_id, tokens, cost)

    def phase_id(self, name: str, seq: int) -> str:
        """Name a phase from facts, never from a counter two threads can race.

        `seq` seeds from `max_phase_seq`, so two nodes that open a phase before
        either writes hold the same number, build the same id, and the second
        one's `phase_upsert` updates the first one's row instead of inserting
        its own: a whole node's phase record vanishes from the trace with no
        error anywhere. Node and attempt are facts about the work rather than
        about the order it happened to be recorded in, so an id built from
        them is unique across concurrent nodes and stable across a resume,
        while `seq` goes on doing the one job it is still good for — ordering.

        A run with no node keeps the historical `<adw_id>_<seq>_<name>` form.
        The sequence is safe there because a classic ADW opens its phases one
        at a time in one thread, and it is load-bearing: a joined run chains
        several ADWs under one adw_id and every one of them opens a phase
        called "request", so the sequence is what keeps those rows apart.
        """
        if not self.node_id:
            return f"{self.adw_id}_{seq:02d}_{name}"
        return f"{self.adw_id}_{self.node_id}_a{self.dag_attempt_no}_{name}"

    # ── the phase primitive ─────────────────────────────────────────────────
    @contextmanager
    def phase(self, params: PhaseParams):
        self._seq += 1
        phase = Phase(phase_id=self.phase_id(params.name, self._seq),
                      adw_id=self.adw_id, seq=self._seq, params=params,
                      status="running", started_at=now_iso())
        self.phases.append(phase)
        self.tracer.phase_upsert(phase)
        self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                      type="phase_start", name=params.name,
                                      payload={"kind": params.kind, "owner": params.owner,
                                               "description": params.description}))
        self.console.phase_started(phase)
        clock = time.monotonic()
        try:
            yield PhaseHandle(self, phase)
        except BaseException as error:
            phase.status = "fail"                      # success must be earned
            phase.error = str(error)[:1000]
            phase.ended_at = now_iso()
            self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                          type="error", name=params.name,
                                          payload={"error": phase.error}))
            self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                          type="phase_end", name=params.name,
                                          payload={"status": "fail"}))
            self.tracer.phase_upsert(phase)
            self.tracer.session_finish(self.adw_id, ok=False)
            self.console.phase_ended(phase, time.monotonic() - clock)
            self.console.session_finished(False, self.tokens, self.cost,
                                          self.cfg.observability.db)
            raise
        else:
            phase.status = "success"
            phase.ended_at = now_iso()
            self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                          type="phase_end", name=params.name,
                                          payload={"status": "success"}))
            self.tracer.phase_upsert(phase)
            self.console.phase_ended(phase, time.monotonic() - clock)

    # ── run outcome ─────────────────────────────────────────────────────────
    def finish(self, accepted: bool = True, reason: str = "") -> int:
        """Finalize the run and return its exit code. Call this exactly once.

        Two criteria, not one. Every phase must have passed, AND the ADW's own
        acceptance test must hold. They are different questions on purpose: a
        test phase that ran the suite did its job even when the suite came back
        red, so the PHASE succeeds while the RUN must not.

        This replaces a `succeeded` property that answered only the first
        question — and, being a property with side effects, wrote the session
        status and printed the banner before the caller's `and test.passed` was
        ever evaluated. A run whose suite never passed was recorded green in the
        db, on the terminal, and in the UI while exiting 1. Anyone reading the
        trace saw success; only a CI job checking `$?` saw the truth. One call
        now settles the db, the banner, and the exit code together, so the three
        cannot disagree.
        """
        phases_ok = bool(self.phases) and all(p.status == "success" for p in self.phases)
        ok = phases_ok and accepted
        if phases_ok and not accepted:
            note = reason or "the run's acceptance criterion was not met"
            self.tracer.event(EventRecord(
                adw_id=self.adw_id,
                phase_id=self.phases[-1].phase_id if self.phases else "",
                type="error", name="not_accepted", payload={"reason": note}))
            self.console.note(f"not accepted: {note}")
        self.tracer.session_finish(self.adw_id, ok=ok)
        self.console.session_finished(ok, self.tokens, self.cost, self.cfg.observability.db)
        return 0 if ok else 1
