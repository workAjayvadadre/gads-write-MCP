# gads-write-mcp

A guardrailed **write** MCP server for Google Ads, for the marketing team to
use from Claude web and desktop.

This server can spend real money. Every design decision here is downstream of
that fact.

Separate repo, separate process, separate hostname (`adswrite.indiraivf.in`)
from the read-only `googleads/google-ads-mcp` already on this box. Google's
code is not forked or modified.

---

## Current state: Phase 1 complete, awaiting verification

Phase 1 is the skeleton: config validation, Google OAuth, caller identity,
role resolution, and one `health_check` tool.

**There is no Google Ads client in this phase.** The `google-ads` library is
not even a dependency — it is added in Phase 3. Nothing in this codebase can
currently touch an ad account.

| Phase | Scope | State |
|---|---|---|
| 1 | Skeleton: settings, OAuth, `health_check`, ops | **built** |
| 2 | Safety core against a fake executor, no API calls | not started |
| 3 | Reads: per-user client, accounts, performance, search terms | not started |
| 4 | First mutation (`pause`/`enable`), test account only | not started |
| 5 | Budget, bids, negatives, keywords, RSA | not started |
| 6 | Rollout: runbook, log shipping, alerting, rollback | not started |

---

## How safety is layered

Five independent things must all agree before a single field changes. Any one
of them says no, nothing happens.

1. **Google's own permissions.** Every Ads call uses the calling user's OAuth
   token. No static refresh token exists anywhere. Remove someone from the MCC
   and they lose access immediately — no redeploy, no config edit here.
2. **The kill switch.** `GADS_WRITE_ENABLED=false` makes the whole server
   read-only. One env change, one restart.
3. **Role tier.** `config/roles.yaml` maps email to `readonly` / `operator` /
   `lead`. Unlisted people get `none`.
4. **Managed accounts.** The accounts this server may touch are derived from
   your MCC, not configured. That protects your developer token; it is not a
   spend control.
5. **Parity with the Google Ads UI.** You can do here what you could already
   do there. Very little is refused: only what cannot be undone (there are no
   delete tools), an account outside your MCC, or a change whose preview would
   misrepresent it. Risky-but-legitimate choices are warnings, not blocks.
   One relative backstop catches a stray digit.
6. **Two-step confirm.** Writes return a preview and a `plan_id` and do
   nothing else. A separate call applies it, and a person must accept the
   preview first. This is the real control — everything above supports it.

---

## Local setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'      # Windows: .venv\Scripts\pip
cp .env.example .env                   # then fill it in
.venv/bin/python -m pytest             # 15 tests, no network needed
```

Note for the Windows dev box: `ops/run.sh` and `ops/nginx.conf` are for the
Ubuntu server. Locally you only need `python -m gads_write.server`, and even
that needs a `.env` with real OAuth credentials to be useful.

## Configuration

Three files, and only these three, decide what this server can do:

| File | Controls | Committed? |
|---|---|---|
| `.env` | credentials, port, kill switch, the two spend settings | **no** |
| `config/roles.yaml` | break-glass tier overrides — **optional, normally absent** | yes |

There is no policy file. Everything it used to hold is now derived or fixed:

| Was in `policy.yaml` | Now |
|---|---|
| `allowed_customer_ids` | every account under `GADS_LOGIN_CUSTOMER_ID` |
| `currency_code`, `timezone` | read from each account |
| `min_daily`, `max_daily`, `max_cpc` | **removed** — a rupee figure is wrong for some account, and always goes stale |
| `max_increase_percent` | `GADS_MAX_INCREASE_PERCENT`, default 1000 — a typo backstop, not an operating limit |
| daily increase ceiling | **removed as a limit** — shown on the preview instead, so the person approving decides |
| `block_broad_match_with_manual_cpc` | **now a warning on the preview**, not a refusal |
| `allowed_final_url_domains` | `GADS_ALLOWED_URL_DOMAINS` |
| structural rules, blocked ops, plan TTL | fixed in `safety/policy.py` |

The point of that table: **deploy it, add people in Google Ads, hand over the
URL.** Nobody edits a file to add an account or onboard a colleague.

No customer ID, budget limit, or email address may be hardcoded in Python.

The server validates all of this at boot and **refuses to start** if anything
is missing or malformed — reporting every problem at once, not one per
restart. Try it: unset `GADS_DEVELOPER_TOKEN` and start the server.

---

## Deploying (Ubuntu EC2)

### 1. Google Cloud console

Create an OAuth 2.0 **Web application** client (or reuse the existing project
— a *new client*, so this server's sessions are independent of the read-only
one). Set:

- Authorised JavaScript origin: `https://adswrite.indiraivf.in`
- Authorised redirect URI: `https://adswrite.indiraivf.in/auth/callback`

Scopes requested are `openid`, `userinfo.email`, and `adwords`. The `adwords`
scope is requested from Phase 1 even though nothing uses it until Phase 3 —
adding a scope later forces every user to re-consent.

### 2. DNS and TLS

Point `adswrite.indiraivf.in` at the box, then:

```bash
sudo cp ops/nginx.conf /etc/nginx/sites-available/adswrite.indiraivf.in
sudo ln -s /etc/nginx/sites-available/adswrite.indiraivf.in /etc/nginx/sites-enabled/
sudo certbot --nginx -d adswrite.indiraivf.in
sudo nginx -t && sudo systemctl reload nginx
```

The Nginx config disables response buffering and raises the read timeout to
300s. Both are required: MCP streamable HTTP delivers server-sent events, and
Nginx's defaults would buffer them and cut the connection at 60s.

### 3. Application

```bash
git clone <repo> /home/ubuntu/gads-write-mcp && cd $_
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env && $EDITOR .env    # keep GADS_WRITE_ENABLED=false
# set cwd in ops/ecosystem.config.js to this directory
pm2 start ops/ecosystem.config.js
pm2 save
pm2 logs gads-write-mcp
```

Port `8081`, so it does not collide with the read-only server on `8000`.
`instances: 1` is deliberate and must stay — Phase 2 holds draft plans in
process memory, and a second worker would not see plans drafted by the first.

### 4. Connect from Claude

Add a custom connector pointing at `https://adswrite.indiraivf.in/mcp`.

---

## Phase 1 verification

Do these in order. Do not proceed to Phase 2 until all four pass.

1. **Boot fails loudly on bad config.** Comment out `GADS_DEVELOPER_TOKEN` in
   `.env`, run `pm2 restart gads-write-mcp`, check `pm2 logs`. Expect a
   refusal listing the problem, not a running server. Put it back.

2. **You see yourself.** Connect from Claude web, run `health_check`. Expect
   your own email, `tier: lead`, and `write_enabled: false`.

3. **Someone else sees themselves.** A second person connects and runs
   `health_check`. Expect *their* email — not yours, not a shared identity.
   This is the one that actually proves per-user OAuth. If both of you see
   the same email, stop; something is badly wrong.

4. **An unlisted person is fenced out.** Someone not in `config/roles.yaml`
   connects and runs `health_check`. Expect `tier: none` and the "ask a lead
   to add you" note.

Worth also checking in step 2: `credentials.google_token_available` should be
`true`. That proves the Phase 3 credential path already works, without the
token ever leaving `auth/identity.py`.

---

## Where things live

```
config/            roles.yaml — break-glass overrides, normally absent
src/gads_write/
  server.py        FastMCP app, tool registration
  settings.py      boot validation, fails fast
  auth/            identity.py (who), roles.py (what tier)
  ads/             phase 3+ — executor.py is the ONLY mutate call site
  safety/          phase 2 — guards, policy, plans, validators, audit
  tools/           phase 3+ — one module per tool group
tests/
ops/               ecosystem.config.js, nginx.conf, run.sh
```

## Money is in micros

Google Ads expresses currency in **millionths** of the unit. ₹500/day is
`500_000_000` micros. A missing conversion is a 1,000,000× error that
type-checks perfectly and passes review.

Rule: every variable holding money carries its unit in the name —
`budget_micros`, `budget_rupees` — and conversion happens in exactly one
place. Never a bare `budget`.
