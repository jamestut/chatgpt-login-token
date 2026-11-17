#!/usr/bin/env python3
"""Serve filtered and raw Codex model catalogs."""

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


CLIENT_VERSION = "0.169.1"
HEADER_COMMAND = [
    sys.executable,
    str(Path(__file__).resolve().with_name("codex_chatgpt.py")),
    "token",
]
ALLOWED_MODELS = {"gpt-6.2-sol", "gpt-6.1-terra", "gpt-6-luna"}
CACHE_TTL_SECONDS = 3600
REQUEST_TIMEOUT_SECONDS = 60
CONTEXT_WINDOW_SIZE = 272000

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
_settings_path: Path | None = None


def load_settings() -> tuple[set[str], str]:
    if _settings_path is None:
        return ALLOWED_MODELS, CLIENT_VERSION

    settings = json.loads(_settings_path.read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise ValueError("Model catalog settings must be a JSON object.")
    allowed_models = settings.get("allowed_models", list(ALLOWED_MODELS))
    client_version = settings.get("client_version", CLIENT_VERSION)
    if not isinstance(allowed_models, list) or not all(
        isinstance(model, str) for model in allowed_models
    ):
        raise ValueError("allowed_models must be an array of strings.")
    if not isinstance(client_version, str):
        raise ValueError("client_version must be a string.")
    return set(allowed_models), client_version


def fetch_raw_models(client_version: str | None = None) -> bytes:
    if client_version is None:
        _, client_version = load_settings()
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
        MODELS_URL + "?" + urlencode({"client_version": client_version}),
        headers={**headers, "Accept": "application/json"},
    )
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return response.read()


def fetch_models() -> bytes:
    # Use one settings snapshot for the upstream request and its filtering.
    allowed_models, client_version = load_settings()
    catalog = json.loads(fetch_raw_models(client_version))

    if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list):
        raise ValueError("The model catalog must contain a models array.")

    filtered_models = []
    for model in catalog["models"]:
        if not isinstance(model, dict):
            raise ValueError("Each model must be a JSON object.")
        if model.get("visibility") == "hide" or model.get("slug") not in allowed_models:
            continue
        for key in REMOVED_MODEL_KEYS:
            model.pop(key, None)
        model["prefer_websockets"] = False
        model["context_window"] = CONTEXT_WINDOW_SIZE
        # Identify the selected model in its instructions before caching the catalog.
        messages = model["model_messages"]
        messages["instructions_template"] = (
            f'<model_id>{model["slug"]}</model_id>\n'
            f'<model_name>{model["display_name"]}</model_name>\n\n\n'
            + messages["instructions_template"]
        )
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


MODEL_ROUTES = {
    "/models.json": models_body,
    "/rawmodels.json": fetch_raw_models,
}


class ModelsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        fetch_body = MODEL_ROUTES.get(urlsplit(self.path).path)
        if fetch_body is None:
            self.send_error(404)
            return

        try:
            body = fetch_body()
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
    global _settings_path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-b", "--bind", default="127.0.0.1", help="bind address (default: 127.0.0.1)"
    )
    parser.add_argument(
        "-p", "--port", type=int, default=8080, help="listen port (default: 8080)"
    )
    parser.add_argument(
        "-s", "--model-catalog-settings", type=Path, help="path to model catalog settings JSON"
    )
    args = parser.parse_args()
    _settings_path = args.model_catalog_settings

    with HTTPServer((args.bind, args.port), ModelsHandler) as server:
        print(f"Serving http://{args.bind}:{server.server_port}/models.json", flush=True)
        print(f"Serving http://{args.bind}:{server.server_port}/rawmodels.json", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
