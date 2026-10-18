"""Music: the Sonos speakers (local network, via SoCo) playing Spotify playlists.

Spotify's Web API is only used to list your playlists; playback itself goes to Sonos directly,
which is always reachable (no Spotify Connect device has to be awake).
"""
import asyncio
import json
import logging
import os
import random
import time
import urllib.parse
import urllib.request
from pathlib import Path

import soco
from soco.plugins.sharelink import ShareLinkPlugin

log = logging.getLogger("home.music")
SPOTIFY_FILE = Path.home() / ".config/nofuss-home/spotify.json"


class Spotify:
    """Minimal Web API client (PKCE app, refresh tokens rotate and are saved back)."""

    def __init__(self):
        self.token, self.expires = None, 0
        self.playlists, self.playlists_at = [], 0

    def _access_token(self):
        if self.token and time.time() < self.expires - 60:
            return self.token
        cfg = json.loads(SPOTIFY_FILE.read_text())
        body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": cfg["refresh_token"],
                                       "client_id": cfg["client_id"]}).encode()
        req = urllib.request.Request("https://accounts.spotify.com/api/token", data=body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        tok = json.load(urllib.request.urlopen(req, timeout=10))
        if tok.get("refresh_token") and tok["refresh_token"] != cfg["refresh_token"]:
            cfg["refresh_token"] = tok["refresh_token"]
            SPOTIFY_FILE.write_text(json.dumps(cfg))
            os.chmod(SPOTIFY_FILE, 0o600)
        self.token, self.expires = tok["access_token"], time.time() + tok.get("expires_in", 3600)
        return self.token

    def get(self, path):
        req = urllib.request.Request(f"https://api.spotify.com/v1{path}",
                                     headers={"Authorization": f"Bearer {self._access_token()}"})
        return json.load(urllib.request.urlopen(req, timeout=10))

    def my_playlists(self):
        if self.playlists and time.time() - self.playlists_at < 600:
            return self.playlists
        out, path = [], "/me/playlists?limit=50"
        while path:
            page = self.get(path)
            for p in page["items"]:
                if not p:
                    continue
                imgs = p.get("images") or []
                out.append({"uri": p["uri"], "name": p["name"], "tracks": (p.get("items") or p.get("tracks") or {}).get("total"),
                            "image": imgs[-1]["url"] if imgs else None})
            nxt = page.get("next")
            path = nxt.replace("https://api.spotify.com/v1", "") if nxt else None
        self.playlists, self.playlists_at = out, time.time()
        return out


class Music:
    def __init__(self, cfg, history):
        self.cfg = cfg  # {"ip": ..., "name": ...}
        self.history = history
        self.spotify = Spotify()
        self.state = {"state": None, "title": None, "artist": None, "album": None, "art": None,
                      "volume": None, "playlist": cfg.get("last_playlist")}
        self.error = None
        self.lock = asyncio.Lock()
        self.on_config_change = lambda: None  # set by main() to persist cfg
        self._coord, self._coord_at = None, 0
        self._miss_at = 0

    def _zone(self):
        # group topology costs ~100 ms per lookup; reuse it for a minute so commands go out immediately
        if self._coord is not None and time.time() - self._coord_at < 60:
            return self._coord
        self._coord, self._coord_at = self._lookup(), time.time()
        return self._coord

    def _lookup(self):
        if not self.cfg.get("ip"):  # nothing configured: take the first Sonos on the network
            if time.time() - self._miss_at < 60:
                raise RuntimeError("no Sonos found")
            found = sorted(soco.discover(timeout=5) or [], key=lambda z: z.player_name)
            if not found:
                self._miss_at = time.time()
                raise RuntimeError("no Sonos found")
            self.cfg.update({"ip": found[0].ip_address, "name": found[0].group.coordinator.player_name})
            log.info("found Sonos %s at %s", self.cfg["name"], self.cfg["ip"])
            self.on_config_change()
        z = soco.SoCo(self.cfg["ip"])
        try:
            return z.group.coordinator
        except Exception:  # noqa: BLE001 — IP changed? find the speaker again by name
            for found in soco.discover(timeout=5) or []:
                if found.player_name == self.cfg.get("name"):
                    self.cfg["ip"] = found.ip_address
                    return found.group.coordinator
            raise

    def _read(self):
        z = self._zone()
        tr = z.get_current_track_info()
        return {"state": z.get_current_transport_info()["current_transport_state"],
                "title": tr.get("title") or None, "artist": tr.get("artist") or None,
                "album": tr.get("album") or None, "art": tr.get("album_art") or None,
                "volume": z.group.volume}

    async def refresh(self):
        try:
            self.state.update(await asyncio.to_thread(self._read))
            self.error = None
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self._coord = None

    def _do(self, action, uri=None, volume=None, shuffle=False):
        z = self._zone()
        if volume is not None:
            z.group.volume = int(volume)
        if action == "playlist":
            z.clear_queue()
            ShareLinkPlugin(z).add_share_link_to_queue(uri)
            z.play_mode = "SHUFFLE_NOREPEAT" if shuffle else "NORMAL"
            # random: start at a random song (Sonos shuffle alone would always begin with song 1)
            z.play_from_queue(random.randrange(max(1, z.queue_size)) if shuffle else 0)
        elif action == "play":
            # after AirPlay/TV the speaker's source is that stream; resuming it plays silence
            if z.get_current_media_info().get("uri", "").startswith("x-rincon-queue:"):
                z.play()
            elif z.queue_size:
                z.play_from_queue(0)
            elif self.cfg.get("last_playlist"):
                self._do("playlist", self.cfg["last_playlist"]["uri"], None, shuffle)
            else:
                z.play()
        elif action == "pause":
            z.pause()
        elif action == "next":
            z.next()
        elif action == "previous":
            z.previous()
        elif action in ("vol_up", "vol_down"):
            g = z.group
            g.volume = max(0, min(100, g.volume + (4 if action == "vol_up" else -4)))

    async def command(self, action, uri=None, volume=None, shuffle=False, reason="manual"):
        async with self.lock:
            try:
                await asyncio.to_thread(self._do, action, uri, volume, shuffle)
            except Exception as exc:  # noqa: BLE001
                self._coord = None
                # "previous" at the first track / "pause" when stopped are harmless Sonos errors
                if action not in ("previous", "pause", "next"):
                    self.history.add(f"FAILED music {action} · {exc}")
                    return False
            if action == "playlist":
                name = next((p["name"] for p in self.spotify.playlists if p["uri"] == uri), "playlist")
                self.state["playlist"] = self.cfg["last_playlist"] = {"uri": uri, "name": name}
                self.on_config_change()
                self.history.add(f"Music: {name}" + ("" if reason == "manual" else f" · {reason}"))
            elif action in ("play", "pause") and reason != "manual":
                self.history.add(f"Music {action} · {reason}")
            # answer right away; the real state follows in the background
            if action in ("play", "playlist"):
                self.state["state"] = "PLAYING"
            elif action == "pause":
                self.state["state"] = "PAUSED_PLAYBACK"
            if volume is not None:
                self.state["volume"] = int(volume)
            asyncio.get_running_loop().call_later(0.6, lambda: asyncio.ensure_future(self.refresh()))
            return True

    async def playlists(self):
        return await asyncio.to_thread(self.spotify.my_playlists)

    async def art(self):
        """Album art bytes via the mini, so it also loads when away from home."""
        url = self.state.get("art")
        if not url:
            return None, None
        def fetch():
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.read(), r.headers.get("Content-Type", "image/jpeg")
        return await asyncio.to_thread(fetch)

    def status(self):
        return {**self.state, "error": self.error, "speaker": self.cfg.get("name")}
