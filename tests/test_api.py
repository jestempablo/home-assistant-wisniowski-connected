"""Exercise the cloud client against a local HTTP server without Home Assistant."""

import importlib
from pathlib import Path
import sys
import time
from types import ModuleType
import unittest
from unittest.mock import patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

# Load the standalone client without running the Home Assistant entry point.
PACKAGE = "_wisniowski_api_tests"
package = ModuleType(PACKAGE)
package.__path__ = [
    str(Path(__file__).resolve().parents[1] / "custom_components/wisniowski_connected")
]
sys.modules[PACKAGE] = package
api = importlib.import_module(f"{PACKAGE}.api")


class GraphqlTests(unittest.IsolatedAsyncioTestCase):
    """Cover broker failures, token refresh, and recovery on the next poll."""

    async def asyncSetUp(self):
        self.responses = []
        self.requests = []
        self.token_requests = []
        self.token_updates = []
        app = web.Application()
        app.router.add_post("/graphql/", self.handle_graphql)
        app.router.add_post("/token", self.handle_token)
        self.server = TestServer(app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.session = ClientSession()
        self.addAsyncCleanup(self.session.close)
        token_url = patch.object(api, "KEYCLOAK_TOKEN_URL", str(self.server.make_url("/token")))
        token_url.start()
        self.addCleanup(token_url.stop)
        self.client = api.WisniowskiClient(
            self.session,
            {
                "token": "original-access-token",
                "refresh_token": "original-refresh-token",
                "expires_at": time.time() + 3600,
                # Every operation uses an explicit override, including retries.
                "broker_url": "http://unused.invalid",
            },
            self.token_updates.append,
        )

    async def handle_graphql(self, request):
        self.requests.append(
            (request.headers["Authorization"], request.query["opname"], await request.json())
        )
        if not self.responses:
            return web.json_response({"errors": ["Unexpected extra request"]}, status=500)
        status, body = self.responses.pop(0)
        # The broker can return JSON with an incorrect content type.
        return web.Response(status=status, text=body, content_type="text/plain")

    async def handle_token(self, request):
        self.token_requests.append(dict(await request.post()))
        return web.json_response(
            {
                "access_token": "refreshed-access-token",
                "refresh_token": "rotated-refresh-token",
                "expires_in": 3600,
            }
        )

    async def graphql(self):
        return await self.client.async_graphql(
            "TestOperation",
            "query TestOperation($id: ID!) { gate(id: $id) { id } }",
            {"id": "test-gate"},
            broker_url=str(self.server.make_url("/")),
        )

    async def test_success_accepts_json_with_wrong_content_type(self):
        self.responses = [(200, '{"data": {"gate": {"id": "test-gate"}}}')]
        self.assertEqual(await self.graphql(), {"gate": {"id": "test-gate"}})
        self.assertEqual(self.token_requests, [])

    async def test_non_json_response_preserves_token_and_next_poll_recovers(self):
        for status, body in ((503, "<html>Service unavailable</html>"), (502, "Bad gateway"), (200, "")):
            with self.subTest(status=status):
                self.responses = [(status, body), (200, '{"data": {"recovered": true}}')]
                expires_at = self.client.data["expires_at"]
                with self.assertRaisesRegex(api.WisniowskiApiError, f"non-JSON response HTTP {status}"):
                    await self.graphql()
                self.assertEqual(self.token_requests, [])
                self.assertEqual(self.token_updates, [])
                self.assertEqual(self.client.data["expires_at"], expires_at)
                self.assertEqual(await self.graphql(), {"recovered": True})

    async def test_error_body_snippet_is_bounded(self):
        body = "x" * 250
        self.responses = [(503, body)]
        with self.assertRaises(api.WisniowskiApiError) as error:
            await self.graphql()
        self.assertEqual(str(error.exception), f"non-JSON response HTTP 503: {body[:200]!r}")

    async def test_401_refreshes_before_parsing_and_preserves_request(self):
        for body in ("<html>Unauthorized</html>", '{"errors": ["Unauthorized"]}'):
            with self.subTest(body=body):
                self.responses = [(401, body), (200, '{"data": {"ok": true}}')]
                previous_refreshes = len(self.token_requests)
                self.assertEqual(await self.graphql(), {"ok": True})
                self.assertEqual(len(self.token_requests), previous_refreshes + 1)
                first, retried = self.requests[-2:]
                self.assertEqual(first[1:], retried[1:])
                self.assertEqual(retried[0], "Bearer refreshed-access-token")
                self.assertEqual(self.token_updates[-1]["refresh_token"], "rotated-refresh-token")
        self.assertEqual(self.requests[0][0], "Bearer original-access-token")
        self.assertEqual(self.token_requests[0]["refresh_token"], "original-refresh-token")
        self.assertEqual(self.token_requests[1]["refresh_token"], "rotated-refresh-token")

    async def test_repeated_401_stops_after_one_refresh(self):
        self.responses = [(401, "Unauthorized"), (401, "Still unauthorized")]
        with self.assertRaisesRegex(api.WisniowskiAuthError, "HTTP 401"):
            await self.graphql()
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(self.token_requests), 1)

    async def test_503_after_refresh_is_api_error_without_another_refresh(self):
        self.responses = [(401, "Unauthorized"), (503, "Service unavailable")]
        with self.assertRaisesRegex(api.WisniowskiApiError, "HTTP 503"):
            await self.graphql()
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(self.token_requests), 1)

    async def test_json_http_error_is_api_error(self):
        self.responses = [(403, '{"message": "Forbidden"}')]
        with self.assertRaisesRegex(api.WisniowskiApiError, "Forbidden"):
            await self.graphql()
        self.assertEqual(self.token_requests, [])

    async def test_graphql_error_is_api_error(self):
        self.responses = [(200, '{"errors": [{"message": "Query failed"}]}')]
        with self.assertRaisesRegex(api.WisniowskiApiError, "Query failed"):
            await self.graphql()

    async def test_non_object_json_is_api_error(self):
        for body in ("null", "[]", '"unexpected"', "123"):
            with self.subTest(body=body):
                self.responses = [(200, body)]
                with self.assertRaisesRegex(api.WisniowskiApiError, "Invalid GraphQL response"):
                    await self.graphql()
        self.assertEqual(self.token_requests, [])


if __name__ == "__main__":
    unittest.main()
