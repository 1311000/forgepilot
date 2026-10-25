# Boundary comparison: NVIDIA OpenShell (0.1.0) against this system

**Why this file exists.** `docs/notes/threat-model.md` says what *we* enforce. It cannot
say whether the boundary is a strong one, because it has nothing to compare against. This
note takes a public, shipped design — NVIDIA OpenShell 0.1.0, an open-source runtime that
"enforces which systems and data an AI agent can access" — and puts our claims beside it,
one row each. The purpose is to find out where we are not merely different but **weaker**,
and to write those places down instead of leaving the threat-model table to imply parity.

**What this file is not.** It is not a benchmark: no numbers are compared, because none of
our tests measure what their sandbox measures. It is not a verdict on fitness: OpenShell
targets fleets of agents in containers and VMs; ForgePilot is one server process with a
ticket workflow, and a boundary we do not need is not a gap. And it is not maintained from
memory — every row names where our mechanism lives, and `tests/contract/test_the_boundary_
comparison_is_kept.py` fails if a named file or test disappears.

**Evidence for the OpenShell side**, all read 2026-10-25 and cited as untrusted external
data:

- [Add Runtime Controls to AI Agents with NVIDIA OpenShell](https://developer.nvidia.com/blog/add-runtime-controls-to-ai-agents-with-nvidia-openshell)
  (NVIDIA Technical Blog, 2026-09-28) — the architecture, the policy example, the
  credential-proxy figure, the policy advisor and the prover.
- [NVIDIA OpenShell](https://www.nvidia.com/en-us/ai/openshell/) — product overview.
- [NVIDIA/OpenShell](https://github.com/NVIDIA/OpenShell) — the repository the blog links.

Verdicts are a closed set: **equal**, **ours stronger**, **theirs stronger**,
**not comparable**. Where the public material does not describe a mechanism, the row says
`no public material` rather than guessing — an absence of evidence is not evidence of absence.

## The table

| Claim | OpenShell 0.1.0 | ForgePilot | Verdict | Our mechanism | Our test |
|---|---|---|---|---|---|
| Enforcement lives outside the workload | Gateway + Supervisor + Sandbox; the supervisor inspects traffic, the sandbox has "no network path except through the supervisor"; kernel-level filesystem and process controls | Policy, guards and approval all run **in the agent's own process**; only `run` crosses into a sandbox | theirs stronger | `src/forgepilot/runtime/tools/pipeline.py` | `tests/security/test_pre_execute_is_pure.py` |
| Network is denied by default | `no-network.yaml` sandbox; outbound is refused until a policy names a host, port and binary | `NetworkAccess` grant ∩ project policy, then the sandbox's network namespace; `run_checks` and `run` inherit both | equal | `src/forgepilot/seams/security.py` | `tests/security/test_the_network_boundary_is_enforced_or_refused.py` |
| A read may be allowed while a write to the same API is refused | `protocol: rest` inspection: `access: read-only` allows `GET` and blocks `POST` to the same host, per binary | Not expressible: the grant is all-or-nothing per sandbox, and `web_fetch` is GET-only and reaches only an allowlisted host | theirs stronger | `src/forgepilot/runtime/tools/web.py` | `tests/tools/test_web_fetch.py` |
| The real credential never enters the workload | A placeholder is sent; the supervisor substitutes the real key outside the workload, bound to approved endpoints; both network policy and credential binding must permit | The `${NAME}` channel is resolved **in this process** and substituted into the command string, so the value is in the child's argv while it runs; it is scrubbed from the log and from the tool result | theirs stronger | `src/forgepilot/runtime/credentials.py` | `tests/security/test_a_secret_reaches_the_child_argv_and_not_the_log.py` |
| Credentials are not reachable by name from the model | Provider profiles attach a credential to a service, not to a shell | `${NAME}` only resolves names the profile declares; an undeclared name is refused, and a literal secret in a command is refused | equal | `src/forgepilot/providers/credentials/env.py` | `tests/security/test_credentials_are_never_literals.py` |
| Every decision is auditable | Policy decisions recorded in an OCSF audit trail | Every tool call, guard denial, approval and refusal is a session event; the log is the only place the model's view comes from | equal | `src/forgepilot/runtime/session.py` | `tests/contract/test_the_model_call_goes_through_the_guard.py` |
| Permissions can be proved, not just tested | A formal policy prover over OPA/Rego: proves modeled permissions stay inside a boundary or names an action that crosses it; adversarial agents could not talk it around | A threat-model table whose every row names a mechanism and a test, and contract tests that fail when a mechanism disappears — evidence by example, not by proof | theirs stronger | `docs/notes/threat-model.md` | `tests/contract/test_the_threat_model_is_checkable.py` |
| Authority can widen only with a human's answer | The agent may *propose* a narrower policy; it is pending by default, the agent cannot approve its own request, a human (or an AI reviewer with the prover's evidence) decides | `open_pr`, `forget_memory`, `purge_memory` stop for a one-shot approval; there is no path for the model to propose a new *capability* at all, so widening needs a deployment change | not comparable | `src/forgepilot/identity/policy.py` | `tests/identity/test_policy_is_wired.py` |
| A subagent cannot exceed its parent | Controls stay in place when the agent "proposes task delegation to sub-agents" | `spawn_subagent` narrows to the parent ∩ a read-only cap, and refuses nesting at run time | equal | `src/forgepilot/runtime/tools/subagent.py` | `tests/tools/test_subagent.py` |
| A third-party tool is reached by name, never registered here | Remote MCP servers are reached through the supervisor, which can allow a query and block a write on the same server | `mcp_call` addresses a tool by name; nothing enters the tool registry; a per-server allowlist decides which names exist | theirs stronger on inspection, equal on registration | `src/forgepilot/runtime/tools/mcp.py` | `tests/tools/test_mcp_tools.py` |
| Nothing a hook does can precede the approval it should wait for | `no public material` — the design enforces outside the workload rather than sealing hooks inside it | `pre-execute` runs on a scope with no filesystem, process or network capability, so a hook that tries to act raises `CapabilityNotGranted` before the approval prompt exists | not comparable | `src/forgepilot/runtime/tools/pipeline.py` | `tests/security/test_pre_execute_is_pure.py` |
| Filesystem policy is per file | Kernel controls over "which files a workload can read or change", established at sandbox start | Read-only bind mounts over whole trees, writable only under `writable_paths`, plus a path guard that refuses anything outside the workspace | theirs stronger | `src/forgepilot/providers/sandbox/namespace.py` | `tests/providers/test_namespace_sandbox.py` |

## What the comparison changed here

One claim was **overstated**, and the comparison is what surfaced it. The threat model said
"A credential never appears as a literal — the value lives in the environment". That is true
of the model's text, the tool result and the log, and false of the child's `argv` while the
command runs: `${NAME}` is substituted here and handed to `shell_argv`. The row now says
exactly what is enforced, and
`tests/security/test_a_secret_reaches_the_child_argv_and_not_the_log.py` pins both halves —
the value is in the argv, and it is not in anything recorded.

That is the whole finding, and it is worth its size: the difference between "we never let a
secret become a literal" and "we never let it become a literal *in the log*" is exactly the
kind of sentence this repository keeps finding in front of a mechanism that does less.

## Decisions this comparison produced

| Decision | Why, now | What would reopen it |
|---|---|---|
| Do **not** build a supervisor-side credential proxy | It only pays off when the workload is untrusted and long-running, which is what a container per agent buys; our one server process would be proxying for itself | a deployment that runs untrusted commands with real credentials — then substitution has to move outside the process, and `web_fetch`/MCP are the natural clients of an egress proxy |
| Do **not** build per-request protocol inspection (read-vs-write on one API) | Our tools are typed: `web_fetch` is a GET, `run` is confined to the sandbox's netns, and the MCP allowlist is per tool name | a scenario where the same service must be readable and not writable from the same tool — today no shipped tool has both verbs |
| Do **not** build a policy prover | A prover is only as good as the model it is handed, and our policy surface is a small typed grant rather than a configuration language | the policy surface grows to something a human cannot enumerate — that is when a prover starts paying |
| Keep the threat-model table as the proof | It costs one row per claim and it caught this one by being compared | nothing: a better prover would subsume it, and that is a reason to revisit it, not to drop it |

This note is a *record*, not a rule: the rules a change must follow live in CLAUDE.md, and the enforced claims live in docs/notes/threat-model.md. What is here is what a comparison taught that neither of those files could have produced on its own — and the four decisions it changed.
