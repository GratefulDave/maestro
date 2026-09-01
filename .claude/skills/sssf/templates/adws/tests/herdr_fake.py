"""In-memory Herdr 0.8.2 (API protocol 20) for launcher tests.

Every reply is shaped like the real CLI's ``{"result": {"type": ..., ...}}``
envelope, with the record shapes from ``herdr api schema --json``:

* ``WorkspaceInfo``: ``workspace_id, number, label, focused, pane_count,
  tab_count, active_tab_id, agent_status``; ``tokens`` is present only once
  tagged. ``worktree`` (a ``WorkspaceWorktreeInfo`` of ``repo_key, repo_name,
  repo_root, checkout_path, is_linked_worktree``) is present ONLY for a Space
  Herdr bound when it created it -- ``workspace create --cwd <repo>`` on a
  repository with no source Space yet, or ``worktree open``. It is absent
  from a Space the operator opened or the session restored, and from a second
  create on an already-sourced repository, and is never backfilled. It is
  therefore NOT a Space's repository binding; ``worktree list`` is, and
  answers for every Space. See ``add_workspace(reports_binding=...)``.
* ``WorktreeInfo``: ``path, branch, is_bare, is_detached, is_prunable,
  is_linked_worktree, label, open_workspace_id`` (``None`` once closed).
* ``TabInfo``: ``tab_id, workspace_id, number, label, focused, pane_count,
  agent_status``.
* ``PaneInfo``: ``pane_id, terminal_id, workspace_id, tab_id, focused,
  agent_status, revision, cwd``; ``label``/``tokens`` absent until set.
* ``AgentInfo``: ``agent_status`` only (there is no ``status`` field and no
  dead value; a gone agent is ``agent_not_found``), ``name, pane_id,
  workspace_id, tab_id, terminal_id, focused, revision, interactive_ready,
  launch_pending``.

Refusals raise ``launcher.HerdrCallError`` with the real ``error.code``:
``workspace_not_found``, ``pane_not_found``, ``agent_not_found``,
``agent_pane_busy`` (pane already hosts a live agent), ``agent_not_ready``
(injected; agent registered as ``blocked``).

Public surface, kept small on purpose (other test modules import it):

``FakeHerdr()``
    Callable as ``launcher._herdr(*argv, env=..., timeout=...)``.
``.calls``
    Every argv tuple, in order.
``.workspaces / .worktrees / .tabs / .panes / .agents``
    Live state keyed by id (``worktrees`` is ``parent_id -> [WorktreeInfo]``).
``.closed_workspaces / .closed_panes``
    Ids closed through the CLI.
``.add_workspace(label, cwd, *, linked=False, repo_root=None, tokens=None)``
    Plant a workspace the launcher did not create (an operator's Space, a
    stale run, a foreign repo). Returns its id.
``.open_child(parent_id, path, label, *, tokens=None)``
    Plant an already-open linked child under ``parent_id`` (untagged unless
    ``tokens`` given). Returns its id.
``.start_agent(name, pane_id, status="idle")`` / ``.set_agent_status(name, status)``
    Plant or move a live agent record.
``.hooks_before[(group, verb)] / .hooks_after[(group, verb)]``
    Lists of ``callable(argv)`` run before/after that verb executes. A hook
    may block on a ``threading.Barrier`` (deterministic races) or raise
    ``FakeHerdrStopped`` (the launcher process died at exactly that point).
``.crash_after(verb, nth=1)`` / ``.crash_before(verb, nth=1)``
    Raise ``FakeHerdrStopped`` after/before the nth call of ``verb``, once.
``.agent_start_refusal``
    A Herdr error code every ``agent start`` refuses with (``agent_not_ready``
    registers the agent as ``blocked`` first, as the real CLI documents).
``.close_pane_error`` / ``.close_workspace_error``
    A Herdr error code ``pane close`` / ``workspace close`` refuses with.
``.cascade_close_children``
    Whether ``workspace close`` on a non-linked Space also closes the linked
    children opened under it. Default True: OBSERVED on herdr 0.8.2 -- closing
    the parent Space removed its linked child from ``workspace list``. The
    launcher is still tested under both.
``.source_space_rule``
    Which non-linked Space Herdr treats as a repository's source, i.e. the
    Space a linked child is grouped under and reported as
    ``WorktreeSourceInfo.source_workspace_id``. Default ``"first-open"``:
    OBSERVED on herdr 0.8.2 -- a repository's source is the first Space
    opened on its primary checkout, and a second ``workspace create --cwd
    <same repo>`` neither displaces it nor receives a binding of its own.
    ``"last-open"`` and ``"requested"`` remain for contrast.
``.source_workspace_nullable``
    Report ``source_workspace_id: null`` in ``worktree list`` (the schema
    allows it); the launcher must then rely on the listing membership. Real
    Herdr omits the key entirely when no Space is open on the source
    checkout, which the fake also does whenever no live Space is bound.
``.snapshot()`` / ``.records_unchanged(snapshot, ids)``
    Deep copy of all state; whether the named ids are byte-identical to it.
``FakeHerdrStopped``
    ``BaseException`` modelling the launcher process dying mid-sequence.
"""

from __future__ import annotations

import copy
import sys
import threading
from pathlib import Path
from typing import Callable, Dict, List, NoReturn, Optional

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

from adw_modules import launcher as lch  # noqa: E402

Verb = tuple[str, str]


class FakeHerdrStopped(BaseException):
    """The launcher process died here; nothing after this call ran."""


def flag(args: tuple[str, ...], name: str) -> str | None:
    if name in args:
        return args[args.index(name) + 1]
    return None


def tokens_from_args(args: tuple[str, ...]) -> dict[str, str]:
    tokens: dict[str, str] = {}
    index = 0
    while index < len(args):
        if args[index] == "--token" and index + 1 < len(args):
            key, _, value = args[index + 1].partition("=")
            tokens[key] = value
            index += 2
            continue
        index += 1
    return tokens


def env_from_args(args: tuple[str, ...]) -> dict[str, str]:
    values: dict[str, str] = {}
    for index, item in enumerate(args):
        if item == "--env" and index + 1 < len(args):
            key, _, value = args[index + 1].partition("=")
            values[key] = value
    return values


def same_path(left: str | Path | None, right: str | Path) -> bool:
    if left is None or left == "":
        return False
    return Path(left).resolve() == Path(right).resolve()


def _resolved(path: str) -> str:
    return str(Path(path).resolve()) if path else ""


class FakeHerdr:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.calls: list[tuple[str, ...]] = []
        self.workspaces: Dict[str, dict] = {}
        self.worktrees: Dict[str, List[dict]] = {}
        self.tabs: Dict[str, dict] = {}
        self.panes: Dict[str, dict] = {}
        self.agents: Dict[str, dict] = {}
        self.closed_workspaces: set[str] = set()
        self.closed_panes: set[str] = set()
        self.hooks_before: Dict[Verb, List[Callable[[tuple[str, ...]], None]]] = {}
        self.hooks_after: Dict[Verb, List[Callable[[tuple[str, ...]], None]]] = {}
        self.agent_start_refusal = ""
        self.rename_confirms = True
        self.wait_output_error = ""
        self.close_workspace_error = ""
        self.close_pane_error = ""
        self.cascade_close_children = True
        self.source_space_rule = "first-open"
        self.source_workspace_nullable = False
        #: cwd -> repo_root for checkouts that are linked worktrees of a repo
        #: (``workspace create --cwd <linked checkout>`` binds as linked).
        self.linked_checkouts: Dict[str, str] = {}
        #: cwds that are not inside any git repository (``worktree: null``).
        self.non_repo_cwds: set[str] = set()
        self._seq = 0
        self._counts: Dict[Verb, int] = {}

    # ---- planting state -------------------------------------------------

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return "{}{}".format(prefix, self._seq)

    def _raise(self, code: str) -> NoReturn:
        raise lch.HerdrCallError("LAUNCH_REFUSED:{}".format(code), code)

    def _new_tab(self, workspace_id: str, label: str) -> dict:
        tab_id = "{}:{}".format(workspace_id, self._next("t"))
        self.tabs[tab_id] = {
            "tab_id": tab_id,
            "workspace_id": workspace_id,
            "number": len(self.tabs) + 1,
            "label": label,
            "focused": False,
            "pane_count": 1,
            "agent_status": "unknown",
        }
        return self.tabs[tab_id]

    def _new_pane(self, workspace_id: str, tab_id: str, cwd: str) -> dict:
        pane_id = "{}:{}".format(workspace_id, self._next("p"))
        self.panes[pane_id] = {
            "pane_id": pane_id,
            "terminal_id": "term-{}".format(pane_id),
            "workspace_id": workspace_id,
            "tab_id": tab_id,
            "focused": False,
            "agent_status": "unknown",
            "revision": 0,
            "cwd": _resolved(cwd) or None,
        }
        return self.panes[pane_id]

    def _new_workspace(
        self,
        label: str,
        worktree: Optional[dict],
        *,
        tokens: Optional[dict[str, str]] = None,
        reports_binding: bool = False,
    ) -> dict:
        workspace_id = self._next("w")
        record = {
            "workspace_id": workspace_id,
            "number": len(self.workspaces) + 1,
            "label": label,
            "focused": False,
            "pane_count": 1,
            "tab_count": 1,
            "active_tab_id": "",
            "agent_status": "unknown",
            "worktree": worktree,
            # Whether this Space's *emitted* WorkspaceInfo carries `worktree`.
            # The fake keeps the binding internally for every Space so
            # `worktree list` can answer, exactly as real Herdr resolves it
            # live; what varies is whether the record reports it.
            "_reports_binding": bool(reports_binding and worktree),
        }
        if tokens:
            record["tokens"] = dict(tokens)
        self.workspaces[workspace_id] = record
        tab = self._new_tab(workspace_id, label)
        record["active_tab_id"] = tab["tab_id"]
        cwd = str(worktree.get("checkout_path") or "") if worktree else ""
        self._new_pane(workspace_id, tab["tab_id"], cwd)
        return record

    def _binding_for_cwd(self, cwd: str) -> Optional[dict]:
        resolved = _resolved(cwd)
        if not resolved or resolved in self.non_repo_cwds:
            return None
        linked_root = self.linked_checkouts.get(resolved)
        repo_root = linked_root or resolved
        return {
            "repo_key": "repo:{}".format(repo_root),
            "repo_name": Path(repo_root).name,
            "repo_root": repo_root,
            "checkout_path": resolved,
            "is_linked_worktree": linked_root is not None,
        }

    def add_workspace(
        self,
        label: str,
        cwd: str | Path,
        *,
        linked: bool = False,
        repo_root: str | Path | None = None,
        tokens: Optional[dict[str, str]] = None,
        reports_binding: bool = False,
    ) -> str:
        """Plant a Space the launcher did not create.

        `reports_binding` defaults to False because that is the shape of every
        Space an operator has open: Herdr fills `WorkspaceInfo.worktree` in
        only for a Space it bound at creation, and never backfills. The Space
        is still the repository's source in `worktree list`.
        """
        with self.lock:
            resolved = _resolved(str(cwd))
            if linked:
                root = _resolved(str(repo_root or Path(resolved).parent))
                self.linked_checkouts[resolved] = root
            binding = self._binding_for_cwd(resolved)
            return self._new_workspace(
                label, binding, tokens=tokens, reports_binding=reports_binding
            )["workspace_id"]

    def open_child(
        self,
        parent_id: str,
        path: str | Path,
        label: str,
        *,
        tokens: Optional[dict[str, str]] = None,
    ) -> str:
        with self.lock:
            record = self._open_linked(parent_id, str(path), label)
            if tokens:
                self.workspaces[record["workspace_id"]]["tokens"] = dict(tokens)
            return record["workspace_id"]

    def start_agent(self, name: str, pane_id: str, status: str = "idle") -> None:
        with self.lock:
            self._register_agent(name, pane_id, status)

    def set_agent_status(self, name: str, status: str) -> None:
        with self.lock:
            agent = self.agents[name]
            agent["agent_status"] = status
            pane_id = str(agent.get("pane_id") or "")
            if pane_id in self.panes:
                self.panes[pane_id]["agent_status"] = status

    def crash_after(self, verb: Verb, nth: int = 1) -> None:
        self.hooks_after.setdefault(verb, []).append(self._crash_on(verb, nth, 0))

    def crash_before(self, verb: Verb, nth: int = 1) -> None:
        self.hooks_before.setdefault(verb, []).append(self._crash_on(verb, nth, 1))

    def _crash_on(
        self, verb: Verb, nth: int, pending: int
    ) -> Callable[[tuple[str, ...]], None]:
        fired = threading.Event()

        def hook(_args: tuple[str, ...]) -> None:
            # One process dies once; the restart that follows runs clean.
            if not fired.is_set() and self._counts.get(verb, 0) + pending == nth:
                fired.set()
                raise FakeHerdrStopped("stopped at {} #{}".format(verb, nth))

        return hook

    def snapshot(self) -> dict:
        with self.lock:
            return copy.deepcopy(
                {
                    "workspaces": self.workspaces,
                    "worktrees": self.worktrees,
                    "tabs": self.tabs,
                    "panes": self.panes,
                    "agents": self.agents,
                    "closed_workspaces": self.closed_workspaces,
                    "closed_panes": self.closed_panes,
                }
            )

    def records_unchanged(self, snapshot: dict, ids: set[str]) -> bool:
        """Whether every workspace/tab/pane/agent among ``ids`` is unchanged."""
        with self.lock:
            for store in ("workspaces", "tabs", "panes", "agents"):
                live = getattr(self, store)
                for key in ids:
                    if key in snapshot[store] or key in live:
                        if snapshot[store].get(key) != live.get(key):
                            return False
            for key in ids:
                was_closed = key in snapshot["closed_workspaces"] or key in snapshot["closed_panes"]
                now_closed = key in self.closed_workspaces or key in self.closed_panes
                if was_closed != now_closed:
                    return False
            return True

    # ---- CLI dispatch ----------------------------------------------------

    def __call__(self, *args: str, **kwargs: object) -> dict:
        del kwargs
        group = args[0] if len(args) >= 1 else ""
        verb: Verb = (group, args[1] if len(args) >= 2 else "")
        # Hooks run outside the state lock so a barrier can hold one caller
        # while another proceeds; the verb itself is atomic under the lock.
        for hook in list(self.hooks_before.get(verb, ())):
            hook(args)
        with self.lock:
            self.calls.append(args)
            self._counts[verb] = self._counts.get(verb, 0) + 1
            reply = self._dispatch(verb, args)
        for hook in list(self.hooks_after.get(verb, ())):
            hook(args)
        return reply

    def _dispatch(self, verb: Verb, args: tuple[str, ...]) -> dict:
        if verb == ("workspace", "create"):
            return self._workspace_create(args)
        if verb == ("workspace", "list"):
            return {
                "result": {
                    "type": "workspace_list",
                    "workspaces": [
                        self._workspace_info(item["workspace_id"])
                        for item in self.workspaces.values()
                        if item["workspace_id"] not in self.closed_workspaces
                    ],
                }
            }
        if verb == ("workspace", "get"):
            workspace_id = args[2]
            self._require_workspace(workspace_id)
            return {
                "result": {
                    "type": "workspace_info",
                    "workspace": self._workspace_info(workspace_id),
                }
            }
        if verb == ("workspace", "close"):
            return self._workspace_close(args[2])
        if verb == ("workspace", "report-metadata"):
            self._require_workspace(args[2])
            return self._tag(self.workspaces, args[2], args)
        if verb == ("worktree", "open"):
            return self._worktree_open(args)
        if verb == ("worktree", "list"):
            return self._worktree_list(
                flag(args, "--workspace") or "", flag(args, "--cwd") or ""
            )
        if verb == ("tab", "list"):
            workspace_id = flag(args, "--workspace") or ""
            self._require_workspace(workspace_id)
            return {
                "result": {
                    "type": "tab_list",
                    "tabs": [
                        dict(tab)
                        for tab in self.tabs.values()
                        if tab.get("workspace_id") == workspace_id
                    ],
                }
            }
        if verb == ("tab", "close"):
            self.tabs.pop(args[2], None)
            return {"result": {"type": "ok"}}
        if verb == ("pane", "split"):
            return self._pane_split(args)
        if verb == ("pane", "rename"):
            pane_id, label = args[2], args[3]
            self._require_pane(pane_id)
            self.panes[pane_id]["label"] = label
            return {"result": {"type": "ok"}}
        if verb == ("pane", "get"):
            self._require_pane(args[2])
            return {"result": {"type": "pane_info", "pane": dict(self.panes[args[2]])}}
        if verb == ("pane", "list"):
            workspace_id = flag(args, "--workspace") or ""
            self._require_workspace(workspace_id)
            return {
                "result": {
                    "type": "pane_list",
                    "panes": [
                        dict(pane)
                        for pane in self.panes.values()
                        if pane.get("workspace_id") == workspace_id
                        and pane["pane_id"] not in self.closed_panes
                    ],
                }
            }
        if verb == ("pane", "close"):
            self._require_pane(args[2])
            if self.close_pane_error:
                self._raise(self.close_pane_error)
            self.closed_panes.add(args[2])
            workspace_id = str(self.panes[args[2]]["workspace_id"])
            if not any(
                pane["workspace_id"] == workspace_id and pid not in self.closed_panes
                for pid, pane in self.panes.items()
            ):
                # Closing the last pane closes its workspace, as in Herdr.
                self.closed_workspaces.add(workspace_id)
            return {"result": {"type": "ok"}}
        if verb == ("pane", "report-metadata"):
            self._require_pane(args[2])
            return self._tag(self.panes, args[2], args)
        if verb == ("pane", "read"):
            self._require_pane(args[2])
            return {
                "result": {
                    "type": "pane_text",
                    "text": str(self.panes[args[2]].get("output") or ""),
                }
            }
        if verb == ("pane", "send-text"):
            pane_id, text = args[2], args[3]
            self._require_pane(pane_id)
            pane = self.panes[pane_id]
            pane["output"] = str(pane.get("output") or "") + text
            pane["last_text"] = text
            pane["revision"] = int(pane.get("revision") or 0) + 1
            return {}
        if verb == ("pane", "send-keys"):
            pane_id, key = args[2], args[3]
            self._require_pane(pane_id)
            pane = self.panes[pane_id]
            pane["keys"] = list(pane.get("keys") or []) + [key]
            if key.lower() == "enter" and self.rename_confirms:
                last = str(pane.get("last_text") or "")
                if last.startswith("/rename "):
                    pane["output"] = lch.session_rename_confirmation(
                        last[len("/rename ") :]
                    )
            return {}
        if verb == ("pane", "wait-output"):
            if self.wait_output_error:
                self._raise(self.wait_output_error)
            self._require_pane(args[2])
            needle = flag(args, "--match") or ""
            text = str(self.panes[args[2]].get("output") or "")
            if needle and needle not in text:
                self._raise("wait_output_timeout")
            return {"result": {"type": "pane_text", "text": text}}
        if verb == ("pane", "process-info"):
            pane_id = flag(args, "--pane") or ""
            self._require_pane(pane_id)
            return {
                "result": {
                    "type": "pane_process_info",
                    "process_info": {
                        "pane_id": pane_id,
                        "shell_pid": 11,
                        "tty": "/dev/ttys011",
                        "foreground_process_group_id": 11,
                        "foreground_processes": [{"pid": 11, "name": "zsh"}],
                    },
                }
            }
        if verb == ("agent", "start"):
            return self._agent_start(args)
        if verb in (("agent", "get"), ("agent", "wait")):
            return {
                "result": {
                    "type": "agent_info",
                    "agent": self._agent_info(args[2]),
                }
            }
        raise AssertionError(args)

    # ---- records -----------------------------------------------------------

    def _require_workspace(self, workspace_id: str) -> None:
        if workspace_id not in self.workspaces or workspace_id in self.closed_workspaces:
            self._raise(lch.WORKSPACE_NOT_FOUND)

    def _require_pane(self, pane_id: str) -> None:
        if pane_id not in self.panes or pane_id in self.closed_panes:
            self._raise(lch.PANE_NOT_FOUND)

    def _workspace_info(self, workspace_id: str) -> dict:
        record = dict(self.workspaces[workspace_id])
        # Herdr 0.8.2 emits `WorkspaceInfo.worktree` only for a Space it bound
        # when it created it -- `workspace create --cwd <repo>` on a
        # repository with no source Space yet, or `worktree open`. A Space the
        # operator opened, one restored with the session, and a second Space
        # created on an already-sourced repository all report no binding, even
        # while `worktree list` names one of them the repository's source.
        # Herdr never backfills the field, so it is not the binding and
        # nothing may read it as one.
        if not record.pop("_reports_binding", False):
            record.pop("worktree", None)
        record["pane_count"] = sum(
            1
            for pane in self.panes.values()
            if pane["workspace_id"] == workspace_id
            and pane["pane_id"] not in self.closed_panes
        )
        record["tab_count"] = sum(
            1 for tab in self.tabs.values() if tab["workspace_id"] == workspace_id
        )
        return record

    def _agent_info(self, name: str) -> dict:
        agent = self.agents.get(name)
        if agent is None:
            self._raise(lch.AGENT_NOT_FOUND)
        pane_id = str(agent.get("pane_id") or "")
        if pane_id in self.closed_panes or pane_id not in self.panes:
            self._raise(lch.AGENT_NOT_FOUND)
        pane = self.panes[pane_id]
        info = {
            "agent_status": agent["agent_status"],
            "name": agent.get("name"),
            "pane_id": pane_id,
            "workspace_id": pane["workspace_id"],
            "tab_id": pane["tab_id"],
            "terminal_id": pane["terminal_id"],
            "focused": False,
            "revision": int(pane.get("revision") or 0),
            "interactive_ready": agent["agent_status"] in ("idle", "done"),
            "launch_pending": False,
            "cwd": pane.get("cwd"),
        }
        if agent.get("agent_session") is not None:
            info["agent_session"] = agent["agent_session"]
        return info

    def _register_agent(self, name: str, pane_id: str, status: str) -> dict:
        self.agents[name] = {
            "name": name,
            "pane_id": pane_id,
            "agent_status": status,
        }
        if pane_id in self.panes:
            self.panes[pane_id]["agent_status"] = status
        return self.agents[name]

    def _live_agent_in_pane(self, pane_id: str) -> Optional[str]:
        for name, agent in self.agents.items():
            if str(agent.get("pane_id") or "") == pane_id:
                return name
        return None

    def _tag(self, store: Dict[str, dict], item_id: str, args: tuple[str, ...]) -> dict:
        tokens = dict(store[item_id].get("tokens") or {})
        tokens.update(tokens_from_args(args))
        store[item_id]["tokens"] = tokens
        return {"result": {"type": "ok"}}

    def _workspace_create(self, args: tuple[str, ...]) -> dict:
        cwd = flag(args, "--cwd") or ""
        label = flag(args, "--label") or ""
        binding = self._binding_for_cwd(cwd)
        # A repository has one source Space: the first one opened on it. A
        # create that follows is handed no binding of its own (observed:
        # `workspace create --cwd <repo>` twice, second record has no
        # `worktree`), and does not displace the source.
        repo_root = str(binding.get("repo_root") or "") if binding else ""
        already_sourced = bool(repo_root) and bool(
            self._source_space_for("", repo_root)
        )
        record = self._new_workspace(
            label, binding, reports_binding=not already_sourced
        )
        workspace_id = record["workspace_id"]
        tab = self.tabs[record["active_tab_id"]]
        root = next(
            pane for pane in self.panes.values() if pane["workspace_id"] == workspace_id
        )
        return {
            "result": {
                "type": "workspace_created",
                "workspace": self._workspace_info(workspace_id),
                "tab": dict(tab),
                "root_pane": dict(root),
            }
        }

    def _repo_root_of(self, workspace_id: str, path: str) -> str:
        binding = self.workspaces[workspace_id].get("worktree")
        if isinstance(binding, dict) and binding.get("repo_root"):
            return str(binding["repo_root"])
        resolved = _resolved(path)
        return self.linked_checkouts.get(resolved) or resolved

    def _source_space_for(self, requested: str, repo_root: str) -> str:
        """The Space Herdr reports as `repo_root`'s source, or ``""``.

        Empty when no live Space is open on the repository's primary
        checkout: real `worktree list` then omits `source_workspace_id`
        entirely rather than naming the Space that asked.
        """
        if not repo_root:
            return requested
        bound = [
            wid
            for wid, rec in self.workspaces.items()
            if wid not in self.closed_workspaces
            and isinstance(rec.get("worktree"), dict)
            and rec["worktree"].get("is_linked_worktree") is False
            and _resolved(str(rec["worktree"].get("repo_root") or ""))
            == _resolved(repo_root)
        ]
        if self.source_space_rule == "requested" and requested:
            return requested
        if not bound:
            # No live Space is open on the primary checkout. Herdr reports no
            # source at all; it does not name the Space that asked.
            return ""
        return bound[-1] if self.source_space_rule == "last-open" else bound[0]

    def _open_linked(self, parent_id: str, path: str, label: str) -> dict:
        # Real Herdr does not refuse an unbound or linked `--workspace`; the
        # child is grouped under whichever Space it resolves as the repo's
        # source (see `source_space_rule` in the module docstring).
        repo_root = self._repo_root_of(parent_id, path)
        resolved = _resolved(path)
        self.linked_checkouts[resolved] = repo_root
        # Herdr binds a Space it opens on a worktree and reports it: observed
        # `worktree open` -> `workspace.worktree.is_linked_worktree: true`.
        child = self._new_workspace(
            label, self._binding_for_cwd(resolved), reports_binding=True
        )
        source_id = self._source_space_for(parent_id, repo_root)
        self.worktrees.setdefault(source_id, []).append(
            {
                "path": resolved,
                "branch": "maestro/{}".format(label),
                "is_bare": False,
                "is_detached": False,
                "is_prunable": False,
                "is_linked_worktree": True,
                "label": label,
                "open_workspace_id": child["workspace_id"],
            }
        )
        return child

    def _worktree_open(self, args: tuple[str, ...]) -> dict:
        parent_id = flag(args, "--workspace") or ""
        path = flag(args, "--path") or ""
        label = flag(args, "--label") or ""
        self._require_workspace(parent_id)
        resolved = _resolved(path)
        already_open = False
        child: Optional[dict] = None
        for record in self.workspaces.values():
            binding = record.get("worktree")
            if (
                isinstance(binding, dict)
                and binding.get("is_linked_worktree")
                and binding.get("checkout_path") == resolved
                and record["workspace_id"] not in self.closed_workspaces
            ):
                child = record
                already_open = True
                break
        if child is None:
            child = self._open_linked(parent_id, path, label)
        child_id = child["workspace_id"]
        tab = self.tabs[child["active_tab_id"]]
        root = next(
            pane
            for pane in self.panes.values()
            if pane["workspace_id"] == child_id and pane["pane_id"] not in self.closed_panes
        )
        worktree = next(
            (
                item
                for items in self.worktrees.values()
                for item in items
                if item.get("open_workspace_id") == child_id
            ),
            {"path": resolved, "is_linked_worktree": True, "label": label},
        )
        return {
            "result": {
                "type": "worktree_opened",
                "already_open": already_open,
                "workspace": self._workspace_info(child_id),
                "tab": dict(tab),
                "root_pane": dict(root),
                "worktree": dict(worktree),
            }
        }

    def _worktree_list(self, parent_id: str = "", cwd: str = "") -> dict:
        """`worktree list` for a Space (`--workspace`) or a path (`--cwd`).

        Both answer about the *repository*: `source` describes the primary
        checkout and names the Space open on it, and `worktrees` is the whole
        set. A `--cwd` inside a linked checkout resolves to the same primary,
        and a path outside any work tree is `not_git_worktree`.
        """
        if parent_id:
            self._require_workspace(parent_id)
            binding = self.workspaces[parent_id].get("worktree")
        else:
            binding = self._binding_for_cwd(cwd)
        if not isinstance(binding, dict):
            self._raise(lch.NOT_GIT_WORKTREE)
        repo_root = str(binding.get("repo_root") or "")
        if not repo_root:
            self._raise(lch.NOT_GIT_WORKTREE)
        source_id = self._source_space_for(parent_id, repo_root)
        source: dict = {
            "repo_key": "repo:{}".format(repo_root),
            "repo_name": Path(repo_root).name,
            "repo_root": repo_root,
            "source_checkout_path": repo_root,
        }
        if self.source_workspace_nullable:
            source["source_workspace_id"] = None
        elif source_id:
            source["source_workspace_id"] = source_id
        # The worktrees of a repository are a repository-level fact: any
        # Space bound to the repo lists them all, including those opened
        # under a Space that has since closed.
        keys = [source_id] if not repo_root else [
            wid
            for wid, rec in self.workspaces.items()
            if isinstance(rec.get("worktree"), dict)
            and _resolved(str(rec["worktree"].get("repo_root") or ""))
            == _resolved(repo_root)
        ]
        if source_id not in keys:
            keys.append(source_id)
        # The primary checkout is a worktree like any other, and is the entry
        # that reports which Space is open on the source.
        worktrees = [
            {
                "path": _resolved(repo_root),
                "branch": "main",
                "is_bare": False,
                "is_detached": False,
                "is_prunable": False,
                "is_linked_worktree": False,
                "label": Path(repo_root).name,
                "open_workspace_id": source_id or None,
            }
        ]
        for key in keys:
            for item in self.worktrees.get(key, ()):
                entry = dict(item)
                if entry.get("open_workspace_id") in self.closed_workspaces:
                    entry["open_workspace_id"] = None
                worktrees.append(entry)
        return {
            "result": {
                "type": "worktree_list",
                "source": source,
                "worktrees": worktrees,
            }
        }

    def _pane_split(self, args: tuple[str, ...]) -> dict:
        parent_id = args[2]
        self._require_pane(parent_id)
        parent = self.panes[parent_id]
        pane = self._new_pane(
            str(parent["workspace_id"]), str(parent["tab_id"]), flag(args, "--cwd") or ""
        )
        return {"result": {"type": "pane_info", "pane": dict(pane)}}

    def _agent_start(self, args: tuple[str, ...]) -> dict:
        name = args[2]
        pane_id = flag(args, "--pane") or ""
        self._require_pane(pane_id)
        if self._live_agent_in_pane(pane_id) is not None:
            self._raise("agent_pane_busy")
        if self.agent_start_refusal:
            if self.agent_start_refusal == lch.AGENT_NOT_READY:
                self._register_agent(name, pane_id, "blocked")
            self._raise(self.agent_start_refusal)
        self._register_agent(name, pane_id, "idle")
        return {
            "result": {
                "type": "agent_started",
                "agent": self._agent_info(name),
                "argv": list(args[args.index("--") + 1 :]) if "--" in args else [],
            }
        }

    def _workspace_close(self, workspace_id: str) -> dict:
        if self.close_workspace_error:
            self._raise(self.close_workspace_error)
        self._require_workspace(workspace_id)
        self._close_workspace_state(workspace_id)
        if self.cascade_close_children:
            for item in self.worktrees.get(workspace_id, ()):
                child_id = str(item.get("open_workspace_id") or "")
                if child_id and child_id not in self.closed_workspaces:
                    self._close_workspace_state(child_id)
        return {"result": {"type": "ok"}}

    def _close_workspace_state(self, workspace_id: str) -> None:
        self.closed_workspaces.add(workspace_id)
        for pane_id, pane in list(self.panes.items()):
            if pane.get("workspace_id") == workspace_id:
                self.closed_panes.add(pane_id)
