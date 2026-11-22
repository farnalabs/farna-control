---
id: feat-notifications
prd: N/A
adr: []
code:
  - backend/src/farna/core/notifier
  - backend/src/farna/core/hitl_manager/deadline_warning.py
  - backend/src/farna/api/routes/admin_notifications.py
  - backend/src/farna/api/routes/notifications.py
  - backend/src/farna/api/routes/in_app_notifications.py
  - backend/src/farna/core/email_service.py
  - backend/src/farna/db/models/notification.py
  - backend/src/farna/db/models/notification_delivery.py
  - backend/src/farna/db/models/notification_endpoint.py
unit-tests:
  - backend/tests/unit/notifier/test_notifier.py
  - backend/tests/unit/notifier/test_event_mapper.py
  - backend/tests/unit/notifier/test_hitl_awaiting_terminal_suppression.py
  - backend/tests/unit/hitl_manager/test_deadline_warning.py
  - backend/tests/integration/test_hitl_deadline_warning.py
  - backend/tests/unit/api/test_notifications_endpoint.py
  - backend/tests/unit/api/test_admin_notifications_webhooks.py
  - backend/tests/unit/api/test_in_app_notifications_preferences.py
  - backend/tests/unit/api/test_in_app_notifications_lifecycle.py
  - backend/tests/unit/api/test_in_app_notifications_run_metadata.py
  - backend/tests/unit/api/test_admin_email.py
bdd:
  - backend/tests/bdd/features/notifications/failure_webhook.feature
  - backend/tests/bdd/features/notifications/hitl_webhook.feature
  - backend/tests/bdd/features/notifications/signing.feature
  - backend/tests/bdd/features/in_app_notifications/dashboard_panel.feature
  - backend/tests/bdd/features/in_app_notifications/dismiss_flow.feature
  - backend/tests/bdd/features/in_app_notifications/notification_filters.feature
  - backend/tests/bdd/features/in_app_notifications/sse_integration.feature
  - backend/tests/bdd/steps/test_alpha_notifications.py
  - backend/tests/bdd/steps/test_in_app_notifications.py
depends-on:
  - feat-runs
  - feat-hitl
status: covered
---

# Notifications

Notifications surface pipeline events to operators across `/notifications`,
`/settings/email`, `/settings/notifications`, `/admin/notification-delivery` and
the in-app panel; `/settings/notifications` is where each user sets their own
in-app preferences (a per-category toggle set plus the dashboard minimum level).
The `core/notifier` dispatches outgoing webhooks with HMAC-SHA256 signing, bounded retry, and
dead-letter tracking of final failures; `api/routes/admin_notifications.py` exposes the
notification-delivery log with retry/DLQ admin actions; `event_mapper.py` maps run/HITL
events into typed notification payloads; and in-app notifications stream over SSE.

## Behaviors

- [x] A failure webhook fires on an unhandled node exception with the `run_id` and
      `error_detail`, includes the failed node name and error message, retries up to 3
      times, and auto-disables the endpoint after 10 consecutive failures (logging an
      alert) (`failure_webhook.feature`)
- [x] A HITL webhook fires when a run reaches an approval gate with `run_id`/`review_id`
      and gate context, retries on failure, and after final failure the event lands on the
      dead-letter queue (`hitl_webhook.feature`)
- [x] Outgoing webhooks are signed with HMAC-SHA256 (per-endpoint secrets) and carry
      `X-Farna-Control-Signature` / `X-Farna-Control-Timestamp`, and a receiver can verify the signature
      (`signing.feature`)
- [x] Delivery attempts are recorded against `NotificationDeliveryLog` and are
      manageable from `/admin/notification-delivery` – titled **Webhook
      Deliveries** – (status/event filters, retry of failed deliveries)
      (`api/routes/admin_notifications.py`, `test_admin_notifications_webhooks.py`).
      The page explains its own scope (FAR-2596): each row is one outbound
      delivery attempt, including deliveries to a webhook that has since been
      deleted (the FK is nulled, the history is kept), while in-app error alerts
      are excluded – the error-tracking alert dispatcher also writes
      `NotificationDeliveryLog` rows, with `status='in_app'` and a NULL
      `endpoint_id`, and both the list query and its count filter them by STATUS
      (`status != 'in_app'`, never `endpoint_id IS NOT NULL`), so the page and
      its `total` describe only outbound attempts; inbound trigger webhooks are
      linked out to the Trigger Event Log; event types and statuses render their
      human labels, and the status filter offers only values the log can contain
      (delivered / failed / dead_lettered); a "Dead Lettered" line says the
      delivery gave up after the final attempt and that a retry starts a fresh
      attempt; the empty state distinguishes "no deliveries yet" from "no
      deliveries match these filters". The "No outbound notification webhooks
      are configured" banner is shown ONLY when `GET /api/v1/notifications`
      reports zero endpoints AND the log is empty – its copy is forward-looking
      ("New deliveries appear once you add an endpoint"), it links to
      `/settings/teams` (omitted when `team_rbac` is inactive) and carries a
      secondary line telling an org with no team to create one first. An empty
      endpoint list alongside retained history (a deleted webhook keeps its
      rows) shows a quieter history notice instead, so the page never claims
      "nothing will appear here" above a populated table. The endpoint signal is
      read from the endpoint list, never inferred from the delivery log, and is
      ONE tri-state (`unknown` / `none` / `configured`): a failed or malformed
      read stays `unknown` and renders no claim at all, so no empty state is
      ever shown before the read resolves.
- [x] In-app notifications stream over SSE with a dashboard panel, per-user filters and a
      dismiss flow (`in_app_notifications/*.feature`, `test_in_app_notifications.py`)
- [x] Email delivery settings and message sending are configured under `/settings/email`
      (`core/email_service.py`, `test_admin_email.py`)

### Lifecycle semantics

- [x] **`expires_at` is enforced on every live read (TTL is not decorative).** A
      notification past its `expires_at` – per-event TTLs verified against
      `_EVENT_CONFIG` in `core/notifier/event_mapper.py`: 72h for
      `hitl.awaiting` **and** `run.stalled`, 168h for `run.failed`,
      24h for `hitl.claim_expired`, 336h for `eval.regression` /
      `feedback.pending`, 90d default (`create_notification`) – drops out of the
      dashboard panel, the unread badge
      (`GET /unread-count` and the dashboard's `total_unread`), the inbox
      default (no `status`) and `status=active` – enforced in
      `db/crud/notifications.py` (`_not_expired_clause`), applied to BOTH the
      list and the count so a page's `items` and `total` always agree. The row
      is never deleted: an explicit historical filter (`status=dismissed_self`
      / `dismissed_scope`) and the by-id detail read still retrieve it, so an
      expired notification stops presenting as active/actionable without being
      lost (`test_in_app_notifications_lifecycle.py`, including the
      `expires_at IS NULL` never-expires arm – see the note below on why that
      arm exists despite the ORM model declaring `NOT NULL`).
- [x] **The `status` query param is constrained to its vocabulary.** The
      allowed values are `active`, `dismissed_self`, `dismissed_scope`, or the
      param omitted (the inbox default) – exactly the branches
      `_apply_status_filter` understands and the filter set the
      `/notifications` UI offers. Any other value (including the empty string
      `?status=`, which is *not* "unset") returns **422** from
      `api/routes/in_app_notifications.py` **before** any DB read, instead of
      the previous unfiltered 200 that served every row – expired and
      dismissed – as if live. The CRUD carries the same guard as an `else`
      that raises, so a future caller cannot reintroduce the bypass
      (`test_in_app_notifications_routes.py`,
      `tests/unit/db/crud/test_notifications.py`).
- [x] **Dismissals hide on EVERY live view, including the default inbox.**
      `_hidden_from_user_clause` is applied to the inbox default (no `status`)
      as well as `status=active`, the dashboard panel and the unread count, so
      a `self` dismissal hides the row for its actor and a `scope` dismissal
      hides it for the whole org on all four. `dismissed_self` /
      `dismissed_scope` keep returning the acting user's own history, and the
      by-id detail read is unchanged (documented exception – it applies
      neither the TTL nor the dismissal clause, so a dismissed row stays
      fetchable by its id). List and count share `_apply_status_filter`, so
      `items` and `total` cannot disagree (`test_in_app_notifications_lifecycle.py`).
- [x] **Dismissal scope is honored in the active filter.**
      `POST /{id}/dismiss` with `dismiss_scope="self"` writes a per-user
      dismissal that hides the row ONLY for the dismissing user;
      `dismiss_scope="scope"` writes a dismissal that hides the row for EVERY
      user in the org (subject to the existing `dismiss_strategy` gate:
      `user_only` refuses, `org_admin` requires an admin, `any_scope` allows
      any member). The active filter (`_hidden_from_user_clause`) excludes a
      row when ANY user has dismissed it at scope, correlated on
      `organisation_id` so a dismissal can never cross a tenant on a backend
      without RLS; `dismissed_self` / `dismissed_scope` remain the record of
      what the acting user dismissed (`test_in_app_notifications_lifecycle.py`).
- [x] **A `self` dismissal can be escalated to `scope`.** A user who filed
      "Review later" (`dismiss_scope="self"`) may then call
      `POST /{id}/dismiss` with `dismiss_scope="scope"`: the existing row is
      **updated** to `scope` in place (the `(notification_id,
      dismissed_by_user_id)` unique constraint means escalation can never
      duplicate the row) instead of being refused as a "concurrent" duplicate
      – which used to make the org-wide hide unreachable for that actor. The
      escalation runs **after** the same `dismiss_strategy`/admin gate that
      governs a fresh scope dismissal, so it cannot be used to sidestep it;
      an identical-scope repeat keeps the pre-existing refusal
      (`test_in_app_notifications_lifecycle.py`,
      `tests/unit/db/crud/test_notifications.py`).
- [x] **No fresh `hitl.awaiting` for an already-terminal run.** The sole
      `EVENT_HITL_AWAITING` call site is `PipelineExecutor._dispatch_hitl_awaiting`
      (`core/pipeline_engine/executor.py`), which funnels through
      `Notifier.dispatch_event` → `_dispatch_inline` – the single choke point
      every emission and every resume/re-dispatch re-entry passes. When the
      linked run's CURRENT status is in `TERMINAL_STATUSES` (operator-cancelled,
      gate-expiry terminalized, failed, …), the dispatch is suppressed entirely
      (no webhook POST, no in-app row) and logged as
      `notifier.hitl_awaiting_suppressed_terminal_run`; a missing run row
      **or a run-status read that raises** (transient DB error – e.g. the
      `runs` table unreadable) fails OPEN (the dispatch proceeds) so a DB blip
      can never swallow a live review request. Other event types are
      unaffected. This is the emission-side fix; the FAR-1234 read-time
      enrichment (`run_status` / `run_terminal` / `run_cancel_reason`) is
      unchanged (`test_hitl_awaiting_terminal_suppression.py`, including the
      read-error fail-open case).
- [x] **The approaching-deadline HITL sweep has a webhook + in-app leg
      (FAR-1295).** The `hitl_deadline_warning` system cron
      (`core/hitl_manager/deadline_warning.py`) dispatches
      `EVENT_HITL_DEADLINE_WARNING` through `Notifier.dispatch_event` – the
      webhook endpoints subscribe to it and the `NotificationEventMapper`
      turns it into a typed in-app notification – under the SAME once-only
      fire-once `SET NX EX` claim (`hitl:deadline_warning:{claim_id}`, with an
      in-process monotonic backstop when Redis errors) as the email leg: ONE
      claim guards BOTH channels, so a gate can never produce a second email
      NOR a second webhook / in-app notification on a later tick, an org with
      email recipients and/or a subscribed endpoint is warned exactly once per
      gate, and an org with neither channel leaves the marker unset so a later
      opt-in or subscription can still warn while the band is open
      (`backend/tests/unit/hitl_manager/test_deadline_warning.py`,
      `backend/tests/integration/test_hitl_deadline_warning.py`,
      `backend/tests/unit/notifier/test_event_mapper.py`)

#### Why the `expires_at IS NULL` arm is still there

The ORM model declares `expires_at` `NOT NULL`, and a concurrent change on
PR #1022 removed the `IS NULL` arm as "dead" to satisfy the changed-lines
coverage gate. The model is not the migration history, so the arm stays:

- migration `0003` creates `notifications.expires_at` as **`nullable=True`**;
- the only tightening, in `0110`, is **conditional** –
  `IF ... is_nullable='YES' AND NOT EXISTS (... WHERE expires_at IS NULL) THEN SET NOT NULL` –
  so a database that already holds legacy NULL rows is left NULLable forever;
- the unconditional backfill (`0029_fix_expiry_fields_non_null`, renumbered
  `0057`) was **deleted** by the migration-tree rewrite in #1477, so nothing
  ever backfills those rows;
- `create_notification` gained its 90-day default only in #383 – before that,
  "runtime paths still wrote NULL".

Without the arm, `NULL > now` evaluates to NULL (not TRUE) and those legacy
rows silently disappear from every live view. It costs one `OR` term and is
covered by `test_never_expiring_row_is_present_on_every_live_view`. The
coverage gate is satisfied by that test rather than by deleting the branch.

## Known Gaps

- **In-app notification preference defaults and digest batching** are not surfaced here;
      the SSE panel is the primary in-app surface cited.
- **Webhook payload schemas are not versioned** – receivers depend on the documented
      field set (run_id / error_detail / gate context); there is no webhook payload
      version negotiation.

## QA History
- 2026-11-20: **FAR-2596 pre-PR QA – the banner can no longer contradict the
  table.** `GET /api/v1/notifications` excludes soft-deleted endpoints while the
  log deliberately keeps their delivery rows, so an operator who deleted their
  last webhook saw "Nothing will appear here until you add one" directly above
  a table of retained history. The banner now renders only when the log is also
  empty, its copy is forward-looking, and an empty endpoint list with history
  shows a quieter notice. Also fixed in the same pass: the endpoint read is ONE
  tri-state (`unknown` / `none` / `configured`) so no empty state is rendered
  before it resolves, and a secondary line under the CTA tells an org with no
  team to create one first (endpoints are created per team). Pinned by the
  retained-history regression test, which fails with the old banner condition.
- 2026-11-20: **FAR-2596 follow-up – the page is now strictly outbound.** The
  first slice's banner claim ("Nothing will appear here until you add one") was
  false for an org with in-app error-alert rules: the error-tracking alert
  dispatcher writes `NotificationDeliveryLog` rows with `status='in_app'` and a
  NULL `endpoint_id`, and the org-wide delivery-log query did not exclude them.
  Both the list query and `_count_deliveries` now filter `status != 'in_app'`
  (deliberately NOT `endpoint_id IS NOT NULL`, because `ondelete=SET NULL` keeps
  a deleted webhook's history visible), the page's status filter drops its
  never-matching `pending` option, the boundary explainer states that in-app
  error alerts are not shown and that deliveries to deleted webhooks remain, and
  a real-SQLite unit test pins all three outcomes
  (`test_list_all_deliveries_excludes_in_app_rows_and_keeps_deleted_endpoint_rows`).
- 2026-11-20: **FAR-2596 delivery page comprehension pass** – the product
  owner opened `/admin/notification-delivery` against production, saw an empty
  page and could not tell what it was for. The page now states its boundary
  (outbound deliveries only, inbound trigger webhooks linked to the Trigger
  Event Log), warns when no outbound endpoints exist with a CTA to
  `/settings/teams`, splits the two empty states, and renders event types and
  statuses through the existing locale labels. The "zero endpoints" condition
  is read from `GET /api/v1/notifications` and fails open – it is never inferred
  from the delivery log, because a soft-deleted endpoint leaves historical
  deliveries behind. Retitled from "Webhook Notifications" to "Webhook
  Deliveries" across the page, the sidebar label and the manifest.
- 2026-09-30: **Improve Architecture product-map walk** – reconciled this
  tracker with the shipped `hitl_deadline_warning` surface (FAR-1295,
  merged 2026-09-29): the manifest `feat-notifications` registry tracked the
  approaching-deadline warning's webhook / in-app leg but the human-readable
  graph entry lagged behind (no behavior line, no code/unit-test citation).
  Added the checked behavior (the single fire-once `hitl:deadline_warning`
  claim guarding BOTH the email and the Notifier/webhook/in-app legs), the
  `core/hitl_manager/deadline_warning.py` code citation, and the
  `test_deadline_warning.py` / `test_hitl_deadline_warning.py` /
  `test_event_mapper.py` unit citations.
  `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-09-27: **qa-iterate follow-up pass** – closed six majors found by the
  multi-lens review of the fix above: (F1) `status` is now validated against
  its vocabulary at the route (422 before any DB read) with a fail-closed
  `else` in `_apply_status_filter` as backstop; (F2) `_hidden_from_user_clause`
  now also applies to the default (no `status`) live view; (F3) added coverage
  for the `expires_at IS NULL` never-expires arm; (F4) added coverage for the
  `_run_is_terminal` fail-open-on-DB-error branch (run-status read raising);
  (F5) corrected the TTL table in this document – `run.stalled` is **72h**, not
  168h, verified against `_EVENT_CONFIG` in `core/notifier/event_mapper.py`
  rather than against either side alone; (F6) a `self` dismissal can now be
  escalated to `scope` (row updated in place, same privilege gate, identical-scope
  duplicates still refused). Each new test was proven to fail with its fix
  reverted.

- 2026-09-27: **notification-lifecycle fix pass** – documented the three
  lifecycle semantics now enforced in code: `expires_at` TTL on every live read
  (dashboard / unread count / inbox default / `status=active`, list and count
  kept in agreement), `dismiss_scope` honored in the active filter (`scope`
  hides for the whole org, `self` stays per-user), and post-terminal
  `hitl_awaiting` suppression at the single `Notifier.dispatch_event` choke
  point (fail-open on an unreadable run). Behaviors verified against
  `db/crud/notifications.py`, `core/notifier/__init__.py` and the new
  `test_in_app_notifications_lifecycle.py` / `test_hitl_awaiting_terminal_suppression.py`
  suites (each proven to fail without its fix). The authoritative
  `frontend/src/manifest.yaml` entry was not modified by this pass (out of the
  delivery's file scope); no existing manifest behavior text contradicted the
  new semantics.

- 2026-09-12: **product-map review pass** – registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/admin/notification-delivery`, `/settings/email`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Assistant's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-11: **product-map review pass** – extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/notifications`: the whole-page view(s) `NotificationsPage.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Assistant's docs indexer /
  `/api/v1/manifest`.

- 2026-08-27: **product-map review pass** – added this behavior-tracker
  for the registered manifest feature `feat-notifications`, which previously had no
  `docs/product-map/` entry. Behaviors verified against `core/notifier`,
  `api/routes/admin_notifications.py` and the notifications BDD/unit suites.
  Status: covered.
