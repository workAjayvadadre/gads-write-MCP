# gads-write-mcp

A guardrailed **write** MCP server for Google Ads. It can spend real money.
Guardrails are the primary requirement, not a feature.

Separate repo, separate process, separate hostname (`adswrite.indiraivf.in`)
from the read-only `googleads/google-ads-mcp` already running on this box.
We do not fork or modify Google's code.

Stack: Python 3.11+, FastMCP 3.4.x, streamable-http, Google OAuth, PM2,
Nginx, Ubuntu EC2. No Docker.

## Working agreement

The author knows Node/TypeScript well and Python poorly. Explain
Python-specific choices when they are not obvious from a TS background, and
bridge MCP concepts to Express equivalents. Prefer plain, explicit code over
clever idioms.

Build **one phase at a time**. Before each phase, state what you are
building, why it comes at this point, what it protects against, and how to
verify it. Then build it and **stop** for verification. Do not run ahead.

If you need a config value, a decision, or a file from the existing server,
**ask** rather than assuming.

## Phase status

| Phase | Scope | State |
|---|---|---|
| 1 | Skeleton: settings, OAuth, `health_check`, ops | **done, verified in production** |
| 2 | Safety core + gate + tier middleware, no API calls | **done, 201 tests** |
| 3 | Reads: per-user Ads client, accounts, performance, search terms | **done, 268 tests** |
| 4 | First mutation (`pause`/`enable`), test account only | not started |
| 5 | Budget, bids, negatives, keywords, RSA | not started |
| 6 | Rollout: runbook, log shipping, alerting, rollback | not started |

Pinned to `google-ads` 31.4.x / Google Ads API **v25**. The version lives in
`ads/api_version.py` and nowhere else.

`config/roles.yaml` still ships `mode: file`. The Google Ads resolver is
built, tested and boots, but flipping the switch is a deliberate decision
that depends on the open risk below.

## Non-negotiable constraints

### Architecture
- Only `src/gads_write/ads/executor.py` may call the Google Ads API mutate,
  and only `src/gads_write/ads/reads.py` may run a read. Tool functions call
  neither the API nor a service object directly.
  `tests/test_architecture.py` fails the build otherwise, and also pins that
  the read path may only ask for `GoogleAdsService` and `CustomerService`.
- GAQL is assembled by string interpolation because the API takes a query
  string with no bound parameters. Every interpolated value is validated in
  `safety/validators.py` and asserted again by `_literal()` in `ads/reads.py`.
- Every write tool returns a `plan_id` + preview. It does not execute.
  Execution happens only in `tools/confirm.py`, after re-evaluating policy.
- Every tool calls `Guard.check()` before doing anything else.

### Forbidden in v1
- No remove/delete tools for campaigns, ad groups, keywords, conversion
  actions, or budgets. Pausing is reversible; removal is not.
- No hardcoded customer IDs, budgets, limits, or emails in Python. All of
  that lives in `config/policy.yaml` and `config/roles.yaml`.
- No wildcard account access. Customer IDs must be on the allowlist.

### Credentials
- Never a static refresh token. Every Google Ads call uses the authenticated
  user's OAuth token, so Google's own permission model is the outermost
  guard.
- Never log tokens, client secrets, or credentials.

### Adding a write tool - all must be true
1. Registered in `tools/registry.py` with its required tier
2. Inputs validated via `safety/validators.py`
3. `Guard.check()` called first, and AGAIN at confirm time
4. Returns preview + `plan_id`, no side effects
5. Audit line written on apply via `Guard.record_application()`
6. Has a test in `tests/`

### Style
- Python 3.11+, type hints on every function, async where FastMCP expects it
- Fail fast and loud. Never silently swallow an error from the Ads API.

## No guessing

Google Ads API details (field names, enums, resource name formats, micros
conversion, update masks) are where silent, expensive bugs live. If you are
not certain of an API detail, say so and check the docs rather than
producing plausible-looking code.

**Money is in micros** - millionths of the currency unit. A missing
conversion is a 1,000,000x error that type-checks fine. Name variables with
their unit (`budget_micros`, `budget_units`), never a bare `budget`.

---

## Decisions already made, and why

These are the things that are NOT obvious from reading the code, and that
would otherwise be re-litigated.

**Tier comes from Google Ads, not from a file.** The operational
requirement is: add someone in the Google Ads UI and they work here, with no
developer involvement. `roles.yaml` in `mode: file` is temporary Phase 2
backing only. In Phase 3 it flips to `mode: google_ads` and the `users` map
empties out, becoming a break-glass override.

**Tier is per-account, not global.** `resolve(caller, customer_id)`. Google
stores access on `customer_user_access`, which is per-customer; a person can
be Standard on one account and Read-only on another. A global tier would
have to take the lowest across all accounts (one read-only account demotes
someone everywhere) or the highest (a genuine privilege escalation).

**`tools/list` carries no customer_id**, so visibility and authorization
answer different questions. `visible_tier(caller)` = highest tier anywhere,
used only to build the menu. `resolve(caller, customer_id)` is the boundary.
Hiding a tool is UX; the check on `tools/call` is the security.

**EMAIL_ONLY, UNKNOWN, UNSPECIFIED and no-access-row all map to `none`.**
`UNKNOWN` means a value from a newer API version nobody has reviewed; it
must never map to a permissive tier. There is no `BILLING_ONLY` in the API.

**An indeterminate tier lookup fails closed**, with a distinct audit verdict
(`lookup_failed`) so an outage is visible as an outage. `Tier.NONE` means
"we asked, they have nothing"; `TierLookupError` means "we do not know".
Conflating them would mean anyone able to cause a Google outage could grant
themselves permissions. The break-glass is a deliberate `roles.yaml` edit -
visible, in git, obviously temporary - not an automatic fallback.

**`none` and `readonly` limits are pinned to zero in code**, not trusted to
`policy.yaml`. A test caught `readonly` silently inheriting the permissive
defaults because the config omitted its block. Their blocks in the YAML are
documentation; `safety/policy.py` is the enforcement.

**The daily spend ceiling is derived from the audit log, not a counter.**
An in-memory counter would make `pm2 restart` a way to clear the cap. Only
positive deltas count: a decrease must not create headroom, or lowering
campaign A by 5000 would fund raising campaign B past the cap.

**Plans are consumed before execution**, not after. A crash mid-apply
leaves the plan burnt, forcing a human to look at the account rather than
blindly retry when we cannot know whether the mutation landed.

**A bad config edit keeps the last good version.** Both `PolicyStore` and
`RoleStore` refuse an invalid reload rather than relaxing to defaults or
crashing a live request. `last_error` is surfaced in `health_check`.

**`AccessToken.token` is the real Google access token** under
`GoogleProvider`, not the FastMCP JWT. Verified against fastmcp 3.4.7
source; citations are in `auth/identity.py`. Re-check on a major upgrade.

**The API version is pinned, not inherited.** `google-ads` 31.4.0 ships v21
through v25 and its own default moves on upgrade. Letting that default drift
would silently re-point every query at a version nobody reviewed. The pin is
`ads/api_version.py`; `test_ads_reads.py` parses every SELECT clause and
checks each field against that version's generated protos, so a rename or an
unreviewed version bump fails the build.

**A role is resolved on the account, then inherited from the MCC.** Google
resolves a user's effective role against the `login-customer-id`, and roles
are inherited down the hierarchy. Most team members have one access row on
the MCC and none on the child accounts they work in, so asking only the
child would see "no row" and lock out the whole team. A direct grant on the
child is more specific and wins; otherwise the manager's role applies. That
degrades safely - the failure direction is someone being offered less than
the UI shows them.

**`metrics.average_cpc` is deliberately unused.** It is a double, and the
generated stubs carry no unit comment, so whether it is micros or currency
units could not be established without guessing. Average CPC is computed
from `cost_micros / clicks`, both of which are certain. This is the
1,000,000x rule applied literally: when the unit is unverifiable, derive the
number from ones that are.

**Tiers are cached for 60 seconds, and that is a real trade-off.** Without a
cache, one `tools/list` costs a Google round trip per managed account. The
requirement that mattered was that a demotion must not wait for a reconnect;
a short TTL bounds that window to seconds instead of a session. Configurable
via `GADS_TIER_CACHE_SECONDS`, refused above 900, `0` disables it, and the
value is shown in `health_check`. Failures are never cached - an outage must
not become sticky.

**Reads go through the full gate too.** They cannot spend money, but the
account allowlist has to hold for them or this becomes a way to read any
account the caller has on their personal Google login, through our developer
token. Every read is audited, which is what makes "who looked at this
account, and when" answerable.

## Open risks

**Can a non-admin read their own access role? STILL OPEN.** Google documents
`customer_user_access` as how *admins* list users, and does not say whether a
STANDARD or READ_ONLY user can query their own row with their own token. The
field-reference pages are JavaScript-rendered so WebFetch cannot read them,
and the read-only Google Ads connector available here authenticates as a
Gmail account that is not in the MCC (`NOT_ADS_USER`), so it could not be
settled empirically either.

Phase 3 was built so that this is a *runtime-observable fact rather than a
design assumption*. `GoogleAdsTierResolver` never guesses: a permission
failure becomes `TierLookupError`, which fails closed with the distinct
`lookup_failed` audit verdict and a message naming the fallback. The first
non-admin who connects answers the question loudly, and nobody is
over-privileged in the meantime.

**Settle it before flipping `roles.yaml` to `mode: google_ads`** - have one
STANDARD or READ_ONLY user connect and call `health_check`. If the model
breaks, the fallback is a read-only service credential used *solely* for the
role lookup, with every actual read and write still on the user's own token;
`OverridingTierResolver` keeps the team working in the meantime via a
deliberate, in-git `users:` entry.

**Human approval is enforced by the client, not the server.** Nothing at the
protocol level stops Claude calling draft then confirm in one turn. The
server guarantees the approval is a separate authenticated call, owner-bound,
expiring, single-use and re-evaluated. A server-side guarantee needs
out-of-band approval (Slack/email) or a confirmation code the model never
sees. Decide in Phase 6.

**`policy.yaml` still holds placeholder numbers.** Every limit and the
account allowlist (`0000000000`) were invented to make tests meaningful.
They must be replaced before `GADS_WRITE_ENABLED` is ever true.

## Environment gotchas

- PowerShell 5.1 `Get-Content`/`Set-Content` **corrupts UTF-8**. Use the
  Edit/Write tools for source edits, never a shell round trip.
- The Bash tool here has no coreutils on PATH. Use PowerShell.
- `.gitattributes` forces LF so `ops/run.sh` does not reach Ubuntu with CRLF.
- The package must be `pip install -e .` for `python -m gads_write.server`;
  pytest works without it via `pythonpath` in pyproject.

## Commands

```bash
.venv/Scripts/python -m pytest          # 268 tests, no network, no credentials
.venv/Scripts/python -m gads_write.server
pm2 restart gads-write-mcp              # prod; picks up .env and config/*.yaml
```
