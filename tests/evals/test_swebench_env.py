"""The container recipe is a string, so its load-bearing parts can be asserted.

Every flag here was learned by failing without it; a refactor that drops one would turn a
working environment into an error message that looks like a broken instance.
"""

from __future__ import annotations

import sys
from pathlib import Path

FORGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(FORGE / "tools"))

import swebench_env as E  # noqa: E402


def test_the_recipe_carries_every_flag_that_was_needed() -> None:
    script = E.script_for("astropy__astropy-12907", gold=False)
    assert "SETUPTOOLS_SCM_PRETEND_VERSION=5.0" in script, (
        "without a version astropy refuses to import"
    )
    assert "'setuptools<70'" in script, "astropy 5.0 imports setuptools.dep_util"
    assert "-std=gnu17" in script, "gcc 14 defaults to gnu23 and rejects 2022-era C"
    assert "build_ext --inplace" in script, (
        "astropy's conftest refuses a tree without in-place extensions"
    )
    assert "python -m pytest" in script


def test_the_test_patch_comes_before_the_gold_patch() -> None:
    """FAIL_TO_PASS ids come from the test patch; the gold patch must not be able to hide that."""
    script = E.script_for("i", gold=True)
    assert script.index(".test.patch") < script.index(".gold.patch")
    baseline = E.script_for("i", gold=False)
    assert ".gold.patch" not in baseline


def test_a_missing_checkout_is_reported_before_docker_is_called(tmp_path: Path) -> None:
    assert E.run("nope", tmp_path, gold=False, tests=None, timeout=1.0) == 2
