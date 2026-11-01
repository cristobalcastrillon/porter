-- Target table for the Porter demo (Supabase / Postgres).
-- Porter writes with the service-role key from the server side, so RLS stays on
-- with no policies: the table is not reachable with the anon key.

create table if not exists public.assets (
  external_id  text primary key,
  species      text,
  status       text not null check (status in ('alive', 'stump', 'dead')),
  condition    text check (condition in ('good', 'fair', 'poor')),
  diameter_cm  numeric(7, 1) check (diameter_cm >= 0),
  latitude     double precision not null check (latitude between 40.47 and 40.92),
  longitude    double precision not null check (longitude between -74.27 and -73.69),
  borough      text not null check (borough in ('Manhattan', 'Bronx', 'Brooklyn', 'Queens', 'Staten Island')),
  inspected_on date,
  migrated_at  timestamptz not null default now()  -- set by Porter, not mapped
);

alter table public.assets enable row level security;

comment on table public.assets is 'Field assets migrated by Porter. external_id is the idempotency key for upserts.';
