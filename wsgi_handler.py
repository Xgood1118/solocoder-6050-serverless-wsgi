#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
This module loads the WSGI application specified by FQN in `.serverless-wsgi` and invokes
the request when the handler is called by AWS Lambda.

Author: Logan Raarup <logan@logan.dk>
"""
import importlib
import io
import json
import logging
import os
import signal
import sys
import threading
import traceback
from werkzeug.exceptions import InternalServerError

try:
    import unzip_requirements  # noqa
except ImportError:
    pass

import serverless_wsgi

DEFAULT_COMMAND_TIMEOUTS = {
    "exec": 30,
    "command": 120,
    "manage": 60,
    "flask": 60,
}

DEFAULT_MAX_OUTPUT_BYTES = 5 * 1024 * 1024
OUTPUT_TRUNCATION_MARKER = b"\n... [output truncated due to size limit]\n"
TIMEOUT_MARKER = "\n... [execution timed out, partial output above]\n"


class TruncatingStringIO(io.StringIO):
    def __init__(self, max_bytes):
        super().__init__()
        self.max_bytes = max_bytes
        self._byte_count = 0
        self._truncated = False

    def write(self, s):
        if self._truncated:
            return len(s) if isinstance(s, str) else 0
        data = s if isinstance(s, str) else s.decode("utf-8", errors="replace")
        encoded = data.encode("utf-8", errors="replace")
        remaining = self.max_bytes - self._byte_count
        if remaining <= 0:
            self._do_truncate()
            return len(data)
        if len(encoded) <= remaining:
            self._byte_count += len(encoded)
            return super().write(data)
        keep_chars = 0
        byte_accum = 0
        for ch in data:
            ch_bytes = ch.encode("utf-8", errors="replace")
            if byte_accum + len(ch_bytes) > remaining:
                break
            byte_accum += len(ch_bytes)
            keep_chars += 1
        if keep_chars > 0:
            super().write(data[:keep_chars])
            self._byte_count += byte_accum
        self._do_truncate()
        return len(data)

    def _do_truncate(self):
        if not self._truncated:
            self._truncated = True
            marker = OUTPUT_TRUNCATION_MARKER.decode("utf-8", errors="replace")
            super().write(marker)

    @property
    def truncated(self):
        return self._truncated


class CommandTimeout(Exception):
    pass


def _timeout_handler(signum, frame):
    raise CommandTimeout("Command exceeded allowed execution time")


def _install_timeout(seconds):
    if not seconds or seconds <= 0:
        return None
    if hasattr(signal, "SIGALRM"):
        try:
            old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(int(seconds))
            return ("sigalrm", old_handler)
        except (ValueError, OSError):
            pass
    if threading.current_thread() is threading.main_thread():
        timer = threading.Timer(seconds, lambda: _timeout_handler(None, None))
        timer.daemon = True
        timer.start()
        return ("timer", timer)
    return None


def _restore_timeout(handle):
    if not handle:
        return
    mode = handle[0]
    if mode == "sigalrm":
        try:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, handle[1])
        except (ValueError, OSError):
            pass
    elif mode == "timer":
        try:
            handle[1].cancel()
        except Exception:
            pass


def _resolve_timeout(command, meta):
    override = meta.get("timeout") or meta.get("timeoutSeconds")
    if override is not None:
        try:
            return float(override)
        except (TypeError, ValueError):
            pass
    return DEFAULT_COMMAND_TIMEOUTS.get(command, 60)


def _resolve_max_output(command, meta):
    override = meta.get("max-output") or meta.get("maxOutputBytes")
    if override is not None:
        try:
            return int(override)
        except (TypeError, ValueError):
            pass
    return DEFAULT_MAX_OUTPUT_BYTES


def load_config():
    root = os.path.abspath(os.path.dirname(__file__))
    with open(os.path.join(root, ".serverless-wsgi"), "r") as f:
        return json.loads(f.read())


def import_app(config):
    wsgi_fqn = config["app"].rsplit(".", 1)
    wsgi_fqn_parts = wsgi_fqn[0].rsplit("/", 1)

    if len(wsgi_fqn_parts) == 2:
        root = os.path.abspath(os.path.dirname(__file__))
        sys.path.insert(0, os.path.join(root, wsgi_fqn_parts[0]))

    try:
        wsgi_module = importlib.import_module(wsgi_fqn_parts[-1])

        return getattr(wsgi_module, wsgi_fqn[1])
    except Exception as err:
        logging.exception("Unable to import app: '{}' - {}".format(config["app"], err))
        return InternalServerError("Unable to import app: {}".format(config["app"]))


def append_text_mime_types(config):
    if "text_mime_types" in config and isinstance(config["text_mime_types"], list):
        serverless_wsgi.TEXT_MIME_TYPES.extend(config["text_mime_types"])


def handler(event, context):
    if "_serverless-wsgi" in event:
        import shlex
        import subprocess

        native_stdout = sys.stdout
        native_stderr = sys.stderr
        meta = event["_serverless-wsgi"]
        command = meta.get("command", "")
        timeout_seconds = _resolve_timeout(command, meta)
        max_output_bytes = _resolve_max_output(command, meta)
        output_buffer = TruncatingStringIO(max_output_bytes)
        timed_out = False
        exit_code = 0

        timeout_handle = _install_timeout(timeout_seconds)
        try:
            sys.stdout = output_buffer
            sys.stderr = output_buffer

            if command == "exec":
                exec(meta.get("data", ""))
            elif command == "command":
                result = subprocess.check_output(
                    meta.get("data", ""),
                    shell=True,
                    stderr=subprocess.STDOUT,
                    timeout=timeout_seconds if timeout_seconds else None,
                )
                output_buffer.write(result.decode("utf-8", errors="replace"))
            elif command == "manage":
                from django.core import management

                management.call_command(*shlex.split(meta.get("data", "")))
            elif command == "flask":
                from flask.cli import FlaskGroup

                flask_group = FlaskGroup(create_app=_create_app)
                flask_group.main(
                    shlex.split(meta.get("data", "")), standalone_mode=False
                )
            else:
                raise Exception("Unknown command: {}".format(command))
        except subprocess.TimeoutExpired as e:
            timed_out = True
            exit_code = -1
            if e.output:
                output_buffer.write(
                    e.output.decode("utf-8", errors="replace")
                )
            output_buffer.write(TIMEOUT_MARKER)
        except CommandTimeout:
            timed_out = True
            exit_code = -1
            output_buffer.write(TIMEOUT_MARKER)
        except subprocess.CalledProcessError as e:
            return [e.returncode, e.output.decode("utf-8", errors="replace")]
        except:  # noqa
            return [1, traceback.format_exc()]
        finally:
            _restore_timeout(timeout_handle)
            sys.stdout = native_stdout
            sys.stderr = native_stderr

        output_value = output_buffer.getvalue()
        if timed_out:
            return [exit_code, output_value, {"timedOut": True}]
        return [exit_code, output_value]
    else:
        return serverless_wsgi.handle_request(wsgi_app, event, context)


def _create_app():
    return wsgi_app


config = load_config()
wsgi_app = import_app(config)
append_text_mime_types(config)
