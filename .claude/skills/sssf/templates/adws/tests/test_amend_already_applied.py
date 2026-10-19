"""`run amend` of a plan whose digest is already a revision exits 0 and says so."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro
from adw_modules import scheduler_types as st
from adw_modules.lifecycle import AmendmentAlreadyApplied


class AmendAlreadyAppliedTest(unittest.TestCase):
    def test_the_verb_reports_current_status_and_exits_zero(self) -> None:
        store = mock.Mock()
        row = {"plan_revision": 2, "target_repository_root": "/r", "target_main_ref": "m"}
        bound = ({}, mock.Mock(), store, row, object(), object())
        scheduler = mock.Mock()
        scheduler.status.return_value = st.RunStatus.WAITING
        with (
            mock.patch.object(maestro, "_bind_existing_run", return_value=bound),
            mock.patch.object(maestro, "_compile_plan", return_value=object()),
            mock.patch.object(
                maestro,
                "apply_factory_amendment",
                side_effect=AmendmentAlreadyApplied("plan digest d is already revision 2"),
            ),
            mock.patch.object(maestro.gitpub, "revalidate_binding"),
            mock.patch.object(maestro, "_actor_for", return_value=object()),
            mock.patch.object(maestro, "_max_probe_rounds", return_value=0),
            mock.patch.object(maestro, "FactoryScheduler", return_value=scheduler),
            mock.patch.object(maestro, "_pin_plan_artifact") as pin,
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = maestro._run_amend(argparse.Namespace(run_id="r1", plan="/p"))
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["outcome"], "AMENDMENT_ALREADY_APPLIED")
        self.assertEqual(payload["status"], st.RunStatus.WAITING.value)
        pin.assert_not_called()
        scheduler.run.assert_not_called()
