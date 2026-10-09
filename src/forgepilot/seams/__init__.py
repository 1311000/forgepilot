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
    MemoryCandidate,
    MemoryRecord,
    MemoryScope,
    MemoryStore,
    Provenance,
    ProvenanceKind,
    RecallQuery,
    UnattributableScope,
    audience_repo,
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
    EventKind,
    Page,
    SessionEvent,
    SessionStore,
    SessionSummary,
    TURN_END_REASONS,
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
    # llm
    "ChatMessage", "ChatRequest", "ChatResponse", "LLMCapabilities", "LLMService",
    "Role", "StreamChunk", "ToolCallRequest", "ToolSchema", "Usage", "drain_stream",
    # memory
    "MemoryCandidate", "MemoryRecord", "MemoryScope", "MemoryStore", "Provenance",
    "ProvenanceKind", "RecallQuery", "UnattributableScope", "audience_repo",
    # refs
    "AgentId", "BranchRef", "CodeRef", "CommitRef", "LineSpan", "MemoryId", "Mode",
    "PrincipalId", "RepoRef", "SessionId", "TeamId", "TenantId", "TicketId", "TurnId",
    # retriever
    "CodeHit", "FileSlice", "Retriever", "RetrieverCapabilities", "SearchQuery",
    "SymbolHit", "SymbolKind", "SymbolQuery",
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
