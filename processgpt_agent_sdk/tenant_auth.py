"""processgpt_agent_sdk/tenant_auth.py — 요청자의 테넌트 신원 검증.

## 왜 SDK 에 있나

`mount_chat_sse()` 가 만드는 핸들러는 요청 본문의 `tenant_id` 를 그대로
`ChatRequest.tenant_id` 에 넣는다. 그 값은 에이전트 구현에서
스킬 디렉터리 로드·샌드박스 마운트·작업공간 경로까지 그대로 흘러가므로,
값만 바꿔 호출하면 남의 테넌트 자원에 닿을 수 있다.

SDK 가 소유한 라우트라서 각 에이전트 저장소에서는 핸들러에 가드를 달 수 없고,
실제로 deepagents 는 ASGI 미들웨어로 앞단에서 가로채는 우회를 만들어야 했으며
codex 는 아직 아무 검증이 없다. 검증을 프레임워크로 올려 두 저장소가 같은
규칙을 공유하게 한다.

## 검증 경로

요청자의 Supabase JWT(`Authorization: Bearer …`, 하위호환으로 본문 `user_jwt`)를
서명·만료까지 검증해 "이 사용자가 실제로 속한 테넌트 집합" 을 구한다.
요청이 보낸 `tenant_id` 는 그 집합에 있을 때만 통과한다(아니면 403).

토큰 헤더의 `alg` 에 따라 갈린다.
  1. 비대칭(ES256/RS256 …) — Supabase JWKS(`/auth/v1/.well-known/jwks.json`) 로컬 검증.
     최신 Supabase 프로젝트(JWT signing keys)가 여기 해당한다.
  2. HS256 + `SUPABASE_JWT_SECRET` — 레거시 대칭키 프로젝트/커스텀 SSO 토큰 로컬 검증.
  3. 둘 다 불가하면 GoTrue `/auth/v1/user` 에 위임.
검증 결과는 토큰 단위로 짧게 캐시한다.

테넌트 소속은 JWT 클레임(`tenant_id` / `app_metadata.tenant_id`)을 우선 쓰고,
없으면 `users` 테이블에서 조회한다(멀티 테넌트 소속 사용자 대응).

## 의존성

PyJWT 는 선택 의존이다(`process-gpt-agent-sdk[auth]`). import 를 함수 안에서
하므로, 인증을 켜지 않은 서버는 설치하지 않아도 기동에 영향이 없다.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 검증된 토큰/테넌트 소속 캐시 TTL(초). 짧게 잡아 권한 회수가 곧 반영되게 한다.
_TOKEN_CACHE_TTL = 60.0
_TENANTS_CACHE_TTL = 60.0

_token_cache: Dict[str, Tuple[float, dict]] = {}
_tenants_cache: Dict[str, Tuple[float, frozenset]] = {}


class TenantAuthError(Exception):
    """인증/인가 실패. status_code 그대로 응답한다."""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class RequestIdentity:
    user_id: str
    email: str
    tenant_ids: frozenset


# ---------------------------------------------------------------------------
# 요청에서 값 뽑기
# ---------------------------------------------------------------------------

async def _payload(request: Any) -> dict:
    """본문(JSON 또는 form)을 dict 로 반환한다. 파싱 실패 시 빈 dict.

    Starlette 의 `Request.json()/form()` 은 결과를 캐시하므로, 여기서 먼저 읽어도
    핸들러가 다시 읽을 때 같은 값을 그대로 받는다(업로드 파일 포함).
    """
    if request.method in ("GET", "HEAD"):
        return {}
    ctype = (request.headers.get("content-type") or "").lower()
    if ctype.startswith("multipart/form-data") or ctype.startswith("application/x-www-form-urlencoded"):
        try:
            form = await request.form()
        except Exception:
            return {}
        return {k: v for k, v in form.items() if isinstance(v, str)}
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


async def _extract_token(request: Any) -> str:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            return token
    # 하위호환: 채팅 경로가 쓰던 본문 user_jwt 방식도 허용한다.
    body = await _payload(request)
    return str(body.get("user_jwt") or "").strip()


async def _claimed_tenant_id(request: Any) -> str:
    claimed = (request.query_params.get("tenant_id") or "").strip()
    if claimed:
        return claimed
    claimed = (request.headers.get("x-tenant-id") or "").strip()
    if claimed:
        return claimed
    body = await _payload(request)
    return str(body.get("tenant_id") or "").strip()


# ---------------------------------------------------------------------------
# JWT 검증
# ---------------------------------------------------------------------------

_ASYMMETRIC_ALGS = frozenset({
    "RS256", "RS384", "RS512",
    "ES256", "ES384", "ES512",
    "PS256", "PS384", "PS512",
    "EdDSA",
})

_jwk_client = None


def _require_pyjwt():
    try:
        import jwt  # noqa: F401
    except ImportError as e:  # pragma: no cover - 설치 안내 경로
        raise TenantAuthError(
            "테넌트 인증에는 PyJWT 가 필요합니다. "
            "`pip install 'process-gpt-agent-sdk[auth]'` 로 설치하세요.",
            500,
        ) from e
    return jwt


def _get_jwk_client():
    """Supabase JWKS 클라이언트(프로세스 1개, 키셋은 내부 캐시)."""
    global _jwk_client
    if _jwk_client is None:
        from jwt import PyJWKClient

        base = (os.environ.get("SUPABASE_URL") or os.environ.get("SUPABASE_KEY_URL") or "").strip()
        if not base:
            raise TenantAuthError("SUPABASE_URL 이 없어 토큰을 검증할 수 없습니다", 500)
        _jwk_client = PyJWKClient(
            f"{base.rstrip('/')}/auth/v1/.well-known/jwks.json",
            cache_keys=True,
            lifespan=3600,
            timeout=10,
        )
    return _jwk_client


def _verify_with_jwks(token: str, alg: str) -> dict:
    jwt = _require_pyjwt()

    try:
        signing_key = _get_jwk_client().get_signing_key_from_jwt(token)
    except TenantAuthError:
        raise
    except Exception as e:
        raise TenantAuthError(f"서명 키 조회 실패: {e}") from e
    try:
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=[alg],
            options={"verify_aud": False},
        )
    except jwt.ExpiredSignatureError as e:
        raise TenantAuthError("토큰이 만료되었습니다") from e
    except jwt.InvalidTokenError as e:
        raise TenantAuthError(f"유효하지 않은 토큰입니다: {e}") from e


def _verify_with_secret(token: str, secret: str) -> dict:
    jwt = _require_pyjwt()

    try:
        return jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            # Supabase 토큰의 aud 는 'authenticated' 이지만 커스텀 SSO 토큰은 다를 수 있어
            # 서명·만료만 강제한다.
            options={"verify_aud": False},
        )
    except jwt.ExpiredSignatureError as e:
        raise TenantAuthError("토큰이 만료되었습니다") from e
    except jwt.InvalidTokenError as e:
        raise TenantAuthError(f"유효하지 않은 토큰입니다: {e}") from e


def _verify_with_gotrue(token: str) -> dict:
    """대칭키 시크릿이 없을 때 GoTrue 에 검증을 위임한다(서명·만료 모두 서버가 확인)."""
    from .database import get_db_client, initialize_db

    try:
        # 가드는 미들웨어라 채팅 핸들러의 initialize_db() 보다 먼저 돈다.
        # 여기서 직접 챙기지 않으면 첫 요청이 "DB 미초기화" 로 떨어진다(멱등).
        initialize_db()
        resp = get_db_client().auth.get_user(token)
    except Exception as e:
        raise TenantAuthError(f"토큰 검증 실패: {e}") from e
    user = getattr(resp, "user", None)
    if user is None or not getattr(user, "id", None):
        raise TenantAuthError("유효하지 않은 토큰입니다")
    return {
        "sub": str(user.id),
        "email": getattr(user, "email", "") or "",
        "app_metadata": dict(getattr(user, "app_metadata", None) or {}),
        "user_metadata": dict(getattr(user, "user_metadata", None) or {}),
    }


def verify_token(token: str) -> dict:
    """토큰을 검증해 클레임 dict 를 반환한다. 실패 시 TenantAuthError."""
    key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.monotonic()
    hit = _token_cache.get(key)
    if hit and hit[0] > now:
        return hit[1]

    jwt = _require_pyjwt()

    try:
        alg = str(jwt.get_unverified_header(token).get("alg") or "")
    except Exception as e:
        raise TenantAuthError(f"유효하지 않은 토큰입니다: {e}") from e

    secret = os.environ.get("SUPABASE_JWT_SECRET", "").strip()
    if alg in _ASYMMETRIC_ALGS:
        claims = _verify_with_jwks(token, alg)
    elif alg == "HS256" and secret:
        claims = _verify_with_secret(token, secret)
    else:
        claims = _verify_with_gotrue(token)

    # exp 가 캐시 TTL 보다 먼저 끝나면 그 시점까지만 캐시한다.
    ttl = _TOKEN_CACHE_TTL
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        ttl = min(ttl, max(0.0, exp - time.time()))
    if ttl > 0:
        _token_cache[key] = (now + ttl, claims)
    return claims


# ---------------------------------------------------------------------------
# 테넌트 소속 확인
# ---------------------------------------------------------------------------

def _tenants_from_claims(claims: dict) -> set:
    found: set = set()
    sources = [claims]
    for meta_key in ("app_metadata", "user_metadata"):
        meta = claims.get(meta_key)
        if isinstance(meta, dict):
            sources.append(meta)
    for src in sources:
        one = src.get("tenant_id")
        if isinstance(one, str) and one.strip():
            found.add(one.strip())
        many = src.get("tenant_ids")
        if isinstance(many, list):
            found.update(str(t).strip() for t in many if str(t).strip())
    return found


def _tenants_from_db(user_id: str) -> frozenset:
    """users 테이블에서 사용자가 속한 테넌트를 조회한다(멀티 테넌트 소속 대응)."""
    now = time.monotonic()
    hit = _tenants_cache.get(user_id)
    if hit and hit[0] > now:
        return hit[1]

    tenants: set = set()
    try:
        from .database import get_db_client, initialize_db

        # _verify_with_gotrue 와 같은 이유로 여기서도 초기화를 보장한다. 이 조회가
        # 실패하면 아래에서 "소속 없음"(fail-closed)으로 떨어져 정상 사용자가 403 을
        # 받게 되므로, 초기화 누락이 조용한 인가 실패로 번지지 않게 막는다.
        initialize_db()
        resp = get_db_client().table("users").select("tenant_id").eq("id", user_id).execute()
        for row in getattr(resp, "data", None) or []:
            tid = str((row or {}).get("tenant_id") or "").strip()
            if tid:
                tenants.add(tid)
    except Exception:
        # DB 조회 실패는 "소속 없음" 으로 처리한다(fail-closed). 클레임에 tenant_id 가
        # 있으면 그쪽으로 통과하므로 정상 사용자는 영향받지 않는다.
        logger.warning("users 테넌트 조회 실패 user=%s", user_id, exc_info=True)
        return frozenset()

    result = frozenset(tenants)
    _tenants_cache[user_id] = (now + _TENANTS_CACHE_TTL, result)
    return result


async def resolve_identity(request: Any) -> RequestIdentity:
    """요청자의 검증된 신원(사용자 id + 소속 테넌트 집합)을 반환한다."""
    token = await _extract_token(request)
    if not token:
        raise TenantAuthError("인증 토큰이 필요합니다 (Authorization: Bearer …)")

    # 검증은 JWKS/GoTrue 네트워크 호출을 포함할 수 있어(첫 호출) 스레드로 넘긴다 —
    # 같은 프로세스가 SSE 채팅 스트림도 서빙하므로 이벤트 루프를 막으면 안 된다.
    claims = await asyncio.to_thread(verify_token, token)
    user_id = str(claims.get("sub") or "").strip()
    if not user_id:
        raise TenantAuthError("토큰에 사용자 식별자(sub)가 없습니다")

    tenant_ids = _tenants_from_claims(claims)
    if not tenant_ids:
        tenant_ids = set(await asyncio.to_thread(_tenants_from_db, user_id))

    return RequestIdentity(
        user_id=user_id,
        email=str(claims.get("email") or "").strip(),
        tenant_ids=frozenset(tenant_ids),
    )


async def authorize_tenant(request: Any) -> RequestIdentity:
    """신원을 검증하고 요청된 tenant_id 접근 권한을 확인한다.

    통과하면 `request.state.tenant_id` / `request.state.user_id` 를 채우고
    신원을 반환한다. 핸들러는 요청이 보낸 값 대신 이 값을 써야 한다.
    """
    identity = await resolve_identity(request)
    claimed = await _claimed_tenant_id(request)

    if claimed:
        if claimed not in identity.tenant_ids:
            logger.warning(
                "테넌트 접근 거부: user=%s requested=%s allowed=%s path=%s",
                identity.user_id, claimed, sorted(identity.tenant_ids), request.url.path,
            )
            raise TenantAuthError(f"테넌트 '{claimed}' 에 대한 접근 권한이 없습니다", 403)
        resolved = claimed
    elif len(identity.tenant_ids) == 1:
        resolved = next(iter(identity.tenant_ids))
    elif not identity.tenant_ids:
        raise TenantAuthError("사용자에 연결된 테넌트가 없습니다", 403)
    else:
        raise TenantAuthError("tenant_id is required", 400)

    request.state.tenant_id = resolved
    request.state.user_id = identity.user_id
    return identity


def tenant_guard(handler: Callable[..., Awaitable[Any]]):
    """핸들러 진입 전에 테넌트 권한을 확인하는 데코레이터."""
    from starlette.responses import JSONResponse

    @functools.wraps(handler)
    async def wrapper(request: Any):
        try:
            await authorize_tenant(request)
        except TenantAuthError as e:
            return JSONResponse({"error": e.message}, status_code=e.status_code)
        return await handler(request)

    return wrapper


def auth_guard(handler: Callable[..., Awaitable[Any]]):
    """테넌트 스코프가 없는 엔드포인트용 — 인증만 확인한다."""
    from starlette.responses import JSONResponse

    @functools.wraps(handler)
    async def wrapper(request: Any):
        try:
            identity = await resolve_identity(request)
        except TenantAuthError as e:
            return JSONResponse({"error": e.message}, status_code=e.status_code)
        request.state.user_id = identity.user_id
        return await handler(request)

    return wrapper


def request_tenant_id(request: Any) -> str:
    """tenant_guard 가 검증해 둔 tenant_id. 가드 없이 호출되면 예외."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise RuntimeError("tenant_guard 없이 request_tenant_id() 가 호출되었습니다")
    return tenant_id


def request_user_id(request: Any) -> str:
    return getattr(request.state, "user_id", "") or ""


def clear_caches() -> None:
    """테스트용 — 토큰/테넌트/JWKS 캐시를 비운다."""
    global _jwk_client
    _token_cache.clear()
    _tenants_cache.clear()
    _jwk_client = None


# ---------------------------------------------------------------------------
# 채팅 라우트 가드 (ASGI 미들웨어)
# ---------------------------------------------------------------------------
# `mount_chat_sse()` 가 등록하는 핸들러는 프레임워크 내부에서 만들어지므로 호출부가
# 데코레이터를 달 수 없다. 그런데 본문 tenant_id 는
#   ChatRequest.tenant_id → AgentExecutor → 스킬 로드/샌드박스 마운트/작업공간 경로
# 까지 그대로 흘러가므로, 여기서 막지 않으면 다른 API 를 잠근 의미가 없다.
#
# BaseHTTPMiddleware 는 SSE 스트리밍 응답과 궁합이 나쁘므로 순수 ASGI 미들웨어로 만든다.
# 요청 본문만 버퍼링하고 send 는 그대로 통과시켜 스트리밍에 영향을 주지 않는다.

_MAX_GUARDED_BODY = 32 * 1024 * 1024


def _replay_receive(body: bytes, original_receive):
    """버퍼링해 둔 본문을 하위 앱에 다시 흘려보내는 receive 콜러블.

    본문을 다 돌려준 뒤에는 반드시 원래 receive 로 위임해야 한다. 여기서 곧바로
    http.disconnect 를 만들어 돌려주면, Starlette StreamingResponse 가 그것을
    "클라이언트가 끊었다" 로 읽고(listen_for_disconnect) task group 을 취소해
    SSE 응답이 시작하자마자 잘린다 — 채팅이 전송은 되는데 네트워크 오류로 끝나는 증상.
    """
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await original_receive()

    return receive


def _scope_with_body_length(scope: dict, length: int) -> dict:
    """본문을 고쳐 쓴 경우 content-length 를 맞춰 준다(chunked 표기는 제거)."""
    headers = [
        (k, v)
        for k, v in scope.get("headers", [])
        if k.lower() not in (b"content-length", b"transfer-encoding")
    ]
    headers.append((b"content-length", str(length).encode("latin-1")))
    new_scope = dict(scope)
    new_scope["headers"] = headers
    return new_scope


class ChatTenantGuardMiddleware:
    """지정 경로의 요청 본문 tenant_id 를 JWT 로 검증한다.

    본문에 tenant_id 가 없으면 검증된 테넌트를 채워 넣어, 하위 핸들러가 보는 값이
    항상 "요청자가 실제로 속한 테넌트" 가 되게 한다.
    """

    def __init__(self, app, paths: Tuple[str, ...] = ("/chat/stream",)) -> None:
        self.app = app
        self.paths = frozenset(paths)

    async def __call__(self, scope, receive, send):
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        if scope.get("type") != "http" or scope.get("path") not in self.paths:
            await self.app(scope, receive, send)
            return

        body = b""
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if len(body) > _MAX_GUARDED_BODY:
                await JSONResponse({"error": "request body too large"}, status_code=413)(
                    scope, receive, send
                )
                return
            more_body = message.get("more_body", False)

        request = Request(scope, receive=_replay_receive(body, receive))
        try:
            await authorize_tenant(request)
        except TenantAuthError as e:
            await JSONResponse({"error": e.message}, status_code=e.status_code)(
                scope, receive, send
            )
            return

        resolved = request.state.tenant_id
        downstream_scope = scope
        ctype = (request.headers.get("content-type") or "").lower()
        if ctype.startswith("application/json"):
            try:
                data = json.loads(body) if body else None
            except ValueError:
                data = None
            if isinstance(data, dict) and not str(data.get("tenant_id") or "").strip():
                data["tenant_id"] = resolved
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                downstream_scope = _scope_with_body_length(scope, len(body))

        await self.app(downstream_scope, _replay_receive(body, receive), send)
