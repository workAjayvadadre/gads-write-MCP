# Runbook - gads-write-mcp

This server can change Google Ads campaigns and spend real money. Read the
first section before touching anything.

Written for whoever is on call, including someone who has never seen the
code. Where a step is dangerous, it says why.

---

## 0. Stop it right now

If this server is doing something wrong, in order of severity:

```bash
# 1. Make it read-only. Writes are refused at the guard; reads keep working.
#    Edit .env: GADS_WRITE_ENABLED=false
pm2 restart gads-write-mcp

# 2. Stop it entirely.
pm2 stop gads-write-mcp
```

Nothing this server does is retroactive. Stopping it prevents further
changes; it does not undo changes already applied. To undo, see section 5.

The kill switch is checked before the caller's permissions, so it beats
everything including a `lead`. It cannot be bypassed by any tool call.

---

## 1. What is running

| | |
|---|---|
| Process | PM2 app `gads-write-mcp`, **one instance** |
| Port | 8081 on 127.0.0.1, behind Nginx |
| Public | `https://adswrite.indiraivf.in` |
| Logs | `pm2 logs gads-write-mcp` |
| Audit | `logs/audit-YYYY-MM-DD.jsonl` |
| Health | `GET /healthz` (no auth) |

**`instances: 1` must stay 1.** Draft plans live in process memory. A second
worker would not see the first worker's plans, and "a plan can be applied
exactly once" would stop being true.

This is a different process from the read-only `google-ads-mcp` on port
8000. Do not merge them.

---

## 2. Is it healthy?

```bash
curl -s https://adswrite.indiraivf.in/healthz | python3 -m json.tool
```

| Response | Meaning | Action |
|---|---|---|
| `200` `"status":"ok"` | Alive, running the config on disk | None |
| `503` `"status":"degraded"` | **Alive, but a config edit was REFUSED** | Section 4 |
| Connection refused | Process or Nginx is down | Section 3 |

**Point your uptime monitor at `/healthz` and alert on any non-200.**

The degraded case is the one people miss. The server keeps working perfectly
on its last good config, so nothing looks wrong - but the limit someone
thinks they changed is not in force. Only `/healthz` says so.

---

## 3. It is down

```bash
pm2 list
pm2 logs gads-write-mcp --lines 100
```

Boot failures are loud and specific. The server refuses to start rather than
run half-configured, and lists **every** problem at once:

```
Refusing to start: 2 configuration problem(s).
  - GADS_JWT_SIGNING_KEY is required in production...
  - policy file not found at ...
```

Fix them all, then `pm2 restart gads-write-mcp`.

If PM2 is restart-looping, it is almost always a config error. `min_uptime`
is 20s so it will not loop forever, but check the logs rather than
restarting repeatedly.

---

## 4. `/healthz` says degraded

A config file was edited into an invalid state. The server **kept the last
good version** - that is deliberate, so a typo cannot take the server down or
silently relax a limit.

```bash
pm2 logs gads-write-mcp --lines 50 | grep -i "REFUSED"
```

You will see which file and why. Fix the YAML and save. Both `policy.yaml`
and `roles.yaml` hot-reload within seconds - **no restart needed**. Re-check
`/healthz` until it returns 200.

Until you fix it, the running limits are the older ones, not what the file
says.

---

## 5. Someone made a change they should not have

Every applied change is in the audit log with who, when, and what.

```bash
cd /home/ubuntu/gads-write-mcp
# What was actually applied today?
grep '"applied": true' logs/audit-$(date +%F).jsonl | python3 -m json.tool

# Everything one person did today
grep '"user_email": "someone@indiraivf.com"' logs/audit-$(date +%F).jsonl
```

Each applied line carries `resource_names` - the exact Google Ads resources
touched - and `plan_id`, plus the preview the person approved.

**To undo:** this server has no undo, and deliberately no delete or remove
tools. Reverse the change in the Google Ads UI. If it was a budget or bid,
the previous value is in the audit line's preview.

**To stop it recurring:** lower the limit in `config/policy.yaml` (takes
effect in seconds, no restart) or reduce the person's role in the Google Ads
UI (takes effect within the tier cache window, default 60s).

---

## 6. The audit log

**Do not add a logrotate rule. Do not move, truncate or delete these files.**

The daily spend ceiling is *rebuilt by reading these files*. An external
rotation that truncates or moves them silently resets every user's daily
allowance to zero-used, with no error and no log line.

Rotation is handled in-process: one file per day, and files older than
`GADS_AUDIT_RETENTION_DAYS` (default 400) are deleted automatically on
write. There is nothing to schedule and nothing to maintain.

Sizing: roughly 160 bytes per tool call. A busy day of 500 calls is ~80 KB,
so a full 400-day retention is on the order of 30 MB.

---

## 7. Routine changes

**Change a spending limit** - edit `config/policy.yaml`, save. Hot-reloads in
seconds. Verify with `/healthz` (200 = accepted, 503 = your edit was
rejected).

**Give someone access** - add them in the Google Ads UI. Nothing to do here.
Their tier comes from their own Google Ads role, within the cache window.

**Remove someone** - remove them in the Google Ads UI. They lose access
immediately: every call uses their own OAuth token, so Google refuses them
regardless of anything cached here.

**Break glass (Google Ads lookup is failing)** - in `config/roles.yaml`, set
`mode: file` and add the person under `users:`. Hot-reloads. This is visible,
in git, and obviously temporary. Undo it when Google recovers.

---

## 8. Deploying

```bash
cd /home/ubuntu/gads-write-mcp
git pull
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q            # must be all green
.venv/bin/python -m gads_write.server    # boots clean? Ctrl-C
pm2 restart gads-write-mcp
curl -s -o /dev/null -w '%{http_code}\n' https://adswrite.indiraivf.in/healthz
```

Run the server by hand once before `pm2 restart`. A config error then shows
up immediately instead of as a restart loop.

**Rollback:**

```bash
git log --oneline -5
git checkout <previous-good-commit>
.venv/bin/pip install -e '.[dev]'
pm2 restart gads-write-mcp
```

Open draft plans are lost on restart. That is safe - the worst case is
someone re-drafts a change.

---

## 9. Before first live use

- [ ] Real account ID in `config/policy.yaml` (`0000000000` is a placeholder)
- [ ] Real limits in `config/policy.yaml`
- [ ] `GADS_JWT_SIGNING_KEY` set (production refuses to boot without it)
- [ ] `GADS_REQUIRE_HUMAN_CONFIRMATION=true`
- [ ] `roles.yaml` on `mode: google_ads`
- [ ] Uptime monitor on `/healthz`
- [ ] Only then `GADS_WRITE_ENABLED=true`, and pause one campaign that does
      not matter as the first live test
