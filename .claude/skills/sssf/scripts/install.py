#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///
"""/install — stamp the SSSF factory from the skill into the cwd. Idempotent.

Usage:
    uv run <skill>/scripts/install.py [--force] [--adws-only]

Stamps: adws/ (modules + starter ADWs), adws/adw_data/prompt_engineering/
(4 starter agents), adws/adw_sssf_config/sssf.config.yaml, .env.sample,
.gitignore entries. `--adws-only` limits the install to `adws/`.
Existing files are skipped unless --force.
"""

import argparse
import importlib.util
import sys
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def _load_runtime_sync():
    """Load the template's own verified-copy primitive.

    An install is a copy of runtime bytes between checkouts, which is the same
    operation `tools/runtime_sync.py` exists to make provable. Loading it here
    rather than calling `shutil.copy2` directly means there is exactly one
    definition in this repository of what counts as a copy having arrived, and
    one place to change if that definition ever needs to get stricter.
    """
    module_path = TEMPLATES / "adws" / "tools" / "runtime_sync.py"
    spec = importlib.util.spec_from_file_location("runtime_sync", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime_sync = _load_runtime_sync()

GITIGNORE_ENTRIES = [
    "adws/adw_data/sessions/",
    "adws/adw_data/sssf.db*",
    ".env",
    # The ADWs are Python, so importing adw_modules writes bytecode next to it.
    # Chains that end in a commit phase call `git add -A`, so without this a
    # stamped repo commits its own .pyc files — 15 of them showed up in the
    # first repo that was ever installed into from scratch.
    "__pycache__/",
    "*.pyc",
]


def is_junk(child: Path) -> bool:
    """Whether a path under templates/ is tooling residue rather than factory.

    Whatever sits in the template tree is what gets stamped, so anything a
    tool leaves behind while the factory is being worked on is shipped to
    every repository that installs it. That is not hypothetical: an agent
    harness wrote its own state directory into templates/adws/ during
    development and it reached a freshly stamped repo intact.

    The rule is shaped rather than named — every dot-directory and every
    compiled artefact — so a tool nobody has heard of yet is excluded on the
    same terms as the ones that have already done this.
    """
    if child.is_dir():
        return child.name.startswith(".") or child.name == "__pycache__"
    return child.suffix in {".pyc", ".pyo"} or child.name == ".DS_Store"


def stamp(src: Path, dest: Path, force: bool, stamped: list, skipped: list,
          refused: list = None, overwrite_newer: bool = False) -> None:
    """Copy one file or tree into the destination, proving each copy arrived.

    `--force` used to be an unconditional overwrite, which made re-stamping an
    installed repository a way to silently discard work that existed nowhere
    else — the installed `maestro.config.yaml` naming that installation's lane
    vendors is the obvious case, but any locally patched runtime file has the
    same shape. A destination that differs from the template and is *newer*
    than it is now refused by name and listed at the end, unless the operator
    asks for the discard explicitly with `--overwrite-newer`.
    """
    if refused is None:
        refused = []
    if src.is_dir():
        for child in sorted(src.iterdir()):
            if is_junk(child):
                continue
            stamp(child, dest / child.name, force, stamped, skipped,
                  refused, overwrite_newer)
        return
    if dest.exists() and not force:
        skipped.append(str(dest))
        return
    if dest.exists() and not overwrite_newer:
        if (src.read_bytes() != dest.read_bytes()
                and dest.stat().st_mtime_ns > src.stat().st_mtime_ns):
            refused.append(str(dest))
            return
    runtime_sync.copy_verified(src, dest)
    stamped.append(str(dest))


def ensure_gitignore(root: Path, stamped: list) -> None:
    gitignore = root / ".gitignore"
    existing = gitignore.read_text().splitlines() if gitignore.exists() else []
    missing = [e for e in GITIGNORE_ENTRIES if e not in existing]
    if missing:
        with gitignore.open("a") as f:
            f.write("\n# sssf runtime\n" + "\n".join(missing) + "\n")
        stamped.append(f"{gitignore} (+{len(missing)} entries)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    parser.add_argument(
        "--overwrite-newer",
        action="store_true",
        help="with --force, also discard destination files that are newer than "
             "the template's; without it those are refused and listed",
    )
    parser.add_argument(
        "--adws-only",
        action="store_true",
        help="stamp only adws/; leave root files and .gitignore untouched",
    )
    args = parser.parse_args()

    root = Path.cwd()
    stamped, skipped, refused = [], [], []
    newer = args.overwrite_newer

    stamp(TEMPLATES / "adws", root / "adws", args.force, stamped, skipped, refused, newer)
    stamp(TEMPLATES / "prompt_engineering",
          root / "adws" / "adw_data" / "prompt_engineering", args.force, stamped,
          skipped, refused, newer)
    stamp(TEMPLATES / "harness_engineering",
          root / "adws" / "adw_data" / "harness_engineering", args.force, stamped,
          skipped, refused, newer)
    stamp(TEMPLATES / "sssf.config.yaml",
          root / "adws" / "adw_sssf_config" / "sssf.config.yaml",
          args.force, stamped, skipped, refused, newer)
    if not args.adws_only:
        stamp(TEMPLATES / "env.sample", root / ".env.sample", args.force, stamped,
              skipped, refused, newer)
        # The recipes are part of the operating experience, and several cookbooks
        # plus the run banner tell you to use them, so a stamped repo has to have
        # them. Skipped like any other file if the repo already has a justfile.
        stamp(TEMPLATES / "justfile", root / "justfile", args.force, stamped,
              skipped, refused, newer)
        ensure_gitignore(root, stamped)

    print(f"sssf installed into {root}")
    print(f"  stamped: {len(stamped)} file(s)")
    for s in stamped:
        print(f"    + {s}")
    if skipped:
        print(f"  skipped (already exist, use --force to overwrite): {len(skipped)}")
    if refused:
        print(f"  REFUSED (destination is newer than the template): {len(refused)}")
        for r in refused:
            print(f"    ! {r}")
        print("    Reconcile those by hand, or re-run with --overwrite-newer to")
        print("    discard the installed version. They were not written.")
        return 1
    print("\nnext step:")
    if args.adws_only:
        print("  uv run adws/maestro.py workspace --help  # verify Maestro")
    else:
        print("  1. cp .env.sample .env   # then set the key(s) your roster needs")
        print("  2. just demo             # two cheap read-only runs, end to end")
        print("  3. just sessions         # what just happened")
        print("  4. just obs              # the trace UI, needs bun")
        print("\n  no just? the raw form of step 2 is:")
        print("     uv run adws/adw_prompt.py \"say hello\" --agent scout")
    return 0


if __name__ == "__main__":
    sys.exit(main())
