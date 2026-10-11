"""Writing text out of a program whose console is not UTF-8.

⚠️ **This exists because the same defect was fixed twice, in one place each time.**
`forgepilot.cli.main` grew a private `_use_utf8_output` after a ticket title arrived
as mojibake; `evals/report.py` then printed an em dash as `��` for weeks and nobody
noticed, until it printed a `⚠️` and died outright:

    UnicodeEncodeError: 'gbk' codec can't encode character '\\u26a0' in position 0

and it died *after* printing the table, so the run had already done its work. The
CLI's own docstring says a fix at one call site is a fix that has not been made; it
then made exactly that mistake about itself.
"""

from __future__ import annotations

import sys


def use_utf8_output() -> None:
    """Write stdout and stderr as UTF-8.

    A ticket title is whatever the reporter typed, and `--task` prints it. When
    stdout is a pipe — a redirect, a CI log, `| tail` — Python encodes it with the
    machine's ANSI code page, so a Chinese title arrives as mojibake and a `⚠` either
    arrives as the literal characters ``\\u26a0`` or raises `UnicodeEncodeError`.

    ``errors="replace"`` rather than strict: output that cannot be encoded should
    arrive with a placeholder, not abort a run that has already done the work.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # a stream someone replaced with a plain writer
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # Already detached, or a closed stream. Not worth failing a command over.
            pass
