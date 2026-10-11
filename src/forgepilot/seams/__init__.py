"""Seam role 1 of 3: **interface definitions only**.

The three roles are:

1. ``seams/`` — Protocols and value objects. Defines *what* a capability is.
2. ``providers/`` — implementations. May import ``seams/``, never each other.
3. Consumers (tools, the loop) — import only ``seams/`` and reach the
   implementation at runtime through the service container.

``tests/contract/test_import_graph.py`` walks the import graph and fails on a
violation, so this is enforced rather than merely documented.

**Nothing in this package may import ``cordis_port``, and nothing may import from
``providers/``.** Seams describe capabilities; they must not know how a
capability is hosted, or the seam is no longer replaceable.

Note that ``seams/security.py`` is the load-bearing module: every other seam
takes :class:`~forgepilot.seams.security.ExecutionConstraints` and is expected to
re-check it rather than trusting that the caller already did.
"""

from forgepilot.seams.agent_loop import (
    AGENTS_SERVICE_NAME,
    Agent,
    AgentFactory,
    AgentLoop,
    AgentSpec,
    AgentState,
    TurnResult,
    UserInput,
)
from forgepilot.seams.approval import (
    ApprovalOutcome,
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalService,
)
from forgepilot.seams.credentials import (
    CredentialChannel,
    CredentialRef,
    UnknownCredential,
    placeholder,
    placeholders,
)
from forgepilot.seams.llm import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    LLMCapabilities,
    LLMService,
    Role,
    StreamChunk,
    ToolCallRequest,
    ToolSchema,
    Usage,
    drain_stream,
)
from forgepilot.seams.memory import (
    CONFIDENCE_FOR_PROVENANCE,
    LEGAL_TRANSITIONS,
    MEMORY_TYPE_MODES,
    RECALLABLE_STATUSES,
    Confidence,
    EventRef,
    IllegalTransition,
    MemoryCandidate,
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryStore,
    MemoryType,
    Provenance,
    ProvenanceKind,
    RecallQuery,
    UnattributableScope,
    UnattributableTenant,
    assert_transition,
    audience_repo,
    confidence_for,
    content_hash_of,
    normalize_subject,
)
from forgepilot.seams.refs import (
    AgentId,
    BranchRef,
    CodeRef,
    CommitRef,
    LineSpan,
    MemoryId,
    Mode,
    PrincipalId,
    RepoRef,
    SessionId,
    TeamId,
    TenantId,
    TicketId,
    TurnId,
)
from forgepilot.seams.retriever import (
    WORKING_TREE,
    CodeHit,
    FileSlice,
    Retriever,
    RetrieverCapabilities,
    SearchQuery,
    SymbolHit,
    SymbolKind,
    SymbolQuery,
)
from forgepilot.seams.sandbox import (
    ExecResult,
    Sandbox,
    SandboxEnforcement,
    SandboxExecutionPolicy,
    SandboxMode,
)
from forgepilot.seams.security import (
    Budget,
    ExecutionConstraints,
    NetworkAccess,
    Operation,
    Principal,
)
from forgepilot.seams.session import (
    TURN_END_REASONS,
    EventKind,
    Page,
    SessionEvent,
    SessionStore,
    SessionSummary,
)
from forgepilot.seams.tickets import (
    AttachmentMeta,
    Comment,
    CommentRef,
    Ticket,
    TicketPriority,
    TicketRef,
    TicketSource,
    TicketStatus,
)
from forgepilot.seams.tools import (
    Capabilities,
    ResourceTargets,
    ToolCall,
    ToolContext,
    ToolDefinition,
    ToolEffect,
    ToolOutcome,
    ToolRegistry,
    ToolSessionRecorder,
)
from forgepilot.seams.vcs import (
    ChangedFile,
    CommitAuthor,
    DiffBundle,
    PullRequestDraft,
    PullRequestRef,
    Vcs,
)
from forgepilot.seams.workspace import (
    NotARepository,
    UnifiedDiff,
    UnknownRepository,
    Workspace,
    WorkspaceHandle,
)

__all__ = [
    # agent loop
    "Agent", "AgentFactory", "AgentLoop", "AgentSpec", "AgentState",
    "TurnResult", "UserInput", "AGENTS_SERVICE_NAME",
    # approval
    "ApprovalOutcome", "ApprovalPolicy", "ApprovalRequest", "ApprovalService",
    # credentials
    # The channel a command names instead of carrying a secret. Re-exported with
    # the rest of the seams so a consumer imports one module, not a path.
    "CredentialChannel", "CredentialRef", "UnknownCredential", "placeholder",
    "placeholders",
    # llm
    "ChatMessage", "ChatRequest", "ChatResponse", "LLMCapabilities", "LLMService",
    "Role", "StreamChunk", "ToolCallRequest", "ToolSchema", "Usage", "drain_stream",
    # memory
    "MemoryCandidate", "MemoryRecord", "MemoryScope", "MemoryStore", "Provenance",
    "ProvenanceKind", "RecallQuery", "UnattributableScope", "audience_repo",
    # memory, M11b's half: what kind of thing it is, how far it is believed, where
    # it is in its life, and the typed citation the belief rests on.
    "Confidence", "EventRef", "IllegalTransition", "MemoryStatus", "MemoryType",
    "UnattributableTenant", "assert_transition", "confidence_for", "content_hash_of",
    "normalize_subject", "CONFIDENCE_FOR_PROVENANCE", "LEGAL_TRANSITIONS",
    "MEMORY_TYPE_MODES", "RECALLABLE_STATUSES",
    # refs
    "AgentId", "BranchRef", "CodeRef", "CommitRef", "LineSpan", "MemoryId", "Mode",
    "PrincipalId", "RepoRef", "SessionId", "TeamId", "TenantId", "TicketId", "TurnId",
    # retriever
    "CodeHit", "FileSlice", "Retriever", "RetrieverCapabilities", "SearchQuery",
    "SymbolHit", "SymbolKind", "SymbolQuery",
    # `WORKING_TREE` is the sentinel meaning "the checkout as it is now, not a
    # commit" — public in `seams/retriever.py`, so it is re-exported here with the
    # rest of that seam. It was missing from `__all__`, which `ruff` reported as an
    # unused import, correctly: an import nothing can reach is either a mistake or an
    # unfinished re-export, and only one of those is worth keeping.
    "WORKING_TREE",
    # The labelling rule both providers must agree about, and may not import each
    # other to share. In the seam for the same reason `WORKING_TREE` is.
    "ref_to_serve",
    # sandbox
    "ExecResult", "Sandbox", "SandboxEnforcement", "SandboxExecutionPolicy", "SandboxMode",
    # security
    "Budget", "ExecutionConstraints", "NetworkAccess", "Operation", "Principal",
    # session
    "EventKind", "Page", "SessionEvent", "SessionStore", "SessionSummary",
    "TURN_END_REASONS",
    # tickets
    "AttachmentMeta", "Comment", "CommentRef", "Ticket", "TicketPriority", "TicketRef",
    "TicketSource", "TicketStatus",
    # tools
    "ToolCall", "ToolContext", "ToolDefinition", "ToolEffect", "ToolOutcome",
    "ToolRegistry",
    "ToolSessionRecorder",
    "Capabilities",
    "ResourceTargets",
    # vcs
    "ChangedFile", "CommitAuthor", "DiffBundle", "PullRequestDraft", "PullRequestRef",
    "Vcs",
    # workspace
    "NotARepository", "UnifiedDiff", "UnknownRepository", "Workspace", "WorkspaceHandle",
]
