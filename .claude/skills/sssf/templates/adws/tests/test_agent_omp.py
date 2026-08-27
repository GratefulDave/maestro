"""The omp route's argv and parser, measured against a real child process.

Every test here runs a real executable on disk rather than patching
`subprocess`. The fake omp is a script that records the argv it was handed and
replies with the JSONL shape a real `omp -p --mode json` run produces, captured
from an actual run of omp 17.3.1 on 2026-08-13. That keeps the argv builder and
the stream parser honest about the process boundary they actually cross.

Three properties are pinned:

* the catalog comes from `omp models --json`, not from a `--list-models` flag
  that omp 17.3.1 does not have;
* session identity travels as `--session-dir` plus `-c`, because omp has no
  `--session-id`, and continuing is what rejoining a context window means;
* the model omp REPORTS is checked against the model that was REQUESTED. omp
  17.3.1 answers a request for `openai-codex/gpt-5.6-luna` by running
  `openai-codex/gpt-5.6-terra`, so a route that trusts its own request records a
  model that never ran.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules import agent_pi
from adw_modules.data_types import AgentConfig, PiRequest, PromptEngineering


CATALOG = {
    "models": [
        {"provider": "openai-codex", "id": "gpt-5.6-luna",
         "selector": "openai-codex/gpt-5.6-luna", "contextWindow": 272000},
        {"provider": "openai-codex", "id": "gpt-5.6-terra",
         "selector": "openai-codex/gpt-5.6-terra", "contextWindow": 272000},
        {"provider": "openrouter", "id": "openai/gpt-5.6-luna",
         "selector": "openrouter/openai/gpt-5.6-luna", "contextWindow": 1050000},
        # A profile-selected model and one CONCRETE routing alias of it. omp
        # enumerates `:free` and never `:auto`, which is the asymmetry the
        # suffix rule turns on.
        {"provider": "deepseek", "id": "deepseek-v4-flash",
         "selector": "deepseek/deepseek-v4-flash", "contextWindow": 1000000},
        {"provider": "openrouter", "id": "deepseek/deepseek-v4-flash:free",
         "selector": "openrouter/deepseek/deepseek-v4-flash:free",
         "contextWindow": 164000},
    ]
}

# The script stands in for omp. It answers `models --json` from CATALOG and any
# other invocation with one assistant turn, reporting whichever model
# FAKE_OMP_REPORTS_MODEL names — which is how the substitution is reproduced.
FAKE_OMP = '''#!/usr/bin/env python3
import json, os, sys

argv = sys.argv[1:]
if argv[:1] == ["models"]:
    sys.stdout.write(json.dumps({catalog}))
    sys.exit(0)

record = os.environ.get("FAKE_OMP_ARGV")
if record:
    with open(record, "a") as handle:
        handle.write(json.dumps(argv) + "\\n")

reported = os.environ.get("FAKE_OMP_REPORTS_MODEL", "gpt-5.6-luna")
provider = os.environ.get("FAKE_OMP_REPORTS_PROVIDER", "openai-codex")
message = {{
    "role": "assistant",
    "content": [{{"type": "text", "text": "ok"}}],
    "provider": provider,
    "model": reported,
    "usage": {{"input": 10, "output": 2, "totalTokens": 12,
              "cost": {{"total": 0.001}}}},
    "stopReason": "stop",
}}
# A real run emits an echo turn with no model before the assistant turn.
sys.stdout.write(json.dumps({{"type": "message_end",
                            "message": {{"role": "user", "content": []}}}}) + "\\n")
sys.stdout.write(json.dumps({{"type": "message_end", "message": message}}) + "\\n")
sys.exit(0)
'''


def write_fake_omp(directory: Path) -> Path:
    script = directory / "fake-omp"
    script.write_text(FAKE_OMP.format(catalog=json.dumps(CATALOG)))
    script.chmod(0o755)
    return script


class OmpRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.omp = write_fake_omp(self.root)
        self._env_before = dict(os.environ)
        os.environ["PI_PATH"] = str(self.omp)
        os.environ.pop("FAKE_OMP_REPORTS_MODEL", None)
        os.environ.pop("FAKE_OMP_REPORTS_PROVIDER", None)
        agent_pi.catalog.cache_clear()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_before)
        agent_pi.catalog.cache_clear()
        self._tmp.cleanup()

    # ── catalog ─────────────────────────────────────────────────────────────

    def test_catalog_comes_from_models_json_subcommand(self):
        """omp 17.3.1 has no --list-models; the catalog is `omp models --json`."""
        rows = agent_pi.catalog()
        self.assertIn(("openai-codex", "gpt-5.6-luna", 272000), rows)

    def test_resolve_model_accepts_an_exact_selector(self):
        self.assertEqual(agent_pi.resolve_model("openai-codex/gpt-5.6-luna"),
                         ("openai-codex", "gpt-5.6-luna"))

    def test_an_auto_routing_suffix_resolves_to_the_base_model(self):
        """Recorded failure, run-6357251adc7d41dc9b2a72645f778c9c.

        The omp route launches `omp --profile <name>` and passes NO model --
        the profile picks one, and may append a routing alias:
        `deepseek/deepseek-v4-flash:auto`. omp resolves that at call time; the
        catalog never lists it. Refusing it made B13 report HandoffTooLarge --
        a model whose window cannot be read cannot be shown to fit one -- and
        all seven attempts died at zero turns having launched no agent.

        `:auto` selects a route, not a window, so the base model's window is
        the right ceiling to measure the handoff against.
        """
        self.assertEqual(
            agent_pi.resolve_model("deepseek/deepseek-v4-flash:auto"),
            ("deepseek", "deepseek-v4-flash"),
        )
        self.assertEqual(
            agent_pi.context_window("deepseek", "deepseek-v4-flash"), 1000000
        )

    def test_a_listed_alias_is_never_stripped_to_its_base(self):
        """`:free` IS a catalog entry, with its own much smaller window.

        Stripping every suffix would silently measure a 164K alias against a
        1M ceiling, which is the overflowing-reviewer failure B13 exists to
        prevent -- so the rule strips only what the catalog does not list.
        """
        self.assertEqual(
            agent_pi.resolve_model("deepseek/deepseek-v4-flash:free"),
            ("openrouter", "deepseek/deepseek-v4-flash:free"),
        )

    def test_unknown_model_names_the_omp_subcommand_in_its_error(self):
        with self.assertRaises(ValueError) as caught:
            agent_pi.resolve_model("no-such/model-9")
        self.assertIn("omp models", str(caught.exception))

    def test_context_window_reads_the_omp_catalog(self):
        """The ceiling comes from the same catalog call, not ~/.pi/agent/models.json."""
        os.environ["PI_MODELS_PATH"] = str(self.root / "absent.json")
        self.assertEqual(agent_pi.context_window("openai-codex", "gpt-5.6-luna"),
                         272000)

    # ── argv ────────────────────────────────────────────────────────────────

    def _request(self, session_dir: Path, prompt: str = "hello") -> PiRequest:
        return PiRequest(
            prompt=prompt,
            system_prompt="be brief",
            model="openai-codex/gpt-5.6-luna",
            thinking="low",
            session_id="sssf-abc-scout-1",
            session_dir=str(session_dir),
            raw_output_path=str(self.root / "raw_output.jsonl"),
            cwd=str(self.root),
        )

    def _run_capturing_argv(self, request: PiRequest):
        record = self.root / "argv.jsonl"
        os.environ["FAKE_OMP_ARGV"] = str(record)
        result = agent_pi.run(request)
        lines = record.read_text().splitlines()
        return result, json.loads(lines[-1])

    def test_argv_carries_no_session_id_flag(self):
        """omp 17.3.1 rejects --session-id; passing it would fail every run."""
        _, argv = self._run_capturing_argv(self._request(self.root / "sessions"))
        self.assertNotIn("--session-id", argv)

    def test_argv_carries_the_session_dir(self):
        sessions = self.root / "sessions"
        _, argv = self._run_capturing_argv(self._request(sessions))
        self.assertIn("--session-dir", argv)
        self.assertEqual(argv[argv.index("--session-dir") + 1], str(sessions))

    def test_first_run_in_an_empty_session_dir_does_not_continue(self):
        _, argv = self._run_capturing_argv(self._request(self.root / "sessions"))
        self.assertNotIn("-c", argv)

    def test_a_second_run_continues_the_existing_session(self):
        """Same session dir = same context window, which is what -c buys."""
        sessions = self.root / "sessions"
        self._run_capturing_argv(self._request(sessions))
        # A real omp run writes `<timestamp>_<uuid>.jsonl` into the session
        # directory; the fake one has no reason to, so the file that a second
        # run must notice is planted here in that exact shape.
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / "2026-08-13T00-00-00-000Z_0199.jsonl").write_text("{}\n")
        _, argv = self._run_capturing_argv(self._request(sessions, "again"))
        self.assertIn("-c", argv)

    def test_argv_keeps_the_flags_omp_still_understands(self):
        _, argv = self._run_capturing_argv(self._request(self.root / "sessions"))
        for flag in ("-p", "--mode", "--provider", "--model", "--thinking",
                     "--system-prompt"):
            self.assertIn(flag, argv)

    def test_model_route_keeps_json_as_the_mode_value(self):
        _, argv = self._run_capturing_argv(self._request(self.root / "sessions"))
        self.assertEqual(argv[argv.index("--mode") + 1], "json")

    def test_profile_route_omits_model_selection_and_records_reported_model(self):
        request = self._request(self.root / "sessions")
        request.pm_profile = "grok"
        result, argv = self._run_capturing_argv(request)

        self.assertEqual(argv[argv.index("--profile") + 1], "grok")
        self.assertNotIn("--provider", argv)
        self.assertNotIn("--model", argv)
        self.assertNotIn("--thinking", argv)
        self.assertEqual(result.model_ran, "openai-codex/gpt-5.6-luna")

    # ── binding verification (§9.5) ─────────────────────────────────────────

    def test_a_substituted_model_is_refused(self):
        """omp answering luna with terra must fail loudly, not be recorded as luna."""
        os.environ["FAKE_OMP_REPORTS_MODEL"] = "gpt-5.6-terra"
        with self.assertRaises(agent_pi.ModelBindingError) as caught:
            agent_pi.run(self._request(self.root / "sessions"))
        message = str(caught.exception)
        self.assertIn("gpt-5.6-luna", message)
        self.assertIn("gpt-5.6-terra", message)

    def test_a_substituted_provider_is_refused(self):
        os.environ["FAKE_OMP_REPORTS_PROVIDER"] = "openrouter"
        with self.assertRaises(agent_pi.ModelBindingError):
            agent_pi.run(self._request(self.root / "sessions"))

    def test_the_model_that_ran_is_recorded_when_it_matches(self):
        result = agent_pi.run(self._request(self.root / "sessions"))
        self.assertEqual(result.model_ran, "openai-codex/gpt-5.6-luna")

    def test_the_echo_turn_without_a_model_does_not_trip_the_check(self):
        """A real run's first message_end carries no model; it must be ignored."""
        result = agent_pi.run(self._request(self.root / "sessions"))
        self.assertEqual(result.text, "ok")


class CodingAgentNameTest(unittest.TestCase):
    """`omp` is the runner's real name; `pi` stays valid for stamped repos."""

    def _agent(self, runner: str) -> AgentConfig:
        return AgentConfig(
            name="scout", coding_agent=runner, purpose="recon",
            prompt_engineering=PromptEngineering(system="s.md", user="u.md"),
        )

    def test_omp_is_an_accepted_coding_agent(self):
        self.assertEqual(self._agent("omp").coding_agent, "omp")

    def test_pi_remains_accepted(self):
        """Repos stamped before the rename keep validating."""
        self.assertEqual(self._agent("pi").coding_agent, "pi")

    def test_an_unknown_runner_is_still_refused(self):
        with self.assertRaises(Exception):
            self._agent("cursor")


if __name__ == "__main__":
    unittest.main()
