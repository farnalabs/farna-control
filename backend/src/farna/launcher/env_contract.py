"""Single source of truth for the launcher's service-contract environment names.

The product was renamed ``modulo`` -> Farna Control. Every environment variable
that crosses a producer/consumer boundary therefore has a canonical
``FARNA_CONTROL_*`` name and a legacy ``MODULO_*`` alias. Three separate
incidents were the same defect: one side was renamed and the other was not, so
the value was silently written and never read.

* FAR-2554 - the native launchers exported ``MODULO_BUNDLED_BIN_DIR`` while the
  resolver read ``FARNA_CONTROL_BUNDLED_BIN_DIR``, so the boot fell through to
  a directory no bundle produces.
* FAR-2560/FAR-2561 - two ``APP_DB_NAME`` constants, so the app connected to a
  database that first boot never created.
* FAR-2563 - the macOS bundle launcher exported ``MODULO_SERVE_SPA`` /
  ``MODULO_FRONTEND_DIST`` and the Windows bundled launcher exported NEITHER
  name, while :mod:`farna.api.main` read ``FARNA_CONTROL_SERVE_SPA`` /
  ``FARNA_CONTROL_FRONTEND_DIST``. On macOS the built SPA was therefore never
  mounted and ``/`` answered 404; on Windows single-port SPA serving did not
  exist at all, and this module's Windows producer adds it.
  ``.github/workflows/windows-artifacts.yml`` compiles that launcher on every
  push to ``main`` that touches it, on a weekly schedule and on dispatch, so the
  C does get compiled in CI. What the default Alpha release path does NOT do is
  package or boot the Windows bundle: ``release-unified.yml`` runs
  ``windows-native-artifacts`` / ``windows-installer`` only when
  ``include_windows=true``.
* FAR-2563 (second half) - ``MODULO_LAN_ORIGINS`` was documented as the
  operator knob that widens the loopback allowlist, but the middleware read
  ``FARNA_CONTROL_LAN_ORIGINS`` directly, so a self-hoster who followed the
  docs got a silent 403. Every name a reader takes must live in this registry
  and be read through :func:`read_env`; a documented spelling that no reader
  accepts is a defect, not documentation.

The names below are the ONE definition. Consumers read through :func:`read_env`
so an already-installed bundle that still exports the legacy spelling keeps
working across an app upgrade; producers must write the canonical name, and
``backend/tests/unit/launcher/test_env_contract.py`` fails when a service
script writes or reads a ``MODULO_`` name without its canonical counterpart in
the same file, or when a Python reader takes a registered name any way other
than through :func:`read_env`. The macOS bundle additionally enforces the SPA
pair at BUILD time (``scripts/macos/build-native-bundle.sh``), mirroring the
FAR-2554 gate; the Windows launcher has no build-time CONTRACT gate, so the
anchored C sweep in that test module is what pins its names - the CI compile in
``windows-artifacts.yml`` proves the C compiles, not that its names agree with
the Python readers.

Deliberately a leaf module: no project imports and no environment reads at
import time, so it is cheap and side-effect free for any caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

CANONICAL_PREFIX: Final = "FARNA_CONTROL_"
LEGACY_PREFIX: Final = "MODULO_"

#: Serve the built SPA from the API port (native single-port install).
SERVE_SPA_ENV: Final = "FARNA_CONTROL_SERVE_SPA"
#: Directory holding the built SPA (``index.html`` + ``assets/``).
FRONTEND_DIST_ENV: Final = "FARNA_CONTROL_FRONTEND_DIST"
#: The ``farna_control_system`` role URL used by cross-org system crons.
SYSTEM_DATABASE_URL_ENV: Final = "FARNA_CONTROL_SYSTEM_DATABASE_URL"
#: Comma-separated LAN hostnames the Host/Origin allowlist accepts when the API
#: serves the SPA itself. ``MODULO_LAN_ORIGINS`` is the spelling
#: ``docs/configuration-reference.md`` publishes, so the alias is load-bearing:
#: registering it is what makes the documented knob work.
LAN_ORIGINS_ENV: Final = "FARNA_CONTROL_LAN_ORIGINS"
#: Native install root / shim directory / bundled data directory.
INSTALL_ROOT_ENV: Final = "FARNA_CONTROL_INSTALL_ROOT"
INSTALL_BIN_DIR_ENV: Final = "FARNA_CONTROL_BIN_DIR"
DATA_DIR_ENV: Final = "FARNA_CONTROL_DATA_DIR"
#: Path of the legacy ``/usr/local/bin/modulo`` shim the installer renames aside.
LEGACY_SHIM_PATH_ENV: Final = "FARNA_CONTROL_LEGACY_SHIM_PATH"

#: Canonical name -> the legacy spellings a READER must still accept.
#:
#: Order matters only in that the canonical name is always tried first; the
#: legacy names are a compatibility fallback, never a preference.
#:
#: ``FARNA_CONTROL_SYSTEM_DATABASE_URL`` is the one entry where the reader is
#: deliberately NOT :func:`read_env`: ``farna.db.bootstrap_role`` reads the
#: canonical name only, because an ambient legacy value from an earlier
#: process or configuration layer would reset an existing system-role password
#: on a boot with no explicit system URL (FAR-1519). Its producer must therefore
#: export the canonical name - see ``deploy/fly/entrypoint.sh`` - which is why
#: the alias is registered here for the settings-level readers while the
#: bootstrap reader stays single-name.
LEGACY_ALIASES: Final[Mapping[str, tuple[str, ...]]] = {
    SERVE_SPA_ENV: ("MODULO_SERVE_SPA",),
    FRONTEND_DIST_ENV: ("MODULO_FRONTEND_DIST",),
    SYSTEM_DATABASE_URL_ENV: ("MODULO_SYSTEM_DATABASE_URL",),
    LAN_ORIGINS_ENV: ("MODULO_LAN_ORIGINS",),
    INSTALL_ROOT_ENV: ("MODULO_INSTALL_ROOT",),
    INSTALL_BIN_DIR_ENV: ("MODULO_BIN_DIR",),
    DATA_DIR_ENV: ("MODULO_DATA_DIR",),
    LEGACY_SHIM_PATH_ENV: ("MODULO_LEGACY_SHIM_PATH",),
}

#: Service-script ``MODULO_*`` names that intentionally have NO canonical
#: counterpart. Each entry states why; the sweep gate treats them as a pass.
#: Anything else that a producer writes must be paired with its canonical name.
#:
#: ``MODULO_BUNDLED_BIN_DIR`` is NOT listed: it is owned by
#: :data:`farna.launcher.entry.BUNDLED_BIN_DIR_ENV` (FAR-2554), which cannot
#: import this leaf module without breaking the launcher's scrub-first import
#: order. Its own producer/consumer gate lives in
#: ``test_macos_installer_normalize.py``.
LEGACY_ONLY_EXEMPT: Final[Mapping[str, str]] = {
    "MODULO_MACOS_REHEARSAL_ALLOW_UNSTAPLED_TICKET": ("rehearsal-only installer knob set by .github/workflows/macos-installer.yml and read only by scripts/macos/install.sh; it never reaches the app, so it has no service contract"),
    "MODULO_MACOS_REHEARSAL_ALLOW_UNSTAPLED_TICKET_REASON": ("companion value for MODULO_MACOS_REHEARSAL_ALLOW_UNSTAPLED_TICKET; same rehearsal-only scope"),
    "MODULO_SMOKE_EMAIL": "docker-compose smoke-test knob in scripts/smoke-compose.sh; test-only",
    "MODULO_SMOKE_PASSWORD": "docker-compose smoke-test knob in scripts/smoke-compose.sh; test-only",
    "MODULO_SELF_REPORT_URL": "CI self-report destination set and read inside one workflow step",
    "MODULO_SELF_REPORT_TOKEN": "CI self-report credential set and read inside one workflow step",
    "MODULO_DEPLOY_DRAIN_TOKEN": "CI deploy-drain credential set and read inside one workflow step",
    "MODULO_RELEASE_PR_TOKEN": "CI release-PR credential set and read inside one workflow step",
}


def candidate_names(canonical: str) -> tuple[str, ...]:
    """Return ``canonical`` followed by every legacy alias a reader accepts."""
    return (canonical, *LEGACY_ALIASES.get(canonical, ()))


def read_env(source: Mapping[str, str], canonical: str, default: str = "") -> str:
    """Read ``canonical`` (then its legacy aliases) from ``source``.

    Returns the first non-empty value, or ``default``. An empty value is
    treated as unset so a producer that exports ``NAME=""`` cannot shadow a
    populated legacy alias.
    """
    for name in candidate_names(canonical):
        value = source.get(name, "")
        if value:
            return value
    return default
