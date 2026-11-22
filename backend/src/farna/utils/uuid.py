"""Backward-compatibility shim -- prefer ``farna.util.uuid`` for new code."""

from farna.util.uuid import coerce_uuid

__all__ = ["coerce_uuid"]
