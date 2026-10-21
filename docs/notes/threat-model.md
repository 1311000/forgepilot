# The threat model, as claims with a mechanism and a test

This is the document P17 asked for: for every security-relevant statement the project makes,
the mechanism that makes it true and the test that would fail if it stopped being true.

It exists because the recurring defect here is **a field that looks enforced and is not** —
`docs/notes/defect-shape.md` has seventeen of them. Prose is where those defects hide, so
this file's rows are checked mechanically by
`tests/contract/test_the_threat_model_is_checkable.py`, which fails when a row names a test
that does not exist, a module that does not exist, or a status outside the closed set below.

This file is a note, not a rule: `CLAUDE.md`'s "Where the rest lives" table sends a
reader here before they touch a permission, a guard, or a claim about enforcement.

## The status column means exactly three things

| Status | Means | What a reader may conclude |
|---|---|---|
| `enforced` | A mechanism refuses the act, and a test drives the refusal | Doing this fails loudly; do not build a control on top of it |
| `partial` | Enforced on some platforms or some paths, named in the row | Read the row before relying on it |
| `declaration` | Written down, consumed by nothing, or true of the object and not the system | **Nothing is prevented**; it is a statement of intent or a gap |

`declaration` is not a synonym for "planned". A row is `declaration` when the mechanism a
reader would assume exists does not: a config key nothing reads, a method with no caller, a
sentence in a docstring that no code can violate.

## Claims

| Claim | Mechanism | Where | Test | Status |
|---|---|---|---|---|
| A `pre-execute` hook cannot touch the host | `CapabilityNotGranted` on a scope with no fs/process/network capability | `src/forgepilot/runtime/tools/pipeline.py` | `tests/security/test_pre_execute_is_pure.py` | enforced |
| A write outside `writable_paths` is refused | `path_scope_guard`, monotonic deny | `src/forgepilot/runtime/guards/builtin.py` | `tests/tools/test_pipeline.py` | enforced |
| A command outside the declared set is refused | `command_scope_guard` + `runtime/commandline.py` (an unparsable line is refused) | `src/forgepilot/runtime/guards/builtin.py` | `tests/security/test_command_guard.py` | enforced |
| A tool call runs inside a real boundary or not at all | `probe()` names the mechanism; `--sandbox-check` exits non-zero when the boundary is weaker than the profile needs | `src/forgepilot/providers/sandbox/namespace.py` | `tests/providers/test_namespace_sandbox.py` | partial — full on Linux with a namespace boundary, `partial` on Windows |
| Network access is refused, not assumed away | `Operation.NETWORK` grant ∩ project policy, then the sandbox's netns | `src/forgepilot/seams/security.py` | `tests/security/test_the_network_boundary_is_enforced_or_refused.py` | enforced |
| `web_fetch` reaches nothing it was not told to reach | empty-by-default host allowlist, no redirect following, non-public addresses refused before connecting | `src/forgepilot/runtime/tools/web.py` | `tests/tools/test_web_fetch.py` | enforced |
| An MCP tool is addressed by name and never registered here | `mcp_call` carries the name; nothing enters the tool registry | `src/forgepilot/runtime/tools/mcp.py` | `tests/tools/test_mcp_tools.py` | enforced |
| A subagent cannot exceed its parent's authority | `narrowed_to` intersects with a read-only cap; nesting is refused at run time | `src/forgepilot/runtime/tools/subagent.py` | `tests/tools/test_subagent.py` | enforced |
| Authority is signed, and the signature is checked on the production path | `assemble_session` verifies with the configured key and refuses a signed set it cannot check | `src/forgepilot/runtime/assembly.py` | `tests/security/test_the_signature_is_verified.py` | enforced |
| A credential never appears as a literal | the `${NAME}` channel; the value lives in the environment | `src/forgepilot/seams/security.py` | `tests/security/test_credentials_are_never_literals.py` | enforced |
| Anything the model sees was logged first | `projection.py` is the only request builder; `log_guard.py` the only way to the model | `src/forgepilot/runtime/log_guard.py` | `tests/contract/test_the_model_call_goes_through_the_guard.py` | enforced |
| Providers do not import each other | AST walk over the import graph | `tests/contract/test_import_graph.py` | `tests/contract/test_import_graph.py` | enforced |
| Session data for one principal is not readable by another | the store's listing and the API's `_owned` check | `src/forgepilot/httpapi/routes.py` | `tests/httpapi/test_boundary.py` | partial — the JSONL store trusts the filesystem's permissions |
| The model's proposal cannot reach the decision | `PolicyService.decide()` does not accept a `TicketUnderstanding` | `src/forgepilot/identity/policy.py` | `tests/identity/test_policy_is_wired.py` | enforced |

## Assumptions — things this system does **not** defend against

| Assumption | Why it is acceptable here | What would make it false |
|---|---|---|
| The filesystem holding session logs is not hostile | single-tenant workstation or a volume only the service can read | a shared volume, or a log store reachable by other tenants |
| One server process owns the state directory | `serve_lock` refuses a second server on the same state dir | a deployment that runs two replicas against one volume — see *multi-replica*, still unbuilt |
| The memory store is a local file | `MemoryScope.REPO` and the SQLite store are local-only by decision | a network filesystem, where SQLite's locking is not the lock it claims |
| The index service is inside the trust boundary | its `/status` is unauthenticated and it holds text, not authority | exposing it outside the network that already holds the code |
| The model endpoint is trusted with the prompt | the prompt contains code and ticket text, never the signing key | a third-party model that logs prompts, if the code is sensitive |
| OIDC tokens are HS256 and verified with a shared secret | the venv has no `cryptography`; RS256/JWKS is refused **by name** | a real IdP issuing RS256 — then this row becomes the blocker |
| The container is the deployment boundary | `uids`/`gids`, read-only mounts, no `--privileged` | a host where the container runs as root with the docker socket mounted |

## What this file is not

It is not a claim that the system is secure. It is a claim that each sentence above has a
reader: `enforced` rows name the code that refuses the act and a test that drives the
refusal; `partial` rows say where the line is; `declaration` rows admit that nothing stops
the act. The next defect of the shape this project keeps finding should be added here as a
`declaration` row **before** it is fixed, so the gap is visible while it is still a gap.
