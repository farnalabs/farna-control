Feature: Multi-Backend Database Support
  As a Farna Control operator
  I want tenant isolation, migrations, and locks to behave correctly on SQLite (test-only) and Postgres
  So that the automated test suite exercises the same repository abstractions as the supported PostgreSQL runtime

  Scenario: SQLite tenant filtering via GenericRepository
    Given a GenericRepository connected to SQLite
    When set_org_context is called with org "acme"
    Then the session stores org_id in session.info
    And apply_tenant_filter injects WHERE organisation_id = :org_id

  Scenario: Postgres uses RLS natively
    Given a PostgresRepository connected to Postgres
    When set_org_context is called with org "acme"
    Then set_config('app.organisation_id', :oid, true) is executed
    And apply_tenant_filter returns the statement unchanged

  Scenario: Cross-org isolation on SQLite
    Given entity records belonging to org "acme" and org "othercorp"
    When the session is scoped to org "othercorp"
    And a SELECT query is executed through GenericRepository
    Then only records for org "othercorp" are returned

  Scenario: Alembic env.py configures a driver and URL per backend
    Given a SQLite and Postgres database URL
    When the migration env.py configures the backend
    Then render_as_batch is enabled for SQLite
    And the async-to-sync driver conversion succeeds for each backend

  Scenario: Advisory lock abstraction
    Given a lock service for Postgres
    When a lock is acquired for key "pipeline:42"
    Then pg_try_advisory_lock is called
    Given a lock service for SQLite
    When a lock is acquired for the same key
    Then an asyncio.Lock is used instead

  Scenario: SQLite assigns created_at from the current_timestamp server default
    Given a table whose created_at defaults to current_timestamp
    When a row is inserted into an in-memory SQLite database
    Then the stored created_at is set and within the last minute
