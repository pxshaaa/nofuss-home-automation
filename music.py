"""Music: the Sonos speakers (local network, via SoCo) playing Spotify playlists.

Spotify's Web API is only used to list your playlists; playback itself goes to Sonos directly,
which is always reachable (no Spotify Connect device has to be awake).
"""
import asyncio
import json
import logging
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import soco
from soco.plugins.sharelink import ShareLinkPlugin

log = logging.getLogger("home.music")
SPOTIFY_FILE = Path.home() / ".config/nofuss-home/spotify.json"
LIKED = "spotify:liked"  # pseudo URI for Liked Songs


class Spotify:
    """Minimal Web API client (PKCE app, refresh tokens rotate and are saved back)."""

    def __init__(self):
        self.token, self.expires = None, 0
        self.playlists, self.playlists_at = [], 0
        self.track_cache = {}

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
        # Liked Songs isn't a playlist in the API; it gets its own pseudo URI (played track by track)
        try:
            out.insert(0, {"uri": LIKED, "name": "Liked Songs", "tracks": self.get("/me/tracks?limit=1")["total"],
                           "image": None})
        except Exception as exc:  # noqa: BLE001
            log.warning("liked songs unavailable: %s", exc)
        self.playlists, self.playlists_at = out, time.time()
        return out

    def _pages(self, path):
        while path:
            page = self.get(path)
            yield from page["items"]
            nxt = page.get("next")
            path = nxt.replace("https://api.spotify.com/v1", "") if nxt else None

    def tracks(self, uri):
        """Songs of a playlist (or Liked Songs): [{uri, name, artist, image}], cached for 10 minutes."""
        hit = self.track_cache.get(uri)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        out = []
        if uri == LIKED:
            rows = (r.get("track") for r in self._pages("/me/tracks?limit=50"))
        else:
            pid = uri.split(":")[-1]
            try:
                rows = [r.get("item") or r.get("track") for r in self._pages(f"/playlists/{pid}/items?limit=50")]
            except urllib.error.HTTPError as exc:
                if exc.code != 403:
                    raise
                rows = None  # Spotify only shows a dev app the songs of playlists you own
        if rows is None:
            out = self._embed_tracks(uri)
        else:
            for t in rows:
                if not t or not str(t.get("uri", "")).startswith("spotify:track:"):
                    continue  # local files, removed songs, podcast episodes
                imgs = (t.get("album") or {}).get("images") or []
                out.append({"uri": t["uri"], "name": t["name"],
                            "artist": ", ".join(a["name"] for a in t.get("artists") or []),
                            "image": imgs[-1]["url"] if imgs else None})
        self.track_cache[uri] = (time.time(), out)
        return out

    @staticmethod
    def _embed_tracks(uri):
        """Followed playlists: read the song list from Spotify's public embed player instead."""
        req = urllib.request.Request(f"https://open.spotify.com/embed/playlist/{uri.split(':')[-1]}",
                                     headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=10).read().decode()
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
        return [{"uri": t["uri"], "name": t.get("title") or "", "artist": t.get("subtitle") or "", "image": None}
                for t in entity.get("trackList") or [] if str(t.get("uri", "")).startswith("spotify:track:")]


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
        self._gen = 0

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
        song = re.search(r"spotify%3atrack%3a(\w+)", tr.get("uri") or "")
        return {"state": z.get_current_transport_info()["current_transport_state"],
                "title": tr.get("title") or None, "artist": tr.get("artist") or None,
                "album": tr.get("album") or None, "art": tr.get("album_art") or None,
                "track": "spotify:track:" + song.group(1) if song else None,
                "volume": z.group.volume}

    async def refresh(self):
        try:
            self.state.update(await asyncio.to_thread(self._read))
            self.error = None
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self._coord = None

    def _queue_tracks(self, z, uris):
        """Queue single songs (Liked Songs has no share link). Sonos takes ~3 s per 16 songs, so the first 16
        are queued here and the rest in the background, stopped if another playlist starts meanwhile.
        The first song goes via ShareLink (it knows which Spotify service the Sonos uses); the others reuse
        its stream URI, read back from the queue."""
        ShareLinkPlugin(z).add_share_link_to_queue(uris[0])
        first = z.get_queue(0, 1)[0].resources[0].uri  # x-sonos-spotify:spotify%3atrack%3a<id>?sid=9&flags=…&sn=…
        prefix, query = first.split("spotify%3atrack%3a")[0], "?" + first.split("?", 1)[1]
        sn = int(re.search(r"sid=(\d+)", query).group(1)) * 256 + 7  # service type, e.g. sid 9 -> 2311
        meta = ('<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
                'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
                '<item id="00032020{enc}" parentID="-1" restricted="true"><dc:title></dc:title>'
                '<upnp:class>object.item.audioItem.musicTrack</upnp:class><desc id="cdudn" '
                'nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">SA_RINCON{sn}_X_#Svc{sn}-0-Token</desc>'
                '</item></DIDL-Lite>')
        rest = [u.replace(":", "%3a") for u in uris[1:]]

        def add(chunk):
            z.avTransport.AddMultipleURIsToQueue([
                ("InstanceID", 0), ("UpdateID", 0), ("NumberOfURIs", len(chunk)),
                ("EnqueuedURIs", " ".join(prefix + e + query for e in chunk)),
                ("EnqueuedURIsMetaData", " ".join(meta.format(enc=e, sn=sn) for e in chunk)),
                ("ContainerURI", ""), ("ContainerMetaData", ""),
                ("DesiredFirstTrackNumberEnqueued", 0), ("EnqueueAsNext", 0)])

        if rest[:15]:
            add(rest[:15])
        gen = self._gen

        def background():
            try:
                for i in range(15, len(rest), 16):
                    if self._gen != gen:
                        return
                    add(rest[i:i + 16])
            except Exception as exc:  # noqa: BLE001
                log.warning("queueing liked songs stopped: %s", exc)
        threading.Thread(target=background, daemon=True).start()

    @staticmethod
    def _queue_index(z, track_uri):
        """Position of a song in the queue (Sonos may drop unavailable songs, so look it up)."""
        needle = track_uri.replace(":", "%3a")
        start = 0
        while True:
            items = z.get_queue(start, 100)
            for i, item in enumerate(items):
                if any(needle in (r.uri or "") for r in item.resources) or needle in (item.item_id or ""):
                    return start + i
            if len(items) < 100:
                return None
            start += 100

    def _do(self, action, uri=None, volume=None, shuffle=False, start=None):
        z = self._zone()
        if volume is not None:
            z.group.volume = int(volume)
        if action == "playlist":
            self._gen += 1  # stops a Liked Songs queue still filling in the background
            z.clear_queue()
            z.play_mode = "SHUFFLE_NOREPEAT" if shuffle else "NORMAL"
            if uri == LIKED:
                # queue it starting at the chosen (or a random) song, wrapping around, so playback starts at once
                songs = [t["uri"] for t in self.spotify.tracks(LIKED)]
                i = songs.index(start) if start in songs else random.randrange(len(songs)) if shuffle else 0
                self._queue_tracks(z, songs[i:] + songs[:i])
                z.play_from_queue(0)
                return
            ShareLinkPlugin(z).add_share_link_to_queue(uri)
            at = self._queue_index(z, start) if start else None
            if at is None:
                # random: start at a random song (Sonos shuffle alone would always begin with song 1)
                at = random.randrange(max(1, z.queue_size)) if shuffle else 0
            z.play_from_queue(at)
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

    async def command(self, action, uri=None, volume=None, shuffle=False, reason="manual", start=None):
        async with self.lock:
            try:
                await asyncio.to_thread(self._do, action, uri, volume, shuffle, start)
            except Exception as exc:  # noqa: BLE001
                self._coord = None
                # "previous" at the first track / "pause" when stopped are harmless Sonos errors
                if action not in ("previous", "pause", "next"):
                    self.history.add(f"FAILED music {action} · {exc}")
                    return False
            if action == "playlist":
                name = next((p["name"] for p in self.spotify.playlists if p["uri"] == uri),
                            "Liked Songs" if uri == LIKED else "playlist")
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

    async def tracks(self, uri):
        return await asyncio.to_thread(self.spotify.tracks, uri)

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
