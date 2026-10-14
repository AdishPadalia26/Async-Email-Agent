# Async Email Agent

An MCP server that lets Claude schedule emails from your own mailbox and sends them from the cloud while you're offline.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![FastMCP](https://img.shields.io/badge/FastMCP-2.10%2B-6f42c1)
![Postgres](https://img.shields.io/badge/Postgres-psycopg%203-336791)

## Overview

LLM assistants can draft an email but can't send it at 9am tomorrow if your laptop is closed. Async Email Agent is a multi-tenant [Model Context Protocol](https://modelcontextprotocol.io) server. Each user connects their own Gmail (OAuth) or SMTP mailbox, and Claude schedules emails through MCP tools. Delivery is handled by a durable delayed queue (Upstash QStash) that calls back a signed webhook at send time, so no worker process or cron job has to stay running. On the technical side, it covers serverless MCP hosting, idempotent delivery through an atomic database claim, and encrypted credential storage per tenant.

## Table of contents

- [Key features](#key-features)
- [Demo](#demo)
- [Architecture](#architecture)
- [Tech stack](#tech-stack)
- [Project structure](#project-structure)
- [Getting started](#getting-started)
- [Usage](#usage)
- [Testing](#testing)
- [Deployment](#deployment)
- [Design decisions and tradeoffs](#design-decisions-and-tradeoffs)
- [Roadmap and known limitations](#roadmap-and-known-limitations)
- [Contributing](#contributing)

## Key features

- **Four MCP tools:** `connect_mailbox`, `schedule_emails`, `list_scheduled` and `cancel_email` (`main.py`).
- **Two sending options:** Google sign-in sends via the Gmail API (`gmail.send` scope only), and SMTP with an app password works for Gmail, Outlook, Yahoo and others.
- **Timezone-aware batch scheduling:** one call creates a job per send time. Naive ISO 8601 times are read in any IANA timezone and stored in UTC.
- **Idempotent delivery:** QStash deduplicates by `job_id`, and the `/send` webhook atomically moves a job from `pending` to `sending` before it sends.
- **Multi-tenant isolation:** API keys are stored only as SHA-256 hashes, and every job query is scoped to the caller's key hash.
- **Encryption at rest:** SMTP passwords, Google refresh tokens and session cookies are encrypted with Fernet.
- **Web dashboard** (`dashboard.html`): sign in with Google, copy connection snippets for Claude Desktop, Claude Code, claude.ai or Cursor, view and cancel jobs, and rotate your key.
- **Serverless:** runs as a stateless ASGI function on Vercel (`api/index.py`) or as a long-running process (`python main.py`).

## Demo

![Demo: Claude schedules a weekly email and it's sent at 9:00 with the laptop closed](demo/app-demo.gif)

**[▶ Watch the demo video](demo/app-demo.mp4)** (20s): Claude schedules a weekly email, the laptop stays closed, and it's sent at 9:00 from your own Gmail.

**Live:** [async-email-agent.vercel.app](https://async-email-agent.vercel.app/). Sign in with Google to get an API key and a ready-made connection snippet.

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

Job lifecycle: `pending → sending → sent`. A failed send becomes `failed`, and QStash retries it (up to 3 times). A `pending` job can be `cancelled`.

## Tech stack

| Layer | Technology | Purpose |
|---|---|---|
| MCP server / web | FastMCP ≥ 2.10 (Starlette) | MCP tools, custom HTTP routes, dashboard |
| Database | PostgreSQL via `psycopg[binary]` ≥ 3.2 | Users, encrypted credentials, job state |
| Scheduler | Upstash QStash (`qstash` ≥ 2.0) | Delayed delivery, retries, request signing |
| Mail transport | Gmail REST API (`httpx`), `smtplib` | Sending messages |
| Crypto | `cryptography` Fernet | Encrypting secrets and cookies |
| Hosting | Vercel Python runtime, or Render (`render.yaml`) | Serving HTTP |

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

## Getting started

### Prerequisites

- Python 3.10+ (developed on 3.13)
- A PostgreSQL database (`.env.example` uses Neon)
- An Upstash QStash account
- Optional: a Google Cloud OAuth web client, for Google sign-in
- Optional: Node.js, for `npx mcp-remote` and `npx @modelcontextprotocol/inspector`

### Installation

```bash
git clone https://github.com/AdishPadalia26/Async-Email-Agent.git
cd Async-Email-Agent
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

### Environment variables

| Name | Description | Required | Example |
|---|---|---|---|
| `DATABASE_URL` | Postgres connection string | Yes | `postgresql://user:pass@ep-xxx.neon.tech/neondb?sslmode=require` |
| `FERNET_KEY` | Encryption key for secrets and cookies. Changing it makes stored secrets unreadable. | Yes | output of `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `QSTASH_TOKEN` | QStash API token | Yes | from the Upstash console |
| `QSTASH_CURRENT_SIGNING_KEY` | Verifies `/send` requests | Yes | from the Upstash console |
| `QSTASH_NEXT_SIGNING_KEY` | Verifies `/send` requests during key rotation | Yes | from the Upstash console |
| `QSTASH_URL` | Regional QStash endpoint | No | `https://qstash.upstash.io` |
| `PUBLIC_URL` | Public base URL, no trailing slash. Required off Vercel. | No (Vercel) | `https://<your-app>.vercel.app` |
| `GOOGLE_CLIENT_ID` | OAuth web client ID. Without it, Google sign-in returns 503. | No | from Google Cloud |
| `GOOGLE_CLIENT_SECRET` | OAuth web client secret | No | from Google Cloud |
| `PORT` | Listen port for `python main.py` | No | `8000` |

On Vercel, `PUBLIC_URL` defaults to `https://$VERCEL_PROJECT_PRODUCTION_URL`, which Vercel sets automatically.

### Run

`main.py` reads variables from the process environment and doesn't load `.env` itself, so export them first:

```bash
set -a; source .env; set +a      # bash/zsh
python main.py                   # serves http://0.0.0.0:8000, MCP at /mcp
```

The schema is applied automatically on startup. QStash has to reach `<PUBLIC_URL>/send` publicly, so to test delivery locally, expose the server with a tunnel and set `PUBLIC_URL` to the tunnel URL.

## Usage

**Connect Claude Code** (the dashboard at `/` generates this with your key filled in):

```bash
claude mcp add --transport http email-agent https://<your-app>.vercel.app/mcp --header "Authorization: Bearer ea_..."
```

**Clients that can't set headers** (claude.ai, Cursor): use `https://<your-app>.vercel.app/mcp?key=ea_...`, and treat that URL as a secret.

**SMTP without Google:** connect to `/mcp` with no key and call `connect_mailbox(email, app_password, smtp_host="smtp.gmail.com", smtp_port=465)`. It checks the login and returns an API key once.

**Schedule an email** (`schedule_emails` arguments):

```json
{
  "to": ["team@example.com"],
  "subject": "Demo reminder",
  "body": "The demo starts at 10:00.",
  "send_times": ["2026-10-15T09:00", "2026-10-15T16:00"],
  "timezone": "Asia/Kolkata"
}
```

Response shape (from `job_out` in `main.py`):

```json
{
  "from": "you@gmail.com",
  "scheduled": [
    {"job_id": "<uuid>", "to": ["team@example.com"], "subject": "Demo reminder",
     "send_at_utc": "2026-10-15T03:30:00+00:00", "status": "pending"}
  ]
}
```

**HTTP routes**

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/mcp` | Bearer or `?key=` | MCP endpoint |
| GET | `/` | none | Dashboard |
| GET | `/auth/google`, `/auth/google/callback` | `state` cookie | Google OAuth |
| GET | `/api/me`, `/api/jobs` | session cookie | Current user, jobs |
| POST | `/api/jobs/{job_id}/cancel`, `/api/key/rotate`, `/logout` | session cookie | Cancel, rotate key, sign out |
| POST | `/send` | QStash signature | Worker webhook |
| GET | `/health` | none | Config check (send URL, signing-key fingerprints, OAuth redirect URI) |

## Testing

The repo has no automated tests or linter config yet. To check it by hand:

```bash
npx @modelcontextprotocol/inspector     # connect to http://localhost:8000/mcp (Streamable HTTP)
curl http://localhost:8000/health       # send_url must match the URL QStash calls
```

## Deployment

- **Vercel (primary):** import the repo, set the environment variables and deploy. `vercel.json` rewrites every path to `api/index.py` with a 60s `maxDuration`.
- **Render:** `render.yaml` runs `python main.py` with a `/health` check. Render's free tier blocks outbound SMTP, so use Google sign-in there. The blueprint doesn't list `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`, so add them manually.
- **Google OAuth:** enable the Gmail API. Set the consent screen to External with scopes `openid`, `email` and `gmail.send`, and publishing status **In production** (in Testing, refresh tokens expire after 7 days). Use the redirect URI `<PUBLIC_URL>/auth/google/callback`.

## Design decisions and tradeoffs

- **A delayed queue instead of a scheduler process.** QStash holds each job until `send_at`, so the app stays stateless and fits serverless hosting. The cost is that the maximum scheduling horizon depends on the QStash plan.
- **Postgres is the source of truth for job state.** The `UPDATE … WHERE status IN ('pending','failed') RETURNING` claim prevents double sends, and cancelling flips the status before deleting the QStash message. A crash after the provider accepts a message leaves the job stuck in `sending` rather than risking a duplicate send.
- **A fresh stateless MCP app per request on Vercel.** FastMCP's session manager can start only once per app instance, and serverless invocations don't share state.
- **Gmail API added after SMTP.** The git history shows Vercel adopted because Render's free tier blocks outbound SMTP. OAuth with only `gmail.send` also avoids users having to create app passwords.
- **Bearer API keys instead of MCP OAuth.** Simpler to build and works in every client, including through `?key=`, but users have to copy a secret, and keys in URLs can end up in logs.

## Roadmap and known limitations

- [ ] MCP OAuth login, so there's no key to copy
- [ ] Google app verification (until then: an "unverified app" warning and a 100-user cap)
- [ ] A sweeper for jobs stuck in `sending`
- [ ] Per-user rate limits (`connect_mailbox` is unauthenticated by design)
- [ ] A reschedule tool, plus HTML bodies, CC/BCC and attachments (only plain text is sent today)
- [ ] Automated tests and CI

## Contributing

Issues and pull requests are welcome. For changes, keep `schema.sql` idempotent (it runs on every startup) and check that `/health` reports the expected `send_url` after you deploy.
