"""Version info.

Resolution order (FAR-2581):

1. A native bundle's own stamp. ``bundle-release.yml`` writes
   ``<bundle>/VERSION`` (``bundle-v<X.Y.Z>``) beside the ``launcher`` wrapper,
   and this module ships at ``<bundle>/backend/src/farna/version.py``. That
   file IS the release identity: the installed ``farna-control`` dist-info in
   the bundled venv carries whatever ``backend/pyproject.toml`` said at the
   built commit, which for a dry-run (built from main before the release
   bump) is the PREVIOUS release — the 0.3.329 dry-run bundle reported
   0.3.328.
   The macOS app carries the same stamp (FAR-2589): its bundle root is
   ``<App>.app/Contents/Resources/bundle`` (``VERSION`` and ``backend/src``
   both live there) and its launcher is the compiled
   ``Contents/MacOS/<exe>`` shim, so the bundle marker there is the app's
   ``Contents/Info.plist`` rather than a ``launcher`` file.
2. The installed package metadata (wheel / container / dev checkout).
3. ``"0.0.0-dev"`` when nothing is installed.
"""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

PROGRAM_NAME = "farna-control"
_BUNDLE_VERSION_PREFIX = "bundle-v"
# <bundle>/backend/src/farna/version.py -> parents[3] is <bundle>.
_BUNDLE_ROOT_DEPTH = 3


def _looks_like_bundle(root: Path) -> bool:
    """True for a Linux bundle root or a macOS app's ``Resources/bundle``."""
    if (root / "launcher").is_file():
        return True
    # <App>.app/Contents/Resources/bundle: the app's Info.plist is the marker.
    return root.name == "bundle" and root.parent.name == "Resources" and (root.parent.parent / "Info.plist").is_file()


def _bundle_version(module_file: str = __file__) -> str | None:
    """Return the enclosing native bundle's stamped version, or None.

    Only a directory that looks like a bundle (a ``launcher`` wrapper, or a
    macOS app's ``Contents/Resources/bundle``, holding a ``VERSION`` file
    carrying the ``bundle-v`` prefix) counts, so a dev checkout or a container
    image never picks up a stray file.
    """
    try:
        root = Path(module_file).resolve().parents[_BUNDLE_ROOT_DEPTH]
        stamp_file = root / "VERSION"
        if not _looks_like_bundle(root) or not stamp_file.is_file():
            return None
        stamp = stamp_file.read_text(encoding="utf-8").strip()
    except (IndexError, OSError, UnicodeDecodeError):
        return None
    if not stamp.startswith(_BUNDLE_VERSION_PREFIX):
        return None
    value = stamp[len(_BUNDLE_VERSION_PREFIX) :]
    return value or None


def _resolve_version() -> str:
    bundled = _bundle_version()
    if bundled is not None:
        return bundled
    try:
        return version(PROGRAM_NAME)
    except PackageNotFoundError:
        return "0.0.0-dev"


__version__ = _resolve_version()


def get_version() -> str:
    """Return the running build's version (bundle stamp > package metadata)."""
    return __version__
