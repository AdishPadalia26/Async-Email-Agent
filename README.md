# Async Email Agent

A multi-tenant MCP server: anyone connects **their own** mailbox, asks Claude to schedule emails at any times, and they're sent from the cloud while the user is offline.

**Architecture:** Claude → MCP + Bearer key → FastMCP (Vercel serverless) → Neon Postgres (users, jobs) + QStash (delays until `send_at`) → `POST /send` → SMTP → inbox.

**Connect:** add `https://<your-app>.vercel.app/mcp` as an MCP server, call `connect_mailbox` with an SMTP app password, then reconnect with `Authorization: Bearer <api_key>`, for example in Claude Desktop:
```json
{ "mcpServers": { "email-agent": { "command": "npx",
  "args": ["mcp-remote", "https://<your-app>.vercel.app/mcp", "--header", "Authorization: Bearer ${KEY}"],
  "env": { "KEY": "ea_..." } } } }
```

**Tools:** `connect_mailbox` · `schedule_emails(to, subject, body, send_times, timezone)` · `list_scheduled` · `cancel_email(job_id)`. Sends are idempotent: an atomic `pending → sending` claim plus QStash dedup by `job_id`. SMTP passwords are Fernet-encrypted and API keys are stored only as hashes.

**Next steps:** Google OAuth + Gmail API instead of app passwords, MCP OAuth login, server-side LLM drafting, a sweeper for stuck `sending` jobs, per-user rate limits, reschedule.

## Run / deploy
1. `pip install -r requirements.txt`, then set the env vars from `.env.example`.
2. Import the GitHub repo in Vercel (`api/index.py` + `vercel.json`) and set the env vars. `PUBLIC_URL` must be the production domain. The schema is created automatically on first request. (`python main.py` / `render.yaml` also work on any host that allows outbound SMTP; Render's free tier does not.)
3. Test: `npx @modelcontextprotocol/inspector`, then connect to `<PUBLIC_URL>/mcp`.

Notes: QStash's free tier limits how far ahead a message can be delayed. Times are stored in UTC.
