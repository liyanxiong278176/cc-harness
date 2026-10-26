"""Transparent local Langfuse proxy with durable annotation revisions.

Only score mutations are journalled; credentials, cookies and login bodies are
never recorded. Requests have a durable intent before forwarding, and a durable
response before acknowledging success to the browser. Uncertain requests remain
explicit intents after a crash, rather than invented successful reviews.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import sqlite3
import threading
from urllib.parse import parse_qs, urlsplit
import uuid
import sys

sys.path.insert(0, "/app")
from secret_patterns import redact_credentials

LOCK = threading.Lock()
DB = Path(os.environ.get("AUDIT_DB", "/audit/revisions.db"))
MUTATIONS = {"scores.createAnnotationScore", "scores.updateAnnotationScore", "scores.deleteAnnotationScore", "scores.deleteMany"}
FIELDS = {"id", "name", "value", "stringValue", "comment", "dataType", "configId", "projectId", "scoreTarget", "scoreIds", "timestamp", "traceId", "observationId", "authorUserId", "updatedAt", "createdAt", "source", "queueId", "environment", "sessionId"}


def safe_fields(value):
    if isinstance(value, dict):
        return {key: safe_fields(item) for key, item in value.items() if key in FIELDS}
    if isinstance(value, list):
        return [safe_fields(item) for item in value]
    return redact_credentials(value) if isinstance(value, str) else value


def journal(request_id, phase, body):
    with LOCK, sqlite3.connect(DB) as db:
        db.execute("INSERT INTO revisions(request_id,phase,at,body) VALUES(?,?,?,?)",
                   (request_id, phase, datetime.now(timezone.utc).isoformat(), json.dumps(body, ensure_ascii=False)))


def decode_body(value):
    if isinstance(value, dict) and "json" in value:
        return value["json"]
    return value


def annotation_requests(path, raw):
    routes = urlsplit(path).path.removeprefix("/api/trpc/").split(",")
    if not any(route in MUTATIONS for route in routes):
        return []
    body = json.loads(raw)
    batch = parse_qs(urlsplit(path).query).get("batch") == ["1"]
    return [{"index": index, "route": route, "input": safe_fields(decode_body(body.get(str(index), {})) if batch else decode_body(body))}
            for index, route in enumerate(routes) if route in MUTATIONS]


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_):
        pass  # URLs, sessions and request bodies must not enter Docker logs.

    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def do_DELETE(self):
        self.forward()

    def do_PATCH(self):
        self.forward()

    def forward(self):
        if self.path.startswith("/__cc_harness/revisions"):
            expected = "Basic " + base64.b64encode((os.environ["LANGFUSE_PUBLIC_KEY"] + ":" + os.environ["LANGFUSE_SECRET_KEY"]).encode()).decode()
            import hmac
            if not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
                self.send_error(401)
                return
            try:
                after = int(parse_qs(urlsplit(self.path).query).get("after", ["0"])[0])
            except ValueError:
                self.send_error(400)
                return
            with LOCK, sqlite3.connect(DB) as db:
                rows = db.execute("SELECT seq,request_id,phase,at,body FROM revisions WHERE seq>? ORDER BY seq LIMIT 200", (after,)).fetchall()
            data = json.dumps({"data": [{"seq": r[0], "request_id": r[1], "phase": r[2], "at": r[3], "body": json.loads(r[4])} for r in rows]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            mutations = annotation_requests(self.path, raw) if self.command == "POST" and self.path.startswith("/api/trpc/") else []
            request_id = uuid.uuid4().hex
            if mutations:
                journal(request_id, "intent", mutations)
            headers = {key: value for key, value in self.headers.items() if key.lower() not in {"connection", "transfer-encoding", "accept-encoding"}}
            upstream = http.client.HTTPConnection(os.environ.get("UPSTREAM_HOST", "web"), int(os.environ.get("UPSTREAM_PORT", "3000")), timeout=120)
            upstream.request(self.command, self.path, body=raw, headers=headers)
            response = upstream.getresponse()
            if mutations:
                data = response.read()
                decoded = json.loads(data)
                values = decoded if isinstance(decoded, list) else [decoded]
                records = []
                for mutation in mutations:
                    item = values[mutation["index"]] if mutation["index"] < len(values) else {}
                    records.append({**mutation, "success": "error" not in item and "result" in item,
                                    "output": safe_fields(decode_body(item.get("result", {}).get("data", {})))})
                journal(request_id, "response", {"status": response.status, "records": records})
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in {"connection", "transfer-encoding"}:
                    self.send_header(key, value)
            self.end_headers()
            if mutations:
                self.wfile.write(data)
            else:
                while chunk := response.read(65536):
                    self.wfile.write(chunk)
            upstream.close()
        except (OSError, ValueError, sqlite3.Error, http.client.HTTPException):
            self.send_error(502, "Local Langfuse transport or audit storage unavailable")


if __name__ == "__main__":
    DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS revisions(seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, phase TEXT NOT NULL, at TEXT NOT NULL, body TEXT NOT NULL)")
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "3000"))), Proxy).serve_forever()
