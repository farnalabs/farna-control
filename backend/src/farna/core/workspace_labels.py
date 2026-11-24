"""Provider-neutral workspace label/annotation keys (FAR-2693).

Farna Control stamps a small vocabulary of keys onto every ephemeral
workspace it creates — Docker container labels, Kubernetes pod labels and
annotations, and E2B sandbox metadata — so the workspace-orphan reconciler
(:mod:`farna.core.bundled_runner.runner_reconciler`) can identify this
deployment's workspaces and reap the ones that outlived their run. The
vocabulary historically carried the product's retired name as its prefix
(see FAR-2641); the current prefix is ``farna-control`` and every key keeps
its dot-separated shape, so the change is a mechanical prefix substitution.

Dual-write transition (FAR-2693)
--------------------------------
These keys are written onto LIVE workspaces and read back to reap orphans,
so the prefix cannot be switched in one release. A one-sided rename would
leave every workspace created before the change unrecognisable to the
reconciler — never torn down, leaking sandboxes and cost. Therefore, for the
transition:

* every writer stamps BOTH the current key and its legacy counterpart
  (:func:`stamp`), and
* every reader prefers the current key and falls back to the legacy one
  (:func:`preferred`), everywhere a key is read.

Removal note (FAR-2693)
-----------------------
The legacy fallbacks and the legacy write may be dropped once no live
workspace can predate the dual-write release. That point is the longest
workspace lifetime plus the reconciler's grace window — the reconciler's
24-hour max-lifetime backstop plus its 5-minute grace period — plus a
margin, i.e. once the dual-write release has been deployed for longer than a
day and every pre-existing workspace is gone. At that point remove the
``_LEGACY_*`` constants in the writer/reader modules and the ``legacy_key=``
arguments here. Until then, dropping either half strands live workspaces.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping

#: Current key prefix. A plain literal prefix (not an environment toggle):
#: the scheme is fixed, and a configurable prefix would reintroduce exactly
#: the unrecognised-workspace hazard this transition exists to avoid.
PREFIX = "farna-control"

#: The run a workspace was created for (stamped at dispatch).
RUN_ID = f"{PREFIX}.run.id"
#: The organisation the run belongs to (stamped at dispatch).
ORG_ID = f"{PREFIX}.org.id"
#: The pipeline node the workspace was created for (stamped at dispatch).
NODE_ID = f"{PREFIX}.node.id"
#: The deployment identity scoping machine-scoped reconciler filters.
MACHINE_ID = f"{PREFIX}.machine.id"
#: Epoch-seconds creation marker driving the sweep's grace/lifetime ages.
CREATED_AT = f"{PREFIX}.created_at"
#: The runtime tier that owns a workspace (Kubernetes stamps it).
PROVIDER = f"{PREFIX}.provider"
#: The typed egress posture resolved for the dispatch (FAR-1690).
EGRESS_POSTURE = f"{PREFIX}.egress.posture"


def preferred(mapping: Mapping[str, str], key: str, legacy_key: str, default: str = "") -> str:
    """Read *key*, falling back to *legacy_key* (FAR-2693 dual-read).

    The current key wins when it carries a value. Only an absent or empty
    current value falls through to the legacy key, so an operator-supplied
    current key can never be rescued by a stale legacy value. Returns
    *default* when neither key carries a value.
    """
    value = mapping.get(key)
    if value:
        return value
    return mapping.get(legacy_key) or default


def stamp(mapping: MutableMapping[str, str], key: str, legacy_key: str, value: str) -> None:
    """Write *value* under both *key* and *legacy_key* (FAR-2693 dual-write).

    Assignment (never ``setdefault``): a reserved identity value is the
    provider's own fact and must win over caller-supplied metadata.
    """
    mapping[key] = value
    mapping[legacy_key] = value
