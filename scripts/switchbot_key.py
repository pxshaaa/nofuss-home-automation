"""One-time: fetch the Bluetooth encryption key for an encrypted SwitchBot device
(Smart Radiator Thermostat, Lock, …) so NoFuss Home can control it locally.

Usage:  .venv/bin/python scripts/switchbot_key.py <device-MAC>
        (the MAC is shown in the SwitchBot app → device → ⚙️ → device info, e.g. AA:BB:CC:DD:EE:FF)

You type your SwitchBot account e-mail and password here; they're only sent to SwitchBot to read the key
and are never stored. Only the key is saved, in ~/.config/nofuss-home/switchbot_keys.json.
(Signed up with Apple/Google? Set a password first in the SwitchBot app → Profile → Account.)
"""
import asyncio
import getpass
import json
import os
import sys
from pathlib import Path

import aiohttp
from switchbot.devices.device import SwitchbotEncryptedDevice

OUT = Path.home() / ".config" / "nofuss-home" / "switchbot_keys.json"


async def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    mac = sys.argv[1].strip().upper()
    email = input("SwitchBot account e-mail: ").strip()
    password = getpass.getpass("SwitchBot password (hidden): ")
    async with aiohttp.ClientSession() as session:
        key = await SwitchbotEncryptedDevice.async_retrieve_encryption_key(session, mac, email, password)
    keys = json.loads(OUT.read_text()) if OUT.exists() else {}
    keys[mac] = {"key_id": key["key_id"], "encryption_key": key["encryption_key"]}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(keys, indent=2))
    os.chmod(OUT, 0o600)
    print(f"✅ Key for {mac} saved to {OUT}. Restart NoFuss Home (or wait a minute) to control it.")


if __name__ == "__main__":
    asyncio.run(main())
