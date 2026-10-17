"""`lane_gates.py` prints probe columns beside, never inside, the suite columns."""

from __future__ import annotations

from adw_modules import code_review as cr

from test_lane_gates import _rewrite, _run, ledger, lane_gates  # noqa: F401


def _review_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("review[")]


def test_review_without_probes_prints_exactly_as_before(ledger, capsys):
    _code, out = _run(ledger, capsys, "--lane", "lane-build")
    lines = _review_lines(out)
    assert lines
    for line in lines:
        assert "probes=" not in line
        assert line.rstrip().endswith("agree=True") or line.rstrip().endswith(
            "agree=False"
        )


def test_probe_columns_are_separate_from_suite_columns(ledger, capsys):
    _code, before = _run(ledger, capsys, "--lane", "lane-build")
    _rewrite(
        ledger,
        2,
        lambda payload: payload.update(
            probe_records=[{"probe_id": "p1"}, {"probe_id": "p2"}, {"probe_id": "p3"}],
            probe_retired=[{"probe_id": "p3"}],
            probe_status={"p1": "DISCHARGED", "p2": "OPEN"},
        ),
    )
    _code, after = _run(ledger, capsys, "--lane", "lane-build")
    changed = [
        line for line in _review_lines(after) if "probes=" in line
    ]
    assert len(changed) == 1
    line = changed[0]
    assert " probes=active/discharged/open 2/1/1" in line
    suite, _sep, _probe = line.partition(" probes=")
    assert suite in _review_lines(before)  # suite columns unchanged


def test_probe_columns_are_the_shared_helper(ledger, capsys, monkeypatch):
    _rewrite(
        ledger,
        2,
        lambda payload: payload.update(
            probe_records=[{"probe_id": "p1"}], probe_status={"p1": "DISCHARGED"}
        ),
    )
    calls = []
    real = cr.review_progress

    def spy(payload):
        calls.append(payload)
        return real(payload)

    monkeypatch.setattr(lane_gates.cr, "review_progress", spy)
    _code, out = _run(ledger, capsys, "--lane", "lane-build")
    assert calls
    assert "probes=active/discharged/open 1/1/0" in out
