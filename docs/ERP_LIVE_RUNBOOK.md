# Live ERP Runbook — pointing the harness at a real tenant

The harness is validated in CI against the in-process BC twin. This is how you
run it against a **real** tenant once that bar is met. The code path is
identical — `discover()` / `run_onboarding()` are unchanged; only the transport
behind the `SafetyEnforcer` differs (`build_live_enforcer` selects it from env).

## Precondition (non-negotiable)
A live-tenant run is **gated behind a green twin demonstrate-the-catch matrix**
(harness spec §6). Don't point at a tenant until the twin suite is green and the
permissions manifest has been actioned by the customer's IT.

## Business Central (SaaS) — OAuth + HTTPS, no terminal access
1. IT registers an Entra app, grants the **read-only** scopes from the C1
   permissions manifest, and returns client id/secret + tenant.
2. Set env and run the gated smoke (or your onboarding entrypoint):
   ```sh
   export SKU_ERP_KIND=bc
   export SKU_ERP_BASE_URL="https://api.businesscentral.dynamics.com/v2.0/<tenant>/<env>/api/v2.0"
   export SKU_ERP_TOKEN_URL="https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token"
   export SKU_ERP_CLIENT_ID=...   SKU_ERP_CLIENT_SECRET=...
   export SKU_ERP_SCOPE="https://api.businesscentral.dynamics.com/.default"
   pytest tests/test_live_erp_smoke.py -v
   ```

## NAV on-prem — SQL Server (needs network reach OR an on-prem run)
NAV has no OData surface; the harness reads SQL Server directly. You need
either (a) network reach to the SQL box (VPN / inside the network) or (b) to run
this from a host inside the customer network. Catalog *decoding* alone never
needs this — a customer can export the item master to Excel/PDF and use the
`catalog_source` pathways instead.
```sh
pip install '.[erp]'        # pyodbc
export SKU_ERP_KIND=nav
export SKU_ERP_SQL_DSN="Driver={ODBC Driver 18 for SQL Server};Server=...;Database=...;UID=readonly;PWD=...;Encrypt=yes"
export SKU_ERP_SQL_SCHEMA=dbo
pytest tests/test_live_erp_smoke.py -v
```

## Budgets / safety
`build_live_enforcer` wraps the backend in the SAME `SafetyEnforcer` the twin
runs under: method allowlist (read-only), per-minute rate ceiling
(`SKU_ERP_RATE_PER_MIN`, default 120), total-call budget
(`SKU_ERP_CALL_BUDGET`, default 2000), exponential backoff, and an append-only
journal. The agent cannot exceed these by being clever — they are code.
