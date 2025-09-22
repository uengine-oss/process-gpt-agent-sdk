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
  proc_inst_id text null,
  proc_def_id text null,
  activity_id text null,
  activity_name text null,
  start_date timestamp without time zone null,
  end_date timestamp without time zone null,
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
  feedback jsonb null,
  updated_at timestamp with time zone null default now(),
  username text null,
  status public.todo_status null,
  agent_mode public.agent_mode null,
  temp_feedback text null,
  agent_orch public.agent_orch null,
  draft_status public.draft_status null,
  root_proc_inst_id text null,
  execution_scope text null,
  output_url text null,
  rework_count integer null default 0,
  constraint todolist_pkey primary key (id),
  constraint todolist_tenant_id_fkey foreign KEY (tenant_id) references tenants (id) on update CASCADE on delete CASCADE
) TABLESPACE pg_default;

create trigger set_updated_at BEFORE
update on todolist for EACH row
execute FUNCTION update_updated_at_column ();

create trigger update_user_id_trigger
after
update on todolist for EACH row when (old.user_id is distinct from new.user_id)
execute FUNCTION update_notification_user_id ();

create trigger delete_notification_trigger
after DELETE on todolist for EACH row
execute FUNCTION delete_notification_on_todolist_delete ();

create trigger trigger_update_bpm_proc_inst_updated_at
after
update on todolist for EACH row
execute FUNCTION update_bpm_proc_inst_updated_at ();