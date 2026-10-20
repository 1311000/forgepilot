"""SWE-bench-lite, as a dataset this project can *check* rather than replace.

`evals/` measures this project's own mechanisms on eleven fixtures. SWE-bench-lite
is the opposite question — "how does this do on somebody else's bugs" — and the
honest version of that question needs three things this box cannot supply by
itself: the instance list, a checkout per instance at its `base_commit`, and a
runner whose failed-test ids can be read.

So this module is the **adapter**, not a benchmark result:

- it parses and validates the instance list (a malformed or unpinned instance is
  refused here rather than measured, because a spike that pins the wrong revision
  measures nothing);
- it grades a check result against `FAIL_TO_PASS` / `PASS_TO_PASS`, refusing to
  say "resolved" when the runner's output carried no test ids — the ambiguity P7
  documented is fatal to grading, not cosmetic;
- it reports whether each instance's local checkout really is at `base_commit`,
  which is the one precondition a spike can verify before spending anything.

Deliberately absent: downloading the dataset or cloning repositories. Both are
network operations a deployment performs; `tools/swebench_spike.py` names the
dataset and fails honestly when it is not there.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "MalformedInstance",
    "SwebenchInstance",
    "SpikeRow",
    "VERDICTS",
    "load_instances",
    "slug_of",
    "spike",
    "verdict",
]

#: A commit has to be specific enough to pin: seven is git's own minimum.
_SHA = re.compile(r"^[0-9a-f]{7,40}$")

#: `resolved` is the only verdict that means "this instance's fix worked".
#: `unknown` is not a synonym for "resolved": it means the runner told us nothing.
VERDICTS = frozenset({"resolved", "unresolved", "unknown", "not_run"})


class MalformedInstance(ValueError):
    """A record the adapter will not measure, with the field that was wrong."""


@dataclass(frozen=True, slots=True)
class SwebenchInstance:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...] = ()
    test_patch: str = ""
    gold_patch: str = ""

    @property
    def slug(self) -> str:
        """`owner__name`, the directory name a checkout is looked for under."""
        return slug_of(self.repo)


@dataclass(frozen=True, slots=True)
class SpikeRow:
    instance_id: str
    path: Path | None
    head: str = ""
    ok: bool = False
    reason: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "instanceId": self.instance_id,
            "path": str(self.path) if self.path else None,
            "head": self.head,
            "ok": self.ok,
            "reason": self.reason,
        }


def slug_of(repo: str) -> str:
    """`owner/name` → `owner__name`. A slash in a directory name is a bug on
    Windows and confusing everywhere else."""
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        raise MalformedInstance(f"a repository must be 'owner/name', not {repo!r}")
    return f"{owner}__{name}"


def load_instances(path: Path, *, limit: int | None = None) -> tuple[SwebenchInstance, ...]:
    """Read the JSONL the dataset ships, and refuse anything unmeasurable.

    Two shapes are accepted for `FAIL_TO_PASS`, because the published dataset
    stores it as a *string* holding a JSON list while a hand-written fixture
    usually holds a real list. A record with an **empty** `FAIL_TO_PASS` is
    refused: without it there is nothing to prove the fix, and such a row would
    quietly count as a resolved instance.
    """
    if not path.is_file():
        raise FileNotFoundError(
            f"no SWE-bench instance file at {path}. The dataset is published on "
            "HuggingFace as princeton-nlp/SWE-bench_Lite; download instances.jsonl "
            "wherever the network allows and pass its path."
        )

    seen: set[str] = set()
    instances: list[SwebenchInstance] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MalformedInstance(f"line {number} is not JSON: {exc}") from exc
        instance = _one(record, number)
        if instance.instance_id in seen:
            raise MalformedInstance(
                f"line {number}: duplicate instance_id {instance.instance_id!r}"
            )
        seen.add(instance.instance_id)
        instances.append(instance)
        if limit is not None and len(instances) >= limit:
            break
    return tuple(instances)


def _one(record: Any, number: int) -> SwebenchInstance:
    if not isinstance(record, dict):
        raise MalformedInstance(f"line {number}: an instance must be an object")
    for key in ("instance_id", "repo", "base_commit", "problem_statement"):
        value = record.get(key)
        if not isinstance(value, str) or not value.strip():
            raise MalformedInstance(f"line {number}: {key!r} must be a non-empty string")
    base = record["base_commit"].strip()
    if not _SHA.match(base):
        raise MalformedInstance(
            f"line {number}: base_commit {base!r} is not a commit sha — a spike that "
            "cannot pin the revision measures whatever the checkout happens to be"
        )
    fail_to_pass = _tests(record, "FAIL_TO_PASS", number)
    if not fail_to_pass:
        raise MalformedInstance(f"line {number}: FAIL_TO_PASS is empty, so nothing proves the fix")
    return SwebenchInstance(
        instance_id=record["instance_id"],
        repo=record["repo"],
        base_commit=base,
        problem_statement=record["problem_statement"],
        fail_to_pass=fail_to_pass,
        pass_to_pass=_tests(record, "PASS_TO_PASS", number),
        test_patch=str(record.get("test_patch") or ""),
        gold_patch=str(record.get("patch") or ""),
    )


def _tests(record: dict[str, Any], key: str, number: int) -> tuple[str, ...]:
    value = record.get(key, [])
    if isinstance(value, str):
        if not value.strip():
            return ()
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise MalformedInstance(f"line {number}: {key} is not a JSON list: {exc}") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise MalformedInstance(f"line {number}: {key} must be a list of test ids")
    return tuple(item for item in value if item)


def verdict(
    instance: SwebenchInstance,
    failed: Iterable[str],
    *,
    ran: bool = True,
    runner_told_us: bool = True,
) -> str:
    """Grade one check result against the instance's test lists.

    `runner_told_us` is not decoration. P7's `failed_tests()` returns an empty
    tuple for a runner it does not recognise, and an empty tuple is also what a
    fully green pytest run produces — so the two readings are indistinguishable
    from the ids alone. Grading therefore refuses the ambiguous case: a run whose
    output nobody could parse is `unknown`, never `resolved`.
    """
    if not ran:
        return "not_run"
    if not runner_told_us:
        return "unknown"
    broken = set(failed)
    if broken & set(instance.fail_to_pass):
        return "unresolved"
    if broken & set(instance.pass_to_pass):
        return "unresolved"
    return "resolved"


def spike(
    instances: Sequence[SwebenchInstance],
    repos_dir: Path,
    *,
    timeout_s: float = 30.0,
) -> tuple[SpikeRow, ...]:
    """Check that each instance's checkout exists and really is at `base_commit`.

    The three instances the plan asks for are *spent* against a real model, so the
    cheap precondition is verified first: a checkout pointing at another revision
    turns the spike into a measurement of the wrong code.
    """
    rows: list[SpikeRow] = []
    for instance in instances:
        path = repos_dir / instance.slug
        if not path.is_dir():
            rows.append(SpikeRow(instance.instance_id, path, reason="no checkout at that path"))
            continue
        head = _git_head(path, timeout_s)
        if head is None:
            rows.append(SpikeRow(instance.instance_id, path, reason="not a git checkout"))
            continue
        if not head.startswith(instance.base_commit):
            rows.append(
                SpikeRow(
                    instance.instance_id,
                    path,
                    head=head,
                    reason=f"HEAD is {head}, not base_commit {instance.base_commit}",
                )
            )
            continue
        rows.append(SpikeRow(instance.instance_id, path, head=head, ok=True))
    return tuple(rows)


def _git_head(path: Path, timeout_s: float) -> str | None:
    """The commit of *this* directory's checkout, or None.

    `git -C <dir> rev-parse HEAD` walks **up** to the enclosing repository, so a
    plain directory inside one answers with somebody else's HEAD — the same
    defect the code-index builder hit at M19, measured again here by a test that
    expected "not a checkout" and got the enclosing repo's sha. `--show-toplevel`
    is what makes the answer about this directory rather than about its parent.
    """
    toplevel = _git(path, timeout_s, "rev-parse", "--show-toplevel")
    if toplevel is None or Path(toplevel).resolve() != path.resolve():
        return None
    return _git(path, timeout_s, "rev-parse", "HEAD")


def _git(path: Path, timeout_s: float, *args: str) -> str | None:
    try:
        done = subprocess.run(  # noqa: S603 - argv only, no shell
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip() or None
