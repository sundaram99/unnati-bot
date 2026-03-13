-- Unnati CRM — Supabase Schema
-- Run this in the Supabase SQL Editor (https://supabase.com/dashboard → SQL Editor)

-- ── Users ────────────────────────────────────────────────────────────────────

create table if not exists users (
  id               uuid primary key default gen_random_uuid(),
  telegram_chat_id bigint unique not null,
  name             text not null,
  created_at       timestamptz default now()
);

-- Index for fast lookup by Telegram chat ID (used on every request)
create index if not exists users_telegram_chat_id_idx on users (telegram_chat_id);


-- ── Contacts ─────────────────────────────────────────────────────────────────

create table if not exists contacts (
  id                uuid primary key default gen_random_uuid(),
  user_id           uuid references users(id) on delete cascade,
  name              text not null,
  company           text not null default 'Unknown',
  stage             text not null default 'Lead'
                    check (stage in ('Lead','Evaluating','Proposal Sent','Negotiating','Won','Lost')),
  source            text not null default 'manual',
  added_on          timestamptz default now(),
  last_updated      timestamptz default now(),
  interaction_count integer not null default 1
);

create index if not exists contacts_user_id_idx       on contacts (user_id);
create index if not exists contacts_last_updated_idx  on contacts (last_updated desc);
create index if not exists contacts_name_trgm_idx     on contacts using gin (name gin_trgm_ops);

-- Enable pg_trgm for the name ILIKE search used in /context and /addnote
create extension if not exists pg_trgm;


-- ── Notes ────────────────────────────────────────────────────────────────────

create table if not exists notes (
  id          uuid primary key default gen_random_uuid(),
  contact_id  uuid references contacts(id) on delete cascade,
  user_id     uuid references users(id) on delete cascade,
  note_text   text not null,
  logged_on   timestamptz default now()
);

create index if not exists notes_contact_id_idx on notes (contact_id);
create index if not exists notes_logged_on_idx  on notes (logged_on desc);


-- ── Row Level Security (optional but recommended for production) ──────────────
-- If you use the service role key in your bot you can skip RLS.
-- If you use the anon key, enable RLS and add policies:

-- alter table users    enable row level security;
-- alter table contacts enable row level security;
-- alter table notes    enable row level security;
