"""
Auto-deploy webhook listener for the VM-hosted relay (and, later, the
static overlay/dashboard pages once self-hosted alongside it).

GitHub POSTs here on every push to main; this verifies the request
genuinely came from GitHub (HMAC signature against a shared secret --
without this, anyone who found the URL could trigger a deploy or run
arbitrary git operations), then runs `git pull` in the repo and
restarts relay.service so the new code actually takes effect. Stdlib
only (http.server + hmac), no extra dependency to install alongside
websockets.

Runs on localhost only (see the systemd service) -- reached from the
internet through Caddy's reverse proxy on a path under the existing
titanium-relay.duckdns.org domain, not exposed on its own port
directly. Configure the matching webhook in GitHub: repo Settings ->
Webhooks -> Add webhook, Payload URL
https://titanium-relay.duckdns.org/deploy-webhook, content type
application/json, secret = the same DEPLOY_SECRET set below, trigger
on "push" events only.
"""

import hashlib
import hmac
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("WEBHOOK_PORT", "9001"))
SECRET = os.environ.get("DEPLOY_SECRET", "").encode()
REPO_DIR = os.environ.get("REPO_DIR", "/home/ubuntu/MOBA-OCR-Overlay")


def verify_signature(payload_body, signature_header):
    if not SECRET:
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(SECRET, payload_body, hashlib.sha256).hexdigest()
    got = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, got)


def run(cmd, cwd=None):
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=60,
    )
    return result.returncode, result.stdout, result.stderr


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[deploy-webhook] {self.address_string()} - {fmt % args}")

    def do_POST(self):
        if self.path != "/deploy-webhook":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        if not verify_signature(body, self.headers.get("X-Hub-Signature-256")):
            print("[deploy-webhook] rejected: bad or missing signature")
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"invalid signature")
            return

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
        ref = payload.get("ref", "")
        if ref not in ("refs/heads/main", ""):
            # Push to a branch other than main -- accept the webhook
            # (so GitHub doesn't flag it as failing) but don't deploy.
            print(f"[deploy-webhook] ignoring push to {ref}")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ignored (not main)")
            return

        print("[deploy-webhook] valid push to main -- pulling and restarting")
        code, out, err = run(["git", "pull", "--ff-only", "origin", "main"], cwd=REPO_DIR)
        print(f"[deploy-webhook] git pull exit={code}\n{out}\n{err}")
        if code != 0:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"git pull failed: {err}".encode())
            return

        code, out, err = run(["sudo", "systemctl", "restart", "relay"])
        print(f"[deploy-webhook] restart relay exit={code}\n{out}\n{err}")

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"deployed")


def main():
    if not SECRET:
        print("WARNING: DEPLOY_SECRET is not set -- every request will be rejected.")
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"deploy-webhook listening on 127.0.0.1:{PORT}, repo={REPO_DIR}")
    server.serve_forever()


if __name__ == "__main__":
    main()
