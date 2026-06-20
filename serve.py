#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
This module serves a WSGI application using werkzeug, with optional
request recording and replay capabilities.

Author: Logan Raarup <logan@logan.dk>
"""
import argparse
import hashlib
import importlib
import io
import json
import os
import sys
import time
from urllib.parse import urlencode

try:
    from werkzeug import serving
    from werkzeug.wrappers import Request, Response
except ImportError:  # pragma: no cover
    sys.exit("Unable to import werkzeug (run: pip install werkzeug)")

try:
    import msgpack
except ImportError:  # pragma: no cover
    msgpack = None

import serverless_wsgi


def _require_msgpack():
    if msgpack is None:
        sys.exit(
            "msgpack is required for record/replay mode. "
            "Install with: pip install msgpack"
        )


def _query_string_hash(event):
    qs = event.get("rawQueryString")
    if qs is None:
        params = event.get("multiValueQueryStringParameters")
        if not params:
            params = event.get("queryStringParameters") or {}
        if isinstance(params, dict):
            items = []
            for k, v in sorted(params.items()):
                if isinstance(v, list):
                    for vi in sorted(v):
                        items.append((k, vi))
                else:
                    items.append((k, v))
            qs = urlencode(items, doseq=True)
        else:
            qs = str(params)
    h = hashlib.sha256(qs.encode("utf-8")).hexdigest()
    return h


def _event_from_environ(environ, request):
    headers = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            header_name = key[5:].replace("_", "-").title()
            headers[header_name] = value
    if "CONTENT_TYPE" in environ and environ["CONTENT_TYPE"]:
        headers["Content-Type"] = environ["CONTENT_TYPE"]

    body = request.get_data()
    is_base64 = False
    try:
        body_str = body.decode("utf-8")
    except UnicodeDecodeError:
        import base64
        body_str = base64.b64encode(body).decode("ascii")
        is_base64 = True

    event = {
        "version": "2.0",
        "rawPath": environ.get("PATH_INFO", "/"),
        "rawQueryString": environ.get("QUERY_STRING", ""),
        "headers": headers,
        "requestContext": {
            "http": {
                "method": environ.get("REQUEST_METHOD", "GET"),
                "path": environ.get("PATH_INFO", "/"),
                "sourceIp": environ.get("REMOTE_ADDR", "127.0.0.1"),
            },
            "stage": "offline",
        },
        "isBase64Encoded": is_base64,
        "body": body_str,
    }
    if request.cookies:
        event["cookies"] = [
            "{}={}".format(k, v) for k, v in request.cookies.items()
        ]
    return event


class RecordingMiddleware:
    def __init__(self, app, record_dir):
        self.app = app
        self.record_dir = record_dir
        os.makedirs(record_dir, exist_ok=True)
        self._counter = 0

    def __call__(self, environ, start_response):
        request = Request(environ)
        event = _event_from_environ(environ, request)
        context = {
            "function_name": "wsgi-offline",
            "recorded_at": time.time(),
        }

        response = Response.from_app(self.app, environ)
        response_data = {
            "status_code": response.status_code,
            "headers": [list(h) for h in response.headers],
            "data": response.get_data(),
            "mimetype": response.mimetype,
        }

        record = {
            "event": event,
            "context": context,
            "response": response_data,
            "timestamp": time.time(),
        }

        safe_ts = str(int(record["timestamp"] * 1e6))
        self._counter += 1
        filename = "req-{}-{}.msgpack".format(safe_ts, self._counter)
        filepath = os.path.join(self.record_dir, filename)

        _require_msgpack()
        with open(filepath, "wb") as f:
            f.write(msgpack.packb(record, use_bin_type=True))

        return response(environ, start_response)


def _load_recordings(replay_dir):
    _require_msgpack()
    records = []
    if not os.path.isdir(replay_dir):
        sys.exit("Replay directory does not exist: {}".format(replay_dir))
    for fname in sorted(os.listdir(replay_dir)):
        if not fname.endswith(".msgpack"):
            continue
        fpath = os.path.join(replay_dir, fname)
        try:
            with open(fpath, "rb") as f:
                data = msgpack.unpackb(f.read(), raw=False)
            records.append(data)
        except Exception as e:
            sys.stderr.write(
                "Warning: failed to load recording {}: {}\n".format(fname, e)
            )
    records.sort(key=lambda r: r.get("timestamp", 0))
    return records


def _response_from_recorded(recorded_response):
    response = Response(
        recorded_response.get("data", b""),
        status=recorded_response.get("status_code", 200),
        mimetype=recorded_response.get("mimetype"),
    )
    for h in recorded_response.get("headers", []):
        if len(h) >= 2:
            response.headers.add(h[0], h[1])
    return response


def run_replay(app, replay_dir):
    records = _load_recordings(replay_dir)
    if not records:
        sys.stderr.write("No recording files found in {}\n".format(replay_dir))
        return

    cache = {}
    for rec in records:
        event = rec.get("event", {})
        context = rec.get("context", {})
        qs_hash = _query_string_hash(event)

        if qs_hash in cache:
            sys.stdout.write(
                "[replay cache-hit qs={}] {}\n".format(
                    qs_hash[:12], event.get("rawPath", "/")
                )
            )
            recorded_resp = cache[qs_hash]
        else:
            sys.stdout.write(
                "[replay invoke qs={}] {}\n".format(
                    qs_hash[:12], event.get("rawPath", "/")
                )
            )
            try:
                ret = serverless_wsgi.handle_request(app, event, context)
            except Exception as e:
                sys.stderr.write(
                    "[replay error] {}\n{}\n".format(
                        event.get("rawPath", "/"), e
                    )
                )
                continue
            recorded_resp = rec.get("response")
            cache[qs_hash] = recorded_resp

        expected = rec.get("response", {})
        expected_status = expected.get("status_code")
        actual_status = recorded_resp.get("status_code")
        if expected_status and expected_status != actual_status:
            sys.stderr.write(
                "[replay status-mismatch] expected {} got {} for {}\n".format(
                    expected_status, actual_status, event.get("rawPath", "/")
                )
            )

    sys.stdout.write(
        "Replay complete: {} requests, {} cache hits\n".format(
            len(records), len(cache)
        )
    )


def parse_args():  # pragma: no cover
    parser = argparse.ArgumentParser(description="serverless-wsgi server")

    parser.add_argument("cwd", help="Set current working directory for server")
    parser.add_argument("app", help="Full import path to WSGI app")
    parser.add_argument(
        "port", type=int, nargs="?", default=5000, help="Port for server to listen on"
    )
    parser.add_argument(
        "host", nargs="?", default="localhost", help="Host/ip to bind the server to"
    )

    parser.add_argument("--disable-threading", action="store_false", dest="use_threads")
    parser.add_argument("--num-processes", type=int, dest="processes", default=1)

    parser.add_argument("--ssl", action="store_true", dest="ssl")
    parser.add_argument("--ssl-pub", dest="ssl_pub")
    parser.add_argument("--ssl-pri", dest="ssl_pri")

    parser.add_argument(
        "--record",
        dest="record_dir",
        default=None,
        help="Directory to record request/response as messagepack files",
    )
    parser.add_argument(
        "--replay",
        dest="replay_dir",
        default=None,
        help="Directory containing recorded messagepack files to replay",
    )

    return parser.parse_args()


def serve(
    cwd,
    app,
    port=5000,
    host="localhost",
    threaded=True,
    processes=1,
    ssl=False,
    ssl_keys=None,
    record_dir=None,
    replay_dir=None,
):
    sys.path.insert(0, cwd)

    os.environ["IS_OFFLINE"] = "True"

    wsgi_fqn = app.rsplit(".", 1)
    wsgi_fqn_parts = wsgi_fqn[0].rsplit("/", 1)
    if len(wsgi_fqn_parts) == 2:
        sys.path.insert(0, os.path.join(cwd, wsgi_fqn_parts[0]))
    wsgi_module = importlib.import_module(wsgi_fqn_parts[-1])
    wsgi_app = getattr(wsgi_module, wsgi_fqn[1])

    try:
        wsgi_app.debug = True
    except:  # noqa: E722
        pass

    if replay_dir:
        run_replay(wsgi_app, replay_dir)
        return

    effective_app = wsgi_app
    if record_dir:
        effective_app = RecordingMiddleware(wsgi_app, record_dir)

    if ssl:
        ssl_context = ssl_keys or "adhoc"
    else:
        ssl_context = None

    serving.run_simple(
        host,
        int(port),
        effective_app,
        use_debugger=True,
        use_reloader=True,
        use_evalex=True,
        threaded=threaded,
        processes=processes,
        ssl_context=ssl_context,
    )


def _validate_ssl_keys(cert_file, private_key_file):
    if not cert_file and not private_key_file:
        return None
    if not cert_file or not private_key_file:
        sys.exit("Missing either cert file or private key file (hint: --ssl-pub <file> and --ssl-pri <file>)")
    if not os.path.exists(cert_file):
        sys.exit("Cert file can't be found")
    if not os.path.exists(private_key_file):
        sys.exit("Private key file can't be found")
    return (cert_file, private_key_file)


if __name__ == "__main__":  # pragma: no cover
    args = parse_args()

    serve(
        cwd=args.cwd,
        app=args.app,
        port=args.port,
        host=args.host,
        threaded=args.use_threads,
        processes=args.processes,
        ssl=args.ssl or (bool(args.ssl_pub) and bool(args.ssl_pri)),
        ssl_keys=_validate_ssl_keys(args.ssl_pub, args.ssl_pri),
        record_dir=args.record_dir,
        replay_dir=args.replay_dir,
    )
