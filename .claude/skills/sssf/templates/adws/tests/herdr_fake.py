"""In-memory Herdr 0.8.2 (API protocol 20) for launcher tests.

Every reply uses the real CLI's ``{"result": {"type": ..., ...}}`` envelope
and the workspace -> tab -> pane record hierarchy.

Public surface:

``FakeHerdr()``
    Callable as ``launcher._herdr(*argv, env=..., timeout=...)``.
``.calls``
    Every argv tuple, in order.
``.workspaces / .tabs / .panes / .agents``
    Live state keyed by explicit Herdr id.
``.closed_workspaces / .closed_panes``
    Ids closed through pane lifecycle operations.
``.add_workspace(label, cwd, *, tokens=None)``
    Plant an already-open repository workspace. Returns its id.
``.start_agent(name, pane_id, status="idle")`` / ``.set_agent_status(...)``
    Plant or move a live agent record.
``.hooks_before[(group, verb)] / .hooks_after[(group, verb)]``
    Lists of ``callable(argv)`` run before or after a verb executes.
``.crash_after(verb, nth=1)`` / ``.crash_before(verb, nth=1)``
    Raise ``FakeHerdrStopped`` once at the selected command boundary.
``.snapshot()`` / ``.records_unchanged(snapshot, ids)``
    Deep copy of all state; whether named records remain byte-identical.
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


#: The pane width this fake renders composer output at. Narrow on purpose: a
#: session name carries a 64-hex repository fingerprint, so any realistic pane
#: is narrower than the confirmation sentence.
COMPOSER_COLUMNS = 51


def _as_composer_renders(message: str) -> str:
    """`message` broken across lines the way a real composer prints it.

    Two different breaks, both observed in the pane that refused run
    98fa094e's cleanup. The composer word-wraps its own text, putting a real
    newline before a word that will not fit; the terminal then hard-wraps a
    word longer than the pane mid-token. Emitting the sentence on one line --
    which this fake used to do -- agreed with a contiguous substring match and
    so could never fail while that match was wrong.
    """
    lines: List[str] = []
    line = ""
    for word in str(message).split(" "):
        candidate = word if not line else line + " " + word
        if line and len(candidate) > COMPOSER_COLUMNS:
            lines.append(line)
            line = word
        else:
            line = candidate
        while len(line) > COMPOSER_COLUMNS:
            lines.append(line[:COMPOSER_COLUMNS])
            line = line[COMPOSER_COLUMNS:]
    if line:
        lines.append(line)
    return "\n".join(lines)


def _claude_rename_confirmation(session_name: str) -> str:
    """The confirmation a Claude composer prints, verbatim from pane w1EY:p2.

    Written out here rather than derived from
    `lch.session_rename_confirmation`, which is omp's wording. Deriving it made
    this fake agree with whatever production expected, so no test in the suite
    could fail while the comparison was wrong -- and none did, through the
    whole time it was wrong for every Claude pane.
    """
    return "Session renamed to:\n{}".format(session_name)


class FakeHerdr:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.calls: list[tuple[str, ...]] = []
        self.workspaces: Dict[str, dict] = {}
        self.tabs: Dict[str, dict] = {}
        self.panes: Dict[str, dict] = {}
        self.agents: Dict[str, dict] = {}
        self.closed_workspaces: set[str] = set()
        self.closed_panes: set[str] = set()
        self.hooks_before: Dict[Verb, List[Callable[[tuple[str, ...]], None]]] = {}
        self.hooks_after: Dict[Verb, List[Callable[[tuple[str, ...]], None]]] = {}
        self.agent_start_refusal = ""
        self.rename_confirms = True
        self.close_pane_error = ""
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
        cwd: str,
        *,
        tokens: Optional[dict[str, str]] = None,
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
        }
        if tokens:
            record["tokens"] = dict(tokens)
        self.workspaces[workspace_id] = record
        tab = self._new_tab(workspace_id, label)
        record["active_tab_id"] = tab["tab_id"]
        self._new_pane(workspace_id, tab["tab_id"], cwd)
        return record

    def add_workspace(
        self,
        label: str,
        cwd: str | Path,
        *,
        tokens: Optional[dict[str, str]] = None,
    ) -> str:
        """Plant an already-open repository workspace."""
        with self.lock:
            return self._new_workspace(
                label, _resolved(str(cwd)), tokens=tokens
            )["workspace_id"]

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
                was_closed = (
                    key in snapshot["closed_workspaces"]
                    or key in snapshot["closed_panes"]
                )
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
        if verb == ("workspace", "rename"):
            workspace_id = args[2]
            self._require_workspace(workspace_id)
            self.workspaces[workspace_id]["label"] = args[3]
            return {
                "result": {
                    "type": "workspace_renamed",
                    "workspace": self._workspace_info(workspace_id),
                }
            }
        if verb == ("workspace", "report-metadata"):
            self._require_workspace(args[2])
            return self._tag(self.workspaces, args[2], args)
        if verb == ("tab", "create"):
            workspace_id = flag(args, "--workspace") or ""
            self._require_workspace(workspace_id)
            tab = self._new_tab(workspace_id, flag(args, "--label") or "")
            pane = self._new_pane(
                workspace_id, tab["tab_id"], flag(args, "--cwd") or ""
            )
            if "--no-focus" not in args:
                self._focus_pane(pane["pane_id"])
            return {
                "result": {
                    "type": "tab_created",
                    "tab": dict(tab),
                    "root_pane": dict(pane),
                }
            }
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
        if verb == ("tab", "rename"):
            tab_id = args[2]
            if tab_id not in self.tabs:
                self._raise(lch.TAB_NOT_FOUND)
            self.tabs[tab_id]["label"] = " ".join(args[3:])
            return {"result": {"type": "tab_renamed", "tab": dict(self.tabs[tab_id])}}
        if verb == ("tab", "close"):
            self.tabs.pop(args[2], None)
            return {"result": {"type": "ok"}}
        if verb == ("pane", "move"):
            old_id = args[2]
            self._require_pane(old_id)
            tab_id = flag(args, "--tab") or ""
            tab = self.tabs[tab_id]
            old = self.panes.pop(old_id)
            if old["workspace_id"] == tab["workspace_id"]:
                pane_id = old_id
            else:
                pane_id = "{}:{}".format(tab["workspace_id"], self._next("p"))
            moved = dict(
                old, pane_id=pane_id, workspace_id=tab["workspace_id"], tab_id=tab_id
            )
            self.panes[pane_id] = moved
            for agent in self.agents.values():
                if agent["pane_id"] == old_id:
                    agent["pane_id"] = pane_id
            return {
                "result": {"type": "pane_move", "changed": True, "pane": dict(moved)}
            }
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
            if workspace_id:
                self._require_workspace(workspace_id)
            return {
                "result": {
                    "type": "pane_list",
                    "panes": [
                        dict(pane)
                        for pane in self.panes.values()
                        if (
                            not workspace_id or pane.get("workspace_id") == workspace_id
                        )
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
            if (
                key.lower() == "enter"
                and self.rename_confirms
                and self._live_agent_in_pane(pane_id) is not None
            ):
                last = str(pane.get("last_text") or "")
                if last.startswith("/rename "):
                    pane["output"] = "\n".join(
                        _as_composer_renders(line)
                        for line in _claude_rename_confirmation(
                            last[len("/rename ") :]
                        ).split("\n")
                    )
            return {}
        # `pane wait-output` is deliberately not answered here. Nothing in the
        # runtime calls it any more: it matches a literal substring against the
        # snapshot, and the one thing Maestro waited on -- the composer's
        # rename confirmation -- reaches the pane wrapped across lines, so that
        # match could never succeed. An unimplemented verb reaches the
        # `AssertionError` below, which is what a fake owes a caller that has
        # started using a surface no test has agreed on.
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
        if verb == ("agent", "focus"):
            agent = self._agent_info(args[2])
            self._focus_pane(agent["pane_id"])
            return {
                "result": {"type": "agent_info", "agent": self._agent_info(args[2])}
            }
        if verb in (("agent", "get"), ("agent", "wait")):
            return {
                "result": {
                    "type": "agent_info",
                    "agent": self._agent_info(args[2]),
                }
            }
        raise AssertionError(args)

    # ---- records -----------------------------------------------------------

    def _focus_pane(self, pane_id: str) -> None:
        self._require_pane(pane_id)
        pane = self.panes[pane_id]
        workspace_id, tab_id = pane["workspace_id"], pane["tab_id"]
        for workspace in self.workspaces.values():
            workspace["focused"] = workspace["workspace_id"] == workspace_id
        self.workspaces[workspace_id]["active_tab_id"] = tab_id
        for tab in self.tabs.values():
            tab["focused"] = tab["tab_id"] == tab_id
        for candidate in self.panes.values():
            candidate["focused"] = candidate["pane_id"] == pane_id

    def _require_workspace(self, workspace_id: str) -> None:
        if (
            workspace_id not in self.workspaces
            or workspace_id in self.closed_workspaces
        ):
            self._raise(lch.WORKSPACE_NOT_FOUND)

    def _require_pane(self, pane_id: str) -> None:
        if pane_id not in self.panes or pane_id in self.closed_panes:
            self._raise(lch.PANE_NOT_FOUND)

    def _workspace_info(self, workspace_id: str) -> dict:
        record = dict(self.workspaces[workspace_id])
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
            "focused": pane["focused"],
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


    def _pane_split(self, args: tuple[str, ...]) -> dict:
        parent_id = args[2]
        self._require_pane(parent_id)
        parent = self.panes[parent_id]
        pane = self._new_pane(
            str(parent["workspace_id"]),
            str(parent["tab_id"]),
            flag(args, "--cwd") or "",
        )
        if "--no-focus" not in args:
            self._focus_pane(pane["pane_id"])
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


    def _close_workspace_state(self, workspace_id: str) -> None:
        """Simulate an externally closed workspace."""
        self.closed_workspaces.add(workspace_id)
        for pane_id, pane in self.panes.items():
            if pane.get("workspace_id") == workspace_id:
                self.closed_panes.add(pane_id)
