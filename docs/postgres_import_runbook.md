# Runbook: import the CRM from MySQL into Supabase PostgreSQL (staging first)

The importer (`scripts/import_mysql_to_postgres.py`) copies the CRM from an isolated
local MySQL schema, `crm_import_source`, into an empty PostgreSQL database. It builds the
schema from the current models, copies every row with its original ID, resets sequences,
validates everything and stamps Alembic revision `20261013_0014`, all in one PostgreSQL
transaction. `--dry-run` rolls that transaction back.

Rules for every step:

- Run nothing against production. Rehearse the full run on the Supabase **staging** project
  first, and get explicit approval before each database step.
- Never touch `ai_sales_crm`, `ai_sales_crm_rehearsal`, `ai_sales_crm_test` or any other
  existing local database. Every MySQL statement below names `crm_import_source`.
- Use only `before_0013_backup.sql`. Never use `ai_sales_crm_pre0013_20261013_142622.sql`
  (corrupted). Never run `clear_test_crm.py`.
- Credentials go only into process environment variables of the current PowerShell
  window, or are typed at a password prompt. Never put them in `.env`, in a file, in a
  command line or in this document. Placeholders look like `<like-this>`.
- Close the PowerShell window when you finish, so the variables disappear.

Commands are PowerShell, run from the repository root with the project's virtual
environment active. `mysql` is the MySQL 8 client; `psql` is the PostgreSQL client.

## 0. What is already known (from reading the files, no database involved)

- `before_0013_backup.sql` (MySQL 8.0.37 dump of `ai_sales_crm`) is at revision
  **`20261013_0012`**, not 0014: it contains `INSERT INTO alembic_version VALUES
  ('20261013_0012')`. Migrations 0013 and 0014 must be applied to the restored copy
  (step 4) before the importer will accept it.
- The dump has no `CREATE DATABASE` or `USE` statement, so it restores into whatever
  database the client selects. It does contain `DROP TABLE IF EXISTS` for each table,
  which is why it must only ever be restored into the new, empty `crm_import_source`.
- Every timestamp column is `datetime`, not `timestamp`, so the dump's
  `SET TIME_ZONE='+00:00'` does not change any value.
- Timestamps come from two places: the application writes UTC (`datetime.now(timezone.utc)`,
  or naive values treated as UTC), while `created_at`, `updated_at` and the other
  `server_default=func.now()` columns are written by MySQL `NOW()` in the **session time
  zone**, which the app never sets. In the dump, `hitl_action_proposals` rows have
  `approved_at` (app, UTC) within seconds of `created_at` (MySQL `NOW()`), e.g.
  `11:06:48` and `11:06:46`, so those rows were written with a UTC session. That covers a
  few rows only; step 3 checks the whole copy.
- Row counts in the dump: agent_conversation_messages 44, agent_conversations 3,
  alembic_version 1, calendar_event_links 10, calls 65, companies 20, contacts 56,
  customers 50, follow_ups 42, google_calendar_connections 1, hitl_action_audits 64,
  hitl_action_proposals 17, hitl_approval_decisions 17, hitl_execution_audits 15,
  hitl_tasks 28, meetings 84, organizations 1, sales_enquiries 75, scheduling_locks 1,
  users 4.

## 1. Create the isolated `crm_import_source` schema and its account

Connect as a local MySQL administrator (the client prompts for the password):

```powershell
mysql --host=127.0.0.1 --port=3306 --user=<mysql-admin> --password
```

```sql
-- Must return 0 rows. If it exists, stop: inspect it, never reuse or overwrite it.
SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'crm_import_source';
SELECT user, host FROM mysql.user WHERE user = 'crm_import';   -- must return 0 rows

CREATE DATABASE crm_import_source CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
-- The password is typed here only; use a new random value of 24+ characters.
CREATE USER 'crm_import'@'127.0.0.1' IDENTIFIED BY '<new-random-password>';
-- Restore and migrations 0013-0014 need these, on this schema only.
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, REFERENCES, LOCK TABLES
  ON crm_import_source.* TO 'crm_import'@'127.0.0.1';
SHOW GRANTS FOR 'crm_import'@'127.0.0.1';   -- only USAGE and the crm_import_source grant
```

Then clear the client history entry that holds the password (`%APPDATA%\MySQL\mysql_history`
or `~/.mysql_history`, if your client keeps one).

## 2. Restore `before_0013_backup.sql`

```powershell
Get-FileHash .\before_0013_backup.sql -Algorithm SHA256        # record the hash
Select-String -Path .\before_0013_backup.sql -Pattern '^(CREATE DATABASE|USE )'   # must print nothing
Select-String -Path .\before_0013_backup.sql -Pattern "INSERT INTO ``alembic_version``"  # '20261013_0012'

# --database selects the target; the dump itself names no database.
mysql --host=127.0.0.1 --port=3306 --user=crm_import --password --database=crm_import_source `
  --execute="source before_0013_backup.sql"
```

## 3. Verify row counts and representative timestamps

Connect as `crm_import` with `--database=crm_import_source`, then:

```sql
SELECT DATABASE();                                 -- crm_import_source
SELECT version_num FROM alembic_version;           -- 20261013_0012
SELECT 'agent_conversation_messages', COUNT(*) FROM agent_conversation_messages
UNION ALL SELECT 'agent_conversations', COUNT(*) FROM agent_conversations
UNION ALL SELECT 'calendar_event_links', COUNT(*) FROM calendar_event_links
UNION ALL SELECT 'calls', COUNT(*) FROM calls UNION ALL SELECT 'companies', COUNT(*) FROM companies
UNION ALL SELECT 'contacts', COUNT(*) FROM contacts UNION ALL SELECT 'customers', COUNT(*) FROM customers
UNION ALL SELECT 'follow_ups', COUNT(*) FROM follow_ups
UNION ALL SELECT 'google_calendar_connections', COUNT(*) FROM google_calendar_connections
UNION ALL SELECT 'hitl_action_audits', COUNT(*) FROM hitl_action_audits
UNION ALL SELECT 'hitl_action_proposals', COUNT(*) FROM hitl_action_proposals
UNION ALL SELECT 'hitl_approval_decisions', COUNT(*) FROM hitl_approval_decisions
UNION ALL SELECT 'hitl_execution_audits', COUNT(*) FROM hitl_execution_audits
UNION ALL SELECT 'hitl_tasks', COUNT(*) FROM hitl_tasks UNION ALL SELECT 'meetings', COUNT(*) FROM meetings
UNION ALL SELECT 'organizations', COUNT(*) FROM organizations
UNION ALL SELECT 'sales_enquiries', COUNT(*) FROM sales_enquiries
UNION ALL SELECT 'scheduling_locks', COUNT(*) FROM scheduling_locks UNION ALL SELECT 'users', COUNT(*) FROM users;
```

Every count must equal the list in section 0. Any difference: stop.

Timestamps. The importer treats every `DATETIME` as UTC. Before any `--commit`, record
the evidence below in the change record:

```sql
-- T1 The server's zone now (the zone NOW() used when rows were written is what matters).
SELECT @@global.time_zone, @@system_time_zone;
-- T2 App-written UTC vs MySQL-written NOW() on the same row: the gap must be seconds,
--    not a whole number of hours. A gap of e.g. 5:30 means server columns are local time.
SELECT action_id, approved_at, created_at, TIMEDIFF(approved_at, created_at) AS gap
FROM hitl_action_proposals WHERE approved_at IS NOT NULL ORDER BY created_at DESC LIMIT 20;
SELECT execution_id, started_at, completed_at FROM hitl_execution_audits
ORDER BY started_at DESC LIMIT 10;   -- started_at is NOW(); completed_at is app-written UTC
-- T3 The newest server-written values must not be later than the dump time in UTC
--    (the dump file was written 2026-10-13; compare with its UTC write time).
SELECT 'meetings', MAX(created_at), MAX(updated_at) FROM meetings
UNION ALL SELECT 'calls', MAX(created_at), MAX(updated_at) FROM calls
UNION ALL SELECT 'hitl_action_audits', MAX(created_at), NULL FROM hitl_action_audits
UNION ALL SELECT 'agent_conversation_messages', MAX(created_at), NULL FROM agent_conversation_messages;
-- T4 Scheduled times next to their display zone, to compare with what the app shows.
SELECT meeting_id, scheduled_at, scheduled_timezone FROM meetings ORDER BY meeting_id DESC LIMIT 10;
```

If T2 or T3 shows an offset, **stop**: server-written columns are not UTC, and the
importer must not be run until a conversion is designed and approved.

Case and spacing (record the results; see section 10 for why they matter):

```sql
-- C1 Emails the app did not normalize (the app stores lower(trim(email))): expect 0.
SELECT COUNT(*) FROM users WHERE email <> LOWER(TRIM(email));
-- C2 Names that collide under the app's own lower(trim()) matching: expect 0 rows each.
SELECT organization_id, LOWER(TRIM(company_name)), COUNT(*) FROM companies
GROUP BY 1, 2 HAVING COUNT(*) > 1;
SELECT company_id, LOWER(TRIM(customer_name)), COUNT(*) FROM customers
GROUP BY 1, 2 HAVING COUNT(*) > 1;
```

## 4. Apply migrations 0013 and 0014 to `crm_import_source`

`DATABASE_URL` set in the process environment overrides `.env` for Alembic; nothing is
written to `.env`. First check that the target is the import schema, never `ai_sales_crm`:

```powershell
$cred = Get-Credential -UserName crm_import -Message "crm_import MySQL password"
$env:DATABASE_URL = "mysql+pymysql://crm_import:$([uri]::EscapeDataString($cred.GetNetworkCredential().Password))@127.0.0.1:3306/crm_import_source"
python -c "from app.core.config import settings as s; u = s.database_url; print(u.drivername, u.host, u.port, u.database)"
# must print: mysql+pymysql 127.0.0.1 3306 crm_import_source
python -m alembic current           # 20261013_0012
```

Run the read-only preflight queries P1-P8 from `docs/migration_0013_runbook.md`
section 3 against `crm_import_source` (P1 expects `20261013_0012`, P2 one
`default-workspace` organization). Then:

```powershell
python -m alembic upgrade 20261013_0014
python -m alembic current           # 20261013_0014 (head)
Remove-Item Env:\DATABASE_URL
```

Rerun the step 3 row counts (unchanged, except `scheduling_locks` may gain one row if the
legacy organization's ID is not 1) and the post-migration checks from the 0013 runbook.
Then make the account read-only for the import:

```sql
-- as the MySQL administrator
REVOKE ALL PRIVILEGES ON crm_import_source.* FROM 'crm_import'@'127.0.0.1';
GRANT SELECT ON crm_import_source.* TO 'crm_import'@'127.0.0.1';
SHOW GRANTS FOR 'crm_import'@'127.0.0.1';
```

## 5. Prepare a fresh Supabase staging project

1. Create a **new** Supabase project for staging. Do not reuse a project that holds data.
2. Disable the Data API: Project Settings > Data API (or Integrations > Data API), turn off
   "Enable Data API", save, and record a screenshot. It must stay off.
3. Note the connection details from Connect: the **direct** connection
   (`db.<project-ref>.supabase.co:5432`) or the **session pooler** (port 5432). The
   importer and Alembic need one of those; the app later uses the transaction pooler (6543).

## 6. Create `crm_owner` and `crm_app` (least privilege)

As `postgres`, with `psql` against the staging project (type passwords at `\password`
prompts so they never appear in SQL history):

```sql
CREATE ROLE crm_owner LOGIN NOINHERIT;   -- owns the schema objects; used by importer and Alembic
CREATE ROLE crm_app   LOGIN NOINHERIT;   -- the application: data access only
\password crm_owner
\password crm_app

GRANT USAGE, CREATE ON SCHEMA public TO crm_owner;
GRANT USAGE ON SCHEMA public TO crm_app;          -- no CREATE: the app cannot change the schema
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER ROLE crm_owner SET search_path = public;
ALTER ROLE crm_app SET search_path = public;
```

Confirm the target is empty and nobody else has default privileges on `crm_owner`'s objects:

```sql
SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema = 'public';   -- 0 rows
SELECT * FROM pg_default_acl d JOIN pg_roles r ON r.oid = d.defaclrole WHERE r.rolname = 'crm_owner';  -- 0 rows
```

Then, connected **as `crm_owner`**, give `crm_app` data access to every table and sequence
`crm_owner` will create:

```sql
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO crm_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO crm_app;
```

## 7. Run the importer: dry run first

Set both URLs for this PowerShell window only (passwords typed at the prompt):

```powershell
$src = Get-Credential -UserName crm_import -Message "crm_import MySQL password"
$env:IMPORT_SOURCE_URL = "mysql+pymysql://crm_import:$([uri]::EscapeDataString($src.GetNetworkCredential().Password))@127.0.0.1:3306/crm_import_source"
$dst = Get-Credential -UserName crm_owner -Message "crm_owner staging password"
# Direct connection shown; for the session pooler the user is crm_owner.<project-ref>.
$env:IMPORT_TARGET_URL = "postgresql://crm_owner:$([uri]::EscapeDataString($dst.GetNetworkCredential().Password))@db.<project-ref>.supabase.co:5432/postgres?sslmode=require"

python scripts/import_mysql_to_postgres.py --dry-run `
  --confirm-source-database crm_import_source `
  --confirm-target-database postgres --confirm-target-host db.<project-ref>.supabase.co
```

It must print every table's row count (matching step 3) and end with "Validated and
rolled back (dry run)". Re-check that the staging `public` schema is still empty. Any
"Import stopped" message: nothing was committed; fix the cause, never the target.

Only after the dry run passes and the run is approved:

```powershell
python scripts/import_mysql_to_postgres.py --commit `
  --confirm-source-database crm_import_source `
  --confirm-target-database postgres --confirm-target-host db.<project-ref>.supabase.co
Remove-Item Env:\IMPORT_SOURCE_URL, Env:\IMPORT_TARGET_URL
```

What the importer refuses: a non-MySQL source or non-PostgreSQL target; any source other
than local `crm_import_source` on port 3306 (and always `ai_sales_crm`, the rehearsal/test
schemas and MySQL's system schemas); URL options other than `charset` (source) or
`sslmode`/`sslrootcert` (target), since options such as `host=`, `dbname=` or
`read_default_file=` could connect somewhere unconfirmed; a remote target without
`sslmode=require` or stricter; unconfirmed names or host; a source not at `20261013_0014`
or whose tables differ from the models; a target that is not empty, not connected as
`crm_owner`, or whose search_path does not start with `public`; a created schema whose
tables, columns, types, keys, indexes or check constraints differ from the models; NUL
characters, booleans other than 0/1, invalid dates, non-finite decimals and invalid JSON.
Driver errors are reported as class and SQLSTATE only.

If the importer reports "COMMIT did not confirm", the connection dropped during COMMIT and
the outcome is unknown: run the section 8 checks before anything else. Never rerun the
importer to "fix" it; a committed import makes the target non-empty and it will refuse.

## 8. Validate the import and check application readiness

As `crm_owner` on staging:

```sql
SELECT version_num FROM alembic_version;          -- 20261013_0014
SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY relname;   -- rough; use COUNT(*) for exact
SELECT tableowner, COUNT(*) FROM pg_tables WHERE schemaname = 'public' GROUP BY 1;   -- only crm_owner
-- crm_app may read and write data; anon/authenticated/service_role/PUBLIC must have nothing.
SELECT grantee, COUNT(*) FROM information_schema.role_table_grants
WHERE table_schema = 'public' GROUP BY grantee;   -- expect crm_owner and crm_app only
-- Every organization has its scheduling lock.
SELECT o.organization_id FROM organizations o
LEFT JOIN scheduling_locks l ON l.lock_id = o.organization_id WHERE l.lock_id IS NULL;   -- 0 rows
-- Timestamps arrived as the same instants (compare with step 3, T4).
SELECT meeting_id, scheduled_at AT TIME ZONE 'UTC', scheduled_timezone FROM meetings ORDER BY meeting_id DESC LIMIT 10;
```

Alembic on PostgreSQL (as `crm_owner`, direct or session connection) must now work and
report the stamp; on an empty PostgreSQL database it refuses to run:

```powershell
$dst = Get-Credential -UserName crm_owner -Message "crm_owner staging password"
$env:DATABASE_URL = "postgresql://crm_owner:$([uri]::EscapeDataString($dst.GetNetworkCredential().Password))@db.<project-ref>.supabase.co:5432/postgres?sslmode=require"
python -m alembic current     # 20261013_0014 (head)
Remove-Item Env:\DATABASE_URL
```

The application must not be able to rewrite the migration state (as `crm_owner`):

```sql
REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM crm_app;
```

Application smoke test against staging, as `crm_app` through the transaction pooler:

```powershell
$app = Get-Credential -UserName "crm_app.<project-ref>" -Message "crm_app staging password"
$env:DATABASE_URL = "postgresql://crm_app.<project-ref>:$([uri]::EscapeDataString($app.GetNetworkCredential().Password))@<pooler-host>:6543/postgres"
$env:DATABASE_POOL_MODE = "serverless"
uvicorn app.main:app --port 8000
# In another window: GET http://127.0.0.1:8000/health/ready must report the database ready.
# Log in, open customers, a meeting and a lead, create and delete a test follow-up,
# and schedule a test activity (this exercises the scheduling lock and sequences).
```

## 9. Rollback and cleanup

- A dry run or any failed run leaves the target untouched (one rolled-back transaction).
  Confirm with the step 6 "0 rows" query.
- After a committed staging import, to retry: delete the staging project, or, after
  review, have an administrator drop the imported objects manually. The importer never
  drops anything and refuses a non-empty target.
- The local MySQL source is disposable and separate from everything else. When the import
  is finished and verified, as the MySQL administrator:

  ```sql
  DROP DATABASE crm_import_source;   -- only this name; double-check before running
  DROP USER 'crm_import'@'127.0.0.1';
  ```

- Close the PowerShell window to clear `IMPORT_*_URL`, `DATABASE_URL` and the credential
  variables. Rotate any password that was ever pasted anywhere other than a prompt.
- The application's `.env` and the existing local MySQL databases were never changed, so
  the app keeps running on MySQL until a separate, approved cutover.
- Alembic cannot be used to roll PostgreSQL back: it refuses any downgrade or stamp below
  `20261013_0014` (and relative targets such as `-1`), because everything before that
  revision is MySQL-only. Rolling back a PostgreSQL import means discarding the target.

## 10. Case sensitivity: required application changes before a production cutover

MySQL compares these columns with `utf8mb4_unicode_ci`: case-insensitive,
accent-insensitive (`é` = `e`) and ignoring trailing spaces. That applies to `=` and to
**unique constraints**. PostgreSQL's default collation compares exactly. The import copies
values unchanged and cannot create duplicates (MySQL already enforced uniqueness), but
after cutover the database stops enforcing the looser equality.

| Behaviour | Code | On PostgreSQL |
|---|---|---|
| Login and registration duplicate check | `auth_service`: `lower(trim(User.email)) == normalized` | Same result. New emails are stored normalized by `schemas/auth.py`, so the exact `ix_users_email` unique index still catches concurrent duplicates. Unchanged if C1 is 0. |
| Policy audit actor lookup | `hitl_policy`: `User.email == current_user.email` | Same result: the value comes from the same row. |
| Search | `customer_service`, `enquiry_service`: `.ilike()` | Same: ILIKE is case-insensitive on PostgreSQL. (Accent-insensitive matching is lost.) |
| Company / customer duplicate names | `customer_service` pre-checks with `lower(trim())`; DB backstop `uq_companies_organization_company_name`, `uq_customers_company_customer_name` | The pre-check still works, but the **database backstop becomes exact**: "Acme" and "acme" (or "Acme ") can both be inserted by a concurrent request or by any path that skips the pre-check, such as the spreadsheet import (`import_service`). |
| Name ordering | `ORDER BY` on names | Order changes slightly (collation), cosmetic only. |

Required before production cutover (a separate, reviewed change; not part of this import):

1. Add a migration after `20261013_0014` that creates functional unique indexes matching
   the app's own normalization, after re-running C2 on the PostgreSQL copy:
   `UNIQUE (organization_id, lower(btrim(company_name)))` on `companies` and
   `UNIQUE (company_id, lower(btrim(customer_name)))` on `customers`.
2. Update `_is_duplicate_customer_integrity_error` in `customer_service.py`, which matches
   on the constraint name `uq_customers_company_customer_name`, to recognise the new index
   name too, so concurrent duplicates still return 409 instead of 500.
3. Make the spreadsheet import check names with `lower(trim())`, or rely on step 1.

Recommended, not required: a unique index on `lower(email)` for `users` (only valid if C1
is 0) so login lookups can use an index. Accent-insensitive equality would need an ICU
non-deterministic collation, which breaks `LIKE`/`ILIKE` on older PostgreSQL versions; do
not adopt it without a separate decision.

## Appendix: opt-in PostgreSQL tests

`tests/test_import_mysql_to_postgres.py` has tests that run only when `PG_TEST_URL` is set.
They refuse unless the URL names exactly `crm_import_test` on a local host name, with no
URL options except `sslmode`, and the live database has that name, carries the marker
comment below, has no Supabase roles and has an empty `public` schema. Every change is
inside a transaction that is rolled back. One-time setup on a local, disposable
PostgreSQL server (as its administrator):

```sql
CREATE DATABASE crm_import_test;
COMMENT ON DATABASE crm_import_test IS 'disposable: crm_import_test';
```
