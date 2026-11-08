"""Security rules for the phone panel (system/hexaweb), kept free of I/O so they can be tested.

- Pairing: the comma shows a 6-digit code for PIN_TTL seconds; a phone that sends it gets a random token.
  Only the token's SHA-256 is stored. Codes are single use, and too many wrong codes clear the code.
- Writes: an option that changes how the car drives can only be written with the car off, or parked
  (P, standstill, openpilot not engaged). Some options are never writable from the phone.
- Network: only private, loopback and link-local addresses may talk to the panel.
"""
import hashlib
import hmac
import ipaddress
import re
import secrets
import socket
import time
from dataclasses import dataclass

from openpilot.selfdrive.hexa.options import HexaOption

PIN_TTL = 120.  # s
MAX_TOKENS = 10  # phones, tablets, computers (owner, 04/11: with 5 the one in use fell off the list)
SEEN_EVERY_S = 3600.  # a phone's last use is written at most once an hour: params live on flash
MAX_AUDIT = 100
MAX_PIN_FAILURES = 5
PORT = 8088
HOTSPOT_IP = "192.168.43.1"

PIN_PARAM = "HexaWebPin"
TOKENS_PARAM = "HexaWebTokens"
AUDIT_PARAM = "HexaWebAudit"


def wall_time() -> float:
  return time.time()  # noqa: TID251 - token and audit times are shown to the user as dates


@dataclass(frozen=True)
class VehicleState:
  onroad: bool  # ignition on and openpilot started
  parked: bool = False  # gear in P
  standstill: bool = False
  engaged: bool = False


def can_write(option: HexaOption, state: VehicleState) -> tuple[bool, str]:
  if not option.phone_writable:
    return False, "só pelo comma"
  if option.driving and state.onroad and not (state.parked and state.standstill and not state.engaged):
    return False, "carro andando: só leitura"
  return True, ""


def is_local_address(remote: str | None) -> bool:
  if not remote:
    return False
  try:
    ip = ipaddress.ip_address(remote.split("%")[0])
  except ValueError:
    return False
  return ip.is_private or ip.is_loopback or ip.is_link_local


# *** pairing code ***

def new_pin(now: float | None = None) -> str:
  """Returns the value to store in PIN_PARAM: '<6 digits>:<expiry, monotonic clock>'.
  The monotonic clock is shared by all processes until reboot, and PIN_PARAM is cleared on manager start."""
  now = time.monotonic() if now is None else now
  return f"{secrets.randbelow(10**6):06d}:{int(now + PIN_TTL)}"


def parse_pin(stored: str | None, now: float | None = None) -> str | None:
  """The current code, or None if there is none or it expired."""
  now = time.monotonic() if now is None else now
  if not stored or ":" not in stored:
    return None
  pin, expiry = stored.split(":", 1)
  try:
    if now > float(expiry):
      return None
  except ValueError:
    return None
  return pin if len(pin) == 6 and pin.isdigit() else None


def pin_matches(stored: str | None, attempt: str, now: float | None = None) -> bool:
  pin = parse_pin(stored, now)
  return pin is not None and hmac.compare_digest(pin.encode(), str(attempt).strip().encode())


# *** tokens ***

def hash_token(token: str) -> str:
  return hashlib.sha256(token.encode()).hexdigest()


def last_use(entry: dict) -> float:
  return float(entry.get("seen") or entry.get("created") or 0)


def issue_token(tokens: list[dict] | None, name: str, now: float | None = None) -> tuple[str, list[dict]]:
  """Returns (token for the phone, new token list to store). Keeps at most MAX_TOKENS: when full, the one unused the
  longest goes, never one in use just because it was paired first (dropped() tells which)."""
  now = wall_time() if now is None else now
  token = secrets.token_urlsafe(32)
  entry = {"id": secrets.token_hex(4), "hash": hash_token(token), "name": (name or "").strip()[:40] or "celular", "created": int(now)}
  tokens = list(tokens or [])
  while len(tokens) >= MAX_TOKENS:
    tokens.remove(min(tokens, key=last_use))
  return token, tokens + [entry]


def dropped(before: list[dict] | None, after: list[dict] | None) -> list[dict]:
  """The phones a pairing took off the list."""
  ids = {t.get("id") for t in after or []}
  return [t for t in before or [] if t.get("id") not in ids]


def touch_token(tokens: list[dict] | None, token_id: str, now: float | None = None) -> list[dict] | None:
  """The phone's last use, or None when it was written less than SEEN_EVERY_S ago (nothing to store)."""
  now = wall_time() if now is None else now
  for t in tokens or []:
    if t.get("id") == token_id:
      if now - float(t.get("seen") or 0) < SEEN_EVERY_S:
        return None
      return [{**x, "seen": int(now)} if x.get("id") == token_id else x for x in tokens]
  return None


def find_token(tokens: list[dict] | None, token: str | None) -> dict | None:
  if not token:
    return None
  h = hash_token(token)
  for entry in tokens or []:
    if hmac.compare_digest(str(entry.get("hash", "")), h):
      return entry
  return None


# *** this is my comma (the LizPilot app, APP-02) ***
# The app keeps hash_token() of its own pairing. Before it opens a page or lets the token go out, it asks for
# HMAC(hash, nonce) with a nonce of its own. Only the comma that paired the phone knows the hash: another comma on the
# same hotspot address, or a device answering for lizpilot.local, can't answer. The token never travels for this.
IDENT_PREFIX = "lizpilot-ident|v1|"
IDENT_NONCE = re.compile(r"[A-Za-z0-9_-]{16,64}")
IDENT_PHONE = re.compile(r"[0-9a-f]{16}")


def ident_phone(token_hash: str) -> str:
  """How the app says which phone it is: a hash of the stored hash, so it opens nothing."""
  return hashlib.sha256(token_hash.encode()).hexdigest()[:16]


def ident_mac(token_hash: str, nonce: str) -> str:
  return hmac.new(token_hash.encode(), (IDENT_PREFIX + nonce).encode(), hashlib.sha256).hexdigest()


def ident_answer(tokens: list[dict] | None, phone_id, nonce) -> dict | None:
  """None for a malformed question. mac None: this comma doesn't know that phone (never paired here, or taken off)."""
  if not (isinstance(nonce, str) and IDENT_NONCE.fullmatch(nonce) and isinstance(phone_id, str) and IDENT_PHONE.fullmatch(phone_id)):
    return None
  for entry in tokens or []:
    h = str(entry.get("hash", ""))
    if h and hmac.compare_digest(ident_phone(h), phone_id):
      return {"v": 1, "mac": ident_mac(h, nonce)}
  return {"v": 1, "mac": None}


# *** the route on the car's screen (Android Auto, NAV-03 step 2) ***
# The app's car part reads the route without the token: it answers a challenge the comma handed out with
# HMAC(hash, CAR_PREFIX + challenge), hash being hash_token() of its pairing, which the app keeps (APP-02). A challenge is
# good once and for CHALLENGE_TTL_S. Its own prefix, never IDENT_PREFIX: /api/ident answers HMAC(hash, IDENT_PREFIX + any
# nonce) to anyone, so with one prefix for both anyone could get a valid answer by asking /api/ident with the challenge.
CAR_PREFIX = "lizpilot-car|v1|"
CAR_MAC = re.compile(r"[0-9a-f]{64}")
CHALLENGE_TTL_S = 30.
CHALLENGE_PER_ADDRESS = 4  # open challenges per asking address: one asking in a loop pushes out only its own
CHALLENGE_KEEP = 256  # open challenges in all: 64 addresses asking in a loop, before anyone else's leaves


def car_mac(token_hash: str, challenge: str) -> str:
  return hmac.new(token_hash.encode(), (CAR_PREFIX + challenge).encode(), hashlib.sha256).hexdigest()


class Challenges:
  """The challenges the comma handed out, each tied to the address that asked and redeemed once, by that address, within
  CHALLENGE_TTL_S. Another device of the network can neither push the app's out nor use it up (CTO review of navcel-02,
  item 3). Not thread safe: the caller locks."""

  def __init__(self, mono=time.monotonic):
    self.mono = mono
    self._open: dict[str, tuple[str, float]] = {}  # challenge -> (address that asked, when)

  def issue(self, who: str) -> str:
    now = self.mono()
    self._open = {c: (w, t) for c, (w, t) in self._open.items() if now - t < CHALLENGE_TTL_S}
    mine = [c for c, (w, _) in self._open.items() if w == who]
    for c in mine[:max(0, len(mine) - CHALLENGE_PER_ADDRESS + 1)]:
      self._open.pop(c)
    while len(self._open) >= CHALLENGE_KEEP:
      self._open.pop(next(iter(self._open)))
    c = secrets.token_urlsafe(16)  # 22 characters of [A-Za-z0-9_-]
    self._open[c] = (str(who), now)
    return c

  def redeem(self, challenge, who) -> bool:
    if not isinstance(challenge, str):
      return False
    got = self._open.get(challenge)
    if got is None or got[0] != who:  # never handed out, or handed to another address: that one keeps it
      return False
    self._open.pop(challenge)
    return self.mono() - got[1] < CHALLENGE_TTL_S


def car_reader(tokens: list[dict] | None, phone_id, challenge, mac) -> dict | None:
  """The pairing whose hash answered the challenge, or None. The caller redeems the challenge first, right or wrong."""
  if not (isinstance(phone_id, str) and IDENT_PHONE.fullmatch(phone_id) and isinstance(challenge, str) and IDENT_NONCE.fullmatch(challenge)
          and isinstance(mac, str) and CAR_MAC.fullmatch(mac)):
    return None
  for entry in tokens or []:
    h = str(entry.get("hash", ""))
    if h and hmac.compare_digest(ident_phone(h), phone_id) and hmac.compare_digest(car_mac(h, challenge), mac):
      return entry
  return None


def revoke_token(tokens: list[dict] | None, token_id: str) -> list[dict]:
  return [t for t in tokens or [] if t.get("id") != token_id]


# *** audit ***

def add_audit(audit: list[dict] | None, entry: dict, now: float | None = None) -> list[dict]:
  now = wall_time() if now is None else now
  return (list(audit or []) + [{"t": int(now), **entry}])[-MAX_AUDIT:]


# *** network ***

def local_ip() -> str:
  """Best guess of the address a phone on the same network should open. No packets are sent."""
  try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
      s.connect(("8.8.8.8", 80))
      ip = s.getsockname()[0]
      if ip and not ip.startswith("127."):
        return ip
  except OSError:
    pass
  return HOTSPOT_IP


def panel_url(ip: str | None = None) -> str:
  return f"http://{ip or local_ip()}:{PORT}"


def pair_url(ip: str | None = None) -> str:
  """What the QR on the comma opens: the panel, straight on the pairing screen. It carries no secret:
  the 6-digit code shown on the comma is still needed."""
  return panel_url(ip) + "/#parear"
