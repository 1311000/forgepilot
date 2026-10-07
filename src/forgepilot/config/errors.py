"""Configuration-plane errors."""

from __future__ import annotations

from forgepilot.kernel.errors import ForgePilotError


class ConfigError(ForgePilotError):
    """Base class for configuration problems."""


class ProfileNotFound(ConfigError):
    """The requested profile directory does not exist."""


class BundleNotFound(ConfigError):
    """A profile referenced a bundle that cannot be resolved."""


class PatchFormatError(ConfigError):
    """A patch layer is malformed."""


class PluginResolutionError(ConfigError):
    """A patch row referenced a plugin that could not be imported."""


class DuplicateRowId(ConfigError):
    """Two entries in the *same* layer claimed the same id."""
