"""Model-backend / connector hub group of :class:`PipelineExecutor`.

Step 4a of the split described in ``GAPS-harden-w2-executor.md`` §5: hub
construction, its fail-closed failure handling, the run-level connector fetch
scope, degraded-marker persistence and the guardrail-blocked compensation path
move out of ``executor.py`` as a mixin.

Patch safety
------------
The bodies below were moved verbatim. Their free variables used to resolve from
``farna.core.pipeline_engine.executor``'s module globals, which is exactly where
``mock.patch`` targets land, so each one is resolved through
:func:`~.support._ref` at CALL time instead of being bound as a module-local in
this file. ``_log`` is :data:`~.support._log`, which forwards to
``executor._log``. ``asyncio`` / ``suppress`` / ``select`` and the ORM and
exception classes are imported directly: they are shared objects, so a patch on
``executor.<name>`` either mutates the same object or would have no meaning.

``_teardown_hub`` is a module-level helper that already lives in
:mod:`.run_shape`; ``executor.py`` re-exports it (``saq_worker`` imports it from
there) and ``run_shape`` resolves it through the same bridge, so a
``mock.patch("...executor._teardown_hub")`` is observed here too.

The in-function ``from ...decorator import set_connector_hub`` in
``_build_connector_hub`` deliberately STAYS a local import (it was one before
the move): it binds the decorator's attribute at CALL time, which is what a
``patch("farna.core.pipeline_engine.decorator.set_connector_hub")`` target
relies on. Routing it through ``_ref`` would read the import-time binding on
``executor`` and stop such a patch intercepting.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from farna.connectors._rate_bucket import SharedBudgetUnavailableError
from farna.core.model_backend_hub import ModelBackendHub
from farna.db.models.model_backend import ModelBackend

from .support import _log, _ref


class HubsMixin:
    """Model-backend and connector hub construction, scoping and compensation."""

    # Assigned on the host in ``PipelineExecutor.__init__``.
    _session_factory: async_sessionmaker[AsyncSession]
    _run_connector_scope: list[str] | None
    _registry_gated_agents: set[str]

    async def _init_model_backend_hub(self, org_id: uuid.UUID) -> ModelBackendHub | None:
        """Load active model backends for the org and initialise ModelBackendHub.

        Sets the hub on the current ContextVar so node_runner can access it.
        Returns the hub (or None if no backends are configured) for cleanup
        in the caller's finally block.
        """
        hub: ModelBackendHub | None = None
        try:
            async with self._session_factory() as session, session.begin():
                await _ref("set_rls_org")(session, org_id)
                await _ref("set_rls_execution_context")(session)
                backend_rows = (
                    (
                        await session.execute(
                            select(ModelBackend).where(
                                ModelBackend.organisation_id == org_id,
                                ModelBackend.status == "active",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if isinstance(backend_rows, list) and backend_rows:
                    from farna.settings import get_settings

                    _settings = get_settings()
                    from farna.core.secrets_backend import create_secrets_backend

                    secrets_backend = create_secrets_backend(
                        fernet_key=_settings.fernet_key,
                        session=session,
                    )
                    hub = ModelBackendHub()
                    await hub.__aenter__()
                    await hub.initialise(backend_rows, secrets_backend=secrets_backend)
                    _ref("set_model_backend_hub")(hub)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("pipeline.model_backend_hub_init_failed")
            if hub is not None:
                await _ref("_teardown_hub")(hub)
            hub = None
        return hub

    async def _load_connector_scope_grants(
        self,
        session: Any,
        org_id: uuid.UUID,
        agent_ids: list[uuid.UUID],
    ) -> tuple[dict[str, set[str]], set[str]]:
        """Load connector grants and apply the FAR-764 execution gate.

        Agents from a ``registry_sourced`` collection install (registry-sourced
        installs still set it) that the operator has not yet granted are
        returned in the gated set and lose their connector grants
        (default-deny until ``grant_collection_agents`` flips the install).
        """
        from sqlalchemy import select

        from farna.core.capability_scope import agent_granted_connector_types
        from farna.db.models.agent import Agent
        from farna.db.models.collection_install import CollectionInstall

        grants: dict[str, set[str]] = {}
        gated: set[str] = set()
        if not agent_ids:
            return grants, gated
        agent_rows = (await session.execute(select(Agent).where(Agent.id.in_(agent_ids), Agent.organisation_id == org_id))).scalars().all()
        install_ids = {cid for agent in agent_rows if (cid := getattr(agent, "collection_install_id", None)) is not None}
        install_rows = []
        if install_ids:
            install_rows = (await session.execute(select(CollectionInstall).where(CollectionInstall.install_id.in_(install_ids)))).scalars().all()
        gated_installs = {str(ci.install_id) for ci in install_rows if ci.registry_sourced and not ci.agents_granted}
        for agent in agent_rows:
            agent_key = str(agent.id)
            grants[agent_key] = agent_granted_connector_types(agent.connector_type_refs)
            collection_install_id = getattr(agent, "collection_install_id", None)
            if collection_install_id is not None and str(collection_install_id) in gated_installs:
                gated.add(agent_key)
                grants.pop(agent_key, None)
        return grants, gated

    async def _resolve_run_connector_scope(
        self,
        session: Any,
        org_id: uuid.UUID,
        graph_json: dict[str, Any] | None,
    ) -> list[str] | None:
        """Resolve the run-level fetch scope (deny-by-default, FAR-435 on FAR-418).

        With *graph_json* (the run-start path) the scope is the union of every
        node's ``capability_scope.allowed_connectors`` and the referenced
        Agents' ``connector_type_refs`` grants; it is cached on the executor
        (``_run_connector_scope``) and reused by the compensation path (which
        has no graph).

        The connector scope is deny-by-default: only connector types granted by
        referenced agents are returned.

        Also enforces the execution gate (FAR-764 / ADR 032 D2): agents whose
        ``collection_install_id`` references a registry-sourced,
        not-yet-granted install get NO connector scope (default-deny) and are
        recorded in ``_registry_gated_agents`` for the node-level gate.
        """
        if graph_json is None:
            return self._run_connector_scope

        from farna.core.capability_scope import compute_run_fetch_scope

        agent_ids = _ref("_connector_scope_agent_ids")(graph_json)
        grants, registry_gated = await self._load_connector_scope_grants(
            session,
            org_id,
            agent_ids,
        )
        self._registry_gated_agents = registry_gated
        allowed_connectors = compute_run_fetch_scope(graph_json, grants)
        self._run_connector_scope = allowed_connectors
        return allowed_connectors

    async def _persist_connector_degraded_markers(self, session: Any, hub: Any) -> None:
        """FAR-495 degraded-marker persistence (best-effort, own savepoint).

        Persists a degraded marker for the skipped instances and clears stale
        markers for instances that initialised successfully (a connector fixed
        via a config/plugin change stops being flagged degraded), inside its
        own savepoint so a failure here can NEVER fail or roll back the
        run-start transaction (the hub itself already logged the skips). Only
        instances actually attempted this run appear in ``hub.skipped`` /
        ``hub.healthy``, so out-of-scope instances are never touched.
        """
        from farna.db.crud.connector_instance import (
            clear_degraded_markers,
            mark_instances_degraded,
        )

        try:
            async with session.begin_nested():
                if hub.skipped:
                    await mark_instances_degraded(session, hub.skipped)
                if hub.healthy:
                    await clear_degraded_markers(session, hub.healthy)
        except Exception:
            # No run id in scope here (_init_connector_hub is
            # org-scoped); the instance ids are the correlatable
            # identifiers.
            _log.warning(
                "pipeline.connector_degraded_marker_failed skipped=%s healthy=%s",
                sorted(str(i) for i in hub.skipped),
                sorted(str(i) for i in hub.healthy),
                exc_info=True,
            )

    async def _build_connector_hub(
        self,
        session: Any,
        org_id: uuid.UUID,
        rows: list[Any],
        allowed_connectors: list[str] | None,
    ) -> Any:
        """Build, enter and initialise the ConnectorHub for the active rows.

        Connectors confirmed configured — any failure below re-raises
        (fail closed) via the configured path. The runtime-provider
        hub stays reachable for the teardown path via the ConnectorHub
        constructor seam (``runtime_provider=`` -> ``_runtime_provider``);
        _teardown_hub acloses it there, disposing provider-tracked workspaces
        e.g. billable E2B sandboxes.
        """
        from farna.core.connector_hub import ConnectorHub
        from farna.core.pipeline_engine.decorator import set_connector_hub
        from farna.core.runtime_provider import create_default_hub
        from farna.core.secrets_backend import create_secrets_backend
        from farna.settings import get_settings

        _settings = get_settings()
        secrets_backend = create_secrets_backend(
            fernet_key=_settings.fernet_key,
            session=session,
        )
        runtime_hub = create_default_hub()
        hub: ConnectorHub | None = None
        try:
            hub = ConnectorHub(
                secrets_backend=secrets_backend,
                runtime_provider=runtime_hub,
                org_id=str(org_id),
            )
            await hub.__aenter__()
            await hub.initialise(rows, allowed_connectors=allowed_connectors)
            if hub.skipped or hub.healthy:
                await self._persist_connector_degraded_markers(session, hub)
            set_connector_hub(hub)
        except BaseException:
            # A partially-initialised hub (constructed but __aenter__ or
            # initialise raised) must still be torn down before the
            # fail-closed re-raise: the caller sees hub=None for this
            # failure mode (this helper never returned), so without this
            # guard the teardown — and with it the runtime-provider
            # aclose disposing provider-tracked workspaces (e.g.
            # billable E2B sandboxes) — would be skipped exactly here.
            # Cleanup failures are logged inside _teardown_hub and
            # suppressed here so they never mask the original error.
            if hub is not None:
                with suppress(Exception):
                    await _ref("_teardown_hub")(hub)
            raise
        return hub

    async def _hub_init_failure_hub(self, connectors_configured: bool, hub: Any) -> Any | None:
        """Fail-closed teardown for a generic hub-init failure; returns the final hub.

        *connectors_configured* means connectors ARE configured, OR we could
        NOT establish that they are NOT (a read/query error with the
        fail-closed default). A run that may require connector work must never
        fall through to ``hub=None`` — the node_runner would treat it as
        vacuous success, silently no-op'ing a configured remote integration
        and finalising the run GREEN. Re-raise so the run terminalises as
        FAILED.

        Defensive: ``connectors_configured`` is False ONLY on a confirmed
        empty result, which never raises — this branch is otherwise
        unreachable. Keep the vacuous-success return for safety.
        """
        if connectors_configured:
            _log.exception("pipeline.connector_hub_init_failed_configured")
            if hub is not None:
                await _ref("_teardown_hub")(hub)
            raise
        _log.exception("pipeline.connector_hub_init_failed")
        if hub is not None:
            await _ref("_teardown_hub")(hub)
        return None

    async def _init_connector_hub(
        self,
        org_id: uuid.UUID,
        *,
        graph_json: dict[str, Any] | None = None,
    ) -> Any | None:
        """Load active ConnectorInstance rows for the org and initialise ConnectorHub.

        Sets the hub on the current ContextVar so make_connector_fn can access it.
        Returns the hub (or None if no connectors are configured).

        When *graph_json* is provided (the run-start path), the hub applies a
        run-level fetch scope (deny-by-default, FAR-435 building on FAR-418): the
        union of every node's ``capability_scope.allowed_connectors`` and the
        referenced Agents' ``connector_type_refs`` grants. The hub decrypts ONLY
        those connectors, so out-of-scope credentials are never disclosed. The scope
        is cached on the executor and reused by the compensation path (which has no
        graph) so both apply the same deny-by-default fetch gate.

        Contract (fail-closed on the configured path):
          - If NO active connectors are configured for this run, returns None —
            the node_runner treats a None hub as vacuous success, which is correct
            only when no connector work is expected.
          - If connectors ARE configured, any failure to build the hub (settings
            read, secrets_backend construction, ConnectorHub construction,
            ``__aenter__``, or ``initialise``) RAISES, so the run terminalises as
            FAILED rather than silently completing green with no connector work.
        """
        hub: Any | None = None
        # Fail-closed default (FAR-439): ``connectors_configured`` starts True so
        # that ANY failure that prevents us from determining whether connectors
        # exist — a failed session / RLS / ``session.execute`` read, a settings
        # read, secrets_backend construction, ConnectorHub construction,
        # ``__aenter__``, or ``initialise`` — takes the re-raise path below. It is
        # set False ONLY when the reader query returns a confirmed-EMPTY result
        # (no error): the single case where ``hub=None`` (vacuous success) is
        # correct. A read/query error is NOT swallowed into the None path —
        # "couldn't determine whether connectors exist" is a fatal hub-build
        # failure (fail closed), not evidence that none are configured.
        connectors_configured = True
        try:
            async with self._session_factory() as session, session.begin():
                await _ref("set_rls_org")(session, org_id)
                await _ref("set_rls_execution_context")(session)
                from sqlalchemy import select

                from farna.db.models.connector_instance import ConnectorInstance

                allowed_connectors = await self._resolve_run_connector_scope(session, org_id, graph_json)

                rows = (
                    (
                        await session.execute(
                            select(ConnectorInstance).where(
                                ConnectorInstance.organisation_id == org_id,
                                ConnectorInstance.status == "active",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if isinstance(rows, list) and rows:
                    # Connectors confirmed configured: build the hub (fail-closed).
                    hub = await self._build_connector_hub(session, org_id, rows, allowed_connectors)
                else:
                    # Confirmed-EMPTY result (no error): the ONE genuine "no
                    # connectors configured" case. ``connectors_configured`` False
                    # -> ``hub=None`` (vacuous success). Unreachable from a read
                    # error — the fail-closed default above re-raises instead.
                    connectors_configured = False
        except asyncio.CancelledError:
            raise
        except SharedBudgetUnavailableError:
            # Configured-but-unconstructable shared Redis budget / settings-read
            # failure on the executor path (FAR-439). Swallowing it and returning
            # None would make the connector node vacuously "succeed" with the
            # "no connector hub" fallback, silently no-op'ing the remote
            # integration and finalising the run GREEN. Fail closed, loudly.
            _log.exception("pipeline.connector_hub_init_failed_shared_budget")
            if hub is not None:
                await _ref("_teardown_hub")(hub)
            raise
        except Exception:
            hub = await self._hub_init_failure_hub(connectors_configured, hub)
        return hub

    async def _compensate_blocked_run_best_effort(
        self,
        *,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        executed_nodes: dict[str, Any],
    ) -> None:
        """Run-termination compensation for a guardrail-blocked mid-run terminalization (FAR-291).

        A run whose earlier nodes performed connector side effects (pushed a PR,
        flipped a Linear status) before a later node's output was
        guardrail-blocked must have those side effects compensated. Invoked from
        ``_finalize_run_after_stream`` AFTER the terminal status write
        (``finalize_cost``), so both the ``execute()`` and ``resume()`` paths
        share this single wiring point.

        Uses its OWN fresh session (``set_rls_org`` inside ``session.begin``)
        and a FRESH connector hub for the compensation window — it never touches
        the claim_token-fenced ``finalize_cost`` transaction, and the hub
        teardown cannot interfere with the run's own hub lifecycle. Best-effort
        + failure-isolated (guard-the-guard): every raise is logged here and
        never propagates into terminalization.
        """
        hub: Any | None = None
        try:
            hub = await self._init_connector_hub(org_id)
            # The compensation + summary write commit INSIDE this session
            # transaction (so the blocked_partial summary is durable) BEFORE
            # the hub is torn down. If teardown failed inside the transaction
            # block it would roll the summary back with it — guard-the-guard:
            # teardown must never lose an already-committed compensation.
            async with self._session_factory() as session, session.begin():
                await _ref("set_rls_org")(session, org_id)
                await _ref("set_rls_execution_context")(session)
                run = await _ref("get_run")(session, run_id)
                if run is None:
                    _log.warning("guardrails.compensation.run_not_found run=%s", run_id)
                    return
                from farna.core.guardrails.compensation import compensate_blocked_run

                await compensate_blocked_run(
                    session,
                    run,
                    guardrail_block=run.error_detail or "",
                    connector_hub=hub,
                    executed_nodes=executed_nodes,
                    blocking_eval_name="",
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("guardrails.compensation.error run=%s", run_id)
        finally:
            if hub is not None:
                await _ref("_teardown_hub")(hub)
            _ref("set_connector_hub")(None)
