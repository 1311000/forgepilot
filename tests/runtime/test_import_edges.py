"""P22: what "who imports this" is allowed to mean.

The edge is deliberately cheap: a static ``import``/``require`` naming a module, no name
resolution and no call graph. These tests pin what is read and — as importantly — what is
not: an unknown language yields no edges, and no edges is not evidence that a file imports
nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from forgepilot.runtime.index_update import (
    imports_for,
    plan,
    plan_full,
)


def _targets(source: str, path: str = "pkg/module.py") -> list[str]:
    return [edge.target for edge in imports_for(path, source)]


# -- Python ----------------------------------------------------------------------------------


def test_an_absolute_import_records_the_module_it_names() -> None:
    assert _targets("import os\nimport numpy as np\n") == ["os", "numpy"]


def test_a_from_import_records_the_module_and_not_the_names_it_takes() -> None:
    """`from a.b import c` says the file depends on `a.b`. Whether `c` is a module is a
    question the resolver would have to answer, and this layer does not resolve."""
    assert _targets("from astropy.modeling import separable\n") == ["astropy.modeling"]


def test_a_relative_import_is_written_against_the_package_root() -> None:
    targets = _targets("from .shared import clamp\nfrom ..core import limits\n", "paging/limits.py")
    assert targets == ["paging.shared", "core"]


def test_a_file_that_does_not_parse_yields_no_edges() -> None:
    """A file mid-edit is common; guessing its imports would be worse than saying nothing."""
    assert _targets("def broken(:\n    pass\n") == []


def test_the_same_module_imported_twice_is_one_edge() -> None:
    assert _targets("import json\nimport json\n") == ["json"]


# -- JavaScript / TypeScript and Go -----------------------------------------------------------


def test_es_module_and_commonjs_imports_are_both_edges() -> None:
    source = (
        "import React from 'react';\n"
        "import { clamp } from './util';\n"
        "const side = require('./side');\n"
    )
    assert _targets(source, "web/page.js") == ["react", "./util", "./side"]


def test_the_order_of_edges_follows_the_file_not_the_scanner() -> None:
    """Three regexes scan the file in turn; the answer must still read top to bottom."""
    source = "const a = require('./a');\nimport { b } from './b';\nimport './c';\n"
    assert _targets(source, "web/page.js") == ["./a", "./b", "./c"]


def test_a_go_import_block_and_a_single_import_are_edges() -> None:
    block = 'import (\n\t"fmt"\n\tsvc "github.com/foo/bar"\n)\n'
    single = 'import "os"\n'
    assert _targets(block, "cmd/main.go") == ["fmt", "github.com/foo/bar"]
    assert _targets(single, "cmd/main.go") == ["os"]


def test_a_language_nobody_taught_it_yields_no_edges() -> None:
    """The absence is honest: "unknown language" is not "imports nothing"."""
    assert _targets("use std::fmt;\n", "src/lib.rs") == []


# -- the plan carries the edges --------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    return root


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def test_a_delta_sends_the_edges_of_the_files_it_sends(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "paging").mkdir()
    (root / "paging" / "limits.py").write_text("import math\n", encoding="utf-8")
    base = _commit(root, "first")
    (root / "paging" / "limits.py").write_text("import math\nimport json\n", encoding="utf-8")
    head = _commit(root, "second")

    update = plan(root, repo="git.local/demo/svc", base=base, head=head).update
    assert [document.path for document in update.documents] == ["paging/limits.py"]
    assert sorted(edge.target for edge in update.imports) == ["json", "math"]
    assert all(edge.path == "paging/limits.py" for edge in update.imports)


def test_a_full_snapshot_sends_the_edges_of_the_whole_tree(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "a.py").write_text("import os\n", encoding="utf-8")
    (root / "b.py").write_text("import sys\n", encoding="utf-8")
    _commit(root, "first")

    update = plan_full(root, repo="git.local/demo/svc").update
    assert sorted(edge.target for edge in update.imports) == ["os", "sys"]
    assert update.complete is True
