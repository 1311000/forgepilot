"""`GET /sessions/{id}` reports what the session spent (P8).

The workbench shows this line, and it is a fold of the log rather than a counter
the process holds — so it survives a restart, and it is the same number the next
reader of that log would compute.
"""

from __future__ import annotations


async def test_a_new_session_reports_zero_spend(api, session_id: str) -> None:
    response = await api.client.get(f"/sessions/{session_id}", headers=api.auth())
    assert response.status_code == 200, response.text
    assert response.json()["usage"] == {
        "calls": 0,
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadTokens": 0,
        "tokens": 0,
        "costUsd": 0.0,
    }


async def test_the_detail_still_answers_the_questions_it_answered_before(
    api, session_id: str
) -> None:
    """The usage key was added to a payload people already read; nothing else moved."""
    response = await api.client.get(f"/sessions/{session_id}", headers=api.auth())
    body = response.json()
    assert body["sessionId"] == session_id
    assert body["running"] is False
    assert isinstance(body["events"], int)
    assert body["pendingApprovals"] == []
