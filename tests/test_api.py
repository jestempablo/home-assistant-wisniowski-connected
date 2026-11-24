"""Exercise the cloud client against a local HTTP server without Home Assistant."""

import importlib
from pathlib import Path
import sys
import time
from types import ModuleType
import unittest
from unittest.mock import patch

from aiohttp import ClientConnectionError, ClientSession, web
from aiohttp.test_utils import TestServer

# Load the standalone client without running the Home Assistant entry point.
PACKAGE = "_wisniowski_api_tests"
package = ModuleType(PACKAGE)
package.__path__ = [
    str(Path(__file__).resolve().parents[1] / "custom_components/wisniowski_connected")
]
sys.modules[PACKAGE] = package
api = importlib.import_module(f"{PACKAGE}.api")


class CloudClientTests(unittest.IsolatedAsyncioTestCase):
    """Cover broker failures, token refresh, and recovery on the next poll."""

    async def asyncSetUp(self):
        self.responses = []
        self.requests = []
        self.token_requests = []
        self.token_updates = []
        self.token_error = None
        self.token_lifetime = 3600
        app = web.Application()
        app.router.add_post("/graphql/", self.handle_graphql)
        app.router.add_post("/token", self.handle_token)
        app.router.add_post("/gate/setDirection", self.handle_gate_command)
        app.router.add_get("/userinfo", self.handle_userinfo)
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
        if self.token_error is not None:
            status, body = self.token_error
            return web.Response(status=status, text=body)
        return web.json_response(
            {
                "access_token": "refreshed-access-token",
                "refresh_token": "rotated-refresh-token",
                "expires_in": self.token_lifetime,
            }
        )

    async def handle_gate_command(self, request):
        self.requests.append(
            (request.headers["Authorization"], request.headers["installationId"], await request.json())
        )
        if not self.responses:
            return web.json_response({"error": "Unexpected extra command"}, status=500)
        status, body = self.responses.pop(0)
        return web.Response(status=status, text=body)

    async def handle_userinfo(self, request):
        status, body = self.responses.pop(0)
        return web.Response(status=status, text=body)

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

    async def test_token_transport_errors_preserve_refresh_token_and_can_retry(self):
        client = api.WisniowskiClient(
            self.session, {**self.client.data, "expires_at": 0}, self.token_updates.append
        )
        for error in (ClientConnectionError("disconnected"), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                with patch.object(self.session, "post", side_effect=error):
                    with self.assertRaises(api.WisniowskiApiError) as raised:
                        await client.async_access_token()
                self.assertIs(raised.exception.__cause__, error)
                self.assertEqual(client.data["refresh_token"], "original-refresh-token")
                self.assertEqual(self.token_updates, [])
        self.assertEqual(await client.async_access_token(), "refreshed-access-token")
        self.assertEqual(len(self.token_requests), 1)
        self.assertEqual(self.token_updates[0]["refresh_token"], "rotated-refresh-token")

    async def test_token_endpoint_errors_distinguish_rejection_from_outage(self):
        for status, body, expected, message in (
            (400, '{"error_description": "Token has expired"}', api.WisniowskiAuthError, "Token has expired"),
            (400, '{"error": "temporarily_unavailable"}', api.WisniowskiApiError, "temporarily_unavailable"),
            (400, '{"error": "server_error", "error_description": "Try later"}', api.WisniowskiApiError, "Try later"),
            (401, '{"error": "invalid_client"}', api.WisniowskiAuthError, "invalid_client"),
            (401, "<html>Unauthorized</html>", api.WisniowskiAuthError, "401"),
            (429, '{"error": "Too many requests"}', api.WisniowskiApiError, "429"),
            (503, "<html>Unavailable</html>", api.WisniowskiApiError, "503"),
            (200, "{}", api.WisniowskiApiError, "access token"),
        ):
            with self.subTest(status=status, body=body):
                self.token_error = status, body
                with self.assertRaisesRegex(expected, message):
                    await api.WisniowskiClient.async_from_code(self.session, "code", "verifier")

    async def test_userinfo_distinguishes_denied_access_from_service_failure(self):
        with patch.object(api, "KEYCLOAK_USERINFO_URL", str(self.server.make_url("/userinfo"))):
            for status, body, expected in (
                (401, "<html>Unauthorized</html>", api.WisniowskiAuthError),
                (403, '{"error": "insufficient_scope"}', api.WisniowskiAuthError),
                (429, '{"error": "Too many requests"}', api.WisniowskiApiError),
                (503, "<html>Unavailable</html>", api.WisniowskiApiError),
            ):
                with self.subTest(status=status):
                    self.responses = [(status, body)]
                    with self.assertRaisesRegex(expected, f"HTTP {status}"):
                        await self.client.async_userinfo()
            self.responses = [(200, '{"sub": "test-user"}')]
            self.assertEqual(await self.client.async_userinfo(), {"sub": "test-user"})
        self.assertEqual(self.token_requests, [])

    async def test_userinfo_transport_failure_is_retryable(self):
        for error in (ClientConnectionError("disconnected"), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                with patch.object(self.session, "get", side_effect=error):
                    with self.assertRaises(api.WisniowskiApiError) as raised:
                        await self.client.async_userinfo()
                self.assertIs(raised.exception.__cause__, error)

    def rest_client(self):
        return api.WisniowskiClient(
            self.session,
            {
                **self.client.data,
                "installation_id": "test-installation",
                "broker_url": str(self.server.make_url("/")),
            },
            self.token_updates.append,
        )

    async def test_rest_401_retries_same_command_once_with_refreshed_token(self):
        self.responses = [(401, "<html>Unauthorized</html>"), (200, '{"ok": true}')]
        self.token_lifetime = 60
        command = {"channelId": "test-gate", "direction": 2}
        result = await self.rest_client()._async_rest_post("/gate/setDirection", command)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            self.requests,
            [
                ("Bearer original-access-token", "test-installation", command),
                ("Bearer refreshed-access-token", "test-installation", command),
            ],
        )
        self.assertEqual(len(self.token_requests), 1)

    async def test_rest_503_does_not_replay_command(self):
        self.responses = [(503, '{"error": "Unavailable"}')]
        with self.assertRaisesRegex(api.WisniowskiApiError, "Unavailable"):
            await self.rest_client()._async_rest_post("/gate/setDirection", {"direction": 3})
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.token_requests, [])

    async def test_rest_repeated_401_stops_after_one_refresh(self):
        self.responses = [(401, "{}"), (401, "{}")]
        with self.assertRaisesRegex(api.WisniowskiAuthError, "401"):
            await self.rest_client()._async_rest_post("/gate/setDirection", {"direction": 3})
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(self.token_requests), 1)

    async def test_rest_does_not_replay_command_on_timeout(self):
        with patch.object(self.session, "post", side_effect=TimeoutError()) as post:
            with self.assertRaises(api.WisniowskiApiError):
                await self.rest_client()._async_rest_post("/gate/setDirection", {"direction": 3})
        post.assert_called_once()
        self.assertEqual(self.token_updates, [])


if __name__ == "__main__":
    unittest.main()
