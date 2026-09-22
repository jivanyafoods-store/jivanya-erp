-- SHREEJU + JIVANYA ERP
-- Supabase/PostgreSQL bootstrap schema.
-- Run this ONCE in Supabase SQL Editor for a fresh ERP database.
-- For long-term versioning, keep this SQL in supabase/migrations and deploy
-- through the Supabase CLI.
--
-- IMPORTANT:
-- 1) This file does not create Supabase Auth users. Create the first admin
--    user in Authentication > Users, then connect its UUID to app_users.
-- 2) Never put service-role keys or OpenAI API keys in this SQL.
-- 3) RLS is enabled on application tables. The FastAPI backend should use a
--    server-side key and enforce firm/role authorization before exposing data.

create extension if not exists pgcrypto;
create extension if not exists vector;

create table if not exists public.firms (
  id text primary key,
  name text not null,
  legal_name text,
  gstin text,
  fssai_no text,
  active boolean not null default true,
  created_at timestamptz not null default now()
);

insert into public.firms(id,name)
values ('FIRM-SHREEJU','Shreeju Foods'),('FIRM-JIVANYA','Jivanya Foods')
on conflict (id) do update set name=excluded.name;

create table if not exists public.app_users (
  id uuid primary key default gen_random_uuid(),
  auth_user_id uuid unique,
  full_name text not null,
  email text,
  active boolean not null default true,
  created_at timestamptz not null default now()
);

create table if not exists public.roles (
  id uuid primary key default gen_random_uuid(),
  code text unique not null,
  name text not null
);

insert into public.roles(code,name) values
('SUPER_ADMIN','Super Admin'),('ADMIN','Admin'),('MANAGER','Manager'),
('FINANCE','Finance'),('WAREHOUSE','Warehouse'),('SALES','Sales'),
('PRODUCTION','Production'),('AI_REVIEWER','AI Reviewer')
on conflict (code) do nothing;

create table if not exists public.user_firms (
  user_id uuid references public.app_users(id) on delete cascade,
  firm_id text references public.firms(id) on delete cascade,
  role_code text references public.roles(code),
  primary key(user_id,firm_id,role_code)
);

create table if not exists public.audit_logs (
  id uuid primary key default gen_random_uuid(),
  firm_id text references public.firms(id),
  actor_user_id uuid,
  action text not null,
  entity_type text not null,
  entity_id text,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists public.master_catalog (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  sku text not null,
  title text not null,
  category text,
  sub_category text,
  cogs_price numeric(14,2) not null default 0,
  other_expenses numeric(14,2) not null default 0,
  min_floor_price numeric(14,2) not null default 0,
  selling_price numeric(14,2),
  mrp numeric(14,2),
  stock_hathras numeric(14,3) not null default 0,
  variants jsonb,
  image_url text,
  short_description text,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(firm_id,sku)
);

create table if not exists public.inventory_stock (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  sku text not null,
  on_hand numeric(14,3) not null default 0,
  reserved numeric(14,3) not null default 0,
  updated_at timestamptz not null default now(),
  unique(firm_id,sku)
);

create table if not exists public.stock_ledger (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  sku text not null,
  qty_delta numeric(14,3) not null,
  reason text not null,
  reference_id text,
  created_at timestamptz not null default now()
);

create table if not exists public.customers (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  name text not null,
  phone text,
  email text,
  city text,
  state text,
  customer_type text not null default 'RETAIL',
  credit_limit numeric(14,2) not null default 0,
  created_at timestamptz not null default now()
);

create table if not exists public.orders (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  order_number text not null,
  channel text not null,
  customer_name text,
  phone text,
  shipping_address jsonb not null default '{}'::jsonb,
  subtotal numeric(14,2) not null default 0,
  tax_amount numeric(14,2) not null default 0,
  total_amount numeric(14,2) not null default 0,
  status text not null default 'CONFIRMED',
  idempotency_key text,
  created_at timestamptz not null default now(),
  unique(firm_id,order_number),
  unique(firm_id,idempotency_key)
);

create table if not exists public.order_items (
  id uuid primary key default gen_random_uuid(),
  order_id uuid not null references public.orders(id) on delete cascade,
  firm_id text not null references public.firms(id),
  sku text not null,
  quantity numeric(14,3) not null,
  unit_price numeric(14,2) not null,
  line_total numeric(14,2) not null,
  created_at timestamptz not null default now()
);

create table if not exists public.tax_rates (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  code text not null,
  rate numeric(7,3) not null default 0,
  active boolean not null default true,
  unique(firm_id,code)
);

create table if not exists public.raw_material_inwards (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  material_code text not null,
  name text not null,
  material_type text not null,
  unit text not null,
  quantity numeric(14,3) not null,
  cost_per_unit numeric(14,2) not null default 0,
  created_at timestamptz not null default now()
);

create table if not exists public.boms (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  finished_sku text not null,
  recipe_name text not null,
  wastage_percent numeric(7,3) not null default 0,
  active boolean not null default true,
  created_at timestamptz not null default now()
);

create table if not exists public.bom_items (
  id uuid primary key default gen_random_uuid(),
  bom_id uuid not null references public.boms(id) on delete cascade,
  firm_id text not null references public.firms(id),
  material_code text not null,
  quantity_required numeric(14,5) not null
);

create table if not exists public.production_runs (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  finished_sku text not null,
  quantity_to_pack numeric(14,3) not null,
  batch_number text not null,
  mfg_date date not null,
  expiry_date date not null,
  status text not null default 'COMPLETED',
  created_at timestamptz not null default now()
);

create table if not exists public.settlement_audits (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  channel text not null,
  order_number text not null,
  settlement_ref text,
  gross_order_value numeric(14,2) not null,
  commission numeric(14,2) not null default 0,
  shipping_fee numeric(14,2) not null default 0,
  fixed_fee numeric(14,2) not null default 0,
  actual_bank_credit numeric(14,2) not null,
  expected_credit numeric(14,2) not null,
  variance numeric(14,2) not null,
  status text not null,
  created_at timestamptz not null default now()
);

create table if not exists public.shipments (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  order_number text,
  courier_name text,
  awb text,
  dead_weight_grams numeric(12,3),
  dimensions jsonb,
  status text not null default 'CREATED',
  created_at timestamptz not null default now()
);

create table if not exists public.rto_claims (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  order_number text,
  awb text,
  reason text,
  amount numeric(14,2),
  status text not null default 'PREPARED',
  evidence jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists public.ai_agents (
  id uuid primary key default gen_random_uuid(),
  agent_key text not null unique,
  agent_name text not null,
  purpose text not null,
  mode text not null default 'FREE_LOCAL',
  enabled boolean not null default true,
  requires_approval boolean not null default false,
  created_at timestamptz not null default now()
);

insert into public.ai_agents (agent_key, agent_name, purpose, mode, requires_approval) values
('SUPERVISOR','Supervisor Agent','Coordinates specialist agents and prioritizes exceptions','FREE_LOCAL',false),
('CFO','CFO Agent','Cash, margin, tax and settlement exception analysis','FREE_LOCAL',true),
('INVENTORY','Inventory Agent','Stockout, expiry, FEFO and replenishment analysis','FREE_LOCAL',false),
('SALES_CRM','Sales & CRM Agent','Sales trends, repeat customers and D2C opportunities','FREE_LOCAL',false),
('PROCUREMENT','Procurement Agent','Purchase and raw-material risk analysis','FREE_LOCAL',true),
('LOGISTICS','Logistics Agent','Shipment, NDR, RTO and weight-variance analysis','FREE_LOCAL',false),
('FRAUD','Fraud & Anomaly Agent','Duplicate, variance and suspicious-pattern detection','FREE_LOCAL',true),
('SEO','SEO & Marketing Agent','SEO drafts and content opportunities from verified facts','FREE_LOCAL',false),
('MARKET','Market Intelligence Agent','Free public-web retrieval and evidence collection','FREE_LOCAL',false),
('COMPLIANCE','Compliance Agent','FSSAI, GST, document expiry and traceability checks','FREE_LOCAL',true),
('RAG','RAG Knowledge Agent','Searches ERP knowledge documents and SOPs','FREE_LOCAL',false),
('CREATIVE','Creative Agent','Creates copy/layout/SVG creative drafts without paid image APIs','FREE_LOCAL',false)
on conflict (agent_key) do update set agent_name=excluded.agent_name, purpose=excluded.purpose, mode=excluded.mode, requires_approval=excluded.requires_approval;

create table if not exists public.ai_runs (
  id uuid primary key default gen_random_uuid(),
  firm_id text references public.firms(id),
  agent_name text not null,
  task text,
  result jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists public.ai_recommendations (
  id uuid primary key default gen_random_uuid(),
  firm_id text references public.firms(id),
  agent_name text not null,
  recommendation text not null,
  risk_level text not null default 'MEDIUM',
  proposed_action jsonb not null default '{}'::jsonb,
  status text not null default 'PENDING',
  reviewed_by text,
  reviewed_at timestamptz,
  created_at timestamptz not null default now()
);

create table if not exists public.knowledge_documents (
  id uuid primary key default gen_random_uuid(),
  firm_id text references public.firms(id),
  title text not null,
  source_type text,
  source_url text,
  content text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists public.knowledge_chunks (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null references public.knowledge_documents(id) on delete cascade,
  firm_id text references public.firms(id),
  chunk_text text not null,
  embedding vector(1536),
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_stock_firm_sku on public.inventory_stock(firm_id,sku);
create index if not exists idx_stock_ledger_firm_sku on public.stock_ledger(firm_id,sku,created_at desc);
create index if not exists idx_orders_firm_created on public.orders(firm_id,created_at desc);
create index if not exists idx_order_items_order on public.order_items(order_id);
create index if not exists idx_audit_firm_created on public.audit_logs(firm_id,created_at desc);
create index if not exists idx_ai_runs_firm_created on public.ai_runs(firm_id,created_at desc);
create index if not exists idx_settlement_firm_created on public.settlement_audits(firm_id,created_at desc);
create index if not exists idx_knowledge_chunks_firm on public.knowledge_chunks(firm_id);

-- Atomic stock decrement helper. Use this RPC from high-concurrency checkout
-- implementations rather than read-then-write application logic.
create or replace function public.decrement_stock(
  p_firm_id text,
  p_sku text,
  p_qty numeric
)
returns numeric
language plpgsql
security definer
as $$
declare
  new_stock numeric;
begin
  if p_qty <= 0 then
    raise exception 'quantity must be positive';
  end if;

  update public.inventory_stock
  set on_hand = on_hand - p_qty,
      updated_at = now()
  where firm_id = p_firm_id
    and sku = p_sku
    and on_hand >= p_qty
  returning on_hand into new_stock;

  if new_stock is null then
    raise exception 'insufficient stock';
  end if;

  return new_stock;
end;
$$;

-- RLS: backend service access is intentionally kept separate from direct
-- browser access. If you later query these tables directly from the browser,
-- replace these deny-by-default policies with user/firm membership policies.
do $$
declare
  t text;
begin
  foreach t in array array[
    'firms','app_users','roles','user_firms','audit_logs','master_catalog',
    'inventory_stock','stock_ledger','customers','orders','order_items',
    'tax_rates','raw_material_inwards','boms','bom_items','production_runs',
    'settlement_audits','shipments','rto_claims','ai_runs',
    'ai_recommendations','knowledge_documents','knowledge_chunks'
  ]
  loop
    execute format('alter table public.%I enable row level security', t);
  end loop;
end $$;

-- No public/anon policies are created here, so direct browser access is
-- denied until explicit policies are designed around your Supabase Auth model.

-- ---------------------------------------------------------------------------
-- Extended production tables used by the final dashboard/backend
-- ---------------------------------------------------------------------------

create table if not exists public.expenses (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  account text not null,
  amount numeric(14,2) not null check (amount >= 0),
  description text,
  expense_date date not null default current_date,
  created_at timestamptz not null default now()
);

create table if not exists public.bank_transactions (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  txn_date date not null,
  utr text,
  amount numeric(14,2) not null,
  description text,
  matched boolean not null default false,
  matched_reference text,
  created_at timestamptz not null default now()
);

create table if not exists public.chart_of_accounts (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  account_code text not null,
  account_name text not null,
  account_type text not null,
  parent_code text,
  active boolean not null default true,
  unique(firm_id, account_code)
);

create table if not exists public.journal_entries (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  entry_date date not null default current_date,
  reference_type text not null,
  reference_id text,
  description text,
  reversed_entry_id uuid,
  created_at timestamptz not null default now()
);

create table if not exists public.journal_lines (
  id uuid primary key default gen_random_uuid(),
  journal_id uuid not null references public.journal_entries(id) on delete cascade,
  firm_id text not null references public.firms(id),
  account_code text not null,
  debit numeric(14,2) not null default 0 check (debit >= 0),
  credit numeric(14,2) not null default 0 check (credit >= 0),
  description text
);

create table if not exists public.buy_box_observations (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  sku text not null,
  competitor text not null,
  competitor_price numeric(14,2) not null,
  our_price numeric(14,2) not null default 0,
  observed_at timestamptz not null default now()
);

create table if not exists public.weight_claims (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  awb text not null,
  expected_weight numeric(14,3) not null default 0,
  charged_weight numeric(14,3) not null default 0,
  claim_amount numeric(14,2) not null default 0,
  status text not null default 'PREPARED',
  created_at timestamptz not null default now()
);

create table if not exists public.ai_creative_jobs (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  brand text not null,
  product text,
  visual_type text not null,
  prompt text not null,
  output_url text,
  status text not null default 'QUEUED',
  created_at timestamptz not null default now(),
  completed_at timestamptz
);

create table if not exists public.compliance_items (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  type text not null,
  reference_no text not null,
  issued_on date,
  expires_on date,
  owner text,
  notes text,
  created_at timestamptz not null default now()
);

create table if not exists public.production_orders (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  order_no text not null default ('MO-' || to_char(now(),'YYYYMMDDHH24MISSMS')),
  sku text not null,
  planned_quantity numeric(14,3) not null,
  batch_code text,
  status text not null default 'PLANNED',
  created_at timestamptz not null default now()
);

create table if not exists public.pos_sessions (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  session_no text not null,
  opened_by text,
  opening_cash numeric(14,2) not null default 0,
  closing_cash numeric(14,2),
  status text not null default 'OPEN',
  opened_at timestamptz not null default now(),
  closed_at timestamptz
);

create table if not exists public.pos_transactions (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  session_id uuid references public.pos_sessions(id),
  order_id uuid references public.orders(id),
  invoice_no text,
  payment_mode text not null,
  total numeric(14,2) not null default 0,
  created_at timestamptz not null default now()
);

create table if not exists public.notifications (
  id uuid primary key default gen_random_uuid(),
  firm_id text references public.firms(id),
  type text not null,
  title text not null,
  message text not null,
  read_at timestamptz,
  created_at timestamptz not null default now()
);

create table if not exists public.events (
  id uuid primary key default gen_random_uuid(),
  firm_id text references public.firms(id),
  event_type text not null,
  payload jsonb not null default '{}'::jsonb,
  status text not null default 'PENDING',
  created_at timestamptz not null default now(),
  processed_at timestamptz
);

create table if not exists public.background_jobs (
  id uuid primary key default gen_random_uuid(),
  firm_id text references public.firms(id),
  job_type text not null,
  payload jsonb not null default '{}'::jsonb,
  status text not null default 'QUEUED',
  attempts integer not null default 0,
  last_error text,
  created_at timestamptz not null default now(),
  finished_at timestamptz
);

create table if not exists public.traceability_records (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  sku text not null,
  batch_no text not null,
  source_type text,
  source_reference text,
  mfg_date date,
  expiry_date date,
  quantity numeric(14,3) not null default 0,
  created_at timestamptz not null default now()
);

-- Settlement fields used by the reconciliation screen. Safe on an existing database.
alter table public.settlement_audits add column if not exists expected_payout numeric(14,2) default 0;
alter table public.settlement_audits add column if not exists variance numeric(14,2) default 0;
alter table public.settlement_audits add column if not exists reconciled boolean default false;

-- Seed basic accounting heads for both firms. The backend can post balanced journals immediately.
insert into public.chart_of_accounts(firm_id,account_code,account_name,account_type) values
('FIRM-SHREEJU','SALES','Sales Revenue','INCOME'),
('FIRM-SHREEJU','COGS','Cost of Goods Sold','EXPENSE'),
('FIRM-SHREEJU','BANK_CASH','Bank / Cash','ASSET'),
('FIRM-SHREEJU','GST_OUTPUT','Output GST','LIABILITY'),
('FIRM-JIVANYA','SALES','Sales Revenue','INCOME'),
('FIRM-JIVANYA','COGS','Cost of Goods Sold','EXPENSE'),
('FIRM-JIVANYA','BANK_CASH','Bank / Cash','ASSET'),
('FIRM-JIVANYA','GST_OUTPUT','Output GST','LIABILITY')
on conflict(firm_id,account_code) do nothing;

create index if not exists idx_expenses_firm_date on public.expenses(firm_id,expense_date desc);
create index if not exists idx_bank_txn_firm_date on public.bank_transactions(firm_id,txn_date desc);
create index if not exists idx_journal_firm_date on public.journal_entries(firm_id,entry_date desc);
create index if not exists idx_journal_lines_journal on public.journal_lines(journal_id);
create index if not exists idx_buybox_firm_sku on public.buy_box_observations(firm_id,sku,observed_at desc);
create index if not exists idx_compliance_firm_expiry on public.compliance_items(firm_id,expires_on);
create index if not exists idx_jobs_status on public.background_jobs(status,created_at);
create index if not exists idx_events_status on public.events(status,created_at);
create index if not exists idx_traceability_firm_batch on public.traceability_records(firm_id,batch_no);

-- Extend deny-by-default RLS to the new tables.
do $$
declare t text;
begin
  foreach t in array array[
    'expenses','bank_transactions','chart_of_accounts','journal_entries','journal_lines',
    'buy_box_observations','weight_claims','ai_creative_jobs','compliance_items',
    'production_orders','pos_sessions','pos_transactions','notifications','events',
    'background_jobs','traceability_records'
  ] loop
    execute format('alter table public.%I enable row level security', t);
  end loop;
end $$;

-- Helper for onboarding a Supabase Auth user into the ERP without storing passwords.
create or replace function public.provision_erp_user(
  p_auth_user_id uuid,
  p_full_name text,
  p_email text,
  p_firm_id text,
  p_role_code text default 'ADMIN'
)
returns uuid
language plpgsql
security definer
as $$
declare v_user_id uuid;
begin
  insert into public.app_users(auth_user_id,full_name,email)
  values(p_auth_user_id,p_full_name,p_email)
  on conflict(auth_user_id) do update set full_name=excluded.full_name,email=excluded.email
  returning id into v_user_id;
  insert into public.user_firms(user_id,firm_id,role_code)
  values(v_user_id,p_firm_id,p_role_code)
  on conflict do nothing;
  return v_user_id;
end;
$$;

create table if not exists public.manifest_imports (
  id uuid primary key default gen_random_uuid(),
  firm_id text not null references public.firms(id),
  source_filename text not null,
  order_number text not null,
  raw_line text,
  status text not null default 'REVIEW',
  created_at timestamptz not null default now()
);

create table if not exists public.label_ocr_results (
  id uuid primary key default gen_random_uuid(),
  filename text not null,
  awb text,
  phone text,
  raw_text text,
  status text not null default 'REVIEW',
  created_at timestamptz not null default now()
);

alter table public.manifest_imports enable row level security;
alter table public.label_ocr_results enable row level security;
create index if not exists idx_manifest_imports_firm_created on public.manifest_imports(firm_id,created_at desc);
create index if not exists idx_ocr_results_created on public.label_ocr_results(created_at desc);
