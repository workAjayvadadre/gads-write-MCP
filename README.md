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
4. **Policy.** `config/policy.yaml` holds the account allowlist and spend
   limits, checked at draft time **and again** at confirm time.
5. **Two-step confirm.** Writes return a preview and a `plan_id` and do
   nothing else. A separate call applies it.

Phase 1 implements 1–3. Layers 4 and 5 arrive in Phase 2.

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
| `.env` | credentials, port, kill switch | **no** |
| `config/policy.yaml` | account allowlist, spend limits | yes |
| `config/roles.yaml` | who has which tier | yes |

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
config/            policy.yaml, roles.yaml  — all limits and permissions
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
