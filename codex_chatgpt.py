#!/usr/bin/env python3
"""Small CLI for Codex-managed ChatGPT auth and account quota."""

from __future__ import annotations

import argparse
import base64
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from typing import Any


APP_NAME = "codex-chatgpt"
APP_VERSION = "0.1.0"
RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
TOKEN_REFRESH_WINDOW = timedelta(minutes=5)
TOKEN_SCHEDULED_REFRESH_INTERVAL = timedelta(days=8)
APP_SERVER_TIMEOUT_SECONDS = 45


class ProxyError(Exception):
    """A user-facing error safe to print without exposing credentials."""


class AppServer:
    """Minimal JSON-RPC client for Codex app-server over stdio."""

    def __init__(self, codex_home: Path) -> None:
        codex = shutil.which("codex")
        if codex is None:
            raise ProxyError(
                "Could not find `codex` on PATH. Install Codex or add its executable to PATH."
            )

        env = os.environ.copy()
        env["CODEX_HOME"] = str(codex_home)
        try:
            self.process = subprocess.Popen(
                [codex, "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            raise ProxyError(f"Could not start Codex app-server: {exc}") from exc

        self._stdout_lines: queue.Queue[str | None] = queue.Queue()
        self._stderr_lines: queue.Queue[str] = queue.Queue()
        self._next_id = 1
        self._reader = threading.Thread(
            target=self._read_stdout, name="codex-app-server-stdout", daemon=True
        )
        self._stderr_reader = threading.Thread(
            target=self._read_stderr, name="codex-app-server-stderr", daemon=True
        )
        self._reader.start()
        self._stderr_reader.start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                self._stdout_lines.put(line)
        finally:
            self._stdout_lines.put(None)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            # Drain stderr so the child cannot block. Never echo it: diagnostics may
            # contain implementation details that should not reach the token output.
            self._stderr_lines.put(line)

    def _send(self, message: dict[str, Any]) -> None:
        if self.process.poll() is not None:
            raise ProxyError("Codex app-server exited unexpectedly. Check your Codex installation.")
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ProxyError("Could not communicate with Codex app-server.") from exc

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"id": request_id, "method": method, "params": params})

        deadline = time.monotonic() + APP_SERVER_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProxyError(f"Timed out waiting for Codex app-server method `{method}`.")
            try:
                line = self._stdout_lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise ProxyError(f"Timed out waiting for Codex app-server method `{method}`.") from exc
            if line is None:
                raise ProxyError("Codex app-server closed its output unexpectedly.")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict) or message.get("id") != request_id:
                # Ignore notifications and responses to any unrelated request.
                continue
            if "error" in message:
                error = message.get("error")
                if isinstance(error, dict):
                    detail = error.get("message")
                else:
                    detail = None
                raise ProxyError(
                    f"Codex app-server could not complete `{method}`"
                    + (f": {detail}" if isinstance(detail, str) else ".")
                )
            result = message.get("result")
            if not isinstance(result, dict):
                raise ProxyError(f"Codex app-server returned an invalid response for `{method}`.")
            return result

    def start(self) -> None:
        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": APP_NAME,
                    "title": "Codex ChatGPT",
                    "version": APP_VERSION,
                }
            },
        )
        self._send({"method": "initialized"})

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._request(method, params)

    def close(self) -> None:
        if self.process.poll() is None:
            if self.process.stdin is not None:
                try:
                    self.process.stdin.close()
                except OSError:
                    pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()

    def __enter__(self) -> AppServer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def codex_home_path() -> Path:
    value = os.environ.get("CODEX_HOME")
    if value:
        return Path(value).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def _decode_jwt_exp(access_token: str) -> datetime | None:
    """Read exp locally for refresh scheduling; this does not verify the JWT."""
    claims = _decode_jwt_claims(access_token)
    if claims is None:
        return None
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(exp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _decode_jwt_claims(access_token: str) -> dict[str, Any] | None:
    """Decode a JWT payload for local inspection; signature is not verified."""
    pieces = access_token.split(".")
    if len(pieces) != 3:
        return None
    payload = pieces[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.b64decode(
            payload.encode("ascii"), altchars=b"-_", validate=True
        )
        claims = json.loads(decoded)
        if not isinstance(claims, dict):
            return None
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError, OSError):
        return None
    return claims


def _claim_text(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if name == "scope" and value is None:
        value = claims.get("scp")
    if isinstance(value, str) and value:
        return _safe_claim_text(value)
    if name == "aud" and isinstance(value, list):
        audiences = [item for item in value if isinstance(item, str) and item]
        if audiences:
            return ", ".join(_safe_claim_text(item) for item in audiences)
    if name == "scope" and isinstance(value, list):
        scopes = [item for item in value if isinstance(item, str) and item]
        if scopes:
            return " ".join(_safe_claim_text(item) for item in scopes)
    return "unavailable"


def _safe_claim_text(value: str) -> str:
    """Keep unverified claim values from injecting terminal control characters."""
    return "".join(char if char.isprintable() else f"\\u{ord(char):04x}" for char in value)


def _claim_timestamp(claims: dict[str, Any], name: str) -> datetime | None:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _format_utc(value: datetime | None) -> str:
    if value is None:
        return "unavailable"
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _expiry_status(expires_at: datetime | None, now: datetime) -> tuple[str, str]:
    if expires_at is None:
        return "unavailable (no valid exp claim)", "unavailable"
    remaining = expires_at - now
    if remaining.total_seconds() <= 0:
        status = "expired"
        duration = -remaining
        relation = "ago"
    elif remaining <= TOKEN_REFRESH_WINDOW:
        status = "expiring soon"
        duration = remaining
        relation = "remaining"
    else:
        status = "valid"
        duration = remaining
        relation = "remaining"

    total_seconds = max(0, int(duration.total_seconds()))
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return status, f"{' '.join(parts)} {relation}"


def token_info_command() -> None:
    auth_path = codex_home_path() / "auth.json"
    auth_data = _read_auth_file(auth_path)
    token = _access_token(auth_data)
    if token is None:
        raise ProxyError(
            "No ChatGPT access token is present in the Codex file credential store. "
            "Sign in with `codex login`, and confirm Codex is using the file store."
        )

    claims = _decode_jwt_claims(token)
    if claims is None:
        raise ProxyError(
            "The stored access token is not a decodable JWT. Check the Codex login and "
            "credential-store setting."
        )

    now = datetime.now(timezone.utc)
    issued_at = _claim_timestamp(claims, "iat")
    expires_at = _claim_timestamp(claims, "exp")
    status, remaining = _expiry_status(expires_at, now)
    print("JWT access-token information (claims decoded locally; signature not verified)")
    print(f"Issuer: {_claim_text(claims, 'iss')}")
    print(f"Audience: {_claim_text(claims, 'aud')}")
    print(f"Scope: {_claim_text(claims, 'scope')}")
    print(f"Issued at: {_format_utc(issued_at)}")
    print(f"Expires at: {_format_utc(expires_at)}")
    print(f"Status: {status}")
    print(f"Time: {remaining}")
    print("Note: decoded claims do not verify the signature or guarantee service acceptance.")


def _parse_last_refresh(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_auth_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProxyError(
            f"No file credential store found at {path}. Set `cli_auth_credentials_store = \"file\"` "
            "in `$CODEX_HOME/config.toml`, then sign in with `codex login`."
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ProxyError(f"Could not read Codex credentials from {path}.") from exc
    if not isinstance(value, dict):
        raise ProxyError(f"Codex credential file at {path} has an unexpected format.")
    return value


def _access_token(auth_data: dict[str, Any]) -> str | None:
    tokens = auth_data.get("tokens")
    token = tokens.get("access_token") if isinstance(tokens, dict) else None
    return token if isinstance(token, str) and token else None


def _refresh_needed(auth_data: dict[str, Any], *, force: bool) -> bool:
    if force:
        return True
    token = _access_token(auth_data)
    if token is None:
        return True

    now = datetime.now(timezone.utc)
    expires_at = _decode_jwt_exp(token)
    if expires_at is not None:
        return expires_at <= now + TOKEN_REFRESH_WINDOW

    # Match Codex's fallback when a token has no readable exp claim. Refreshing
    # once after login also establishes last_refresh for older auth files.
    last_refresh = _parse_last_refresh(auth_data.get("last_refresh"))
    return last_refresh is None or last_refresh <= now - TOKEN_SCHEDULED_REFRESH_INTERVAL


@contextmanager
def _token_lock(home: Path) -> Iterator[None]:
    # Keep the lock file so all processes lock the same inode.
    lock_path = home / "codex-chatgpt.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise ProxyError(f"Could not open token lock at {lock_path}: {exc.strerror}.") from exc

    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            raise ProxyError(f"Could not acquire token lock at {lock_path}: {exc.strerror}.") from exc
        yield
    finally:
        os.close(fd)


def token_command(*, force_refresh: bool = False) -> None:
    home = codex_home_path()
    with _token_lock(home):
        _token_command_locked(home, force_refresh=force_refresh)


def _token_command_locked(home: Path, *, force_refresh: bool) -> None:
    auth_path = home / "auth.json"
    auth_data = _read_auth_file(auth_path)

    if _refresh_needed(auth_data, force=force_refresh):
        with AppServer(home) as app_server:
            app_server.start()
            app_server.call("account/read", {"refreshToken": True})
        # Codex's managed refresh rotates and persists credentials; always reread
        # after the server exits rather than using the pre-refresh copy.
        auth_data = _read_auth_file(auth_path)

    token = _access_token(auth_data)
    if token is None:
        raise ProxyError(
            "No ChatGPT access token is present in the Codex file credential store. "
            "Sign in with `codex login`, and confirm Codex is using the file store."
        )

    expires_at = _decode_jwt_exp(token)
    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        raise ProxyError(
            "Codex did not provide a currently valid access token. Check your network and "
            "Codex login, then retry with `token --refresh`."
        )

    headers = {"Authorization": f"Bearer {token}"}
    account_id = auth_data.get("tokens", {}).get("account_id")
    if isinstance(account_id, str) and account_id:
        headers["ChatGPT-Account-ID"] = account_id
    print(json.dumps(headers, separators=(",", ":")))


def _format_reset(timestamp: Any) -> str:
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return "reset time unavailable"
    try:
        local_time = datetime.fromtimestamp(timestamp).astimezone()
        return "resets " + local_time.strftime("%Y-%m-%d %H:%M:%S %Z")
    except (OverflowError, OSError, ValueError):
        return "reset time unavailable"


def _format_window(label: str, value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    used = value.get("usedPercent")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return [f"  {label}: usage unavailable"]
    duration = value.get("windowDurationMins")
    duration_text = f" ({duration} min window)" if isinstance(duration, int) else ""
    return [
        f"  {label}{duration_text}: {used:g}% used, {100 - used:g}% remaining; "
        f"{_format_reset(value.get('resetsAt'))}"
    ]


def _print_usage_human(data: dict[str, Any]) -> None:
    allowed = data.get("ordinaryUsageAllowed")
    allowed_text = {True: "yes", False: "no", None: "unavailable"}[allowed]
    print(f"Ordinary included usage allowed: {allowed_text}")

    buckets = data.get("rateLimitsByLimitId")
    if isinstance(buckets, dict) and buckets:
        named_buckets = buckets.items()
    else:
        snapshot = data.get("rateLimits")
        named_buckets = [("account", snapshot)] if isinstance(snapshot, dict) else []

    if not named_buckets:
        print("Rate-limit quota details are unavailable.")

    for key, bucket in named_buckets:
        if not isinstance(bucket, dict):
            continue
        name = bucket.get("limitName") or key
        print(f"\n{name} quota:")
        if bucket.get("planType"):
            print(f"  Plan: {bucket['planType']}")
        windows = _format_window("Primary", bucket.get("primary"))
        windows.extend(_format_window("Secondary", bucket.get("secondary")))
        if windows:
            print("\n".join(windows))
        else:
            print("  Rate-limit windows unavailable")

    reset_credits = data.get("rateLimitResetCredits")
    if isinstance(reset_credits, dict):
        count = reset_credits.get("availableCount")
        if isinstance(count, int) and not isinstance(count, bool):
            print(f"\nAvailable reset credits: {count}")


def usage_command(*, output_json: bool) -> None:
    home = codex_home_path()
    with AppServer(home) as app_server:
        app_server.start()
        result = app_server.call("account/rateLimits/read", {})

    if output_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_usage_human(result)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codex-chatgpt",
        description=(
            "Read Codex-managed ChatGPT auth and quota information. "
            f"Use the auth headers with {RESPONSES_URL}, not api.openai.com."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    token_parser = subparsers.add_parser(
        "token",
        help="print ChatGPT Codex Responses auth headers as JSON",
        description=(
            f"Print JSON auth headers for {RESPONSES_URL}. "
            "These ChatGPT credentials do not grant public OpenAI API access."
        ),
    )
    token_parser.add_argument(
        "--refresh",
        action="store_true",
        help="force Codex to refresh credentials before printing the header",
    )

    subparsers.add_parser(
        "token-info", help="show local metadata for the stored JWT access token"
    )

    usage_parser = subparsers.add_parser("usage", help="print remaining account quota")
    usage_parser.add_argument(
        "--json", action="store_true", help="print the raw quota response as JSON"
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "token":
            token_command(force_refresh=args.refresh)
        elif args.command == "token-info":
            token_info_command()
        elif args.command == "usage":
            usage_command(output_json=args.json)
        return 0
    except ProxyError as exc:
        print(f"{APP_NAME}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"{APP_NAME}: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
