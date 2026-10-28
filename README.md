# chatgpt-login-token

A small Python CLI that uses Codex-managed ChatGPT authentication to print auth headers for the ChatGPT Codex Responses endpoint or the current account quota.

This project is meant to be used with [openai-responses-proxy](https://github.com/jamestut/openai-responses-proxy).

Use these headers with `https://chatgpt.com/backend-api/codex/responses`. They do not grant access to `https://api.openai.com/v1/responses`, which requires a separate OpenAI API key. Refreshing a ChatGPT token does not add public API permissions.

## Requirements

- Python 3.11 or newer
- Codex CLI installed and available as `codex` on `PATH`
- A completed ChatGPT login in Codex
- The Codex file credential store enabled in `$CODEX_HOME/config.toml` (normally `~/.codex/config.toml`):

  ```toml
  cli_auth_credentials_store = "file"
  ```

The CLI uses the current `CODEX_HOME` environment variable, or `~/.codex` when it is unset. It never edits Codex configuration or prints credentials to stderr.

## Install

From this directory:

```sh
python -m pip install .
```

You can also run the script directly with Python:

```sh
python codex_chatgpt.py token
```

## Commands

### Print ChatGPT Codex Responses auth headers

```sh
codex-chatgpt token
```

Output is always JSON containing the header map:

```json
{"Authorization":"Bearer <access-token>","ChatGPT-Account-ID":"<account-id>"}
```

`ChatGPT-Account-ID` is included when present in the Codex credential store, matching Codex's account routing.

### Make a Responses request

After installation, this example reads the headers without displaying credentials and sends a small `gpt-6-luna` request with low reasoning:

```sh
python - <<'PY'
import json
import subprocess
import urllib.request

headers = json.loads(subprocess.check_output(["codex-chatgpt", "token"], text=True))
headers["Content-Type"] = "application/json"
payload = {
    "model": "gpt-6-luna",
    "reasoning": {"effort": "low"},
    "instructions": "Reply briefly.",
    "input": [{"role": "user", "content": [{"type": "input_text", "text": "Reply with exactly OK."}]}],
    "stream": True,
    "store": False,
}
request = urllib.request.Request(
    "https://chatgpt.com/backend-api/codex/responses",
    data=json.dumps(payload).encode(),
    headers=headers,
)
with urllib.request.urlopen(request, timeout=60) as response:
    for line in response:
        print(line.decode(), end="")
PY
```

The Codex backend returns server-sent events (SSE). Use an input item list, provide `instructions`, and set `stream: true` and `store: false` as shown. This backend follows Codex's request contract; it is not interchangeable with the public API.

### Refresh credentials

The command reads the access token and its JWT expiry locally. If it remains valid for more than five minutes, no app-server process or network request is needed. When refresh is due, it starts `codex app-server`, sends `account/read` with `refreshToken: true`, then rereads `$CODEX_HOME/auth.json`. The refresh threshold follows the current Codex implementation and can change with Codex versions. The file format is an internal Codex format.

To force a refresh:

```sh
codex-chatgpt token --refresh
```

Use this if the token was revoked before its encoded expiry. The command cannot detect revocation without making a request to the service.

### Inspect JWT access-token metadata

```sh
codex-chatgpt token-info
```

This command reads the stored access token locally and prints issuer, audience, scope (`scope` or `scp`), issue and expiry times, and an expiry status. It does not start app-server, refresh the token, or contact OpenAI. It decodes the JWT payload without verifying its signature, so the displayed claims do not prove that the token is authentic or will be accepted by the service. The token itself and identity claims are not printed.

### Print quota

```sh
codex-chatgpt usage
```

This queries Codex for a fresh quota snapshot and prints ordinary included usage status, primary and secondary windows, reset times, and available reset credits when supplied by the backend.

For the raw quota response as JSON:

```sh
codex-chatgpt usage --json
```

The quota endpoint is live data, so `usage` starts app-server and makes a network request on every invocation.

## Troubleshooting

- If a Responses request fails with HTTP 401 and `Missing scopes: api.responses.write`, check the URL. ChatGPT credentials belong on `https://chatgpt.com/backend-api/codex/responses`; use an OpenAI API key for the public API.
- If `codex` cannot be found, add the Codex CLI executable to `PATH`.
- If no file credential store exists, set `cli_auth_credentials_store = "file"` in `$CODEX_HOME/config.toml`, then complete sign-in with `codex login`.
- If refresh fails, check that the Codex login is still valid and that network access is available. A revoked or expired refresh token may require signing in again.
- If the Codex app-server protocol changes, update this CLI to match the Codex runtime installed on the machine.
