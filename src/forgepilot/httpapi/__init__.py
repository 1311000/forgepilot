"""The HTTP surface — the boundary the frontend is written against.

Deliberately framework-neutral for now: nothing here imports a web framework, so
the identity boundary can be built and tested without committing to one. The web
app arrives at M10; what it will mount is :func:`forgepilot.httpapi.deps.authenticate`.

That boundary matters more than the framework choice: it is the **only** place a
credential becomes authority. Everything below receives ``ExecutionConstraints``
as an argument and re-checks it, and no layer beneath this one may widen what was
established here.
"""

from forgepilot.httpapi.deps import RequestContext, authenticate

__all__ = ["RequestContext", "authenticate"]
