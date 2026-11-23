"""Structural gate: ``runtime-config.js`` is never served from a stale cache.

The file is regenerated at container start and publishes a global whose name was
rebranded (the pre-rebrand global -> ``__FARNA_CONFIG__``). A static-asset rule
serves ``.js`` as ``public, immutable`` for a year from an unchanged URL, so a
returning client would keep the OLD script (which sets the OLD global) and the
new SPA would silently lose auto-login and monitor config. Two halves close
that, and both are asserted here:

* an exact-match location answering ``/runtime-config.js`` with ``no-store`` -
  an exact match beats the static-extension regex regardless of file order;
* a version query in ``frontend/index.html`` so an existing immutable cache
  entry is bypassed by the URL itself.

Every shipped nginx source is listed below - all three static confs, the Helm
ConfigMap's inline config and the production Dockerfile's heredoc - because a
fixed list that omits one certifies a deployment that still has the defect.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: Every place a shipped nginx server block is defined. Add a new one here.
_NGINX_CONFIGS: tuple[str, ...] = (
    "deploy/fly/nginx.conf",
    "deploy/nginx/all-in-one.conf",
    "deploy/nginx/default.conf",
    "deploy/helm/farna-control/templates/frontend-nginx-configmap.yaml",
    "frontend/Dockerfile.prod",
)

#: The configs that also carry the long-lived static-asset rule. The production
#: Dockerfile deliberately does not (every asset is fetched with default
#: caching there), so the "hashed assets stay immutable" check does not apply.
_STATIC_CACHE_CONFIGS: tuple[str, ...] = (
    "deploy/fly/nginx.conf",
    "deploy/nginx/all-in-one.conf",
    "deploy/nginx/default.conf",
    "deploy/helm/farna-control/templates/frontend-nginx-configmap.yaml",
)

_EXACT_LOCATION = "location = /runtime-config.js"
_INDEX_HTML = _REPO_ROOT / "frontend" / "index.html"


def _location_block(text: str) -> str:
    """The exact-match runtime-config block, from its directive to its brace.

    The block holds no nested braces, so the first ``}`` after the directive
    ends it.
    """
    start = text.find(_EXACT_LOCATION)
    assert start != -1, f"missing `{_EXACT_LOCATION}`"
    end = text.find("}", start)
    assert end != -1, f"unterminated `{_EXACT_LOCATION}` block"
    return text[start:end]


@pytest.mark.parametrize("relative_path", _NGINX_CONFIGS)
def test_runtime_config_has_an_exact_match_no_store_location(relative_path: str) -> None:
    block = _location_block((_REPO_ROOT / relative_path).read_text(encoding="utf-8"))

    assert "no-store" in block, block
    assert "try_files $uri =404" in block, block


@pytest.mark.parametrize("relative_path", _STATIC_CACHE_CONFIGS)
def test_the_static_asset_rule_still_caches_hashed_assets(relative_path: str) -> None:
    """The exact match must not have replaced the long cache for every asset."""
    text = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")

    assert "public, immutable" in text


def test_index_html_versions_the_runtime_config_url() -> None:
    html = _INDEX_HTML.read_text(encoding="utf-8")

    assert 'src="/runtime-config.js?v=' in html
