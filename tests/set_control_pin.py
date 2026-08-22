"""Administrative helper for rotating the local SkyHi dashboard PIN."""

import hashlib
import json
import os
import secrets
import sys
from pathlib import Path


auth_path = Path("/home/shravanxd/.config/skyhi/control-auth.json")
pin = sys.argv[1]
if len(pin) != 6 or not pin.isdigit():
    raise SystemExit("PIN must contain exactly six digits")

auth = json.loads(auth_path.read_text(encoding="utf-8"))
salt = secrets.token_hex(16)
auth.update(
    salt=salt,
    pin_hash=hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 200000).hex(),
    secret=secrets.token_hex(32),
)
temporary = auth_path.with_suffix(".tmp")
temporary.write_text(json.dumps(auth, separators=(",", ":")), encoding="utf-8")
os.chmod(temporary, 0o600)
os.replace(temporary, auth_path)
