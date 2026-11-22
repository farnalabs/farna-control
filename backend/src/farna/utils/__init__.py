"""Backward-compatibility shim -- prefer ``farna.util`` for new code.

This package is deprecated; all utilities live in ``farna.util`` now.
"""

from farna.util.uuid import coerce_uuid

__all__ = ["coerce_uuid"]
