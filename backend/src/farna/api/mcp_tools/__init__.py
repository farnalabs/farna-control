"""Extracted MCP tool/resource domains.

Each module in this package registers its handlers on the ``FastMCP`` app
instance created in ``farna.api.mcp_server``, at import time, with the same
``@mcp.tool`` / ``@mcp.resource`` decorators, signatures, defaults and
docstrings the handlers had before the split -- so the advertised schema is
byte-identical.

Nothing is imported eagerly here. ``farna.api.mcp_server`` imports each domain
module at the END of its own body (after ``mcp`` and the shared spine exist) and
re-exports the names the rest of the tree still reaches through it; a bare
``import farna.api.mcp_tools`` must therefore not start a cycle.

Shared helpers that the suite patches by module path are reached through
``_patch_surface`` so the interception stays live from this package.
"""
