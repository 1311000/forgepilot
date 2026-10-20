"""The review endpoint, and the one thing it must not do: reimplement M9.

The browser and the Markdown must not be able to tell different stories about the
same change, so the endpoint is a thin call into ``build_review`` plus the same
``render_json`` the CLI uses. If someone ever adds "just for the UI" logic here,
these tests are what should object.
"""

from __future__ import annotations

import difflib

from forgepilot.runtime.review import render_json


def a_diff() -> str:
    before = "def retry(client):\n    while True:\n        client.login()\n"
    after = "MAX = 3\n\n\ndef retry(client):\n    for _ in range(MAX):\n        client.login()\n"
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="a/src/login.py",
            tofile="b/src/login.py",
        )
    )


async def test_review_returns_the_same_json_the_runtime_renders(api) -> None:
    """Same generator, same shape — no UI-shaped variant of the narrative."""
    from forgepilot.orchestration.modes.review import build_review

    diff = a_diff()
    expected = render_json(await build_review(diff, title="fix: bounded retry", use_model=False))

    response = await api.client.post(
        "/review",
        json={"diff": diff, "title": "fix: bounded retry"},
        headers=api.auth(),
    )

    assert response.status_code == 200
    assert response.json() == expected


async def test_the_review_carries_the_order_and_the_coverage(api) -> None:
    """The two things that make this a review rather than a summary."""
    response = await api.client.post(
        "/review", json={"diff": a_diff(), "title": "t"}, headers=api.auth()
    )

    payload = response.json()
    assert payload["groups"], "the reading order is the product"
    assert all(g["positionReason"] for g in payload["groups"])
    assert "unexamined" in payload["coverage"]


async def test_a_request_with_no_diff_is_refused(api) -> None:
    response = await api.client.post(
        "/review", json={"diff": "   ", "title": "t"}, headers=api.auth()
    )

    assert response.status_code == 400


async def test_review_requires_a_credential(api) -> None:
    response = await api.client.post("/review", json={"diff": a_diff()})

    assert response.status_code == 401
