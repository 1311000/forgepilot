"""The distillation watermark file: an append-only ledger of how far a session has
been read.

The properties here are the ones a crash can break. A watermark that is lost
re-distils a whole session (expensive, and the reason the file exists); a watermark
that is *too high* skips memories the model was never asked for (silent, and the
reason loading is defensive). So the tests are about the two directions of failure
and about boot leaving no trace.
"""

from __future__ import annotations

import json
from pathlib import Path

from forgepilot.config import resolve_plugin
from forgepilot.kernel import create_root, dispose_fiber

SPEC = "forgepilot.providers.distillation.watermark:JsonlWatermarkPlugin"


async def build(path: Path):
    root = create_root()
    fiber = await root.plugin(resolve_plugin(SPEC), {"path": str(path)})
    return root.distillationWatermarks, fiber


async def test_an_unknown_session_starts_at_zero(tmp_path: Path) -> None:
    store, fiber = await build(tmp_path / "watermarks.jsonl")
    try:
        assert await store.get("s-never-seen") == 0
    finally:
        await dispose_fiber(fiber)


async def test_the_watermark_survives_a_new_process(tmp_path: Path) -> None:
    """The whole point: the next run of the CLI is a different process."""
    path = tmp_path / "watermarks.jsonl"
    first, fiber = await build(path)
    await first.set("s-1", 42)
    await dispose_fiber(fiber)

    second, fiber2 = await build(path)
    try:
        assert await second.get("s-1") == 42
        assert second.snapshot() == {"s-1": 42}
    finally:
        await dispose_fiber(fiber2)


async def test_a_torn_last_line_is_skipped_and_earlier_lines_still_count(tmp_path: Path) -> None:
    """A crash mid-`set` must not lose the watermarks that were already durable.

    The torn line was never a record; skipping it means the worst case is one
    stretch distilled twice, which is a cost. Treating the file as unreadable would
    instead re-distil every session, and reading the torn line as a number would
    skip everything after it.
    """
    path = tmp_path / "watermarks.jsonl"
    store, fiber = await build(path)
    await store.set("s-1", 10)
    await store.set("s-2", 20)
    await dispose_fiber(fiber)

    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"session": "s-3", "seq": 30')  # killed mid-write

    other, fiber2 = await build(path)
    try:
        assert await other.get("s-1") == 10
        assert await other.get("s-2") == 20
        assert await other.get("s-3") == 0
    finally:
        await dispose_fiber(fiber2)


async def test_a_watermark_never_moves_backwards(tmp_path: Path) -> None:
    """`set` is a high-water mark, not an assignment.

    Two distillation passes racing on one session would otherwise let the slower
    one rewind the ledger and pay for the same stretch again — and, worse, a
    rewound watermark makes the *next* pass look like new work.
    """
    store, fiber = await build(tmp_path / "watermarks.jsonl")
    try:
        await store.set("s-1", 50)
        await store.set("s-1", 10)
        assert await store.get("s-1") == 50
    finally:
        await dispose_fiber(fiber)


async def test_loading_a_profile_writes_nothing(tmp_path: Path) -> None:
    """Booting a tree must have no filesystem side effects.

    The file is created on the first `set`. A deployment that distils nothing
    should leave no trace of having loaded the plugin, and `--boot` on a read-only
    checkout must not fail because a watermark directory was missing.
    """
    path = tmp_path / "nested" / "watermarks.jsonl"
    store, fiber = await build(path)
    try:
        assert not path.exists()
        assert not path.parent.exists()
        await store.set("s-1", 7)
        assert path.exists()
    finally:
        await dispose_fiber(fiber)


async def test_the_file_is_one_json_record_per_line(tmp_path: Path) -> None:
    """Append-only, so a reader can recover the history and not just the value."""
    path = tmp_path / "watermarks.jsonl"
    store, fiber = await build(path)
    try:
        await store.set("s-1", 5)
        await store.set("s-1", 9)
    finally:
        await dispose_fiber(fiber)

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [record["seq"] for record in records] == [5, 9]
    assert all(record["session"] == "s-1" for record in records)
    assert all(str(record["at"]) for record in records)


async def test_a_line_this_version_cannot_read_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "watermarks.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                json.dumps({"session": "s-1", "seq": 3}),
                json.dumps({"session": "s-2"}),  # a shape from a newer version
                json.dumps(["not", "a", "record"]),
                json.dumps({"session": "s-3", "seq": "not a number"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    store, fiber = await build(path)
    try:
        assert store.snapshot() == {"s-1": 3}
    finally:
        await dispose_fiber(fiber)
