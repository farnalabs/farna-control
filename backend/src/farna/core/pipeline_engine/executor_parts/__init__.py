"""Sibling modules extracted from :mod:`farna.core.pipeline_engine.executor`.

Step 1 of the split described in ``GAPS-harden-w2-executor.md`` §5. This pass
moves ONLY the module-level helpers, value objects and private exceptions out of
``executor.py``; the ``PipelineExecutor`` class still lives there and
``executor.py`` re-exports every moved name, so the module's importable surface
is unchanged.

* :mod:`.support` — patch bridge, private exceptions, stream/value dataclasses,
  detail sanitisation and the finalize-lock retry predicate.
* :mod:`.outcomes` — node-output, connector and terminal-outcome classification.
* :mod:`.run_shape` — graph shape/timeouts, admission capacity, work-intact,
  token/OTel accounting, interrupt payload readers and eval-mode helpers.

Deliberately empty: importing anything here would create an import cycle with
``executor`` (which imports all three).
"""
