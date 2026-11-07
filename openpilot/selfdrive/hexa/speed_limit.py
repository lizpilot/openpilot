"""LizPilot: the road's speed limit from the map (CRUISE-03, step 1: show and warn; it never changes the speed).

mapd (github.com/pfeiferj/mapd, MIT) reads OpenStreetMap offline on the comma and publishes mapdOut: the road's name, its
speed limit (m/s, direction of travel included) and the next one ahead. This file:
- decides when mapd runs: the option on, and the binary on the disk matching MAPD_SHA256 (checked when it runs, not only when
  it was installed: a binary of another schema must never run);
- installs mapd: our own build of mapd 8e5eee6 with 25 msgq reader slots (MAP-02), pinned by URL and sha256;
- decodes mapd's messages from their raw bytes, so a message of another schema is dropped, never raised (decode());
- warns, from selfdrived, when the car goes above the limit plus the tolerance of Res. CONTRAN 798/2020 (7 km/h measured up to
  100 km/h, 7% above), only on a limit that held for SETTLE_S, once per overshoot;
- gives the phone what to show, and the rules for installing and downloading (unmetered network, free space, car off).

The option (HexaSpeedLimit): 0 desligado, 1 mostrar, 2 avisar, 3 ajustar (avisar, and the ACC set speed follows the limit:
LimitFollow below, the + and - of the ACC through the panda, CRUISE-01).
Not driving: it holds for every car.
"""
import hashlib
import json
import os
import stat
import urllib.request
from collections.abc import Callable
from pathlib import Path

OPTION_PARAM = "HexaSpeedLimit"
OFF, SHOW, WARN, ADJUST = 0, 1, 2, 3  # ADJUST warns too, and takes the ACC's set speed to the limit (CRUISE-01)

MAPD_DIR = Path("/data/lizpilot/mapd")
MAPD_BIN = MAPD_DIR / "mapd"
# We build mapd 8e5eee6 (the schema in cereal/custom.capnp) on the Mac and publish it with its hash (owner, 05/11:
# "melhor né, aí temos controle"). Empty: nothing is downloaded and nothing runs.
# 8e5eee6 as published talks to a msgq of 15 reader slots, and ours has 25 (msgq a95f3c1): its queue header was 240 bytes off,
# and loggerd never read a mapdOut (MAP-01). The build carries tools/lizpilot/mapd/0001-msgq-25-reader-slots.patch (MAP-02).
# Back: mapd-8e5eee6, b8b6c8a2180df22833201023e725da990cbc4ec689fdad870fe9276ea9a126fa (the one that never talked)
MAPD_VERSION = "8e5eee6-msgq25"
MAPD_URL = "https://github.com/CPontes/openpilot/releases/download/mapd-8e5eee6-msgq25/mapd"  # tools/lizpilot/mapd/README.md
MAPD_SHA256 = "7305d508e67e738c17b9a9dbbcb5a16076026615f6c233bb1340675ac88abb8b"  # reproducible: two clean builds, same hash
MAX_BINARY = 64 * 1024 * 1024
INSTALL_FREE_BYTES = 200 * 1024 * 1024

# Areas the phone offers (mapd_download_menu.json at the repository root, which mapd reads from /data/openpilot), and the free
# space each asks for before it starts: [hipótese] the sizes of the archives were not measured
AREAS = (("br_state.RJ", "Rio de Janeiro (estado)", 2 * 1024 ** 3), ("nation.BR", "Brasil inteiro", 12 * 1024 ** 3))

KPH = 3.6
TOLERANCE_KPH = 7.  # Res. CONTRAN 798/2020: 7 km/h on a measured speed up to 100 km/h
TOLERANCE_PCT = 0.07  # 7% of the measured speed above it
WARN_AFTER_S = 2.  # above the limit this long before it warns: a passing overshoot is no warning
SETTLE_S = 3.  # a limit counts only after it held this long: a 50 ms drop to 0, or 60/61 flapping, is no new limit
REARM_UNDER_S = 5.  # under the limit this long arms the warning again
MIN_LIMIT_KPH = 20.
STALE_S = 1.5  # no mapdOut for this long: no limit


def tolerance_kph(v_kph: float) -> float:
  return TOLERANCE_KPH if v_kph <= 100 else v_kph * TOLERANCE_PCT


def option(params) -> int:
  try:
    v = params.get(OPTION_PARAM)
    v = int(v) if v is not None else OFF
  except (TypeError, ValueError):
    return OFF
  return v if v in (OFF, SHOW, WARN, ADJUST) else OFF


# *** the binary: checked when it runs ***

_hash_cache: dict[tuple, str] = {}


def file_sha256(path: Path) -> str | None:
  """The sha256 of a file, cached by its size and mtime (process_config asks every manager loop). None if unreadable."""
  try:
    st = path.stat()
  except OSError:
    return None
  key = (str(path), st.st_size, st.st_mtime_ns)
  if key not in _hash_cache:
    h = hashlib.sha256()
    try:
      with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
          h.update(chunk)
    except OSError:
      return None
    _hash_cache.clear()
    _hash_cache[key] = h.hexdigest()
  return _hash_cache[key]


def binary_ok(path: Path = MAPD_BIN, sha256: str | None = None) -> bool:
  """The binary is there, executable, and exactly the pinned one. No pin: never."""
  sha256 = MAPD_SHA256 if sha256 is None else sha256
  if len(sha256) != 64:
    return False
  try:
    if not (path.is_file() and os.access(path, os.X_OK)):
      return False
  except OSError:
    return False
  return file_sha256(path) == sha256.lower()


installed = binary_ok


def should_run(started: bool, params, CP) -> bool:
  """process_config: mapd runs, car on or off (it downloads maps parked), while the option is on and the pinned binary is there."""
  return option(params) != OFF and binary_ok()


# *** mapd's messages, from their raw bytes ***

def decode(raw: bytes | None, which: str) -> dict | None:
  """The fields LizPilot reads from a mapdOut or mapdExtendedOut, or None for anything else: another union, another schema,
  bytes that don't parse. Never raises: a mapd of another version must never take selfdrived or the phone panel down."""
  if not raw:
    return None
  try:
    from openpilot.cereal import log
    with log.Event.from_bytes(raw) as ev:
      if ev.which() != which:
        return None
      m = getattr(ev, which)
      if which == "mapdOut":
        return {"speedLimit": float(m.speedLimit), "nextSpeedLimit": float(m.nextSpeedLimit),
                "nextSpeedLimitDistance": float(m.nextSpeedLimitDistance), "tileLoaded": bool(m.tileLoaded), "roadName": str(m.roadName)[:60]}
      if which == "mapdExtendedOut":
        p = m.downloadProgress
        return {"active": bool(p.active), "done": int(p.downloadedFiles), "total": int(p.totalFiles)}
  except Exception:
    return None
  return None


class SpeedLimitWatch:
  """Warns once when the car stays above the limit plus the tolerance. A limit counts only after it held SETTLE_S (a drop to 0
  for a frame, or a limit flapping 60/61, is no news). The warning arms again only when the limit goes down and holds there, or
  after the car stayed under the limit REARM_UNDER_S."""

  def __init__(self, dt: float):
    self.dt = dt
    self.limit_kph = 0.  # the settled limit
    self.candidate, self.candidate_s = 0., 0.
    self.armed = True
    self.over_s = 0.
    self.under_s = 0.

  def update(self, v_cluster_ms: float, limit_ms: float, fresh: bool) -> bool:
    raw = float(round(limit_ms * KPH)) if fresh and limit_ms > 0 else 0.
    if raw != self.candidate:
      self.candidate, self.candidate_s = raw, 0.
    else:
      self.candidate_s += self.dt
    if self.candidate_s >= SETTLE_S and self.candidate != self.limit_kph:
      if 0 < self.candidate < self.limit_kph:  # a lower limit, held: news
        self.armed = True
      self.limit_kph, self.over_s = self.candidate, 0.
    limit = self.limit_kph
    if limit < MIN_LIMIT_KPH:
      self.over_s = 0.
      return False
    v = v_cluster_ms * KPH
    if v <= limit:
      self.under_s += self.dt
      self.over_s = 0.
      if self.under_s >= REARM_UNDER_S:
        self.armed = True
      return False
    self.under_s = 0.
    if v - tolerance_kph(v) > limit:
      self.over_s += self.dt
    else:
      self.over_s = 0.
    if self.armed and self.over_s >= WARN_AFTER_S:
      self.armed = False
      return True
    return False


last_limit_kph = 0  # what the alert says (events.speed_limit_over_alert)


# *** what the phone shows ***

def view(out: dict | None, age_s: float, option_value: int, is_installed: bool, progress: dict | None = None) -> dict:
  """out: decode(..., "mapdOut"); progress: decode(..., "mapdExtendedOut")."""
  res: dict = {"option": option_value, "installed": is_installed, "fresh": False, "pinned": len(MAPD_SHA256) == 64}
  if out is not None and age_s < STALE_S:
    lim, nxt = out["speedLimit"], out["nextSpeedLimit"]
    res.update({
      "fresh": True,
      "tile": out["tileLoaded"],
      "limit": round(lim * KPH) if lim > 0 else None,
      "next": round(nxt * KPH) if nxt > 0 else None,
      "next_m": round(out["nextSpeedLimitDistance"]) if nxt > 0 else None,
      "road": out["roadName"],
    })
  if progress is not None:
    res["download"] = progress
  return res


# *** installing mapd and its maps: car off, unmetered network, free space ***

class InstallError(Exception):
  """What the phone shows (Portuguese, translated there)."""


def check_conditions(offroad: bool, network_ok: bool, free_bytes: int, need_bytes: int) -> None:
  if not offroad:
    raise InstallError("Só com o carro desligado.")
  if not network_ok:
    raise InstallError("Só num Wi-Fi: o comma não baixa pelos dados móveis.")
  if free_bytes < need_bytes:
    raise InstallError("Falta espaço no comma para o mapa desta região.")


def install(url: str = MAPD_URL, sha256: str = MAPD_SHA256, dest: Path = MAPD_BIN,
            fetch: Callable[[str], bytes] | None = None) -> str:
  """Downloads the pinned mapd, checks its sha256, writes it to the disk for good (fsync), and only then makes it executable."""
  if not url or len(sha256) != 64 or not url.startswith("https://"):
    raise InstallError("Esta versão ainda não traz o mapd: falta a revisão do CTO.")
  if fetch is None:
    def fetch(u: str) -> bytes:
      req = urllib.request.Request(u, headers={"User-Agent": "LizPilot/1"})
      with urllib.request.urlopen(req, timeout=120) as r:
        return r.read(MAX_BINARY + 1)
  try:
    data = fetch(url)
  except Exception as e:
    raise InstallError("Sem internet no comma para baixar o mapd.") from e
  got = hashlib.sha256(data).hexdigest()
  if len(data) > MAX_BINARY or got != sha256.lower():
    raise InstallError("O arquivo baixado não é o mapd esperado.")
  dest.parent.mkdir(parents=True, exist_ok=True)
  tmp = dest.with_suffix(".tmp")
  with open(tmp, "wb") as f:
    f.write(data)
    f.flush()
    os.fsync(f.fileno())
  os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
  os.replace(tmp, dest)
  (dest.parent / "VERSION").write_text(json.dumps({"version": MAPD_VERSION, "sha256": got}) + "\n")
  return got


def area_need(area: str) -> int:
  for key, _, need in AREAS:
    if key == area:
      return need
  raise InstallError("Região desconhecida.")


def mapd_in(kind: str, text: str = ""):
  """A mapdIn message: "download" an area from AREAS, or "cancelDownload"."""
  import time
  from openpilot.cereal import log
  msg = log.Event.new_message(logMonoTime=int(time.monotonic() * 1e9), valid=True)  # as messaging.new_message builds it
  m = msg.init("mapdIn")
  m.type = kind
  m.str = text
  return msg


def download_message(area: str):
  area_need(area)
  return mapd_in("download", area)


class DownloadGuard:
  """Stops a map download when the car turns on: mapd downloads with the car off only. Asks once per start."""

  def __init__(self):
    self.sent = False

  def update(self, onroad: bool, downloading: bool) -> bool:
    if not onroad:
      self.sent = False
      return False
    if downloading and not self.sent:
      self.sent = True
      return True
    return False


# *** "ajustar": the ACC's set speed follows the road's limit (CRUISE-01) ***

FOLLOW_MIN_KPH = 30  # the ACC's lowest set speed
FOLLOW_MAX_KPH = 110  # the panda's ceiling for the plus (gwm.h GWM_PLUS_MAX_KPH)
FOLLOW_STEP_KPH = 5  # one press of + or - (18 of 20 and 6 of 6 presses measured)
FOLLOW_MAX_RAISE_KPH = 40  # per limit: the panda's budget is 8 presses of the plus a minute
FOLLOW_TIMEOUT_S = 15.
FOLLOW_NO_RESPONSE_S = 2.  # pressing with the set speed not moving: the ACC isn't taking it


class LimitFollow:
  """Takes the ACC's set speed to a new limit, a press at a time: +1 (plus), -1 (minus) or 0, at the rate it is called.

  Once per limit: a limit that held (SpeedLimitWatch) and is new starts it; the set speed at the limit (on the 5 km/h steps
  of the ACC, between FOLLOW_MIN_KPH and FOLLOW_MAX_KPH, at most FOLLOW_MAX_RAISE_KPH up) ends it. The driver always wins:
  the gas, the brake or the set speed moving the other way end it for this limit, and it waits for the next one. With
  openpilot or the ACC off, or a bump slowing the car, it waits and goes on when they're back.
  """

  def __init__(self, dt: float):
    self.dt = dt
    self.done_for = 0.  # the last limit it finished, or that the driver took over
    self.target: float | None = None
    self.set_prev: float | None = None
    self.left = 0.
    self.stuck = 0.
    self.direction = 0  # the way this adjustment goes: it never turns back (no + then -)

  def _stop(self, limit_kph: float) -> int:
    self.done_for, self.target, self.set_prev = limit_kph, None, None
    return 0

  def update(self, enabled: bool, acc_driving: bool, moving: bool, set_kph: float, limit_kph: float, gas_pressed: bool,
             brake_pressed: bool, slowing_for_bump: bool) -> int:
    if limit_kph < FOLLOW_MIN_KPH or limit_kph == self.done_for:
      self.target, self.set_prev = None, None
      return 0
    if not (enabled and acc_driving and moving) or slowing_for_bump:
      self.target, self.set_prev = None, None  # waits: goes on when they're back
      return 0
    if gas_pressed or brake_pressed:
      return self._stop(limit_kph)
    if self.target is None:
      target = FOLLOW_STEP_KPH * round(min(max(limit_kph, FOLLOW_MIN_KPH), FOLLOW_MAX_KPH) / FOLLOW_STEP_KPH)
      self.target = min(target, set_kph + FOLLOW_MAX_RAISE_KPH)
      self.left, self.stuck, self.set_prev, self.direction = FOLLOW_TIMEOUT_S, 0., None, 0
    # a whole step away, or nothing: a set speed off the 5 km/h steps (78 for 80) stays as it is
    direction = 1 if self.target - set_kph >= FOLLOW_STEP_KPH else -1 if set_kph - self.target >= FOLLOW_STEP_KPH else 0
    if self.direction == 0:
      self.direction = direction
    elif direction != self.direction:
      direction = 0  # there, or past it: done
    if self.set_prev is not None:
      moved = set_kph - self.set_prev
      if moved * direction < 0:  # the other way: the driver
        return self._stop(limit_kph)
      self.stuck = 0. if moved != 0 else self.stuck + self.dt
    self.left -= self.dt
    if direction == 0 or self.left <= 0 or self.stuck >= FOLLOW_NO_RESPONSE_S:
      return self._stop(limit_kph)
    self.set_prev = set_kph
    return direction
