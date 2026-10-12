"""Per-project configuration — ``project.yaml``.

⚠️ **This format is an invention, not a reading of the design doc.** The doc calls
for a "fixed build/test/repair workflow" without defining how one is described.
``build``/``test``/``lint`` command lists plus a retry policy is a reasonable
guess, and it is *a* guess — worth confirming against whatever the teams actually
use before it becomes load-bearing.

Two things it deliberately separates:

- **What the project permits** (repos, operations, writable paths, network) —
  intersected with the caller's grant to produce authority.
- **What the project *is*** (name, default branch, build/test commands) —
  context for the model and for the repair loop, carrying no authority at all.

Mixing those would be the same mistake as treating a prompt as a policy.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from forgepilot.config.errors import ConfigError
from forgepilot.config.patch import expand_env
from forgepilot.identity.constraints import ProjectPolicy
from forgepilot.seams.refs import RepoRef
from forgepilot.seams.security import Budget, NetworkAccess, Operation

__all__ = [
    "DEFAULT_TEST_PATHS",
    "RetryPolicy",
    "ProjectConfig",
    "load_project",
    "matches_test_path",
    "project_from_mapping",
]

DEFAULT_FILENAME = "project.yaml"

#: Which changed paths count as "the check's own oracle", when a project does not
#: say. A convention, and the default errs toward pinning *more*: pinning a path
#: that turns out not to be a test makes a check run against older code, which
#: fails loudly; missing one lets a weakened test decide the run, which does not
#: fail at all.
#:
#: A trailing slash names a directory **at any depth**, matched component-wise —
#: `tests/` therefore covers `tests/x.py` and `a/tests/x.py`, and does not cover
#: `mytests/x.py`.
DEFAULT_TEST_PATHS: tuple[str, ...] = (
    "tests/",
    "test/",
    "test_*.py",
    "*_test.py",
    "conftest.py",
)


def matches_test_path(path: str, pattern: str) -> bool:
    """Does ``path`` (a repository-relative path) match one test-path pattern?

    Both sides are normalised to ``/`` because the pattern was written for a
    repository and the path may have arrived from `git` on Windows.
    """
    cleaned = path.replace("\\", "/").strip("/")
    if not cleaned:
        return False

    if pattern.endswith("/"):
        directory = pattern.rstrip("/")
        return directory in cleaned.split("/")[:-1]

    return fnmatch.fnmatch(cleaned.rsplit("/", 1)[-1], pattern) or fnmatch.fnmatch(
        cleaned, pattern
    )


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How many times the repair loop may retry, and what stops it.

    Bounded on purpose: an unbounded repair loop is how a broken build consumes a
    day of tokens.
    """

    max_attempts: int = 2
    #: ``stop`` — give up and report. (Reserved for future strategies.)
    on_failure: str = "stop"


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    name: str
    repos: tuple[RepoRef, ...] = ()
    repo_prefixes: tuple[str, ...] = ()
    default_branch: str = "main"
    build: tuple[str, ...] = ()
    test: tuple[str, ...] = ()
    lint: tuple[str, ...] = ()
    allowed_operations: frozenset[Operation] = frozenset()
    writable_paths: tuple[str, ...] = ()
    #: Programs a `run` call in this project may start. ⚠️ **Absent is not the
    #: same as empty**: ``commands:`` with no value (or no key at all) means the
    #: project states no list, while ``commands: []`` states a list that permits
    #: nothing. Only the second one refuses every command. See
    #: ``ExecutionConstraints.allowed_commands``.
    commands: tuple[str, ...] | None = None
    network: NetworkAccess = NetworkAccess.DENY
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    budget: Budget = field(default_factory=Budget)
    #: Named tool sets the router may choose between, e.g.
    #: ``{"default": [...], "bugfix": [...], "investigate": [...]}``.
    #:
    #: The router resolves a **name**; what that name means lives here, in
    #: configuration. That is what bounds a model's influence over tool
    #: selection to "pick one of the sets someone already approved".
    tool_sets: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: Category (from the classifier) to tool-set name. A category with no entry
    #: falls back to ``default_tool_set`` — never to "everything".
    category_tool_sets: Mapping[str, str] = field(default_factory=dict)
    #: Category to agent-engine key, for the same reason.
    category_engines: Mapping[str, str] = field(default_factory=dict)
    default_tool_set: str = "default"
    default_engine: str = "forgepilot"
    #: Paths that are the check's own oracle. A change here does not decide the
    #: check — see `runtime/pinning.py`. An explicit empty list means "pin
    #: nothing", which is a project saying its tests are somewhere this
    #: convention does not describe.
    test_paths: tuple[str, ...] = DEFAULT_TEST_PATHS
    #: Free-form, for context assembly only. Never read by a decision.
    notes: str = ""

    def tests_in(self, paths: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        """The subset of ``paths`` this project counts as tests, in order."""
        return tuple(
            path
            for path in paths
            if any(matches_test_path(path, pattern) for pattern in self.test_paths)
        )

    def to_policy(self) -> ProjectPolicy:
        """The project's contribution to authority.

        Note what does **not** cross over: build/test commands and notes are
        context, not permission, and a project cannot widen a caller's grant by
        describing itself generously.

        ``commands`` **does** cross over, and it is a different thing from those
        three: ``build``/``test``/``lint`` are lines this project runs itself,
        while ``commands`` is the list of programs a *model's* ``run`` call may
        start. One is context the harness executes; the other is a permission the
        project grants, and only the second belongs on the policy.
        """
        return ProjectPolicy(
            repos=frozenset(self.repos),
            repo_prefixes=frozenset(self.repo_prefixes),
            allowed_operations=self.allowed_operations,
            writable_paths=tuple(self.writable_paths),
            commands=None if self.commands is None else frozenset(self.commands),
            network=self.network,
            budget=self.budget,
        )


def project_from_mapping(raw: Mapping[str, Any], *, name: str = "project") -> ProjectConfig:
    repos: list[RepoRef] = []
    for entry in raw.get("repos") or ():
        if isinstance(entry, str):
            parts = entry.split("/")
            if len(parts) != 3:
                raise ConfigError(
                    f"repo {entry!r} must be 'host/namespace/name'"
                )
            repos.append(RepoRef(host=parts[0], namespace=parts[1], name=parts[2]))
        elif isinstance(entry, Mapping):
            repos.append(
                RepoRef(
                    host=str(entry["host"]),
                    namespace=str(entry["namespace"]),
                    name=str(entry["name"]),
                )
            )
        else:
            raise ConfigError(f"unrecognised repo entry: {entry!r}")

    operations = raw.get("allowed_operations")
    if operations is None:
        # Absent means "nothing is permitted", matching ProjectPolicy's default.
        permitted: frozenset[Operation] = frozenset()
    else:
        try:
            permitted = frozenset(Operation(str(op)) for op in operations)
        except ValueError as exc:
            raise ConfigError(f"unknown operation in {operations!r}: {exc}") from exc

    retry_raw = raw.get("retry") or {}
    budget_raw = raw.get("budget") or {}

    def _string_tuple_map(key: str) -> dict[str, tuple[str, ...]]:
        value = raw.get(key) or {}
        if not isinstance(value, Mapping):
            raise ConfigError(f"{key!r} must be a mapping of name to a list of strings")
        return {
            str(name): (tuple(str(v) for v in items) if not isinstance(items, str) else (items,))
            for name, items in value.items()
        }

    def _string_map(key: str) -> dict[str, str]:
        value = raw.get(key) or {}
        if not isinstance(value, Mapping):
            raise ConfigError(f"{key!r} must be a mapping of strings")
        return {str(k): str(v) for k, v in value.items()}

    def _commands(key: str) -> tuple[str, ...]:
        value = raw.get(key) or ()
        if isinstance(value, str):
            return (value,)
        return tuple(str(v) for v in value)

    def _command_allow_list() -> tuple[str, ...] | None:
        """``commands``: absent is "no list", ``[]`` is "nothing permitted".

        Conflating the two would make ``commands: []`` mean "unrestricted",
        which is the opposite of what someone writing it intends.
        """
        value = raw.get("commands")
        if value is None:
            return None
        if isinstance(value, str):
            return (value,)
        if not isinstance(value, (list, tuple)):
            raise ConfigError(
                f"'commands' must be a list of program names, not {value!r}"
            )
        return tuple(str(v) for v in value)

    return ProjectConfig(
        name=str(raw.get("name", name)),
        repos=tuple(repos),
        repo_prefixes=tuple(str(p) for p in raw.get("repo_prefixes") or ()),
        default_branch=str(raw.get("default_branch", "main")),
        build=_commands("build"),
        test=_commands("test"),
        lint=_commands("lint"),
        allowed_operations=permitted,
        writable_paths=tuple(str(p) for p in raw.get("writable_paths") or ()),
        commands=_command_allow_list(),
        network=NetworkAccess(str(raw.get("network", "deny"))),
        retry=RetryPolicy(
            max_attempts=int(retry_raw.get("max_attempts", 2)),
            on_failure=str(retry_raw.get("on_failure", "stop")),
        ),
        budget=Budget(
            max_tokens=int(budget_raw.get("max_tokens", Budget().max_tokens)),
            max_tool_calls=int(budget_raw.get("max_tool_calls", Budget().max_tool_calls)),
            max_wall_seconds=float(
                budget_raw.get("max_wall_seconds", Budget().max_wall_seconds)
            ),
            max_cost_usd=float(budget_raw.get("max_cost_usd", Budget().max_cost_usd)),
        ),
        tool_sets=_string_tuple_map("tool_sets"),
        category_tool_sets=_string_map("category_tool_sets"),
        category_engines=_string_map("category_engines"),
        default_tool_set=str(raw.get("default_tool_set", "default")),
        default_engine=str(raw.get("default_engine", "forgepilot")),
        # Absent means "use the convention"; `[]` means "pin nothing", which is a
        # different statement and one a project is entitled to make.
        test_paths=(
            DEFAULT_TEST_PATHS
            if raw.get("test_paths") is None
            else tuple(str(p) for p in raw["test_paths"])
        ),
        notes=str(raw.get("notes", "")),
    )


def load_project(path: Path) -> ProjectConfig:
    """Read a ``project.yaml``. A missing file is an error, not an empty project.

    Defaulting to a blank project would silently strip every permission — a
    failure that looks like "the agent refuses to do anything" rather than "the
    config is missing".
    """
    if path.is_dir():
        path = path / DEFAULT_FILENAME
    if not path.is_file():
        raise ConfigError(f"no project configuration at {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{path} must contain a mapping")

    # `${VAR}` / `${VAR:-default}`, the same rule the profile layer uses. It
    # matters here for a different reason than it does there: a profile's plugin
    # config is deployment-wide, while a `test:` command is a *machine's*
    # business — the interpreter that has pytest, the path to a checkout. Those
    # differ per developer, and `project.yaml` is a file teams check in.
    raw = expand_env(raw, where=str(path))

    return project_from_mapping(raw, name=path.parent.name)


def find_project(start: Path | None = None) -> ProjectConfig | None:
    """Locate a ``project.yaml`` at or above ``start``. ``None`` if there is none."""
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / DEFAULT_FILENAME
        if candidate.is_file():
            return load_project(candidate)
    return None
