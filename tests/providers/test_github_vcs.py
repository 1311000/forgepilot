"""The GitHub repo host: a real ``push`` and a real pull request.

Two halves, and they are testable in different ways.

**The git half is testable for real.** Only the URL is GitHub-specific — `clone`,
`fetch` and `push` are git, and git speaks `file://`. So these tests put a bare
repository on disk, point `remote_base` at it, and let the provider do actual
clones and actual pushes. A stand-in for git would be testing a fiction, which is
the same argument as the ripgrep provider having no Python fallback.

**The HTTP half is not.** Opening a pull request needs `api.github.com`. The
request body and the response parsing are pure and are tested directly; the call
itself is one thin method behind an injectable transport, exercised with
`httpx.MockTransport`. The gated live test at `test_github_vcs_live.py` is the
only thing here that touches the network, and it does not run by default.

What is *not* tested by running it is the redaction — a real token against a real
GitHub that fails in a specific way. That is covered two ways: `redact` directly,
and a test that forces a git failure on an https remote and asserts the token is
absent from every message the provider produced.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from forgepilot.seams.refs import CommitAuthor, RepoRef
from forgepilot.seams.security import ExecutionConstraints, NetworkAccess, Operation
from forgepilot.seams.vcs import PullRequestDraft

REPO = RepoRef(host="github.com", namespace="1311000", name="test")
SLUG = REPO.slug
TOKEN = "ghp_exampleTokenThatMustNeverBePrinted"


def git(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, f"git {' '.join(args)}: {completed.stderr}"
    return completed.stdout


def tmp_path_of(mirror: Path) -> Path:
    """A scratch directory beside the mirror, for the worktrees these tests make."""
    return mirror.parent / "_wt"


def branch_with_a_commit(
    mirror: Path, branch: str, scratch: Path, files: dict[str, str]
) -> None:
    """Put a real commit on ``branch``, the way `workspace.prepare` plus a model would.

    A **bare** repository has no working tree, so `git checkout -b` refuses with
    "this operation must be run in a work tree" — which is what the first version
    of these tests asked it to do. `git worktree` is both the only way in and
    exactly what the workspace provider does for a session, so the test and the
    production path agree about the shape of the thing being pushed.
    """
    tree = scratch / branch.replace("/", "-")
    tree.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "-b", branch, str(tree), cwd=mirror)
    for name, content in files.items():
        (tree / name).write_text(content, encoding="utf-8")
    git("add", "-A", cwd=tree)
    git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-m", branch, cwd=tree)


def constraints_for(
    *,
    repos: frozenset[RepoRef] = frozenset({REPO}),
    operations: frozenset[Operation] = frozenset(
        {Operation.READ_CODE, Operation.WRITE_CODE, Operation.OPEN_PR}
    ),
) -> ExecutionConstraints:
    from forgepilot.seams.refs import PrincipalId, TenantId
    from forgepilot.seams.security import Principal

    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        allowed_repos=repos,
        permitted_operations=operations,
        writable_paths=("src",),
        network=NetworkAccess.DENY,
    )


@pytest.fixture
def remote_base(tmp_path: Path) -> str:
    """Where the stand-in host lives.

    Shared between the provider and the assertions, so the two cannot end up
    disagreeing about which URL they are both talking about.
    """
    return f"file://{(tmp_path / 'remotes').as_posix()}"


@pytest.fixture
def remote(remote_base: str, tmp_path: Path) -> Path:
    """A bare repository standing in for the host, with one commit on ``main``.

    ``file://`` rather than a path: this is the URL form git itself uses to reach
    a repository somewhere else, which is the shape the provider has to handle.
    """
    path = tmp_path / "remotes" / REPO.namespace / f"{REPO.name}.git"
    path.mkdir(parents=True)
    git("init", "--bare", "-b", "main", cwd=path)

    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    git("init", "-b", "main", cwd=seed)
    git("add", "-A", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-m", "initial", cwd=seed)
    git("remote", "add", "origin", f"file://{path.as_posix()}", cwd=seed)
    git("push", "origin", "main", cwd=seed)
    return path


@pytest.fixture
async def vcs(
    tmp_path: Path, remote: Path, remote_base: str, monkeypatch: pytest.MonkeyPatch
):
    """The provider, pointed at the local stand-in host."""
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber

    monkeypatch.setenv("FORGEPILOT_GITHUB_TOKEN", TOKEN)

    root = create_root()
    fiber = await root.plugin(
        resolve_plugin("forgepilot.providers.vcs.github:GitHubVcsPlugin"),
        {
            "mirror_root": str(tmp_path / "mirrors"),
            "remote_base": remote_base,
            "api_base": "https://api.github.com",
        },
    )
    try:
        yield root.vcs
    finally:
        await dispose_fiber(fiber)


# ---------------------------------------------------------------------------
# Pure: the pieces that decide what goes on the wire
# ---------------------------------------------------------------------------
def test_the_authenticated_url_never_contains_the_token() -> None:
    """The token travels in the *environment*, not in the URL.

    Two reasons, and the second is the one that forced the design. A URL in argv
    is visible to `ps`. Worse, git records the URL it cloned from in the clone's
    own config — so a token-URL mirror is a **credential at rest** under
    `$FORGEPILOT_HOME`, readable by anything that can read the checkout, long
    after the run that created it.
    """
    from forgepilot.providers.vcs.github import remote_url

    url = remote_url("https://github.com", REPO)

    assert url == f"https://github.com/{REPO.namespace}/{REPO.name}.git"
    assert "token" not in url.lower()


def test_the_token_is_injected_through_git_config_in_the_environment() -> None:
    """`url.<base>.insteadOf` rewrites the plain URL at request time.

    Which is what lets the mirror hold an ordinary URL while the request still
    authenticates.
    """
    from forgepilot.providers.vcs.github import authenticated_env

    env = authenticated_env(TOKEN, "https://github.com")

    assert env["GIT_CONFIG_COUNT"] == "1"
    assert TOKEN in env["GIT_CONFIG_KEY_0"]
    assert env["GIT_CONFIG_VALUE_0"] == "https://github.com/"
    # The credential travels in *this* process's environment and nowhere else.
    assert "PATH" in env, "the allow-list was dropped, so git would not resolve"


def test_redact_removes_a_secret_from_anything_git_says() -> None:
    """git echoes the URL it was given, so its errors are a leak path.

    Belt and braces on top of the environment trick: a token can also arrive in
    the config, in a redirect, or in a message somebody else wrote.
    """
    from forgepilot.providers.vcs.github import redact

    message = f"fatal: unable to access 'https://x-access-token:{TOKEN}@github.com/a/b.git/'"

    cleaned = redact(message, [TOKEN])

    assert TOKEN not in cleaned
    assert "***" in cleaned
    assert "fatal: unable to access" in cleaned, "the useful part was thrown away too"
    assert redact("", [TOKEN]) == ""


def test_the_pull_request_body_is_what_github_expects() -> None:
    from forgepilot.providers.vcs.github import pull_request_payload

    draft = PullRequestDraft(
        repo=REPO,
        source_branch="forgepilot/session-1",
        target_branch="main",
        title="fix: 少返回一个窗口",
        body_md="Fixes BUG-1.",
        ticket_id="BUG-1",
    )

    payload = pull_request_payload(draft)

    assert payload == {
        "title": "fix: 少返回一个窗口",
        "head": "forgepilot/session-1",
        "base": "main",
        "body": "Fixes BUG-1.",
        "draft": True,
    }


def test_a_draft_is_sent_as_a_draft() -> None:
    """`PullRequestDraft.draft` was set in one place and read in none for the
    whole life of the seam. This is its first consumer — if it is dropped here it
    goes back to being a field that looks enforced and is not."""
    from forgepilot.providers.vcs.github import pull_request_payload

    draft = PullRequestDraft(
        repo=REPO, source_branch="b", target_branch="main", title="t", body_md="", draft=True
    )
    assert pull_request_payload(draft)["draft"] is True

    ready = PullRequestDraft(
        repo=REPO, source_branch="b", target_branch="main", title="t", body_md="", draft=False
    )
    assert pull_request_payload(ready)["draft"] is False


def test_githubs_own_error_message_is_what_gets_reported() -> None:
    """A 422 means the branch is missing or a PR already exists, and GitHub says
    which. Replacing that with "request failed" throws away the only useful part."""
    from forgepilot.providers.vcs.github import GitHubRequestFailed, parse_error

    message = parse_error(
        422,
        {
            "message": "Validation Failed",
            "errors": [{"resource": "PullRequest", "field": "head", "code": "invalid"}],
        },
    )

    assert "422" in message
    assert "Validation Failed" in message
    assert "head" in message


def test_an_unparseable_error_body_still_reports_the_status() -> None:
    from forgepilot.providers.vcs.github import parse_error

    assert "500" in parse_error(500, None)
    assert "500" in parse_error(500, "upstream is down")


# ---------------------------------------------------------------------------
# The git half, run for real
# ---------------------------------------------------------------------------
async def test_capabilities_declare_a_host_that_can_deliver(vcs) -> None:
    """This is what turns `TaskState.READY` into `DELIVERED`.

    `run_task` consults `capabilities()` before opening a pull request, so a
    provider that forgot to declare `pull_requests` would finish every task at
    "the draft is ready" while looking perfectly healthy.
    """
    assert vcs.capabilities() >= {"push", "pull_requests"}


async def test_ensure_mirror_clones_the_repository_for_real(vcs, remote: Path) -> None:
    path = await vcs.ensure_mirror(REPO, constraints_for())

    assert path.exists()
    assert git("rev-parse", "--is-bare-repository", cwd=path).strip() == "true"
    assert git("rev-parse", "main", cwd=path).strip(), "the base branch did not come across"


async def test_ensure_mirror_is_idempotent(vcs) -> None:
    first = await vcs.ensure_mirror(REPO, constraints_for())
    second = await vcs.ensure_mirror(REPO, constraints_for())

    assert first == second
    assert first.exists()


async def test_the_mirror_records_an_ordinary_url(vcs, remote_base: str) -> None:
    """The credential-at-rest property, checked where it would actually show.

    With a `file://` fixture there is no token in play at all, so this asserts
    the *shape*: whatever URL ends up in the clone's config is the plain one,
    which is the same string `remote_url` builds and that the pure test above
    proved cannot contain a secret.
    """
    from forgepilot.providers.vcs.github import remote_url

    path = await vcs.ensure_mirror(REPO, constraints_for())

    recorded = git("remote", "get-url", "origin", cwd=path).strip()
    assert recorded == remote_url(remote_base, REPO)
    assert TOKEN not in recorded


async def test_push_really_puts_the_branch_on_the_remote(vcs, remote: Path) -> None:
    """The assertion is against the *remote*, not against what the provider says.

    A `push` that reports success without a ref appearing on the other side is
    the failure this whole seam exists to prevent, and it is invisible unless you
    go and look.
    """
    mirror = await vcs.ensure_mirror(REPO, constraints_for())
    branch_with_a_commit(
        mirror, "fix/one-window", tmp_path_of(mirror), {"app.py": "def main():\n    return 2\n"}
    )

    await vcs.push(REPO, "fix/one-window", constraints_for())

    heads = git("ls-remote", "--heads", str(remote))
    assert "refs/heads/fix/one-window" in heads


async def test_push_refuses_a_principal_that_may_not_write_code(vcs) -> None:
    from forgepilot.seams.security import Operation as Op

    await vcs.ensure_mirror(REPO, constraints_for())

    with pytest.raises(PermissionError):
        await vcs.push(
            REPO,
            "any",
            constraints_for(operations=frozenset({Op.READ_CODE, Op.OPEN_PR})),
        )


async def test_a_repository_the_caller_cannot_reach_is_refused_before_anything_is_run(vcs) -> None:
    other = RepoRef(host="github.com", namespace="someone-else", name="private")

    with pytest.raises(PermissionError):
        await vcs.ensure_mirror(other, constraints_for())


async def test_a_missing_token_is_named_rather_than_used_as_empty(
    vcs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`NoToken` instead of an anonymous request.

    An empty credential against a private repository gets a 404, and a 404 reads
    as "that repository does not exist" — which sends somebody looking for a typo
    in the name for as long as it takes them to think of the token.
    """
    from forgepilot.providers.vcs.github import NoToken

    monkeypatch.delenv("FORGEPILOT_GITHUB_TOKEN")

    with pytest.raises(NoToken) as caught:
        await vcs.push(REPO, "any", constraints_for())

    assert "FORGEPILOT_GITHUB_TOKEN" in str(caught.value)


async def test_diff_reads_the_mirror(vcs) -> None:
    mirror = await vcs.ensure_mirror(REPO, constraints_for())
    branch_with_a_commit(
        mirror, "work", tmp_path_of(mirror), {"app.py": "def main():\n    return 2\n"}
    )

    bundle = await vcs.diff(REPO, "main", "work", constraints_for())

    assert "return 2" in bundle.unified_diff
    files = await vcs.changed_files(REPO, "main", "work", constraints_for())
    assert [f.path for f in files] == ["app.py"]


# ---------------------------------------------------------------------------
# The HTTP half, against a fake transport
# ---------------------------------------------------------------------------
class Recorder:
    """Captures the request and answers with a scripted response."""

    def __init__(self, status: int = 201, payload: object | None = None) -> None:
        self.status = status
        self.payload = payload if payload is not None else {
            "number": 7,
            "html_url": f"https://github.com/{REPO.namespace}/{REPO.name}/pull/7",
        }
        self.requests: list[tuple[str, str, bytes, dict[str, str]]] = []

    def handler(self, request):  # noqa: ANN001 - httpx.Request
        import httpx

        self.requests.append(
            (
                request.method,
                str(request.url),
                request.read(),
                dict(request.headers),
            )
        )
        return httpx.Response(
            self.status,
            json=self.payload,
            headers={"content-type": "application/json"},
        )


def client_factory(recorder: Recorder):
    import httpx

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))


async def test_open_pull_request_posts_the_draft_and_returns_the_reference(vcs) -> None:
    import json

    recorder = Recorder()
    vcs.transport_factory = client_factory(recorder)

    draft = PullRequestDraft(
        repo=REPO,
        source_branch="forgepilot/session-1",
        target_branch="main",
        title="fix: 少返回一个窗口",
        body_md="Fixes BUG-1.",
        ticket_id="BUG-1",
    )

    ref = await vcs.open_pull_request(draft, constraints_for())

    method, url, body, headers = recorder.requests[0]
    assert method == "POST"
    assert url == f"https://api.github.com/repos/{REPO.namespace}/{REPO.name}/pulls"
    assert json.loads(body)["head"] == "forgepilot/session-1"
    assert json.loads(body)["draft"] is True

    # The PR is an object the *host* owns, with a number it assigned. Anything
    # this provider returned that GitHub did not say would be invented.
    assert ref.number == 7
    assert ref.url.endswith("/pull/7")
    assert ref.repo == REPO


async def test_the_token_is_sent_as_a_bearer_credential(vcs) -> None:
    recorder = Recorder()
    vcs.transport_factory = client_factory(recorder)

    await vcs.open_pull_request(
        PullRequestDraft(
            repo=REPO, source_branch="b", target_branch="main", title="t", body_md=""
        ),
        constraints_for(),
    )

    headers = {k.lower(): v for k, v in recorder.requests[0][3].items()}
    assert headers.get("authorization") == f"Bearer {TOKEN}"


async def test_a_rejected_pull_request_says_why(vcs) -> None:
    """GitHub's 422 is the one that happens: the branch is not there yet, or a
    pull request for it already exists, and the body says which."""
    from forgepilot.providers.vcs.github import GitHubRequestFailed

    recorder = Recorder(
        status=422,
        payload={
            "message": "Validation Failed",
            "errors": [{"resource": "PullRequest", "field": "head", "code": "invalid"}],
        },
    )
    vcs.transport_factory = client_factory(recorder)

    with pytest.raises(GitHubRequestFailed) as caught:
        await vcs.open_pull_request(
            PullRequestDraft(
                repo=REPO, source_branch="gone", target_branch="main", title="t", body_md=""
            ),
            constraints_for(),
        )

    assert "Validation Failed" in str(caught.value)
    assert "head" in str(caught.value)
    assert TOKEN not in str(caught.value)


async def test_open_pull_request_refuses_without_the_operation(vcs) -> None:
    from forgepilot.seams.security import Operation as Op

    recorder = Recorder()
    vcs.transport_factory = client_factory(recorder)

    with pytest.raises(PermissionError):
        await vcs.open_pull_request(
            PullRequestDraft(
                repo=REPO, source_branch="b", target_branch="main", title="t", body_md=""
            ),
            constraints_for(operations=frozenset({Op.READ_CODE})),
        )

    assert recorder.requests == [], "a refusal must not first make the request"


def test_a_local_remote_gets_no_credential_injected() -> None:
    """`insteadOf` rewrites a *host*, and `file://` has none.

    The first version injected the token unconditionally, so every clone against
    this suite's stand-in host became
    `fatal: '//x-access-token:***@C:/…' does not appear to be a git repository` —
    a URL that cannot resolve, produced by the provider that was supposed to be
    reaching the repository. Six tests went red at once and the message pointed
    at the test fixture rather than at the rewrite.
    """
    from forgepilot.providers.vcs.github import authenticated_env

    env = authenticated_env(TOKEN, "file:///srv/remotes")

    assert "GIT_CONFIG_COUNT" not in env
    assert TOKEN not in " ".join(env.values())
    # The prompt guard is about behaviour, not credentials, so it still applies.
    assert env["GIT_TERMINAL_PROMPT"] == "0"


async def test_a_failing_git_command_cannot_carry_the_token_out(
    vcs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git quotes the URL back at you, and that message becomes a log entry.

    The environment trick keeps the token out of the clone's config; this is the
    second line of defence, for the other ways it can reach a message: a redirect,
    a proxy, a credential helper, or a transport error from the client.
    """
    import forgepilot.providers.vcs.github as module

    class Failing:
        @staticmethod
        def run(*args, **kwargs):  # noqa: ANN002, ANN003
            return subprocess.CompletedProcess(
                args=args[0],
                returncode=128,
                stdout="",
                stderr=(
                    f"fatal: could not read from "
                    f"'https://x-access-token:{TOKEN}@github.com/1311000/test.git'"
                ),
            )

    # The stub fails *every* git call, and `ensure_mirror` is one — so it is
    # installed after the mirror exists. The first version installed it first,
    # and the clone's own failure already demonstrated the redaction; that is how
    # this test came to be reached by accident rather than on purpose.
    await vcs.ensure_mirror(REPO, constraints_for())
    monkeypatch.setattr(module, "subprocess", Failing)

    with pytest.raises(module.GitFailed) as caught:
        await vcs.push(REPO, "whatever", constraints_for())

    message = str(caught.value)
    assert TOKEN not in message, "the credential reached an exception message"
    assert "***" in message
    assert "could not read from" in message, "the diagnosis was redacted along with the token"
