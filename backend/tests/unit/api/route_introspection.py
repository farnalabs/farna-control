"""Shared route-introspection helpers for the ADR 047 permission sweep.

These helpers walk the FastAPI router and extract the authorisation metadata
the strict introspection tests need to assert the route guards
(``test_route_introspection.py``). Two kinds of metadata are collected:

* a **permission tag** — a ``Depends`` built by one of the ``require_*``
  factories in ``farna.api.dependencies`` (``_tagged_dep`` stamps
  ``permission`` / ``permission_kind`` / ``min_role`` on it). A tag may be
  declared on the endpoint signature, on the route decorator, merged from an
  ``APIRouter(dependencies=[...])`` / ``include_router(..., dependencies=[...])``,
  or nested inside another dependency — **all of them are collected**
  (``get_permission_tag`` -> ``_iter_applied_depends``). Reading only the
  endpoint signature (the pre-FAR-1969 behaviour) made every route-level gate
  invisible.
* the **dependency callables** applied to a route (``get_applied_dependency_names``),
  which is how a read route can be shown to be authenticated by a
  principal-bearing dependency rather than by a permission tag.
* the **credential kind** a route accepts (``get_credential_kind``, FAR-2727),
  derived from those same principal dependencies: a JWT is mandatory wherever
  ``get_current_user`` / ``get_current_tenant_user`` appears (so an ``mk_`` key
  is rejected), ``get_current_tenant_user_or_api_key`` accepts an ``mk_`` key,
  and ``get_scim_principal`` is the SCIM shared secret. Every read route that is
  not ``mk_``-capable must be declared in the shrink-only
  ``read_credential_jwt_only_allowlist.txt`` (``load_read_credential_allowlist``
  / ``find_read_credential_violations``).

FastAPI stores route-level ``Depends`` objects on ``route.dependencies`` but
rebuilds them as ``Dependant`` nodes that do **not** carry the introspection
tag, so the raw ``route.dependencies`` list must be read directly — the
``Dependant`` tree alone cannot see a route-level permission gate.
"""

from __future__ import annotations

import typing
from pathlib import Path

# ---------------------------------------------------------------------------
# Credential-kind vocabulary (FAR-2727).
#
# A route's credential surface is decided by the principal dependency FastAPI
# resolves for it — not by which permission tag it carries. The three principal
# dependencies are mutually exclusive in effect:
#
# * ``get_current_user`` / ``get_current_tenant_user`` decode a Bearer **JWT**
#   (``HTTPBearer`` -> ``decode_principal``), so an ``mk_`` org API key is
#   rejected outright (401). Anywhere one of these appears in the resolved
#   dependency tree, the route is JWT-only — even if another dependency on the
#   route is the any-credential variant, because FastAPI resolves *all* of them.
# * ``get_current_tenant_user_or_api_key`` accepts a JWT **or** an ``mk_`` key
#   (PRD §5.2 / §9.3, the documented CI/CD credential path).
# * ``get_scim_principal`` authenticates a SCIM shared secret, which is neither
#   a user JWT nor an ``mk_`` key.
# ---------------------------------------------------------------------------
JWT_PRINCIPAL_DEPENDENCIES: frozenset[str] = frozenset({"get_current_user", "get_current_tenant_user"})
API_KEY_PRINCIPAL_DEPENDENCIES: frozenset[str] = frozenset({"get_current_tenant_user_or_api_key"})
SHARED_SECRET_PRINCIPAL_DEPENDENCIES: frozenset[str] = frozenset({"get_scim_principal"})

#: Every dependency that establishes a caller principal (authentication).
PRINCIPAL_DEPENDENCIES: frozenset[str] = JWT_PRINCIPAL_DEPENDENCIES | API_KEY_PRINCIPAL_DEPENDENCIES | SHARED_SECRET_PRINCIPAL_DEPENDENCIES

CREDENTIAL_JWT_ONLY = "jwt_only"
CREDENTIAL_ANY = "any_credential"
CREDENTIAL_SHARED_SECRET = "shared_secret"
CREDENTIAL_NONE = "none"

#: Read routes that do not accept an ``mk_`` key but are authenticated must be
#: declared here, each with a reason. Shrink-only ratchet — see the file header.
READ_CREDENTIAL_ALLOWLIST_FILENAME = "read_credential_jwt_only_allowlist.txt"
_READ_CREDENTIAL_ALLOWLIST_PATH = Path(__file__).with_name(READ_CREDENTIAL_ALLOWLIST_FILENAME)

_READ_CREDENTIAL_GUIDANCE = f"""\
Read routes should normally accept an `mk_` org API key so CI/CD and external agents
can read the API with the credential they are configured with. Do ONE of:

  1. WIDEN the route — gate it with `require_permission_any_credential(...)` (or
     depend on `get_current_tenant_user_or_api_key`) instead of the JWT-only
     variant. This is the default fix.
  2. If the route is DELIBERATELY not mk_-capable (an audit-chain read, an
     evidence pack, API-key management, a team / HITL / admin surface, or
     user-scoped identity such as `me/*`), add a justified entry to
     `{READ_CREDENTIAL_ALLOWLIST_FILENAME}`.

`{READ_CREDENTIAL_ALLOWLIST_FILENAME}` is a SHRINK-ONLY RATCHET. It freezes the
reads that are JWT-only today so an accidental one cannot appear unnoticed, and so a
deliberate one is reviewed. Never add an entry to get a NEW read green — widening the
route is the default. Removing entries is always welcome. (The FAR-2699
shrink-only-baseline guard can be extended to cover this file so that adding an entry
fails CI even when the test itself passes.)"""


def get_all_apiroutes(app: object) -> list:
    """Extract all APIRoute instances, including nested included routers.

    FastAPI >= 0.116 wraps included routers in ``_IncludedRouter`` entries that
    hide their routes behind ``original_router``; recurse into those. Skip
    Mount/WebSocket routes.
    """
    routes: list = []

    def collect(router: object) -> None:
        for route in getattr(router, "routes", []):
            route_type = type(route).__name__
            if route_type == "APIRoute":
                routes.append(route)
            elif route_type == "_IncludedRouter" and hasattr(route, "original_router"):
                collect(route.original_router)

    collect(app)
    return routes


def get_mutating_routes(app: object) -> list:
    """Return routes with mutating HTTP methods (POST/PUT/PATCH/DELETE)."""
    out = []
    for route in get_all_apiroutes(app):
        methods = route.methods or set()
        if methods & {"POST", "PUT", "PATCH", "DELETE"}:
            out.append(route)
    return out


def get_read_routes(app: object) -> list:
    """Return routes that serve a GET method (the read surface).

    Reads were outside the introspection guard before FAR-1969, so a read
    route could be added without any authorisation and nothing failed.
    """
    out = []
    for route in get_all_apiroutes(app):
        if "GET" in (route.methods or set()):
            out.append(route)
    return out


def _deps_from_callable(fn: object) -> list:
    """Return every ``Depends`` applied as a parameter of ``fn``.

    Two FastAPI patterns are supported:

    * classic default-value form —
      ``user: T = Depends(require_system_permission("..."))`` — where the
      ``Depends`` is the parameter *default*;
    * ``Annotated`` form (ADR 047 sweep, used to drop ``# type: ignore``) —
      ``user: Annotated[T, Depends(require_system_permission("..."))]`` — where
      the ``Depends`` lives in the annotation metadata rather than the default.
      ``include_extras=True`` is essential: under ``from __future__ import
      annotations`` the ``Depends`` lives in an ``Annotated`` metadata slot,
      and default ``inspect.signature`` resolution strips those extras.

    Returns ``[]`` (fail-soft) when the callable's signature cannot be
    introspected; the route is still covered by the other declaration sites.
    """
    import inspect

    out: list = []
    try:
        sig = inspect.signature(fn)  # type: ignore[arg-type]
        hints = typing.get_type_hints(fn, include_extras=True)
    except (TypeError, ValueError, NameError):
        return out
    for name, param in sig.parameters.items():
        default = getattr(param, "default", None)
        if type(default).__name__ == "Depends":
            out.append(default)
        annotation = hints.get(name, param.annotation)
        for meta in getattr(annotation, "__metadata__", ()):
            if type(meta).__name__ == "Depends":
                out.append(meta)
    return out


def _iter_applied_depends(route) -> list:
    """Every ``Depends`` applied to a route, wherever it is declared.

    Three declaration sites are covered, so a permission gate cannot hide
    behind any of them:

    * the endpoint signature (``param: T = Depends(...)`` / ``Annotated``);
    * ``route.dependencies`` — dependencies declared on the route decorator,
      or merged from ``APIRouter(dependencies=[...])`` /
      ``include_router(..., dependencies=[...])``;
    * the dependency tree (``route.dependant.dependencies``), walked
      recursively so a tagged ``Depends`` nested inside another dependency is
      found too.

    ``route.dependencies`` is load-bearing: FastAPI copies route-level
    ``Depends`` objects into ``route.dependencies`` but rebuilds them as
    ``Dependant`` nodes that do **not** carry the introspection tag, so the tag
    on a route-level gate is only visible on the raw ``Depends``.
    """
    found: list = []
    seen: set[int] = set()

    def add(dep: object) -> None:
        if type(dep).__name__ != "Depends" or id(dep) in seen:
            return
        seen.add(id(dep))
        found.append(dep)

    endpoint = getattr(route, "endpoint", None)
    if endpoint is not None:
        for dep in _deps_from_callable(endpoint):
            add(dep)
    for dep in getattr(route, "dependencies", None) or []:
        add(dep)

    def walk(dependant: object) -> None:
        call = getattr(dependant, "call", None)
        if call is not None and call is not endpoint:
            for dep in _deps_from_callable(call):
                add(dep)
        for sub in getattr(dependant, "dependencies", None) or []:
            walk(sub)

    root = getattr(route, "dependant", None)
    if root is not None:
        walk(root)
    return found


def get_permission_tag(route) -> dict | None:
    """Find the permission-dependency tag(s) on a route, if any.

    The ``_tagged_dep`` helper sets ``permission``/``permission_kind``/``min_role``
    on a ``Depends`` object. Every ``Depends`` applied to the route is inspected
    — endpoint signature, route/router-level ``dependencies=[...]``, and nested
    dependencies (see ``_iter_applied_depends``). Multiple permission deps on one
    route (e.g. ``require_permission`` + ``require_team_membership_or_admin``)
    are returned via the ``tags`` key; the primary tag is the first org-role one.
    """
    tags = []
    for dep in _iter_applied_depends(route):
        if getattr(dep, "permission", None) is None:
            continue
        tags.append(
            {
                "permission": dep.permission,
                "permission_kind": getattr(dep, "permission_kind", None),
                "min_role": getattr(dep, "min_role", None),
            }
        )
    if not tags:
        return None
    primary = next(
        (t for t in tags if t["permission_kind"] in ("tenant", "tenant_or_api_key", "scoped_hybrid", "system", "system_or_org")),
        tags[0],
    )
    return {**primary, "tags": tags}


def get_applied_dependency_names(route) -> set[str]:
    """Names of every dependency callable applied to a route.

    Used by the read-route guard to assert a route is authenticated by a
    principal-bearing dependency even without a permission tag
    (``test_route_introspection.py``).
    """
    names: set[str] = set()
    for dep in _iter_applied_depends(route):
        fn = getattr(dep, "dependency", None)
        if fn is not None:
            names.add(getattr(fn, "__name__", repr(fn)))
    return names


def get_resolved_min_role(permission: str) -> str:
    """Resolve the minimum org role for a permission key from the registry."""
    from farna.auth.permissions import resolve_required

    return resolve_required(permission)


def get_credential_kind(route) -> str:
    """Classify the credential a route accepts from its applied dependencies.

    ``get_applied_dependency_names`` walks every declaration site *and* the
    nested dependency tree, so the answer reflects what FastAPI will actually
    resolve (e.g. a route-level ``require_feature`` gate composes
    ``get_current_user`` and therefore rejects ``mk_`` keys).

    Precedence is JWT first: a JWT-requiring principal anywhere on the route
    rejects an ``mk_`` key even when another dependency is the any-credential
    variant, because FastAPI resolves all of them. That ordering is what makes
    ``GET /api/v1/pipelines/{{pipeline_id}}`` (a ``require_permission`` gate
    plus a team gate) correctly JWT-only rather than any-credential.
    """
    names = get_applied_dependency_names(route)
    if names & JWT_PRINCIPAL_DEPENDENCIES:
        return CREDENTIAL_JWT_ONLY
    if names & API_KEY_PRINCIPAL_DEPENDENCIES:
        return CREDENTIAL_ANY
    if names & SHARED_SECRET_PRINCIPAL_DEPENDENCIES:
        return CREDENTIAL_SHARED_SECRET
    return CREDENTIAL_NONE


def _read_route_keys(route) -> list[tuple[str, str]]:
    """The ``("GET", path)`` keys a read route answers on."""
    methods = (route.methods or set()) - {"HEAD", "OPTIONS"}
    return [("GET", route.path)] if "GET" in methods else []


def load_read_credential_allowlist(path: Path | None = None) -> dict[tuple[str, str], tuple[str, str]]:
    """Parse the shrink-only read-credential allowlist.

    Format (tab-separated, ``#`` comments and blank lines ignored)::

        <kind>\t<method>\t<path>\t<reason>

    ``kind`` is ``jwt_only`` or ``shared_secret``. Returns
    ``{(method, path): (kind, reason)}``. A malformed line raises rather than
    being skipped — a silently ignored entry would leave a route undeclared.

    Same convention as ``tests/architecture/brand_consistency_baseline.txt``:
    a tracked baseline file with a documented header, one entry per line, frozen
    at the moment the ratchet landed. The FAR-2699 shrink-only-baseline guard
    (``scripts/run_check_ratchet_baseline.py``) can be extended to cover it so an
    ADDED entry fails CI against the base revision.
    """
    source = path or _READ_CREDENTIAL_ALLOWLIST_PATH
    declared: dict[tuple[str, str], tuple[str, str]] = {}
    for lineno, raw in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = raw.split("\t")
        if len(fields) != 4:
            raise AssertionError(f"{source.name}:{lineno}: expected 4 tab-separated fields, got {len(fields)}: {raw!r}")
        kind, _method, _route_path, _reason = (field.strip() for field in fields)
        key = (_method, _route_path)
        if key in declared:
            raise AssertionError(f"{source.name}:{lineno}: duplicate allowlist entry for {key[0]} {key[1]}")
        declared[key] = (kind, _reason)
    return declared


def find_read_credential_violations(
    routes: list,
    declared: dict[tuple[str, str], tuple[str, str]],
) -> list[str]:
    """Return one message per read route whose credential kind is undeclared.

    Rules:

    * an ``any_credential`` read must NOT be declared (declaring it would mean a
      deliberately-JWT-only route was silently widened);
    * a ``jwt_only`` or ``shared_secret`` read must be declared with the same
      kind, and each declaration carries a reason;
    * an unauthenticated read (``none``) is outside this guard — the existing
      read-authorisation test owns the public/pre-auth allowlist;
    * a stale declaration (no such read route, or the route now accepts ``mk_``)
      is itself a violation.

    Returns ``[]`` when every read route is correctly declared.
    """
    violations: list[str] = []
    live: dict[tuple[str, str], str] = {}

    for route in routes:
        kind = get_credential_kind(route)
        for key in _read_route_keys(route):
            live[key] = kind
            entry = declared.get(key)
            if kind == CREDENTIAL_NONE:
                if entry is not None:
                    violations.append(f"{key[0]} {key[1]}: declared {entry[0]} in {READ_CREDENTIAL_ALLOWLIST_FILENAME} (reason: {entry[1]!r}) but the route now has no principal dependency at all (unauthenticated). Either restore the gate or move the route to the public READ_EXEMPT list.")
                continue
            if kind == CREDENTIAL_ANY:
                if entry is not None:
                    violations.append(
                        f"{key[0]} {key[1]}: declared {entry[0]} in {READ_CREDENTIAL_ALLOWLIST_FILENAME} "
                        f"(reason: {entry[1]!r}) but the route now accepts an `mk_` org API key. "
                        "A deliberately JWT-only read was widened — if that is intended, remove the entry and "
                        "say why in the PR; otherwise revert the dependency change."
                    )
                continue
            if entry is None:
                violations.append(f"{key[0]} {key[1]}: read route is {kind} (it does NOT accept an `mk_` org API key) and is not declared in {READ_CREDENTIAL_ALLOWLIST_FILENAME}.")
            elif entry[0] != kind:
                violations.append(f"{key[0]} {key[1]}: declared {entry[0]} in {READ_CREDENTIAL_ALLOWLIST_FILENAME} but the route derives {kind} (reason on file: {entry[1]!r}). Correct the declared kind with a fresh reason.")

    for key, (declared_kind, reason) in declared.items():
        if key not in live:
            violations.append(f"{key[0]} {key[1]}: declared {declared_kind} in {READ_CREDENTIAL_ALLOWLIST_FILENAME} (reason: {reason!r}) but no such read route exists. Remove the stale entry.")

    return violations


def read_credential_guidance() -> str:
    """The author-facing guidance appended to a read-credential failure."""
    return _READ_CREDENTIAL_GUIDANCE
