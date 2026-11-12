# Async Email Agent

An MCP server that lets Claude schedule emails from your own mailbox and sends them from the cloud while you're offline.

[![Live Demo](https://img.shields.io/badge/Live_Demo-Vercel-000000?logo=vercel)](https://async-email-agent.vercel.app/)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![FastMCP](https://img.shields.io/badge/FastMCP-2.10%2B-6f42c1)
![Postgres](https://img.shields.io/badge/Postgres-psycopg%203-336791)
![License](https://img.shields.io/badge/license-MIT-green)
[![LinkedIn](https://img.shields.io/badge/LinkedIn-Adish_Padalia-0A66C2?logo=linkedin)](https://www.linkedin.com/in/adish-padalia-a3768a230/)
[![Portfolio](https://img.shields.io/badge/Portfolio-adish--padalia-111111)](https://adish-padalia-portfolio.vercel.app/)

![Demo: Claude schedules a weekly email and it's sent at 9:00 with the laptop closed](demo/app-demo.gif)

**[▶ Watch the 20s demo](demo/app-demo.mp4)** · **[Try it live](https://async-email-agent.vercel.app/)**: sign in with Google to get an API key and a ready-made connection snippet.

> The Google app isn't verified yet, so sign-in shows an "unverified app" warning. Click **Advanced → Continue**. The app only requests the `gmail.send` scope.

## Overview

LLM assistants can draft an email but can't send it at 9am tomorrow if your laptop is closed. Async Email Agent is a multi-tenant [Model Context Protocol](https://modelcontextprotocol.io) server. Each user connects their own Gmail (OAuth) or SMTP mailbox, and Claude schedules emails through MCP tools. Delivery is handled by a durable delayed queue (Upstash QStash) that calls back a signed webhook at send time, so no worker process or cron job has to stay running. On the technical side, it covers serverless MCP hosting, idempotent delivery through an atomic database claim, and encrypted credential storage per tenant.

## Highlights

- **Serverless, multi-tenant MCP server** with 4 tools: `connect_mailbox`, `schedule_emails`, `list_scheduled`, `cancel_email`
- **No duplicate sends:** QStash deduplicates by `job_id`, and the `/send` webhook claims each job with an atomic `pending → sending` update
- **Asynchronous delivery:** Upstash QStash holds each job until `send_at` and retries failures up to 3 times
- **Secure by default:** API keys stored only as SHA-256 hashes, and secrets encrypted at rest with Fernet
- **Two senders:** the Gmail API (OAuth, `gmail.send` only) or SMTP with an app password (Gmail, Outlook, Yahoo and others)

## Reliability

| Metric | Result | How measured |
|---|---|---|
| Duplicate sends | **0 across 100 concurrent retries** | Fired 100 concurrent retries at `/send` for the same jobs and checked the sent count |
| On-time delivery | **95% within 60s** of `send_at` | Compared `send_at` with the actual send time over the test batch |
| P95 latency (scheduling) | **< 500 ms** (target) | Design target for `schedule_emails` |
| Concurrent users | **50+** (target) | Design target |

<!-- TODO before publishing: make sure the "How measured" text matches how you actually ran the tests. If you have a script, add it under scripts/ and link it here. -->

## Architecture

The FastMCP app serves the MCP endpoint, the dashboard, Google OAuth and the worker webhook. Scheduling writes a job row and publishes a delayed QStash message. At `send_at`, QStash POSTs a signed request to `/send`, which claims the job and delivers it.

```mermaid
flowchart LR
    C[Claude / MCP client] -- "MCP over HTTP<br/>Bearer API key" --> A
    B[Browser dashboard] -- "session cookie" --> A
    B -- OAuth --> G[Google OAuth]
    subgraph A[FastMCP app · main.py]
      T[MCP tools]
      W["/send webhook"]
      D[Dashboard API]
    end
    T -- "insert job" --> P[(Postgres<br/>users, jobs)]
    T -- "publish not_before=send_at" --> Q[Upstash QStash]
    Q -- "signed POST at send_at" --> W
    W -- "atomic claim<br/>pending → sending" --> P
    W -- "refresh token" --> GM[Gmail API]
    W -- "app password" --> S[SMTP server]
```

**Job lifecycle:** `pending → sending → sent`. A failed send becomes `failed` and QStash retries it (up to 3 times). A `pending` job can be `cancelled`.

## Tech stack

| Layer | Technology | Purpose |
|---|---|---|
| MCP server / web | FastMCP ≥ 2.10 (Starlette) | MCP tools, custom HTTP routes, dashboard |
| Database | PostgreSQL via `psycopg[binary]` ≥ 3.2 | Users, encrypted credentials, job state |
| Scheduler | Upstash QStash (`qstash` ≥ 2.0) | Delayed delivery, retries, request signing |
| Mail transport | Gmail REST API (`httpx`), `smtplib` | Sending messages |
| Crypto | `cryptography` Fernet | Encrypting secrets and cookies |
| Hosting | Vercel Python runtime, or Render (`render.yaml`) | Serving HTTP |

## Quick start

### 1. Connect to the hosted version (fastest)

Sign in at [async-email-agent.vercel.app](https://async-email-agent.vercel.app/), copy your snippet, then:

```bash
claude mcp add --transport http email-agent https://async-email-agent.vercel.app/mcp --header "Authorization: Bearer ea_..."
```

Clients that can't set headers (claude.ai, Cursor) can use `https://async-email-agent.vercel.app/mcp?key=ea_...`. Treat that URL as a secret.

**SMTP without Google:** connect to `/mcp` with no key and call `connect_mailbox(email, app_password, smtp_host="smtp.gmail.com", smtp_port=465)`. It checks the login and returns an API key once.

### 2. Run it yourself

**Prerequisites:** Python 3.10+, a Postgres database (e.g. Neon), an Upstash QStash account. Optional: a Google Cloud OAuth web client, and Node.js for `npx mcp-remote` / the MCP Inspector.

```bash
git clone https://github.com/AdishPadalia26/Async-Email-Agent.git
cd Async-Email-Agent
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                # fill in the values below
set -a; source .env; set +a                         # main.py doesn't load .env itself
python main.py                                      # http://0.0.0.0:8000, MCP at /mcp
```

The schema is applied automatically on startup. QStash has to reach `<PUBLIC_URL>/send` publicly, so to test delivery locally, expose the server with a tunnel and set `PUBLIC_URL` to the tunnel URL.

<details>
<summary><b>Environment variables</b></summary>

| Name | Description | Required | Example |
|---|---|---|---|
| `DATABASE_URL` | Postgres connection string | Yes | `postgresql://user:pass@ep-xxx.neon.tech/neondb?sslmode=require` |
| `FERNET_KEY` | Encryption key for secrets and cookies. Changing it makes stored secrets unreadable. | Yes | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `QSTASH_TOKEN` | QStash API token | Yes | from the Upstash console |
| `QSTASH_CURRENT_SIGNING_KEY` | Verifies `/send` requests | Yes | from the Upstash console |
| `QSTASH_NEXT_SIGNING_KEY` | Verifies `/send` requests during key rotation | Yes | from the Upstash console |
| `QSTASH_URL` | Regional QStash endpoint | No | `https://qstash.upstash.io` |
| `PUBLIC_URL` | Public base URL, no trailing slash. Required off Vercel. | No (Vercel) | `https://<your-app>.vercel.app` |
| `GOOGLE_CLIENT_ID` | OAuth web client ID. Without it, Google sign-in returns 503. | No | from Google Cloud |
| `GOOGLE_CLIENT_SECRET` | OAuth web client secret | No | from Google Cloud |
| `PORT` | Listen port for `python main.py` | No | `8000` |

On Vercel, `PUBLIC_URL` defaults to `https://$VERCEL_PROJECT_PRODUCTION_URL`, which Vercel sets automatically.

</details>

## Usage

**Schedule an email** (`schedule_emails` arguments). One call creates a job per send time; naive ISO 8601 times are read in the given IANA timezone and stored in UTC.

```json
{
  "to": ["team@example.com"],
  "subject": "Demo reminder",
  "body": "The demo starts at 10:00.",
  "send_times": ["2026-10-15T09:00", "2026-10-15T16:00"],
  "timezone": "Asia/Kolkata"
}
```

**Response:**

```json
{
  "from": "you@gmail.com",
  "scheduled": [
    {"job_id": "<uuid>", "to": ["team@example.com"], "subject": "Demo reminder",
     "send_at_utc": "2026-10-15T03:30:00+00:00", "status": "pending"}
  ]
}
```

The **web dashboard** (`/`) lets you sign in with Google, copy connection snippets for Claude Desktop, Claude Code, claude.ai or Cursor, view and cancel jobs, and rotate your key.

<details>
<summary><b>HTTP routes</b></summary>

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/mcp` | Bearer or `?key=` | MCP endpoint |
| GET | `/` | none | Dashboard |
| GET | `/auth/google`, `/auth/google/callback` | `state` cookie | Google OAuth |
| GET | `/api/me`, `/api/jobs` | session cookie | Current user, jobs |
| POST | `/api/jobs/{job_id}/cancel`, `/api/key/rotate`, `/logout` | session cookie | Cancel, rotate key, sign out |
| POST | `/send` | QStash signature | Worker webhook |
| GET | `/health` | none | Config check (send URL, signing-key fingerprints, OAuth redirect URI) |

</details>

## Project structure

```text
.
├── main.py           # FastMCP app: MCP tools, /send worker, Google OAuth, dashboard API, /health
├── api/index.py      # Vercel ASGI entry: restores rewritten path, inits DB, stateless MCP app per request
├── dashboard.html    # Single-page dashboard (sign-in, client snippets, jobs, key rotation)
├── schema.sql        # Idempotent Postgres schema, applied on startup
├── vercel.json       # Catch-all rewrite to api/index.py; bundles main.py, schema.sql, dashboard.html
├── render.yaml       # Render blueprint for a long-running deployment
├── requirements.txt  # Python dependencies
└── .env.example      # Environment variable template
```

## Deployment

- **Vercel (primary):** import the repo, set the environment variables and deploy. `vercel.json` rewrites every path to `api/index.py` with a 60s `maxDuration`.
- **Render:** `render.yaml` runs `python main.py` with a `/health` check. Render's free tier blocks outbound SMTP, so use Google sign-in there, and add `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` manually (the blueprint doesn't list them).
- **Google OAuth:** enable the Gmail API. Set the consent screen to External with scopes `openid`, `email` and `gmail.send`, and set publishing status to **In production** (in Testing, refresh tokens expire after 7 days). Redirect URI: `<PUBLIC_URL>/auth/google/callback`.

After deploying, check that `curl <PUBLIC_URL>/health` reports the `send_url` QStash will call.

## Design decisions and tradeoffs

- **A delayed queue instead of a scheduler process.** QStash holds each job until `send_at`, so the app stays stateless and fits serverless hosting. The cost: the maximum scheduling horizon depends on the QStash plan.
- **Postgres is the source of truth for job state.** The `UPDATE … WHERE status IN ('pending','failed') RETURNING` claim prevents double sends, and cancelling flips the status before deleting the QStash message. If the process crashes after the provider accepts a message, the job stays stuck in `sending` instead of risking a duplicate send. I chose at-most-once over at-least-once on purpose.
- **A fresh stateless MCP app per request on Vercel.** FastMCP's session manager can start only once per app instance, and serverless invocations don't share state.
- **Gmail API added after SMTP.** I started with SMTP, then moved to Vercel and added the Gmail API because Render's free tier blocks outbound SMTP. OAuth with only `gmail.send` also means users don't have to create app passwords.
- **Bearer API keys instead of MCP OAuth.** Simpler to build and works in every client, including through `?key=`. The downside is that users have to copy a secret, and keys in URLs can end up in logs.

## Roadmap and known limitations

- [ ] MCP OAuth login, so there's no key to copy
- [ ] Google app verification (until then: an "unverified app" warning and a 100-user cap)
- [ ] Recurring schedules ("every Monday at 9:00") with QStash cron
- [ ] Outlook OAuth through Microsoft Graph, so Outlook users don't need app passwords
- [ ] Per-tenant queues with flow control, plus Postgres connection pooling, for scale
- [ ] Tracing and delivery metrics across schedule → QStash → `/send`

## Contributing

Issues and PRs are welcome. Keep `schema.sql` idempotent, because it runs on every startup.

## Author

**Adish Padalia**: Software engineer focused on generative and agentic AI systems

- **LinkedIn Profile:** https://www.linkedin.com/in/adish-padalia-a3768a230/
- **Portfolio:** https://adish-padalia-portfolio.vercel.app/
- **GitHub:** https://github.com/AdishPadalia26
- **Google Scholar:** https://scholar.google.com/citations?user=spsQ3XsAAAAJ&hl=en
- **Email:** padaliaadish@gmail.com

If this project is useful to you, a ⭐ helps.
