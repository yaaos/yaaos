"""Pure helper: build the `$CODEX_HOME/auth.json` payload from a credential.

`build_auth_json` is called by the claim-time hydrator (phase 7) to mint the
JSON blob the Go agent writes to `<workspace>/.yaaos-codex-home/auth.json`.

The format matches the `chatgptAuthTokens` auth-mode that the Codex CLI reads:
  {
      "auth_mode": "chatgptAuthTokens",
      "tokens": {
          "access_token": "<JWT>",
          "refresh_token": "",
          "id_token":     "<JWT>",
          "account_id":   "<ChatGPT account id>"
      },
      "last_refresh": "<ISO-8601 UTC>"
  }

The refresh_token is always empty — the backend owns the refresh cycle; the
agent never refreshes tokens itself.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from pydantic import SecretStr

from app.core.oauth import UserOAuthCredential


def build_auth_json(cred: UserOAuthCredential) -> SecretStr:
    """Build the `auth.json` payload. Returns a `SecretStr` so the plaintext
    is redacted in logs and Pydantic model dumps."""
    payload = {
        "auth_mode": "chatgptAuthTokens",
        "tokens": {
            "access_token": cred.access_token.get_secret_value(),
            "refresh_token": "",
            "id_token": cred.id_token.get_secret_value() if cred.id_token is not None else "",
            "account_id": cred.external_account_id or "",
        },
        "last_refresh": datetime.now(UTC).isoformat(),
    }
    return SecretStr(json.dumps(payload))
