"""Call-time forwarders to ``farna.api.mcp_server``'s namespace.

Why this module exists
----------------------
``api/mcp_server.py`` was split (structural hardening): the tool/resource
handlers for several domains now live in ``api/mcp_tools/<domain>.py``, while
the shared spine (session factory, auth validation, plan gates, rate limiters,
tenant ContextVar accessors) stays in ``api/mcp_server.py``.

The test suite pins that spine as patch targets BY STRING PATH -- e.g.
``mock.patch("farna.api.mcp_server._session")`` and
``mock.patch("farna.api.mcp_server.validate_current_auth")`` in dozens of
modules -- and every registered tool transitively reaches both. ``mock.patch``
rebinds an attribute on the MODULE object, and a function resolves its globals
from the DEFINING module's ``__dict__``. A relocated handler that imported
``_session`` directly would therefore keep calling the original object: the
patch would stop intercepting, the test would silently run against the real
session factory, and it could pass vacuously.

Every wrapper below re-resolves the name from ``farna.api.mcp_server`` AT CALL
TIME (through ``_resolve``), so a patch applied there is still seen by every
extracted domain -- the same interception the pre-split module had, with the
interception still real rather than vacuous.

A wrapper only exists for a helper that is BOTH (a) called by extracted code and
(b) a patch target somewhere in the suite. Helpers that are not patch targets
are imported directly by the domain module from their original home; keeping
this list explicit means a new extraction adds a wrapper here rather than
quietly losing an interception.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any


def _resolve(name: str) -> Any:
    """Resolve *name* from ``farna.api.mcp_server``'s live namespace.

    Deliberately a name lookup (not a bound import) so ``mock.patch`` applies.
    """
    from farna.api import mcp_server

    return getattr(mcp_server, name)


def patchable_local(name: str, local: Any) -> Any:
    """Keep a SAME-MODULE helper a live ``farna.api.mcp_server`` patch target.

    Most entries in this module forward a helper that still LIVES in
    ``mcp_server``. A few helpers moved into a domain module while the suite
    keeps patching them by their ``farna.api.mcp_server`` path (for example
    ``patch.object(mcp_server, "_list_runs_impl", ...)``). A bare local
    reference would make that patch silently miss — the classic vacuous pass.

    A domain module therefore rebinds its own module global to the value this
    function returns, at the END of the module (after the definition and after
    the ``@mcp.tool`` registration, which captured the original):

        _list_runs_impl = patchable_local("_list_runs_impl", _list_runs_impl)

    Each call re-resolves the name from ``mcp_server``, so a patch applied
    there is seen. While UNPATCHED, that lookup returns this very resolver
    (``mcp_server`` re-exports the domain module's attribute through its PEP 562
    hook), which is why the resolver falls back to the original instead of
    recursing. ``functools.wraps`` keeps the name, docstring and signature
    (including coroutine-ness) identical to the helper it stands in for.
    """

    if inspect.iscoroutinefunction(local):

        @functools.wraps(local)
        async def _async_resolver(*args: Any, **kwargs: Any) -> Any:
            target = _resolve(name)
            if target is _async_resolver:
                target = local
            return await target(*args, **kwargs)

        return _async_resolver

    @functools.wraps(local)
    def _resolver(*args: Any, **kwargs: Any) -> Any:
        target = _resolve(name)
        if target is _resolver:
            target = local
        return target(*args, **kwargs)

    return _resolver


class _LoggerForwarder:
    """Attribute-resolving proxy for ``farna.api.mcp_server._log``.

    ``_log`` is a live patch target (``mock.patch("farna.api.mcp_server._log")``
    replaces the module attribute with a mock so a test can assert the logged
    call), and it is a LOGGER OBJECT, not a function — a ``def _log(...)``
    wrapper cannot carry ``.exception`` / ``.warning``. Extracted modules
    therefore import ``_log`` from here: every attribute access re-resolves the
    live object, so the same interception the pre-split module had still holds.
    """

    def __getattr__(self, attr: str) -> Any:
        return getattr(_resolve("_log"), attr)


_log = _LoggerForwarder()


class HITLManager:
    """Proxy for ``farna.api.mcp_server.HITLManager``: a valid annotation type that
    instantiates whatever ``HITLManager`` is bound to there at call time (live patch target).
    """

    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        return _resolve("HITLManager")(*args, **kwargs)

    def __getattr__(self, attr: str) -> Any:
        """Mirror the forwarded instance's surface (the proxy itself carries no methods)."""
        raise AttributeError(attr)


def _build_resume_executor(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server._build_resume_executor`` at call time (live patch target)."""
    return _resolve("_build_resume_executor")(*args, **kwargs)


def _check_agent_tool_scope(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server._check_agent_tool_scope`` at call time (live patch target)."""
    return _resolve("_check_agent_tool_scope")(*args, **kwargs)


def _check_human_only_gate(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server._check_human_only_gate`` at call time (live patch target)."""
    return _resolve("_check_human_only_gate")(*args, **kwargs)


def _ctx_org_id_val(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server._ctx_org_id_val`` at call time (live patch target)."""
    return _resolve("_ctx_org_id_val")(*args, **kwargs)


def _ctx_role_val(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server._ctx_role_val`` at call time (live patch target)."""
    return _resolve("_ctx_role_val")(*args, **kwargs)


def _ctx_user_id_val(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server._ctx_user_id_val`` at call time (live patch target)."""
    return _resolve("_ctx_user_id_val")(*args, **kwargs)


def _get_doc_index(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server._get_doc_index`` at call time (live patch target)."""
    return _resolve("_get_doc_index")(*args, **kwargs)


def _get_session_factory(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server._get_session_factory`` at call time (live patch target)."""
    return _resolve("_get_session_factory")(*args, **kwargs)


def _load_trigger_row(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server._load_trigger_row`` at call time (live patch target)."""
    return _resolve("_load_trigger_row")(*args, **kwargs)


def _parse_uuid_param(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server._parse_uuid_param`` at call time (live patch target)."""
    return _resolve("_parse_uuid_param")(*args, **kwargs)


def _pipeline_owner_team_id(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server._pipeline_owner_team_id`` at call time (live patch target)."""
    return _resolve("_pipeline_owner_team_id")(*args, **kwargs)


def _pipeline_team_gate(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server._pipeline_team_gate`` at call time (live patch target)."""
    return _resolve("_pipeline_team_gate")(*args, **kwargs)


def _require_plan_feature(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server._require_plan_feature`` at call time (live patch target)."""
    return _resolve("_require_plan_feature")(*args, **kwargs)


def _require_unique_trigger_name(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server._require_unique_trigger_name`` at call time (live patch target)."""
    return _resolve("_require_unique_trigger_name")(*args, **kwargs)


def _restore_failed_mcp_resume_for_reconcile(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server._restore_failed_mcp_resume_for_reconcile`` at call time (live patch target)."""
    return _resolve("_restore_failed_mcp_resume_for_reconcile")(*args, **kwargs)


def _run_is_f6a_reconcile_candidate(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server._run_is_f6a_reconcile_candidate`` at call time (live patch target)."""
    return _resolve("_run_is_f6a_reconcile_candidate")(*args, **kwargs)


def _run_owner_team_id(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server._run_owner_team_id`` at call time (live patch target)."""
    return _resolve("_run_owner_team_id")(*args, **kwargs)


def _session(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server._session`` at call time (live patch target)."""
    return _resolve("_session")(*args, **kwargs)


def _streak_status_for(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server._streak_status_for`` at call time (live patch target)."""
    return _resolve("_streak_status_for")(*args, **kwargs)


def _system_maps_discovery_session(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server._system_maps_discovery_session`` at call time (live patch target)."""
    return _resolve("_system_maps_discovery_session")(*args, **kwargs)


def _team_scoped_key_mismatch(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server._team_scoped_key_mismatch`` at call time (live patch target)."""
    return _resolve("_team_scoped_key_mismatch")(*args, **kwargs)


def anchor_trigger_streak_epoch(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.anchor_trigger_streak_epoch`` at call time (live patch target)."""
    return _resolve("anchor_trigger_streak_epoch")(*args, **kwargs)


def api_key_grants_enabled(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.api_key_grants_enabled`` at call time (live patch target)."""
    return _resolve("api_key_grants_enabled")(*args, **kwargs)


def auth_create_api_key(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.auth_create_api_key`` at call time (live patch target)."""
    return _resolve("auth_create_api_key")(*args, **kwargs)


def auth_list_api_keys(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.auth_list_api_keys`` at call time (live patch target)."""
    return _resolve("auth_list_api_keys")(*args, **kwargs)


def auth_revoke_api_key(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.auth_revoke_api_key`` at call time (live patch target)."""
    return _resolve("auth_revoke_api_key")(*args, **kwargs)


def clear_trigger_streak_after_reenable(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.clear_trigger_streak_after_reenable`` at call time (live patch target)."""
    return _resolve("clear_trigger_streak_after_reenable")(*args, **kwargs)


def compute_next_fire(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.compute_next_fire`` at call time (live patch target)."""
    return _resolve("compute_next_fire")(*args, **kwargs)


def create_secrets_backend(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.create_secrets_backend`` at call time (live patch target)."""
    return _resolve("create_secrets_backend")(*args, **kwargs)


def db_create_model_backend(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.db_create_model_backend`` at call time (live patch target)."""
    return _resolve("db_create_model_backend")(*args, **kwargs)


def db_create_schema(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.db_create_schema`` at call time (live patch target)."""
    return _resolve("db_create_schema")(*args, **kwargs)


def db_list_schemas(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.db_list_schemas`` at call time (live patch target)."""
    return _resolve("db_list_schemas")(*args, **kwargs)


def dispatch_run(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.dispatch_run`` at call time (live patch target)."""
    return _resolve("dispatch_run")(*args, **kwargs)


def finalize_cancelled_run(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.finalize_cancelled_run`` at call time (live patch target)."""
    return _resolve("finalize_cancelled_run")(*args, **kwargs)


def get_or_create_engine(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.get_or_create_engine`` at call time (live patch target)."""
    return _resolve("get_or_create_engine")(*args, **kwargs)


def get_run(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.get_run`` at call time (live patch target)."""
    return _resolve("get_run")(*args, **kwargs)


def get_schema(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.get_schema`` at call time (live patch target)."""
    return _resolve("get_schema")(*args, **kwargs)


def get_pipeline(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.get_pipeline`` at call time (live patch target)."""
    return _resolve("get_pipeline")(*args, **kwargs)


def get_primitive(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.get_primitive`` at call time (live patch target)."""
    return _resolve("get_primitive")(*args, **kwargs)


def get_primitive_by_slug(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.get_primitive_by_slug`` at call time (live patch target)."""
    return _resolve("get_primitive_by_slug")(*args, **kwargs)


def get_settings(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.get_settings`` at call time (live patch target)."""
    return _resolve("get_settings")(*args, **kwargs)


def library_copy_to_adapt(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.library_copy_to_adapt`` at call time (live patch target)."""
    return _resolve("library_copy_to_adapt")(*args, **kwargs)


def list_primitives(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.list_primitives`` at call time (live patch target)."""
    return _resolve("list_primitives")(*args, **kwargs)


def map_legacy_code(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.map_legacy_code`` at call time (live patch target)."""
    return _resolve("map_legacy_code")(*args, **kwargs)


def merge_masked_graph_nodes(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.merge_masked_graph_nodes`` at call time (live patch target)."""
    return _resolve("merge_masked_graph_nodes")(*args, **kwargs)


def present_error(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.present_error`` at call time (live patch target)."""
    return _resolve("present_error")(*args, **kwargs)


def read_run_blobs(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.read_run_blobs`` at call time (live patch target)."""
    return _resolve("read_run_blobs")(*args, **kwargs)


def resolve_plan_context(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.resolve_plan_context`` at call time (live patch target)."""
    return _resolve("resolve_plan_context")(*args, **kwargs)


def resolve_role_from_membership(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.resolve_role_from_membership`` at call time (live patch target)."""
    return _resolve("resolve_role_from_membership")(*args, **kwargs)


def run_analytics_query(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.run_analytics_query`` at call time (live patch target)."""
    return _resolve("run_analytics_query")(*args, **kwargs)


def run_concurrency_query(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.run_concurrency_query`` at call time (live patch target)."""
    return _resolve("run_concurrency_query")(*args, **kwargs)


def set_rls_org(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.set_rls_org`` at call time (live patch target)."""
    return _resolve("set_rls_org")(*args, **kwargs)


def validate_cron_expression(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.validate_cron_expression`` at call time (live patch target)."""
    return _resolve("validate_cron_expression")(*args, **kwargs)


def validate_current_auth(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.validate_current_auth`` at call time (live patch target)."""
    return _resolve("validate_current_auth")(*args, **kwargs)


def validate_hitl_answer(*args: Any, **kwargs: Any) -> Any:
    """Forward to ``farna.api.mcp_server.validate_hitl_answer`` at call time (live patch target)."""
    return _resolve("validate_hitl_answer")(*args, **kwargs)
