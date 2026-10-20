"""MCP (Model Context Protocol) providers.

`stdio.py` is the only transport: a server is a child process this deployment starts, and
its pipes are the channel. An HTTP transport would be a second provider behind the same
seam, and it is deliberately absent rather than half-written — the allowlist, the timeout
and the "a server's claim is not a permission" rules live in the seam, so a second
transport inherits them instead of re-deciding them.
"""
