-- Order state only. Do not add columns for CV text, job text, names, or document bodies.
-- Generated files live on the app server and are deleted after delivery, not in this database.

create table if not exists orders (
  id text primary key,
  wa_id text not null,
  step text not null,
  status text not null,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  paid boolean not null default false,
  handoff boolean not null default false,
  followup_sent boolean not null default false,
  delivery_attempts integer not null default 0,
  pack_id text,
  last_error_code text,
  awaiting_field text
);

create table if not exists processed_messages (
  message_id text primary key,
  order_id text not null,
  received_at timestamptz not null
);

create index if not exists orders_wa_id_idx on orders (wa_id);
create index if not exists orders_status_updated_idx on orders (status, updated_at);
