#!/usr/bin/env python3
"""Serve a filtered, cached Codex model catalog at /models.json."""

from __future__ import annotations

import argparse
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


CLIENT_VERSION = "0.162.0"
HEADER_COMMAND = [
    sys.executable,
    str(Path(__file__).resolve().with_name("codex_chatgpt.py")),
    "token",
]
EXCLUDED_MODELS = {"gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra"}
CACHE_TTL_SECONDS = 3600
REQUEST_TIMEOUT_SECONDS = 60

MODELS_URL = "https://chatgpt.com/backend-api/codex/models"
REMOVED_MODEL_KEYS = {
    "additional_speed_tiers",
    "service_tiers",
    "available_access_programs",
    "availability_nux",
    "available_in_plans"
}

_cached_body: bytes | None = None
_fetched_at = 0.0


def fetch_models() -> bytes:
    result = subprocess.run(
        HEADER_COMMAND,
        capture_output=True,
        text=True,
        check=True,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    headers = json.loads(result.stdout)
    if not isinstance(headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in headers.items()
    ):
        raise ValueError("The header command must return a JSON object of strings.")

    request = Request(
        MODELS_URL + "?" + urlencode({"client_version": CLIENT_VERSION}),
        headers={**headers, "Accept": "application/json"},
    )
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        catalog = json.load(response)

    if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list):
        raise ValueError("The model catalog must contain a models array.")

    filtered_models = []
    for model in catalog["models"]:
        if not isinstance(model, dict):
            raise ValueError("Each model must be a JSON object.")
        if model.get("visibility") == "hide" or model.get("slug") in EXCLUDED_MODELS:
            continue
        for key in REMOVED_MODEL_KEYS:
            model.pop(key, None)
        model["prefer_websockets"] = False
        filtered_models.append(model)

    catalog["models"] = filtered_models
    return json.dumps(catalog, ensure_ascii=False).encode("utf-8")


def models_body() -> bytes:
    global _cached_body, _fetched_at
    if _cached_body is not None and time.monotonic() - _fetched_at <= CACHE_TTL_SECONDS:
        return _cached_body

    body = fetch_models()
    _cached_body = body
    _fetched_at = time.monotonic()
    return body


class ModelsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if urlsplit(self.path).path != "/models.json":
            self.send_error(404)
            return

        try:
            body = models_body()
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, HTTPException):
            self.send_json(502, b'{"error":"Could not retrieve the model catalog."}')
            return

        self.send_json(200, body)

    def send_json(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bind", default="127.0.0.1", help="bind address (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port", type=int, default=8080, help="listen port (default: 8080)"
    )
    args = parser.parse_args()

    with HTTPServer((args.bind, args.port), ModelsHandler) as server:
        print(f"Serving http://{args.bind}:{server.server_port}/models.json", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
