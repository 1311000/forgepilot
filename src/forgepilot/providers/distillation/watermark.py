"""Distillation watermarks, in a JSONL file.

One line per ``set`` — ``{"session": …, "seq": …, "at": …}`` — rather than a
rewritten JSON document, for the same reason the session log is append-only: a
process killed mid-write leaves a torn *last* line instead of a truncated file.
A torn line is skipped on load (it was never a record), and every earlier line
still counts, so the only cost of a crash is re-distilling the stretch after the
last durable watermark. Re-distilling is cheap and idempotent-ish; losing the
watermark would re-distil the whole session, which is the expensive direction.

⚠️ **The file is read once, at boot.** A long-lived process will not see another
process's watermark advance. That is acceptable for the CLI (one process, one
run) and is written down here because the honest failure mode is a second
process re-distilling a session rather than skipping one — a cost, not a
corruption.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.distillation import SERVICE_NAME

__all__ = ["JsonlWatermarkPlugin"]


class JsonlWatermarkPlugin(PluginService):
    """Serves ``distillationWatermarks`` over a JSONL file."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        raw = cfg.get("path")
        if raw:
            self.path = Path(str(raw)).expanduser()
        else:
            home = os.environ.get("FORGEPILOT_HOME")
            self.path = (Path(home) if home else Path.home() / ".forgepilot") / ("watermarks.jsonl")
        # The directory is created on the first `set`, not here: loading a profile
        # must not have filesystem side effects, and a deployment that distils
        # nothing should leave no trace of having booted.
        self._marks: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    session = str(record["session"])
                    seq = int(record["seq"])
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    # A torn tail, or a line this version cannot read. Skipping is
                    # the safe direction: the watermark stays lower than reality,
                    # so the worst case is a repeated distillation.
                    continue
                if seq > self._marks.get(session, 0):
                    self._marks[session] = seq

    # -- WatermarkStore ----------------------------------------------------
    async def get(self, session_id: str) -> int:
        return self._marks.get(str(session_id), 0)

    async def set(self, session_id: str, seq: int) -> None:
        session = str(session_id)
        seq = int(seq)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"session": session, "seq": seq, "at": datetime.now(UTC).isoformat()},
                    ensure_ascii=False,
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        if seq > self._marks.get(session, 0):
            self._marks[session] = seq

    # -- introspection -----------------------------------------------------
    def snapshot(self) -> dict[str, int]:
        """The watermarks as currently known — used by tests and by `--boot` prose."""
        return dict(self._marks)
