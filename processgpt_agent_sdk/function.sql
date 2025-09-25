-- ======================================================================
-- ProcessGPT DB Functions (Refactor + Index Edition)
-- ======================================================================

-- 0) 공용 대기 작업 조회 및 상태 변경 (agent_orch 인자로 필터, 단건 처리 보장)
DROP FUNCTION IF EXISTS public.fetch_pending_task(text, text, integer, text);

CREATE OR REPLACE FUNCTION public.fetch_pending_task(
  p_agent_orch text,
  p_consumer   text,
  p_limit      integer,
  p_env        text
)
RETURNS SETOF todolist
LANGUAGE plpgsql
VOLATILE
AS $$
BEGIN
  RETURN QUERY
    WITH cte AS (
      SELECT t.id
      FROM todolist AS t
      WHERE t.status = 'IN_PROGRESS'
        -- env 분기
        AND (
              (p_env = 'dev' AND t.tenant_id = 'uengine')
           OR (p_env <> 'dev' AND t.tenant_id <> 'uengine')
        )
        -- agent_orch 필터(옵션)
        AND (p_agent_orch IS NULL OR p_agent_orch = '' OR t.agent_orch::text = p_agent_orch)
        -- 처리 대상 선택 로직
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
       RETURNING t.*
    )
    SELECT * FROM upd;
END;
$$;

GRANT EXECUTE ON FUNCTION public.fetch_pending_task(text, text, integer, text) TO anon;

-- 1) 결과 저장 (중간/최종)
DROP FUNCTION IF EXISTS public.save_task_result(uuid, jsonb, boolean);
CREATE OR REPLACE FUNCTION public.save_task_result(
  p_todo_id uuid,
  p_payload jsonb,
  p_final   boolean
)
RETURNS void AS $$
DECLARE
  v_mode text;
BEGIN
  SELECT agent_mode INTO v_mode FROM todolist WHERE id = p_todo_id;

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

GRANT EXECUTE ON FUNCTION public.save_task_result(uuid, jsonb, boolean) TO anon;

-- 2) [신규] 이벤트 다건 저장: record_events_bulk
DROP FUNCTION IF EXISTS public.record_events_bulk(jsonb);
CREATE OR REPLACE FUNCTION public.record_events_bulk(p_events jsonb)
RETURNS void AS $$
BEGIN
  INSERT INTO events (id, job_id, todo_id, proc_inst_id, crew_type, event_type, data, status)
  SELECT COALESCE((e->>'id')::uuid, gen_random_uuid()),
         e->>'job_id',
         e->>'todo_id',
         e->>'proc_inst_id',
         e->>'crew_type',
         (e->>'event_type')::public.event_type_enum,
         (e->'data')::jsonb,
         NULLIF(e->>'status','')::public.event_status
    FROM jsonb_array_elements(COALESCE(p_events, '[]'::jsonb)) AS e;
END;
$$ LANGUAGE plpgsql VOLATILE;

GRANT EXECUTE ON FUNCTION public.record_events_bulk(jsonb) TO anon;

-- 3) [신규] 컨텍스트 번들 조회: 알림 이메일 / MCP / 폼 / 에이전트(원본 행 전체)
DROP FUNCTION IF EXISTS public.fetch_context_bundle(text, text, text, text);
CREATE OR REPLACE FUNCTION public.fetch_context_bundle(
  p_proc_inst_id text,
  p_tenant_id    text,
  p_tool         text,
  p_user_ids     text
) RETURNS TABLE (
  notify_emails text,
  tenant_mcp    jsonb,
  form_id       text,
  form_fields   jsonb,
  form_html     text,
  agents        jsonb
) AS $$
DECLARE
  v_form_id text;
BEGIN
  -- 알림 이메일(사람만)
  SELECT string_agg(u.email, ',')
    INTO notify_emails
    FROM todolist t
    JOIN users u ON u.id::text = ANY(string_to_array(t.user_id, ','))
   WHERE t.proc_inst_id = p_proc_inst_id
     AND (u.is_agent IS NULL OR u.is_agent = false);

  -- MCP
  SELECT mcp INTO tenant_mcp FROM tenants WHERE id = p_tenant_id;

  -- 폼 (필요 시만)
  v_form_id := CASE
                 WHEN p_tool LIKE 'formHandler:%' THEN substring(p_tool from 12)
                 ELSE p_tool
               END;

  SELECT v_form_id,
         COALESCE(fd.fields_json, jsonb_build_array(jsonb_build_object('key', v_form_id, 'type','default','text',''))),
         fd.html
    INTO form_id, form_fields, form_html
    FROM form_def fd
   WHERE fd.id = v_form_id AND fd.tenant_id = p_tenant_id;

  -- 에이전트 목록 (user_ids 유효하면 그중 agent만, 없으면 전체 agent)
  WITH want_ids AS (
    SELECT unnest(string_to_array(COALESCE(p_user_ids, ''), ',')) AS idtxt
  ),
  valid_ids AS (
    SELECT idtxt FROM want_ids WHERE idtxt ~* '^[0-9a-f-]{8}-[0-9a-f-]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
  )
  SELECT jsonb_agg(to_jsonb(u))
    INTO agents
    FROM users u
   WHERE u.is_agent = true
     AND (
       (SELECT count(*) FROM valid_ids) = 0
       OR u.id::text IN (SELECT idtxt FROM valid_ids)
     );

  RETURN;
END;
$$ LANGUAGE plpgsql VOLATILE;

GRANT EXECUTE ON FUNCTION public.fetch_context_bundle(text, text, text, text) TO anon;

-- ======================================================================
-- 인덱스 (성능에 즉효)
-- ======================================================================

-- 폴링 핫패스: IN_PROGRESS + 정렬열
CREATE INDEX IF NOT EXISTS idx_todolist_inprog
ON todolist (agent_orch, tenant_id, start_date)
WHERE status = 'IN_PROGRESS';

-- 번들 RPC에서 proc_inst_id로 참여자 조회
CREATE INDEX IF NOT EXISTS idx_todolist_procinst
ON todolist (proc_inst_id);

-- 번들 RPC에서 폼 조회 (id, tenant_id 조합)
CREATE INDEX IF NOT EXISTS idx_form_def_id_tenant
ON form_def (id, tenant_id);

-- 번들 RPC에서 에이전트 풀 조회 (부분 인덱스)
CREATE INDEX IF NOT EXISTS idx_users_is_agent_true
ON users (is_agent)
WHERE is_agent = true;
