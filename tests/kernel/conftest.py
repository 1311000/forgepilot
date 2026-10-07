"""Shared fixtures for the kernel conformance suite."""

from __future__ import annotations

from typing import Any, Callable

import pytest
from cordis_port import Context, Service


@pytest.fixture
def root() -> Context:
    """A fresh root context per test.

    The root fiber is created ACTIVE. Note that its ``runtime`` is ``None``,
    which is load-bearing: reads on it take the kernel's unchecked path. See
    ``test_root_context_read_is_unchecked``.
    """
    return Context()


@pytest.fixture
def service_factory() -> Callable[..., type[Service]]:
    """Build a ``Service`` subclass that provides ``name`` and carries ``**attrs``.

    Mirrors how ForgePilot's own providers will be shaped: a ``Service`` subclass
    whose construction *is* its registration.
    """

    def make(name: str, **attrs: Any) -> type[Service]:
        class _Service(Service):
            provide = name

            def __init__(self, ctx: Context, svc_name: str | None = None) -> None:
                super().__init__(ctx, svc_name)
                for key, value in attrs.items():
                    setattr(self, key, value)

        _Service.__name__ = f"Service_{name}"
        _Service.__qualname__ = _Service.__name__
        return _Service

    return make
