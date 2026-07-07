"""The spoke view-builder, split into cohesive modules (#166).

``telemetry.langfuse_spoke_tree`` assembles a spoke's flat Langfuse traces into the two nested
views (see that module's docstring). It began as one 4000-line file where every enrichment,
rollup, and view lens lived together, so no two view-builder features could touch disjoint
files. Issue #166 decomposes it into this package — one module per family, with
``langfuse_spoke_tree`` kept as the orchestrator (CLI entrypoint + public re-export surface):

- foundation: :mod:`~telemetry.spoke_tree.ids`, :mod:`~telemetry.spoke_tree.observations`
- core span-copy plumbing: ``indices``, ``assembly``, ``fold``, ``guards``, ``skills``,
  ``blocked_tools``
- rollups + scores: ``rollups``, ``scores``
- view lenses: ``steps`` (View A), ``cycle`` (View B)
- enrichments (one family per file): ``loaded_context``, ``llm_decomp``, ``context_deltas``,
  ``metadata``, ``commits``

The foundation modules import nothing from this package, so the dependency graph is acyclic.
"""
