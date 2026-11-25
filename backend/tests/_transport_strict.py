"""Strict respx transport policy instrumentation (FAR-2702).

Default behaviour is unchanged: nothing here installs a hook unless the
``FARNA_STRICT_TRANSPORT=1`` env var is set. When it IS set, every respx
router is instrumented so that a test is recorded as violated when

* a request reaches a router but is not matched by any registered route
  (``unmatched_request`` - respx auto-mocks a 200 for these when the router
  was created with ``assert_all_mocked=False``, so the test would otherwise
  silently pass against an assumption nobody wrote down),
* a registered route is never called before the router is stopped
  (``unused_route`` - the stub is dead weight and may be pinned to a wire
  shape the provider no longer returns), or
* a write request to our own API is missing ``Authorization`` or
  ``Idempotency-Key`` (``missing_write_headers``). Only requests whose path
  starts with ``/api/`` are in scope: third-party provider writes (GitHub,
  Slack, ...) have their own auth and idempotency conventions and are the
  subject of FAR-2703, not this policy.

Two further properties make the policy meaningful rather than decorative:

* **Findings are ratcheted by fingerprint.** A violation's identity is
  ``(nodeid, kind, fingerprint(detail))``, not ``(nodeid, kind)``. A test that
  already has one baselined ``unused_route`` and grows a SECOND dead stub
  produces a new fingerprint and therefore fails.
* **Baselined findings do not fail a test.** The pytest hooks compare against
  the baseline and only fail a test for a violation the baseline does not
  already record. This is what lets the runner trust pytest's exit code: a
  non-zero exit is always a real problem (a new violation, or a genuine
  assertion failure), never the normal consequence of running a module whose
  pre-existing findings are baselined.

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
collected and the pytest hooks in ``tests/conftest.py`` turn the *unbaselined*
ones into a failing test. That keeps the failure message (which violations, on
which routes) instead of an opaque respx exception, and lets the report file
drive the ratchet and the standalone runner.

Determinism: this runs in one process, serially. The strict runner does not
pass ``-n`` to pytest, and the per-test state is a ``threading.local``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENV_FLAG = "FARNA_STRICT_TRANSPORT"
ENV_REPORT = "FARNA_STRICT_TRANSPORT_REPORT"
ENV_BASELINE = "FARNA_STRICT_TRANSPORT_BASELINE"

UNMATCHED_REQUEST = "unmatched_request"
UNUSED_ROUTE = "unused_route"
MISSING_WRITE_HEADERS = "missing_write_headers"

VIOLATION_KINDS: frozenset[str] = frozenset({UNMATCHED_REQUEST, UNUSED_ROUTE, MISSING_WRITE_HEADERS})

#: Pinky-swear opt-out marker for the write-header requirement. A test that
#: deliberately exercises a header-free write marks itself with
#: ``@pytest.mark.transport_exempt_write_headers``.
WRITE_HEADER_EXEMPT_MARKER = "transport_exempt_write_headers"

#: Opt-out marker for the autouse ``assert_all_mocked`` sentinel router, for a
#: test that deliberately makes a real (unmocked) HTTP call.
LIVE_HTTP_MARKER = "transport_allow_live_http"

#: Node id used for findings that happen outside any test item (session- or
#: module-scoped routers stopped during collection, or requests made by a
#: session fixture). Baselining such a finding is allowed; it is still a real
#: signal. The architecture test treats this id specially because it names no
#: module.
SESSION_NODEID = "<session>"

#: Methods that carry no body and therefore need neither header.
SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

#: Only our own API's writes must carry the two headers (see module docstring).
FIRST_PARTY_PATH_PREFIX = "/api/"

DEFAULT_BASELINE = Path(__file__).resolve().parent / "architecture" / "transport_policy_baseline.json"

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]+")
_LONG_HEX_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_NUMBER_RE = re.compile(r"\b\d+\b")


def fingerprint(detail: str) -> str:
    """A stable 16-hex identity for a violation detail.

    Route stubs and request URLs routinely embed UUIDs, ids and memory
    addresses. Those must not make one finding look like a new one on every
    run, but the *shape* of the finding must still distinguish two dead stubs
    in the same test. Normalise the volatile pieces, then hash.
    """
    normalised = _UUID_RE.sub("<uuid>", detail)
    normalised = _ADDRESS_RE.sub("<addr>", normalised)
    normalised = _LONG_HEX_RE.sub("<hex>", normalised)
    normalised = _NUMBER_RE.sub("<n>", normalised)
    normalised = " ".join(normalised.split())
    return hashlib.sha1(normalised.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


@dataclass(frozen=True, order=True)
class Violation:
    """One strict-mode finding, keyed by test id, kind and detail fingerprint."""

    test: str
    kind: str
    detail: str

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.detail)

    def key(self) -> tuple[str, str, str]:
        return (self.test, self.kind, self.fingerprint)

    def as_dict(self) -> dict[str, str]:
        return {"test": self.test, "kind": self.kind, "detail": self.detail, "fingerprint": self.fingerprint}


_state = threading.local()
_installed = False
_report_path: Path | None = None
_baseline_path: Path = DEFAULT_BASELINE
_baseline_keys: set[tuple[str, str, str]] | None = None
_sentinel_router: Any = None
_session_violations: list[Violation] = []
_session_lock = threading.Lock()


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


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------
def load_baseline_keys(path: Path | None = None) -> set[tuple[str, str, str]]:
    """Read a v2 baseline into ``{(test, kind, fingerprint)}``.

    A missing file is an EMPTY baseline: every finding is new. That is the safe
    direction - it can only fail more, never less. A malformed file raises;
    the architecture test is the place that turns that into a clear failure,
    and the pytest hooks call :func:`unbaselined` which degrades to "all new"
    if the baseline cannot be read rather than crashing the session.
    """
    target = path or _baseline_path
    if not target.exists():
        return set()
    payload = json.loads(target.read_text(encoding="utf-8"))
    keys: set[tuple[str, str, str]] = set()
    for node, entries in payload.get("violations", {}).items():
        for entry in entries:
            if not isinstance(entry, dict):
                # Pre-fingerprint (v1) baseline entry. Refuse it: silently
                # accepting a kind-only entry would re-open the hole that a
                # second dead stub in an already-baselined test passes.
                raise ValueError(f"{target}: {node!r} has a non-fingerprinted entry {entry!r}; regenerate the baseline with --write-baseline")
            kind = entry.get("kind")
            fp = entry.get("fingerprint")
            if kind not in VIOLATION_KINDS or not isinstance(fp, str) or not fp:
                raise ValueError(f"{target}: {node!r} has a malformed entry {entry!r}")
            keys.add((node, kind, fp))
    return keys


def configure_baseline(path: Path | None = None) -> None:
    """Point the comparing hooks at a baseline (env-driven in CI)."""
    global _baseline_path, _baseline_keys
    raw = os.environ.get(ENV_BASELINE, "").strip()
    chosen = path or (Path(raw) if raw else DEFAULT_BASELINE)
    _baseline_path = chosen
    try:
        _baseline_keys = load_baseline_keys(chosen)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"strict transport policy: could not read baseline {chosen}: {exc}; treating every finding as new\n")
        _baseline_keys = set()


def _known_keys() -> set[tuple[str, str, str]]:
    global _baseline_keys
    if _baseline_keys is None:
        try:
            _baseline_keys = load_baseline_keys(_baseline_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:  # pragma: no cover - defensive
            sys.stderr.write(f"strict transport policy: could not read baseline {_baseline_path}: {exc}; treating every finding as new\n")
            _baseline_keys = set()
    return _baseline_keys


def unbaselined(violations: list[Violation]) -> list[Violation]:
    """The findings a run must fail on: everything the baseline does not record."""
    known = _known_keys()
    return [violation for violation in violations if violation.key() not in known]


# ---------------------------------------------------------------------------
# Session / report
# ---------------------------------------------------------------------------
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


def _persist(violations: list[Violation]) -> None:
    if not violations or _report_path is None:
        return
    # Always per-process: xdist workers append concurrently and a single
    # shared file cannot be written safely from several processes.
    path = _report_path.with_name(f"{_report_path.name}.{os.getpid()}.jsonl")
    with path.open("a", encoding="utf-8") as handle:
        for violation in violations:
            handle.write(json.dumps(violation.as_dict(), sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Per-test lifecycle
# ---------------------------------------------------------------------------
def _reset_test_state() -> None:
    _state.violations = []
    _state.write_header_exempt = False
    _state.singleton_routes = None
    _state.singleton_calls = None


def start_test(nodeid: str) -> None:
    """Attribute any respx activity from here on to the test that owns it."""
    _state.test = nodeid
    _reset_test_state()


def set_write_header_exempt(exempt: bool) -> None:
    _state.write_header_exempt = exempt


def _write_header_exempt() -> bool:
    return bool(getattr(_state, "write_header_exempt", False))


def snapshot_singleton() -> None:
    """Snapshot the shared ``respx.mock`` singleton before the test body runs.

    WHY a snapshot instead of clearing: a module- or session-scoped fixture may
    register routes on the singleton during SETUP. Clearing destroyed them and
    made their absence order-dependent. Snapshotting after setup and restoring
    at teardown keeps every fixture-owned route for the whole scope while
    removing only the routes THIS test added.

    Called from ``pytest_runtest_call`` (after the fixture setup phase), which
    is why ``start_test`` - which runs at ``pytest_runtest_logstart``, before
    setup - is not where the snapshot is taken.
    """
    if not _installed:
        return
    from respx.api import mock as global_router
    from respx.models import RouteList

    # RouteList's public copy constructor is the only safe way to snapshot it:
    # the container is not a list and its __setitem__ requires another
    # RouteList, so copying into a plain list would corrupt it at restore.
    _state.singleton_routes = RouteList(global_router.routes)
    _state.singleton_calls = list(global_router.calls)


def restore_singleton() -> None:
    """Drop only the singleton routes THIS test added, then restore calls."""
    if not _installed:
        return
    routes = getattr(_state, "singleton_routes", None)
    if routes is None:
        return
    from respx.api import mock as global_router

    added = [route for route in global_router.routes if not any(route is kept for kept in routes)]
    for route in added:
        if not getattr(route, "called", False):
            _record(UNUSED_ROUTE, _describe_route(route))
    # RouteList.__setitem__ accepts another RouteList (the snapshot is one);
    # CallList is a plain list subclass, so clear + extend is enough.
    global_router.routes[:] = routes
    calls = getattr(_state, "singleton_calls", None)
    if calls is not None:
        global_router.calls.clear()
        global_router.calls.extend(calls)
    _state.singleton_routes = None
    _state.singleton_calls = None


def drain_violations() -> list[Violation]:
    """Take the findings so far, keeping the test id for later teardown work."""
    violations = list(_current())
    _state.violations = []
    _persist(violations)
    return violations


def finish_test() -> list[Violation]:
    """Close the current test: persist its violations and return them."""
    restore_singleton()
    violations = drain_violations()
    _state.test = None
    return violations


def finish_session() -> list[Violation]:
    """Close the session: persist findings that belonged to no test item."""
    with _session_lock:
        violations = list(_session_violations)
        _session_violations.clear()
    _persist(violations)
    return violations


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------
def format_failure(violations: list[Violation]) -> str:
    """Human-readable failure body for the pytest report."""
    lines = [
        f"strict transport policy (FAR-2702) violated - set {ENV_FLAG}=1 only when you mean it; every registered route must be exercised, every outbound request must match a route, and every write to our API must carry Authorization and Idempotency-Key.",
        "",
    ]
    lines.extend(f"  [{violation.kind}] {violation.detail}" for violation in violations)
    return "\n".join(lines)


def _record(kind: str, detail: str) -> None:
    test = current_test()
    violation = Violation(test=test or SESSION_NODEID, kind=kind, detail=detail)
    if test is None:
        # Outside any test item: a session/module-scoped router stopped during
        # collection or a request a session fixture made. Dropping these is how
        # the mechanism used to miss whole classes of finding; keep them in a
        # session bucket and fail the session at finish.
        with _session_lock:
            _session_violations.append(violation)
        return
    _current().append(violation)


def _describe_route(route: object) -> str:
    name = getattr(route, "name", None)
    pattern = getattr(route, "pattern", None)
    rendered = f"{pattern!r}"
    if name:
        return f"{name} {rendered}"[:300]
    return rendered[:300]


def _is_sentinel(router: object) -> bool:
    return bool(getattr(router, "_farna_strict_sentinel", False))


def _check_write_headers(request: object) -> None:
    method = str(getattr(request, "method", "")).upper()
    if method in SAFE_METHODS or _write_header_exempt():
        return
    url = getattr(request, "url", None)
    path = getattr(url, "path", "") or ""
    if not path.startswith(FIRST_PARTY_PATH_PREFIX):
        # A third-party provider call: Authorization/Idempotency-Key are that
        # provider's contract, not ours.
        return
    headers = getattr(request, "headers", {})
    missing = [header for header in ("authorization", "idempotency-key") if header not in headers]
    if not missing:
        return
    _record(MISSING_WRITE_HEADERS, f"{method} {path}: missing {', '.join(missing)}")


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------
def install(*, force: bool = False) -> None:
    """Install the class-level respx hooks.

    No-op unless opted in (``force`` exists for the meta-test that proves the
    instrumentation is live; it installs the hooks regardless of the env flag).
    """
    global _installed
    if _installed or not (enabled() or force):
        return

    from respx.router import MockRouter, Router

    original_record = Router.record
    original_stop = MockRouter.stop

    def record(self, request, *, response=None, route=None):
        original_record(self, request, response=response, route=route)
        if _is_sentinel(self):
            return
        if route is None:
            _record(UNMATCHED_REQUEST, f"{request.method} {request.url}")
        _check_write_headers(request)

    def stop(self, clear: bool = True, reset: bool = True, quiet: bool = False):
        # Capture unused routes BEFORE the default implementation rolls the
        # router back to its snapshot (which drops routes added in this scope).
        unused = [] if quiet or _is_sentinel(self) else [route for route in self.routes if not route.called]
        try:
            return original_stop(self, clear=clear, reset=reset, quiet=quiet)
        finally:
            for route in unused:
                _record(UNUSED_ROUTE, _describe_route(route))

    Router.record = record
    MockRouter.stop = stop
    _installed = True


def start_sentinel() -> None:
    """Start the autouse ``assert_all_mocked`` router for one test.

    It registers NO routes, so every request it is offered raises
    ``AllMockedAssertionError``; respx's ``Mocker.handler`` catches that and
    tries the next registered router. A request that no test router matches
    therefore ends up raising - a raw httpx call with no router fails instead
    of reaching the network. The sentinel is excluded from instrumentation, so
    it never produces findings of its own.
    """
    global _sentinel_router
    from respx.router import MockRouter

    if _sentinel_router is not None:
        return
    router = MockRouter(assert_all_called=False, assert_all_mocked=True)
    router._farna_strict_sentinel = True  # type: ignore[attr-defined]
    router.start()
    _sentinel_router = router


def stop_sentinel() -> None:
    global _sentinel_router
    router = _sentinel_router
    if router is None:
        return
    _sentinel_router = None
    router.stop(quiet=True)


# ---------------------------------------------------------------------------
# Report reading (shared by the runner and the architecture ratchet)
# ---------------------------------------------------------------------------
def iter_report_violations(path: Path) -> list[Violation]:
    """Read a JSON-lines strict report (plus per-process shards)."""
    found: list[Violation] = []
    for candidate in (path, *sorted(path.parent.glob(f"{path.name}.*.jsonl"))):
        if not candidate.exists():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            found.append(
                Violation(
                    test=payload["test"],
                    kind=payload["kind"],
                    detail=payload.get("detail", ""),
                ),
            )
    return found


def collect_report_keys(path: Path) -> set[tuple[str, str, str]]:
    """The ``(test, kind, fingerprint)`` identities a report records."""
    return {violation.key() for violation in iter_report_violations(path)}
