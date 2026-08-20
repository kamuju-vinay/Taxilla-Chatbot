-- Run this once in the Supabase SQL Editor (Project -> SQL Editor -> New query)
-- Replaces the local reports/index.json + reports/deleted_index.json files.

create table if not exists reports (
    id                  uuid primary key default gen_random_uuid(),
    filename            text not null,
    stored_as           text not null,
    ext                 text not null,
    size_bytes          bigint not null,
    source              text not null,              -- gmail | outlook | upload
    provider_message_id text,
    sender              text,
    subject             text,
    received_at         timestamptz,
    fetched_at          timestamptz not null default now(),
    has_extracted_text  boolean not null default false,
    extracted_text      text                          -- PDF text, replaces the local .txt files
);

create index if not exists reports_fetched_at_idx on reports (fetched_at desc);

create table if not exists deleted_reports (
    key         text primary key,   -- "{source}:{provider_message_id}:{filename}"
    deleted_at  timestamptz not null default now()
);

-- Row Level Security: locked down. The backend talks to Supabase using the
-- service_role / secret key, which bypasses RLS entirely, so these tables
-- stay inaccessible to anyone using the publishable/anon key (e.g. if the
-- frontend ever queried Supabase directly by mistake).
alter table reports enable row level security;
alter table deleted_reports enable row level security;
