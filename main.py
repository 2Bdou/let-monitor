#!/usr/bin/env python3
"""LowEndTalk Offers monitor -> notify-gateway.

Fetches https://lowendtalk.com/categories/offers/feed.rss, filters new
threads by keyword, and posts a single structured report to a
notify-gateway project (POST /api/notify).

Only the Python standard library is used, so the ecloudserv Python
container needs no dependencies.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable, Optional, Pattern

DEFAULT_FEED = "https://lowendtalk.com/categories/offers/feed.rss"
USER_AGENT = (
    "Mozilla/5.0 (compatible; let-offers-monitor/1.0; "
    "+https://github.com/2Bdou/notify-gateway)"
)
SEEN_LIMIT = 3000
CONTENT_LIMIT = 4700
DC_CREATOR = "{http://purl.org/dc/elements/1.1/}creator"


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def utcnow() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def log(message: str) -> None:
    print(f"[{utcnow()}] {message}", flush=True)


def load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines without overriding real environment variables."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("'\"")
                if key:
                    os.environ.setdefault(key, value)
    except OSError as exc:
        log(f"could not read {path}: {exc}")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    raw = env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc


def env_bool(name: str, default: bool = False) -> bool:
    raw = env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def compile_terms(raw: str) -> list[Pattern[str]]:
    """Comma separated terms. A term like /cn2|9929/i is a regex."""
    terms: list[Pattern[str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith("/") and part.rfind("/") > 0:
            end = part.rfind("/")
            body = part[1:end]
            flags_text = part[end + 1 :]
            flags = re.IGNORECASE if "i" in flags_text else 0
            try:
                terms.append(re.compile(body, flags))
            except re.error as exc:
                raise SystemExit(f"invalid keyword regex {part!r}: {exc}") from exc
        else:
            terms.append(re.compile(re.escape(part), re.IGNORECASE))
    return terms


# --------------------------------------------------------------------------- #
# config / state
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    feed_url: str = DEFAULT_FEED
    keywords: list[Pattern[str]] = field(default_factory=list)
    excludes: list[Pattern[str]] = field(default_factory=list)
    keyword_label: str = ""
    match_fields: tuple[str, ...] = ("title",)
    notify_url: str = ""
    notify_token: str = ""
    source: str = "let-offers"
    interval_min: int = 10
    state_file: str = "state.json"
    bootstrap: str = "skip"
    dry_run: bool = False
    max_per_run: int = 10
    port: int = 3000
    timeout: int = 20
    channels: list[str] = field(default_factory=list)


def load_config() -> Config:
    keyword_label = env("KEYWORDS")
    fields_raw = env("MATCH_FIELDS", "title").lower()
    match_fields = tuple(
        part.strip() for part in fields_raw.split(",") if part.strip() in ("title", "body")
    )
    if not match_fields:
        match_fields = ("title",)

    channels = [
        part.strip()
        for part in env("GATEWAY_CHANNEL").lower().split(",")
        if part.strip() in ("email", "telegram")
    ]

    bootstrap = env("BOOTSTRAP", "skip").lower()
    if bootstrap not in ("skip", "notify"):
        raise SystemExit("BOOTSTRAP must be 'skip' or 'notify'")

    source = env("SOURCE", "let-offers")[:80]

    cfg = Config(
        feed_url=env("FEED_URL", DEFAULT_FEED),
        keywords=compile_terms(keyword_label),
        excludes=compile_terms(env("EXCLUDE_KEYWORDS")),
        keyword_label=keyword_label[:120],
        match_fields=match_fields,
        notify_url=env("NOTIFY_URL"),
        notify_token=env("NOTIFY_TOKEN"),
        source=source,
        interval_min=max(1, env_int("INTERVAL_MIN", 10)),
        state_file=env("STATE_FILE", "state.json"),
        bootstrap=bootstrap,
        dry_run=env_bool("DRY_RUN"),
        max_per_run=max(1, env_int("MAX_PER_RUN", 10)),
        port=env_int("PORT", 3000),
        timeout=max(5, env_int("REQUEST_TIMEOUT", 20)),
        channels=channels,
    )

    if not cfg.dry_run:
        missing = [
            name
            for name, value in (
                ("NOTIFY_URL", cfg.notify_url),
                ("NOTIFY_TOKEN", cfg.notify_token),
            )
            if not value
        ]
        if missing:
            raise SystemExit(
                "missing " + ", ".join(missing) + " (set them in the panel, or use DRY_RUN=1)"
            )
        if not re.match(r"^https?://", cfg.notify_url):
            raise SystemExit("NOTIFY_URL must start with http:// or https://")
        if not cfg.notify_url.rstrip("/").endswith("/api/notify"):
            log("warning: NOTIFY_URL does not end with /api/notify")

    return cfg


def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("state root is not an object")
    except FileNotFoundError:
        return {"seen": {}}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        log(f"state file {path} unreadable ({exc}); starting fresh")
        return {"seen": {}}
    if not isinstance(data.get("seen"), dict):
        data["seen"] = {}
    return data


def save_state(path: str, state: dict) -> None:
    seen = state.get("seen", {})
    if len(seen) > SEEN_LIMIT:
        state["seen"] = dict(
            sorted(seen.items(), key=lambda item: str(item[1]))[-SEEN_LIMIT:]
        )
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temporary, path)


# --------------------------------------------------------------------------- #
# feed parsing
# --------------------------------------------------------------------------- #

_BLOCK_TAGS = {
    "p",
    "br",
    "div",
    "li",
    "ul",
    "ol",
    "tr",
    "td",
    "th",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "section",
    "article",
    "pre",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: str) -> str:
    if not value:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html.unescape(value))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _text(node: ET.Element, tag: str) -> str:
    child = node.find(tag)
    if child is None:
        return ""
    return "".join(child.itertext()).strip()


def _iso_date(raw: str) -> str:
    if not raw:
        return ""
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (
            parsed.astimezone(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    except (TypeError, ValueError):
        return raw


def parse_feed(xml_bytes: bytes) -> list[dict]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise RuntimeError(f"invalid RSS: {exc}") from exc

    channel = root.find("channel")
    if channel is None:
        channel = root

    items: list[dict] = []
    for node in channel.findall("item"):
        title = _text(node, "title")
        link = _text(node, "link")
        guid = _text(node, "guid") or link
        if not title or not link:
            continue
        description = _text(node, "description")
        items.append(
            {
                "title": html.unescape(title),
                "link": link,
                "guid": guid,
                "author": html.unescape(_text(node, DC_CREATOR)),
                "published": _iso_date(_text(node, "pubDate")),
                "summary": html_to_text(description),
            }
        )
    return items


def select_new(items: Iterable[dict], seen: dict) -> list[dict]:
    return [item for item in items if item["guid"] not in seen]


def match_item(item: dict, cfg: Config) -> bool:
    chunks = []
    if "title" in cfg.match_fields:
        chunks.append(item.get("title", ""))
    if "body" in cfg.match_fields:
        chunks.append(item.get("summary", ""))
    text = "\n".join(chunks)

    if cfg.excludes and any(pattern.search(text) for pattern in cfg.excludes):
        return False
    if not cfg.keywords:
        return True
    return any(pattern.search(text) for pattern in cfg.keywords)


# --------------------------------------------------------------------------- #
# notify-gateway
# --------------------------------------------------------------------------- #


class GatewayError(RuntimeError):
    pass


def build_payload(items: list[dict], cfg: Config) -> dict:
    count = len(items)
    title = f"LET Offers 新帖 {count} 条"
    if len(title) > 200:
        title = title[:199] + "…"

    header = "LowEndTalk Offers"
    if cfg.keyword_label:
        header += f"\n关键词: {cfg.keyword_label}"

    blocks: list[str] = []
    for index, item in enumerate(items, start=1):
        title_text = item["title"]
        if len(title_text) > 160:
            title_text = title_text[:159].rstrip() + "…"
        lines = [f"{index}. {title_text}"]
        meta = []
        if item.get("author"):
            meta.append(f"作者 {item['author']}")
        published = item.get("published") or ""
        if re.match(r"^\d{4}-\d{2}-\d{2}", published):
            published = published[:10]
        if published:
            meta.append(published)
        if meta:
            lines.append("   " + " · ".join(meta))
        lines.append(f"   {item['link']}")
        summary = re.sub(r"\s+", " ", item.get("summary") or "").strip()
        if summary:
            if len(summary) > 180:
                summary = summary[:179].rstrip() + "…"
            lines.append(f"   {summary}")
        blocks.append("\n".join(lines))

    content = header + "\n\n" + "\n\n".join(blocks)
    if len(content) > CONTENT_LIMIT:
        content = content[: CONTENT_LIMIT - 1].rstrip() + "…"

    payload = {
        "source": cfg.source,
        "title": title,
        "content": content,
        "level": "success",
        "data": {"total": count, "success": count, "failed": 0},
    }
    if cfg.channels:
        payload["channel"] = cfg.channels
    return payload


def send_gateway(cfg: Config, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "authorization": f"Bearer {cfg.notify_token}",
        "content-type": "application/json; charset=utf-8",
        "user-agent": USER_AGENT,
    }
    last_error = ""

    for attempt in range(1, 4):
        request = urllib.request.Request(
            cfg.notify_url, data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read().decode("utf-8", "replace")
            data = json.loads(raw) if raw.strip() else {}
            if not isinstance(data, dict):
                raise GatewayError("gateway returned non-object JSON")
            if data.get("success") is False:
                raise GatewayError(
                    f"gateway rejected: {data.get('error') or raw[:200]}"
                )
            return data
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            if exc.code in (400, 401, 403):
                raise GatewayError(
                    f"gateway HTTP {exc.code}: {raw[:300]}"
                ) from exc
            last_error = f"HTTP {exc.code}: {raw[:200]}"
            if attempt < 3:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                delay = (
                    int(retry_after)
                    if retry_after and retry_after.isdigit()
                    else 2**attempt
                )
                time.sleep(min(delay, 30))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < 3:
                time.sleep(min(2**attempt, 30))
        except GatewayError:
            raise

    raise GatewayError(f"gateway send failed after 3 attempts: {last_error}")


# --------------------------------------------------------------------------- #
# polling
# --------------------------------------------------------------------------- #


class FeedNotModified(Exception):
    pass


def fetch_feed(cfg: Config, state: dict) -> tuple[bytes, Optional[str], Optional[str]]:
    headers = {
        "user-agent": USER_AGENT,
        "accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
    }
    if state.get("etag"):
        headers["if-none-match"] = str(state["etag"])
    if state.get("last_modified"):
        headers["if-modified-since"] = str(state["last_modified"])

    request = urllib.request.Request(cfg.feed_url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout) as response:
            return (
                response.read(),
                response.headers.get("ETag"),
                response.headers.get("Last-Modified"),
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            raise FeedNotModified() from exc
        raise RuntimeError(f"feed HTTP {exc.code}: {exc.reason}") from exc


def poll_once(cfg: Config, state: dict) -> dict:
    now = utcnow()
    try:
        raw, etag, last_modified = fetch_feed(cfg, state)
    except FeedNotModified:
        state["last_poll"] = now
        state["last_error"] = None
        save_state(cfg.state_file, state)
        log("feed not modified (304)")
        return {"new": 0, "matched": 0, "sent": 0, "error": None}

    items = parse_feed(raw)
    seen = state.setdefault("seen", {})
    new_items = select_new(items, seen)

    if not seen and cfg.bootstrap == "skip" and not cfg.dry_run:
        for item in items:
            seen[item["guid"]] = now
        state.update(
            etag=etag,
            last_modified=last_modified,
            last_poll=now,
            last_error=None,
            bootstrapped=len(items),
        )
        save_state(cfg.state_file, state)
        log(
            f"bootstrap: marked {len(items)} existing item(s) as seen; "
            "no notification sent"
        )
        return {"new": 0, "matched": 0, "sent": 0, "error": None}

    matched = [item for item in new_items if match_item(item, cfg)]

    if cfg.dry_run:
        log(
            f"dry-run: {len(new_items)} new, {len(matched)} matched "
            "(state not changed)"
        )
        for item in matched[: cfg.max_per_run]:
            log(f"  * {item['title']}  {item['link']}")
        return {
            "new": len(new_items),
            "matched": len(matched),
            "sent": 0,
            "error": None,
        }

    matched_guids = {item["guid"] for item in matched}
    for item in new_items:
        if item["guid"] not in matched_guids:
            seen[item["guid"]] = now

    if not matched:
        state.update(
            etag=etag,
            last_modified=last_modified,
            last_poll=now,
            last_error=None,
        )
        save_state(cfg.state_file, state)
        log(f"{len(new_items)} new, 0 matched")
        return {"new": len(new_items), "matched": 0, "sent": 0, "error": None}

    send_items = matched[: cfg.max_per_run]
    remaining = len(matched) - len(send_items)
    payload = build_payload(send_items, cfg)
    if remaining:
        payload["content"] += f"\n\n另有 {remaining} 条命中，下一轮继续。"

    try:
        result = send_gateway(cfg, payload)
    except Exception as exc:
        state["last_poll"] = now
        state["last_error"] = str(exc)
        # Deliberately do not persist etag/last_modified: the next poll must
        # fetch the feed again so these items can be retried.
        save_state(cfg.state_file, state)
        log(f"send failed: {exc}")
        return {
            "new": len(new_items),
            "matched": len(matched),
            "sent": 0,
            "error": str(exc),
        }

    for item in send_items:
        seen[item["guid"]] = now
    state.update(
        etag=etag,
        last_modified=last_modified,
        last_poll=now,
        last_error=None,
        last_sent=now,
        last_task=result.get("taskId"),
    )
    save_state(cfg.state_file, state)
    channels = result.get("channels") or {}
    channel_text = ", ".join(
        f"{name}:{value.get('status') if isinstance(value, dict) else value}"
        for name, value in channels.items()
    )
    log(
        f"sent {len(send_items)} item(s), taskId={result.get('taskId')}"
        + (f", channels={channel_text}" if channel_text else "")
    )
    return {
        "new": len(new_items),
        "matched": len(matched),
        "sent": len(send_items),
        "error": None,
    }


def cmd_test_notify(cfg: Config) -> dict:
    sample = {
        "title": "LET Offers 监控测试",
        "link": cfg.feed_url,
        "guid": "test",
        "author": "let-offers-monitor",
        "published": utcnow(),
        "summary": "这是一条测试通知。收到即表示 notify-gateway 已接通。",
    }
    payload = build_payload([sample], cfg)
    payload["content"] = (
        "这是一条测试通知。收到即表示 notify-gateway 已接通。\n\n"
        f"Feed: {cfg.feed_url}"
    )
    return send_gateway(cfg, payload)


# --------------------------------------------------------------------------- #
# health endpoint / runtime
# --------------------------------------------------------------------------- #


def start_health_server(cfg: Config, state: dict):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path not in ("/", "/health"):
                self.send_error(404)
                return
            body = json.dumps(
                {
                    "ok": True,
                    "service": "let-offers-monitor",
                    "feed": cfg.feed_url,
                    "seen": len(state.get("seen", {})),
                    "last_poll": state.get("last_poll"),
                    "last_sent": state.get("last_sent"),
                    "last_error": state.get("last_error"),
                    "interval_min": cfg.interval_min,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            return

    try:
        server = ThreadingHTTPServer(("0.0.0.0", cfg.port), Handler)
    except OSError as exc:
        log(f"health server disabled on port {cfg.port}: {exc}")
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"health endpoint listening on 0.0.0.0:{cfg.port}")
    return server


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument(
        "--test-notify", action="store_true", help="send one test notification"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print matches without sending"
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.dry_run:
        cfg.dry_run = True

    if args.test_notify:
        result = cmd_test_notify(cfg)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    state = load_state(cfg.state_file)

    if args.once:
        result = poll_once(cfg, state)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if not result.get("error") else 1

    server = start_health_server(cfg, state)
    stop = threading.Event()

    def handle_stop(*_args) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    log(
        f"starting: feed={cfg.feed_url} interval={cfg.interval_min}m "
        f"keywords={cfg.keyword_label or '(all)'} dry_run={cfg.dry_run}"
    )
    while not stop.is_set():
        try:
            result = poll_once(cfg, state)
            if result.get("error"):
                log(f"poll finished with error: {result['error']}")
        except Exception as exc:  # keep the worker alive
            state["last_poll"] = utcnow()
            state["last_error"] = str(exc)
            try:
                save_state(cfg.state_file, state)
            except OSError:
                pass
            log(f"poll crashed: {exc}")
        stop.wait(cfg.interval_min * 60)

    if server is not None:
        server.shutdown()
        server.server_close()
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
