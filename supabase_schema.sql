-- Munim schema. Run in Supabase SQL Editor. Keep backend credentials server-side.
begin;
create table if not exists notes (
  id bigint generated always as identity primary key,
  kind text not null check (kind in ('fact','pattern','lesson','hypothesis','user')),
  content text not null,
  tags text[] not null default '{}',
  confidence real not null default 0.5 check (confidence between 0 and 1),
  status text not null default 'active' check (status in ('active','retired')),
  reinforced int not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists notes_active_idx on notes (status, confidence desc);

create table if not exists predictions (
  id bigint generated always as identity primary key,
  claim text not null,
  check_by date not null,
  status text not null default 'open' check (status in ('open','right','wrong','unclear')),
  outcome_note text not null default '',
  created_at timestamptz not null default now()
);

create table if not exists digests (
  id bigint generated always as identity primary key,
  slot text not null check (slot in ('morning','evening')),
  day date not null,
  content text not null,
  snapshot jsonb not null default '{}',
  created_at timestamptz not null default now()
);

-- Older rows were written after sending. Keep them and treat them as delivered.
alter table digests add column if not exists delivery_status text not null default 'sent'
  check (delivery_status in ('sending', 'sent'));
create unique index if not exists digests_slot_day_uidx on digests (slot, day);

create table if not exists chat (
  id bigint generated always as identity primary key,
  role text not null check (role in ('user','assistant')),
  content text not null,
  created_at timestamptz not null default now()
);

create table if not exists processed_updates (
  id bigint generated always as identity primary key,
  update_id bigint not null unique,
  created_at timestamptz not null default now()
);

create table if not exists market_observations (
  id bigint generated always as identity primary key,
  observation jsonb not null default '{}',
  created_at timestamptz not null default now()
);

create table if not exists evidence (
  id bigint generated always as identity primary key,
  evidence jsonb not null default '{}',
  created_at timestamptz not null default now()
);

-- All tables are private. Backend uses service_role; never expose that key to a client.
do $$ declare t text; sequence_name text; begin
  foreach t in array array['notes','predictions','digests','chat','processed_updates','market_observations','evidence'] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all on table public.%I from anon, authenticated', t);
    execute format('grant select, insert, update, delete on table public.%I to service_role', t);
    sequence_name := pg_get_serial_sequence(format('public.%I', t), 'id');
    if sequence_name is not null then
      execute format('revoke all on sequence %s from anon, authenticated', sequence_name);
      execute format('grant usage, select on sequence %s to service_role', sequence_name);
    end if;
  end loop;
end $$;
commit;
