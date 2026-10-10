"""Tests for the rounding helpers."""

from __future__ import annotations

import pytest

from metrics import bucket


def test_bucket_rounds_to_the_nearest_whole_number() -> None:
    assert bucket(2.4) == 2
    assert bucket(2.9) == 3


def test_bucket_rounds_half_away_from_zero() -> None:
    assert bucket(2.5) == 3
    assert bucket(-2.5) == -3
