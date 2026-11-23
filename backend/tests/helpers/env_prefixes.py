"""Assembled spellings for the retired pre-rebrand environment prefix.

The prefix is assembled from fragments rather than written literally, so the
repo-wide source sweep that forbids it (``test_settings_environment_aliases``)
does not match the guards themselves. One construction lives here; every test
that needs the retired spelling imports it instead of re-deriving it.
"""

from __future__ import annotations

#: The retired product name, without the environment-variable separator.
RETIRED_BRAND = "MOD" + "ULO"
#: ``MODULO_`` - a bare pre-rebrand environment name.
RETIRED_ENV_PREFIX = RETIRED_BRAND + "_"
#: ``FARNA_CONTROL_MODULO_`` - the doubled form that is never valid.
RETIRED_DOUBLED_ENV_PREFIX = "FARNA_CONTROL_" + RETIRED_ENV_PREFIX


def retired_env_name(suffix: str) -> str:
    """Return the pre-rebrand spelling of one environment name.

    ``retired_env_name("SERVE_SPA")`` is ``MODULO_SERVE_SPA``, assembled so a
    source sweep never sees the literal prefix.
    """
    return RETIRED_ENV_PREFIX + suffix
