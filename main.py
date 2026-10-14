"""Async Email Agent: multi-tenant MCP server that schedules emails and sends them from the cloud."""

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import smtplib
import ssl
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import psycopg
from psycopg.rows import dict_row
from cryptography.fernet import Fernet
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from qstash import QStash, Receiver
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

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

# Google sign-in (optional: the dashboard's Google button returns 503 until these are set).
GOOGLE_CLIENT_ID = env("GOOGLE_CLIENT_ID") if os.environ.get("GOOGLE_CLIENT_ID", "").strip() else ""
GOOGLE_CLIENT_SECRET = env("GOOGLE_CLIENT_SECRET") if os.environ.get("GOOGLE_CLIENT_SECRET", "").strip() else ""
GOOGLE_REDIRECT_URI = f"{PUBLIC_URL}/auth/google/callback"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
SESSION_TTL = 7 * 24 * 3600
DASHBOARD_HTML = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")

mcp = FastMCP(
    "Async Email Agent",
    instructions=(
        "Schedules emails that are sent from the cloud, even while the user is offline. "
        f"The user gets an API key by signing in with Google at {PUBLIC_URL} (or by calling "
        "connect_mailbox once), then connects with header 'Authorization: Bearer <key>'. "
        "Always draft the email and confirm recipients, content and send times with the user "
        "before calling schedule_emails."
    ),
)


# ---------- helpers ----------

def db():
    return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def new_api_key() -> str:
    return "ea_" + secrets.token_urlsafe(32)


def rotate_key(conn, old_hash: str) -> str:
    """Give a user a new API key. key_hash is the primary key (and jobs reference it without
    ON UPDATE CASCADE), so copy the user row, repoint jobs, then drop the old row."""
    api_key = new_api_key()
    with conn.transaction():
        conn.execute(
            """insert into users (key_hash, email, provider, smtp_host, smtp_port, enc_password, enc_refresh_token, created_at)
               select %s, email, provider, smtp_host, smtp_port, enc_password, enc_refresh_token, created_at
               from users where key_hash = %s""",
            (hash_key(api_key), old_hash),
        )
        conn.execute("update jobs set key_hash = %s where key_hash = %s", (hash_key(api_key), old_hash))
        conn.execute("delete from users where key_hash = %s", (old_hash,))
    return api_key


def current_user() -> dict:
    """Resolve the caller from the Bearer key. Every tenant-scoped query filters by key_hash."""
    request = get_http_request()
    auth = request.headers.get("authorization", "")
    # Fallback for clients that can't set headers: <url>/mcp?key=ea_...
    if not auth.lower().startswith("bearer ") and request.query_params.get("key"):
        auth = "Bearer " + request.query_params["key"]
    if not auth.lower().startswith("bearer "):
        raise ToolError(f"Missing API key: send 'Authorization: Bearer <api_key>' or connect to /mcp?key=<api_key>. Get a key by signing in at {PUBLIC_URL} or calling connect_mailbox.")
    # Last token only: tolerates "Bearer Bearer ea_..." (clients that prefix "Bearer" themselves) and quotes.
    api_key = auth.split()[-1].strip("\"'") if auth.split() else ""
    with db() as conn:
        user = conn.execute(
            "select key_hash, email from users where key_hash = %s", (hash_key(api_key),)
        ).fetchone()
    if not user:
        raise ToolError(f"Invalid API key (received one starting {api_key[:6]!r}, length {len(api_key)}; keys start 'ea_' and are 46 chars).")
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

    api_key = new_api_key()
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


def list_jobs(key_hash: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute("select * from jobs where key_hash = %s order by send_at", (key_hash,)).fetchall()
    return [job_out(r) for r in rows]


def cancel_job(key_hash: str, job_id: str) -> dict:
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        raise ToolError("Invalid job_id.")
    with db() as conn:
        # Flip status first: even if the QStash delete fails, /send's atomic claim will skip it.
        row = conn.execute(
            "update jobs set status = 'cancelled' where job_id = %s and key_hash = %s and status = 'pending' returning *",
            (jid, key_hash),
        ).fetchone()
        if not row:
            existing = conn.execute(
                "select status from jobs where job_id = %s and key_hash = %s", (jid, key_hash)
            ).fetchone()
            raise ToolError(f"Cannot cancel: job is {existing['status']}." if existing else "Job not found.")
    if row["qstash_id"]:
        try:
            qstash.message.cancel(row["qstash_id"])
        except Exception:
            pass  # already delivered/expired; DB status guards against sending
    return job_out(row)


@mcp.tool
def list_scheduled() -> list[dict]:
    """List this user's emails (pending, sent, failed, cancelled), soonest first."""
    return list_jobs(current_user()["key_hash"])


@mcp.tool
def cancel_email(job_id: str) -> dict:
    """Cancel a pending scheduled email by job_id (from schedule_emails or list_scheduled)."""
    return cancel_job(current_user()["key_hash"], job_id)


# ---------- worker route (called by QStash) ----------

def gmail_send(enc_refresh_token: str, msg: EmailMessage) -> None:
    token = httpx.post(GOOGLE_TOKEN_URL, timeout=20, data={
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": fernet.decrypt(enc_refresh_token.encode()).decode(),
        "grant_type": "refresh_token",
    })
    if token.status_code != 200:
        raise RuntimeError(f"Google token refresh failed ({token.status_code}): {token.text[:200]}")
    res = httpx.post(
        GMAIL_SEND_URL,
        headers={"Authorization": f"Bearer {token.json()['access_token']}"},
        json={"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()},
        timeout=20,
    )
    if res.status_code != 200:
        raise RuntimeError(f"Gmail send failed ({res.status_code}): {res.text[:200]}")


def deliver(job: dict) -> None:
    msg = EmailMessage()
    msg["From"] = job["email"]
    msg["To"] = ", ".join(job["to_addrs"])
    msg["Subject"] = job["subject"]
    msg.set_content(job["body"])
    if job["provider"] == "google":
        gmail_send(job["enc_refresh_token"], msg)
        return
    password = fernet.decrypt(job["enc_password"].encode()).decode()
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
                   returning j.*, u.email, u.provider, u.smtp_host, u.smtp_port, u.enc_password, u.enc_refresh_token""",
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


# ---------- Google sign-in + dashboard ----------

def set_cookie(resp, name: str, value: str, max_age: int) -> None:
    # Fernet-encrypted and timestamped, so cookies can't be forged and expire server-side too.
    resp.set_cookie(
        name, fernet.encrypt(value.encode()).decode(), max_age=max_age,
        httponly=True, secure=PUBLIC_URL.startswith("https://"), samesite="lax",
    )


def read_cookie(request: Request, name: str, max_age: int) -> str | None:
    try:
        return fernet.decrypt(request.cookies[name].encode(), ttl=max_age).decode()
    except Exception:
        return None


def id_token_claims(id_token: str) -> dict:
    # Received directly from Google's token endpoint over TLS, so no signature check is needed.
    payload = id_token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def upsert_google_user(email: str, refresh_token: str) -> tuple[str, str | None]:
    """Returns (key_hash, new api key). Returning users keep their key (it's already pasted into
    Claude); they get a fresh key only on first sign-in or via 'Rotate key'."""
    enc = fernet.encrypt(refresh_token.encode()).decode()
    with db() as conn:
        user = conn.execute(
            "select key_hash from users where provider = 'google' and email = %s order by created_at desc limit 1",
            (email,),
        ).fetchone()
        if user:
            conn.execute("update users set enc_refresh_token = %s where key_hash = %s", (enc, user["key_hash"]))
            return user["key_hash"], None
        api_key = new_api_key()
        conn.execute(
            "insert into users (key_hash, email, provider, enc_refresh_token) values (%s, %s, 'google', %s)",
            (hash_key(api_key), email, enc),
        )
    return hash_key(api_key), api_key


@mcp.custom_route("/auth/google", methods=["GET"])
async def google_login(_: Request):
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
        return PlainTextResponse("Google sign-in is not configured.", status_code=503)
    state = secrets.token_urlsafe(16)
    resp = RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": f"openid email {GMAIL_SEND_SCOPE}",
        "access_type": "offline",  # refresh token, so we can send while the user is away
        "prompt": "consent",       # always return a refresh token, even on repeat sign-ins
        "state": state,
    }), status_code=303)
    set_cookie(resp, "ea_state", state, 600)
    return resp


@mcp.custom_route("/auth/google/callback", methods=["GET"])
async def google_callback(request: Request):
    def done(url: str):
        resp = RedirectResponse(url, status_code=303)
        resp.delete_cookie("ea_state")
        return resp

    state = read_cookie(request, "ea_state", 600)
    if not state or state != request.query_params.get("state"):
        return done("/?error=state")
    if "code" not in request.query_params:
        return done("/?error=denied")
    async with httpx.AsyncClient(timeout=20) as client:
        res = await client.post(GOOGLE_TOKEN_URL, data={
            "code": request.query_params["code"],
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        })
    if res.status_code != 200:
        log.warning("google token exchange failed: %s", res.text[:300])
        return done("/?error=exchange")
    tokens = res.json()
    # Google lets users untick individual scopes on the consent screen.
    if GMAIL_SEND_SCOPE not in tokens.get("scope", "").split():
        return done("/?error=scope")
    claims = id_token_claims(tokens["id_token"])
    if not claims.get("email_verified") or not tokens.get("refresh_token"):
        return done("/?error=account")

    key_hash, api_key = await asyncio.to_thread(upsert_google_user, claims["email"].lower(), tokens["refresh_token"])
    resp = done("/")
    set_cookie(resp, "ea_session", key_hash, SESSION_TTL)
    if api_key:
        set_cookie(resp, "ea_new_key", api_key, 600)  # shown once by /api/me, then cleared
    return resp


def dashboard_user(request: Request) -> dict | None:
    key_hash = read_cookie(request, "ea_session", SESSION_TTL)
    if not key_hash:
        return None
    with db() as conn:
        return conn.execute(
            "select key_hash, email, provider from users where key_hash = %s", (key_hash,)
        ).fetchone()


def signed_out():
    return JSONResponse({"error": "Not signed in."}, status_code=401)


@mcp.custom_route("/", methods=["GET"])
async def dashboard(_: Request):
    return HTMLResponse(DASHBOARD_HTML)


@mcp.custom_route("/api/me", methods=["GET"])
async def api_me(request: Request):
    user = await asyncio.to_thread(dashboard_user, request)
    if not user:
        return signed_out()
    new_key = read_cookie(request, "ea_new_key", 600)
    resp = JSONResponse({
        "email": user["email"],
        "mcp_url": f"{PUBLIC_URL}/mcp",
        "new_api_key": new_key if new_key and hash_key(new_key) == user["key_hash"] else None,
    })
    resp.delete_cookie("ea_new_key")
    return resp


@mcp.custom_route("/api/jobs", methods=["GET"])
async def api_jobs(request: Request):
    user = await asyncio.to_thread(dashboard_user, request)
    if not user:
        return signed_out()
    return JSONResponse(await asyncio.to_thread(list_jobs, user["key_hash"]))


@mcp.custom_route("/api/jobs/{job_id}/cancel", methods=["POST"])
async def api_cancel(request: Request):
    # POSTs are CSRF-safe: the session cookie is SameSite=Lax, so cross-site POSTs don't carry it.
    user = await asyncio.to_thread(dashboard_user, request)
    if not user:
        return signed_out()
    try:
        return JSONResponse(await asyncio.to_thread(cancel_job, user["key_hash"], request.path_params["job_id"]))
    except ToolError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@mcp.custom_route("/api/key/rotate", methods=["POST"])
async def api_rotate(request: Request):
    user = await asyncio.to_thread(dashboard_user, request)
    if not user:
        return signed_out()

    def rotate():
        with db() as conn:
            return rotate_key(conn, user["key_hash"])

    api_key = await asyncio.to_thread(rotate)
    resp = JSONResponse({"new_api_key": api_key})
    set_cookie(resp, "ea_session", hash_key(api_key), SESSION_TTL)
    return resp


@mcp.custom_route("/logout", methods=["POST"])
async def logout(_: Request):
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("ea_session")
    return resp


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request):
    # Non-secret config fingerprints, to debug env mismatches without exposing keys.
    fp = lambda s: hashlib.sha256(s.encode()).hexdigest()[:8]
    return JSONResponse({
        "ok": True,
        "send_url": SEND_URL,
        "signing_key_fps": [fp(k) for k in SIGNING_KEYS],
        "google_sign_in": bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET),
        "google_redirect_uri": GOOGLE_REDIRECT_URI,
    })


def init_db() -> None:
    with db() as conn:
        conn.execute(Path(__file__).with_name("schema.sql").read_text())


if __name__ == "__main__":
    init_db()
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
