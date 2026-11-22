"""Architecture test: the MCP/WS payload surfaces carry no generic
``internal_error`` code (FAR-1502).

The consuming agent branches on the ``error`` code, and ``internal_error``
names no failure mode. The domain rule (specific, branchable codes only —
``invalid_id``, ``auth_expired``, ``session_contract_error``; ``server_error``
reserved for the truly-unexpected) is enforced here structurally rather than
by prose: a string/AST scan of the payload-producing source files, so a
new call site cannot silently reintroduce the generic code.

Scope: the WHOLE MCP surface — ``api/mcp_server.py`` plus every extracted
``api/mcp_tools/*.py`` domain (the tool/resource handlers were split out of
``mcp_server.py`` for structural hardening) — and ``api/routes/run_ws.py``
(the run WebSocket's control frames). Other REST surfaces use their own
vocabularies (``problem.py``'s ``INTERNAL_ERROR``, ``mcp_setup``'s REST
detail) and are out of scope for this rule.
"""

from __future__ import annotations

import ast
from pathlib import Path

_API_ROOT = Path(__file__).resolve().parent.parent.parent / "src" / "farna" / "api"
_MCP_TOOLS_ROOT = _API_ROOT / "mcp_tools"
#: Every module that can emit an MCP tool/resource payload.
_MCP_SOURCES = (_API_ROOT / "mcp_server.py", *sorted(_MCP_TOOLS_ROOT.glob("*.py")))
_SOURCES = (*_MCP_SOURCES, _API_ROOT / "routes" / "run_ws.py")


def test_no_generic_internal_error_payload_code() -> None:
    """Neither payload-producing source may emit an ``internal_error`` code."""
    for path in _SOURCES:
        source = path.read_text(encoding="utf-8")
        for quote in ('"internal_error"', "'internal_error'"):
            assert quote not in source, f"{path.relative_to(_API_ROOT.parent.parent)} still carries the generic {quote} payload — use a specific, branchable code (FAR-1502)."


def test_tool_error_code_parameter_is_required() -> None:
    """``_tool_error`` must not offer a default code: every call site names one.

    Scans the whole MCP surface so the single-definition invariant holds across
    ``api/mcp_server.py`` and the extracted ``api/mcp_tools/*.py`` domains (a
    second ``_tool_error`` — the shadowing copy this guard exists to prevent —
    fails here wherever it is defined).
    """
    defs: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for path in _MCP_SOURCES:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        defs.extend(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "_tool_error")
    assert len(defs) == 1, f"expected exactly one _tool_error definition across the MCP surface, found {len(defs)}"
    args = defs[0].args
    kw_only = [arg.arg for arg in args.kwonlyargs]
    assert "code" in kw_only, "_tool_error's `code` must be keyword-only so call sites must name it"
    assert args.kw_defaults[kw_only.index("code")] is None, "_tool_error's `code` must have no default"
