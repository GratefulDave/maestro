"""Vendor/model/route recorded at launch and read by `run status` (B15).

The session jsonl `model_change` record has no vendor key. Only a write at
launch, from the launcher's own configuration, can record it (§1.2).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import attempt_identity as ai  # noqa: E402
from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import watchdog as wd  # noqa: E402


SESSION = "/tmp/session.jsonl"
VENDOR = "xai"
MODEL = "grok-4"
ROUTE = "omp"


def make_node(node_id: str = "lane") -> st.PlanNode:
    return st.PlanNode(node_id=node_id, kind=st.NodeKind.CODE, depth=0,
                       needs=(), command=("true",))


def _store(tmp: Path) -> lc.LifecycleStore:
    store = lc.LifecycleStore(tmp / "lifecycle.db")
    store.create_run("run1", "digest", [make_node()])
    return store


def _start(store: lc.LifecycleStore) -> int:
    return store.start_attempt("run1", "lane", base_sha="deadbeef")


def _progress(db: Path) -> dict:
    reader = lc.LifecycleReader.open(db)
    try:
        return maestro._run_progress(
            reader, reader.run("run1"), SimpleNamespace(plan_digests={}))
    finally:
        reader.close()


class LaunchIdentityExtraTests(unittest.TestCase):
    def test_omits_fields_the_launcher_cannot_see(self):
        self.assertEqual(
            ai.launch_identity_extra(vendor=None, model="  ", route=""),
            {})
        self.assertEqual(
            ai.launch_identity_extra(vendor=VENDOR, model=None, route=ROUTE),
            {ai.VENDOR_KEY: VENDOR, ai.ROUTE_KEY: ROUTE})

    def test_empty_string_is_not_recorded(self):
        record = st.AttemptRecord(
            run_id="r", node_id="n", attempt_no=1, base_sha="s",
            extra={ai.VENDOR_KEY: "", ai.MODEL_KEY: "  ", ai.ROUTE_KEY: ROUTE})
        identity = ai.identity_from_record(record)
        self.assertIsNone(identity.vendor)
        self.assertIsNone(identity.model)
        self.assertEqual(identity.route, ROUTE)
        self.assertEqual(ai.display(identity.vendor), ai.NOT_RECORDED)
        self.assertEqual(ai.display(identity.route), ROUTE)


class MaestroLaunchWriteTests(unittest.TestCase):
    def test_launch_extra_keeps_session_path_and_records_identity(self):
        extra = maestro._launch_attempt_extra(
            SESSION, vendor=VENDOR, model=MODEL, route=ROUTE)
        self.assertEqual(extra[wd.SESSION_PATH_KEY], SESSION)
        self.assertEqual(extra[ai.VENDOR_KEY], VENDOR)
        self.assertEqual(extra[ai.MODEL_KEY], MODEL)
        self.assertEqual(extra[ai.ROUTE_KEY], ROUTE)

    def test_mark_launched_keeps_session_path_across_pid_rearm(self):
        """maestro.py writes extra; scheduler on_launch rearms pid only."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(Path(tmp))
            attempt_no = _start(store)
            store.mark_launched(
                "run1", "lane", attempt_no, 11,
                extra=maestro._launch_attempt_extra(
                    SESSION, vendor=VENDOR, model=MODEL, route=ROUTE))
            store.mark_launched("run1", "lane", attempt_no, 11)

            attempt = store.get_attempt("run1", "lane", attempt_no)
            self.assertEqual(attempt.extra[wd.SESSION_PATH_KEY], SESSION)
            identity = ai.identity_from_record(attempt)
            self.assertEqual(identity.vendor, VENDOR)
            self.assertEqual(identity.model, MODEL)
            self.assertEqual(identity.route, ROUTE)
            store.close()


class AttemptIdentityReaderTests(unittest.TestCase):
    def _write(self, store: lc.LifecycleStore, extra=None) -> None:
        attempt_no = _start(store)
        store.mark_launched("run1", "lane", attempt_no, None, extra=extra)

    def test_absent_present_and_partial_through_run_status(self):
        cases = (
            ("absent", None, None, None, None),
            ("present",
             maestro._launch_attempt_extra(
                 SESSION, vendor=VENDOR, model=MODEL, route=ROUTE),
             VENDOR, MODEL, ROUTE),
            ("partial",
             {wd.SESSION_PATH_KEY: SESSION, ai.MODEL_KEY: MODEL},
             None, MODEL, None),
        )
        for name, extra, vendor, model, route in cases:
            with self.subTest(name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    store = _store(root)
                    self._write(store, extra)
                    db = root / "lifecycle.db"
                    store.close()
                    progress = _progress(db)
                    attempt = progress["nodes"][0]["attempts"][0]
                    self.assertEqual(attempt["vendor"], vendor)
                    self.assertEqual(attempt["model"], model)
                    self.assertEqual(attempt["route"], route)
                    if extra is not None:
                        self.assertEqual(attempt["session_path"], SESSION)
                    rendered = maestro._render_progress(progress)
                    self.assertIn(
                        "vendor: {}".format(ai.display(vendor)), rendered)
                    self.assertIn(
                        "model: {}".format(ai.display(model)), rendered)
                    self.assertIn(
                        "route: {}".format(ai.display(route)), rendered)
                    self.assertNotEqual(
                        ai.display(None), VENDOR)
                    json.loads(json.dumps(progress, sort_keys=True))

    def test_preexisting_attempt_reads_as_not_recorded(self):
        record = st.AttemptRecord(
            run_id="r", node_id="n", attempt_no=1, base_sha="s")
        identity = ai.identity_from_record(record)
        self.assertIsNone(identity.vendor)
        self.assertIsNone(identity.model)
        self.assertIsNone(identity.route)
        self.assertEqual(ai.display(identity.vendor), ai.NOT_RECORDED)
        self.assertIsNot(identity.vendor, "")


if __name__ == "__main__":
    unittest.main()
