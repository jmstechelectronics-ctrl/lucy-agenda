#!/usr/bin/env python3
"""One-time OAuth setup for Lucy's TickTick widget."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests


APP_ID = "lucy-ticktick-widget"
REDIRECT_URI = "http://127.0.0.1:8765/callback"
CONFIG_DIR = Path.home() / ".config" / APP_ID


def main() -> int:
    if len(sys.argv) == 3:
        client_id, client_secret = sys.argv[1:]
    else:
        # The setup GUI supplies credentials over a private pipe so the secret
        # never appears in the process list or a configuration file.
        client_id = sys.stdin.readline().strip()
        client_secret = sys.stdin.readline().strip()
        if not client_id or not client_secret:
            print("Client ID and client secret are required", file=sys.stderr)
            return 2
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            if query.get("state", [""])[0] != state:
                self.send_response(400); self.end_headers(); self.wfile.write(b"Invalid state"); return
            result["code"] = query.get("code", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h2>TickTick widget authorized.</h2><p>You can close this tab.</p>")
        def log_message(self, *_args):
            pass

    params = urlencode({
        "client_id": client_id,
        "scope": "tasks:read tasks:write",
        "state": state,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    print("Opening TickTick authorization…")
    webbrowser.open("https://ticktick.com/oauth/authorize?" + params)
    server = HTTPServer(("127.0.0.1", 8765), Handler)
    server.timeout = 300
    while "code" not in result:
        server.handle_request()
    server.server_close()
    if not result.get("code"):
        print("Authorization failed", file=sys.stderr)
        return 1

    response = requests.post(
        "https://ticktick.com/oauth/token",
        auth=(client_id, client_secret),
        data={
            "code": result["code"],
            "grant_type": "authorization_code",
            "scope": "tasks:read tasks:write",
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        },
        timeout=30,
    )
    response.raise_for_status()
    token = response.json()["access_token"]
    stored = subprocess.run(
        ["secret-tool", "store", "--label=Lucy TickTick Widget", "application", APP_ID,
         "account", "access-token"],
        input=token, text=True, timeout=30, check=False,
    )
    if stored.returncode != 0:
        raise RuntimeError("Could not save the TickTick token in GNOME Keyring")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / "authorization.json").write_text(json.dumps({"authorized": True}, indent=2) + "\n")
    print("TickTick authorization saved securely in GNOME Keyring.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
