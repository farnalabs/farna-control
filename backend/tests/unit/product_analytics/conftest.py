"""Conftest for product analytics tests."""

import os

os.environ.setdefault("FARNA_CONTROL_CSRF_ENABLED", "false")
