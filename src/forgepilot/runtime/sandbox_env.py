"""The environment a sandboxed command is given.

Two failure modes bracket this, and the shape below is chosen against both:

**Too little.** Both call sites passed ``env={}``. With no ``PATH`` a bare
``pytest`` cannot be resolved; on Windows a child Python without ``SYSTEMROOT``
cannot import ``_overlapped`` and dies before running anything. An empty
environment is not a safe default, it is one that was never exercised.

**Too much.** ``os.environ`` holds ``FORGEPILOT_LLM_API_KEY``,
``FORGEPILOT_SIGNING_KEY`` and the dev token table. The model can ask for a
command, and the command's output becomes a ``tool/result`` in the session log —
so inheriting the parent environment is a path that prints the LLM credential
into the transcript and shows it to the model.

So this is an **allow-list**: a variable nobody has thought about is *not*
inherited, rather than being inherited until someone remembers to ban it. Adding
a name here is a decision that a model-run command may see it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

__all__ = ["KEEP", "command_env"]

#: Variables a command needs in order to start and behave sensibly. Names only —
#: nothing here is a credential, and nothing here should become one.
KEEP: tuple[str, ...] = (
    # POSIX
    "PATH",
    "HOME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "USER",
    # Windows — SYSTEMROOT in particular is not optional: without it a child
    # Python fails to import its own event-loop modules.
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PROCESSOR_ARCHITECTURE",
)


#: Set on every command, never inherited. See `command_env`.
_FORCED: Mapping[str, str] = {
    "PYTHONDONTWRITEBYTECODE": "1",
}


def command_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment to run a command with.

    ``base`` defaults to the process environment and exists so a caller — or a
    test — can supply one explicitly rather than mutating ``os.environ``.

    ``_FORCED`` is applied **on top**, because these are the harness's decision
    rather than the deployment's. ``PYTHONDONTWRITEBYTECODE`` is there for a
    reason that is easy to miss until it bites: the check command *runs inside
    the tree it is judging*, so a pytest run leaves `tests/__pycache__/*.pyc`
    behind, and `changed_paths` — which asks git what is untracked — reports them
    as the agent's work. Found by a test that pinned `tests/__pycache__/
    test_login.cpython-314-pytest-9.1.1.pyc` as a modified test.

    That is the general shape: **a checker must not modify the thing it is
    checking.** This closes the Python instance of it. A build step that writes
    real artefacts into the checkout is the same problem and is *not* covered —
    git's answer to it is a `.gitignore`, and a repository without one will see
    its build output in its own diff.
    """
    source = os.environ if base is None else base
    env = {name: source[name] for name in KEEP if name in source}
    env.update(_FORCED)
    return env
