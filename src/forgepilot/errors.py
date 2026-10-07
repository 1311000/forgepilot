"""The shared error base.

Lives at the top level rather than inside ``kernel/`` on purpose. ``seams/`` must
not depend on the plugin kernel — not even transitively — and importing
``forgepilot.kernel.errors`` would execute ``forgepilot.kernel.__init__``, which
imports ``cordis_port``. A seam that could not be imported without the kernel
present would not really be a seam.

``kernel/errors.py`` re-exports this so existing imports keep working.
"""

from __future__ import annotations


class ForgePilotError(Exception):
    """Base class for every error ForgePilot raises deliberately."""


__all__ = ["ForgePilotError"]
