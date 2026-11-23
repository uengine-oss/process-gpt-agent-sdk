"""토큰 시각 검사의 시계 차이 여유 — 인증 서버 시계가 몇 초 빨라도 방금 받은 토큰을 거부하지 않는다."""

import time
import unittest

import jwt

from processgpt_agent_sdk import tenant_auth
from processgpt_agent_sdk.tenant_auth import TenantAuthError

SECRET = "test-jwt-secret-0123456789abcdef-32b"


def token(**claims):
    now = int(time.time())
    body = {"sub": "00000000-0000-0000-0000-00000000000a", "iat": now, "exp": now + 3600, **claims}
    return jwt.encode(body, SECRET, algorithm="HS256")


class ClockLeewayTest(unittest.TestCase):
    def test_token_issued_a_few_seconds_ahead_passes(self):
        # 인증 서버 시계가 이 서버보다 2초 빠를 때 방금 받은 토큰
        claims = tenant_auth._verify_with_secret(token(iat=int(time.time()) + 2), SECRET)
        self.assertEqual(claims["sub"], "00000000-0000-0000-0000-00000000000a")

    def test_token_far_in_the_future_is_still_refused(self):
        with self.assertRaises(TenantAuthError):
            tenant_auth._verify_with_secret(token(iat=int(time.time()) + 600, nbf=int(time.time()) + 600), SECRET)

    def test_expired_token_is_still_refused(self):
        now = int(time.time())
        with self.assertRaises(TenantAuthError):
            tenant_auth._verify_with_secret(token(iat=now - 7200, exp=now - 600), SECRET)


if __name__ == "__main__":
    unittest.main()
