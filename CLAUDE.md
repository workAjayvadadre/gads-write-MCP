# gads-write-mcp

A guardrailed **write** MCP server for Google Ads. It can spend real money.
Guardrails are the primary requirement, not a feature.

Separate repo, separate process, separate hostname (`adswrite.indiraivf.in`)
from the read-only `googleads/google-ads-mcp` already running on this box.
We do not fork or modify Google's code.

Stack: Python 3.11+, FastMCP, streamable-http, Google OAuth, PM2, Nginx,
Ubuntu EC2. No Docker.

## Working agreement

The author knows Node/TypeScript well and Python poorly. Explain
Python-specific choices when they are not obvious from a TS background.
Prefer plain, explicit code over clever idioms.

Build **one phase at a time**. Before each phase, state what you are
building, why it comes at this point, what it protects against, and how to
verify it. Then build it and **stop** for verification. Do not run ahead.

If you need a config value, a decision, or a file from the existing server,
**ask** rather than assuming.

## Non-negotiable constraints

### Architecture
- Only `src/gads_write/ads/executor.py` may call the Google Ads API mutate.
  Tool functions NEVER call the API directly. No exceptions.
- Every write tool returns a `plan_id` + preview. It does not execute.
  Execution happens only in `tools/confirm.py`, after re-evaluating policy.
- Every tool calls `check_guards()` before doing anything else.

### Forbidden in v1
- No remove/delete tools for campaigns, ad groups, keywords, conversion
  actions, or budgets. Pausing is reversible; removal is not.
- No hardcoded customer IDs, budgets, limits, or emails in Python. All of
  that lives in `config/policy.yaml` and `config/roles.yaml`.
- No wildcard account access. Customer IDs must be on the allowlist.

### Credentials
- Never a static refresh token. Every Google Ads call uses the authenticated
  user's OAuth token from the request context, so Google's own permission
  model is the outermost guard.
- Never log tokens, client secrets, or credentials.

### Adding a write tool — all must be true
1. Registered per role tier, not globally
2. Inputs validated in `safety/validators.py`
3. Policy checked in `safety/guards.py`, then AGAIN at confirm time
4. Returns preview + `plan_id`, no side effects
5. Audit line written on apply
6. Has a test in `tests/`

### Style
- Python 3.11+, type hints on every function, async where FastMCP expects it
- Fail fast and loud. Never silently swallow an error from the Ads API.

## No guessing

Google Ads API details (field names, enums, resource name formats, micros
conversion, update masks) are where silent, expensive bugs live. If you are
not certain of an API detail, say so and check the docs rather than
producing plausible-looking code.

**Money is in micros** — millionths of the currency unit. A missing
conversion is a 1,000,000x error that type-checks fine. Name variables with
their unit (`budget_micros`, `budget_rupees`), never bare `budget`.

## Phase status

| Phase | Scope | State |
|---|---|---|
| 1 | Skeleton: settings, OAuth, `health_check`, ops | verified |
| 2 | Safety core against a fake executor, no API calls | **partial** - units, validators, policy, plans, spend, audit, executor Protocol built and tested. `guards.py`, `auth/tiers.py` (TierResolver) and the tier middleware are blocked on three interface decisions; see the Phase 2 section below. |
| 3 | Reads: per-user client, accounts, performance, search terms | not started |
| 4 | First mutation (`pause`/`enable`), test account only | not started |
| 5 | Remaining tools: budget, bids, negatives, keywords, RSA | not started |
| 6 | Rollout: runbook, log shipping, alerting, rollback | not started |

`google-ads` is deliberately not a dependency until Phase 3.

### Phase 2: tier resolution is pluggable

Tier must NOT come from roles.yaml in production. It is derived from the
user's own Google Ads MCC role (Read-only -> readonly, Standard -> operator,
Admin -> lead) so that adding someone in the Google Ads UI grants access with
no developer involvement.

- `TierResolver` is an interface. `FileTierResolver` (roles.yaml) is temporary
  Phase 2 backing; `GoogleAdsTierResolver` arrives in Phase 3 and becomes the
  default, at which point roles.yaml shrinks to an optional override list.
- Guards, middleware and tests depend on the interface only, never on
  roles.yaml directly.
- Tier is resolved on **every call**, not at connect. A demoted user must not
  keep operator powers until they reconnect.

Open risk for Phase 3: `customer_user_access` is documented as how admins list
users, but the docs do not say whether a STANDARD or READ_ONLY user can read
their own row using their own token. If they cannot, `GoogleAdsTierResolver`
cannot resolve non-admins. Settle this with an empirical test against the real
MCC before building on it.

## Commands

```bash
.venv/bin/python -m pytest          # tests
.venv/bin/python -m gads_write.server   # run locally
pm2 restart gads-write-mcp          # prod; picks up .env and config/*.yaml
```
