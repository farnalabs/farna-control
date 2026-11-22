"""Single source of truth for the launcher's service-contract environment names.

Every environment variable that crosses a producer/consumer boundary carries the
one canonical ``FARNA_CONTROL_*`` name. There is no legacy alias: the product
ships to exactly two first-party installs (staging and production), so a
rename is a clean cutover rather than a compatibility shim.

The names live here because three separate incidents were the same defect: one
side of a producer/consumer boundary was renamed and the other was not, so a
value was written and never read.

* FAR-2554 - the native launchers exported the pre-rebrand bin-dir name while the
  resolver read the canonical ``FARNA_CONTROL_BUNDLED_BIN_DIR``, so the boot fell
  through to a directory no bundle produces.
* FAR-2560/FAR-2561 - two ``APP_DB_NAME`` constants, so the app connected to a
  database that first boot never created.
* FAR-2563 - the macOS bundle launcher exported the pre-rebrand SPA-serving
  spelling and the Windows bundled launcher exported NEITHER name, while
  :mod:`farna.api.main` read the canonical ``FARNA_CONTROL_SERVE_SPA`` /
  ``FARNA_CONTROL_FRONTEND_DIST``. On macOS the built SPA was never mounted
  and ``/`` answered 404; on Windows single-port SPA serving did not exist at
  all, and this module's Windows producer adds it.
  ``.github/workflows/windows-artifacts.yml`` compiles that launcher on every
  push to ``main`` that touches it, on a weekly schedule and on dispatch, so the
  C does get compiled in CI. What the default Alpha release path does NOT do is
  package or boot the Windows bundle: ``release-unified.yml`` runs
  ``windows-native-artifacts`` / ``windows-installer`` only when
  ``include_windows=true``.
* FAR-2563 (second half) - the LAN-origins knob was documented under a
  non-canonical spelling that the middleware did not read, so a self-hoster who
  followed the docs got a silent 403. Every name a reader takes must live in
  this registry and be read through :func:`read_env`; a documented spelling that
  no reader accepts is a defect, not documentation.

``backend/tests/unit/launcher/test_env_contract.py`` is the structural gate: a
service script that writes or reads a non-canonical name fails there, as does a
Python reader that takes a registered name any way other than through
:func:`read_env`. The macOS bundle additionally enforces the SPA pair at BUILD
time (``scripts/macos/build-native-bundle.sh``), mirroring the FAR-2554 gate;
the Windows launcher has no build-time CONTRACT gate, so the anchored C sweep in
that test module is what pins its names - the CI compile in
``windows-artifacts.yml`` proves the C compiles, not that its names agree with
the Python readers.

Deliberately a leaf module: no project imports and no environment reads at
import time, so it is cheap and side-effect free for any caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

CANONICAL_PREFIX: Final = "FARNA_CONTROL_"

#: Serve the built SPA from the API port (native single-port install).
SERVE_SPA_ENV: Final = "FARNA_CONTROL_SERVE_SPA"
#: Directory holding the built SPA (``index.html`` + ``assets/``).
FRONTEND_DIST_ENV: Final = "FARNA_CONTROL_FRONTEND_DIST"
#: The ``farna_control_system`` role URL used by cross-org system crons.
SYSTEM_DATABASE_URL_ENV: Final = "FARNA_CONTROL_SYSTEM_DATABASE_URL"
#: Comma-separated LAN hostnames the Host/Origin allowlist accepts when the API
#: serves the SPA itself.
LAN_ORIGINS_ENV: Final = "FARNA_CONTROL_LAN_ORIGINS"
#: Native install root / shim directory / bundled data directory.
INSTALL_ROOT_ENV: Final = "FARNA_CONTROL_INSTALL_ROOT"
INSTALL_BIN_DIR_ENV: Final = "FARNA_CONTROL_BIN_DIR"
DATA_DIR_ENV: Final = "FARNA_CONTROL_DATA_DIR"
#: Path of the legacy ``/usr/local/bin/modulo`` shim the installer renames aside.
LEGACY_SHIM_PATH_ENV: Final = "FARNA_CONTROL_LEGACY_SHIM_PATH"


def candidate_names(canonical: str) -> tuple[str, ...]:
    """Return the single canonical spelling for ``canonical``.

    Kept as a function (rather than inlining ``(canonical,)``) so every reader
    goes through the one place that defines the name set; there is no second
    spelling to fall back to.
    """
    return (canonical,)


def read_env(source: Mapping[str, str], canonical: str, default: str = "") -> str:
    """Read the canonical ``FARNA_CONTROL_*`` name from ``source``.

    Returns the value, or ``default`` when it is unset or empty.
    """
    for name in candidate_names(canonical):
        value = source.get(name, "")
        if value:
            return value
    return default
