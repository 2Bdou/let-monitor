"""Unit tests for the LowEndTalk Offers monitor."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import main

FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>Offers - LowEndTalk</title>
    <item>
      <title>Tokyo CN2 VPS &amp; more</title>
      <link>https://lowendtalk.com/discussion/1/tokyo-cn2</link>
      <pubDate>Tue, 06 Oct 2026 16:15:03 +0000</pubDate>
      <dc:creator>Alice</dc:creator>
      <guid isPermaLink="false">1@/discussions</guid>
      <description><![CDATA[<p>Hello <strong>LET</strong></p><p>CN2 GIA 9929</p>]]></description>
    </item>
    <item>
      <title>Cheap storage box in Europe</title>
      <link>https://lowendtalk.com/discussion/2/storage</link>
      <pubDate>Mon, 05 Oct 2026 10:00:00 +0000</pubDate>
      <dc:creator>Bob</dc:creator>
      <guid isPermaLink="false">2@/discussions</guid>
      <description><![CDATA[<p>HDD storage, EUR 20/year. WTB offers?</p>]]></description>
    </item>
  </channel>
</rss>
"""


def make_cfg(**overrides):
    cfg = main.Config()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


class FeedParsingTests(unittest.TestCase):
    def test_parse_feed(self):
        items = main.parse_feed(FEED)
        self.assertEqual(len(items), 2)
        first = items[0]
        self.assertEqual(first["title"], "Tokyo CN2 VPS & more")
        self.assertEqual(first["author"], "Alice")
        self.assertEqual(first["guid"], "1@/discussions")
        self.assertEqual(first["published"], "2026-10-06T16:15:03Z")
        self.assertIn("Hello LET", first["summary"])
        self.assertIn("CN2 GIA 9929", first["summary"])
        self.assertNotIn("<p>", first["summary"])

    def test_select_new_ignores_seen_guids(self):
        items = main.parse_feed(FEED)
        self.assertEqual(len(main.select_new(items, {})), 2)
        self.assertEqual(len(main.select_new(items, {"1@/discussions": "x"})), 1)

    def test_html_to_text_handles_entities_and_blocks(self):
        text = main.html_to_text("<p>A &amp; B</p><p>C<br>D</p>")
        self.assertEqual(text, "A & B\n\nC\nD")


class KeywordTests(unittest.TestCase):
    def test_plain_keywords_are_case_insensitive(self):
        cfg = make_cfg(keywords=main.compile_terms("cn2, storage"))
        items = main.parse_feed(FEED)
        self.assertTrue(main.match_item(items[0], cfg))
        self.assertTrue(main.match_item(items[1], cfg))

    def test_regex_keyword(self):
        cfg = make_cfg(keywords=main.compile_terms("/cn2|9929/i"))
        items = main.parse_feed(FEED)
        self.assertTrue(main.match_item(items[0], cfg))
        self.assertFalse(main.match_item(items[1], cfg))

    def test_body_matching_is_opt_in(self):
        cfg = make_cfg(keywords=main.compile_terms("hdd"), match_fields=("title",))
        items = main.parse_feed(FEED)
        self.assertFalse(main.match_item(items[1], cfg))

        cfg = make_cfg(keywords=main.compile_terms("hdd"), match_fields=("title", "body"))
        self.assertTrue(main.match_item(items[1], cfg))

    def test_exclude_wins(self):
        cfg = make_cfg(
            keywords=main.compile_terms("storage"),
            excludes=main.compile_terms("wtb"),
            match_fields=("title", "body"),
        )
        items = main.parse_feed(FEED)
        self.assertFalse(main.match_item(items[1], cfg))


class PayloadTests(unittest.TestCase):
    def test_payload_shape(self):
        cfg = make_cfg(keyword_label="cn2,9929", source="let-offers")
        items = main.parse_feed(FEED)[:1]
        payload = main.build_payload(items, cfg)

        self.assertEqual(payload["source"], "let-offers")
        self.assertEqual(payload["level"], "success")
        self.assertEqual(payload["data"], {"total": 1, "success": 1, "failed": 0})
        self.assertIn("LET Offers 新帖 1 条", payload["title"])
        self.assertIn("https://lowendtalk.com/discussion/1/tokyo-cn2", payload["content"])
        self.assertIn("cn2,9929", payload["content"])
        self.assertLessEqual(len(payload["title"]), 200)
        self.assertLessEqual(len(payload["content"]), main.CONTENT_LIMIT)

    def test_content_is_truncated(self):
        cfg = make_cfg()
        items = [
            {
                "title": "x" * 200,
                "link": "https://example.com/" + "y" * 100,
                "guid": str(i),
                "author": "a" * 50,
                "published": "2026-10-06T16:15:03Z",
                "summary": "z" * 500,
            }
            for i in range(30)
        ]
        payload = main.build_payload(items, cfg)
        self.assertLessEqual(len(payload["content"]), main.CONTENT_LIMIT)


class GatewayTests(unittest.TestCase):
    def test_send_gateway_posts_bearer_payload(self):
        captured = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length", "0"))
                captured["auth"] = self.headers.get("authorization")
                captured["body"] = json.loads(self.rfile.read(length))
                body = json.dumps(
                    {"success": True, "taskId": 7, "channels": {"telegram": {"status": "sent"}}}
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            cfg = make_cfg(
                notify_url=f"http://127.0.0.1:{server.server_port}/api/notify",
                notify_token="test-token",
            )
            result = main.send_gateway(cfg, {"source": "let-offers", "title": "t", "content": "c"})
            self.assertEqual(result["taskId"], 7)
            self.assertEqual(captured["auth"], "Bearer test-token")
            self.assertEqual(captured["body"]["source"], "let-offers")
        finally:
            server.shutdown()
            server.server_close()

    def test_gateway_401_is_not_retried(self):
        calls = {"n": 0}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                calls["n"] += 1
                body = b'{"success":false,"error":"invalid project API key"}'
                self.send_response(401)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            cfg = make_cfg(
                notify_url=f"http://127.0.0.1:{server.server_port}/api/notify",
                notify_token="bad",
            )
            with self.assertRaises(main.GatewayError):
                main.send_gateway(cfg, {"source": "s", "title": "t", "content": "c"})
            self.assertEqual(calls["n"], 1)
        finally:
            server.shutdown()
            server.server_close()


class PollTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tempdir.name, "state.json")
        self.original_fetch = main.fetch_feed

    def tearDown(self):
        main.fetch_feed = self.original_fetch
        self.tempdir.cleanup()

    def test_bootstrap_marks_existing_items_without_sending(self):
        main.fetch_feed = lambda cfg, state: (FEED, "etag-1", "lm-1")

        def boom(cfg, payload):
            raise AssertionError("gateway must not be called during bootstrap")

        original_send = main.send_gateway
        main.send_gateway = boom
        try:
            cfg = make_cfg(state_file=self.state_file, bootstrap="skip", dry_run=False)
            state = {"seen": {}}
            result = main.poll_once(cfg, state)
        finally:
            main.send_gateway = original_send

        self.assertEqual(result["sent"], 0)
        self.assertEqual(len(state["seen"]), 2)
        self.assertEqual(state["etag"], "etag-1")

    def test_dry_run_reports_matches_without_touching_state(self):
        main.fetch_feed = lambda cfg, state: (FEED, "etag-1", "lm-1")
        cfg = make_cfg(
            state_file=self.state_file,
            dry_run=True,
            keywords=main.compile_terms("cn2"),
        )
        state = {"seen": {}}
        result = main.poll_once(cfg, state)

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(state["seen"], {})


if __name__ == "__main__":
    unittest.main()
