"""Grade one diff against one SWE-bench instance, in a container.

`verdict()` decides from test ids; something has to *produce* those ids, and that something
has to run where the instance can be built. This is that step: stage the checkout, apply the
instance's test patch and (optionally) a diff, run the FAIL_TO_PASS test files, and hand the
failed ids to `verdict()`.

Two limits are deliberate and printed rather than hidden:

* only the FAIL_TO_PASS tests are run. PASS_TO_PASS is the regression half of a real
  SWE-bench score and it is 517–989 ids per instance here; running it is a separate decision
  about time, not a detail to slip in.
* the runner must have parsed the output. A pytest run whose ids nobody could read is
  `unknown`, never `resolved` — the same rule `verdict()` already enforces.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

FORGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FORGE))
sys.path.insert(0, str(FORGE / "tools"))

from swebench_env import APT, CFLAGS, PIP  # noqa: E402

from evals.swebench import SwebenchInstance, load_instances, verdict  # noqa: E402

STAGE = Path(".tmp/sbgrade")


def test_files(instance: SwebenchInstance) -> tuple[str, ...]:
    """The files the FAIL_TO_PASS ids live in, in order, without duplicates.

    pytest is given files rather than node ids: an id like `test_x[compound_model6-result6]`
    is a shell-quoting problem in a mounted container, and the file is what the run needs.
    """
    files: list[str] = []
    for node in instance.fail_to_pass:
        path = node.split("::", 1)[0]
        if path not in files:
            files.append(path)
    return tuple(files)


def parse_failed(output: str) -> tuple[str, ...]:
    """Every `FAILED <id>` line pytest printed, in order, without duplicates."""
    ids: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("FAILED "):
            continue
        node = stripped[len("FAILED ") :].split(" ", 1)[0].rstrip(":")
        if node and node not in ids:
            ids.append(node)
    return tuple(ids)


def _to_lf(root: Path) -> None:
    """`patch` applies nothing to a CRLF checkout, and a Windows copy of one is CRLF."""
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in {".py", ".rst", ".txt", ".cfg", ".yaml", ".yml", ".md", ".c", ".h"}:
            continue
        data = path.read_bytes()
        if b"\r\n" in data:
            path.write_bytes(data.replace(b"\r\n", b"\n"))


def script_for(instance: SwebenchInstance, diff: Path | None) -> str:
    """The bash recipe to run where docker is callable. Printed, never executed here."""
    tests = " ".join(test_files(instance))
    candidate = "true"
    if diff is not None:
        candidate = f"patch -p1 --batch --forward < /work/{instance.instance_id}.candidate.patch"
    return f"""set -e
{APT}
{PIP}
cd /work/{instance.instance_id}
export SETUPTOOLS_SCM_PRETEND_VERSION=5.0
export CFLAGS='{CFLAGS}'
patch -p1 --batch --forward < /work/{instance.instance_id}.test.patch
{candidate}
python setup.py build_ext --inplace -q
python -m pytest {tests} -q -p no:cacheprovider --tb=line
"""


def stage(instance: SwebenchInstance, diff: Path | None) -> Path:
    """Copy the checkout next to the patches, in LF, and say where it is."""
    source = Path(".tmp/swebench-repos") / instance.instance_id
    if not source.is_dir():
        raise SystemExit(f"error: no checkout at {source}; fetch and clone it first")
    staged = STAGE / instance.instance_id
    if staged.exists():
        shutil.rmtree(staged)
    STAGE.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, staged, ignore=shutil.ignore_patterns(".git"))
    _to_lf(staged)
    (STAGE / f"{instance.instance_id}.test.patch").write_text(instance.test_patch, encoding="utf-8")
    if diff is not None:
        (STAGE / f"{instance.instance_id}.candidate.patch").write_text(
            diff.read_text(encoding="utf-8"), encoding="utf-8"
        )
    return STAGE


def report_for(instance: SwebenchInstance, output: str, diff: str) -> dict[str, object]:
    """Grade captured pytest output. Pure, so a test can pin every branch."""
    failed = parse_failed(output)
    told = any(word in output for word in ("passed", "failed", "error"))
    return {
        "instance": instance.instance_id,
        "diff": diff,
        "failed": list(failed),
        "fail_to_pass": list(instance.fail_to_pass),
        "verdict": verdict(instance, failed, ran=bool(output.strip()), runner_told_us=told),
        "tail": output.strip().splitlines()[-3:],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("instance", help="e.g. astropy__astropy-12907")
    parser.add_argument("--instances", type=Path, default=Path(".tmp/swebench_lite.jsonl"))
    parser.add_argument("--diff", type=Path, default=None, help="a unified diff to grade")
    parser.add_argument("--gold", action="store_true", help="grade the instance's own gold patch")
    parser.add_argument("--print-script", action="store_true", help="print the recipe and stop")
    parser.add_argument(
        "--from-output",
        type=Path,
        default=None,
        help="grade pytest output captured elsewhere (the container is not run here)",
    )
    args = parser.parse_args(argv)

    instances = {item.instance_id: item for item in load_instances(args.instances, limit=100)}
    instance = instances.get(args.instance)
    if instance is None:
        print(f"error: {args.instance} is not in {args.instances}", file=sys.stderr)
        return 2

    diff = args.diff
    label = diff.name if diff else "none"
    if args.gold:
        diff = STAGE / f"{args.instance}.gold.patch"
        diff.parent.mkdir(parents=True, exist_ok=True)
        diff.write_text(instance.gold_patch, encoding="utf-8")
        label = "gold"

    if args.print_script:
        stage(instance, diff)
        print(f"# staged at {STAGE.resolve()}; mount it at /work")
        print(script_for(instance, diff))
        return 0
    if args.from_output is None:
        print(
            "error: --from-output is required: this box refuses the docker API to child "
            "processes, so the recipe is printed (--print-script) and the output is graded here",
            file=sys.stderr,
        )
        return 2

    output = args.from_output.read_text(encoding="utf-8", errors="replace")
    report = report_for(instance, output, label)
    print(json.dumps(report, indent=2))
    return 0 if report["verdict"] in {"resolved", "unresolved"} else 1


if __name__ == "__main__":  # pragma: no cover - the module is the test surface
    raise SystemExit(main())
