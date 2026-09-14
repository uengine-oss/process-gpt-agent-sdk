"""테넌트 인증 — 요청이 보낸 tenant_id 를 JWT 로 검증한다."""

import json
import unittest

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from processgpt_agent_sdk import tenant_auth
from processgpt_agent_sdk.tenant_auth import (
    ChatTenantGuardMiddleware,
    TenantAuthError,
    authorize_tenant,
    clear_caches,
    tenant_guard,
)


def make_request(body: dict, *, headers=None, path="/chat/stream") -> Request:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    raw_headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode()),
    ]
    for k, v in (headers or {}).items():
        raw_headers.append((k.lower().encode(), v.encode()))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 123),
        "headers": raw_headers,
    }

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, receive=receive)


class _AuthTestBase(unittest.IsolatedAsyncioTestCase):
    """토큰 문자열을 그대로 클레임으로 읽는 가짜 검증기를 꽂는다.

    실제 서명 검증(JWKS/HS256/GoTrue)은 Supabase 에 붙어야 하므로, 여기서는
    "검증된 클레임이 주어졌을 때의 인가 판단" 만 본다.
    """

    CLAIMS = {}

    async def asyncSetUp(self):
        clear_caches()
        self._orig_verify = tenant_auth.verify_token
        self._orig_db = tenant_auth._tenants_from_db

        def fake_verify(token: str) -> dict:
            if token not in self.CLAIMS:
                raise TenantAuthError("유효하지 않은 토큰입니다")
            return self.CLAIMS[token]

        tenant_auth.verify_token = fake_verify
        tenant_auth._tenants_from_db = lambda user_id: frozenset()

    async def asyncTearDown(self):
        tenant_auth.verify_token = self._orig_verify
        tenant_auth._tenants_from_db = self._orig_db
        clear_caches()


class TestAuthorizeTenant(_AuthTestBase):
    CLAIMS = {
        "acme-token": {"sub": "u1", "email": "a@acme.io", "tenant_id": "acme"},
        "multi-token": {"sub": "u2", "app_metadata": {"tenant_ids": ["acme", "globex"]}},
        "no-tenant-token": {"sub": "u3"},
    }

    async def test_소속_테넌트를_요청하면_통과한다(self):
        request = make_request({"tenant_id": "acme"}, headers={"authorization": "Bearer acme-token"})
        identity = await authorize_tenant(request)
        self.assertEqual(identity.user_id, "u1")
        self.assertEqual(request.state.tenant_id, "acme")

    async def test_남의_테넌트를_요청하면_403(self):
        request = make_request(
            {"tenant_id": "globex"}, headers={"authorization": "Bearer acme-token"}
        )
        with self.assertRaises(TenantAuthError) as cm:
            await authorize_tenant(request)
        self.assertEqual(cm.exception.status_code, 403)

    async def test_토큰이_없으면_401(self):
        with self.assertRaises(TenantAuthError) as cm:
            await authorize_tenant(make_request({"tenant_id": "acme"}))
        self.assertEqual(cm.exception.status_code, 401)

    async def test_본문_user_jwt_도_받는다(self):
        """하위호환 — 기존 프론트는 Authorization 헤더 없이 본문으로 보낸다."""
        request = make_request({"tenant_id": "acme", "user_jwt": "acme-token"})
        await authorize_tenant(request)
        self.assertEqual(request.state.tenant_id, "acme")

    async def test_소속이_하나면_요청에_없어도_채운다(self):
        request = make_request({}, headers={"authorization": "Bearer acme-token"})
        await authorize_tenant(request)
        self.assertEqual(request.state.tenant_id, "acme")

    async def test_소속이_여럿인데_지정이_없으면_400(self):
        request = make_request({}, headers={"authorization": "Bearer multi-token"})
        with self.assertRaises(TenantAuthError) as cm:
            await authorize_tenant(request)
        self.assertEqual(cm.exception.status_code, 400)

    async def test_app_metadata_의_소속도_읽는다(self):
        request = make_request(
            {"tenant_id": "globex"}, headers={"authorization": "Bearer multi-token"}
        )
        await authorize_tenant(request)
        self.assertEqual(request.state.tenant_id, "globex")

    async def test_소속_테넌트가_없으면_403(self):
        request = make_request({}, headers={"authorization": "Bearer no-tenant-token"})
        with self.assertRaises(TenantAuthError) as cm:
            await authorize_tenant(request)
        self.assertEqual(cm.exception.status_code, 403)

    async def test_x_tenant_id_헤더도_인정한다(self):
        request = make_request(
            {}, headers={"authorization": "Bearer acme-token", "x-tenant-id": "acme"}
        )
        await authorize_tenant(request)
        self.assertEqual(request.state.tenant_id, "acme")

    async def test_가드는_오류를_응답으로_바꾼다(self):
        @tenant_guard
        async def handler(request):  # pragma: no cover - 도달하지 않아야 한다
            return JSONResponse({"reached": True})

        resp = await handler(make_request({"tenant_id": "globex"},
                                          headers={"authorization": "Bearer acme-token"}))
        self.assertEqual(resp.status_code, 403)


class TestChatTenantGuardMiddleware(unittest.TestCase):
    """스트림 라우트는 프레임워크가 소유해 데코레이터를 못 달므로 미들웨어로 막는다."""

    CLAIMS = {
        "acme-token": {"sub": "u1", "tenant_id": "acme"},
    }

    def setUp(self):
        clear_caches()
        self._orig_verify = tenant_auth.verify_token
        self._orig_db = tenant_auth._tenants_from_db

        def fake_verify(token: str) -> dict:
            if token not in self.CLAIMS:
                raise TenantAuthError("유효하지 않은 토큰입니다")
            return self.CLAIMS[token]

        tenant_auth.verify_token = fake_verify
        tenant_auth._tenants_from_db = lambda user_id: frozenset()

        async def echo(request: Request):
            body = await request.json()
            return JSONResponse({"seen_tenant_id": body.get("tenant_id")})

        app = Starlette(routes=[
            Route("/chat/stream", echo, methods=["POST"]),
            Route("/open", echo, methods=["POST"]),
        ])
        app.add_middleware(ChatTenantGuardMiddleware, paths=("/chat/stream",))
        self.client = TestClient(app)

    def tearDown(self):
        tenant_auth.verify_token = self._orig_verify
        tenant_auth._tenants_from_db = self._orig_db
        clear_caches()

    def test_토큰이_없으면_401(self):
        r = self.client.post("/chat/stream", json={"tenant_id": "acme"})
        self.assertEqual(r.status_code, 401)

    def test_남의_테넌트는_403(self):
        r = self.client.post(
            "/chat/stream",
            json={"tenant_id": "globex"},
            headers={"authorization": "Bearer acme-token"},
        )
        self.assertEqual(r.status_code, 403)

    def test_통과한_요청은_본문이_그대로_핸들러에_간다(self):
        r = self.client.post(
            "/chat/stream",
            json={"tenant_id": "acme", "message": "안녕"},
            headers={"authorization": "Bearer acme-token"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"seen_tenant_id": "acme"})

    def test_본문에_tenant_id_가_없으면_검증된_값을_채워_넣는다(self):
        r = self.client.post(
            "/chat/stream",
            json={"message": "안녕"},
            headers={"authorization": "Bearer acme-token"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"seen_tenant_id": "acme"})

    def test_지정하지_않은_경로는_건드리지_않는다(self):
        r = self.client.post("/open", json={"tenant_id": "globex"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"seen_tenant_id": "globex"})


if __name__ == "__main__":
    unittest.main()
