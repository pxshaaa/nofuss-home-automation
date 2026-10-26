"""One-time Spotify login for NoFuss Home (lets the app list your playlists).

Usage:  .venv/bin/python scripts/spotify_login.py <client-id>

1. Create an app at https://developer.spotify.com/dashboard (any name), tick "Web API",
   and add this Redirect URI exactly:  http://127.0.0.1:8766/callback
2. Run this script on the Mac where NoFuss Home runs; a browser opens, click "Agree".

Uses PKCE, so no client secret is needed. The login is stored in ~/.config/nofuss-home/spotify.json.
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

REDIRECT = "http://127.0.0.1:8766/callback"
SCOPES = ("playlist-read-private playlist-read-collaborative user-read-playback-state user-read-currently-playing "
          "user-modify-playback-state user-library-read user-library-modify")  # library-modify: the ♥ button
OUT = Path.home() / ".config" / "nofuss-home" / "spotify.json"


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    client_id = sys.argv[1].strip()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT, "scope": SCOPES,
        "code_challenge_method": "S256", "code_challenge": challenge, "state": state})

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] != state or "code" not in q:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Login failed - please run the script again.")
                return
            body = urllib.parse.urlencode({"grant_type": "authorization_code", "code": q["code"][0],
                                           "redirect_uri": REDIRECT, "client_id": client_id,
                                           "code_verifier": verifier}).encode()
            req = urllib.request.Request("https://accounts.spotify.com/api/token", data=body,
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            token = json.load(urllib.request.urlopen(req, timeout=10))
            OUT.parent.mkdir(parents=True, exist_ok=True)
            OUT.write_text(json.dumps({"client_id": client_id, "refresh_token": token["refresh_token"]}))
            os.chmod(OUT, 0o600)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>✅ Spotify connected — you can close this tab.</h2>".encode())
            print(f"Saved to {OUT}. Your playlists now appear in NoFuss Home → Music.")

        def log_message(self, *args):
            pass

    print("Opening Spotify in your browser… if nothing opens, visit:\n" + url)
    webbrowser.open(url)
    http.server.HTTPServer(("127.0.0.1", 8766), Callback).handle_request()


if __name__ == "__main__":
    main()
