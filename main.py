"""Async Email Agent: multi-tenant MCP server that schedules emails and sends them from the cloud."""

import asyncio
import hashlib
import logging
import os
import secrets
import smtplib
import ssl
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg.rows import dict_row
from cryptography.fernet import Fernet
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from qstash import QStash, Receiver
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

def env(name: str) -> str:
    # Tolerate values pasted with surrounding quotes/whitespace (common in dashboard env editors).
    return os.environ[name].strip().strip('"').strip("'").strip()


DATABASE_URL = env("DATABASE_URL")
PUBLIC_URL = (
    env("PUBLIC_URL") if os.environ.get("PUBLIC_URL", "").strip()
    else "https://" + env("VERCEL_PROJECT_PRODUCTION_URL")  # set automatically on Vercel
).rstrip("/")
SEND_URL = f"{PUBLIC_URL}/send"
SIGNING_KEYS = (env("QSTASH_CURRENT_SIGNING_KEY"), env("QSTASH_NEXT_SIGNING_KEY"))

fernet = Fernet(env("FERNET_KEY"))
qstash = QStash(env("QSTASH_TOKEN"), base_url=env("QSTASH_URL") if os.environ.get("QSTASH_URL") else None)
receiver = Receiver(current_signing_key=SIGNING_KEYS[0], next_signing_key=SIGNING_KEYS[1])
log = logging.getLogger("email_agent")

mcp = FastMCP(
    "Async Email Agent",
    instructions=(
        "Schedules emails that are sent from the cloud, even while the user is offline. "
        "First call connect_mailbox once to get an API key, then reconnect with header "
        "'Authorization: Bearer <key>'. Always draft the email and confirm recipients, "
        "content and send times with the user before calling schedule_emails."
    ),
)


# ---------- helpers ----------

def db():
    return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def current_user() -> dict:
    """Resolve the caller from the Bearer key. Every tenant-scoped query filters by key_hash."""
    auth = get_http_request().headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise ToolError("Missing 'Authorization: Bearer <api_key>' header. Call connect_mailbox first.")
    with db() as conn:
        user = conn.execute(
            "select key_hash, email from users where key_hash = %s", (hash_key(auth[7:].strip()),)
        ).fetchone()
    if not user:
        raise ToolError("Invalid API key.")
    return user


def smtp_login(host: str, port: int, email: str, password: str) -> smtplib.SMTP:
    ctx = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=20)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
        server.starttls(context=ctx)
    server.ehlo()
    if "PLAIN" in server.esmtp_features.get("auth", "").upper().split():
        # Single mechanism: smtplib's login() retries with AUTH LOGIN after a 535, and Gmail then
        # drops the connection, masking the real "bad credentials" error.
        server.user, server.password = email, password
        server.auth("PLAIN", server.auth_plain, initial_response_ok=True)
    else:
        server.login(email, password)
    return server


def parse_send_time(value: str, tz: ZoneInfo) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise ToolError(f"Bad send time {value!r}; use ISO 8601 like '2026-10-14T09:30'.")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(dt_timezone.utc)


def job_out(row: dict) -> dict:
    return {
        "job_id": str(row["job_id"]),
        "to": row["to_addrs"],
        "subject": row["subject"],
        "send_at_utc": row["send_at"].isoformat(),
        "status": row["status"],
        **({"error": row["error"]} if row.get("error") else {}),
    }


# ---------- MCP tools ----------

@mcp.tool
def connect_mailbox(
    email: str,
    app_password: str,
    smtp_host: str = "smtp.gmail.com",
    smtp_port: int = 465,
) -> dict:
    """Connect the user's own mailbox via SMTP + app password (Gmail: enable 2FA, then create an
    App Password). Verifies the login, stores the password encrypted, and returns a NEW API key.
    The user must save the key and send it as 'Authorization: Bearer <key>' on future connections.
    Outlook: smtp-mail.outlook.com:587. Yahoo: smtp.mail.yahoo.com:465."""
    try:
        smtp_login(smtp_host, smtp_port, email, app_password).quit()
    except Exception as e:
        raise ToolError(f"SMTP login failed: {e}")

    api_key = "ea_" + secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute(
            "insert into users (key_hash, email, smtp_host, smtp_port, enc_password) values (%s, %s, %s, %s, %s)",
            (hash_key(api_key), email, smtp_host, smtp_port, fernet.encrypt(app_password.encode()).decode()),
        )
    return {
        "api_key": api_key,
        "email": email,
        "note": "Save this key now; it is shown only once. Send it as 'Authorization: Bearer <key>'.",
    }


@mcp.tool
def schedule_emails(
    to: list[str],
    subject: str,
    body: str,
    send_times: list[str],
    timezone: str = "UTC",
) -> dict:
    """Schedule an email to be sent from the user's mailbox at one or more times. Draft the email
    and confirm recipients, content and times with the user BEFORE calling this.
    send_times: ISO 8601 strings, e.g. '2026-10-14T09:30'. Times without an offset are interpreted
    in `timezone` (IANA name like 'Asia/Kolkata'). One job is created per time."""
    user = current_user()
    if not to or not send_times:
        raise ToolError("Need at least one recipient and one send time.")
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        raise ToolError(f"Unknown timezone {timezone!r}; use an IANA name like 'America/New_York'.")

    now = datetime.now(dt_timezone.utc)
    times = [parse_send_time(t, tz) for t in send_times]
    past = [t.isoformat() for t in times if t < now - timedelta(minutes=1)]
    if past:
        raise ToolError(f"These times are in the past: {past}")

    jobs = []
    with db() as conn:
        for send_at in times:
            job_id = uuid.uuid4()
            conn.execute(
                "insert into jobs (job_id, key_hash, to_addrs, subject, body, send_at) values (%s, %s, %s, %s, %s, %s)",
                (job_id, user["key_hash"], to, subject, body, send_at),
            )
            try:
                res = qstash.message.publish_json(
                    url=SEND_URL,
                    body={"job_id": str(job_id)},
                    not_before=int(send_at.timestamp()),
                    deduplication_id=str(job_id),
                    retries=3,
                )
            except Exception as e:
                conn.execute(
                    "update jobs set status = 'failed', error = %s where job_id = %s", (f"queue error: {e}", job_id)
                )
                raise ToolError(f"Failed to queue email for {send_at.isoformat()}: {e}")
            row = conn.execute(
                "update jobs set qstash_id = %s where job_id = %s returning *", (res.message_id, job_id)
            ).fetchone()
            jobs.append(job_out(row))
    return {"from": user["email"], "scheduled": jobs}


@mcp.tool
def list_scheduled() -> list[dict]:
    """List this user's emails (pending, sent, failed, cancelled), soonest first."""
    user = current_user()
    with db() as conn:
        rows = conn.execute(
            "select * from jobs where key_hash = %s order by send_at", (user["key_hash"],)
        ).fetchall()
    return [job_out(r) for r in rows]


@mcp.tool
def cancel_email(job_id: str) -> dict:
    """Cancel a pending scheduled email by job_id (from schedule_emails or list_scheduled)."""
    user = current_user()
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        raise ToolError("Invalid job_id.")
    with db() as conn:
        # Flip status first: even if the QStash delete fails, /send's atomic claim will skip it.
        row = conn.execute(
            "update jobs set status = 'cancelled' where job_id = %s and key_hash = %s and status = 'pending' returning *",
            (jid, user["key_hash"]),
        ).fetchone()
        if not row:
            existing = conn.execute(
                "select status from jobs where job_id = %s and key_hash = %s", (jid, user["key_hash"])
            ).fetchone()
            raise ToolError(f"Cannot cancel: job is {existing['status']}." if existing else "Job not found.")
    if row["qstash_id"]:
        try:
            qstash.message.cancel(row["qstash_id"])
        except Exception:
            pass  # already delivered/expired; DB status guards against sending
    return job_out(row)


# ---------- worker route (called by QStash) ----------

def deliver(job: dict) -> None:
    password = fernet.decrypt(job["enc_password"].encode()).decode()
    msg = EmailMessage()
    msg["From"] = job["email"]
    msg["To"] = ", ".join(job["to_addrs"])
    msg["Subject"] = job["subject"]
    msg.set_content(job["body"])
    server = smtp_login(job["smtp_host"], job["smtp_port"], job["email"], password)
    try:
        server.send_message(msg)
    finally:
        server.quit()


@mcp.custom_route("/send", methods=["POST"])
async def send_route(request: Request):
    raw = await request.body()
    try:
        receiver.verify(signature=request.headers.get("upstash-signature", ""), body=raw.decode(), url=SEND_URL)
    except Exception as e:
        log.warning("rejected /send: %s (expected url %s)", e, SEND_URL)
        return PlainTextResponse("invalid signature", status_code=401)

    try:
        job_id = uuid.UUID((await request.json())["job_id"])
    except Exception:
        return PlainTextResponse("bad body", status_code=400)  # non-retryable garbage

    def claim():
        # Atomic claim: only one delivery can move a job out of pending/failed -> no double sends.
        with db() as conn:
            return conn.execute(
                """update jobs j set status = 'sending'
                   from users u
                   where j.job_id = %s and j.key_hash = u.key_hash and j.status in ('pending', 'failed')
                   returning j.*, u.email, u.smtp_host, u.smtp_port, u.enc_password""",
                (job_id,),
            ).fetchone()

    job = await asyncio.to_thread(claim)
    if not job:
        return PlainTextResponse("nothing to do")  # cancelled, already sent, or in flight

    def finish(status: str, error: str | None):
        with db() as conn:
            conn.execute(
                "update jobs set status = %s, error = %s, sent_at = case when %s = 'sent' then now() end where job_id = %s",
                (status, error, status, job_id),
            )

    try:
        await asyncio.to_thread(deliver, job)
    except Exception as e:
        await asyncio.to_thread(finish, "failed", str(e)[:500])
        return PlainTextResponse("send failed", status_code=500)  # QStash retries
    await asyncio.to_thread(finish, "sent", None)
    return PlainTextResponse("sent")


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request):
    # Non-secret config fingerprints, to debug env mismatches without exposing keys.
    fp = lambda s: hashlib.sha256(s.encode()).hexdigest()[:8]
    return JSONResponse({"ok": True, "send_url": SEND_URL, "signing_key_fps": [fp(k) for k in SIGNING_KEYS]})


def init_db() -> None:
    with db() as conn:
        conn.execute(Path(__file__).with_name("schema.sql").read_text())


if __name__ == "__main__":
    init_db()
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
