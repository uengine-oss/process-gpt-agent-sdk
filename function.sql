-- 0) 공용 대기 작업 조회 및 상태 변경 (agent_orch 인자로 필터)
DROP FUNCTION IF EXISTS public.fetch_pending_task(text, text, integer);

CREATE OR REPLACE FUNCTION public.fetch_pending_task(
  p_agent_orch text,
  p_consumer   text,
  p_limit      integer
)
RETURNS TABLE (
  id uuid,
  user_id text,
  proc_inst_id text,
  proc_def_id text,
  activity_id text,
  activity_name text,
  start_date timestamp without time zone,
  end_date timestamp without time zone,
  description text,
  tool text,
  due_date timestamp without time zone,
  tenant_id text,
  reference_ids text[],
  adhoc boolean,
  assignees jsonb,
  duration integer,
  output jsonb,
  retry integer,
  consumer text,
  log text,
  draft jsonb,
  project_id uuid,
  feedback jsonb,
  updated_at timestamp with time zone,
  username text,
  status public.todo_status,
  agent_mode public.agent_mode,
  agent_orch public.agent_orch,
  temp_feedback text,
  draft_status public.draft_status,
  task_type public.draft_status
)
AS $$
BEGIN
  RETURN QUERY
    WITH cte AS (
      SELECT
        t.*,
        t.draft_status AS task_type
      FROM todolist AS t
      WHERE t.status = 'IN_PROGRESS'
        AND (p_agent_orch IS NULL OR p_agent_orch = '' OR t.agent_orch::text = p_agent_orch)
        AND (
          (t.agent_mode IN ('DRAFT','COMPLETE') AND t.draft IS NULL AND t.draft_status IS NULL)
          OR t.draft_status = 'FB_REQUESTED'
        )
      ORDER BY t.start_date
      LIMIT p_limit
      FOR UPDATE SKIP LOCKED
    ),
    upd AS (
      UPDATE todolist AS t
         SET draft_status = 'STARTED',
             consumer     = p_consumer
        FROM cte
       WHERE t.id = cte.id
       RETURNING
         t.id,
         t.user_id,
         t.proc_inst_id,
         t.proc_def_id,
         t.activity_id,
         t.activity_name,
         t.start_date,
         t.end_date,
         t.description,
         t.tool,
         t.due_date,
         t.tenant_id,
         t.reference_ids,
         t.adhoc,
         t.assignees,
         t.duration,
         t.output,
         t.retry,
         t.consumer,
         t.log,
         t.draft,
         t.project_id,
         t.feedback,
         t.updated_at,
         t.username,
         t.status,
         t.agent_mode,
         t.agent_orch,
         t.temp_feedback,
         t.draft_status,
         cte.task_type
    )
    SELECT * FROM upd;
END;
$$ LANGUAGE plpgsql VOLATILE;

DROP FUNCTION IF EXISTS public.fetch_pending_task_dev(text, text, integer, text);

CREATE OR REPLACE FUNCTION public.fetch_pending_task_dev(
  p_agent_orch text,
  p_consumer   text,
  p_limit      integer,
  p_tenant_id  text
)
RETURNS TABLE (
  id uuid,
  user_id text,
  proc_inst_id text,
  proc_def_id text,
  activity_id text,
  activity_name text,
  start_date timestamp without time zone,
  end_date timestamp without time zone,
  description text,
  tool text,
  due_date timestamp without time zone,
  tenant_id text,
  reference_ids text[],
  adhoc boolean,
  assignees jsonb,
  duration integer,
  output jsonb,
  retry integer,
  consumer text,
  log text,
  draft jsonb,
  project_id uuid,
  feedback jsonb,
  updated_at timestamp with time zone,
  username text,
  status public.todo_status,
  agent_mode public.agent_mode,
  agent_orch public.agent_orch,
  temp_feedback text,
  draft_status public.draft_status,
  task_type public.draft_status
)
AS $$
BEGIN
  RETURN QUERY
    WITH cte AS (
      SELECT
        t.*,
        t.draft_status AS task_type
      FROM todolist AS t
      WHERE t.status = 'IN_PROGRESS'
        AND t.tenant_id = p_tenant_id
        AND (p_agent_orch IS NULL OR p_agent_orch = '' OR t.agent_orch::text = p_agent_orch)
        AND (
          (t.agent_mode IN ('DRAFT','COMPLETE') AND t.draft IS NULL AND t.draft_status IS NULL)
          OR t.draft_status = 'FB_REQUESTED'
        )
      ORDER BY t.start_date
      LIMIT p_limit
      FOR UPDATE SKIP LOCKED
    ),
    upd AS (
      UPDATE todolist AS t
         SET draft_status = 'STARTED',
             consumer     = p_consumer
        FROM cte
       WHERE t.id = cte.id
       RETURNING
         t.id,
         t.user_id,
         t.proc_inst_id,
         t.proc_def_id,
         t.activity_id,
         t.activity_name,
         t.start_date,
         t.end_date,
         t.description,
         t.tool,
         t.due_date,
         t.tenant_id,
         t.reference_ids,
         t.adhoc,
         t.assignees,
         t.duration,
         t.output,
         t.retry,
         t.consumer,
         t.log,
         t.draft,
         t.project_id,
         t.feedback,
         t.updated_at,
         t.username,
         t.status,
         t.agent_mode,
         t.agent_orch,
         t.temp_feedback,
         t.draft_status,
         cte.task_type
    )
    SELECT * FROM upd;
END;
$$ LANGUAGE plpgsql VOLATILE;



-- 2) 완료된 데이터(output/feedback) 조회
DROP FUNCTION IF EXISTS public.fetch_done_data(text);

CREATE OR REPLACE FUNCTION public.fetch_done_data(
  p_proc_inst_id text
)
RETURNS TABLE (
  output jsonb
)
LANGUAGE SQL
AS $$
  SELECT t.output
    FROM public.todolist AS t
   WHERE t.proc_inst_id = p_proc_inst_id
     AND t.status = 'DONE'
     AND t.output IS NOT NULL
   ORDER BY t.start_date;
$$;

-- 3) 결과 저장 (중간/최종)
CREATE OR REPLACE FUNCTION public.save_task_result(
  p_todo_id uuid,
  p_payload jsonb,
  p_final   boolean
)
RETURNS void AS $$
DECLARE
  v_mode text;
BEGIN
  SELECT agent_mode
    INTO v_mode
    FROM todolist
   WHERE id = p_todo_id;

  IF p_final THEN
    IF v_mode = 'COMPLETE' THEN
      UPDATE todolist
         SET output       = p_payload,
             status       = 'SUBMITTED',
             draft_status = 'COMPLETED',
             consumer     = NULL
       WHERE id = p_todo_id;
    ELSE
      UPDATE todolist
         SET draft        = p_payload,
             draft_status = 'COMPLETED',
             consumer     = NULL
       WHERE id = p_todo_id;
    END IF;
  ELSE
    UPDATE todolist
       SET draft = p_payload
     WHERE id = p_todo_id;
  END IF;
END;
$$ LANGUAGE plpgsql VOLATILE;

-- 익명(anon) 역할에 실행 권한 부여
GRANT EXECUTE ON FUNCTION public.fetch_pending_task(text, text, integer) TO anon;
GRANT EXECUTE ON FUNCTION public.fetch_pending_task_dev(text, text, integer, text) TO anon;
GRANT EXECUTE ON FUNCTION public.fetch_done_data(text) TO anon;
GRANT EXECUTE ON FUNCTION public.save_task_result(uuid, jsonb, boolean) TO anon;
