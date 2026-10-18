"""The project has to be installable, and that is checkable without installing it.

A wheel is built in layers: `packages` maps a directory into the archive, and
`force-include` maps files the package rule does not already cover. Mapping a
path that `packages` already contains puts the *same* archive path in the wheel
twice, and current hatchling refuses instead of silently de-duplicating:

    ValueError: A second file is being added to the wheel archive at the same
    path: `forgepilot/bundles/base/patch.yaml`.

Every non-editable install died on it — `pip install .`, and therefore the
container build — while `pip install -e .` kept working, which is why nothing in
the gate noticed: the editable path is the one a development machine uses.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _metadata() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_the_readme_the_metadata_names_exists() -> None:
    """A `readme` that is not there fails the metadata build, not the tests."""
    readme = _metadata()["project"].get("readme")
    assert readme, "the project declares no readme, so the wheel has no description"
    assert (ROOT / readme).is_file(), f"pyproject.toml names {readme}, which is missing"


def test_no_forced_include_repeats_a_path_the_package_rule_already_covers() -> None:
    """The exact defect that made `pip install .` fail while `-e .` passed."""
    targets = _metadata().get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {})
    wheel = targets.get("wheel", {})
    packages = [Path(entry).as_posix() for entry in wheel.get("packages", [])]
    forced = wheel.get("force-include", {})

    assert packages, "the wheel target names no package, so nothing would be installed"

    for source in forced:
        source = Path(source).as_posix()
        for package in packages:
            if source == package or source.startswith(f"{package}/"):
                pytest.fail(
                    f"force-include maps {source!r}, which is already inside the "
                    f"package {package!r}. Hatchling now refuses the duplicate with "
                    f"'A second file is being added to the wheel archive at the same "
                    f"path' — build the wheel to confirm."
                )


def test_the_wheel_target_names_a_directory_that_exists() -> None:
    """The other way metadata builds and the image is still wrong."""
    wheel = (
        _metadata()
        .get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
    )
    for package in wheel.get("packages", []):
        assert (ROOT / package).is_dir(), f"{package} is not a directory in this tree"
