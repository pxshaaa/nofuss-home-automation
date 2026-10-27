"""NoFuss Home: a tiny self-hosted controller for SwitchBot curtains & meters, Govee lamps and Sonos/Spotify.

Runs on an always-on Mac near your devices. Web UI on http://<this-mac>.local:8765
Curtain position: 0 = closed, 100 = open.
"""
import asyncio
import datetime as dt
import faulthandler
import json
import logging
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from astral import LocationInfo
from astral.sun import sun
from bleak import BleakScanner
from switchbot import SwitchbotCurtain, parse_advertisement_data
from switchbot.devices.smart_thermostat_radiator import SwitchbotSmartThermostatRadiator

from music import Music
from push import Push

BASE = Path(__file__).parent
DATA = BASE / "data"                                  # your settings, automations and history (git-ignored)
CONFIG_DIR = Path.home() / ".config" / "nofuss-home"  # secrets: govee.key, spotify.json
SETTINGS_FILE = DATA / "settings.json"
STATE_FILE = DATA / "state.json"
PORT = int(os.environ.get("NOFUSS_PORT", 8765))
MULTIZONE_LAMPS = {"H60B0"}  # Govee lamps whose top/middle/bottom lights switch separately (cloud API only)
GRACE = dt.timedelta(minutes=15)  # late automations still fire within this window (covers restarts)

def local_timezone():
    """IANA name of this Mac's time zone (e.g. "Europe/Berlin")."""
    try:
        return os.path.realpath("/etc/localtime").split("zoneinfo/")[1]
    except (IndexError, OSError):
        return "UTC"


DEFAULTS = {
    "address": "",            # SwitchBot curtain; empty = use the first one found
    "timezone": local_timezone(),
    "lat": None,              # your location, only for sunrise/sunset automations (setup.sh can fill it in)
    "lon": None,
    # lamp backend: "none" until found, "lan" (Govee LAN control) or "cloud" (Govee API key only)
    "lamp": {"backend": "none", "ip": "", "sku": "", "device": ""},
    "automations": [],
    "music": {"ip": "", "name": ""},  # Sonos; empty = use the first one found
    "lamp_presets": [
        {"id": "cozy", "name": "Cozy", "icon": "🛋️", "brightness": 45, "rgb": [255, 120, 40],
         "zones": {"top": True, "middle": True, "bottom": False}},
        {"id": "reading", "name": "Reading", "icon": "📖", "brightness": 90, "kelvin": 4000,
         "zones": {"top": False, "middle": False, "bottom": True}},
        {"id": "intimate", "name": "Intimate", "icon": "🕯️", "brightness": 20, "rgb": [200, 30, 70],
         "zones": {"top": True, "middle": False, "bottom": False}},
    ],
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("home")


def load(path, default):
    try:
        return {**default, **json.loads(path.read_text())}
    except (FileNotFoundError, json.JSONDecodeError):
        return json.loads(json.dumps(default))


def save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


class History:
    def __init__(self):
        self.items = []  # newest first

    def add(self, text):
        self.items.insert(0, {"at": dt.datetime.now().strftime("%d.%m. %H:%M"), "text": text})
        del self.items[30:]
        log.info(text)


class Curtain:
    """Keeps one SwitchbotCurtain instance so the BLE connection is reused between quick commands."""

    def __init__(self, address, history):
        self.address = address
        self.history = history
        self.on_found = lambda address: None  # set by main() to remember an auto-discovered curtain
        self.dev = None
        self.adv = None
        self.seen_at = None
        self.lock = asyncio.Lock()
        self.target = None  # latest requested position while a move is in flight

    def on_adv(self, device, adv_data):
        if self.address and device.address != self.address:
            return
        parsed = parse_advertisement_data(device, adv_data)
        if not parsed:
            return
        if not self.address:  # no curtain configured yet: adopt the first one we hear
            if not str(parsed.data.get("modelFriendlyName", "")).startswith("Curtain"):
                return
            self.address = device.address
            log.info("found curtain %s", device.address)
            self.on_found(device.address)
        self.adv, self.seen_at = parsed, dt.datetime.now()
        if self.dev is None:
            self.dev = SwitchbotCurtain(device=device)
        self.dev.update_from_advertisement(parsed)

    async def _device(self):
        if not self.address:
            raise RuntimeError("no curtain found yet")
        if self.dev is None:
            ble = await BleakScanner.find_device_by_address(self.address, timeout=15)
            if ble is None:
                raise RuntimeError("curtain not found")
            self.dev = SwitchbotCurtain(device=ble)
        return self.dev

    async def command(self, action, position=None, reason="manual"):
        if action == "position":
            self.target = int(position)
        async with self.lock:
            if action == "position":
                if self.target is None:  # a newer request already ran
                    return True
                position, self.target = self.target, None
            for attempt in range(3):
                try:
                    dev = await self._device()
                    if action == "open":
                        ok = await dev.open()
                    elif action == "close":
                        ok = await dev.close()
                    elif action == "stop":
                        ok = await dev.stop()
                    else:
                        ok = await dev.set_position(position)
                    if ok:
                        label = {"open": "Curtain opened", "close": "Curtain closed", "stop": "Curtain stopped"}.get(
                            action, f"Curtain set to {position}%")
                        self.history.add(f"{label}" + ("" if reason == "manual" else f" · {reason}"))
                        return True
                    raise RuntimeError("curtain rejected command")
                except Exception as exc:  # noqa: BLE001
                    log.warning("curtain attempt %s failed: %s", attempt + 1, exc)
                    self.dev = None
                    await asyncio.sleep(2)
            self.history.add(f"FAILED curtain {action}" + ("" if reason == "manual" else f" · {reason}"))
            return False

    def status(self):
        data = self.adv.data["data"] if self.adv else {}
        return {
            "position": data.get("position"),
            "battery": data.get("battery"),
            "moving": bool(data.get("inMotion")),
            "seen": self.seen_at.isoformat() if self.seen_at else None,
            "busy": self.lock.locked(),
        }


_govee_key = {"value": None, "at": 0.0}


def govee_key():
    """Cached for a minute: it's checked on every status poll, so don't hit the disk each time."""
    if _govee_key["value"] is None or time.monotonic() - _govee_key["at"] > 60:
        _govee_key["value"] = (os.environ.get("GOVEE_API_KEY") or _read(CONFIG_DIR / "govee.key")
                               or keychain("govee-api-key"))
        _govee_key["at"] = time.monotonic()
    return _govee_key["value"]


def _read(path):
    try:
        return path.read_text().strip()
    except OSError:
        return ""


class Thermostat:
    """SwitchBot Smart Radiator Thermostat. Readings are broadcast openly; control is encrypted and needs the
    one-time key from scripts/switchbot_key.py (saved in ~/.config/nofuss-home/switchbot_keys.json)."""

    MODES = ["comfort", "eco", "manual", "boost", "off"]
    FOLLOW_EVERY = 1800  # seconds between routine room-meter corrections (each one wakes the radio, may move the valve)
    STALE = 900  # ignore readings older than this
    DEADBAND = 0.3  # room within this of the goal: leave the thermostat alone
    OFFSET_TAU = 7200  # seconds; how slowly the learned room-vs-thermostat gap follows new readings
    PUSH = 2.0  # extra degrees while warming up after the goal was raised well above the room
    def __init__(self, cfg, history):
        self.cfg = cfg  # {"address", "mac", "room_sensor": meter address, "desired": room °C, "offset": learned gap}
        self.history = history
        self.data, self.ble, self.seen_at, self.dev = {}, None, None, None
        self.lock = asyncio.Lock()
        self.on_found = lambda: None  # also called when cfg changes, to save it
        self.rooms = lambda: {}  # meter address -> {"temp", "seen", ...}, set by main()
        self.followed_at = 0
        self.learned_at = None
        self.push = False  # warming up after a big goal increase
        self.hold = None  # (mode, until): a mode we just set, kept until the broadcasts catch up
        self.mode_seq = 0  # bumps on every mode request, so a queued older one can step aside
    def room_temp(self):
        """Temperature from the chosen room meter, or None when none is chosen or it went quiet."""
        r = self.rooms().get(self.cfg.get("room_sensor") or "")
        if not r or r.get("temp") is None or time.time() - (r.get("seen") or 0) > self.STALE:
            return None
        return r["temp"]
    def own_temp(self):
        """The thermostat's own reading (whole degrees, taken at the radiator), or None when stale."""
        if not self.seen_at or (dt.datetime.now() - self.seen_at).total_seconds() > self.STALE:
            return None
        return self.data.get("temperature")
    def learn_offset(self):
        """Track how much warmer the room meter reads than the thermostat, averaged over hours so the
        thermostat's whole-degree steps don't jolt the target."""
        room, own, now = self.room_temp(), self.own_temp(), time.time()
        if room is None or own is None:
            self.learned_at = None
            return
        if self.cfg.get("offset") is None:
            self.cfg["offset"] = room - own
        elif self.learned_at is not None:
            a = 1 - math.exp(-(now - self.learned_at) / self.OFFSET_TAU)
            self.cfg["offset"] += a * (room - own - self.cfg["offset"])
        self.learned_at = now
    def setpoint(self, wanted, exact=False):
        """Target to send so the *room* reaches `wanted`: the goal shifted by the learned sensor gap, so the
        thermostat's own smooth valve control holds the room there. Outside the dead band the room's miss is
        added on top; while warming up after a raise, PUSH is."""
        room, offset = self.room_temp(), self.cfg.get("offset")
        if room is None or offset is None:
            return wanted
        v = wanted - offset
        if self.push:
            v += self.PUSH
        elif abs(wanted - room) > self.DEADBAND:
            v += wanted - room
        v = max(5.0, min(30.0, v))
        return v if exact else round(v * 2) / 2
    async def follow_room(self):
        """While a room meter is in charge, keep the thermostat target tracking it."""
        self.learn_offset()
        wanted, sent = self.cfg.get("desired"), self.data.get("target_temperature")
        room, own = self.room_temp(), self.own_temp()
        if wanted is None or sent is None or room is None or self.data.get("mode") != "manual" or self.lock.locked():
            return
        urgent = False
        if self.push and room >= wanted - self.DEADBAND:  # warmed up: drop the push now, not in 30 min
            self.push, urgent = False, True
        # room past the goal while the valve may still be open: cut now instead of waiting out the interval
        if own is not None and room > wanted + self.DEADBAND and sent > own:
            urgent = True
        if not urgent and time.time() - self.followed_at < self.FOLLOW_EVERY:
            return
        if abs(self.setpoint(wanted, exact=True) - sent) < 0.5:  # close enough; don't flap between half steps
            return
        target = self.setpoint(wanted)
        self.followed_at = time.time()
        async with self.lock:
            try:
                if await self._device().set_target_temperature(target) is not False:
                    self.data["target_temperature"] = target
                    self.on_found()  # saves the learned offset with the config
                    log.info("room %.1f° (thermostat %.1f°, gap %.1f°%s) → thermostat target %g° for %g°",
                             room, self.data.get("temperature"), self.cfg["offset"],
                             ", warming up" if self.push else "", target, wanted)
            except Exception as exc:  # noqa: BLE001
                log.warning("room follow failed: %s", exc)
                self.dev = None
    def on_adv(self, device, adv_data):
        if self.cfg.get("address") and device.address != self.cfg["address"]:
            return
        parsed = parse_advertisement_data(device, adv_data)
        if not parsed or parsed.data.get("modelFriendlyName") != "Smart Thermostat Radiator":
            return
        if not self.cfg.get("address"):
            mfr = adv_data.manufacturer_data.get(2409, b"")
            self.cfg.update({"address": device.address, "mac": ":".join(f"{b:02X}" for b in mfr[:6])})
            log.info("found thermostat %s (%s)", device.address, self.cfg["mac"])
            self.on_found()
        self.ble, self.data, self.seen_at = device, parsed.data.get("data", {}), dt.datetime.now()
        if self.hold:  # the thermostat keeps broadcasting its old mode for a while after a change
            if self.data.get("mode") == self.hold[0] or time.time() > self.hold[1]:
                self.hold = None
            else:
                self.data["mode"] = self.hold[0]
        if self.dev:
            self.dev.update_from_advertisement(parsed)
        self._last_adv = parsed

    def key(self):
        try:
            return json.loads((CONFIG_DIR / "switchbot_keys.json").read_text()).get(self.cfg.get("mac", ""))
        except (OSError, ValueError):
            return None

    def _device(self):
        if self.dev is None:
            k = self.key()
            if not k:
                raise RuntimeError("thermostat needs its key (scripts/switchbot_key.py)")
            if self.ble is None:
                raise RuntimeError("thermostat not seen yet")
            self.dev = SwitchbotSmartThermostatRadiator(device=self.ble, key_id=k["key_id"],
                                                         encryption_key=k["encryption_key"])
            if getattr(self, "_last_adv", None):
                self.dev.update_from_advertisement(self._last_adv)
        return self.dev

    async def command(self, action, value=None, reason="manual"):
        """action: "target" (value = °C) or "mode" (value = one of MODES)."""
        if action == "mode":
            self.mode_seq += 1
            seq = self.mode_seq
        async with self.lock:
            for attempt in range(3):
                if action == "mode" and seq != self.mode_seq:  # a newer mode tap is waiting; let it win
                    return True
                try:
                    dev = self._device()
                    if action == "mode":
                        ok = await (dev.turn_off() if value == "off" else dev.set_preset_mode(value))
                        if ok is not False:
                            self.data["mode"], self.hold = value, (value, time.time() + 120)
                        text = "Heating off" if value == "off" else f"Heating mode {value}"
                    else:
                        if self.data.get("mode") != "manual":  # Manual holds exactly the set temperature
                            await dev.set_preset_mode("manual")
                            self.data["mode"], self.hold = "manual", ("manual", time.time() + 120)
                            await asyncio.sleep(1)
                        room = self.room_temp()
                        self.push = room is not None and room < float(value) - 1.0
                        self.cfg["desired"] = float(value)
                        self.on_found()
                        target = self.setpoint(float(value))
                        ok = await dev.set_target_temperature(target)
                        if ok is not False:
                            self.data["target_temperature"] = target
                        self.followed_at = time.time()
                        text = f"Heating set to {float(value):g}°"
                    if ok is False:
                        raise RuntimeError("thermostat rejected command")
                    self.history.add(text + ("" if reason == "manual" else f" · {reason}"))
                    return True
                except RuntimeError as exc:
                    if "key" in str(exc) or "not seen" in str(exc):
                        self.history.add(f"FAILED heating · {exc}")
                        return False
                    log.warning("thermostat attempt %s failed: %s", attempt + 1, exc)
                except Exception as exc:  # noqa: BLE001
                    log.warning("thermostat attempt %s failed: %s", attempt + 1, exc)
                    self.dev = None
                await asyncio.sleep(2)
            self.history.add(f"FAILED heating {action}")
            return False

    def status(self):
        d, room = self.data, self.room_temp()
        # with a room meter in charge, "current"/"target" are the room's; "own"/"sent" are the thermostat's
        following = room is not None and self.cfg.get("desired") is not None and d.get("mode") == "manual"
        return {"found": bool(self.cfg.get("address")), "seen": self.seen_at.isoformat() if self.seen_at else None,
                "current": d.get("temperature") if room is None else room, "own": d.get("temperature"),
                "target": self.cfg["desired"] if following else d.get("target_temperature"),
                "sent": d.get("target_temperature"), "room_sensor": self.cfg.get("room_sensor") or "",
                "following": following, "mode": d.get("mode"), "offset": self.cfg.get("offset"),
                "warming_up": self.push,
                "on": d.get("isOn"), "battery": d.get("battery"), "window_open": d.get("door_open"),
                "fault": d.get("fault_code"), "controllable": bool(self.cfg.get("mac") and self.key()),
                "mac": self.cfg.get("mac"),
                "busy": self.lock.locked()}


class Sensors:
    """Passive SwitchBot meters (e.g. Meter Pro CO2): they broadcast readings, we just listen."""

    SAMPLE_EVERY = 300  # seconds between history points
    FILE = DATA / "sensors.json"

    def __init__(self):
        self.devices = load(self.FILE, {"devices": {}})["devices"]  # address -> {name, co2, temp, ..., history}

    def on_adv(self, device, adv_data):
        if 2409 not in adv_data.manufacturer_data:
            return
        parsed = parse_advertisement_data(device, adv_data)
        data = parsed.data.get("data", {}) if parsed else {}
        if "temperature" not in data and "co2" not in data:
            return
        if "Thermostat" in str(parsed.data.get("modelFriendlyName", "")):  # has its own Heating card
            return
        d = self.devices.setdefault(device.address, {
            "name": "CO2 Meter" if "co2" in data else "Meter",
            "model": parsed.data.get("modelFriendlyName"), "history": []})
        now = time.time()
        d.update({"co2": data.get("co2"), "temp": data.get("temperature"), "humidity": data.get("humidity"),
                  "battery": data.get("battery"), "seen": now})
        if not d["history"] or now - d["history"][-1][0] >= self.SAMPLE_EVERY:
            d["history"].append([round(now), d["co2"], d["temp"], d["humidity"]])
            d["history"] = [h for h in d["history"] if now - h[0] <= 86400]
            save(self.FILE, {"devices": self.devices})

    def status(self):
        return [{"id": addr, **d} for addr, d in self.devices.items()]


class HeatMeter:
    """Logs how hard the radiator thermostat asks for heat, per day, so heating cost can be estimated later.
    The thermostat reports no valve position or energy, so this keeps two raw numbers and leaves kWh/€ to
    the app (price and radiator size can then change afterwards, e.g. once the Nebenkostenabrechnung is in):
    demand_s = seconds its target was above its own reading; duty_s = the same, weighted by how far the
    valve is likely open (full at SPAN °C below target or in boost)."""

    FILE = DATA / "heating_usage.json"
    SPAN = 2.0  # °C below target at which the valve counts as fully open
    MAX_GAP = 120  # seconds; a longer gap between ticks means we weren't watching, don't count it
    SAVE_EVERY = 300

    def __init__(self):
        self.store = load(self.FILE, {"since": None, "days": {}})
        self.last, self.saved_at = None, 0

    def tick(self, thermostat, now=None):
        now = now or time.time()
        gap, self.last = (now - self.last if self.last else 0), now
        d = thermostat.data
        fresh = thermostat.seen_at and (dt.datetime.now() - thermostat.seen_at).total_seconds() < thermostat.STALE
        if not fresh or not 0 < gap <= self.MAX_GAP or d.get("mode") in (None, "off") or d.get("door_open"):
            return
        own, target = d.get("temperature"), d.get("target_temperature")
        if own is None or target is None:
            return
        duty = 1.0 if d.get("mode") == "boost" else max(0.0, min(1.0, (target - own) / self.SPAN))
        day = self.store["days"].setdefault(dt.date.fromtimestamp(now).isoformat(), {"demand_s": 0, "duty_s": 0})
        self.store["since"] = self.store["since"] or dt.date.fromtimestamp(now).isoformat()
        if duty > 0:
            day["demand_s"] = round(day["demand_s"] + gap)
            day["duty_s"] = round(day["duty_s"] + gap * duty)
        if now - self.saved_at >= self.SAVE_EVERY:
            self.save()

    def save(self):
        self.saved_at = time.time()
        save(self.FILE, self.store)


class Presence:
    """Notices your phone joining or leaving the home Wi-Fi with nothing running on the phone: the Mac pings it
    and reads its own ARP table. A sleeping iPhone ignores pings but keeps answering ARP, and the table's
    "Expire(I)" column only refreshes on traffic *from* the phone, so a live one there means it's home.
    The phone is known by its private Wi-Fi address, which must be set to Fixed for the home network."""

    EVERY = 30
    SWEEP_EVERY = 300  # phone not at its last IP: look for its address across the subnet this often

    def __init__(self, cfg, history):
        self.cfg = cfg  # {"enabled", "mac", "ip", "away_after" min, "preset", "brightness", "dark_margin" min}
        self.history = history
        self.home, self.seen, self.since, self.started, self.swept = None, 0, None, None, 0
        self.on_arrive = self.on_leave = None  # async callbacks, set by main()
        self.on_config_change = lambda: None

    @staticmethod
    def norm(mac):
        return ":".join(x.zfill(2) for x in str(mac or "").strip().lower().replace("-", ":").split(":")) if mac else ""

    @staticmethod
    async def _run(*cmd):
        p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.DEVNULL)
        out, _ = await p.communicate()
        return p.returncode, out.decode(errors="replace")

    ARP_FILE = DATA / "arp.txt"  # macOS hides the ARP table from Python; setup.sh's helper job copies it here

    async def _arp(self):
        """mac -> (ip, heard from it lately)"""
        try:
            if time.time() - self.ARP_FILE.stat().st_mtime > 60:
                raise FileNotFoundError("stale")
            out = self.ARP_FILE.read_text()
        except OSError:
            if not getattr(self, "_warned", False):
                self._warned = True
                log.warning("no fresh %s: run setup.sh again to install the ARP helper", self.ARP_FILE.name)
            _, out = await self._run("/usr/sbin/arp", "-lan")
        rows = {}
        for f in (line.split() for line in out.splitlines()[1:]):
            if len(f) >= 4 and f[1].count(":") == 5:
                rows[self.norm(f[1])] = (f[0], f[3] not in ("expired", "(none)"))
        return rows

    @staticmethod
    def _local_ip():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # picks the outgoing interface; sends nothing
            return s.getsockname()[0]

    @staticmethod
    def _sweep(ip):
        """One empty UDP packet to every address in the /24, so the Mac ARPs them all."""
        base = ip.rsplit(".", 1)[0]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setblocking(False)
            for i in range(1, 255):
                try:
                    s.sendto(b"", (f"{base}.{i}", 9))
                except OSError:
                    pass

    async def probe(self):
        mac, ip = self.norm(self.cfg.get("mac")), self.cfg.get("ip")
        pinged = bool(ip) and (await self._run("/sbin/ping", "-c1", "-W800", ip))[0] == 0
        rows = await self._arp()
        if mac not in rows and time.time() - self.swept > self.SWEEP_EVERY:
            self.swept = time.time()
            self._sweep(ip or self._local_ip())  # answers show up in the next ARP copy
        hit = rows.get(mac)
        if hit and hit[0] != ip:  # DHCP gave it a new address
            self.cfg["ip"] = hit[0]
            self.on_config_change()
            return hit[1]
        return bool(hit and (hit[1] or pinged))

    async def tick(self, now=None):
        if not self.cfg.get("mac"):
            return
        now = now or time.time()
        self.started = self.started or now
        if await self.probe():
            self.seen = now
        away_after = self.cfg.get("away_after", 10) * 60
        if self.home is None:  # just started: settle quietly, don't switch the lamp because we restarted
            if self.seen or now - self.started >= away_after:
                self.home, self.since = bool(self.seen), now
            return
        present = bool(self.seen) and now - self.seen < away_after
        if present == self.home:
            return
        self.home, self.since = present, now
        log.info("phone %s", "home" if present else "away")
        if self.cfg.get("enabled"):
            await (self.on_arrive() if present else self.on_leave())

    def status(self):
        return {**self.cfg, "home": self.home,
                "since": dt.datetime.fromtimestamp(self.since).isoformat() if self.since else None,
                "seen": dt.datetime.fromtimestamp(self.seen).isoformat() if self.seen else None}


def keychain(label):
    try:
        return subprocess.run(["security", "find-generic-password", "-s", label, "-w"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


class Lamp:
    """Govee lamp via LAN control (UDP) or the Govee cloud API."""

    CLOUD = "https://openapi.api.govee.com/router/api/v1"

    def __init__(self, cfg, history):
        self.cfg = cfg
        self.history = history
        self.state = {"on": None, "brightness": None, "kelvin": None, "rgb": None, "zones": None}
        self.zones_at = -1e9
        self.ok_at = None
        self.error = None
        self.on_config_change = lambda: None  # set by main() to persist a changed IP

    @property
    def backend(self):
        return self.cfg.get("backend", "none")

    # LAN ------------------------------------------------------------------
    def _lan_send(self, cmd, data):
        msg = json.dumps({"msg": {"cmd": cmd, "data": data}}).encode()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(msg, (self.cfg["ip"], 4003))

    def _lan_discover(self):
        """Find the lamp's current IP by its device id (the router may hand out a new one)."""
        msg = json.dumps({"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}}).encode()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", 4002))
            s.settimeout(1)
            s.sendto(msg, ("239.255.255.250", 4001))
            end = time.monotonic() + 4
            while time.monotonic() < end:
                try:
                    data = json.loads(s.recvfrom(4096)[0])["msg"]["data"]
                except (socket.timeout, ValueError, KeyError):
                    continue
                if data.get("device") == self.cfg.get("device") or not self.cfg.get("device"):
                    if data["ip"] != self.cfg.get("ip") or not self.cfg.get("device"):
                        log.info("lamp %s (%s) at %s", data.get("sku"), data.get("device"), data["ip"])
                        self.cfg.update({"ip": data["ip"], "device": data.get("device", ""), "sku": data.get("sku", "")})
                        if self.cfg.get("backend", "none") == "none":
                            self.cfg["backend"] = "lan"
                        self.on_config_change()
                    return True
        return False

    def _lan_status(self):
        try:
            return self._lan_status_once()
        except (socket.timeout, OSError):
            if self._lan_discover():
                return self._lan_status_once()
            raise RuntimeError("lamp not reachable")

    def _lan_status_once(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", 4002))
            s.settimeout(2)
            s.sendto(json.dumps({"msg": {"cmd": "devStatus", "data": {}}}).encode(), (self.cfg["ip"], 4003))
            data = json.loads(s.recvfrom(4096)[0])["msg"]["data"]
        c = data.get("color") or {}
        return {"on": data.get("onOff") == 1, "brightness": data.get("brightness"),
                "kelvin": data.get("colorTemInKelvin") or None, "rgb": [c.get("r"), c.get("g"), c.get("b")] if c else None}

    # Cloud ----------------------------------------------------------------
    async def _cloud(self, path, payload):
        key = govee_key()
        if not key:
            raise RuntimeError("Govee API key missing")
        async with aiohttp.ClientSession() as http:
            async with http.post(f"{self.CLOUD}{path}", json={"requestId": str(uuid.uuid4()), "payload": payload},
                                 headers={"Govee-API-Key": key}, timeout=aiohttp.ClientTimeout(total=10)) as r:
                body = await r.json(content_type=None)
                if r.status != 200 or body.get("code") not in (200, None):
                    raise RuntimeError(f"Govee API: {body.get('msg') or r.status}")
                return body

    async def _cloud_discover(self):
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(f"{self.CLOUD}/user/devices", headers={"Govee-API-Key": govee_key()},
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                    devices = (await r.json(content_type=None)).get("data", [])
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            return
        lamp = next((d for d in devices if d.get("type") == "devices.types.light"), None)
        if lamp:
            log.info("lamp %s (%s) via Govee cloud", lamp["sku"], lamp["device"])
            self.cfg.update({"backend": "cloud", "sku": lamp["sku"], "device": lamp["device"]})
            self.on_config_change()

    async def _cloud_control(self, ctype, instance, value):
        await self._cloud("/device/control", {"sku": self.cfg["sku"], "device": self.cfg["device"],
                                              "capability": {"type": ctype, "instance": instance, "value": value}})

    # Public ---------------------------------------------------------------
    ZONES = {"top": "rippleLightToggle", "middle": "sideLightToggle", "bottom": "bottomLightToggle"}

    def zones_supported(self):
        return self.cfg.get("sku") in MULTIZONE_LAMPS

    def has_cloud(self):
        return bool(govee_key()) and bool(self.cfg.get("device"))

    async def refresh(self):
        """LAN status every tick; zone switches (cloud only) at most once a minute."""
        if self.backend == "none":
            # not set up yet: look for a Govee lamp with LAN Control switched on, about once a minute
            if time.monotonic() - self.zones_at > 60:
                self.zones_at = time.monotonic()
                if not await asyncio.to_thread(self._lan_discover) and govee_key():
                    await self._cloud_discover()  # no LAN Control: fall back to the cloud API
            return
        try:
            if self.backend == "lan":
                lan = await asyncio.to_thread(self._lan_status)
                self.state.update({k: v for k, v in lan.items() if k in ("on", "brightness")})
                if not self.has_cloud():
                    self.state.update(lan)
            if self.has_cloud() and (self.backend == "cloud" or time.monotonic() - self.zones_at > 60):
                await self.refresh_cloud()
            self.ok_at, self.error = dt.datetime.now(), None
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)

    async def refresh_cloud(self):
        body = await self._cloud("/device/state", {"sku": self.cfg["sku"], "device": self.cfg["device"]})
        caps = {c["instance"]: c["state"]["value"] for c in body["payload"]["capabilities"]}
        rgb = caps.get("colorRgb")
        self.state.update({
            "on": caps.get("powerSwitch") == 1, "brightness": caps.get("brightness"),
            "kelvin": caps.get("colorTemperatureK") or None,
            "rgb": [(rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255] if isinstance(rgb, int) and rgb else None,
            "zones": {z: caps.get(inst) == 1 for z, inst in self.ZONES.items()},
        })
        self.zones_at = time.monotonic()

    async def apply_preset(self, p, label=None):
        change = {"on": True}
        if p.get("rgb"):
            change["rgb"] = p["rgb"]
        elif p.get("kelvin"):
            change["kelvin"] = p["kelvin"]
        change["brightness"] = p["brightness"]
        if self.zones_supported() and self.has_cloud():
            change["zones"] = p["zones"]  # cloud round trip last; the LAN parts above land instantly
        return await self.set(change, label=label or f"Lamp: {p['name']}")

    async def set(self, change, reason="manual", label=None):
        """Everything over LAN (instant) except the zone switches, which only the cloud offers.

        Zone switches run in parallel and only for zones that actually change.
        """
        if self.backend == "none":
            raise RuntimeError("lamp not set up")
        cloud = self.has_cloud() or self.backend == "cloud"
        try:
            for key, value in change.items():
                if key == "zones":
                    if not cloud:
                        raise RuntimeError("zone switches need the Govee API key")
                    current = self.state.get("zones") or {}
                    todo = {z: bool(on) for z, on in value.items() if current.get(z) != bool(on) or not self.state.get("on")}
                    await asyncio.gather(*(self._cloud_control("devices.capabilities.toggle", self.ZONES[z], 1 if on else 0)
                                           for z, on in todo.items()))
                    self.state["zones"] = {**current, **{z: bool(on) for z, on in value.items()}}
                    continue
                if self.backend == "lan":
                    if key == "on":
                        self._lan_send("turn", {"value": 1 if value else 0})
                    elif key == "brightness":
                        self._lan_send("brightness", {"value": int(value)})
                    elif key == "kelvin":
                        self._lan_send("colorwc", {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": int(value)})
                    elif key == "rgb":
                        r, g, b = value
                        self._lan_send("colorwc", {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0})
                elif cloud:
                    if key == "on":
                        await self._cloud_control("devices.capabilities.on_off", "powerSwitch", 1 if value else 0)
                    elif key == "brightness":
                        await self._cloud_control("devices.capabilities.range", "brightness", int(value))
                    elif key == "kelvin":
                        await self._cloud_control("devices.capabilities.color_setting", "colorTemperatureK", int(value))
                    elif key == "rgb":
                        r, g, b = value
                        await self._cloud_control("devices.capabilities.color_setting", "colorRgb", (r << 16) | (g << 8) | b)
                self.state[key] = value
                if key in ("kelvin", "rgb"):
                    self.state["rgb" if key == "kelvin" else "kelvin"] = None
                if key != "on":
                    self.state["on"] = True
            if label:
                text = label
            elif "zones" in change:
                text = "Lamp lights: " + ", ".join(f"{z} {'on' if v else 'off'}" for z, v in change["zones"].items())
            elif "on" in change:
                text = "Lamp on" if change["on"] else "Lamp off"
            elif "brightness" in change:
                text = f"Lamp brightness {change['brightness']}%"
            else:
                text = "Lamp color changed"
            self.history.add(text + ("" if reason == "manual" else f" · {reason}"))
            return True
        except Exception as exc:  # noqa: BLE001
            self.history.add(f"FAILED lamp · {exc}")
            return False

    def status(self):
        return {**self.state, "backend": self.backend, "error": self.error, "cloud": self.has_cloud(),
                "zones_supported": self.zones_supported(), "model": self.cfg.get("sku"),
                "seen": self.ok_at.isoformat() if self.ok_at else None}


# Automations ------------------------------------------------------------------

def sun_times(day, s):
    """Sunrise/sunset for the configured location, or (None, None) if no location is set."""
    if s.get("lat") is None or s.get("lon") is None:
        return None, None
    loc = LocationInfo(latitude=s["lat"], longitude=s["lon"], timezone=s["timezone"])
    t = sun(loc.observer, date=day, tzinfo=ZoneInfo(s["timezone"]))
    return t["sunrise"], t["sunset"]


def when_on(day, a, s):
    tz = ZoneInfo(s["timezone"])
    if a.get("once"):
        if day.isoformat() != a.get("date"):
            return None
    elif day.weekday() not in a.get("days", range(7)):
        return None
    if a["trigger"] in ("sunrise", "sunset"):
        sunrise, sunset = sun_times(day, s)
        return sunrise if a["trigger"] == "sunrise" else sunset  # None without a location: never fires
    h, m = map(int, a["time"].split(":"))
    return dt.datetime.combine(day, dt.time(h, m), tz)


def arm_once(a, s):
    """Point a one-off at the next occurrence of its time (today if still ahead, else tomorrow)."""
    now = dt.datetime.now(ZoneInfo(s["timezone"]))
    for d in range(2):
        day = now.date() + dt.timedelta(days=d)
        t = when_on(day, {**a, "once": False, "days": list(range(7))}, s)
        if t and t > now:
            a["date"] = day.isoformat()
            return


def next_run(a, s):
    now = dt.datetime.now(ZoneInfo(s["timezone"]))
    for d in range(8):
        t = when_on(now.date() + dt.timedelta(days=d), a, s)
        if t and t > now:
            return t
    return None


def describe(a):
    """Human text for an automation's action, e.g. "Curtain opened · ♫ Morning Coffee"."""
    if a["device"] == "curtain":
        text = {"open": "Curtain opened", "close": "Curtain closed"}.get(a["action"], f"Curtain set to {a.get('position')}%")
    elif a["device"] == "music":
        text = f"Playing {a.get('playlist_name') or 'music'}" if a["action"] == "play" else "Music paused"
    elif a["device"] == "heating":
        text = f"Heating set to {a.get('temperature', 21):g}°" if a["action"] == "target" else "Heating off"
    else:
        text = "Lamp on" + (f" · {a['brightness']}%" if a.get("brightness") else "") if a["action"] == "on" else "Lamp off"
    if a["device"] != "music" and a.get("music_on") and a.get("music_uri"):
        text += f" · ♫ {a.get('music_name') or 'music'}"
    return text


async def run_automation(app, a):
    jobs = [run_main_action(app, a)]
    if a["device"] != "music" and a.get("music_on") and a.get("music_uri"):  # "Also play music", in parallel
        jobs.append(app["music"].command("playlist", a["music_uri"], a.get("music_volume"),
                                         bool(a.get("music_shuffle")), reason="automation"))
    results = await asyncio.gather(*jobs, return_exceptions=True)
    ok = all(r is True for r in results)
    when = a["time"] if a["trigger"] == "time" else a["trigger"]
    # notify only for automations (manual taps never call this)
    try:
        if ok:
            await app["push"].send(describe(a), f"Automation · {when}", url="/#autos", tag=a["id"])
        else:
            await app["push"].send("⚠️ Automation didn't work", f"{describe(a)} · {when}", url="/#autos", tag=a["id"])
    except Exception:  # noqa: BLE001
        log.exception("push failed")


async def run_main_action(app, a):
    label = "automation"
    if a["device"] == "heating":
        if a["action"] == "off":
            return await app["thermostat"].command("mode", "off", reason=label)
        return await app["thermostat"].command("target", a.get("temperature", 21), reason=label)
    if a["device"] == "curtain":
        return await app["curtain"].command(a["action"], a.get("position"), reason=label)
    elif a["device"] == "music":
        if a["action"] == "play" and a.get("uri"):
            return await app["music"].command("playlist", a["uri"], a.get("volume"), a.get("shuffle", False), reason=label)
        return await app["music"].command(a["action"], volume=a.get("volume") if a["action"] == "play" else None,
                                          reason=label)
    else:
        change = {"on": a["action"] == "on"}
        if a["action"] == "on" and a.get("brightness"):
            change["brightness"] = a["brightness"]
        if a["action"] == "on" and a.get("kelvin"):
            change["kelvin"] = a["kelvin"]
        return await app["lamp"].set(change, reason=label)


HEAT_DEFAULT = {"enabled": False, "home": 21.0, "save": 17.0,
                "periods": [{"id": "work", "days": [0, 1, 2, 3, 4], "from": "08:30", "to": "17:30"}]}


def _in_period(p, when):
    """Is `when` inside a saving period? Periods may run past midnight (e.g. 23:00-06:30)."""
    t = when.strftime("%H:%M")
    if p["from"] <= p["to"]:
        return when.weekday() in p["days"] and p["from"] <= t < p["to"]
    if t >= p["from"]:
        return when.weekday() in p["days"]
    return (when - dt.timedelta(days=1)).weekday() in p["days"] and t < p["to"]


def heat_wanted(hs, when):
    """("save" | "home", temperature) the schedule asks for at `when`."""
    if any(_in_period(p, when) for p in hs["periods"]):
        return "save", hs["save"]
    return "home", hs["home"]


def heat_next_change(hs, now):
    """When the schedule next switches, looking ahead up to a week in 5-minute steps."""
    cur = heat_wanted(hs, now)[0]
    t = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=5 - now.minute % 5)
    for _ in range(7 * 24 * 12):
        if heat_wanted(hs, t)[0] != cur:
            return t
        t += dt.timedelta(minutes=5)
    return None


async def apply_heat_schedule(app, state):
    s = app["settings"]
    hs = s.get("heating_schedule") or {}
    th = app["thermostat"]
    if not hs.get("enabled") or not th.status()["controllable"] or th.lock.locked():
        return
    now = dt.datetime.now(ZoneInfo(s["timezone"]))
    label, temp = heat_wanted(hs, now)
    key = f"{label}:{temp}"
    if state.get("heat_applied") == key:  # already switched; manual changes are kept until the next switch
        return
    if await th.command("target", temp, reason="schedule"):
        state["heat_applied"] = key
        save(STATE_FILE, state)
        nxt = heat_next_change(hs, now)
        until = f" until {nxt.strftime('%a %H:%M' if nxt.date() != now.date() else '%H:%M')}" if nxt else ""
        await app["push"].send(f"Heating → {temp:g}° ({'saving' if label == 'save' else 'at home'})",
                               f"Schedule{until}", url="/#heating", tag="heating")
    else:
        await app["push"].send("⚠️ Heating schedule didn't work", f"Couldn't set {temp:g}°", url="/#heating",
                               tag="heating")


async def scheduler(app):
    state = load(STATE_FILE, {"fired": {}})  # read once; kept in memory, written only when something fires
    app["state"] = state
    while True:
        try:
            await apply_heat_schedule(app, state)
        except Exception:  # noqa: BLE001
            log.exception("heating schedule failed")
        try:
            await app["thermostat"].follow_room()
        except Exception:  # noqa: BLE001
            log.exception("room follow failed")
        try:
            app["heat_meter"].tick(app["thermostat"])
        except Exception:  # noqa: BLE001
            log.exception("heat meter tick failed")
        try:
            s = app["settings"]
            now = dt.datetime.now(ZoneInfo(s["timezone"]))
            for a in s["automations"]:
                if not a.get("enabled"):
                    continue
                t = when_on(now.date(), a, s)
                key = f"{now.date()}:{a['id']}:{a['trigger']}:{a['time']}"
                if t and t <= now < t + GRACE and key not in state["fired"]:
                    state["fired"][key] = now.isoformat()
                    state["fired"] = dict(list(state["fired"].items())[-50:])
                    save(STATE_FILE, state)
                    asyncio.create_task(run_automation(app, a))
                    if a.get("once"):  # one-off: done after it fires
                        a["enabled"] = False
                        save(SETTINGS_FILE, s)
        except Exception:  # noqa: BLE001
            log.exception("scheduler tick failed")
        await asyncio.sleep(20)


def is_dark(s, margin_min=30):
    """Between (sunset - margin) and (sunrise + margin); without a location, 18:00-07:00."""
    now = dt.datetime.now(ZoneInfo(s["timezone"]))
    sunrise, sunset = sun_times(now.date(), s)
    if sunrise is None:
        return not 7 <= now.hour < 18
    margin = dt.timedelta(minutes=margin_min)
    return now < sunrise + margin or now > sunset - margin


async def presence_arrive(app):
    s, c = app["settings"], app["settings"]["presence"]
    if not is_dark(s, c.get("dark_margin", 30)):
        log.info("phone home, but it's light out: lamp left alone")
        return
    p = next((p for p in s["lamp_presets"] if p["id"] == c.get("preset")), None) or s["lamp_presets"][0]
    await app["lamp"].apply_preset({**p, "brightness": c.get("brightness", 50)},
                                   label=f"Lamp: {p['name']} {c.get('brightness', 50)}% · you're home")


async def presence_leave(app):
    if app["lamp"].status().get("on") is not False:
        await app["lamp"].set({"on": False}, label="Lamp off · you left")


async def presence_poller(app):
    while True:
        try:
            await app["presence"].tick()
        except Exception:  # noqa: BLE001
            log.exception("presence tick failed")
        await asyncio.sleep(Presence.EVERY)


async def music_poller(app):
    while True:
        await app["music"].refresh()
        await asyncio.sleep(4)


async def lamp_poller(app):
    while True:
        await app["lamp"].refresh()
        await asyncio.sleep(10 if app["lamp"].backend == "lan" else 60)


# HTTP ---------------------------------------------------------------------------

def heat_snapshot(s):
    hs = s["heating_schedule"]
    now = dt.datetime.now(ZoneInfo(s["timezone"]))
    label, temp = heat_wanted(hs, now)
    nxt = heat_next_change(hs, now)
    return {**hs, "now": label, "now_temp": temp,
            "next": nxt.strftime("%a %H:%M" if nxt and nxt.date() != now.date() else "%H:%M") if nxt else None}


def snapshot(app):
    s = app["settings"]
    tz = ZoneInfo(s["timezone"])
    today = dt.datetime.now(tz).date()
    sunrise, sunset = sun_times(today, s)
    autos = []
    for a in s["automations"]:
        n = next_run(a, s) if a.get("enabled") else None
        autos.append({**a, "next": n.isoformat() if n else None})
    return {
        "curtain": app["curtain"].status(),
        "lamp": app["lamp"].status(),
        "automations": autos,
        "sun": {"sunrise": sunrise.strftime("%H:%M") if sunrise else None,
                "sunset": sunset.strftime("%H:%M") if sunset else None},
        "history": app["history"].items,
        "presets": s["lamp_presets"],
        "sensors": app["sensors"].status(),
        "thermostat": app["thermostat"].status(),
        "presence": app["presence"].status(),
        "heating_schedule": heat_snapshot(s),
        "music": app["music"].status(),
        "bluetooth": app.get("bluetooth"),
    }


async def api_state(request):
    return web.json_response(snapshot(request.app))


async def api_curtain(request):
    body = await request.json()
    action = body.get("action")
    if action not in {"open", "close", "stop", "position"}:
        raise web.HTTPBadRequest(text="bad action")
    if action == "position" and not 0 <= int(body.get("position", -1)) <= 100:
        raise web.HTTPBadRequest(text="bad position")
    # respond right away; the UI follows progress via /api/state
    asyncio.create_task(request.app["curtain"].command(action, body.get("position")))
    return web.json_response({"ok": True})


async def api_lamp(request):
    body = await request.json()
    change = {}
    if "on" in body:
        change["on"] = bool(body["on"])
    if "brightness" in body:
        change["brightness"] = max(1, min(100, int(body["brightness"])))
    if "kelvin" in body:
        change["kelvin"] = max(2000, min(9000, int(body["kelvin"])))
    if isinstance(body.get("zones"), dict):
        change["zones"] = {z: bool(v) for z, v in body["zones"].items() if z in Lamp.ZONES}
    if "rgb" in body:
        change["rgb"] = [max(0, min(255, int(v))) for v in body["rgb"]][:3]
    try:
        ok = await request.app["lamp"].set(change)
    except RuntimeError as exc:
        raise web.HTTPConflict(text=str(exc))
    return web.json_response({"ok": ok, "lamp": request.app["lamp"].status()})


def clean_automation(body, existing_id=None):
    device = body.get("device")
    actions = {"curtain": {"open", "close", "position"}, "lamp": {"on", "off"}, "music": {"play", "pause"},
               "heating": {"target", "off"}}
    if device not in actions or body.get("action") not in actions[device]:
        raise web.HTTPBadRequest(text="bad device/action")
    if body.get("trigger") not in {"time", "sunrise", "sunset"}:
        raise web.HTTPBadRequest(text="bad trigger")
    time = body.get("time") or "08:00"
    dt.time.fromisoformat(time)
    days = sorted({int(d) for d in body.get("days", range(7)) if 0 <= int(d) <= 6})
    out = {"id": existing_id or uuid.uuid4().hex[:8], "device": device, "action": body["action"],
           "trigger": body["trigger"], "time": time, "days": days, "enabled": bool(body.get("enabled", True)),
           "once": bool(body.get("once", False))}
    if body["action"] == "position":
        out["position"] = max(0, min(100, int(body.get("position", 50))))
    if device == "lamp" and body["action"] == "on":
        if body.get("brightness"):
            out["brightness"] = max(1, min(100, int(body["brightness"])))
        if body.get("kelvin"):
            out["kelvin"] = max(2000, min(9000, int(body["kelvin"])))
    if device == "heating" and body["action"] == "target":
        out["temperature"] = max(5.0, min(30.0, round(float(body.get("temperature") or 21) * 2) / 2))
    if device != "music" and body.get("music_on"):
        out["music_on"] = 1
        out["music_uri"] = str(body.get("music_uri") or "") or None
        out["music_name"] = str(body.get("music_name") or "")[:80]
        out["music_volume"] = max(0, min(100, int(body.get("music_volume") or 20)))
        out["music_shuffle"] = 1 if int(body.get("music_shuffle", 1) or 0) else 0
    if device == "music" and body["action"] == "play":
        if body.get("uri"):
            out["uri"] = str(body["uri"])
            out["playlist_name"] = str(body.get("playlist_name") or "")[:80]
        out["volume"] = max(0, min(100, int(body.get("volume") or 20)))
        out["shuffle"] = bool(body.get("shuffle", False))
    return out


async def api_heating(request):
    body = await request.json()
    t = request.app["thermostat"]
    if body.get("action") == "mode" and body.get("mode") in Thermostat.MODES:
        ok = await t.command("mode", body["mode"])
    elif body.get("action") == "target":
        ok = await t.command("target", max(5.0, min(30.0, round(float(body["value"]) * 2) / 2)))
    elif body.get("action") == "sensor" and (body.get("sensor") or "") in {"", *request.app["sensors"].devices}:
        t.cfg["room_sensor"] = body.get("sensor") or ""
        t.on_found()
        wanted = t.cfg.get("desired") or t.data.get("target_temperature")
        manual = t.data.get("mode") == "manual"
        ok = await t.command("target", wanted, reason="room sensor") if wanted and manual else True
    else:
        raise web.HTTPBadRequest(text="bad action")
    return web.json_response({"ok": ok, "thermostat": t.status()})


PRESENCE_DEFAULT = {"enabled": False, "mac": "", "ip": "", "away_after": 10, "preset": "cozy", "brightness": 50,
                    "dark_margin": 30}


async def api_presence(request):
    c, body = request.app["settings"]["presence"], await request.json()
    try:
        if "enabled" in body:
            c["enabled"] = bool(body["enabled"])
        if "mac" in body:
            mac = Presence.norm(body["mac"])
            if mac and not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
                raise ValueError(mac)
            if mac != c["mac"]:
                c["mac"], c["ip"] = mac, ""
                request.app["presence"].home = None  # new phone: settle again before acting
        if body.get("preset"):
            if not any(p["id"] == body["preset"] for p in request.app["settings"]["lamp_presets"]):
                raise ValueError(body["preset"])
            c["preset"] = body["preset"]
        for k, lo, hi in (("away_after", 2, 120), ("brightness", 1, 100), ("dark_margin", 0, 120)):
            if k in body:
                c[k] = max(lo, min(hi, int(body[k])))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="bad value")
    save(SETTINGS_FILE, request.app["settings"])
    return web.json_response(request.app["presence"].status())


HEAT_COST_DEFAULT = {"type": "gas", "price": None, "radiator_kw": None,  # None = the app's typical value
                     "from": None, "to": None}


def heat_usage(app):
    return {**app["heat_meter"].store, "cost": app["settings"]["heating_cost"]}


async def api_heating_usage(request):
    return web.json_response(heat_usage(request.app))


async def api_heating_cost(request):
    c, body = request.app["settings"]["heating_cost"], await request.json()
    if body.get("type") in {"gas", "district", "electric", "other"}:
        c["type"] = body["type"]
    try:
        for k, lo, hi in (("price", 0.1, 200.0), ("radiator_kw", 0.1, 10.0)):  # ct/kWh, kW
            if k in body:
                c[k] = None if body[k] in (None, "") else max(lo, min(hi, round(float(body[k]), 2)))
        for k in ("from", "to"):  # billing period, e.g. from the Nebenkostenabrechnung
            if k in body:
                c[k] = dt.date.fromisoformat(body[k]).isoformat() if body[k] else None
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="bad value")
    save(SETTINGS_FILE, request.app["settings"])
    return web.json_response(heat_usage(request.app))


def _hhmm(v):
    dt.time.fromisoformat(v)
    return v[:5]


async def api_heating_schedule(request):
    s, body = request.app["settings"], await request.json()
    hs = s["heating_schedule"]
    if "enabled" in body:
        hs["enabled"] = bool(body["enabled"])
    for k in ("home", "save"):
        if k in body:
            hs[k] = max(5.0, min(30.0, round(float(body[k]) * 2) / 2))
    if isinstance(body.get("periods"), list):
        hs["periods"] = [{"id": str(p.get("id") or uuid.uuid4().hex[:6]),
                          "days": sorted({int(d) for d in p.get("days", []) if 0 <= int(d) <= 6}) or [0, 1, 2, 3, 4],
                          "from": _hhmm(p.get("from", "08:30")), "to": _hhmm(p.get("to", "17:30"))}
                         for p in body["periods"]][:10]
    save(SETTINGS_FILE, s)
    request.app.get("state", {}).pop("heat_applied", None)  # apply the (changed) schedule right away
    asyncio.create_task(apply_heat_schedule(request.app, request.app.get("state", {})))
    return web.json_response(snapshot(request.app))


async def api_music(request):
    body = await request.json()
    action = body.get("action")
    if action not in {"playlist", "play", "pause", "next", "previous", "volume", "vol_up", "vol_down"}:
        raise web.HTTPBadRequest(text="bad action")
    if action == "playlist" and not str(body.get("uri", "")).startswith("spotify:"):
        raise web.HTTPBadRequest(text="bad uri")
    vol = body.get("volume")
    vol = None if vol is None else max(0, min(100, int(vol)))
    start = body.get("start")
    if start is not None and not str(start).startswith("spotify:track:"):
        raise web.HTTPBadRequest(text="bad start")
    ok = await request.app["music"].command(None if action == "volume" else action, body.get("uri"), vol,
                                            bool(body.get("shuffle", False)), start=start)
    return web.json_response({"ok": ok, "music": request.app["music"].status()})


async def api_music_like(request):
    ok, error = await request.app["music"].like(bool((await request.json()).get("on", True)))
    return web.json_response({"ok": ok, "error": error, "music": request.app["music"].status()})


async def api_music_mood(request):
    query = str((await request.json()).get("query") or "").strip()[:60]
    if not query:
        raise web.HTTPBadRequest(text="empty mood")
    try:
        p = await request.app["music"].play_mood(query)
    except Exception as exc:  # noqa: BLE001 — Spotify unreachable, login expired, …
        log.warning("mood search failed: %s", exc)
        return web.json_response({"ok": False, "error": "Spotify search failed"})
    if not p:
        return web.json_response({"ok": False, "error": f"No “{query}” playlist with 1,000+ likes"})
    return web.json_response({"ok": True, "playlist": p, "music": request.app["music"].status()})


async def api_music_step(request):
    """For iPhone Shortcuts / Control Center buttons: one POST, no body."""
    ok = await request.app["music"].command("vol_" + request.match_info["dir"])
    await request.app["music"].refresh()
    return web.json_response({"ok": ok, "volume": request.app["music"].state.get("volume")})


async def api_music_playlists(request):
    try:
        return web.json_response(await request.app["music"].playlists())
    except Exception as exc:  # noqa: BLE001
        raise web.HTTPBadGateway(text=f"Spotify: {exc}")


async def api_music_tracks(request):
    uri = request.query.get("uri", "")
    if not uri.startswith("spotify:"):
        raise web.HTTPBadRequest(text="bad uri")
    try:
        return web.json_response(await request.app["music"].tracks(uri))
    except Exception as exc:  # noqa: BLE001
        raise web.HTTPBadGateway(text=f"Spotify: {exc}")


async def api_music_art(request):
    try:
        data, ctype = await request.app["music"].art()
    except Exception:  # noqa: BLE001
        data = None
    if not data:
        raise web.HTTPNotFound()
    return web.Response(body=data, content_type=ctype.split(";")[0], headers={"Cache-Control": "max-age=300"})


async def api_automation_upsert(request):
    s = request.app["settings"]
    body = await request.json()
    aid = request.match_info.get("id")
    if aid:
        idx = next((i for i, a in enumerate(s["automations"]) if a["id"] == aid), None)
        if idx is None:
            raise web.HTTPNotFound()
        a = clean_automation({**s["automations"][idx], **body}, aid)
        s["automations"][idx] = a
    else:
        a = clean_automation(body)
        s["automations"].append(a)
    if a["once"]:
        arm_once(a, s)
    save(SETTINGS_FILE, s)
    return web.json_response(snapshot(request.app))


async def api_automation_delete(request):
    s = request.app["settings"]
    s["automations"] = [a for a in s["automations"] if a["id"] != request.match_info["id"]]
    save(SETTINGS_FILE, s)
    return web.json_response(snapshot(request.app))


async def api_preset_apply(request):
    p = next((p for p in request.app["settings"]["lamp_presets"] if p["id"] == request.match_info["id"]), None)
    if p is None:
        raise web.HTTPNotFound()
    ok = await request.app["lamp"].apply_preset(p)
    return web.json_response({"ok": ok, "lamp": request.app["lamp"].status()})


async def api_preset_save(request):
    """Save the lamp's current look under a name."""
    s, body, lamp = request.app["settings"], await request.json(), request.app["lamp"].state
    name = str(body.get("name", "")).strip()[:24]
    if not name:
        raise web.HTTPBadRequest(text="name required")
    zones = lamp.get("zones") or {"top": True, "middle": False, "bottom": False}
    p = {"id": uuid.uuid4().hex[:8], "name": name, "icon": str(body.get("icon") or "⭐")[:4],
         "brightness": lamp.get("brightness") or 50, "zones": dict(zones)}
    if lamp.get("rgb"):
        p["rgb"] = lamp["rgb"]
    elif lamp.get("kelvin"):
        p["kelvin"] = lamp["kelvin"]
    s["lamp_presets"].append(p)
    save(SETTINGS_FILE, s)
    return web.json_response(snapshot(request.app))


async def api_preset_order(request):
    s, ids = request.app["settings"], (await request.json()).get("ids", [])
    by_id = {p["id"]: p for p in s["lamp_presets"]}
    s["lamp_presets"] = [by_id[i] for i in ids if i in by_id] + [p for p in s["lamp_presets"] if p["id"] not in ids]
    save(SETTINGS_FILE, s)
    return web.json_response(snapshot(request.app))


async def api_preset_delete(request):
    s = request.app["settings"]
    s["lamp_presets"] = [p for p in s["lamp_presets"] if p["id"] != request.match_info["id"]]
    save(SETTINGS_FILE, s)
    return web.json_response(snapshot(request.app))


async def api_push_key(request):
    return web.json_response({"key": request.app["push"].public_key(), "subscribers": len(request.app["push"].subs)})


async def api_push_subscribe(request):
    try:
        request.app["push"].subscribe(await request.json())
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    return web.json_response({"ok": True})


async def api_push_unsubscribe(request):
    request.app["push"].unsubscribe((await request.json()).get("endpoint", ""))
    return web.json_response({"ok": True})


async def api_push_test(request):
    sent = await request.app["push"].send("🔔 Notifications work", "You'll get one whenever an automation runs.",
                                          url="/#autos", tag="test")
    return web.json_response({"sent": sent})


async def service_worker(request):
    return web.FileResponse(BASE / "web" / "sw.js", headers={"Cache-Control": "no-cache",
                                                              "Content-Type": "application/javascript"})


async def manifest(request):
    return web.json_response({
        "name": "NoFuss Home", "short_name": "Home", "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#000000", "theme_color": "#000000",
        "icons": [{"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
                  {"src": "/static/icon.png", "sizes": "180x180", "type": "image/png"}]},
        content_type="application/manifest+json")


async def index(request):
    return web.Response(body=(BASE / "web" / "index.html").read_bytes(), content_type="text/html",
                        headers={"Cache-Control": "no-cache"})


@web.middleware
async def compress(request, handler):
    """gzip the page and JSON (77 KB -> ~20 KB): every byte counts on a cold start over Tailscale."""
    resp = await handler(request)
    if type(resp) is web.Response and not (resp.content_type or "").startswith("image/"):
        resp.enable_compression()
    return resp


def start_watchdog(loop, timeout=45):
    """If the event loop stops responding (e.g. a file access hangs in the OS), dump all stacks to the
    log and exit; launchd's KeepAlive restarts the app a few seconds later."""
    beat = {"t": time.monotonic()}

    def tick():
        beat["t"] = time.monotonic()
        loop.call_later(5, tick)

    def watch():
        while True:
            time.sleep(10)
            if time.monotonic() - beat["t"] > timeout:
                log.error("event loop unresponsive for %.0f s - restarting", time.monotonic() - beat["t"])
                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
                sys.stderr.flush()
                os._exit(1)

    loop.call_soon(tick)
    threading.Thread(target=watch, name="watchdog", daemon=True).start()


async def on_startup(app):
    start_watchdog(asyncio.get_running_loop())
    def on_adv(device, adv_data):
        app["curtain"].on_adv(device, adv_data)
        app["sensors"].on_adv(device, adv_data)
        app["thermostat"].on_adv(device, adv_data)
    app["scanner"] = BleakScanner(detection_callback=on_adv)
    app["tasks"] = [asyncio.create_task(scheduler(app)), asyncio.create_task(lamp_poller(app)),
                    asyncio.create_task(music_poller(app)), asyncio.create_task(bluetooth(app)),
                    asyncio.create_task(presence_poller(app))]
    log.info("listening on :%s", PORT)


async def bluetooth(app):
    """Listen for SwitchBot devices. Bluetooth is optional: lamp and music keep working without it."""
    while True:
        try:
            await app["scanner"].start()
            app["bluetooth"] = True
            return
        except Exception as exc:  # noqa: BLE001 — e.g. permission not granted yet
            app["bluetooth"] = False
            log.warning("Bluetooth unavailable (%s); allow it for Python in System Settings → Privacy & "
                        "Security → Bluetooth. Retrying in 60 s.", exc)
            await asyncio.sleep(60)


async def on_cleanup(app):
    for t in app["tasks"]:
        t.cancel()
    app["heat_meter"].save()
    if app.get("bluetooth"):
        await app["scanner"].stop()


def main():
    DATA.mkdir(exist_ok=True)
    settings = load(SETTINGS_FILE, DEFAULTS)
    save(SETTINGS_FILE, settings)
    history = History()
    app = web.Application(middlewares=[compress])
    app["settings"] = settings
    app["history"] = history
    app["curtain"] = Curtain(settings["address"], history)

    def remember_curtain(address):
        settings["address"] = address
        save(SETTINGS_FILE, settings)
    app["curtain"].on_found = remember_curtain
    app["sensors"] = Sensors()
    settings.setdefault("thermostat", {"address": "", "mac": ""})
    settings.setdefault("heating_schedule", json.loads(json.dumps(HEAT_DEFAULT)))
    app["thermostat"] = Thermostat(settings["thermostat"], history)
    app["thermostat"].on_found = lambda: save(SETTINGS_FILE, settings)
    app["thermostat"].rooms = lambda: app["sensors"].devices
    settings.setdefault("heating_cost", dict(HEAT_COST_DEFAULT))
    app["heat_meter"] = HeatMeter()
    settings["presence"] = {**PRESENCE_DEFAULT, **settings.get("presence", {})}
    app["presence"] = Presence(settings["presence"], history)
    app["presence"].on_config_change = lambda: save(SETTINGS_FILE, settings)
    app["presence"].on_arrive = lambda: presence_arrive(app)
    app["presence"].on_leave = lambda: presence_leave(app)
    app["push"] = Push(DATA, CONFIG_DIR)
    app["music"] = Music(settings["music"], history)
    app["music"].on_config_change = lambda: save(SETTINGS_FILE, settings)
    app["lamp"] = Lamp(settings["lamp"], history)
    app["lamp"].on_config_change = lambda: save(SETTINGS_FILE, settings)
    app.add_routes([
        web.get("/", index),
        web.get("/sw.js", service_worker),
        web.get("/manifest.webmanifest", manifest),
        web.get("/api/push/key", api_push_key),
        web.post("/api/push/subscribe", api_push_subscribe),
        web.post("/api/push/unsubscribe", api_push_unsubscribe),
        web.post("/api/push/test", api_push_test),
        web.get("/api/state", api_state),
        web.post("/api/curtain", api_curtain),
        web.post("/api/lamp", api_lamp),
        web.post("/api/music", api_music),
        web.post("/api/music/mood", api_music_mood),
        web.post("/api/music/like", api_music_like),
        web.post("/api/heating", api_heating),
        web.post("/api/heating/schedule", api_heating_schedule),
        web.get("/api/heating/usage", api_heating_usage),
        web.post("/api/heating/cost", api_heating_cost),
        web.post("/api/presence", api_presence),
        web.get("/api/music/playlists", api_music_playlists),
        web.get("/api/music/tracks", api_music_tracks),
        web.post("/api/music/volume/{dir:up|down}", api_music_step),
        web.get("/api/music/art", api_music_art),
        web.post("/api/presets", api_preset_save),
        web.post("/api/presets/order", api_preset_order),
        web.post("/api/presets/{id}/apply", api_preset_apply),
        web.delete("/api/presets/{id}", api_preset_delete),
        web.post("/api/automations", api_automation_upsert),
        web.post("/api/automations/{id}", api_automation_upsert),
        web.delete("/api/automations/{id}", api_automation_delete),
        web.static("/static", BASE / "web"),
    ])
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, port=PORT, print=None)


if __name__ == "__main__":
    main()
