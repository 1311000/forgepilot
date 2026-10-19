"""The identity providers' package.

Only *replacements* live here. The shipped ``principal`` row is
:class:`forgepilot.identity.principal.PrincipalServicePlugin` — a token table —
and this package exists so a deployment can swap that row for a provider that
validates what a real identity provider issues, without touching the seam.
"""
