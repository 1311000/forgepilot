"""GitHub as a **repo host** — service ``vcs``.

This is the provider that turns ``TaskState.READY`` into ``TaskState.DELIVERED``.
Until it existed, `LocalRepositoryVcsPlugin` declared no `push` and no
`pull_requests`, so every task ended with a draft and a sentence explaining that
nothing had been published. That was honest, and it was the whole of what this
system could do.

⚠️ **Vocabulary.** The *host* is the machine a command runs on. The service that
owns repositories and pull requests is the **repo host**, and this is one.

Two halves, and they are built differently on purpose:

- **Git speaks for itself.** `clone`, `fetch` and `push` are git, and git reaches
  a repository over any URL including `file://`. Only `remote_url` knows it is
  GitHub, which is why the tests can run real clones and real pushes against a
  bare repository on disk instead of against a stand-in for git.
- **The pull request is an HTTP call**, and that half is one thin method behind
  an injectable transport, with the body and the error parsing as pure functions.
  Same split as `providers/llm/openai_compat.py`, for the same reason: the part
  that is easy to get wrong should be testable without a network.

**The token is never in a URL, and this is load-bearing.** The obvious design
puts it in the remote — `https://x-access-token:…@github.com/o/r.git` — and it
has two costs that are easy to miss. argv is visible to `ps`, and, worse, git
records the URL it cloned from in the clone's own config, so the mirror would be
a **credential at rest** under `$FORGEPILOT_HOME` long after the run that made
it. Instead the URL stays ordinary and the credential arrives through
`url.<base>.insteadOf` in the **environment** (`GIT_CONFIG_COUNT` and friends),
which git reads and the filesystem never sees. `redact` is the second line of
defence, because git echoes URLs back in its errors.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.runtime.sandbox_env import command_env
from forgepilot.seams.refs import RepoRef
from forgepilot.seams.security import ExecutionConstraints, Operation
from forgepilot.seams.vcs import (
    SERVICE_NAME,
    ChangedFile,
    DiffBundle,
    PullRequestDraft,
    PullRequestRef,
)

__all__ = [
    "GitHubVcsPlugin",
    "GitHubError",
    "GitHubRequestFailed",
    "GitFailed",
    "NoToken",
    "authenticated_env",
    "parse_error",
    "pull_request_payload",
    "redact",
    "remote_url",
]

DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_REMOTE_BASE = "https://github.com"
DEFAULT_TOKEN_ENV = "FORGEPILOT_GITHUB_TOKEN"

#: What the token is replaced with in anything that might be shown or logged.
MASK = "***"


class GitHubError(RuntimeError):
    """Base for this provider's deliberate failures."""


class NoToken(GitHubError):
    """No credential is configured for the repo host."""


class GitFailed(GitHubError):
    """A git invocation failed. Carries git's own message, redacted."""


class GitHubRequestFailed(GitHubError):
    """The API refused. Carries GitHub's own explanation, not a paraphrase."""


# ---------------------------------------------------------------------------
# Pure: what goes on the wire
# ---------------------------------------------------------------------------
def redact(text: str, secrets: Sequence[str]) -> str:
    """Replace every secret in ``text`` with :data:`MASK`.

    git quotes the URL it was given when it fails, and this provider hands git a
    URL — so every message that could reach a session log goes through here. The
    surrounding message is *kept*: `fatal: unable to access '…'` plus the status
    is how the reader finds out what went wrong, and a message replaced wholesale
    with `[redacted]` would hide the diagnosis along with the secret.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, MASK)
    return text


def remote_url(remote_base: str, repo: RepoRef) -> str:
    """The **ordinary** clone URL. Never carries a credential — see the module docstring."""
    return f"{remote_base.rstrip('/')}/{repo.namespace}/{repo.name}.git"


def authenticated_env(
    token: str, remote_base: str, *, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The environment git runs with, carrying the credential.

    Built on `command_env()`'s allow-list rather than on `os.environ`: the same
    argument that keeps the model's commands from inheriting the LLM key applies
    here, and it also means the repo-host token is not handed to a subprocess it
    has nothing to do with.

    ``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_n``/``GIT_CONFIG_VALUE_n`` are git's own
    mechanism for config that exists only for one invocation. ``insteadOf`` then
    rewrites the plain host prefix to an authenticated one at request time, so
    the URL that gets *recorded* in a clone's config is the plain one.

    ``remote_base`` is the full URL the caller configured — scheme included —
    because the scheme is part of what `insteadOf` matches on and an enterprise
    host may well be plain `http`. Stripping it here rather than at the call site
    keeps one place responsible: the first version of this function took a bare
    host and built the prefix itself, and the caller passed the scheme anyway, so
    the rewrite key read `url.https://https://github.com/`.
    """
    env = dict(command_env(base))

    # Never let git ask a terminal for a password: there is nobody there, and an
    # interactive prompt inside a background turn is a hang, not an error.
    env["GIT_TERMINAL_PROMPT"] = "0"

    # **A remote with no host has nothing to authenticate.** A `file://` base — a
    # local mirror, or the stand-in host these tests use — has no authority to
    # present a credential to, and rewriting it produces a URL git cannot resolve.
    # The first version injected the token unconditionally and turned every clone
    # into `fatal: '//x-access-token:***@C:/…' does not appear to be a git
    # repository` — which is also why the guard is here rather than a config flag.
    stripped = remote_base.rstrip("/")
    if not stripped.startswith(("http://", "https://")):
        return env

    scheme, _, bare = stripped.partition("://")
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = f"url.{scheme}://x-access-token:{token}@{bare}/.insteadOf"
    env["GIT_CONFIG_VALUE_0"] = f"{stripped}/"
    return env


def pull_request_payload(draft: PullRequestDraft) -> dict[str, Any]:
    """The JSON body for ``POST /repos/{owner}/{repo}/pulls``.

    ``draft`` is passed through rather than defaulted. For the whole life of the
    seam it was a field that looked enforced and was read by nothing, because the
    consumer did not exist — this is the consumer, and dropping it here would put
    it back.
    """
    return {
        "title": draft.title,
        "head": draft.source_branch,
        "base": draft.target_branch,
        "body": draft.body_md,
        "draft": bool(draft.draft),
    }


def parse_error(status: int, body: Any) -> str:
    """GitHub's own explanation, plus the status.

    A 422 from this endpoint means the head branch is not pushed yet, or a pull
    request for it already exists, and the body says which. Replacing that with
    "request failed" discards the only part that shortens the diagnosis.
    """
    detail = ""
    if isinstance(body, Mapping):
        detail = str(body.get("message") or "")
        errors = body.get("errors")
        if errors:
            rendered = "; ".join(
                " ".join(str(part) for part in entry.values())
                if isinstance(entry, Mapping)
                else str(entry)
                for entry in errors
            )
            detail = f"{detail}: {rendered}" if detail else rendered
    elif body:
        detail = str(body)

    return f"GitHub returned {status}" + (f" — {detail}" if detail else "")


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------
class GitHubVcsPlugin(PluginService):
    """Serves ``vcs`` against GitHub.

    Config::

        mirror_root:  ${FORGEPILOT_HOME}/mirrors
        remote_base:  https://github.com          # where clones come from
        api_base:     https://api.github.com      # an enterprise host is a line
        token_env:    FORGEPILOT_GITHUB_TOKEN     # the *name*, never the value
        timeout_s:    60
    """

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})

        root = cfg.get("mirror_root")
        if not root:
            raise ValueError(
                "mirror_root is required: a repo host clones into somewhere the "
                "provider owns. Defaulting to a temp directory would discard the "
                "mirror on reboot and re-clone the whole repository every session."
            )
        self._mirror_root = Path(root).expanduser().resolve()
        self._mirror_root.mkdir(parents=True, exist_ok=True)

        self._remote_base = str(cfg.get("remote_base", DEFAULT_REMOTE_BASE)).rstrip("/")
        self._api_base = str(cfg.get("api_base", DEFAULT_API_BASE)).rstrip("/")
        self._token_env = str(cfg.get("token_env", DEFAULT_TOKEN_ENV))
        self._timeout_s = float(cfg.get("timeout_s", 60.0))

        #: Injectable so a test can answer without a network, and so this stays
        #: the only place that knows about httpx.
        self.transport_factory: Any = None

        #: Refusals, so a test can assert one happened rather than infer it.
        self.denied_calls = 0

    # -- credentials -------------------------------------------------------
    def _token(self) -> str:
        token = os.environ.get(self._token_env, "")
        if not token:
            # Named rather than treated as "anonymous". An empty credential
            # against a private repository returns 404, and a 404 reads as "that
            # repository does not exist" — which sends the reader hunting for a
            # typo for as long as it takes them to think of the token.
            raise NoToken(
                f"no credential: {self._token_env} is unset. It is read from the "
                f"environment on purpose — a token in a config file lands in git and "
                f"in `--dump-config`."
            )
        return token

    # -- authority ---------------------------------------------------------
    def _authorise(
        self, repo: RepoRef, constraints: ExecutionConstraints, operation: Operation
    ) -> None:
        if not constraints.allows_repo(repo):
            self.denied_calls += 1
            raise PermissionError(f"repo {repo.slug!r} is not permitted")
        if not constraints.allows_operation(operation):
            self.denied_calls += 1
            raise PermissionError(
                f"principal {constraints.principal_id!r} may not {operation.value} on "
                f"{repo.slug!r}"
            )

    # -- git ---------------------------------------------------------------
    def _run_git(
        self, *args: str, cwd: Path | None = None, token: str | None = None,
        ok: tuple[int, ...] = (0,),
    ) -> str:
        """Run git, and make sure nothing it says can carry the credential out.

        `redact` is applied to both streams *before* they are put in an exception,
        because git quotes the remote URL in its failures and the exception is
        what reaches the session log.
        """
        secrets = [token] if token else []
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            env=authenticated_env(token, self._remote_base) if token else command_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode not in ok:
            detail = redact(completed.stderr or completed.stdout or "", secrets)
            raise GitFailed(f"git {' '.join(args)} failed: {detail.strip()}")
        return redact(completed.stdout or "", secrets)

    def _mirror_path(self, repo: RepoRef) -> Path:
        # Derived from the slug rather than configured per repository: two
        # sessions on one repository must share a mirror, and a configurable path
        # is how two of them end up with separate ones.
        return (self._mirror_root / repo.host / repo.namespace / repo.name).resolve()

    # -- Vcs ---------------------------------------------------------------
    def capabilities(self) -> frozenset[str]:
        """What this provider can do.

        `pull_requests` is what `run_task` consults before opening one. A host
        that declares it and cannot deliver would produce a `DELIVERED` task with
        no pull request behind it, which is the failure `READY` exists to avoid.
        """
        # No `mirrors` here even though this provider does have one: the set
        # describes what a caller may *branch on*, and `ensure_mirror` is
        # mandatory in the Protocol rather than an optional extra. A capability
        # naming a method every provider must have is a word nothing reads.
        return frozenset({"push", "pull_requests", "diff", "changed_files"})

    async def resolve(self, slug: str) -> RepoRef:
        parts = slug.split("/")
        if len(parts) != 3:
            raise ValueError(f"expected 'host/namespace/name', got {slug!r}")
        return RepoRef(host=parts[0], namespace=parts[1], name=parts[2])

    async def ensure_mirror(self, repo: RepoRef, constraints: ExecutionConstraints) -> Path:
        """Clone or update a bare mirror, and return its path.

        A bare clone rather than `--mirror`: `--mirror` sets
        ``remote.origin.mirror=true`` and a ``+refs/*:refs/*`` refspec, which
        makes ``git push origin <branch>`` publish **every** ref — tags, other
        people's branches, deletions. `push(repo, branch)` means one branch.

        The stored remote is the plain URL. See the module docstring: a
        token-URL would be a credential at rest in this directory.
        """
        self._authorise(repo, constraints, Operation.READ_CODE)
        token = self._token()
        path = self._mirror_path(repo)
        url = remote_url(self._remote_base, repo)

        if (path / "HEAD").is_file():
            # Already here. `fetch` rather than a fresh clone — the point of a
            # mirror is that the second session does not pay for the whole
            # history again, which under the target environment's 300GB figure is
            # the difference between a checkout and an incident.
            self._run_git("fetch", "--prune", "origin", cwd=path, token=token)
            return path

        path.parent.mkdir(parents=True, exist_ok=True)
        self._run_git("clone", "--bare", url, str(path), token=token)
        return path

    async def push(self, repo: RepoRef, branch: str, constraints: ExecutionConstraints) -> None:
        """Publish one branch to the host.

        The refspec is explicit — ``refs/heads/<branch>:refs/heads/<branch>`` —
        and the URL is named directly rather than reached through ``origin``. On
        a clone made with a mirror-style refspec, `push origin` would publish
        every ref; and ``push(repo, branch)`` means exactly one branch.
        """
        self._authorise(repo, constraints, Operation.WRITE_CODE)
        token = self._token()
        path = self._mirror_path(repo)
        if not (path / "HEAD").is_file():
            raise GitFailed(
                f"cannot push {branch!r}: {repo.slug!r} has no mirror at {path}. "
                f"`ensure_mirror` must run first — a push needs a local repository to "
                f"push from, and inventing one would push an empty history."
            )

        refspec = f"refs/heads/{branch}:refs/heads/{branch}"
        self._run_git(
            "push", remote_url(self._remote_base, repo), refspec, cwd=path, token=token
        )

    async def open_pull_request(
        self, draft: PullRequestDraft, constraints: ExecutionConstraints
    ) -> PullRequestRef:
        """Open the pull request, and return the reference **GitHub assigned**.

        Checked before the request is made: a refusal that had already posted
        would be a refusal in name only.
        """
        self._authorise(draft.repo, constraints, Operation.OPEN_PR)
        token = self._token()

        url = f"{self._api_base}/repos/{draft.repo.namespace}/{draft.repo.name}/pulls"
        status, body = await self._post(url, pull_request_payload(draft), token)
        if status not in (200, 201):
            raise GitHubRequestFailed(parse_error(status, body))

        if not isinstance(body, Mapping) or "number" not in body:
            raise GitHubRequestFailed(
                f"GitHub accepted the request but answered without a pull-request "
                f"number, so there is nothing to point a reader at. Body: {body!r}"
            )

        return PullRequestRef(
            repo=draft.repo,
            number=int(body["number"]),
            url=str(body.get("html_url", "")),
        )

    async def diff(
        self, repo: RepoRef, base: str, head: str, constraints: ExecutionConstraints
    ) -> DiffBundle:
        self._authorise(repo, constraints, Operation.READ_CODE)
        return DiffBundle(
            base_ref=base,
            head_ref=head,
            unified_diff=self._run_git(
                "diff", base, head, cwd=self._mirror_path(repo)
            ),
        )

    async def changed_files(
        self, repo: RepoRef, base: str, head: str, constraints: ExecutionConstraints
    ) -> Sequence[ChangedFile]:
        self._authorise(repo, constraints, Operation.READ_CODE)
        raw = self._run_git("diff", "--numstat", base, head, cwd=self._mirror_path(repo))

        out: list[ChangedFile] = []
        for line in raw.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, removed, name = parts
            # `-` rather than a number means binary, which is what numstat says
            # when it declines to count lines. Reporting it as 0 additions would
            # claim the file was read and found empty.
            binary = not added.isdigit()
            out.append(
                ChangedFile(
                    path=name,
                    additions=int(added) if added.isdigit() else 0,
                    deletions=int(removed) if removed.isdigit() else 0,
                    is_binary=binary,
                )
            )
        return tuple(out)

    # -- transport ---------------------------------------------------------
    async def _client(self) -> Any:
        """A short-lived client per call.

        Not cached: opening a pull request happens once at the end of a task, and
        a long-lived client on a plugin is a connection with no owner to close it.
        """
        import httpx

        if self.transport_factory is not None:
            return self.transport_factory()
        return httpx.AsyncClient(timeout=self._timeout_s)

    async def _post(self, url: str, payload: Any, token: str) -> tuple[int, Any]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        client = await self._client()
        try:
            response = await client.post(url, json=payload, headers=headers)
        except Exception as exc:  # noqa: BLE001 - the message may quote the URL
            # A transport failure is reported, not swallowed, and it is redacted
            # for the same reason git's is: some clients echo the request.
            raise GitHubRequestFailed(
                redact(f"could not reach {url}: {type(exc).__name__}: {exc}", [token])
            ) from exc
        finally:
            await _aclose(client)

        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError):
            body = response.text
        return response.status_code, body


async def _aclose(client: Any) -> None:
    """Close a client if it has a close, without demanding one."""
    closer = getattr(client, "aclose", None)
    if closer is not None:
        await closer()
