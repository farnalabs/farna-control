"""Zero-dependency database-URL helpers shared by every boot path (FAR-671).

This is a LEAF module: stdlib only, no SQLAlchemy, no ``farna`` imports, no
logging side effects. It is importable from the container entrypoint
(``deploy/fly/bootstrap_db.py``), the native launcher (ADR 031), and
``farna.settings`` — the single implementation of the boot URL contract.

Unified semantics (FAR-1440, PostgreSQL-only since ADR 068):

* ``deploy/fly/bootstrap_db.py`` historically rewrote ``postgres://`` to the
  asyncpg scheme and stripped **every** ``sslmode`` parameter (asyncpg 0.24
  did not understand ``sslmode`` in URLs; on Fly's private networks the
  driver's SSL default caused ``ConnectionResetError``).
* ``farna.settings`` rewrote the same scheme.

The current posture (FAR-1440): an operator's explicit ``sslmode`` on a
Postgres-family URL is HONOURED, not silently stripped — the docs
(``docs/deployment-security.md`` §3.2) recommend ``sslmode=require``, so the
code must accept it. ``fix_database_url`` therefore only rewrites the legacy
``postgres://`` prefix and leaves the query string untouched; consumers
translate ``sslmode`` themselves:

* the SQLAlchemy engine factory (:mod:`farna.db.session` and the system-role
  engines) extracts ``sslmode`` via :func:`split_postgres_sslmode` and passes
  the translated value through asyncpg's ``ssl`` connect arg;
* ``farna.api.dependencies.pg_connection_string`` copies the parameter into
  the psycopg-compatible URL the checkpointer/migration path uses (psycopg
  honours ``sslmode`` natively).

PostgreSQL 16 is the only supported runtime database (ADR 068). SQLite is
test-only, so this module holds no MySQL-family URL handling.

Absent ``sslmode`` still means explicit plaintext (``ssl=False``) — never the
driver default, which on Fly's private networks caused
``ConnectionResetError``, and never a silent-downgrade mode: asyncpg's
``sslmode=prefer``/``allow`` fail OPEN (CERT_NONE when the server refuses
TLS), so :func:`split_postgres_sslmode` REJECTS them at boot instead.

One deliberate narrowing: the historical deploy variant rewrote
``postgres://`` wherever it first appeared in the string (``str.replace``),
while this implementation only rewrites a ``postgres://`` PREFIX (matching the
historical ``farna.settings`` variant, which used ``startswith``). A URL with
``postgres://`` embedded after the scheme (e.g. inside a password) is not a
real Postgres URL; prefix-only rewriting is strictly safer and matches the
Settings contract.
"""

from urllib.parse import parse_qsl, urlsplit, urlunsplit

# ---------------------------------------------------------------------------
# The three database role names (THE single definition, FAR-2570)
# ---------------------------------------------------------------------------
#: The least-privilege LOGIN role the application connects as. It must be
#: NOSUPERUSER and NOBYPASSRLS: tenant isolation relies on RLS policies, and a
#: superuser (or the cluster owner) silently bypasses every one of them.
APP_ROLE = "farna_control_app"
#: The cross-org system cron role (LOGIN, NOSUPERUSER, BYPASSRLS). Only the
#: system crons and instance-global pre-auth reads use it.
SYSTEM_ROLE = "farna_control_system"
#: The cluster owner / migration role ``initdb`` creates (superuser). It owns
#: the schema and runs DDL (``DATABASE_ADMIN_URL``); the application must NEVER
#: connect as it — that is exactly the regression FAR-2570 fixed (the launcher
#: composed ``DATABASE_URL`` with this role, so the app ran as a superuser, RLS
#: was bypassed, and the boot-time role-posture assertion failed).
OWNER_ROLE = "farna_control"
#: The pre-FAR-2570 bundle bootstrap/owner role. Shipped bundles (for example
#: ``bundle-v0.3.328``) initialised the bundled cluster with ``initdb -U
#: modulo``, so an existing native install has this role (a SUPERUSER cluster
#: owner) and no ``farna_control`` role. The launcher adopts such a cluster by
#: renaming this role to :data:`OWNER_ROLE` before any owner connection; see
#: :func:`farna.launcher.initdb.adopt_legacy_owner_role`. Defined here so the
#: legacy name has a single definition like the three current role names.
LEGACY_OWNER_ROLE = "modulo"

# SSL modes asyncpg recognises (SSLMode enum). ``require``/``verify-ca``/
# ``verify-full`` fail closed in asyncpg (the connection is refused unless the
# server negotiates TLS); ``allow``/``prefer`` downgrade silently to plaintext
# (CERT_NONE) and are never accepted by :func:`split_postgres_sslmode`.
_REQUIRE_TLS_SSLMODES = frozenset({"require", "verify-ca", "verify-full"})
_PLAINTEXT_SSLMODES = frozenset({"disable"})

_POSTGRES_PREFIX = "postgres://"
_POSTGRES_ASYNC_PREFIX = "postgresql+asyncpg://"


def fix_database_url(url: str) -> str:
    """Rewrite the legacy ``postgres://`` driver prefix.

    * ``postgres://`` → ``postgresql+asyncpg://`` (prefix only)
    * ``sslmode`` is PRESERVED (FAR-1440: an operator's explicit TLS setting
      is honoured, translated to asyncpg's ``ssl`` connect arg by the engine
      factory). No MySQL-family prefix rewrite or ``sslmode`` stripping
      remains: PostgreSQL 16 is the only supported runtime database
      (ADR 068).
    """
    if url.startswith(_POSTGRES_PREFIX):
        return _POSTGRES_ASYNC_PREFIX + url[len(_POSTGRES_PREFIX) :]
    return url


_POSTGRES_SCHEMES = frozenset({"postgres", "postgresql", "postgresql+asyncpg", "postgresql+psycopg"})


def _ssl_value_for_mode(raw_value: str) -> str | bool:
    """Translate one ``sslmode`` value to asyncpg's ``ssl`` arg (or raise)."""
    mode = raw_value.strip().lower().replace("_", "-")
    if mode in _PLAINTEXT_SSLMODES:
        return False
    if mode in _REQUIRE_TLS_SSLMODES:
        return mode
    raise ValueError(f"Unsupported sslmode={raw_value!r}: use disable, require, verify-ca or verify-full (prefer/allow silently downgrade to plaintext and are not permitted)")


def _sslmode_value_for_item(raw_item: str) -> str | bool | None:
    """The translated ssl value for an ``sslmode`` query item, else None."""
    parsed_item = parse_qsl(raw_item, keep_blank_values=True)
    key = parsed_item[0][0] if parsed_item else raw_item
    if key != "sslmode":
        return None
    raw_value = parsed_item[0][1] if len(parsed_item[0]) == 2 else ""
    return _ssl_value_for_mode(raw_value)


def split_postgres_sslmode(url: str) -> tuple[str, str | bool]:
    """Extract ``sslmode`` from a Postgres URL into an asyncpg ``ssl`` value.

    Returns ``(url_without_sslmode, ssl)`` where ``ssl`` is the value to pass
    to asyncpg as the ``ssl`` connect arg:

    * absent / ``sslmode=disable`` → ``False`` (explicit plaintext — never the
      driver default, which breaks non-TLS listeners such as Fly's internal
      Postgres with ``ConnectionResetError``);
    * ``require`` / ``verify-ca`` / ``verify-full`` → the same string (asyncpg
      builds an SSLContext that FAILS CLOSED: the connection is refused unless
      the server negotiates TLS);
    * ``prefer`` / ``allow`` → ``ValueError``. asyncpg translates these to an
      SSLContext with ``CERT_NONE`` that silently downgrades to plaintext when
      the server refuses TLS — a security posture we do not permit; set
      ``require`` (or ``verify-*``) explicitly instead;
    * any other value → ``ValueError`` rather than a driver-side guess.

    ``url_without_sslmode`` is rebuilt cleanly (a mid-param ``sslmode`` is
    removed without leaving a stray ``&``). Only Postgres/asyncpg schemes are
    accepted — callers gate on the backend, and any other scheme is refused
    loudly rather than silently passing a bogus ``ssl`` arg.
    """
    parts = urlsplit(url)
    if parts.scheme not in _POSTGRES_SCHEMES:
        raise ValueError(f"split_postgres_sslmode expects a Postgres URL, got scheme {parts.scheme!r}")
    ssl: str | bool = False
    kept_raw: list[str] = []
    for raw_item in parts.query.split("&") if parts.query else []:
        ssl_value = _sslmode_value_for_item(raw_item)
        if ssl_value is not None:
            ssl = ssl_value
        else:
            kept_raw.append(raw_item)
    clean = parts._replace(query="&".join(kept_raw)).geturl()
    return clean, ssl


def split_engine_sslmode(url: str) -> tuple[str, str | bool | None]:
    """Translate ``sslmode`` for a SQLAlchemy async engine, or leave it alone.

    The ONE gate every asyncpg engine factory routes through (FAR-1440): a
    Postgres-family URL is split by :func:`split_postgres_sslmode`; any other
    scheme is returned unchanged with ``None``, signalling "not an asyncpg
    engine URL — pass no ``ssl``/``statement_cache_size`` connect args" so the
    SQLite (test-only) boot path keeps its driver defaults.

    Callers MUST pass ``ssl`` only when it is not ``None``::

        engine_url, ssl = split_engine_sslmode(raw_url)
        if ssl is not None:
            connect_args["ssl"] = ssl
            connect_args["statement_cache_size"] = 0

    Routing every factory through this one function is what keeps the TLS
    posture uniform: a factory that does its own scheme check can silently
    drift (the FAR-1440 defect, where several orphan factories kept passing a
    preserved ``sslmode`` straight to asyncpg and raised ``TypeError`` at
    first connect).
    """
    if not urlsplit(url).scheme.startswith("postgres"):
        return url, None
    return split_postgres_sslmode(url)


def derive_system_database_url(runtime_url: str) -> str:
    """Derive the ``farna_control_system`` URL from the runtime DATABASE_URL.

    Swaps the username to :data:`SYSTEM_ROLE`, preserving the password (so
    ``farna.db.bootstrap_role`` can create the role with the same
    credential), the host/port, the scheme, and any query params. Returns an
    empty string when the netloc has no ``@`` separator (no userinfo), when
    the userinfo has no password, or when the password is explicitly empty
    (``user:@host``) — in all three cases the caller falls back to
    :data:`APP_ROLE` rather than wiring a system URL whose credential could
    never match the role bootstrap would create (it seeds
    ``secrets.token_urlsafe`` when the URL carries no password).
    """
    parts = urlsplit(runtime_url)
    userinfo, sep, hostport = parts.netloc.rpartition("@")
    if sep:
        _, _, password = userinfo.partition(":")
        if password:
            new_userinfo = f"{SYSTEM_ROLE}:{password}"
            parts = parts._replace(netloc=f"{new_userinfo}@{hostport}")
            return urlunsplit(parts)
    return ""
