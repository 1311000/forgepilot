"""Task understanding and routing — the layer between a request and the runtime.

It decides *what kind of work this is, which adapters it runs on, and what
context the model gets*. It executes nothing.

::

    request -> preprocess (rules)  -> does it need a human? -> ask, stop
             -> classify (model)   -> only if the rules could not settle it
             -> route              -> tools and engine, bounded by config + constraints
             -> assemble context    -> trusted parts to the system prompt,
                                       untrusted parts to the user turn

Two invariants live here, both made executable rather than documented:

- **Not every request goes through the model.** A ticket the rules can reject, a
  plain query, and an approval all reach the classifier zero times. Asserted with
  :class:`~forgepilot.orchestration.classifier.ClassifierGateway`.
- **The model may only narrow.** Routing resolves a *name* that configuration
  defined; a classification can pick among pre-approved sets and can never add a
  tool. See :mod:`forgepilot.orchestration.router`.
"""

from forgepilot.orchestration.classifier import ClassifierGateway
from forgepilot.orchestration.context_assembly import (
    PromptSection,
    assemble_system_sections,
    compose_task_brief,
)
from forgepilot.orchestration.preprocess import PreprocessResult, preprocess
from forgepilot.orchestration.router import AdapterSelection, select_adapters
from forgepilot.orchestration.task_state import (
    ALLOWED_TRANSITIONS,
    IllegalTransition,
    TaskState,
    TaskStateMachine,
)

__all__ = [
    "ClassifierGateway",
    "PromptSection",
    "assemble_system_sections",
    "compose_task_brief",
    "PreprocessResult",
    "preprocess",
    "AdapterSelection",
    "select_adapters",
    "TaskState",
    "TaskStateMachine",
    "IllegalTransition",
    "ALLOWED_TRANSITIONS",
]
