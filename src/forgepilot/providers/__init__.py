"""Seam role 2 of 3: **implementations**.

Each provider is a Cordis plugin that registers exactly one service under the
name its seam declares. Rules:

- A provider may import ``seams/`` and ``kernel/``, but **never another
  provider**. Two providers that need to cooperate do so through a service, not
  an import, or neither is replaceable.
- A provider must **not** import ``cordis_port`` directly; ``kernel/`` exists so
  the kernel stays swappable. Use ``forgepilot.kernel.PluginService`` as the base
  class rather than ``cordis_port.Service``.

Both rules are enforced by ``tests/contract/test_import_graph.py``.

The providers that ship first are deliberately unambitious: a replay LLM, an
in-memory retriever over a fixture corpus, a mock ticket source and a mock VCS.
Their job is to let every later milestone be developed and tested with no
network, no model, and no real repository.
"""
