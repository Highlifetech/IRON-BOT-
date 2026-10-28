import os, unittest
from unittest import mock
os.environ.setdefault("LARK_APP_ID", "x"); os.environ.setdefault("LARK_APP_SECRET", "y")
import lark_client

class FakeResp:
    def __init__(self, body, status=200):
        self._b, self.status_code = body, status
    def json(self): return self._b
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(self.status_code)

class TokenRetryTest(unittest.TestCase):
    def setUp(self):
        self.c = lark_client.LarkClient()
        self.tokens = iter(["t1", "t2", "t3"])
        def fake_token():
            if not self.c.token:
                self.c.token = next(self.tokens); self.c.token_expires = 9e12
            return self.c.token
        self.c._get_tenant_token = fake_token

    def test_expired_token_refreshes_and_retries_once(self):
        seen = []
        def fake_get(url, headers=None, **k):
            seen.append(headers["Authorization"])
            return FakeResp({"code": 99991677} if len(seen) == 1 else {"code": 0, "data": {}})
        with mock.patch.object(lark_client.requests, "get", side_effect=fake_get):
            self.assertEqual(self.c._get("/x")["code"], 0)
        self.assertEqual(seen, ["Bearer t1", "Bearer t2"])

    def test_does_not_loop_forever(self):
        with mock.patch.object(lark_client.requests, "post", return_value=FakeResp({"code": 99991663})) as p:
            self.assertEqual(self.c._post("/x", {})["code"], 99991663)
        self.assertEqual(p.call_count, 2)

    def test_normal_error_not_retried(self):
        with mock.patch.object(lark_client.requests, "get", return_value=FakeResp({"code": 1254040})) as g:
            self.assertEqual(self.c._get("/x")["code"], 1254040)
        self.assertEqual(g.call_count, 1)

if __name__ == "__main__":
    unittest.main()
