create table public.events (
  id text not null,
  job_id text not null,
  todo_id text null,
  proc_inst_id text null,
  crew_type text null,
  data jsonb not null,
  timestamp timestamp with time zone null default now(),
  event_type public.event_type_enum null,
  status public.event_status null,
  constraint events_pkey primary key (id)
) TABLESPACE pg_default;



create table public.todolist (
  id uuid not null,
  user_id text null,
  username text null,
  proc_inst_id text null,
  root_proc_inst_id text null,
  execution_scope text null,
  proc_def_id text null,
  version_tag text null,
  version text null,
  activity_id text null,
  activity_name text null,
  start_date timestamp without time zone null,
  end_date timestamp without time zone null,
  status public.todo_status null,
  description text null,
  tool text null,
  due_date timestamp without time zone null,
  tenant_id text null default tenant_id (),
  reference_ids text[] null,
  adhoc boolean null default false,
  assignees jsonb null,
  duration integer null,
  output jsonb null,
  retry integer null default 0,
  consumer text null,
  log text null,
  project_id uuid null,
  draft jsonb null,
  agent_mode public.agent_mode null,
  feedback jsonb null,
  draft_status public.draft_status null,
  updated_at timestamp with time zone null default now(),
  temp_feedback text null,
  output_url text null,
  rework_count integer null default 0,
  query text null,
  feedback_status text null,
  executor_dept_id text null,
  executor_dept_name text null,
  actual_start_date timestamp with time zone null,
  agent_orch text null,
  feedback_collected_count integer not null default 0,
  gateway_decisions jsonb null,
  -- 작업 점유의 만료 시한. 살아 있는 워커가 renew_task_lease 로 주기적으로 연장한다.
  --
  -- NULL = 만료 개념 없이 점유된 행이며 **회수 대상이 아니다**.
  --   - lease 도입 이전에 이미 STARTED 로 남아 있던 기존 행
  --   - lease 를 모르는 구버전 SDK 워커가 집은 행(p_lease_seconds 를 넘기지 않음)
  -- 연장할 주체가 없는 점유를 회수하면, 그 워커가 아직 살아서 일하는 중일 때
  -- 같은 작업이 두 번 수행된다. NULL 은 예전 동작 그대로 둔다.
  lease_until timestamp with time zone null,
  -- 이 행이 점유된 횟수(최초 + 회수). 무한 재클레임을 막는 상한의 근거다.
  -- 기존 retry 컬럼을 쓰지 않는다 — 그쪽은 작업 자체의 재시도라는 다른 의미다.
  claim_count integer not null default 0,
  constraint todolist_pkey primary key (id),
  constraint todolist_tenant_id_fkey foreign KEY (tenant_id) references tenants (id) on update CASCADE on delete CASCADE
) TABLESPACE pg_default;

create index IF not exists idx_todolist_executor_dept on public.todolist using btree (executor_dept_id) TABLESPACE pg_default;

create index IF not exists idx_todolist_actual_start on public.todolist using btree (actual_start_date) TABLESPACE pg_default;

create index IF not exists idx_todolist_procinst on public.todolist using btree (proc_inst_id) TABLESPACE pg_default;

-- 회수 후보(만료된 점유)를 찾는 조건 그대로의 인덱스.
create index IF not exists idx_todolist_lease_reclaim on public.todolist using btree (status, draft_status, lease_until) TABLESPACE pg_default;

create index IF not exists idx_todolist_inprog on public.todolist using btree (agent_orch, tenant_id, start_date) TABLESPACE pg_default
where
  (status = 'IN_PROGRESS'::todo_status);

create trigger trg_task_execution_insert
after INSERT on todolist for EACH row
execute FUNCTION sync_task_execution_on_insert ();

create trigger trg_task_execution_update
after
update on todolist for EACH row when (old.status is distinct from new.status)
execute FUNCTION sync_task_execution_on_update ();

create trigger trg_set_todolist_executor_dept BEFORE INSERT
or
update OF user_id on todolist for EACH row
execute FUNCTION set_todolist_executor_dept ();

create trigger trg_set_actual_start_date BEFORE
update OF status on todolist for EACH row
execute FUNCTION set_actual_start_date ();

create trigger delete_notification_trigger
after DELETE on todolist for EACH row
execute FUNCTION delete_notification_on_todolist_delete ();

create trigger set_updated_at BEFORE
update on todolist for EACH row
execute FUNCTION update_updated_at_column ();

create trigger todolist_change_trigger
after INSERT
or
update on todolist for EACH row
execute FUNCTION handle_todolist_change ();

create trigger trigger_update_bpm_proc_inst_updated_at
after
update on todolist for EACH row
execute FUNCTION update_bpm_proc_inst_updated_at ();

create trigger update_user_id_trigger
after
update on todolist for EACH row when (old.user_id is distinct from new.user_id)
execute FUNCTION update_notification_user_id ();
-- ── 대화가 파드보다 오래 살게 하는 것들 ──────────────────────────────────────
--
-- 세션당 파드 배포에서 파드는 유휴 TTL 이 지나면 회수되고 디스크도 같이 사라진다.
-- 대화를 다시 열려면 상태와 파일이 파드 밖에 있어야 한다.
--
-- 에이전트별로 테이블을 만들지 않는다. 새 에이전트를 붙일 때마다 자기 테이블과
-- 자기 버킷을 만들어야 한다면 채팅 인프라를 다시 만드는 것과 다르지 않다.
-- agent_type 이 열 하나로 들어가고, 무엇을 담을지는 state(jsonb)로 에이전트가 정한다.

create table if not exists public.agent_sessions (
  agent_type      text not null,
  tenant_id       text not null,
  conversation_id text not null,
  state           jsonb not null default '{}'::jsonb,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  primary key (agent_type, tenant_id, conversation_id)
);

-- PostgREST 의 upsert 는 updated_at 을 갱신하지 않으므로 트리거로 둔다.
create or replace function public.agent_sessions_touch_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists agent_sessions_touch_updated_at on public.agent_sessions;
create trigger agent_sessions_touch_updated_at
  before update on public.agent_sessions
  for each row execute function public.agent_sessions_touch_updated_at();

-- 서비스 키로만 접근한다(브라우저가 직접 읽을 일이 없다).
alter table public.agent_sessions enable row level security;

-- agent-sessions 버킷: 대화의 transcript 와 산출물.
--
-- **반드시 비공개여야 한다.** transcript 는 대화 전문을 담는다. 첨부가 쓰는 공개
-- 버킷(files)에 두면 URL 만 아는 사람이 받아 간다.
insert into storage.buckets (id, name, public)
values ('agent-sessions', 'agent-sessions', false)
on conflict (id) do update set public = false;
