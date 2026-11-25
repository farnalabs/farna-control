"""Strict respx transport policy instrumentation (FAR-2702).

Default behaviour is unchanged: nothing here installs a hook unless the
``FARNA_STRICT_TRANSPORT=1`` env var is set. When it IS set, every respx
router is instrumented so that a test is recorded as violated when

* a request reaches a router but is not matched by any registered route
  (``unmatched_request`` - respx auto-mocks a 200 for these when the router
  was created with ``assert_all_mocked=False``, so the test would otherwise
  silently pass against an assumption nobody wrote down), or
* a registered route is never called before the router is stopped
  (``unused_route`` - the stub is dead weight and may be pinned to a wire
  shape the provider no longer returns).

Why patch ``Router.record`` and ``MockRouter.stop`` rather than wrap
``respx.mock``:

* ``respx.mock`` is a ``MockRouter`` singleton used both as a bare context
  manager / decorator (``with respx.mock:``) AND as a factory
  (``respx.mock(assert_all_called=False)``), which builds a *new* router
  per call. Only the class-level hooks cover every router a test creates,
  however it creates it, including routers a test module builds directly.
* ``Router.record`` is the single funnel every matched, unmatched and
  pass-through request passes through, and it is where an unmatched request
  is surfaced with ``route=None``.
* ``MockRouter.stop`` is the single teardown point; it still sees the
  router's own routes before the default ``rollback()`` resets them.

The mechanism is observational, not enforcing-by-raise: violations are
collected and the pytest hook in ``tests/conftest.py`` turns a passing test
into a failing one. That keeps the failure message (which violations, on
which routes) instead of an opaque respx exception, and lets the report
file drive the ratchet and the standalone runner.

Determinism: this runs in one process, serially. The strict runner does not
pass ``-n`` to pytest, and the per-test state is a ``threading.local``.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

ENV_FLAG = "FARNA_STRICT_TRANSPORT"
ENV_REPORT = "FARNA_STRICT_TRANSPORT_REPORT"

UNMATCHED_REQUEST = "unmatched_request"
UNUSED_ROUTE = "unused_route"

VIOLATION_KINDS: frozenset[str] = frozenset({UNMATCHED_REQUEST, UNUSED_ROUTE})


@dataclass(frozen=True, order=True)
class Violation:
    """One strict-mode finding, keyed by test node id and violation kind."""

    test: str
    kind: str
    detail: str

    def key(self) -> tuple[str, str]:
        return (self.test, self.kind)

    def as_dict(self) -> dict[str, str]:
        return {"test": self.test, "kind": self.kind, "detail": self.detail}


_state = threading.local()
_installed = False
_report_path: Path | None = None


def enabled() -> bool:
    """True when the opt-in env flag is set to a truthy value."""
    return os.environ.get(ENV_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


def _current() -> list[Violation]:
    bucket = getattr(_state, "violations", None)
    if bucket is None:
        bucket = []
        _state.violations = bucket
    return bucket


def current_test() -> str | None:
    return getattr(_state, "test", None)


def start_session() -> None:
    """Open (truncate) the report file for this session, when configured.

    Truncation happens only when the strict flag is on: CI runs the strict
    runner first, then re-runs the ratchet architecture test in a second
    process pointed at that report, and that second process must READ the file.

    With pytest-xdist, each worker is a separate process and appends to its own
    ``<report>.<pid>.jsonl`` file; the reader merges the glob. One shared file
    cannot be written safely from concurrent processes.
    """
    global _report_path
    raw = os.environ.get(ENV_REPORT, "").strip()
    _report_path = Path(raw) if raw else None
    if _report_path is not None and enabled():
        _report_path.parent.mkdir(parents=True, exist_ok=True)
        _report_path.write_text("", encoding="utf-8")
        for stale in _report_path.parent.glob(f"{_report_path.name}.*.jsonl"):
            stale.unlink(missing_ok=True)


def _reset_global_router() -> None:
    """Drop routes left on the ``respx.mock`` singleton by an earlier test.

    ``respx.get(...)`` / ``respx.post(...)`` always register on the module-level
    singleton, even inside ``with respx.mock(...)`` (only ``router.get(...)``
    targets the scoped router). A route a test registers that way and never has
    matched therefore survives into the NEXT test that enters the singleton,
    where it is reported as unused. That attribution is order-dependent: a
    strict run over a subset of files would blame a different test - or none -
    than a full run, which would make the baseline non-deterministic.

    Clearing the singleton's routes at the start of every test keeps each
    test's findings its own. This only runs in strict mode, so the default
    suite keeps respx's native behaviour.
    """
    from respx.api import mock as global_router

    global_router.routes.clear()
    global_router.calls.clear()


def start_test(nodeid: str) -> None:
    _state.test = nodeid
    _state.violations = []
    if _installed:
        _reset_global_router()


def _persist(violations: list[Violation]) -> None:
    if not violations or _report_path is None:
        return
    # Always per-process: xdist workers append concurrently and a single
    # shared file cannot be written safely from several processes.
    path = _report_path.with_name(f"{_report_path.name}.{os.getpid()}.jsonl")
    with path.open("a", encoding="utf-8") as handle:
        for violation in violations:
            handle.write(json.dumps(violation.as_dict(), sort_keys=True) + "\n")


def drain_violations() -> list[Violation]:
    """Take the findings so far, keeping the test id for later teardown work."""
    violations = list(_current())
    _state.violations = []
    _persist(violations)
    return violations


def finish_test() -> list[Violation]:
    """Close the current test: persist its violations and return them."""
    violations = drain_violations()
    _state.test = None
    return violations


def format_failure(violations: list[Violation]) -> str:
    """Human-readable failure body for the pytest report."""
    lines = [
        f"strict transport policy (FAR-2702) violated - set {ENV_FLAG}=1 only when you mean it; every registered route must be exercised and every outbound request must match a route.",
        "",
    ]
    for violation in violations:
        lines.append(f"  [{violation.kind}] {violation.detail}")
    return "\n".join(lines)


def _record(kind: str, detail: str) -> None:
    test = current_test()
    if test is None:
        # A route created/used outside any test item (collection-time imports,
        # module-level decorator setup) has no node id to attribute; ignore it
        # rather than polluting the next test's bucket.
        return
    _current().append(Violation(test=test, kind=kind, detail=detail))


def _describe_route(route: object) -> str:
    name = getattr(route, "name", None)
    pattern = getattr(route, "pattern", None)
    rendered = f"{pattern!r}"
    if name:
        return f"{name} {rendered}"[:300]
    return rendered[:300]


def install() -> None:
    """Install the class-level respx hooks. No-op unless opted in, once only."""
    global _installed
    if _installed or not enabled():
        return

    from respx.router import MockRouter, Router

    original_record = Router.record
    original_stop = MockRouter.stop

    def record(self, request, *, response=None, route=None):
        original_record(self, request, response=response, route=route)
        if route is None:
            _record(UNMATCHED_REQUEST, f"{request.method} {request.url}")

    def stop(self, clear: bool = True, reset: bool = True, quiet: bool = False):
        # Capture unused routes BEFORE the default implementation rolls the
        # router back to its snapshot (which drops routes added in this scope).
        unused = [] if quiet else [route for route in self.routes if not route.called]
        try:
            return original_stop(self, clear=clear, reset=reset, quiet=quiet)
        finally:
            for route in unused:
                _record(UNUSED_ROUTE, _describe_route(route))

    Router.record = record
    MockRouter.stop = stop
    _installed = True


def collect_report_violations(path: Path) -> dict[str, set[str]]:
    """Read a JSON-lines strict report (plus per-process shards) into ``{nodeid: {kind}}``."""
    found: dict[str, set[str]] = {}
    candidates = [path, *sorted(path.parent.glob(f"{path.name}.*.jsonl"))]
    for candidate in candidates:
        if not candidate.exists():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            found.setdefault(payload["test"], set()).add(payload["kind"])
    return found
