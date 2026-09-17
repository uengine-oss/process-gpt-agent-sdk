"""기동 로그가 자격증명을 흘리지 않는가.

`initialize_db` 는 어느 Supabase 에 어떤 키로 붙었는지를 로그로 남긴다. 그 로그에
키 값이 그대로 들어가면, 파드 로그를 볼 수 있는 사람 모두가 service_role 키를
읽는다. 세션당 파드 배포에서는 대화마다 파드가 뜨므로 그 줄이 찍히는 횟수도 그만큼
늘어난다.

키 대신 "어느 환경변수에서 왔는가" 를 남긴다. 그것만으로도 "service_role 로 떠야
하는데 anon 으로 떴다" 는 판단은 되고, 자격증명은 넘어가지 않는다.
"""

import logging
import unittest
from unittest.mock import patch

from processgpt_agent_sdk import database


SERVICE_ROLE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.service-role-secret.sig"
ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.anon-secret.sig"


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class InitializeDbLoggingTest(unittest.TestCase):
    def setUp(self):
        database._supabase_client = None
        self.handler = _CaptureHandler()
        self.logger = logging.getLogger(database.__name__)
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.INFO)
        self.addCleanup(self.logger.removeHandler, self.handler)
        self.addCleanup(setattr, database, "_supabase_client", None)

    def _run(self, env):
        with patch.dict("os.environ", env, clear=True), \
             patch.object(database, "create_client", return_value=object()), \
             patch.object(database, "load_dotenv", lambda *a, **k: None):
            database.initialize_db()
        return "\n".join(self.handler.lines)

    def test_service_role_key_is_never_logged(self):
        logged = self._run({
            "ENV": "production",
            "SUPABASE_URL": "https://example.supabase.co",
            "SERVICE_ROLE_KEY": SERVICE_ROLE_KEY,
        })
        self.assertNotIn(SERVICE_ROLE_KEY, logged)
        # 앞부분만 흘려도 복원 시도의 실마리가 된다. 조각도 남기지 않는다.
        self.assertNotIn(SERVICE_ROLE_KEY[:20], logged)
        self.assertNotIn("service-role-secret", logged)

    def test_anon_key_is_never_logged(self):
        logged = self._run({
            "ENV": "production",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_ANON_KEY": ANON_KEY,
        })
        self.assertNotIn(ANON_KEY, logged)
        self.assertNotIn("anon-secret", logged)

    def test_logs_which_env_var_supplied_the_key(self):
        """어느 키로 떴는지는 알 수 있어야 한다 — 그게 이 로그의 쓸모다."""
        logged = self._run({
            "ENV": "production",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_ANON_KEY": ANON_KEY,
        })
        self.assertIn("SUPABASE_ANON_KEY", logged)
        self.assertIn("https://example.supabase.co", logged)

    def test_service_role_wins_over_anon_and_is_named(self):
        logged = self._run({
            "ENV": "production",
            "SUPABASE_URL": "https://example.supabase.co",
            "SERVICE_ROLE_KEY": SERVICE_ROLE_KEY,
            "SUPABASE_ANON_KEY": ANON_KEY,
        })
        self.assertIn("SERVICE_ROLE_KEY", logged)
        self.assertNotIn(SERVICE_ROLE_KEY, logged)

    def test_missing_key_is_reported_without_crashing_the_log(self):
        with self.assertRaises(RuntimeError):
            self._run({"ENV": "production", "SUPABASE_URL": "https://example.supabase.co"})
        logged = "\n".join(self.handler.lines)
        self.assertIn("(없음)", logged)


if __name__ == "__main__":
    unittest.main()
