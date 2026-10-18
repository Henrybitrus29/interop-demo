"""The HTTP layer, driven through the ASGI interface directly (no test client dependency)."""
import asyncio
import json
import unittest
from unittest import mock

from app import main


def call(method, path, body=b"", headers=None, client=("203.0.113.9", 5000)):
    """Send one request to the app and return (status, headers, body bytes)."""
    merged = {"host": "testserver", "content-length": str(len(body))}
    merged.update({k.lower(): v for k, v in (headers or {}).items()})          # callers may override, e.g. a spoofed length
    raw_headers = [(k.encode(), v.encode()) for k, v in merged.items()]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "headers": raw_headers,
             "client": client, "server": ("testserver", 80)}
    sent, messages = False, []

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    asyncio.run(main.app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], {k.decode().lower(): v.decode() for k, v in start["headers"]}, payload


def post(path, field, text, **kw):
    status, headers, body = call("POST", path, json.dumps({field: text}).encode(), {"content-type": "application/json"}, **kw)
    return status, json.loads(body)


class WebTestCase(unittest.TestCase):
    def setUp(self):
        main._hits.clear()


class PageAndStaticTests(WebTestCase):
    def test_index_is_served_with_security_headers(self):
        status, headers, body = call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])
        self.assertIn(b"Two converters you can run", body)
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertIn("default-src 'self'", headers["content-security-policy"])
        self.assertNotIn("unsafe-inline", headers["content-security-policy"])
        self.assertNotIn("unsafe-eval", headers["content-security-policy"])

    def test_static_assets_are_served(self):
        for path in ("/static/app.js", "/static/style.css", "/static/config.json"):
            with self.subTest(path):
                self.assertEqual(call("GET", path)[0], 200)

    def test_page_has_no_inline_script_or_style(self):
        html = call("GET", "/")[2].decode()
        self.assertNotIn("<script>", html)
        self.assertNotIn("style=", html)

    def test_unknown_path_and_wrong_method(self):
        self.assertEqual(call("GET", "/nope")[0], 404)
        self.assertEqual(call("GET", "/api/hl7")[0], 405)

    def test_healthz_reports_validation_layers(self):
        status, _, body = call("GET", "/healthz")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(set(data), {"status", "fhir_validation", "en16931_validation"})
        self.assertEqual(data["status"], "ok")
        self.assertIn(data["en16931_validation"], (True, False, None))      # None while the rule set warms up

    def test_healthz_does_not_wait_for_the_rule_set_to_compile(self):
        with mock.patch.object(main, "_schematron_ok", None), \
                mock.patch.object(main, "_schematron_available", side_effect=AssertionError("healthz must not wait for Saxon")):
            status, _, body = call("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(body)["en16931_validation"])

    def test_samples_endpoint(self):
        status, _, body = call("GET", "/api/samples")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(set(data), {"hl7", "invoice"})
        for kind in data:
            for s in data[kind]:
                self.assertEqual(set(s), {"id", "label", "note", "expect", "text"})
                self.assertTrue(s["text"].strip())


class ConvertTests(WebTestCase):
    def test_hl7_round_trip(self):
        text = next(s for s in main.SAMPLE_DATA["hl7"] if s["id"] == "oru_bmp.hl7")["text"]
        status, data = post("/api/hl7", "message", text)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual([s["id"] for s in data["rail"]], ["parse", "map", "validate", "deliver"])

    def test_invoice_round_trip(self):
        text = next(s for s in main.SAMPLE_DATA["invoice"] if s["id"] == "electricity_metered.json")["text"]
        status, data = post("/api/invoice", "invoice", text)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["summary"]["totals"]["payable"], "15.13")

    def test_invoice_uses_the_official_validator_when_present(self):
        class Issue:
            rule_id, severity, message = "BR-TEST", "error", "injected failure"
        text = next(s for s in main.SAMPLE_DATA["invoice"] if s["id"] == "simple_invoice.json")["text"]
        with mock.patch.object(main, "schematron_validate", lambda xml: [Issue()]), mock.patch.object(main, "_schematron_ok", True):
            status, data = post("/api/invoice", "invoice", text)
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["issues"][0]["where"], "BR-TEST")

    def test_rejected_input_is_a_normal_200_with_a_reason(self):
        status, data = post("/api/hl7", "message", "not hl7")
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertTrue(data["reason"])

    def test_script_in_input_comes_back_as_inert_data(self):
        status, data = post("/api/invoice", "invoice", '{"invoice_number": "<script>alert(1)</script>"}')
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])          # rejected, and the response is JSON the page renders with textContent


class BadRequestTests(WebTestCase):
    def test_body_that_is_not_json(self):
        status, _, body = call("POST", "/api/hl7", b"plain text", {"content-type": "application/json"})
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))

    def test_missing_or_wrong_typed_field(self):
        self.assertEqual(post("/api/hl7", "wrong_name", "x")[0], 400)
        status, _, _ = call("POST", "/api/invoice", b'{"invoice": 5}', {"content-type": "application/json"})
        self.assertEqual(status, 400)

    def test_oversized_body_is_refused(self):
        status, data = post("/api/hl7", "message", "x" * (main.MAX_BODY_BYTES + 10))
        self.assertEqual(status, 413)
        self.assertIn("too large", data["error"])

    def test_oversized_declared_length_is_refused_before_reading(self):
        status, _, _ = call("POST", "/api/hl7", b"{}", {"content-length": str(main.MAX_BODY_BYTES * 4)})
        self.assertEqual(status, 413)


class RateLimitTests(WebTestCase):
    def test_a_noisy_client_is_limited_but_others_are_not(self):
        with mock.patch.object(main, "RATE_LIMIT", 3):
            codes = [post("/api/hl7", "message", "x", client=("198.51.100.1", 1))[0] for _ in range(5)]
            self.assertEqual(codes, [200, 200, 200, 429, 429])
            self.assertEqual(post("/api/hl7", "message", "x", client=("198.51.100.2", 1))[0], 200)

    def test_forwarded_address_identifies_the_client_behind_a_proxy(self):
        with mock.patch.object(main, "RATE_LIMIT", 1):
            hdr = {"x-forwarded-for": "192.0.2.77, 10.0.0.1"}
            first = call("POST", "/api/hl7", b'{"message": "x"}', {"content-type": "application/json", **hdr})[0]
            second = call("POST", "/api/hl7", b'{"message": "x"}', {"content-type": "application/json", **hdr})[0]
            self.assertEqual((first, second), (200, 429))

    def test_reading_pages_is_never_limited(self):
        with mock.patch.object(main, "RATE_LIMIT", 1):
            self.assertTrue(all(call("GET", "/api/samples")[0] == 200 for _ in range(5)))


if __name__ == "__main__":
    unittest.main()
