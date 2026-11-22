"""Unit tests for the query_analytics MCP tool (FAR-102, Part C).

Covers: auth failure, insufficient scope (the analytics.query permission),
the analytics_page feature gate, typed-param parsing (multi-value pipeline_id,
error_code, invalid enums), the shared-service success path, and every
service-error mapping (rate limit, validation, timeout, migration, DB, generic).

The MCP auth context, the plan/gate patches and the service stub are shared
with the concurrency tool's contract in ``analytics_tool_harness``.
"""

import uuid
from unittest.mock import AsyncMock, patch

from farna.api.mcp_server import query_analytics
from farna.core.mcp.scope_validator import MCPAuthorizationError
from tests.unit.mcp.analytics_tool_harness import (
    PLACEHOLDER_ORG_ID,
    AuthContext,
    plan_context,
    service_stub,
)


class TestQueryAnalytics(AuthContext):
    @patch("farna.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await query_analytics()
        assert result["error"] == "auth_expired"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    async def test_insufficient_scope_denies_without_analytics_query(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        with patch(
            "farna.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await query_analytics()
        assert result["error"] == "insufficient_scope"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    async def test_feature_gate_off_returns_feature_required(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        with plan_context(enabled=False):
            result = await query_analytics()
        assert result["error"] == "feature_required"
        assert "analytics_page" in result["detail"]

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_enum_returns_invalid_params(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        with plan_context():
            result = await query_analytics(group_by="fortnight")
        assert result["error"] == "invalid_params"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_pipeline_id_returns_invalid_params(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        with plan_context():
            result = await query_analytics(pipeline_id=["not-a-uuid"])
        assert result["error"] == "invalid_params"
        assert "pipeline_id" in result["detail"]

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    async def test_success_passes_typed_params_to_service(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        canned = {
            "group_by": "day",
            "dimension": "error_code",
            "date_from": "2026-08-01T00:00:00+00:00",
            "date_to": "2026-08-07T23:59:59+00:00",
            "buckets": [{"date": "2026-08-07", "key": "executor_stalled", "count": 1, "stall_count": 1}],
        }
        pid_a = uuid.uuid4()
        pid_b = uuid.uuid4()
        with service_stub("run_analytics_query", return_value=canned) as mock_run:
            result = await query_analytics(
                dimension="error_code",
                pipeline_id=[str(pid_a), str(pid_b)],
                error_code="executor_stalled",
                group_by="day",
            )
        assert result["group_by"] == canned["group_by"]
        assert result["dimension"] == canned["dimension"]
        assert result["buckets"] == canned["buckets"]
        assert "deep_link" in result
        assert result["deep_link"].startswith("/analytics?")
        assert "group_by=day" in result["deep_link"]
        assert "dimension=error_code" in result["deep_link"]
        assert str(pid_a) in result["deep_link"]
        assert str(pid_b) in result["deep_link"]
        assert "error_code=executor_stalled" in result["deep_link"]
        assert "date_from=2026-08-01T00%3A00%3A00%2B00%3A00" in result["deep_link"]
        mock_run.assert_awaited_once()
        kwargs = mock_run.await_args.kwargs
        params = kwargs["params"]
        assert kwargs["org_id"] == PLACEHOLDER_ORG_ID
        assert params.pipeline_ids == (pid_a, pid_b)
        assert params.error_code == "executor_stalled"
        assert params.dimension is not None
        assert params.dimension.value == "error_code"
        assert params.group_by is not None
        assert params.group_by.value == "day"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    async def test_trigger_id_dimension_is_accepted_and_carried_in_deep_link(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """FAR-1421: ``dimension=trigger_id`` groups the series per trigger.

        The dimension must parse (a raw string that is not an
        ``AnalyticsDimension`` member is rejected as ``invalid_params``) and
        survive into the /analytics deep link like every other dimension.
        """
        canned = {
            "group_by": "day",
            "dimension": "trigger_id",
            "date_from": "2026-08-01T00:00:00+00:00",
            "date_to": "2026-08-07T23:59:59+00:00",
            "buckets": [
                {
                    "date": "2026-08-07",
                    "key": "99999999-9999-4999-8999-999999999999",
                    "count": 1,
                    "avg_dispatch_latency_ms": 1234.5,
                }
            ],
        }
        with service_stub("run_analytics_query", return_value=canned) as mock_run:
            result = await query_analytics(dimension="trigger_id", group_by="day")
        assert "error" not in result, "dimension=trigger_id must parse, not fall through to invalid_params"
        assert result["dimension"] == "trigger_id"
        assert "dimension=trigger_id" in result["deep_link"]
        params = mock_run.await_args.kwargs["params"]
        assert params.dimension is not None
        assert params.dimension.value == "trigger_id"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    async def test_rate_limited_maps_to_error_dict(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from farna.core.analytics.service import AnalyticsRateLimitedError

        with service_stub("run_analytics_query", side_effect=AnalyticsRateLimitedError("Rate limit exceeded")):
            result = await query_analytics()
        assert result["error"] == "rate_limited"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_folder_id_returns_invalid_params(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        with plan_context(service_ready=True):
            result = await query_analytics(folder_id="not-a-uuid")
        assert result["error"] == "invalid_params"
        assert "folder_id" in result["detail"]

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    async def test_invalid_date_returns_invalid_params(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        with plan_context(service_ready=True):
            result = await query_analytics(date_from="not-a-date")
        assert result["error"] == "invalid_params"
        assert "date_from" in result["detail"]

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    async def test_date_params_parse_to_aware_datetimes(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        canned = {"group_by": "day", "dimension": None, "date_from": None, "date_to": None, "buckets": []}
        with service_stub("run_analytics_query", return_value=canned) as mock_run:
            result = await query_analytics(date_from="2026-08-01", date_to="2026-08-05T14:00:00Z")
        assert result["group_by"] == canned["group_by"]
        assert result["dimension"] == canned["dimension"]
        assert result["date_from"] == canned["date_from"]
        assert result["date_to"] == canned["date_to"]
        assert result["buckets"] == canned["buckets"]
        assert result["deep_link"] == "/analytics?group_by=day"
        params = mock_run.await_args.kwargs["params"]
        assert params.date_from is not None
        assert params.date_from.day == 1
        assert params.date_to is not None
        assert params.date_to.tzinfo is not None, "an ISO datetime keeps its offset"
        assert params.date_to.hour == 14, "an ISO datetime must be parsed, not treated as a bare date"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    async def test_limit_is_clamped_to_query_cap(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        canned = {"group_by": "day", "buckets": []}
        with service_stub("run_analytics_query", return_value=canned) as mock_run:
            result = await query_analytics(limit=5000)
        assert result["group_by"] == "day"
        assert not result["buckets"]
        assert result["deep_link"] == "/analytics?group_by=day"
        assert mock_run.await_args.kwargs["params"].limit == 1000, "limit must be clamped to the REST cap of 1000"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    async def test_validation_error_maps_to_invalid_params(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from farna.core.analytics.service import AnalyticsValidationError

        with service_stub("run_analytics_query", side_effect=AnalyticsValidationError("date range must be 365 days or less")):
            result = await query_analytics()
        assert result["error"] == "invalid_params"
        assert "365 days" in result["detail"]

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    async def test_query_timeout_maps_to_query_timeout(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from farna.core.analytics.service import AnalyticsQueryTimeoutError

        with service_stub("run_analytics_query", side_effect=AnalyticsQueryTimeoutError("query exceeded timeout")):
            result = await query_analytics()
        assert result["error"] == "query_timeout"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    async def test_migration_required_maps_to_migration_required(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from farna.core.analytics.service import AnalyticsMigrationRequiredError

        with service_stub("run_analytics_query", side_effect=AnalyticsMigrationRequiredError("Feature is not available. Run database migrations.")):
            result = await query_analytics()
        assert result["error"] == "migration_required"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    async def test_database_error_maps_to_database_error(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from farna.core.analytics.service import AnalyticsDatabaseError

        with service_stub("run_analytics_query", side_effect=AnalyticsDatabaseError("Database temporarily unavailable.")):
            result = await query_analytics()
        assert result["error"] == "database_error"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    async def test_programming_error_maps_to_migration_required(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from sqlalchemy.exc import ProgrammingError

        with service_stub("run_analytics_query", side_effect=ProgrammingError("SELECT ...", {}, RuntimeError("no such table"))):
            result = await query_analytics()
        assert result["error"] == "migration_required"

    @patch("farna.api.mcp_server.validate_current_auth", return_value=True)
    async def test_unexpected_error_maps_to_internal_error(
        self,
        mock_validate_auth: AsyncMock,
    ) -> None:
        with service_stub("run_analytics_query", side_effect=RuntimeError("boom")):
            result = await query_analytics()
        assert result["error"] == "server_error"
