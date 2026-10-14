-- Applied automatically on startup (idempotent).
create table if not exists users (
  key_hash     text primary key,          -- sha256 of the API key; raw key is never stored
  email        text not null,
  smtp_host    text not null,
  smtp_port    int  not null,
  enc_password text not null,             -- Fernet-encrypted app password
  created_at   timestamptz not null default now()
);

create table if not exists jobs (
  job_id     uuid primary key default gen_random_uuid(),
  key_hash   text not null references users(key_hash) on delete cascade,
  to_addrs   text[] not null,
  subject    text not null,
  body       text not null,
  send_at    timestamptz not null,        -- always UTC
  status     text not null default 'pending',  -- pending | sending | sent | failed | cancelled
  qstash_id  text,
  error      text,
  sent_at    timestamptz,
  created_at timestamptz not null default now()
);

create index if not exists jobs_key_hash_send_at on jobs (key_hash, send_at);
