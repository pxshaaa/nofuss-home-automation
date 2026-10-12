"""Web Push: lock-screen notifications for the home-screen web app (iOS 16.4+, Android, desktop).

Needs the app to be opened over HTTPS (e.g. `tailscale serve`) and added to the home screen.
VAPID keys are created on first start and kept in ~/.config/nofuss-home/.
"""
import asyncio
import base64
import json
import logging
from pathlib import Path

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

log = logging.getLogger("home.push")
CLAIMS = {"sub": "https://github.com"}  # contact for push services (required; bare https origin or mailto:)


class Push:
    def __init__(self, data_dir: Path, config_dir: Path):
        self.key_file = config_dir / "vapid_private.pem"
        self.subs_file = data_dir / "push.json"
        config_dir.mkdir(parents=True, exist_ok=True)
        if not self.key_file.exists():
            v = Vapid()
            v.generate_keys()
            v.save_key(str(self.key_file))
            self.key_file.chmod(0o600)
        self.vapid = Vapid.from_file(str(self.key_file))
        try:
            self.subs = json.loads(self.subs_file.read_text())
        except (FileNotFoundError, ValueError):
            self.subs = []

    def public_key(self):
        raw = self.vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def _save(self):
        self.subs_file.write_text(json.dumps(self.subs))

    def subscribe(self, sub):
        if not sub.get("endpoint") or not sub.get("keys"):
            raise ValueError("bad subscription")
        self.subs = [s for s in self.subs if s["endpoint"] != sub["endpoint"]] + [
            {"endpoint": sub["endpoint"], "keys": sub["keys"]}]
        self._save()

    def unsubscribe(self, endpoint):
        self.subs = [s for s in self.subs if s["endpoint"] != endpoint]
        self._save()

    def _send_one(self, sub, payload):
        try:
            webpush(subscription_info=sub, data=payload, vapid_private_key=str(self.key_file),
                    vapid_claims=dict(CLAIMS), ttl=3600, timeout=10)
            return True
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):  # the phone unsubscribed / app removed: forget it
                return None
            log.warning("push failed (%s): %s", status, exc)
            return False

    async def send(self, title, body="", url="/", tag=None):
        if not self.subs:
            return 0
        payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
        results = await asyncio.gather(*(asyncio.to_thread(self._send_one, s, payload) for s in self.subs))
        gone = [s for s, r in zip(self.subs, results) if r is None]
        if gone:
            self.subs = [s for s in self.subs if s not in gone]
            self._save()
        return sum(1 for r in results if r)
