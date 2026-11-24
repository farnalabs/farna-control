# Product Analytics Licensing: `product_analytics_required`

**Audience:** licence issuers and self-hosting operators who mint free/partner
licence keys and need the instance's product-analytics switch to default ON.

**Prerequisite reading:**
- `docs/configuration-reference.md` – the `FARNA_CONTROL_PRODUCT_ANALYTICS_ENABLED` row, which documents the switch precedence.

---

## Why this exists

A partner or free licence can express the expectation that the instance sends
aggregate product analytics. The signed claim `product_analytics_required`
supplies the **default** for the instance-level analytics switch, so a fresh
instance with no explicit operator setting starts with analytics ON.

The claim is read from the signed licence payload only. It is **not** an
enforcement control: it moves the default, and an operator can always override
it (see [Limitation](#limitation-a-default-never-a-control)).

## The claim

`product_analytics_required` must be present in the signed payload and be either
boolean `true` or the string `"true"` (case-insensitive). Anything else —
`false`, `"false"`, a missing claim, or a malformed payload — reads as not
required. The reader is
`farna.core.product_analytics.consent.partner_license_requires_analytics`.

## Minting a key that carries it

The admin issue route (`POST /admin/license/issue`) and Stripe fulfilment both
call `license_signing.generate_team_license`, and neither `generate_team_license`
nor `build_team_payload` accepts a way to set `product_analytics_required`. A
key minted through either path therefore **cannot** carry the claim.

To mint one, assemble the full payload yourself and sign it with the issuer's
Ed25519 private key via `farna.core.license_signing.encode_license_key`:

```python
from farna.core.license_signing import encode_license_key

# Every standard claim the product emits, plus the analytics claim.
payload = {
    "tier": "team",
    "features": ["team_rbac", "sso", "audit_viewer"],
    "expires_at": "2027-11-24T00:00:00+00:00",   # timezone-aware, in the future
    "org_id": "<org uuid, or a stable id you assign>",
    "renewal_mode": "manual",                     # "auto" or "manual"
    "term": 12,                                   # months, the unit expires_at derives from
    "licence_id": "<opaque stable id for this licence>",
    "issued_at": "2026-11-24T00:00:00+00:00",    # true mint time, ISO 8601
    "product_analytics_required": True,           # boolean true, or the string "true"
}

private_key_hex = "<issuer Ed25519 private key, hex>"  # placeholder — never commit the real key
licence_key = encode_license_key(payload, private_key_hex)
print(licence_key)
```

Notes:

- `expires_at` should be timezone-aware (an ISO 8601 instant with an offset) and
  in the future. It must parse as a valid ISO 8601 instant, or `parse_and_verify`
  rejects the key as malformed. Two lenient behaviours are worth knowing: a
  **naive** `expires_at` (no offset) is read as local time rather than rejected,
  and the capability lapses at `expires_at + LICENCE_GRACE_DAYS` (14 days), not
  at `expires_at` itself, so a key dated slightly in the past still verifies as
  valid. Only `starts_at` is strictly required to be timezone-aware. Prefer an
  offset-aware future value for an unambiguous mint.
- The private key must be the half matching the deployment's trusted public
  anchor (`FARNA_CONTROL_LICENSE_PUBLIC_KEY`). A key signed by any other keypair
  fails signature verification at install time.
- The placeholder above is a stand-in. Load the real issuer key from your secret
  store; never hardcode or commit it.

## Verifying it took effect

Install the key by setting it as `FARNA_CONTROL_LICENSE_KEY` (redeploy or
restart so the running process picks it up), then confirm the effective switch:

```bash
curl -s https://<your-instance>/api/v1/product-analytics/transparency \
  -H "Authorization: Bearer <token>"
```

The token must hold the `system.config.manage` system permission:
`GET /api/v1/product-analytics/transparency` is gated by
`require_system_permission("system.config.manage")`, so a normal Bearer token
without that permission returns `403`.

With no stored operator value and no
`FARNA_CONTROL_PRODUCT_ANALYTICS_ENABLED` set, the response reports:

```json
{
  "instance_enabled": true,
  "instance_switch_source": "licence",
  "licence_requires_analytics": true
}
```

`instance_switch_source` is the authority: `licence` means the claim is the
effective reason analytics defaults ON. If it reads `operator`, `environment` or
`default`, something else decided the value (see the precedence in
`docs/configuration-reference.md`).

## Limitation: a default, never a control

The claim sets a DEFAULT only. The resolved switch takes the first match from:

1. the stored `system_config["product_analytics_enabled"]` value (operator),
2. `FARNA_CONTROL_PRODUCT_ANALYTICS_ENABLED` (operator),
3. the licence claim (default ON),
4. otherwise OFF.

An explicit operator value always wins, **including OFF** over a licence that
requires analytics. A key cannot force analytics on against the operator's
choice. Treat this as a partner expectation (a contractual default), not a
software control — and a partner that disables analytics is breaching a
contract, which is a conversation, not something the product enforces.

## Constraint: the built-in mint paths cannot emit the claim

Both production mint paths call `generate_team_license`, whose payload builder
has no `product_analytics_required` input:

- `POST /admin/license/issue` (`api/routes/admin_license.py`)
- Stripe fulfilment (`core/stripe_fulfilment.py`)

A key minted through either will not carry the claim. Only the hand-built
payload above does.

Renewal does not currently drop the claim, because no production caller passes
`expires_from`, so nothing re-mints. If a future caller starts renewing by
re-minting through `generate_team_license`, the new key will NOT carry
`product_analytics_required` — re-add it to the hand-built payload on renewal, or
the instance silently reverts to the analytics default (OFF).
