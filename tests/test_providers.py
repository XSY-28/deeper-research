import io
import json
import os
import socket
import threading
import unittest
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

try:
    from deeper_research import providers
except ImportError:
    providers = None


def profile_data(protocol="deepseek_chat", **changes):
    data = {
        "protocol": protocol,
        "base_url": "https://api.example.test/v1",
        "model": "configured-model",
        "api_key_env": "TEST_PROVIDER_KEY",
        "input_usd_per_million": "0.30",
        "output_usd_per_million": "1.20",
        "options": {},
    }
    data.update(changes)
    return data


def chat_response(**changes):
    data = {
        "id": "chat-123",
        "model": "served-model",
        "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
    }
    data.update(changes)
    return data


def responses_response(**changes):
    data = {
        "id": "resp-123",
        "model": "served-responses",
        "status": "completed",
        "output": [{"type": "message", "status": "completed", "content": [
            {"type": "output_text", "text": '{"ok":true}'}]}],
        "usage": {"input_tokens": 30, "output_tokens": 12, "total_tokens": 42,
                  "input_tokens_details": {"cached_tokens": 10},
                  "output_tokens_details": {"reasoning_tokens": 7}},
    }
    data.update(changes)
    return data


class Response(io.BytesIO):
    def __init__(self, body):
        super().__init__(json.dumps(body).encode("utf-8"))
        self.headers = {"x-request-id": "http-123"}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(providers, "The provider boundary is not implemented")
        self.env = patch.dict(os.environ, {"TEST_PROVIDER_KEY": "test-secret-key"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.messages = [{"role": "system", "content": "Return JSON."},
                         {"role": "user", "content": "Question"}]

    def make_provider(self, protocol="deepseek_chat", **changes):
        return providers.create_provider(providers.ProviderProfile.from_dict(profile_data(protocol, **changes)))

    def test_example_config_profiles_are_valid_without_real_credentials(self):
        path = Path(__file__).resolve().parents[1] / "config.example.json"
        self.assertTrue(path.is_file(), "The configurable provider example is missing")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn(data["default_profile"], data["profiles"])
        self.assertGreaterEqual(Decimal(data["project_budget_usd"]), 0)
        self.assertEqual({value["protocol"] for value in data["profiles"].values()},
                         {"deepseek_chat", "openai_chat", "openai_responses"})
        for profile in data["profiles"].values():
            parsed = providers.ProviderProfile.from_dict(profile)
            self.assertEqual(parsed.public_dict()["model"], profile["model"])

    def dispatch(self, provider, body):
        with patch.object(providers, "urlopen", return_value=Response(body)) as transport:
            result = provider.complete(self.messages, max_output_tokens=100, timeout=9)
        request = transport.call_args.args[0]
        return result, request, json.loads(request.data), transport

    def test_profile_prices_are_explicit_decimal_and_public_config_has_no_key(self):
        profile = self.make_provider().profile
        self.assertEqual(profile.input_usd_per_million, Decimal("0.30"))
        public = profile.public_dict()
        self.assertEqual(public["output_usd_per_million"], "1.20")
        self.assertEqual(public["api_key_env"], "TEST_PROVIDER_KEY")
        self.assertNotIn("test-secret-key", json.dumps(public))
        public["options"]["thinking"]["type"] = "enabled"
        self.assertEqual(profile.options["thinking"]["type"], "disabled")

    def test_missing_invalid_or_nonfinite_prices_rejected(self):
        for field in ("input_usd_per_million", "output_usd_per_million"):
            data = profile_data()
            del data[field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                providers.ProviderProfile.from_dict(data)
            for price in ("-1", "NaN", "Infinity", "bad", True, None):
                with self.subTest(field=field, price=price), self.assertRaises(ValueError):
                    providers.ProviderProfile.from_dict(profile_data(**{field: price}))

    def test_unsafe_urls_rejected_and_loopback_http_accepted(self):
        for url in ("http://example.com", "https://user:password@example.com", "https://example.com?q=key",
                    "https://example.com#fragment", "file:///tmp/api", "https://", "https://example.com:bad",
                    "https://example.com\nHeader: injected"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.make_provider(base_url=url)
        for url in ("http://127.0.0.1:8080/v1", "http://localhost:8080", "http://[::1]:8080/v1"):
            self.assertEqual(self.make_provider(base_url=url).profile.base_url, url)

    def test_unrecognized_config_or_protocol_rejected(self):
        for extra in ({"api_key": "secret"}, {"protocol": "unknown"}, {"api_key_env": "bad-key"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.make_provider(**extra)

    def test_options_are_protocol_specific_and_cannot_override_boundary(self):
        for protocol, option in (("openai_chat", {"thinking": {"type": "disabled"}}),
                                 ("openai_responses", {"reasoning_effort": "low"}),
                                 ("deepseek_chat", {"reasoning": {"effort": "low"}})):
            with self.subTest(protocol=protocol), self.assertRaises(ValueError):
                self.make_provider(protocol, options=option)
        for option in ({"model": "another"}, {"stream": True}, {"max_tokens": 1}, {"headers": {}},
                       {"thinking": {"type": "disabled", "secret": "x"}}, {"json_object": "yes"},
                       {"thinking": {"type": "wrong"}}, {"token_limit_field": "messages"}):
            with self.subTest(option=option), self.assertRaises(ValueError):
                self.make_provider(options=option)

    def test_deepseek_default_payload_and_chat_normalization(self):
        result, request, payload, transport = self.dispatch(self.make_provider(), chat_response())
        self.assertEqual(request.full_url, "https://api.example.test/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret-key")
        self.assertEqual(payload, {"model": "configured-model", "messages": self.messages,
                                 "stream": False, "max_tokens": 100, "thinking": {"type": "disabled"}})
        self.assertEqual((result.input_tokens, result.output_tokens, result.total_tokens), (20, 8, 28))
        self.assertEqual(result.content, '{"ok":true}')
        self.assertEqual(result.model, "served-model")
        self.assertEqual(result.request_id, "http-123")
        self.assertEqual(transport.call_args.kwargs["timeout"], 9)

    def test_generic_chat_options_and_v1_endpoint_are_separate(self):
        provider = self.make_provider("openai_chat", options={"reasoning_effort": "low", "json_object": True,
                                                             "token_limit_field": "max_completion_tokens"})
        _, request, payload, _ = self.dispatch(provider, chat_response())
        self.assertEqual(request.full_url, "https://api.example.test/v1/chat/completions")
        self.assertNotIn("thinking", payload)
        self.assertNotIn("max_tokens", payload)
        self.assertEqual(payload["max_completion_tokens"], 100)
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_responses_emits_own_protocol_and_normalizes_nested_usage(self):
        provider = self.make_provider("openai_responses", options={"reasoning": {"effort": "low"}, "json_object": True})
        body = responses_response()
        result, request, payload, _ = self.dispatch(provider, body)
        self.assertEqual(request.full_url, "https://api.example.test/v1/responses")
        self.assertEqual(payload, {"model": "configured-model", "input": self.messages,
                                 "stream": False, "max_output_tokens": 100, "reasoning": {"effort": "low"},
                                 "text": {"format": {"type": "json_object"}}})
        self.assertEqual((result.input_tokens, result.output_tokens, result.total_tokens), (30, 12, 42))
        self.assertEqual(result.raw_usage, body["usage"])
        self.assertEqual(result.content, '{"ok":true}')

    def test_cache_and_reasoning_are_not_added_again_to_provider_totals(self):
        usage = {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130,
                 "prompt_cache_hit_tokens": 70, "prompt_cache_miss_tokens": 30,
                 "completion_tokens_details": {"reasoning_tokens": 20}}
        result, *_ = self.dispatch(self.make_provider(), chat_response(usage=usage))
        self.assertEqual((result.input_tokens, result.output_tokens, result.total_tokens), (100, 30, 130))
        self.assertEqual(result.raw_usage, usage)

    def test_missing_usage_and_partial_usage_remain_unknown(self):
        for protocol, body in (("openai_chat", chat_response()), ("openai_responses", responses_response())):
            del body["usage"]
            result, *_ = self.dispatch(self.make_provider(protocol), body)
            self.assertEqual((result.input_tokens, result.output_tokens, result.total_tokens), (None, None, None))
        result, *_ = self.dispatch(self.make_provider(), chat_response(usage={"prompt_tokens": 12}))
        self.assertEqual((result.input_tokens, result.output_tokens, result.total_tokens), (12, None, None))

    def test_chat_truncation_is_distinct_error(self):
        body = chat_response(choices=[{"finish_reason": "length", "message": {"content": '{"ok":'}}])
        with self.assertRaises(providers.IncompleteResponseError) as ctx:
            self.dispatch(self.make_provider(), body)
        self.assertFalse(ctx.exception.outcome_unknown)

    def test_responses_incomplete_status_is_distinct_error(self):
        for body in (responses_response(status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
                     responses_response(output=[{"type": "message", "status": "incomplete", "content": [
                         {"type": "output_text", "text": "partial"}]}])):
            with self.subTest(body=body), self.assertRaises(providers.IncompleteResponseError):
                self.dispatch(self.make_provider("openai_responses"), body)

    def test_missing_content_refusals_or_bad_response_are_rejected(self):
        for body in (chat_response(choices=[]), chat_response(choices=[{"finish_reason": "stop", "message": {}}]),
                     chat_response(choices=[{"finish_reason": "stop", "message": {"content": ""}}]),
                     chat_response(choices=[{"finish_reason": "tool_calls", "message": {"content": "hello"}}]),
                     ["bad"]):
            with self.subTest(body=body), self.assertRaises(providers.ProviderError):
                self.dispatch(self.make_provider(), body)
        body = responses_response(output=[{"type": "message", "status": "completed", "content": [
            {"type": "refusal", "refusal": "no"}]}])
        with self.assertRaises(providers.ProviderError):
            self.dispatch(self.make_provider("openai_responses"), body)

    def test_missing_credentials_prevents_transport_and_validation_is_public(self):
        os.environ.pop("TEST_PROVIDER_KEY")
        provider = self.make_provider()
        with patch.object(providers, "urlopen") as transport:
            with self.assertRaises(providers.ProviderError):
                provider.validate_credentials()
            with self.assertRaises(providers.ProviderError) as ctx:
                provider.complete(self.messages, max_output_tokens=100, timeout=1)
        self.assertEqual(transport.call_count, 0)
        self.assertFalse(ctx.exception.outcome_unknown)

    def test_credentials_are_resolved_for_each_call_not_cached(self):
        provider = self.make_provider()
        os.environ["TEST_PROVIDER_KEY"] = "replacement-key"
        _, request, _, _ = self.dispatch(provider, chat_response())
        self.assertEqual(request.get_header("Authorization"), "Bearer replacement-key")
        self.assertNotIn("replacement-key", repr(provider.profile))

    def test_non_header_safe_credentials_are_redacted_before_dispatch(self):
        for key in ("secret\x7fkey", "secret\u4e2dkey"):
            os.environ["TEST_PROVIDER_KEY"] = key
            with self.subTest(key=repr(key)), patch.object(providers, "urlopen", return_value=Response(chat_response())) as transport:
                with self.assertRaises(providers.ProviderError) as ctx:
                    self.make_provider().complete(self.messages, max_output_tokens=10, timeout=1)
                self.assertNotIn(key, str(ctx.exception))
                self.assertEqual(transport.call_count, 0)

    def test_http_rejection_is_known_redacted_and_not_retried(self):
        error = HTTPError("https://test-secret-key.example", 429, "test-secret-key", {}, io.BytesIO(b"test-secret-key"))
        with patch.object(providers, "urlopen", side_effect=error) as transport:
            with self.assertRaises(providers.ProviderError) as ctx:
                self.make_provider().complete(self.messages, max_output_tokens=100, timeout=1)
        self.assertFalse(ctx.exception.outcome_unknown)
        self.assertNotIn("test-secret-key", str(ctx.exception))
        self.assertIn("429", str(ctx.exception))
        self.assertEqual(transport.call_count, 1)

    def test_transport_failure_is_unknown_redacted_and_not_retried(self):
        for error in (URLError("test-secret-key"), socket.timeout("test-secret-key"), OSError("test-secret-key")):
            with self.subTest(error=type(error).__name__), patch.object(providers, "urlopen", side_effect=error) as transport:
                with self.assertRaises(providers.ProviderError) as ctx:
                    self.make_provider().complete(self.messages, max_output_tokens=100, timeout=1)
                self.assertTrue(ctx.exception.outcome_unknown)
                self.assertNotIn("test-secret-key", str(ctx.exception))
                self.assertEqual(transport.call_count, 1)

    def test_invalid_dispatch_arguments_fail_before_transport(self):
        for caps, timeout in ((0, 1), (True, 1), (100, 0), (100, float("nan")), (100, float("inf"))):
            with self.subTest(caps=caps, timeout=timeout), patch.object(providers, "urlopen") as transport:
                with self.assertRaises(ValueError):
                    self.make_provider().complete(self.messages, max_output_tokens=caps, timeout=timeout)
                self.assertEqual(transport.call_count, 0)

    def test_local_http_transport_emits_request_and_does_not_follow_redirect(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                received.append((self.path, self.headers["Authorization"], json.loads(self.rfile.read(length))))
                if self.path == "/redirect/chat/completions":
                    self.send_response(307)
                    self.send_header("Location", "/forwarded")
                    self.end_headers()
                    return
                body = json.dumps(chat_response()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("x-request-id", "local-request-id")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            result = self.make_provider(base_url=base + "/v1").complete(self.messages, max_output_tokens=17, timeout=2)
            self.assertEqual(result.request_id, "local-request-id")
            self.assertEqual(received[0], ("/v1/chat/completions", "Bearer test-secret-key", {
                "model": "configured-model", "messages": self.messages, "stream": False,
                "max_tokens": 17, "thinking": {"type": "disabled"}}))
            with self.assertRaises(providers.ProviderError) as ctx:
                self.make_provider(base_url=base + "/redirect").complete(self.messages, max_output_tokens=17, timeout=2)
            self.assertFalse(ctx.exception.outcome_unknown)
            self.assertEqual([row[0] for row in received], ["/v1/chat/completions", "/redirect/chat/completions"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        for messages in ([], [{"role": "user", "content": None}], [{"role": "tool", "content": "x"}],
                         [{"role": "user", "content": "x", "api_key": "secret"}]):
            with self.subTest(messages=messages), patch.object(providers, "urlopen") as transport:
                with self.assertRaises(ValueError):
                    self.make_provider().complete(messages, max_output_tokens=10, timeout=1)
                self.assertEqual(transport.call_count, 0)


if __name__ == "__main__":
    unittest.main()
