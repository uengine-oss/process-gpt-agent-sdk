-- ======================================================================
-- ProcessGPT DB Functions (Refactor + Index Edition)
-- ======================================================================

-- 0) 공용 대기 작업 조회 및 상태 변경 (agent_orch 인자로 필터, 단건 처리 보장)
--
-- 점유에는 만료 시한(lease)이 있다. 예전에는 없었다 — 워커가 kill -9 로 죽으면
-- 그 행은 draft_status='STARTED' 로 영구히 남고, 집는 조건(draft_status IS NULL
-- 또는 FB_REQUESTED)에 다시 걸리지 않아 아무도 집지 않았다(고아 STARTED).
-- 레플리카를 늘리거나 KEDA 로 줄이면 그만큼 작업이 조용히 사라진다.
--
--   - 집을 때 lease_until = now() + p_lease_seconds 를 적는다
--   - 워커는 수행 중 renew_task_lease() 로 주기적으로 연장한다
--   - 연장이 끊긴(만료된) STARTED 행은 이 RPC 가 다시 집어간다
--
-- 판정은 전부 이 함수 안에서 일어난다. 워커가 "나 살아 있다" 를 보고하는 방식은
-- 쓸 수 없다 — 죽은 워커는 아무것도 보고하지 못한다. 회수 여부는 DB 의 시계와
-- 행의 상태만으로 결정되고, 집는 순간은 FOR UPDATE SKIP LOCKED 로 직렬화되므로
-- 회수도 둘이 동시에 집을 수 없다.
--
-- p_lease_seconds 가 NULL 이면 lease_until 도 NULL 로 둔다(= 만료 없는 점유).
-- lease 를 모르는 구버전 SDK 워커는 이 인자를 넘기지 않으므로 예전과 똑같이
-- 동작한다. 연장할 주체가 없는 점유를 회수하면 그 워커가 아직 일하는 중일 때
-- 같은 작업이 두 번 수행되므로, NULL 은 회수하지 않는다.
--
-- 회수에는 상한이 있다(p_max_claims). 매번 같은 지점에서 죽는 작업이 영원히
-- 재집행되며 자원을 태우는 것을 막는다. 상한에 닿은 행은 FAILED 로 종결한다 —
-- STARTED 로 남기면 회수 대상에 계속 걸리고, NULL 로 되돌리면 신규 작업으로
-- 다시 집힌다. 둘 다 무한 재집행이다.
DROP FUNCTION IF EXISTS public.fetch_pending_task(text, text, integer, text);
DROP FUNCTION IF EXISTS public.fetch_pending_task(text, text, integer, text, integer);
DROP FUNCTION IF EXISTS public.fetch_pending_task(text, text, integer, text, integer, integer);

CREATE OR REPLACE FUNCTION public.fetch_pending_task(
  p_agent_orch     text,
  p_consumer       text,
  p_limit          integer,
  p_env            text,
  -- 이 두 인자는 기본값이 있다. 구버전 워커의 4-인자 호출이 그대로 동작해야 한다.
  p_lease_seconds  integer DEFAULT NULL,
  p_max_claims     integer DEFAULT 3
)
RETURNS SETOF todolist
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
  v_max_claims integer := GREATEST(coalesce(p_max_claims, 3), 1);
BEGIN
  -- 1) 상한에 닿은 만료 점유를 FAILED 로 종결한다.
  --    폴링마다 돌지만 조건이 idx_todolist_lease_reclaim 그대로라 비용은 없다.
  UPDATE todolist AS t
     SET draft_status = 'FAILED',
         lease_until  = NULL
   WHERE t.status = 'IN_PROGRESS'
     AND t.draft_status = 'STARTED'
     AND t.lease_until IS NOT NULL
     AND t.lease_until < now()
     AND coalesce(t.claim_count, 0) >= v_max_claims
     AND (p_agent_orch IS NULL OR p_agent_orch = '' OR t.agent_orch::text = p_agent_orch);

  -- 2) 집을 수 있는 행 하나를 원자적으로 점유한다.
  RETURN QUERY
    WITH cte AS (
      SELECT t.id
      FROM todolist AS t
      WHERE t.status = 'IN_PROGRESS'
        -- agent_orch 필터(옵션)
        AND (p_agent_orch IS NULL OR p_agent_orch = '' OR t.agent_orch::text = p_agent_orch)
        AND (
          -- 신규 작업
          (t.agent_mode IN ('DRAFT','COMPLETE') AND t.draft IS NULL AND t.draft_status IS NULL)
          -- 사용자 피드백으로 되돌아온 작업
          OR t.draft_status = 'FB_REQUESTED'
          -- 점유가 만료된 작업(= 집은 워커가 더 이상 연장하지 못한다)
          --
          -- draft_status='STARTED' 만 본다. HUMAN_ASKED 처럼 사람 답변을 기다리는
          -- 정상 대기는 여기에 걸리지 않는다. 기다림은 장애가 아니고, 회수해도
          -- 다시 같은 질문 앞에서 멈출 뿐이다.
          OR (
            t.draft_status = 'STARTED'
            AND t.lease_until IS NOT NULL
            AND t.lease_until < now()
            AND coalesce(t.claim_count, 0) < v_max_claims
          )
        )
      ORDER BY t.start_date
      LIMIT p_limit
      FOR UPDATE SKIP LOCKED
    ),
    upd AS (
      UPDATE todolist AS t
         SET draft_status = 'STARTED',
             consumer     = p_consumer,
             lease_until  = CASE
                              WHEN p_lease_seconds IS NULL OR p_lease_seconds <= 0 THEN NULL
                              ELSE now() + make_interval(secs => p_lease_seconds)
                            END,
             -- 회수일 때만 누적한다. 피드백으로 되돌아온 정상 재집행
             -- (FB_REQUESTED)이 상한을 먹으면, 피드백을 몇 번 주고받은 작업이
             -- 멀쩡한데도 FAILED 로 끝난다.
             claim_count  = CASE
                              WHEN t.draft_status = 'STARTED' THEN coalesce(t.claim_count, 0) + 1
                              ELSE 1
                            END
        FROM cte
       WHERE t.id = cte.id
       RETURNING t.*
    )
    SELECT * FROM upd;
END;
$$;

GRANT EXECUTE ON FUNCTION public.fetch_pending_task(text, text, integer, text, integer, integer) TO anon;

-- 0-1) lease 연장(heartbeat). 수행 중인 워커가 주기적으로 호출한다.
--
-- 단순 UPDATE 가 아니라 결과에 이유를 담아 돌려준다. 연장이 실패하는 경우가
-- 두 가지이고 워커가 할 일이 서로 다르기 때문이다.
--   not_owner   : 다른 워커가 이미 회수했다 → 지금 하는 일을 버려야 한다.
--                 그러지 않으면 같은 작업이 둘에서 동시에 수행된다(펜싱).
--   not_started : COMPLETED/HUMAN_ASKED/CANCELLED 등으로 이미 넘어갔다 →
--                 연장할 점유가 없을 뿐이고, 버릴 일은 아니다.
CREATE OR REPLACE FUNCTION public.renew_task_lease(
  p_todo_id       uuid,
  p_consumer      text,
  p_lease_seconds integer
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
  v_status   text;
  v_consumer text;
  v_until    timestamptz;
BEGIN
  IF p_todo_id IS NULL OR coalesce(p_consumer, '') = '' OR coalesce(p_lease_seconds, 0) <= 0 THEN
    RETURN jsonb_build_object('renewed', false, 'reason', 'bad_request');
  END IF;

  SELECT t.draft_status::text, t.consumer
    INTO v_status, v_consumer
    FROM todolist AS t
   WHERE t.id = p_todo_id
     FOR UPDATE;

  IF NOT FOUND THEN
    RETURN jsonb_build_object('renewed', false, 'reason', 'missing');
  END IF;

  IF v_status IS DISTINCT FROM 'STARTED' THEN
    RETURN jsonb_build_object('renewed', false, 'reason', 'not_started',
                              'draft_status', v_status);
  END IF;

  IF v_consumer IS DISTINCT FROM p_consumer THEN
    RETURN jsonb_build_object('renewed', false, 'reason', 'not_owner',
                              'consumer', v_consumer);
  END IF;

  UPDATE todolist
     SET lease_until = now() + make_interval(secs => p_lease_seconds)
   WHERE id = p_todo_id
   RETURNING lease_until INTO v_until;

  RETURN jsonb_build_object('renewed', true, 'reason', 'ok', 'lease_until', v_until);
END;
$$;

GRANT EXECUTE ON FUNCTION public.renew_task_lease(uuid, text, integer) TO anon;

-- 0-2) 점유 해제. 워커가 정상적으로 작업을 떠날 때(= 더 이상 연장하지 않을 때) 쓴다.
-- 남은 lease 를 기다리지 않고 바로 다음 워커가 집을 수 있게 한다.
CREATE OR REPLACE FUNCTION public.release_task_lease(
  p_todo_id  uuid,
  p_consumer text
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
  v_rows integer;
BEGIN
  IF p_todo_id IS NULL OR coalesce(p_consumer, '') = '' THEN
    RETURN false;
  END IF;

  UPDATE todolist AS t
     SET lease_until = NULL
   WHERE t.id = p_todo_id
     AND t.consumer = p_consumer;

  GET DIAGNOSTICS v_rows = ROW_COUNT;
  RETURN v_rows > 0;
END;
$$;

GRANT EXECUTE ON FUNCTION public.release_task_lease(uuid, text) TO anon;

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
             consumer     = NULL,
             -- 점유도 같이 끝낸다. 남겨 두면 만료를 기다리는 동안 lease 가
             -- 끝난 작업을 가리킨다.
             lease_until  = NULL
       WHERE id = p_todo_id;
    ELSE
      UPDATE todolist
         SET draft        = p_payload,
             draft_status = 'COMPLETED',
             consumer     = NULL,
             lease_until  = NULL
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
    SELECT idtxt FROM want_ids WHERE idtxt ~* '^[0-9a-f-]{8}-[0-9a-f-]{4}-[0-9a-f-]{4}-[0-9a-f-]{4}-[0-9a-f]{12}$'
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

-- 회수 후보(만료된 점유)를 찾는 조건 그대로의 인덱스.
-- 모든 워커가 폴링마다 이 조건을 본다.
CREATE INDEX IF NOT EXISTS idx_todolist_lease_reclaim
ON todolist (status, draft_status, lease_until);

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
