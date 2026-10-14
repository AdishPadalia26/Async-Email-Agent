# Async Email Agent

A multi-tenant MCP server: anyone connects **their own** mailbox, asks Claude to schedule emails at any times, and they're sent from the cloud while the user is offline.

**Architecture:** Claude → MCP + Bearer key → FastMCP (Vercel serverless) → Neon Postgres (users, jobs) + QStash (delays until `send_at`) → `POST /send` → Gmail API (Google sign-in) or SMTP (app password) → inbox.

**Dashboard (share this link):** `https://<your-app>.vercel.app/`. Users sign in with Google, copy a ready-made snippet for Claude Desktop, Claude Code or claude.ai/Cursor (API key filled in), and view or cancel their scheduled emails. The API key is shown once; "Rotate key" issues a new one.

**Connect without Google:** add `https://<your-app>.vercel.app/mcp` as an MCP server, call `connect_mailbox` with an SMTP app password (works for Outlook/Yahoo too), then reconnect with `Authorization: Bearer <api_key>`, for example in Claude Desktop:
```json
{ "mcpServers": { "email-agent": { "command": "npx",
  "args": ["mcp-remote", "https://<your-app>.vercel.app/mcp", "--header", "Authorization: Bearer ${KEY}"],
  "env": { "KEY": "ea_..." } } } }
```

**Tools:** `connect_mailbox` · `schedule_emails(to, subject, body, send_times, timezone)` · `list_scheduled` · `cancel_email(job_id)`. Sends are idempotent: an atomic `pending → sending` claim plus QStash dedup by `job_id`. SMTP passwords and Google refresh tokens are Fernet-encrypted, API keys are stored only as hashes, and dashboard cookies are Fernet-encrypted, HttpOnly and SameSite=Lax.

**Next steps:** MCP OAuth login (no key to copy), Google app verification, server-side LLM drafting, a sweeper for stuck `sending` jobs, per-user rate limits, reschedule.

## Run / deploy
1. `pip install -r requirements.txt`, then set the env vars from `.env.example`.
2. Import the GitHub repo in Vercel (`api/index.py` + `vercel.json`) and set the env vars. `PUBLIC_URL` must be the production domain. The schema is created automatically on first request. (`python main.py` / `render.yaml` also work on any host that allows outbound SMTP; Render's free tier does not.)
3. Google sign-in: in Google Cloud, enable the Gmail API; on the OAuth consent screen choose External, add scopes `openid`, `email`, `gmail.send`, and set publishing status to **In production** (in Testing, refresh tokens expire after 7 days). Create an OAuth client (Web application) with redirect URI `<PUBLIC_URL>/auth/google/callback` and set `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`. `/health` shows the exact redirect URI in use. Until Google verifies the app, users see an "unverified app" warning and the app is capped at 100 users. No billing account is needed.
4. Test: open `<PUBLIC_URL>/`, or `npx @modelcontextprotocol/inspector` against `<PUBLIC_URL>/mcp`.

Notes: QStash's free tier limits how far ahead a message can be delayed. Times are stored in UTC.
