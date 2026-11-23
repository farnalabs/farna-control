"""Sibling modules extracted from :mod:`farna.core.pipeline_engine.executor`.

Steps 1-4 of the split described in ``GAPS-harden-w2-executor.md`` §5.

Step 1 moved the module-level helpers, value objects and private exceptions out
of ``executor.py``; the ``PipelineExecutor`` class still lives there and
``executor.py`` re-exports every moved name, so the module's importable surface
is unchanged.

* :mod:`.support` — patch bridge, private exceptions, stream/value dataclasses,
  detail sanitisation and the finalize-lock retry predicate.
* :mod:`.outcomes` — node-output, connector and terminal-outcome classification.
* :mod:`.run_shape` — graph shape/timeouts, admission capacity, work-intact,
  token/OTel accounting, interrupt payload readers and eval-mode helpers.

Steps 2-4 split seven of the class's method groups into mixins that
``PipelineExecutor`` inherits (and that nothing else is meant to import):

* :mod:`.finalize` — terminalization / masked-failure downgrades / the
  post-commit alert, notification, evidence and teardown-seal tail.
* :mod:`.telemetry` — cost and token accounting, the evidence probes, and the
  cancellation / context-write audit hooks.
* :mod:`.hubs` — model-backend and connector hub construction, the run-level
  connector fetch scope, degraded-marker persistence and the guardrail-blocked
  compensation path.
* :mod:`.admission` — capacity, claim, execution-spend, retry-ceiling and the
  spend/policy pre-gates.
* :mod:`.hitl` — interrupt-gate creation (coalescing, briefing, config,
  window) and the ``hitl_awaiting`` notification dispatch.
* :mod:`.streaming` — ``astream_events`` consumption, event mapping, the
  exception-to-terminal-outcome classification and ``_stream_graph``.
* :mod:`.run_execute` — ``execute`` / ``resume``, their load/validate/admit/
  prepare and stream-phase helpers, and transient-failure recovery.

Every mixin resolves its free names through :func:`.support._ref` so a
``mock.patch`` on ``farna.core.pipeline_engine.executor.<name>`` is still
observed at call time.

Deliberately exports nothing: importing anything here would create an import
cycle with ``executor`` (which imports all seven mixin modules plus the shared
helper modules).
"""
