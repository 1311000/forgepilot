"""Build a SWE-bench instance inside a container and run its tests.

This exists because the recipe is not obvious and every part of it was learned by failing
at it (2026-10-21, `astropy__astropy-12907`):

* **No `.git` in the staged copy**, so `setuptools_scm` cannot derive a version and astropy
  refuses to import — `SETUPTOOLS_SCM_PRETEND_VERSION` is the fix, not a git init.
* **`setuptools<70`**: astropy 5.0 imports `setuptools.dep_util`, removed in newer setuptools.
* **`CFLAGS='-std=gnu17 …'`**: the Debian images ship gcc 14, which defaults to `-std=gnu23`
  and refuses 2022-era C initialisers (`fast_sigma_clip.c:44`). Without this the build dies
  before a single test runs.
* **`build_ext --inplace`**, then run pytest *from the source tree*: astropy's conftest
  refuses to run in a tree whose extension modules were not built in place, and `--pyargs`
  cannot reach the tests of a source checkout.
* **The test patch first, the gold patch second**: the FAIL_TO_PASS ids come from the test
  patch, so a baseline measured without it is a baseline of the wrong tests.

The mount is read-write: the patches are applied in the container, on a copy the caller made.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

IMAGE = "python:3.10-slim"
PIP = (
    "pip install -q 'setuptools<70' wheel 'numpy<2' cython 'extension-helpers>=1' "
    "setuptools_scm jinja2 pytest hypothesis pyerfa pyyaml packaging pyparsing"
)
APT = (
    "apt-get update -qq >/dev/null 2>&1 && DEBIAN_FRONTEND=noninteractive "
    "apt-get install -y -qq --no-install-recommends build-essential patch git >/dev/null 2>&1"
)
CFLAGS = "-std=gnu17 -Wno-error=incompatible-pointer-types -Wno-error=int-conversion"


def script_for(instance: str, *, gold: bool, tests: str | None = None) -> str:
    """The shell recipe. Kept as a string so a test can assert the load-bearing flags."""
    patches = [f"patch -p1 --batch --forward < /work/{instance}.test.patch"]
    if gold:
        patches.append(f"patch -p1 --batch --forward < /work/{instance}.gold.patch")
    target = tests or "astropy"
    return f"""set -e
{APT}
{PIP}
cd /work/{instance}
export SETUPTOOLS_SCM_PRETEND_VERSION=5.0
export CFLAGS='{CFLAGS}'
{" ".join(patches)}
python setup.py build_ext --inplace -q
python -m pytest {target} -q -p no:cacheprovider
"""


def run(instance: str, work: Path, *, gold: bool, tests: str | None, timeout: float) -> int:
    if not (work / instance).is_dir():
        print(f"error: {work / instance} is not there; stage the checkout first", file=sys.stderr)
        return 2
    command = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{work}:/work",
        "-w",
        "/work",
        IMAGE,
        "bash",
        "-c",
        script_for(instance, gold=gold, tests=tests),
    ]
    completed = subprocess.run(command, timeout=timeout, check=False)
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("instance", help="e.g. astropy__astropy-12907")
    parser.add_argument("--work", type=Path, default=Path(".tmp/sbwork"))
    parser.add_argument("--gold", action="store_true", help="also apply the instance's gold patch")
    parser.add_argument("--tests", default=None, help="pytest target (default: the whole package)")
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--print-script", action="store_true")
    args = parser.parse_args(argv)

    if args.print_script:
        print(script_for(args.instance, gold=args.gold, tests=args.tests))
        return 0
    return run(args.instance, args.work, gold=args.gold, tests=args.tests, timeout=args.timeout)


if __name__ == "__main__":  # pragma: no cover - the module is the test surface
    raise SystemExit(main())
