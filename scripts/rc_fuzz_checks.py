"""Schemathesis extension that applies the RC fuzz status policy (FAR-2800).

Schemathesis loads this module because the fuzz step exports
``SCHEMATHESIS_HOOKS`` pointing at it (its documented extension hook). Importing
it registers exactly one check, ``status_policy``, which classifies every GET
response through :func:`rc_fuzz_policy.classify` and raises a failure only for a
``finding``.

Why a custom check at all: the built-in ``status_code_conformance`` check treats
every status the OpenAPI document does not enumerate as a failure, so on this API
it reported the correct 401/402 refusals as defects - 434 of them on the first
real run. The two passes also need the SAME operation judged differently (401
expected without a credential, a bug with one), which no single built-in check can
express. This check therefore replaces ``status_code_conformance`` (the fuzz step
excludes it) while every other built-in check keeps running.

Inputs, all from the environment so nothing travels on a command line:

* ``RC_FUZZ_PASS`` - ``unauthenticated`` or ``authenticated``; validated by
  :mod:`rc_fuzz_reporting`, which aborts when it is missing or unknown so a run
  can never proceed unpoliced.
* ``FUZZ_JWT`` - the seeded account's token. Used ONLY to decide whether the exact
  credential was presented, and never printed or persisted.

State lives in :mod:`rc_fuzz_reporting` because Schemathesis re-executes this
module several times per run; see that module for the measurement.
"""

from __future__ import annotations

import hmac
import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import rc_fuzz_reporting
import schemathesis
from rc_fuzz_policy import Verdict, classify

_PASS = rc_fuzz_reporting.PASS
_TOKEN = os.environ.get("FUZZ_JWT", "")


def _presented_authorization(response: object) -> str:
    """The Authorization header this response's request actually carried, if any."""
    request = getattr(response, "request", None)
    headers = getattr(request, "headers", None)
    if not headers:
        return ""
    for name, value in headers.items():
        if str(name).lower() == "authorization":
            return str(value)
    return ""


def credential_sent(response: object, token: str) -> bool:
    """Whether the request presented ``token`` EXACTLY.

    A mutated or absent header is not the seeded credential: the coverage phase
    strips and rewrites the header on purpose, so a 401 for it is the correct
    answer to a request that carried no valid credential, not an authorisation bug.
    """
    presented = _presented_authorization(response)
    if not presented or not token:
        return False
    return hmac.compare_digest(presented, f"Bearer {token}")


@schemathesis.check
def status_policy(ctx: object, response: object, case: object) -> bool | None:
    """Apply :func:`rc_fuzz_policy.classify` to one response."""
    # The leg fuzzes GET operations. The coverage phase additionally probes HTTP
    # methods the specification does not declare; those answers are
    # `unsupported_method`'s business (it fails a 405 that omits the RFC 9110
    # `Allow` header - measured: 271 such answers on v0.3.330-rc3), and judging
    # them by a GET status policy as well would only double-report them.
    if str(getattr(case, "method", "") or "").upper() != "GET":
        return None
    status_code = int(response.status_code)
    presented = credential_sent(response, _TOKEN)
    verdict = classify(_PASS, status_code, credential_sent=presented)
    operation = str(getattr(getattr(case, "operation", None), "label", "<unknown operation>"))
    rc_fuzz_reporting.record(
        operation=operation,
        status_code=status_code,
        credential_sent=presented,
        is_finding=verdict is Verdict.FINDING,
    )
    if verdict is Verdict.FINDING:
        credential_state = "the seeded credential was presented" if presented else "no credential was presented"
        raise AssertionError(f"{operation} answered {status_code} in the {_PASS} pass and {credential_state}. The fuzz status policy treats that as a finding.")
    return None
