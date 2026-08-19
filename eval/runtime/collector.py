#!/usr/bin/env python3
"""
Sentry envelope collector. Captures what an instrumented app actually emits and
turns it into the `observed.json` that gap/analyze.py already consumes.

This is the runtime half of the eval. Static grading (eval/grade.py) proves the
call sites exist; it structurally cannot prove a span runs on the right code path,
and it cannot resolve a value's real type when the source says `cart.total`. This
can do both, because it reads the bytes the SDK put on the wire.

No Sentry account needed. Point the app's DSN at this server:

    http://publickey@127.0.0.1:<port>/1
        → POST /api/1/envelope/?sentry_version=7&sentry_key=publickey

Wire-format facts, verified against a live @sentry/node 10.70.0 rather than docs:
  - `Transfer-Encoding: chunked` with no Content-Length; gzip is also possible on
    larger payloads. Both are handled — reading Content-Length alone captured
    zero bytes on the first attempt.
  - An envelope is newline-delimited JSON: one envelope header, then alternating
    item-header / payload lines.
  - For `type: transaction`, the ROOT span's name is the payload's top-level
    `transaction` field, not anything inside `contexts.trace`.
  - Root span attributes live in `contexts.trace.data`; child spans are in
    `spans[]` with the name in `description`.
  - SDK-internal attributes are prefixed `sentry.` (`sentry.op`,
    `sentry.origin`, `sentry.source`, `sentry.sample_rate`).
  - Attribute values arrive with their REAL types — `cart.value: 129.99` is a
    JSON number. That is the payoff over static analysis.

Usage:
    ./collector.py --port 9911 --duration 30 --out observed.json
    # or import EnvelopeCollector and use it as a context manager
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Attribute namespaces the SDK or OTel owns. Everything else is treated as
# customer-defined. Same reasoning as gap/instrumentation_profile.py, but here it
# is a heuristic rather than the documented `attributeSource.source_type` — the
# live API is not available to a local collector. Labelled as such in the output.
SDK_PREFIXES = (
    "sentry.", "http.", "db.", "server.", "client.", "url.", "user_agent.",
    "network.", "messaging.", "gen_ai.", "browser.", "device.", "os.", "process.",
    "thread.", "code.", "span.", "trace.", "resource.", "faas.", "cloud.", "k8s.",
    "otel.", "telemetry.", "service.", "rpc.", "graphql.", "cache.",
)


def json_type(value: Any) -> str:
    """Map a real JSON value to the `attributeType` vocabulary the Sentry
    attributes API uses: array | boolean | number | string."""
    if isinstance(value, bool):          # must precede int — bool subclasses int
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    return "string"


def parse_envelope(raw: bytes) -> tuple[dict, list[tuple[dict, Any]]]:
    """Split one envelope into (header, [(item_header, payload), ...])."""
    lines = [ln for ln in raw.split(b"\n") if ln.strip()]
    if not lines:
        return {}, []
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError:
        return {}, []
    items: list[tuple[dict, Any]] = []
    i = 1
    while i + 1 <= len(lines) - 1:
        try:
            item_header = json.loads(lines[i])
        except json.JSONDecodeError:
            break
        payload_raw = lines[i + 1]
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError:
            payload = payload_raw.decode("utf-8", "replace")
        items.append((item_header, payload))
        i += 2
    return header, items


def spans_from_payload(item_header: dict, payload: Any) -> list[dict]:
    """Normalise one envelope item into flat span records."""
    kind = (item_header or {}).get("type")
    out: list[dict] = []
    if not isinstance(payload, dict):
        return out

    if kind == "transaction":
        trace = (payload.get("contexts") or {}).get("trace") or {}
        data = dict(trace.get("data") or {})
        out.append({
            "name": payload.get("transaction") or trace.get("description") or "",
            "op": trace.get("op") or data.get("sentry.op") or "",
            "span_id": trace.get("span_id"),
            "trace_id": trace.get("trace_id"),
            "parent_span_id": trace.get("parent_span_id"),
            "status": trace.get("status"),
            "attributes": data,
            "is_root": True,
        })
        for s in payload.get("spans") or []:
            sdata = dict(s.get("data") or {})
            out.append({
                "name": s.get("description") or s.get("name") or "",
                "op": s.get("op") or sdata.get("sentry.op") or "",
                "span_id": s.get("span_id"),
                "trace_id": s.get("trace_id") or trace.get("trace_id"),
                "parent_span_id": s.get("parent_span_id"),
                "status": s.get("status"),
                "attributes": sdata,
                "is_root": False,
            })

    elif kind in ("span", "otel_span"):
        # Standalone span items, emitted by newer SDKs / span streaming.
        sdata = dict(payload.get("data") or payload.get("attributes") or {})
        out.append({
            "name": payload.get("description") or payload.get("name") or "",
            "op": payload.get("op") or sdata.get("sentry.op") or "",
            "span_id": payload.get("span_id"),
            "trace_id": payload.get("trace_id"),
            "parent_span_id": payload.get("parent_span_id"),
            "status": payload.get("status"),
            "attributes": sdata,
            "is_root": payload.get("is_segment", False),
        })
    return out


def build_observed(spans: list[dict], *, org: str = "local-eval",
                   stats_period: str = "runtime",
                   traces_sample_rate: float | None = 1.0) -> dict:
    """Assemble the `observed.json` shape gap/analyze.py consumes."""
    names: dict[str, int] = {}
    ops: dict[str, int] = {}
    pairs: dict[tuple[str, str], int] = {}
    attrs: dict[str, dict] = {}
    examples: dict[str, str] = {}

    for s in spans:
        if s["name"]:
            names[s["name"]] = names.get(s["name"], 0) + 1
            # name↔op pairing, which separate aggregate lists lose. Without it a
            # profile has to guess whether `SELECT * FROM carts` is a business span;
            # with it the op (`db.query`) settles the question definitively.
            key = (s["name"], s["op"] or "")
            pairs[key] = pairs.get(key, 0) + 1
            if s["name"] not in examples and s.get("trace_id"):
                examples[s["name"]] = s["trace_id"]
        if s["op"]:
            ops[s["op"]] = ops.get(s["op"], 0) + 1
        for key, value in (s.get("attributes") or {}).items():
            t = json_type(value)
            rec = attrs.get(key)
            if rec is None:
                attrs[key] = {
                    "key": key, "name": key, "attributeType": t,
                    "attributeSource": {
                        "source_type": "sentry" if key.startswith(SDK_PREFIXES)
                        else "user",
                    },
                    "observed_values": [value] if not isinstance(value, (dict, list))
                    else [],
                }
            elif rec["attributeType"] != t:
                # A key emitted as two different types is a real defect: half the
                # rows become unaggregatable. Surface the conflict, don't pick one.
                rec["attributeType"] = "string" if "string" in (rec["attributeType"], t) else t
                rec["type_conflict"] = sorted({rec["attributeType"], t})
            elif len(rec.get("observed_values", [])) < 5 and not isinstance(value, (dict, list)):
                if value not in rec["observed_values"]:
                    rec["observed_values"].append(value)

    return {
        "org": org,
        "dataset": "spans",
        "stats_period": stats_period,
        "traces_sample_rate": traces_sample_rate,
        "span_names": [{"name": n, "count": c}
                       for n, c in sorted(names.items(), key=lambda kv: -kv[1])],
        "span_ops": [{"op": o, "count": c}
                     for o, c in sorted(ops.items(), key=lambda kv: -kv[1])],
        "span_pairs": [{"name": n, "op": o, "count": c}
                       for (n, o), c in sorted(pairs.items(), key=lambda kv: -kv[1])],
        "attributes": [attrs[k] for k in sorted(attrs)],
        "example_traces": examples,
        "_provenance": {
            "source": "local Sentry envelope collector (eval/runtime/collector.py)",
            "attributeType": "derived from the REAL JSON value type on the wire — "
                             "more reliable than the attributes API, which reports a "
                             "single type per key",
            "attributeSource": "HEURISTIC (namespace prefix). The documented "
                               "`attributeSource.source_type` field is only available "
                               "from GET /trace-items/attributes/ against a live org.",
            "traces_sample_rate": "asserted by the runner, not observed",
        },
    }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    envelopes: list[bytes] = []
    lock = threading.Lock()

    def _read_body(self) -> bytes:
        te = (self.headers.get("transfer-encoding") or "").lower()
        if "chunked" in te:
            buf = b""
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                try:
                    size = int(line.strip().split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline()
                    break
                buf += self.rfile.read(size)
                self.rfile.readline()
            body = buf
        else:
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
        if (self.headers.get("content-encoding") or "").lower() == "gzip":
            try:
                body = gzip.decompress(body)
            except OSError:
                pass
        return body

    def do_POST(self) -> None:  # noqa: N802
        body = self._read_body()
        with _Handler.lock:
            _Handler.envelopes.append(body)
        payload = b'{"id":"00000000000000000000000000000000"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        payload = json.dumps({"envelopes": len(_Handler.envelopes)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # silence per-request noise
        pass


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        """Swallow the noise of a client dropping a keep-alive connection.

        The SDK and test clients close sockets on exit, which otherwise prints a
        full ConnectionResetError traceback to stderr on every run — alarming, and
        entirely uninformative. Real errors still surface through the response.
        """
        exc = sys.exc_info()[0]
        if exc in (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            return
        super().handle_error(request, client_address)


class EnvelopeCollector:
    """Context manager wrapping the capture server."""

    def __init__(self, port: int = 0, host: str = "127.0.0.1"):
        _Handler.envelopes = []
        self.server = _QuietServer((host, port), _Handler)
        self.host, self.port = self.server.server_address[0], self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> EnvelopeCollector:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def dsn(self) -> str:
        return f"http://publickey@{self.host}:{self.port}/1"

    @property
    def envelopes(self) -> list[bytes]:
        with _Handler.lock:
            return list(_Handler.envelopes)

    def spans(self) -> list[dict]:
        out: list[dict] = []
        for raw in self.envelopes:
            _, items = parse_envelope(raw)
            for item_header, payload in items:
                out += spans_from_payload(item_header, payload)
        return out

    def observed(self, **kw) -> dict:
        return build_observed(self.spans(), **kw)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Capture Sentry envelopes locally.")
    ap.add_argument("--port", type=int, default=9911)
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--out", default="observed.json")
    ap.add_argument("--raw-out", help="Also dump the raw envelopes here, for debugging.")
    args = ap.parse_args(argv)

    with EnvelopeCollector(port=args.port) as c:
        print(f"collector listening on {c.host}:{c.port}", file=sys.stderr)
        print(f"point the app at DSN: {c.dsn}", file=sys.stderr)
        deadline = time.time() + args.duration
        try:
            while time.time() < deadline:
                time.sleep(0.25)
        except KeyboardInterrupt:
            pass
        spans = c.spans()
        Path(args.out).write_text(json.dumps(c.observed(), indent=2) + "\n")
        if args.raw_out:
            Path(args.raw_out).write_bytes(b"\n===ENVELOPE===\n".join(c.envelopes))
    print(f"captured {len(c.envelopes)} envelope(s), {len(spans)} span(s) → {args.out}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
