#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Run or list tests on the GitHub Actions spot fleet. The agents' test command.

Agents never run pytest or vitest on the operator's machine. This command takes
the agent's checkout as it stands -- its commit plus every uncommitted file --
runs the gate's runner over the named test files on the spot fleet, and prints
the runner's output. Exit status is the runner's.

    uv run --script adws/tools/remote_tests.py --tree . -- tests/test_x.py
    uv run --script adws/tools/remote_tests.py --tree . --collect -- src/a.test.ts

Provisioning is the deployment's own `provision_argv` from the
`maestro.config.yaml` beside this runtime, the same command every measurement
tree is provisioned with.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_RUNTIME_ROOT = Path(__file__).resolve().parent.parent
if str(_RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_ROOT))

from adw_modules import remote_suite as rs  # noqa: E402
from adw_modules import runner_resolution as rr  # noqa: E402
from adw_modules import tests_chain as tc  # noqa: E402


def _provisioning() -> tuple[list[str], float | None]:
    import yaml

    path = _RUNTIME_ROOT / "maestro.config.yaml"
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    loaded = loaded or {}
    argv = [str(item) for item in loaded.get("provision_argv") or ()]
    timeout = loaded.get("provision_timeout_s")
    return argv, None if timeout is None else float(timeout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tree", default=".", help="the checkout to test (default: .)")
    parser.add_argument("--runner", choices=sorted(rr.EXECUTE_ARGS), help="default: from the file names")
    parser.add_argument("--cwd", default=".", help="the gate's working directory inside the tree")
    parser.add_argument("--collect", action="store_true", help="list cases without running them")
    parser.add_argument("--timeout-s", type=float, default=tc.SUITE_TIMEOUT_S)
    parser.add_argument("paths", nargs="+", help="test files, relative to --cwd")
    args = parser.parse_args(argv)
    runners = {tc._file_runner(path) for path in args.paths}
    runner = args.runner or (runners.pop() if len(runners) == 1 else None)
    if runner is None:
        parser.error("the files name no single runner; pass --runner")
    provision_argv, provision_timeout_s = _provisioning()
    try:
        result = rs.call(
            "command",
            Path(args.tree),
            {
                "runner": runner,
                "cwd": args.cwd,
                "paths": list(args.paths),
                "collect": args.collect,
                "timeout_s": args.timeout_s,
            },
            provision_argv=provision_argv,
            provision_timeout_s=provision_timeout_s,
            timeout_s=args.timeout_s,
        )
    except Exception as exc:  # a refusal is this command's answer, printed
        print("{0}: {1}".format(type(exc).__name__, getattr(exc, "detail", "") or exc), file=sys.stderr)
        return 2
    if args.collect:
        print("\n".join(result["ids"]))
        return 0
    print(result.get("output") or "")
    if result.get("timed_out"):
        return 124
    return int(result.get("returncode") or 0)


if __name__ == "__main__":
    sys.exit(main())
