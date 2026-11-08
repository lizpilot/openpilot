#!/usr/bin/env python3
"""LizPilot (Hexa) phone panel: a small local web page to see the comma and change the fork's options.

Runs only while the "painel no celular" option (HexaWebEnabled) is on. Security rules live in
openpilot/selfdrive/hexa/phone.py; this file only parses HTTP and calls PanelBackend.
"""
import gzip
import hashlib
import json
import math
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from openpilot.selfdrive.hexa import comma_ctl
from openpilot.selfdrive.hexa import edition as hexa_edition
from openpilot.selfdrive.hexa import i18n
from openpilot.selfdrive.hexa import live as hexa_live
from openpilot.selfdrive.hexa import marks as hexa_marks
from openpilot.selfdrive.hexa import mdns as hexa_mdns
from openpilot.selfdrive.hexa import missions as hexa_missions
from openpilot.selfdrive.hexa import nav as hexa_nav
from openpilot.selfdrive.hexa import radars
from openpilot.selfdrive.hexa import report as hexa_report
from openpilot.selfdrive.hexa import share as hexa_share
from openpilot.selfdrive.hexa import speed_limit
from openpilot.selfdrive.hexa import phone
from openpilot.selfdrive.hexa import timeline
from openpilot.selfdrive.hexa import updates
from openpilot.selfdrive.hexa import versions as hexa_versions
from openpilot.selfdrive.hexa.options import GROUPS, LONG_CALIBRATED, OPTIONS, get_option, supported
from openpilot.selfdrive.hexa.phone import VehicleState

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 4096
MAX_IMPORT_BODY = 256 * 1024  # a saved copy of the radar points
STATIC_FILES = {
  "/": ("index.html", "text/html; charset=utf-8"),
  "/index.html": ("index.html", "text/html; charset=utf-8"),
  "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
  "/replay": ("replay.html", "text/html; charset=utf-8"),
  "/viagens": ("viagens.html", "text/html; charset=utf-8"),
  "/icon-180.png": ("icon-180.png", "image/png"),
  "/icon-192.png": ("icon-192.png", "image/png"),
  "/icon-512.png": ("icon-512.png", "image/png"),
  # where iOS and browsers look on their own, whatever page was saved to the home screen (owner, 03/11: a plain "L")
  "/apple-touch-icon.png": ("icon-180.png", "image/png"),
  "/apple-touch-icon-precomposed.png": ("icon-180.png", "image/png"),
  "/favicon.ico": ("favicon.png", "image/png"),
  "/favicon.png": ("favicon.png", "image/png"),
  "/lp.css": ("lp.css", "text/css; charset=utf-8"),
  "/i18n.js": ("i18n.js", "text/javascript; charset=utf-8"),
  "/lp-logic.js": ("lp-logic.js", "text/javascript; charset=utf-8"),
  "/fonts/barlow-400.woff2": ("fonts/barlow-400.woff2", "font/woff2"),
  "/fonts/barlow-600.woff2": ("fonts/barlow-600.woff2", "font/woff2"),
  "/fonts/barlow-700.woff2": ("fonts/barlow-700.woff2", "font/woff2"),
  "/fonts/barlow-semi-condensed-700.woff2": ("fonts/barlow-semi-condensed-700.woff2", "font/woff2"),
  # the same catalogs the comma screen uses (selfdrive/hexa/i18n.py)
  "/i18n/en.json": (i18n.CATALOG_DIR / "en.json", "application/json; charset=utf-8"),
  "/i18n/es.json": (i18n.CATALOG_DIR / "es.json", "application/json; charset=utf-8"),
}
CAR_ROUTE_KEYS = ("active", "next", "type", "modifier", "in_m", "left_m", "left_s", "path")  # what the car's screen draws (NAV-03)
ICON_CACHE = {"Cache-Control": "public, max-age=604800"}  # the home screen keeps the icon: "no-store" may leave it out
REPLAY_CACHE = 3
TRIP_CACHE = 120  # minutes of trips kept in memory
TRIP_INDEX_S = 30.  # the page asks once per minute of the trip: don't walk the log folder every time
SUMMARY_PARAM = "HexaTripSummaries"  # {route: who had the wheel, for how long and how far}
SUMMARY_NEWEST = 300  # drives summarized in the background, newest first, with the car off: the whole history on the comma
SUMMARY_KEEP = 300
COP_KEEP = 10  # the copilot's events stay in the newest drives only: "A conferir" is about the last drives, and the param stays small
TRIPS_LISTED = 300
PATH_DIR = "hexa_trips"  # next to the log folder: each drive's path, for the map on the phone. Never leaves the comma
PATH_PRUNE_S = 600.  # paths of drives the comma already deleted go too, looked at this often, car off
HIDDEN_PARAM = "HexaTripsHidden"  # drives the owner hid from the phone's lists: the logs stay on the comma
HIDDEN_KEEP = 600
PRESERVE_ATTR = "user.preserve"  # system/loggerd/deleter.py: what keeps a flagged minute from being deleted
SUMMARY_VERSION = 8  # 2: version, alerts. 3: the path. 4: the KPIs (wobble, lane seen, where it let go).
# 5: time in slow traffic (the missions). 6: the copilot's events (cop), for "A conferir", and the drive's clock (t0).
# 7: the car's signals seen changing (sg_*), for the comma approval checks (MISS-01). 8: what the comma learned of the car
# (cb_*: camera calibration, steer ratio, torque, wheels against the GPS), for the calibration trail.
# An older summary is read again, the newest SUMMARY_NEWEST only
# ...unless what came after it only matters to the newest drives: version -> how many newest drives still need the read.
# 6 only added the copilot's events, kept in the newest COP_KEEP (CTO review of app-12: reading 300 drives again meant hours
# with no numbers on the phone, and the param rewritten 300 times). Take an entry out when a version adds more than that
# 7 and 8 only added the signals and the learned values, which matter to the newest drives: those SIGNALS_KEEP are read again
SIGNALS_KEEP = 10
SUMMARY_STILL_GOOD = {5: COP_KEEP, 6: SIGNALS_KEEP, 7: SIGNALS_KEEP}
ERRORS_EVERY_S = 30.
# The clip of a mark starts before it: the owner marks after it happened (owner, 06/11: "quando acontece um evento, normalmente
# marca-se o que aconteceu"). The mark's own window says how far back; with none ("Agora", the comma's flag, the copilot),
# 30 s, the same as Marcar's "30 s antes" and the log sent to the developers (30 s before, 15 after)
CLIP_BEFORE_S = 30.
CLIP_MARGIN_S = 10.  # past a "10 s antes" / "30 s antes" window: a little more, for when the owner guessed short
CLIP_LEAD_S = 5.  # "30 s depois": from just before the mark
CLIP_AFTER_S = 15.  # at least this much after the mark, so the next minute comes along when the mark is at its end
CLIP_TICKET_S = 600.  # a clip link works this long, for that clip only
FIXED_NONCE_S = 60.  # a same-comma check for lizpilot.local works this long, once
NET_GUARD_MIN_S = 10.  # the phone counts as back on the new network only after this long
MAX_FIXED_NONCES = 8
CLIP_VIDEO = "qcamera.ts"  # the light video loggerd keeps for each minute
SEGMENT_S = 60.
PROFILES_PARAM = "HexaOptionProfiles"  # {name: {option key: value}}, saved by the owner from the phone
MAX_PROFILES = 6
MAX_PROFILE_NAME = 20
GZIP_MIN_BYTES = 16 * 1024  # segments kept in memory
SERVE_RETRY = 5.  # s between tries when the port can't be opened
SHARE_EVERY_S = 20.  # the share worker's turn: one cut or one send
LIMIT_EVERY_S = 2.  # a map download stops this soon after the car turns on
SECURITY_HEADERS = {
  "Cache-Control": "no-store",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "no-referrer",
  # connect-src: a page opened by address may check that the fixed name answers on this network, and nothing else
  "Content-Security-Policy": "; ".join(("default-src 'self'", "script-src 'self' 'unsafe-inline'", "style-src 'self' 'unsafe-inline'",
                                         "img-src 'self' data: https://tile.openstreetmap.org", f"connect-src 'self' http://{hexa_mdns.NAME}:{phone.PORT}")),
}


class MapJob:
  """A map download as the phone sees it, from the tap to the end (UI-19, owner 07/11: "começou, e depois não tinha como
  acompanhar"). mapd only says how many files it has, about once a second (mapdExtendedOut.downloadProgress; view() drops it
  after 10 s without one). The ask, the end, why it stopped and mapd's silence are kept here.

  idle → asked → downloading → done (every file), or stopped (car: the car turned on; you: Parar; network, space or partial:
  files missing at the end, mapd skips a file it can't get). silent: no word from mapd START_S after the ask (start), or in
  the middle of a download (middle); a word from it goes back to downloading. refused: the comma said no before asking."""
  START_S = 20.  # mapd answers a download in about a second (the first progress, then one a second)

  def __init__(self):
    self.state, self.area, self.asked, self.done, self.total, self.reason, self.error, self.stop_asked = "idle", "", 0., 0, 0, "", "", ""

  def ask(self, area: str, now: float) -> None:
    self.state, self.area, self.asked, self.done, self.total, self.reason, self.error, self.stop_asked = "asked", area, now, 0, 0, "", "", ""

  def refuse(self, area: str, error: str) -> None:
    if self.state not in ("asked", "downloading"):
      self.state, self.area, self.error, self.reason = "refused", area, error, ""

  def stop(self, why: str) -> None:
    if self.state in ("asked", "downloading", "silent"):
      self.stop_asked = why

  def update(self, progress: dict | None, now: float, network_ok: Callable[[], bool], room: Callable[[], bool]) -> dict | None:
    """progress: decode(..., "mapdExtendedOut") if fresh, else None. Returns {"area", "files"} when a download just ended whole."""
    p = progress
    if self.state == "asked" and not (p and p["active"]):
      if self.stop_asked:
        self.state, self.reason = "stopped", self.stop_asked
      elif now - self.asked >= self.START_S:  # an old download's progress, or none at all: mapd didn't take the ask
        self.state, self.reason = "silent", "start"
      return None
    if self.state == "silent":
      if p is None or (self.reason == "start" and not p["active"]):
        return None
      self.reason = ""
    if self.state not in ("asked", "silent", "downloading"):
      return None
    if p is None:
      self.state, self.reason = "silent", "middle"
      return None
    self.state, self.done, self.total = "downloading", p["done"], p["total"]
    if p["active"]:
      return None
    if self.stop_asked:
      self.state, self.reason = "stopped", self.stop_asked
    elif self.total > 0 and self.done >= self.total:
      self.state = "done"
      return {"area": self.area, "files": self.total}
    else:
      self.state, self.reason = "stopped", "network" if not network_ok() else "space" if not room() else "partial"
    return None

  def view(self) -> dict:
    return {"state": self.state, "area": self.area, "done": self.done, "total": self.total, "reason": self.reason, "error": self.error}


def clip_window(mark: dict) -> tuple[float, float]:
  """Seconds of video [before, after] the mark, from the window chosen in Marcar (marks.py clean_window)."""
  win = hexa_marks.clean_window(mark.get("win"))
  if not win:
    return CLIP_BEFORE_S, CLIP_AFTER_S  # "Agora", or a mark with no window
  back, ahead = win
  before = back + CLIP_MARGIN_S if back else CLIP_LEAD_S  # "30 s depois": the mark is where it starts
  return before, max(CLIP_AFTER_S, ahead + CLIP_MARGIN_S)


def clip_minutes(seg: int, offset: float, before: float, after: float, has: Callable[[int], bool]) -> tuple[int, int]:
  """The first and last minute of the clip: every minute from `before` the mark to `after` it, next to each other.
  A minute that isn't there (the drive's start or end, or cleaned up) stops the clip there."""
  first = seg
  while first > seg + math.floor((offset - before) / SEGMENT_S) and has(first - 1):
    first -= 1
  last = seg
  while last < seg + math.floor((offset + after) / SEGMENT_S) and has(last + 1):
    last += 1
  return first, last


class PanelBackend:
  def __init__(self, store, vehicle_state: Callable[[], VehicleState],
               clock: Callable[[], float] = phone.wall_time, mono: Callable[[], float] = time.monotonic,
               log_event: Callable[[dict], None] | None = None, log_root: str | None = None,
               signal_updater: Callable[[str], None] = updates.signal_updater,
               live_state: Callable[[], dict] | None = None, radar_path: Path | None = None,
               gps_fix: Callable[[], tuple[float, float, float, float] | None] | None = None,
               health_state: Callable[[], dict] | None = None,
               summarize: Callable[[dict], dict] = timeline.trip_summary, spawn: Callable[[Callable[[], None]], None] | None = None,
               sharer: hexa_share.Sharer | None = None,
               navigator: hexa_nav.Navigator | None = None, route_state: Callable[[], dict] | None = None,
               limit_state: Callable[[], dict] | None = None, send_mapd: Callable[[object], None] | None = None,
               install_mapd: Callable[[], str] = speed_limit.install, network_ok: Callable[[], bool] | None = None,
               free_bytes: Callable[[], int] | None = None, network_kind: Callable[[], str] | None = None,
               maps_path: Path | None = None):
    self.store = store  # Params, or anything with get/put/get_bool/put_bool/remove
    self._vehicle_state = vehicle_state
    self.clock = clock  # wall clock: token, audit and mark dates
    self.mono = mono  # monotonic clock, same as logMonoTime: pairing code expiry, bookmark matching
    self.log_event = log_event or (lambda fields: None)  # writes a 'hexa_mark' event to the log
    self.lock = threading.Lock()
    self._vs_lock = threading.Lock()  # requests run in threads; SubMaster is not thread safe
    self.pin_failures = 0
    self._last_mark: dict[str, float] = {}  # token id -> monotonic time of the last phone mark
    self._car_challenges = phone.Challenges(lambda: self.mono())  # the car's screen (Android Auto): read the route without the token
    self.log_root = log_root
    self._replay_cache: OrderedDict[str, dict] = OrderedDict()
    self._replay_lock = threading.Lock()
    self._trip_cache: OrderedDict[str, dict] = OrderedDict()
    self._trip_index: tuple[float, list[dict]] = (-TRIP_INDEX_S, [])
    self.signal_updater = signal_updater
    self.radar_path = radar_path
    self._radar_job = {"busy": False, "error": ""}
    self._radar_rows: tuple[tuple, object] = ((), None)
    self._gps_fix = gps_fix or (lambda: None)
    self._radar_pins: dict[str, tuple] = {}
    self._live_state = live_state or dict
    self._health_state = health_state or dict
    self._was_engaged = False
    self._last_off: dict | None = None
    self._summarize = summarize
    self._spawn = spawn or (lambda job: threading.Thread(target=job, daemon=True).start())
    self._summary_busy = False
    self._paths_pruned = -1e9
    self._clips: dict[str, dict] = {}  # ticket -> {expires, files, start, name}
    self._fixed_nonces: dict[str, tuple] = {}  # one-time code -> (expiry, answer, phone), to prove lizpilot.local is this comma
    self.fixed_status: Callable[[], dict] = hexa_mdns.status
    self._boot = secrets.token_hex(4)  # a new process starts new versions: the phone asks for everything once
    self._revs: dict[str, int] = {}  # param -> how many times this process wrote it
    self._todo_left = False  # drives still to summarize: a same version must not stop asking for them
    self._catalog_v = ""
    # what each listed drive looked like when last sent, and the change number of this process that last changed it:
    # the trips delta never depends on the comma's clock (CTO review of app01-01)
    self._sync_lock = threading.Lock()
    self._trip_sigs: dict[str, tuple[str, int]] = {}
    self._trip_gen = 0
    self.sharer = sharer  # sending a mark's moment to the developers (selfdrive/hexa/share.py)
    self.navigator = navigator or hexa_nav.Navigator(store)  # where to go: openpilot's own navigation takes it from there
    self._route_state = route_state or (lambda: {"active": False})
    # the road's limit from the map (selfdrive/hexa/speed_limit.py): what mapd says, and asking it for a region's maps
    self._limit_state = limit_state or (lambda: speed_limit.view(None, 1e9, speed_limit.option(self.store), speed_limit.installed()))
    self._send_mapd = send_mapd or (lambda msg: None)
    self._install_mapd = install_mapd
    self._network_ok = network_ok or (lambda: False)  # an unmetered network (deviceState)
    self._free_bytes = free_bytes or (lambda: 0)  # free space under /data
    self._limit_job = {"busy": False, "error": "", "ok": False}
    self._download_guard = speed_limit.DownloadGuard()
    self._network_kind = network_kind or (lambda: "")  # "wifi", "metered", "none", or "" before the first deviceState
    self._maps_path = maps_path or speed_limit.MAPD_DIR / "maps.json"  # the maps downloaded whole: area -> date and files
    self._maps = MapJob()
    self._maps_lock = threading.Lock()

  def vehicle_state(self) -> VehicleState:
    with self._vs_lock:
      try:
        return self._vehicle_state()
      except Exception:
        return VehicleState(onroad=True)  # unknown: treat as driving, read only

  # *** helpers ***

  def _tokens(self) -> list[dict]:
    return self.store.get(phone.TOKENS_PARAM) or []

  def _audit(self, entry: dict) -> None:
    self.store.put(phone.AUDIT_PARAM, phone.add_audit(self.store.get(phone.AUDIT_PARAM), entry, self.clock()))

  def _value(self, opt):
    if opt.kind == "bool":
      return bool(self.store.get_bool(opt.key))
    v = self.store.get(opt.key)
    return opt.default if v is None else int(v)

  def live(self) -> dict:
    with self._vs_lock:
      try:
        data = self._live_state()
      except Exception:
        data = {}
    return {**data, "last_off": self._last_off}

  def health(self) -> dict:
    with self._vs_lock:
      try:
        data = self._health_state()
      except Exception:
        data = {}
    now = self.clock()
    errors = hexa_report.annotate(list(data.get("errors") or []), hexa_report.clock_floor(self.store, now), now)
    sent = self.sharer.reports() if self.sharer is not None else []
    return {**data, "errors": errors, "last_off": self._last_off,
            "reports": {"sent": len(sent), "receipt": sent[-1].get("receipt", "") if sent else ""}}

  def on_drive_state(self, engaged: bool, events) -> None:
    """Notes why openpilot dropped out, even with no phone looking."""
    if self._was_engaged and not engaged:
      self._last_off = {"t": int(self.clock()), "why": hexa_live.why_not(events)}
    self._was_engaged = engaged

  # *** API ***

  def authenticate(self, token: str | None) -> dict | None:
    with self.lock:
      tokens = self._tokens()
      who = phone.find_token(tokens, token)
      if who is not None:  # its last use, at most once an hour: the list keeps the phones in use, and shows when each was
        touched = phone.touch_token(tokens, who.get("id", ""), self.clock())
        if touched is not None:
          self.store.put(phone.TOKENS_PARAM, touched)
    return who

  def ident(self, phone_id: str, nonce: str) -> tuple[int, dict]:
    """The LizPilot app asks if this is the comma it paired with (phone.ident_answer): no token, opens nothing."""
    with self.lock:
      out = phone.ident_answer(self._tokens(), phone_id, nonce)
    return (400, {"error": "Valor inválido."}) if out is None else (200, out)

  def car_route(self, phone_id: str, challenge: str, mac: str, who: str) -> tuple[int, dict]:
    """The car's screen (Android Auto, NAV-03): the route, read without the token by the app that answers the comma's
    challenge with its pairing's hash (phone.car_reader). Each challenge goes to the address that asked (who), and only
    that address can use it. Only reading, and only what the car's screen draws while a route guides: the next turn, the
    arrival, the path, the destination and the car. With no route, not even where the car is (CTO review of navcel-02,
    item 4). Every answer, yes or no, carries the next challenge, so the app polls with one request."""
    with self.lock:
      ok = self._car_challenges.redeem(challenge, who) and phone.car_reader(self._tokens(), phone_id, challenge, mac) is not None
      nxt = self._car_challenges.issue(who)
    if not ok:
      return 401, {"v": 1, "n": nxt}
    s = self.nav_state()
    route, d = s.get("route") or {}, s.get("destination")
    if not (route.get("active") and d):
      return 200, {"v": 1, "n": nxt, "route": {"active": False}, "car": None, "destination": None, "computing": bool(d)}
    return 200, {"v": 1, "n": nxt, "route": {k: route[k] for k in CAR_ROUTE_KEYS if k in route}, "car": s.get("near"), "computing": False,
                 # the comma connect's destination has only the point (athenad.py): no name is fine (review item 2)
                 "destination": {"name": str(d.get("place_name") or ""), "latitude": d["latitude"], "longitude": d["longitude"]}}

  def pair(self, pin: str, name: str) -> tuple[int, dict]:
    with self.lock:
      stored = self.store.get(phone.PIN_PARAM)
      if phone.parse_pin(stored, self.mono()) is None:
        return 403, {"error": "Gere um código no comma: Configurações → LizPilot → parear celular."}
      if not phone.pin_matches(stored, pin, self.mono()):
        self.pin_failures += 1
        if self.pin_failures >= phone.MAX_PIN_FAILURES:
          self.store.remove(phone.PIN_PARAM)
          self.pin_failures = 0
          self._audit({"event": "pin_locked"})
          return 403, {"error": "Código errado várias vezes. Gere um novo código no comma."}
        return 403, {"error": "Código errado."}
      self.pin_failures = 0
      self.store.remove(phone.PIN_PARAM)  # single use
      before = self._tokens()
      token, tokens = phone.issue_token(before, name, self.clock())
      self.store.put(phone.TOKENS_PARAM, tokens)
      gone = phone.dropped(before, tokens)
      self._audit({"event": "paired", "who": tokens[-1]["name"], **({"new": ", ".join(t.get("name", "") for t in gone)} if gone else {})})
      return 200, {"token": token, "name": tokens[-1]["name"], "removed": [t.get("name", "") for t in gone]}

  def state(self, who: dict) -> dict:
    vs = self.vehicle_state()
    groups = []
    with self.lock:
      for gkey, gtitle in GROUPS:
        items = []
        for opt in OPTIONS:
          if opt.group != gkey or not supported(opt, self.store):
            continue
          writable, reason = phone.can_write(opt, vs)
          items.append({
            "key": opt.key, "title": opt.title, "description": opt.description, "kind": opt.kind,
            "choices": list(opt.choices), "value": self._value(opt), "writable": writable, "reason": reason,
            "restart_needed": opt.restart_needed, "testing": opt.testing,
          })
        if items:
          groups.append({"key": gkey, "title": gtitle, "options": items})
      audit = (self.store.get(phone.AUDIT_PARAM) or [])[-20:]
      return {
        "device": {
          "branch": self.store.get("GitBranch") or "",
          "commit": (self.store.get("GitCommit") or "")[:7],
          "version": self.store.get("Version") or "",
          "edition": hexa_edition.EDITION,  # the phone's first Usar | Melhorar: Usar on LizPilot for every car
        },
        "vehicle": {"onroad": vs.onroad, "parked": vs.parked, "standstill": vs.standstill, "engaged": vs.engaged},
        "groups": groups,
        "audit": list(reversed(audit)),
        "titles": {**{i.key: i.title for i in comma_ctl.ITEMS}, **{o.key: o.title for o in OPTIONS}},
        "long_steps": [{"key": k, "label": label, "done": k == "calibrado" and LONG_CALIBRATED} for k, label in comma_ctl.LONG_STEPS],
        "profiles": self.store.get(PROFILES_PARAM) or {},
        # what a speed bump below the Haval ACC's 30 km/h would need: shown in the bump editor, changes nothing
        "longitudinal": {"enabled": bool(self.store.get_bool("AlphaLongitudinalEnabled")), "calibrated": LONG_CALIBRATED},
        "fixed": self.fixed_status(),
        "you": who.get("name", ""),
      }

  def set_option(self, who: dict, key: str, value) -> tuple[int, dict]:
    opt = get_option(key)
    if opt is None or not supported(opt, self.store):
      return 404, {"error": "Opção desconhecida."}
    try:
      value = opt.coerce(value)
    except ValueError as e:
      return 400, {"error": str(e)}
    vs = self.vehicle_state()
    with self.lock:
      ok, reason = phone.can_write(opt, vs)
      old = self._value(opt)
      if not ok:
        self._audit({"event": "denied", "who": who.get("name", ""), "key": key, "new": value, "reason": reason})
        return 409, {"error": reason}
      if opt.kind == "bool":
        self.store.put_bool(key, value)
      else:
        self.store.put(key, value)
      self._audit({"event": "set", "who": who.get("name", ""), "key": key, "old": old, "new": value})
      msg = "Vale na próxima partida do carro." if opt.restart_needed else "Vale no próximo engate."
      return 200, {"ok": True, "message": msg}

  # *** the comma's own settings, from the phone (UI-09) ***

  def _release(self) -> bool:
    return bool(self.store.get_bool("IsReleaseBranch"))

  def comma_state(self) -> dict:
    vs = self.vehicle_state()
    release = self._release()
    items = []
    for it in comma_ctl.ITEMS:
      if not comma_ctl.visible(it, release):
        continue
      ok, reason = comma_ctl.can_change(it, vs, self.store)
      items.append({"key": it.key, "group": it.group, "title": it.title, "description": it.description, "level": it.level,
                    "value": bool(self.store.get_bool(it.key)), "writable": ok, "reason": reason, "effect": comma_ctl.effect(it),
                    "warnings": list(it.warnings), "blocked": it.blocked})
    off = not vs.onroad
    return {"groups": [list(g) for g in comma_ctl.GROUPS], "items": items, "onroad": vs.onroad,
            "actions": [{"action": a, "title": t, "description": d, "allowed": off} for a, t, d in comma_ctl.DEVICE_ACTIONS],
            "long": {"steps": [{"key": k, "label": label, "done": k == "calibrado" and LONG_CALIBRATED} for k, label in comma_ctl.LONG_STEPS]}}

  def comma_set(self, who: dict, key: str, value) -> tuple[int, dict]:
    it = comma_ctl.get_item(key)
    if it is None or not comma_ctl.visible(it, self._release()):
      return 404, {"error": "Ajuste desconhecido."}
    if not isinstance(value, bool):
      return 400, {"error": "Valor inválido."}
    ok, reason = comma_ctl.can_change(it, self.vehicle_state(), self.store)
    if not ok:
      return 409, {"error": reason}
    with self.lock:
      old = bool(self.store.get_bool(key))
      comma_ctl.apply(it, value, self.store)
      self._audit({"event": "comma_set", "who": who.get("name", ""), "key": key, "old": old, "new": value})
    return 200, {"ok": True, "message": comma_ctl.effect(it)}

  def comma_action(self, who: dict, action: str) -> tuple[int, dict]:
    if action not in {a for a, _, _ in comma_ctl.DEVICE_ACTIONS}:
      return 404, {"error": "Ação desconhecida."}
    if self.vehicle_state().onroad:
      return 409, {"error": "Desligue o carro."}
    with self.lock:
      self._audit({"event": "comma_action", "who": who.get("name", ""), "new": action})
      if action == "reboot":
        self.store.put_bool("DoReboot", True)
        return 200, {"ok": True, "message": "Reiniciando. O painel volta sozinho em cerca de 1 minuto."}
      if action == "poweroff":
        self.store.put_bool("DoShutdown", True)
        return 200, {"ok": True, "message": "Desligando o comma."}
      for key in comma_ctl.RECALIBRATE_REMOVES:
        self.store.remove(key)
      self.store.put_bool("OnroadCycleRequested", True)
    return 200, {"ok": True, "message": "Calibração apagada. O comma recalibra na próxima viagem."}

  # *** lizpilot.local is this comma: anyone on the network can answer for a name, so the page checks ***

  def fixed_nonce(self, peer: str) -> dict:
    """A one-time code and its answer, from the comma that served the page. Only this comma knows the answer, and it only
    gives it to the phone that asked: a fake on the network can neither make it up nor relay the question from its own address."""
    st = self.fixed_status()
    if st.get("state") != "ok":
      return {"nonce": None, "state": st.get("state")}
    now = self.mono()
    with self.lock:
      self._fixed_nonces = {k: v for k, v in self._fixed_nonces.items() if v[0] > now}
      while len(self._fixed_nonces) >= MAX_FIXED_NONCES:
        self._fixed_nonces.pop(next(iter(self._fixed_nonces)))
      nonce, answer = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
      self._fixed_nonces[nonce] = (now + FIXED_NONCE_S, answer, peer)
    return {"nonce": nonce, "answer": answer, "state": "ok"}

  def fixed_check(self, nonce: str, peer: str) -> dict:
    with self.lock:
      entry = self._fixed_nonces.pop(nonce, None)  # once
    return {"answer": entry[1] if entry is not None and entry[0] > self.mono() and entry[2] == peer else None}

  # *** the clip of a mark: the light video of that moment, on the phone, with the car off ***

  def clip_ticket(self, who: dict, mark_id: str) -> tuple[int, dict]:
    """A short-lived link to the minutes of video around a mark. The phone's video player can't send the token,
    so it gets a ticket good for this clip only, for 10 minutes."""
    if self.vehicle_state().onroad:
      return 409, {"error": "As viagens só abrem com o carro desligado."}
    with self.lock:
      mark = next((m for m in self._marks() if m.get("id") == mark_id), None)
    if mark is None or not mark.get("t"):
      return 404, {"error": "Marca não encontrada."}
    t = float(mark["t"])
    before, after = clip_window(mark)
    with self._replay_lock:
      trips = self._trips()
    for trip in trips:
      segs = sorted(trip["files"])
      videos = {n: Path(trip["files"][n]).parent / CLIP_VIDEO for n in segs}
      try:
        ends = {n: Path(trip["files"][n]).stat().st_mtime for n in segs}  # the minute's log closes when the minute ends
      except OSError:
        continue  # the comma cleaned this drive up after the list was read
      seg = next((n for n in segs if ends[n] - SEGMENT_S - 2 <= t <= ends[n] + 2), None)
      if seg is None:
        continue
      if not videos[seg].is_file():
        break
      offset = max(0., t - (ends[seg] - SEGMENT_S))
      first, last = clip_minutes(seg, offset, before, after, lambda n, v=videos: n in v and v[n].is_file())
      parts = list(range(first, last + 1))
      mark_at = offset + (seg - first) * SEGMENT_S  # where the mark is in the clip (and in the file to download)
      start = max(0., mark_at - before)  # without the minute before (cleaned up, or the drive's first), from the mark's minute
      ticket = secrets.token_urlsafe(18)
      now = self.mono()
      self._clips = {k: v for k, v in self._clips.items() if v["expires"] > now}
      while len(self._clips) >= 8:
        self._clips.pop(next(iter(self._clips)))
      self._clips[ticket] = {"expires": now + CLIP_TICKET_S, "files": [str(videos[n]) for n in parts], "start": start,
                             "name": f"lizpilot-marca-{time.strftime('%Y%m%d-%H%M%S', time.localtime(t))}"}
      # video: every minute of the clip in one file. The minutes' timestamps run on from one to the next
      # (loggerd writes the frame's own time), so they play as one video
      return 200, {"ticket": ticket, "playlist": f"/api/clip.m3u8?ticket={ticket}", "video": f"/api/clip.ts?ticket={ticket}&download=1",
                   "start": round(start, 1), "mark_at": round(mark_at, 1), "lead": round(mark_at - start, 1),
                   "short": mark_at - start < before - 1,  # the minute before isn't on the comma (anymore)
                   "seconds": len(parts) * SEGMENT_S, "minutes": len(parts)}
    return 404, {"error": "Sem vídeo para esta marca."}

  def clip(self, ticket: str) -> dict | None:
    c = self._clips.get(ticket)
    return c if c is not None and c["expires"] > self.mono() and not self.vehicle_state().onroad else None  # never with the car on

  def clip_playlist(self, ticket: str) -> str | None:
    c = self.clip(ticket)
    if c is None:
      return None
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{int(SEGMENT_S) + 1}", "#EXT-X-PLAYLIST-TYPE:VOD",
             f"#EXT-X-START:TIME-OFFSET={c['start']:.1f},PRECISE=YES"]
    for i in range(len(c["files"])):
      lines += [f"#EXTINF:{SEGMENT_S:.1f},", f"/api/clip.ts?ticket={ticket}&n={i}"]
    return "\n".join(lines + ["#EXT-X-ENDLIST", ""])

  # *** a copy of the options, and profiles ***

  def _phone_options(self) -> list:
    return [o for o in OPTIONS if supported(o, self.store) and o.phone_writable]

  def options_export(self) -> dict:
    """The options the phone can change, in a file of the owner's: a reset of the comma erases them. No positions, no logs."""
    with self.lock:
      return {"exported_at": int(self.clock()), "options": {o.key: self._value(o) for o in self._phone_options()},
              "profiles": self.store.get(PROFILES_PARAM) or {}}

  def options_changes(self, raw) -> tuple[list[dict], list[str]]:
    """What a copy or a profile would change: [{key, old, new}], and the keys it can't use (unknown here, bad value)."""
    changes, skipped = [], []
    if not isinstance(raw, dict):
      return changes, skipped
    known = {o.key: o for o in self._phone_options()}
    for key, value in raw.items():
      opt = known.get(key)
      if opt is None:
        skipped.append(str(key)[:40])
        continue
      try:
        value = opt.coerce(value)
      except ValueError:
        skipped.append(key)
        continue
      old = self._value(opt)
      if value != old:
        changes.append({"key": key, "old": old, "new": value})
    return changes, skipped

  def profiles_changes(self, raw) -> tuple[list[dict], dict, list[str]]:
    """What the profiles in a copy would change: [{name, new}], all the profiles after it, and the names left out.
    A profile with the same name is replaced; the others stay. Values are checked like a tap; unknown options drop out."""
    kept = self.profiles()
    merged = dict(kept)
    changes: list[dict] = []
    skipped: list[str] = []
    if not isinstance(raw, dict):
      return changes, merged, skipped
    known = {o.key: o for o in self._phone_options()}
    for raw_name, values in list(raw.items())[:2 * MAX_PROFILES]:
      name = " ".join(str(raw_name or "").split())[:MAX_PROFILE_NAME]
      clean = {}
      for key, value in (values.items() if isinstance(values, dict) else ()):
        if key in known:
          try:
            clean[key] = known[key].coerce(value)
          except ValueError:
            pass
      if not name or not clean or (name not in merged and len(merged) >= MAX_PROFILES):
        if name:
          skipped.append(name)
        continue
      if merged.get(name) != clean:
        changes.append({"name": name, "new": name not in kept})
        merged[name] = clean
    return changes, merged, skipped

  def options_import(self, who: dict, raw, apply: bool, profiles=None) -> tuple[int, dict]:
    """Shows what changes; with apply, writes it, option by option, under the same rules as a tap (car off or in P).
    A copy of the settings also brings its profiles back (profiles); a profile alone sends only its options."""
    changes, skipped = self.options_changes(raw)
    p_changes, _, p_skipped = self.profiles_changes(profiles)
    if not apply:
      out = {"changes": changes, "skipped": skipped}
      if profiles is not None:
        out.update(profiles=p_changes, profiles_skipped=p_skipped)
      return 200, out
    if not changes and not p_changes:
      return 200, {"ok": True, "changes": [], "message": "Nada a mudar."}
    vs = self.vehicle_state()
    parked = not vs.onroad or (vs.parked and vs.standstill and not vs.engaged)
    if any(not phone.can_write(get_option(c["key"]), vs)[0] for c in changes) or (p_changes and not parked):
      return 409, {"error": "Só com o carro em P ou desligado.", "changes": changes}  # all or nothing: never half a copy
    done, failed = [], []
    for c in changes:
      status, _ = self.set_option(who, c["key"], c["new"])
      (done if status == 200 else failed).append(c)
    if p_changes:
      with self.lock:
        p_changes, merged, _ = self.profiles_changes(profiles)  # again, under the lock: another phone may have saved one
        if p_changes:
          self.store.put(PROFILES_PARAM, merged)
          self._audit({"event": "profiles_restored", "who": who.get("name", ""), "new": ", ".join(c["name"] for c in p_changes)[:80]})
    if not done and not failed:
      return 200, {"ok": True, "changes": [], "failed": [], "profiles": p_changes, "message": "Perfis restaurados."}
    restart = any(o.restart_needed for o in OPTIONS if o.key in {c["key"] for c in done})
    msg = "Vale na próxima partida do carro." if restart else "Vale no próximo engate."
    return (200 if not failed else 409), {"ok": not failed, "changes": done, "failed": failed, "profiles": p_changes, "message": msg}

  def profiles(self) -> dict:
    return self.store.get(PROFILES_PARAM) or {}

  def profile_save(self, who: dict, name: str) -> tuple[int, dict]:
    name = " ".join(str(name or "").split())[:MAX_PROFILE_NAME]
    if not name:
      return 400, {"error": "Dê um nome ao perfil."}
    with self.lock:
      kept = self.profiles()
      if name not in kept and len(kept) >= MAX_PROFILES:
        return 409, {"error": "Já tem 6 perfis. Apague um antes."}
      kept[name] = {o.key: self._value(o) for o in self._phone_options()}
      self.store.put(PROFILES_PARAM, kept)
      self._audit({"event": "profile_saved", "who": who.get("name", ""), "new": name})
    return 200, {"ok": True, "message": "Perfil salvo com os ajustes de agora."}

  def profile_delete(self, who: dict, name: str) -> tuple[int, dict]:
    with self.lock:
      kept = self.profiles()
      if name not in kept:
        return 404, {"error": "Perfil não encontrado."}
      del kept[name]
      self.store.put(PROFILES_PARAM, kept)
      self._audit({"event": "profile_removed", "who": who.get("name", ""), "new": name})
    return 200, {"ok": True}

  # *** sync (APP-01): the phone keeps its own copy, and asks only for what changed ***

  def _put(self, key: str, value) -> None:
    self.store.put(key, value)
    self._revs[key] = self._revs.get(key, 0) + 1

  def _stamp(self, *keys: str) -> str:
    """A cheap version of some params: this process's writes, plus each file's time and size on the comma. Never reads a value."""
    parts = [self._boot]
    path_of = getattr(self.store, "get_param_path", None)
    for k in keys:
      parts.append(str(self._revs.get(k, 0)))
      if path_of is not None:
        try:
          st = os.stat(path_of(k))
          parts.append(f"{st.st_mtime_ns:x}-{st.st_size:x}")
        except (OSError, TypeError, ValueError):
          parts.append("-")
    return hashlib.sha1(".".join(parts).encode()).hexdigest()[:12]

  def _cursor(self, since: str) -> tuple[str, str, int | None]:
    """'<trips version>.<marks version>.<process>.<change number>', as the last answer gave it. Another process (a restart)
    or anything else: no cursor, the whole list."""
    m = re.fullmatch(r"([0-9a-f]{1,16})\.([0-9a-f]{1,16})\.([0-9a-f]{8})\.([0-9]{1,12})", since or "")
    if not m or m.group(3) != self._boot or int(m.group(4)) > self._trip_gen:
      return "", "", None
    return m.group(1), m.group(2), int(m.group(4))

  def _trip_changes(self, listed: list[dict]) -> tuple[dict[str, int], int]:
    """The change number of each listed drive: it moves when anything the phone gets of it moves (a summary kept again,
    a minute the deleter took), whatever the clock says. Hidden goes in every delta by itself."""
    with self._sync_lock:
      for t in listed:
        sig = hashlib.sha1(json.dumps({k: v for k, v in t.items() if k != "hidden"}, sort_keys=True, default=str).encode()).hexdigest()[:12]
        old = self._trip_sigs.get(t["id"])
        if old is None or old[0] != sig:
          self._trip_gen += 1
          self._trip_sigs[t["id"]] = (sig, self._trip_gen)
      self._trip_sigs = {t["id"]: self._trip_sigs[t["id"]] for t in listed}
      return {k: g for k, (_, g) in self._trip_sigs.items()}, self._trip_gen

  def _trips_version(self, trips: list[dict]) -> str:
    index = ";".join(f"{t['id']}:{len(t['files'])}:{int(t['mtime'])}" for t in trips)  # the folder listing: no log is opened
    return hashlib.sha1(f"{SUMMARY_VERSION}|{self._stamp(SUMMARY_PARAM, HIDDEN_PARAM)}|{index}".encode()).hexdigest()[:12]

  # *** marks ***

  def _marks(self) -> list[dict]:
    return self.store.get(hexa_marks.MARKS_PARAM) or []

  def _custom_reasons(self) -> list[dict]:
    return self.store.get(hexa_marks.REASONS_PARAM) or []

  def marks_state(self, since: str = "") -> dict:
    with self.lock:
      v = self._stamp(hexa_marks.MARKS_PARAM, hexa_marks.REASONS_PARAM)
      if since == v:
        return {"same": True, "v": v}
      marks = self._marks()
      return {
        "reasons": hexa_marks.reasons(self._custom_reasons()),
        "marks": [hexa_marks.public(m) for m in reversed(marks[-30:])],
        "pending": sum(1 for m in marks if not m.get("reason")),
        "v": v,
      }

  def export_marks(self) -> dict:
    with self.lock:
      return {"exported_at": int(self.clock()), "marks": [hexa_marks.public(m) for m in self._marks()]}

  def _drive_time(self, at) -> float | None:
    """A time inside one of the newest drives, with the car off: "A conferir" answers about the drive after it, and the
    mark goes to when it happened, so it sits in the drive (CTO review of app-12). None otherwise: the mark is now."""
    if isinstance(at, bool) or not isinstance(at, int | float) or not math.isfinite(at) or at > self.clock():
      return None
    if self.vehicle_state().onroad:
      return None  # driving, a mark is this drive's, and its bookmark goes in this drive's log
    with self._replay_lock:
      trips = self._trips()[:COP_KEEP]
    for t in trips:
      end = float(t["mtime"])
      if end - len(t["files"]) * 60 - 60 <= at <= end + 60:  # the phone's tripMarks window
        return float(at)
    return None

  def mark(self, who: dict, reason: str, note: str = "", delay=0., label: str | None = None, at=None, window=None) -> tuple[int, dict]:
    """Phone mark: always allowed (it doesn't change how the car drives), at most one per MIN_MARK_INTERVAL.
    at: with the car off, when the drive's event happened (seconds); no bookmark then, there is no log being written."""
    when = self._drive_time(at) if at is not None else None
    now_mono = self.mono()
    with self.lock:
      label = label or hexa_marks.reason_label(self._custom_reasons(), reason)
      if label is None:
        return 400, {"error": "Motivo desconhecido."}
      last = self._last_mark.get(who.get("id", ""))
      if last is not None and now_mono - last < hexa_marks.MIN_MARK_INTERVAL:
        return 429, {"error": "Espere um instante entre marcas."}
      self._last_mark[who.get("id", "")] = now_mono
      delay = hexa_marks.clean_delay(delay) if when is None else round(self.clock() - when, 1)
      mark = hexa_marks.new_mark("celular", who=who.get("name", ""), reason=reason, label=label,
                                 note=hexa_marks.clean_note(note), now=self.clock() - delay if when is None else when)
      mark["delay_s"] = delay
      win = hexa_marks.clean_window(window)  # [seconds before, seconds after] the mark, when it wasn't just the instant
      if win:
        mark["win"] = win
      if when is None:
        mark["_req_mono_ns"] = int(now_mono * 1e9)
      self._put(hexa_marks.MARKS_PARAM, hexa_marks.append(self._marks(), mark))
      if when is None:
        self.store.put_bool(hexa_marks.BOOKMARK_REQUEST_PARAM, True)  # the comma raises its own bookmark
    self.log_event(hexa_marks.log_fields(mark))
    return 200, {"ok": True, "mark": hexa_marks.public(mark)}

  def classify(self, who: dict, mark_id: str, reason: str, note: str = "", replace_note: bool = False) -> tuple[int, dict]:
    with self.lock:
      label = hexa_marks.reason_label(self._custom_reasons(), reason)
      if label is None:
        return 400, {"error": "Motivo desconhecido."}
      try:
        marks, mark = hexa_marks.set_reason(self._marks(), mark_id, reason, label, note, replace_note)
      except KeyError:
        return 404, {"error": "Marca não encontrada."}
      mark = dict(mark, who=mark.get("who") or who.get("name", ""))
      marks = [mark if m.get("id") == mark_id else m for m in marks]
      self._put(hexa_marks.MARKS_PARAM, marks)
    self.log_event(hexa_marks.log_fields(mark))
    return 200, {"ok": True, "mark": hexa_marks.public(mark)}

  def on_bookmark(self, mono_ns: int) -> dict:
    """A bookmark showed up in the log (comma flag, or the one a phone mark asked for)."""
    with self.lock:
      marks, mark, created = hexa_marks.attach_bookmark(self._marks(), mono_ns)
      self._put(hexa_marks.MARKS_PARAM, marks)
    if not created:
      self.log_event(hexa_marks.log_fields(mark))  # now with the bookmark's exact log time
    return mark

  def add_reason(self, who: dict, label: str) -> tuple[int, dict]:
    with self.lock:
      try:
        custom, reason = hexa_marks.add_reason(self._custom_reasons(), label)
      except ValueError as e:
        return 400, {"error": str(e)}
      self._put(hexa_marks.REASONS_PARAM, custom)
      self._audit({"event": "reason_added", "who": who.get("name", ""), "key": reason["key"], "new": reason["label"]})
    return 200, {"ok": True, "reason": reason}

  def remove_reason(self, who: dict, key: str) -> tuple[int, dict]:
    with self.lock:
      custom = self._custom_reasons()
      if not any(c.get("key") == key for c in custom):
        return 404, {"error": "Só dá para remover os motivos criados por você."}
      self._put(hexa_marks.REASONS_PARAM, hexa_marks.remove_reason(custom, key))
      self._audit({"event": "reason_removed", "who": who.get("name", ""), "key": key})
    return 200, {"ok": True}

  # *** missions (owner, 02/11): the tests the car still needs ***

  def missions_state(self, since: str = "") -> dict:
    catalog = hexa_missions.public(str(self.store.get("GitCommit") or ""))  # built from the code and the running commit, no disk
    if not self._catalog_v:
      self._catalog_v = hashlib.sha1(json.dumps(catalog, sort_keys=True, default=str).encode()).hexdigest()[:8]
    with self.lock:
      v = self._catalog_v + self._stamp(hexa_missions.MISSIONS_PARAM)[:8]
      if since == v:
        return {"same": True, "v": v}
      st = hexa_missions.clean(self.store.get(hexa_missions.MISSIONS_PARAM))
    return dict(catalog, state=st, v=v)

  def missions_action(self, who: dict, body: dict) -> tuple[int, dict]:
    """take / drop: what goes on the next drive, any time (it changes nothing in the car).
    answer: the owner says how it went, parked only. mark: "Fiz agora", a mark with the mission, any time, like Marcar."""
    action, key = str(body.get("action", "")), str(body.get("key", ""))
    if action == "pack":
      with self.lock:
        try:
          st = hexa_missions.take_pack(hexa_missions.clean(self.store.get(hexa_missions.MISSIONS_PARAM)), key)
        except ValueError as e:
          return 400, {"error": str(e)}
        self._put(hexa_missions.MISSIONS_PARAM, st)
      return 200, {"ok": True, "state": st}
    m = hexa_missions.BY_KEY.get(key)
    if m is None:
      return 404, {"error": "Missão não encontrada."}
    if action == "answer":
      vs = self.vehicle_state()
      if vs.onroad and not (vs.parked and vs.standstill and not vs.engaged):
        return 409, {"error": "Responda com o carro parado."}
    result = str(body.get("result", "done"))
    if action == "mark":  # "Fiz agora", or the passenger's copilot: a mark the CTO checks in the log, never done by itself
      if not hexa_missions.can_mark(m) or result not in hexa_missions.RESULTS:
        return 400, {"error": "Esta missão não vai na viagem."}
      # the copilot's answer to an event: the passenger's note after the title, and the mark goes back to when it happened
      extra = hexa_marks.clean_note(body.get("note", ""))
      code, data = self.mark(who, "missao", note=f"{m.title} — {extra}" if extra else m.title, label=hexa_missions.RESULTS[result],
                             delay=body.get("delay_s", 0.), at=body.get("at"))
      if code != 200:
        return code, data
    with self.lock:
      st = hexa_missions.clean(self.store.get(hexa_missions.MISSIONS_PARAM))
      try:
        if action == "take":
          st = hexa_missions.take(st, key)
        elif action == "drop":
          st = hexa_missions.drop(st, key)
        elif action == "answer":
          st = hexa_missions.answer(st, key, body.get("ok") is True)
        elif action == "mark":
          st = hexa_missions.add_pending(st, key, data["mark"]["id"], data["mark"]["t"], result)
        else:
          return 400, {"error": "Ação desconhecida."}
      except ValueError as e:
        return 400, {"error": str(e)}
      self._put(hexa_missions.MISSIONS_PARAM, st)
    return 200, {"ok": True, "state": st}

  # *** replay ***

  def replay_list(self) -> tuple[int, dict]:
    if not self.log_root:
      return 404, {"error": "Sem pasta de logs."}
    segs = timeline.segment_files(self.log_root)
    return 200, {"segments": [{"id": s["id"], "label": s["id"]} for s in segs]}

  def replay_data(self, seg_id: str) -> tuple[int, dict]:
    """Reading a log takes CPU: only with the car off, never while driving."""
    if self.vehicle_state().onroad:
      return 409, {"error": "O replay só abre com o carro desligado."}
    if not self.log_root:
      return 404, {"error": "Sem pasta de logs."}
    files = {s["id"]: s["file"] for s in timeline.segment_files(self.log_root, limit=10_000)}
    if seg_id not in files:  # only ids from the list: no paths from the outside
      return 404, {"error": "Trecho não encontrado."}
    with self._replay_lock:
      if seg_id not in self._replay_cache:
        try:
          self._replay_cache[seg_id] = timeline.build(timeline.read_rlog(files[seg_id]))
        except Exception as e:
          return 500, {"error": f"Não foi possível ler o trecho: {type(e).__name__}"}
        while len(self._replay_cache) > REPLAY_CACHE:
          self._replay_cache.popitem(last=False)
      self._replay_cache.move_to_end(seg_id)
      return 200, self._replay_cache[seg_id]

  # *** trips ***

  def _trips(self) -> list[dict]:
    at, trips = self._trip_index
    if self.mono() - at >= TRIP_INDEX_S:
      trips = timeline.trip_files(self.log_root or "", limit=10_000)
      self._trip_index = (self.mono(), trips)
    return trips

  def trips_list(self, since: str = "") -> tuple[int, dict]:
    """The drives with their summaries. With the cursor of the last answer (since), only what changed after it:
    nothing new costs a folder listing (kept 30 s) and a stat, never a log or the summaries."""
    if self.vehicle_state().onroad:
      return 409, {"error": "As viagens só abrem com o carro desligado."}
    if not self.log_root:
      return 404, {"error": "Sem pasta de logs."}
    with self._replay_lock:
      every = self._trips()
    trips = every[:TRIPS_LISTED]
    v, mv = self._trips_version(trips), self._stamp(hexa_marks.MARKS_PARAM)
    old_v, old_mv, old_gen = self._cursor(since)
    if old_gen is not None and old_v == v and old_mv == mv and not self._todo_left:
      return 200, {"same": True, "v": since}  # the old cursor stays: nothing happened since it
    self._prune_paths({t["id"] for t in every})
    kept = self.store.get(SUMMARY_PARAM) or {}  # read once: the whole history is in it
    newest = [t["id"] for t in trips[:SUMMARY_NEWEST]]
    summaries = {t["id"]: self._summary_of(t, kept, newest) for t in trips}
    todo = [t for t in trips[:SUMMARY_NEWEST] if summaries[t["id"]] is None]
    self._todo_left = bool(todo)
    if todo and not self._summary_busy:
      self._summary_busy = True
      self._spawn(lambda: self._summarize_trips(todo))
    hidden = set(self.store.get(HIDDEN_PARAM) or [])
    listed = [{"id": t["id"], "mtime": int(t["mtime"]), "minutes": len(t["files"]), "segments": sorted(t["files"]),
               "summary": summaries[t["id"]], "hidden": t["id"] in hidden} for t in trips]
    changes, gen = self._trip_changes(listed)
    out: dict = {"pending": bool(todo), "pending_n": len(todo), "v": f"{v}.{mv}.{self._boot}.{gen}"}
    if old_mv != mv:
      with self.lock:
        out["marks"] = [hexa_marks.public(m) for m in self._marks()]  # /api/marks brings only the newest: the history covers every drive
    if old_gen is None:
      return 200, dict(out, trips=listed, marks=out.get("marks", []))
    changed = [t for t in listed if changes[t["id"]] > old_gen]
    return 200, dict(out, delta=True, trips=changed, ids=[t["id"] for t in listed], hidden=sorted(hidden & {t["id"] for t in listed}))

  def trip_hide(self, who: dict, route: str, hidden: bool) -> tuple[int, dict]:
    """Hides a drive from the phone's lists, or shows it again. Only a name on the list: nothing is deleted."""
    if self.vehicle_state().onroad:
      return 409, {"error": "As viagens só abrem com o carro desligado."}
    with self._replay_lock:
      known = any(t["id"] == route for t in self._trips())
    if not known:
      return 404, {"error": "Viagem não encontrada."}
    with self.lock:
      kept = [r for r in (self.store.get(HIDDEN_PARAM) or []) if r != route]
      self._put(HIDDEN_PARAM, (kept + [route])[-HIDDEN_KEEP:] if hidden else kept)
      self._audit({"event": "trip_hidden" if hidden else "trip_shown", "who": who.get("name", ""), "key": route})
    return 200, {"ok": True, "hidden": hidden}

  def _summary_of(self, trip: dict, kept: dict | None = None, newest: list | None = None) -> dict | None:
    s = (kept if kept is not None else self.store.get(SUMMARY_PARAM) or {}).get(trip["id"])
    if not isinstance(s, dict) or s.get("segments", 0) < len(trip["files"]):  # a drive that grew is redone, one the deleter trimmed is not
      return None
    if newest is None:
      newest = [t["id"] for t in self._trips()[:SUMMARY_NEWEST]]
    if s.get("sv") == SUMMARY_VERSION or trip["id"] not in newest:
      return s  # old drives keep what they have
    still = SUMMARY_STILL_GOOD.get(s.get("sv"))
    return s if still is not None and trip["id"] not in newest[:still] else None

  def _path_file(self, route: str) -> Path | None:
    return Path(self.log_root).parent / PATH_DIR / f"{route}.json" if self.log_root and re.fullmatch(r"[0-9a-f]{8}--[0-9a-f]{10}", route) else None

  def _keep_path(self, route: str, path: list | None) -> None:
    f = self._path_file(route)
    if f is None or not path:
      return
    try:
      f.parent.mkdir(parents=True, exist_ok=True)
      tmp = f.with_suffix(".tmp")
      tmp.write_text(json.dumps(path, separators=(",", ":")))
      tmp.replace(f)  # whole or nothing: a cut write never leaves a broken path behind a finished summary
      for old in sorted(f.parent.glob("*.json"))[:-SUMMARY_KEEP]:  # route names grow with each drive
        old.unlink(missing_ok=True)
    except OSError:
      from openpilot.common.swaglog import cloudlog
      cloudlog.exception("hexaweb: trip path not kept")

  def _prune_paths(self, routes: set[str]) -> None:
    """A path whose drive the comma already deleted (loggerd's deleter) goes too: no location stays behind its log."""
    if not routes or not self.log_root or self.mono() - self._paths_pruned < PATH_PRUNE_S:  # no drives at all: the folder, not the logs
      return
    self._paths_pruned = self.mono()
    try:
      for f in (Path(self.log_root).parent / PATH_DIR).glob("*.json"):
        if f.stem not in routes:
          f.unlink(missing_ok=True)
    except OSError:
      pass

  def trip_path(self, route: str, since: str = "") -> tuple[int, dict]:
    """The drive's path for the map on the phone: [lat, lon, who, s from the start], a point every ~10 s. Car off only."""
    if self.vehicle_state().onroad:
      return 409, {"error": "As viagens só abrem com o carro desligado."}
    with self._replay_lock:
      known = any(t["id"] == route for t in self._trips())  # only ids from the list: no paths from outside
    f = self._path_file(route) if known else None
    try:
      st = f.stat() if f is not None else None
    except OSError:
      st = None
    v = f"{st.st_mtime_ns:x}-{st.st_size:x}" if st is not None else ""
    if v and since == v:
      return 200, {"same": True, "v": v}  # the phone has this one: a stat, not the file
    try:
      points = json.loads(f.read_text()) if st is not None else None
    except (OSError, ValueError):
      points = None
    if not isinstance(points, list):
      return 404, {"error": "Sem caminho desta viagem ainda."}
    return 200, {"points": points, "v": v}

  def _summarize_trips(self, todo: list[dict]) -> None:
    """Reads each drive once, after it ended, and keeps the numbers. Stops as soon as the car is on again."""
    try:
      for trip in todo:
        if self.vehicle_state().onroad:
          return
        summary = self._summarize(trip["files"], lambda: self.vehicle_state().onroad)
        if summary is None:
          return
        summary = {**summary, "sv": SUMMARY_VERSION}
        try:  # the drive's clock: its last read minute's file closed when that minute ended (a cut last minute isn't read)
          f = trip["files"].get(summary.get("end_seg"))
          if f is not None:
            summary["t0"] = round(Path(f).stat().st_mtime - float(summary.get("end_s", 0.)), 1)
        except OSError:
          pass
        self._keep_path(trip["id"], summary.pop("path", None))
        self._auto_marks(trip, summary)
        with self.lock:
          kept = self.store.get(SUMMARY_PARAM) or {}
          kept[trip["id"]] = summary
          for old in sorted(kept)[:-SUMMARY_KEEP]:  # route names grow with each drive
            del kept[old]
          for old in sorted(kept)[:-COP_KEEP]:
            if isinstance(kept[old], dict):
              kept[old].pop("cop", None)
          self._put(SUMMARY_PARAM, kept)
    finally:
      self._summary_busy = False

  def _auto_marks(self, trip: dict, summary: dict) -> None:
    """Very hard brakes found in a drive become marks, and their video stays, as a flag's does. A minute's file is
    written last at the end of that minute: "left" is how long before it [hipótese] give or take a second or two."""
    found = []
    for a in summary.get("auto_marks") or []:
      path = trip["files"].get(a.get("seg"))
      try:
        found.append((Path(path).stat().st_mtime - float(a["left"]), a["kind"]))
        os.setxattr(str(Path(path).parent), PRESERVE_ATTR, b"1")
      except (AttributeError, TypeError, OSError, KeyError, ValueError):  # AttributeError: no xattrs on the Mac
        continue
    if found:
      with self.lock:
        self._put(hexa_marks.MARKS_PARAM, hexa_marks.auto_marks(self._marks(), found))

  def _with_reasons(self, events: list[dict]) -> list[dict]:
    with self.lock:
      marks = [m for m in self._marks() if m.get("mono_ns") and m.get("label")]
    out = []
    for e in events:
      e = dict(e)
      mono = e.pop("mono_ns", None)
      if mono is not None:
        for m in marks:
          if abs(m["mono_ns"] - mono) < 2e9:
            e["text"] = m["label"] + (f": {m['note']}" if m.get("note") else "")
      out.append(e)
    return out

  def trip_data(self, route: str, segment: str) -> tuple[int, dict]:
    if self.vehicle_state().onroad:
      return 409, {"error": "As viagens só abrem com o carro desligado."}
    if not self.log_root:
      return 404, {"error": "Sem pasta de logs."}
    with self._replay_lock:
      trip = next((t for t in self._trips() if t["id"] == route), None)  # only ids from the list: no paths from outside
      path = trip["files"].get(int(segment)) if trip and segment.isdigit() else None
      if path is None:
        return 404, {"error": "Viagem não encontrada."}
      key = f"{route}--{segment}"
      if key not in self._trip_cache:
        try:
          self._trip_cache[key] = timeline.trip_segment(timeline.read_rlog(path))
        except Exception as e:
          return 500, {"error": f"Não foi possível ler a viagem: {type(e).__name__}"}
        while len(self._trip_cache) > TRIP_CACHE:
          self._trip_cache.popitem(last=False)
      self._trip_cache.move_to_end(key)
      data = self._trip_cache[key]
    return 200, {**data, "events": self._with_reasons(data["events"])}

  # *** speed cameras ***

  def radars_state(self) -> dict:
    updated = self.radar_path is not None and self.radar_path.is_file()
    path = self.radar_path if updated else radars.BUNDLED
    with open(path) as f:
      count = sum(1 for line in f if not line.startswith("#"))
    allowed, reason = updates.can_update(self.vehicle_state())
    return {"count": count, "date": int(path.stat().st_mtime) if updated else None, "allowed": allowed, "reason": reason, **self._radar_job}

  def _gps(self) -> tuple[float, float, float, float] | None:
    with self._vs_lock:
      try:
        return self._gps_fix()
      except Exception:
        return None

  def _edits(self) -> list[dict]:
    return self.store.get(radars.EDITS_PARAM) or []

  def _rows(self):
    """The list with the edits, built again only when one of them changes."""
    edits = self._edits()
    path = self.radar_path if self.radar_path is not None and self.radar_path.is_file() else radars.BUNDLED
    stamp = (str(path), path.stat().st_mtime, str(edits), int(self.clock() // 60))  # a minute, for a blitz to expire
    if self._radar_rows[0] != stamp:
      self._radar_rows = (stamp, radars.merge(radars.load(path), edits, self.clock()))
    return self._radar_rows[1], edits

  def radars_near(self) -> dict:
    fix = self._gps()
    with self.lock:
      rows, edits = self._rows()
      mine = [{"key": e["key"], "kind": e["kind"], "limit": e.get("limit"), "t": e.get("t"), "removed": bool(e.get("removed")),
               "side": "dois" if e.get("both") else "?", "passed": e.get("passed")} for e in reversed(radars.active(edits, self.clock()))][:40]
    if fix is None:
      return {"gps": False, "moving": False, "near": [], "mine": mine}
    return {"gps": True, "moving": fix[3] >= radars.DIRECTION_MIN_SPEED, "near": radars.nearby(rows, edits, fix[0], fix[1], fix[2]), "mine": mine}

  def radars_export(self) -> dict:
    """The owner's points in a file of their own: a reset of the comma erases them. Only on request, and it has positions."""
    with self.lock:
      return {"exported_at": int(self.clock()), "edits": radars.active(self._edits(), self.clock())}

  def radars_import(self, who: dict, raw) -> tuple[int, dict]:
    if self.vehicle_state().onroad:
      return 409, {"error": "Desligue o carro para restaurar."}
    incoming = radars.clean_edits(raw)
    if not incoming:
      return 400, {"error": "Nenhum ponto válido nesse arquivo."}
    with self.lock:
      edits = self._edits()
      for entry in incoming:
        edits = radars.apply_edit(edits, entry, self.clock())
      self.store.put(radars.EDITS_PARAM, edits)
      self._audit({"event": "radar", "who": who.get("name", ""), "key": "import", "new": len(incoming)})
    return 200, {"ok": True, "message": f"Pontos restaurados: {len(incoming)}. Valem na próxima partida do carro."}

  def radars_pin(self) -> tuple[int, dict]:
    """Holds where the car is at the tap: at 60 km/h, ten seconds later it is 170 m ahead of the point."""
    fix = self._gps()
    if fix is None:
      return 409, {"error": "Sem GPS agora: não dá para saber onde é."}
    with self.lock:
      now = self.clock()
      self._radar_pins = {k: v for k, v in self._radar_pins.items() if now - v[0] < radars.PIN_SECONDS}
      pin = secrets.token_urlsafe(8)
      self._radar_pins[pin] = (now, fix)
    return 200, {"pin": pin, "moving": fix[3] >= radars.DIRECTION_MIN_SPEED, "speed": round(fix[3] * 3.6)}

  def radars_edit(self, who: dict, body: dict) -> tuple[int, dict]:
    fix = self._gps()
    point_key = str(body.get("key", ""))
    limit = body.get("limit")
    with self.lock:
      rows, edits = self._rows()
      hit = [i for i in range(len(rows)) if radars.key(rows[i, 0], rows[i, 1]) == point_key]
      if not hit:
        return 404, {"error": "Ponto não encontrado."}
      row = rows[hit[0]]
      if not radars.valid_limit(limit, int(row[4])):
        return 400, {"error": "Limite inválido."}
      side = body.get("side")
      if side in ("mesmo", "contrario") and fix is None:
        return 409, {"error": "Sem GPS para saber o sentido."}
      if side in ("mesmo", "contrario") and fix[3] < radars.DIRECTION_MIN_SPEED:
        return 409, {"error": "Com o carro parado o GPS não sabe o sentido. Marque andando."}
      old = next((e for e in edits if e["key"] == point_key), {})
      direction = None if math.isnan(row[3]) else float(row[3])
      both = bool(old.get("both"))
      if side in radars.SIDES:
        direction = radars.direction_of(side, fix[2] if fix else 0.)
        both = side == "dois"
      elif side == "?":
        direction, both = None, False
      entry = {**old, "key": point_key, "lat": float(row[0]), "lon": float(row[1]), "kind": old.get("kind", "edit"), "limit": limit,
               "direction": direction, "both": both, "confirmed": bool(body.get("confirmed")), "removed": bool(body.get("removed")),
               "who": who.get("name", ""), "t": int(self.clock())}
      self.store.put(radars.EDITS_PARAM, radars.apply_edit(edits, entry, self.clock()))
      self._audit({"event": "radar", "who": who.get("name", ""), "key": "removed" if entry["removed"] else "edit", "new": limit})
    return 200, {"ok": True, "message": "Vale na próxima partida do carro."}

  def radars_add(self, who: dict, body: dict) -> tuple[int, dict]:
    kind = str(body.get("kind", ""))
    limit = body.get("limit")
    if kind not in radars.KINDS:
      return 400, {"error": "Tipo desconhecido."}
    if not radars.valid_limit(limit, radars.KINDS[kind]):
      return 400, {"error": "Velocidade inválida."}
    with self.lock:
      now = self.clock()
      pin = str(body.get("pin", ""))
      held = self._radar_pins.get(pin)  # the phone never sends a position
      if held is None or now - held[0] >= radars.PIN_SECONDS:
        self._radar_pins.pop(pin, None)
        return 409, {"error": "Esse toque expirou. Toque de novo ao passar pelo ponto."}
      lat, lon, bearing, speed = held[1]
      side = body.get("side")
      moving = speed >= radars.DIRECTION_MIN_SPEED
      if side in ("mesmo", "contrario") and not moving:
        return 409, {"error": "Com o carro parado o GPS não sabe o sentido. Escolha dois sentidos."}  # keeps the tap
      del self._radar_pins[pin]
      direction = radars.direction_of(side, bearing) if side in radars.SIDES else (bearing if moving else None)
      rows, edits = self._rows()
      if radars.taken(rows, lat, lon, direction, radars.KINDS[kind]) or any(e["key"] == radars.key(lat, lon) for e in edits):
        return 409, {"error": "Já tem um ponto aqui: confira na lista."}
      entry = {"key": radars.key(lat, lon), "lat": lat, "lon": lon, "kind": kind, "limit": limit, "direction": direction, "both": side == "dois",
               "passed": round(speed * 3.6), "who": who.get("name", ""), "t": int(now), "expires": now + radars.BLITZ_HOURS * 3600 if kind == "blitz" else None}
      self.store.put(radars.EDITS_PARAM, radars.apply_edit(edits, entry, now))
      self._audit({"event": "radar", "who": who.get("name", ""), "key": "add", "new": kind})
    return 200, {"ok": True, "message": "Salvo. Vale na próxima partida do carro."}

  def radars_delete(self, who: dict, point_key: str) -> tuple[int, dict]:
    with self.lock:
      edits = self._edits()
      kept = [e for e in edits if e.get("key") != point_key]
      if len(kept) == len(edits):
        return 404, {"error": "Ponto não encontrado."}
      self.store.put(radars.EDITS_PARAM, kept)
      self._audit({"event": "radar", "who": who.get("name", ""), "key": "delete", "new": ""})
    return 200, {"ok": True}

  def radars_update(self, who: dict) -> tuple[int, dict]:
    allowed, reason = updates.can_update(self.vehicle_state())
    if not allowed:
      return 409, {"error": reason}
    if self.radar_path is None:
      return 404, {"error": "Sem lugar para guardar a lista."}
    with self.lock:
      if self._radar_job["busy"]:
        return 409, {"error": "A lista já está sendo baixada."}
      self._radar_job = {"busy": True, "error": ""}
      self._audit({"event": "update", "who": who.get("name", ""), "key": "radars", "new": ""})

    def run():
      try:
        radars.download(self.radar_path)
        self._radar_job = {"busy": False, "error": ""}
      except Exception as e:
        self._radar_job = {"busy": False, "error": f"Não foi possível baixar: {type(e).__name__}"}

    threading.Thread(target=run, daemon=True).start()
    return 200, {"ok": True}

  # *** updates ***

  def update_state(self) -> dict:
    allowed, reason = updates.can_update(self.vehicle_state())
    with self.lock:
      return {**updates.state(self.store), "allowed": allowed, "reason": reason}

  def update_action(self, who: dict, action: str, branch: str = "") -> tuple[int, dict]:
    allowed, reason = updates.can_update(self.vehicle_state())
    with self.lock:
      if not allowed:
        self._audit({"event": "denied", "who": who.get("name", ""), "key": "update", "new": action, "reason": reason})
        return 409, {"error": reason}
      if action == "branch":
        if branch not in updates.branches(self.store.get("UpdaterAvailableBranches")):
          return 400, {"error": "Versão desconhecida."}
        self.store.put("UpdaterTargetBranch", branch)
        self.signal_updater(updates.SIGNALS["check"])
      elif action in updates.SIGNALS:
        self.signal_updater(updates.SIGNALS[action])
      elif action == "install":
        if not self.store.get_bool("UpdateAvailable"):
          return 409, {"error": "Nenhuma versão pronta para instalar."}
        self.store.put_bool("DoReboot", True)
      elif action == "reboot":
        self.store.put_bool("DoReboot", True)
      else:
        return 400, {"error": "Ação desconhecida."}
      self._audit({"event": "update", "who": who.get("name", ""), "key": action, "new": branch})
      return 200, {"ok": True}

  def phones(self, who: dict) -> dict:
    with self.lock:
      return {"max": phone.MAX_TOKENS,
              "phones": [{"id": t.get("id", ""), "name": t.get("name", ""), "created": t.get("created"), "seen": t.get("seen"),
                          "you": t.get("id") == who.get("id")} for t in self._tokens()]}

  def revoke_phone(self, who: dict, token_id: str) -> tuple[int, dict]:
    if token_id == who.get("id"):
      return 400, {"error": "Para este celular, use Desconectar."}
    with self.lock:
      tokens = self._tokens()
      gone = next((t for t in tokens if t.get("id") == token_id), None)
      if gone is None:
        return 404, {"error": "Celular não encontrado."}
      self.store.put(phone.TOKENS_PARAM, phone.revoke_token(tokens, token_id))
      self._audit({"event": "phone_revoked", "who": who.get("name", ""), "new": gone.get("name", "")})
    return 200, {"ok": True}

  # *** sending a mark's moment to the developers (selfdrive/hexa/share.py): only what the owner picks ***

  def share_state(self) -> dict:
    if self.sharer is None:
      return {"enabled": False, "receiver": False, "items": [], "forgetting": False, "sent": 0}
    with self.lock:
      return self.sharer.state()

  def share_add(self, who: dict, mark_id: str, with_note: bool = False) -> tuple[int, dict]:
    if self.sharer is None:
      return 404, {"error": "Este comma não envia trechos."}
    with self.lock:
      mark = next((m for m in self._marks() if m.get("id") == mark_id), None)
      if mark is None:
        return 404, {"error": "Marca não encontrada."}
      status, out = self.sharer.enqueue(mark, with_note=with_note)
      if status == 200:
        self._audit({"event": "share_queued", "who": who.get("name", ""), "new": mark_id})
      return status, out

  def share_cancel(self, who: dict, mark_id: str) -> tuple[int, dict]:
    if self.sharer is None:
      return 404, {"error": "Este comma não envia trechos."}
    with self.lock:
      return self.sharer.cancel(mark_id)

  def share_forget(self, who: dict) -> tuple[int, dict]:
    if self.sharer is None:
      return 404, {"error": "Este comma não envia trechos."}
    with self.lock:
      self._audit({"event": "share_forget", "who": who.get("name", "")})
      return self.sharer.forget()

  def share_step(self, network_ok: bool) -> str:
    """The worker's turn (main(), every SHARE_EVERY_S). The cut reads the logs outside the lock: only the queue is locked."""
    if self.sharer is None:
      return "nada"
    with self.lock:
      marks = [dict(m) for m in self._marks()]
    return self.sharer.step(offroad=not self.vehicle_state().onroad, network_ok=network_ok, marks=marks)

  # *** the errors of the "Erros" card, sent to the developers when the owner confirms (selfdrive/hexa/report.py, ERR-01) ***

  def _report(self) -> tuple[dict | None, str]:
    from openpilot.selfdrive.hexa import edition
    if self.sharer is None:
      return None, "Este comma não envia relatórios."
    if not edition.allows(hexa_report.BRANDS, self.store):
      return None, "Por enquanto, o envio de erros é só do Haval."
    return hexa_report.build(self.store, self.health(), self.sharer.edition, self.clock()), ""

  def errors_preview(self, who: dict) -> tuple[int, dict]:
    rep, why = self._report()
    return (409, {"error": why}) if rep is None else (200, rep)

  def errors_send(self, who: dict) -> tuple[int, dict]:
    rep, why = self._report()
    if rep is None:
      return 409, {"error": why}
    data = hexa_report.encode(rep)
    try:
      hexa_report.check(self.store, data)
    except hexa_share.ShareError as e:
      return 409, {"error": str(e)}
    status, out = self.sharer.report(data)
    if status == 200:
      with self.lock:
        self._audit({"event": "errors_sent", "who": who.get("name", ""), "new": out.get("receipt", "")})
    return status, out

  # *** the route: the phone picks the destination, openpilot's navigation (navd) does the rest. It never changes the driving ***

  def nav_state(self) -> dict:
    n = self.navigator
    near = n.position()  # the car on the phone's Mapa and the distance to each result; the drives already show the paths
    return {"enabled": n.enabled(), "destination": n.destination(), "places": n.places(),
            "token": bool(self.store.get(hexa_nav.TOKEN_PARAM)), "route": self._route_state(),
            "near": [round(near[0], 4), round(near[1], 4)] if near else None}

  def _nav(self, fn) -> tuple[int, dict]:
    try:
      out = fn()
    except hexa_nav.NavError as e:
      return 400, {"error": str(e)}
    return 200, {"ok": True, **(out or {})}

  def nav_search(self, who: dict, query: str) -> tuple[int, dict]:
    return self._nav(lambda: {"results": self.navigator.search(query)})

  def nav_go(self, who: dict, body: dict) -> tuple[int, dict]:
    status, out = self._nav(lambda: {"destination": self.navigator.go(body.get("latitude"), body.get("longitude"), body.get("name", ""))})
    if status == 200:
      self._audit({"event": "nav_go", "who": who.get("name", "")})  # not where: the audit is read on the phones
    return status, out

  def nav_stop(self, who: dict) -> tuple[int, dict]:
    return self._nav(lambda: self.navigator.stop())

  def nav_place(self, who: dict, body: dict) -> tuple[int, dict]:
    return self._nav(lambda: {"places": self.navigator.save_place(body.get("name"), body.get("latitude"), body.get("longitude"))})

  def nav_place_remove(self, who: dict, name: str) -> tuple[int, dict]:
    return self._nav(lambda: {"places": self.navigator.remove_place(name)})

  def nav_token(self, who: dict, token) -> tuple[int, dict]:
    if self.vehicle_state().onroad:
      return 409, {"error": "Só com o carro desligado."}
    return self._nav(lambda: {"token": self.navigator.set_token(token)})
  # *** the road's limit from the map: installing mapd and its maps, car off only ***

  def limit_state(self) -> dict:
    try:
      state = self._limit_state()
    except Exception:
      state = {"fresh": False}
    with self._maps_lock:
      ended = self._maps.update(state.get("download"), self.mono(), self._network_ok,
                                lambda: self._free_bytes() >= speed_limit.INSTALL_FREE_BYTES)
      if ended:
        self._map_saved(ended)
      job = self._maps.view()
    saved = self._saved_maps()
    return {**state, "job": dict(self._limit_job), "areas": [{"key": k, "name": n} for k, n, _ in speed_limit.AREAS],
            "version": speed_limit.MAPD_VERSION if state.get("installed") else "",  # installed = the pin's sha256 (binary_ok)
            "map": job, "saved": [{"area": k, **saved[k]} for k, _, _ in speed_limit.AREAS if k in saved]}

  def _saved_maps(self) -> dict:
    try:
      got = json.loads(self._maps_path.read_text())
      return {k: {"date": float(v["date"]), "files": int(v["files"])} for k, v in got.items() if isinstance(v, dict)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
      return {}

  def _map_saved(self, ended: dict) -> None:
    """A map downloaded whole, with the date: closing and opening the phone still says what is on the comma."""
    saved = self._saved_maps()
    saved[ended["area"]] = {"date": self.clock(), "files": ended["files"]}
    try:
      self._maps_path.parent.mkdir(parents=True, exist_ok=True)
      tmp = self._maps_path.with_suffix(".tmp")
      tmp.write_text(json.dumps(saved) + "\n")
      os.replace(tmp, self._maps_path)
    except OSError:
      from openpilot.common.swaglog import cloudlog
      cloudlog.exception("hexaweb: could not keep the downloaded map")

  def _limit_ready(self, need_bytes: int) -> None:
    offroad = not self.vehicle_state().onroad
    if offroad and self._network_kind() == "none":
      raise speed_limit.InstallError("Sem rede no comma: conecte o comma a um Wi-Fi.")
    speed_limit.check_conditions(offroad, self._network_ok(), self._free_bytes(), need_bytes)

  def limit_install(self, who: dict) -> tuple[int, dict]:
    try:
      self._limit_ready(speed_limit.INSTALL_FREE_BYTES)
    except speed_limit.InstallError as e:
      with self.lock:
        if not self._limit_job["busy"]:  # the card says why it didn't install, not only a toast that goes away
          self._limit_job = {"busy": False, "error": str(e), "ok": False}
      return 409, {"error": str(e)}
    with self.lock:
      if self._limit_job["busy"]:
        return 200, {"ok": True, "busy": True}
      self._limit_job = {"busy": True, "error": "", "ok": False}

    def job():
      try:
        self._install_mapd()
        self._limit_job = {"busy": False, "error": "", "ok": True}
      except speed_limit.InstallError as e:
        self._limit_job = {"busy": False, "error": str(e), "ok": False}
      except Exception:
        self._limit_job = {"busy": False, "error": "Não foi possível instalar o mapd.", "ok": False}
    self._audit({"event": "mapd_install", "who": who.get("name", "")})
    self._spawn(job)
    return 200, {"ok": True, "busy": True}

  def limit_download(self, who: dict, area: str) -> tuple[int, dict]:
    if speed_limit.option(self.store) == speed_limit.OFF or not self.limit_state().get("installed"):
      return 409, {"error": "Ligue \"limite da via pelo mapa\" e instale o mapd antes."}
    try:
      need = speed_limit.area_need(area)
    except speed_limit.InstallError as e:
      return 400, {"error": str(e)}
    try:
      self._limit_ready(need)
      msg = speed_limit.download_message(area)
    except speed_limit.InstallError as e:
      with self._maps_lock:
        self._maps.refuse(area, str(e))
      return 409, {"error": str(e)}
    self._send_mapd(msg)
    with self._maps_lock:
      self._maps.ask(area, self.mono())
    return 200, {"ok": True}

  def limit_cancel(self, who: dict) -> tuple[int, dict]:
    """Parar: mapd stops after the file it is on."""
    self._send_mapd(speed_limit.mapd_in("cancelDownload"))
    with self._maps_lock:
      self._maps.stop("you")
    return 200, {"ok": True}

  def limit_tick(self) -> bool:
    """The worker's turn: a map download stops when the car turns on (mapd downloads only parked), and a download that
    ends with the phone closed is kept with its date. True if it asked mapd to stop."""
    download = self.limit_state().get("download") or {}
    if self._download_guard.update(self.vehicle_state().onroad, bool(download.get("active"))):
      self._send_mapd(speed_limit.mapd_in("cancelDownload"))
      with self._maps_lock:
        self._maps.stop("car")
      return True
    return False

  def logout(self, who: dict) -> tuple[int, dict]:
    with self.lock:
      self.store.put(phone.TOKENS_PARAM, phone.revoke_token(self._tokens(), who.get("id", "")))
      self._audit({"event": "logout", "who": who.get("name", "")})
    return 200, {"ok": True}


def make_handler(backend: PanelBackend):
  class Handler(BaseHTTPRequestHandler):
    server_version = "hexaweb"
    sys_version = ""

    def log_message(self, format, *args):  # noqa: A002 - keep the request log out of stderr
      pass

    def _send(self, status: int, body: bytes, content_type: str, compress: bool = True, extra: dict | None = None) -> None:
      # a replay is ~400 KB of JSON per minute and ~50 KB gzipped
      gz = compress and len(body) >= GZIP_MIN_BYTES and "gzip" in self.headers.get("Accept-Encoding", "")
      if gz:
        body = gzip.compress(body, compresslevel=5)
      self.send_response(status)
      self.send_header("Content-Type", content_type)
      self.send_header("Content-Length", str(len(body)))
      self.send_header("Vary", "Accept-Encoding")
      if gz:
        self.send_header("Content-Encoding", "gzip")
      for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
        self.send_header(k, v)
      self.end_headers()
      self.wfile.write(body)

    def _json(self, status: int, obj: dict) -> None:
      self._send(status, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode(), "application/json; charset=utf-8")

    def _allowed(self) -> bool:
      if not phone.is_local_address(self.client_address[0]):
        self._json(403, {"error": "Só na rede local."})
        return False
      return True

    def _who(self) -> dict | None:
      auth = self.headers.get("Authorization", "")
      token = auth[7:] if auth.startswith("Bearer ") else None
      who = backend.authenticate(token)
      if who is None:
        self._json(401, {"error": "Celular não pareado."})
      return who

    def _body(self, limit: int = MAX_BODY) -> dict | None:
      try:
        length = int(self.headers.get("Content-Length", "0"))
      except ValueError:
        length = -1
      if not 0 <= length <= limit:
        self._json(413, {"error": "Pedido grande demais."})
        return None
      try:
        body = json.loads(self.rfile.read(length) or b"{}")
      except (json.JSONDecodeError, UnicodeDecodeError):
        body = None
      if not isinstance(body, dict):
        self._json(400, {"error": "JSON inválido."})
        return None
      return body

    def do_HEAD(self):
      """Some phones ask HEAD before saving the icon: the static files only, their uncompressed headers, never a body."""
      if not phone.is_local_address(self.client_address[0]):
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return
      path = self.path.partition("?")[0]
      if path not in STATIC_FILES:
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return
      fn, ctype = STATIC_FILES[path]
      size = (fn if isinstance(fn, Path) else STATIC_DIR / fn).stat().st_size
      self.send_response(200)
      self.send_header("Content-Type", ctype)
      self.send_header("Content-Length", str(size))
      for k, v in {**SECURITY_HEADERS, **(ICON_CACHE if ctype == "image/png" else {})}.items():
        self.send_header(k, v)
      self.end_headers()

    def do_GET(self):
      if not self._allowed():
        return
      path, _, query = self.path.partition("?")
      if path in STATIC_FILES:
        fn, ctype = STATIC_FILES[path]
        self._send(200, (fn if isinstance(fn, Path) else STATIC_DIR / fn).read_bytes(), ctype, extra=ICON_CACHE if ctype == "image/png" else None)
      elif path == "/api/state":
        who = self._who()
        if who is not None:
          self._json(200, backend.state(who))
      elif path == "/api/marks":
        who = self._who()
        if who is not None:
          self._json(200, backend.marks_state(parse_qs(query).get("since", [""])[0]))
      elif path == "/api/marks/export":
        who = self._who()
        if who is not None:
          self._json(200, backend.export_marks())
      elif path == "/api/versions":  # what each channel version brought: static, read once
        if self._who() is not None:
          self._json(200, hexa_versions.public(backend.store.get("GitCommit") or ""))
      elif path == "/api/missions":
        if self._who() is not None:
          self._json(200, backend.missions_state(parse_qs(query).get("since", [""])[0]))
      elif path == "/api/replay/list":
        if self._who() is not None:
          self._json(*backend.replay_list())
      elif path == "/api/replay/data":
        if self._who() is not None:
          self._json(*backend.replay_data(parse_qs(query).get("id", [""])[0]))
      elif path == "/api/trips":
        if self._who() is not None:
          self._json(*backend.trips_list(parse_qs(query).get("since", [""])[0]))
      elif path == "/api/trip/path":
        if self._who() is not None:
          q = parse_qs(query)
          self._json(*backend.trip_path(q.get("route", [""])[0], q.get("since", [""])[0]))
      elif path == "/api/trip/segment":
        if self._who() is not None:
          q = parse_qs(query)
          self._json(*backend.trip_data(q.get("route", [""])[0], q.get("n", [""])[0]))
      elif path == "/api/update":
        if self._who() is not None:
          self._json(200, backend.update_state())
      elif path == "/api/radars":
        if self._who() is not None:
          self._json(200, backend.radars_state())
      elif path == "/api/radars/near":
        if self._who() is not None:
          self._json(200, backend.radars_near())
      elif path == "/api/radars/export":
        if self._who() is not None:
          self._json(200, backend.radars_export())
      elif path == "/api/comma":
        if self._who() is not None:
          self._json(200, backend.comma_state())
      elif path == "/api/ident":  # the LizPilot app: is this the comma that paired me? No token, opens nothing
        q = parse_qs(query)
        self._json(*backend.ident((q.get("p") or [""])[0], (q.get("n") or [""])[0]))
      elif path == "/api/car/route":  # the LizPilot app's car screen (Android Auto): no token, the pairing's hash answers a challenge
        q = parse_qs(query)
        self._json(*backend.car_route((q.get("p") or [""])[0], (q.get("n") or [""])[0], (q.get("m") or [""])[0],
                                      self.client_address[0]))
      elif path == "/api/fixed/nonce":  # before pairing: carries no secret and opens nothing
        self._json(200, backend.fixed_nonce(self.client_address[0]))
      elif path == "/api/fixed/check":  # asked by a page served by address: it may read only this yes or no
        n = (parse_qs(query).get("n") or [""])[0]
        body = json.dumps(backend.fixed_check(n, self.client_address[0])).encode()
        self._send(200, body, "application/json; charset=utf-8", extra={"Access-Control-Allow-Origin": "*"})
      elif path in ("/api/clip.m3u8", "/api/clip.ts"):  # the video player can't send the token: the clip's own ticket
        q = parse_qs(query)
        ticket = (q.get("ticket") or [""])[0]
        if path == "/api/clip.m3u8":
          playlist = backend.clip_playlist(ticket)
          if playlist is None:
            self._json(404, {"error": "Esse link do clipe expirou. Abra a marca de novo."})
          else:
            self._send(200, playlist.encode(), "application/vnd.apple.mpegurl", compress=False)
        else:
          c = backend.clip(ticket)
          n = (q.get("n") or [""])[0]  # one minute, for the player; none: the whole clip, the file to download
          if c is None or (n and (not n.isdigit() or int(n) >= len(c["files"]))) or (not n and "download" not in q):
            self._json(404, {"error": "Esse link do clipe expirou. Abra a marca de novo."})
          else:
            extra = {"Content-Disposition": f'attachment; filename="{c["name"]}.ts"'} if "download" in q else None
            files = [c["files"][int(n)]] if n else c["files"]
            self._send(200, b"".join(Path(f).read_bytes() for f in files), "video/mp2t", compress=False, extra=extra)
      elif path == "/api/options/export":
        if self._who() is not None:
          self._json(200, backend.options_export())
      elif path == "/api/live":
        if self._who() is not None:
          self._json(200, backend.live())
      elif path == "/api/health":
        if self._who() is not None:
          self._json(200, backend.health())
      elif path == "/api/share":
        who = self._who()
        if who is not None:
          self._json(200, backend.share_state())
      elif path == "/api/nav":
        who = self._who()
        if who is not None:
          self._json(200, backend.nav_state())
      elif path == "/api/limit":
        who = self._who()
        if who is not None:
          self._json(200, backend.limit_state())
      elif path == "/api/phones":
        who = self._who()
        if who is not None:
          self._json(200, backend.phones(who))
      else:
        self._json(404, {"error": "Não encontrado."})

    def do_POST(self):
      if not self._allowed():
        return
      path = self.path.split("?", 1)[0]
      if path == "/api/pair":
        body = self._body()
        if body is not None:
          self._json(*backend.pair(str(body.get("pin", "")), str(body.get("name", ""))))
        return
      routes = {
        "/api/option": lambda who, b: backend.set_option(who, str(b.get("key", "")), b.get("value")),
        "/api/mark": lambda who, b: backend.mark(who, str(b.get("reason", "")), b.get("note", ""), b.get("delay_s", 0.), at=b.get("at"),
                                                 window=b.get("window")),
        "/api/mark/classify": lambda who, b: backend.classify(who, str(b.get("id", "")), str(b.get("reason", "")), b.get("note", ""),
                                                             b.get("replace_note") is True),
        "/api/reasons": lambda who, b: backend.add_reason(who, str(b.get("label", ""))),
        "/api/reasons/remove": lambda who, b: backend.remove_reason(who, str(b.get("key", ""))),
        "/api/radars": lambda who, b: backend.radars_update(who),
        "/api/radars/edit": lambda who, b: backend.radars_edit(who, b),
        "/api/radars/pin": lambda who, b: backend.radars_pin(),
        "/api/radars/add": lambda who, b: backend.radars_add(who, b),
        "/api/radars/delete": lambda who, b: backend.radars_delete(who, str(b.get("key", ""))),
        "/api/radars/import": lambda who, b: backend.radars_import(who, b.get("edits")),
        "/api/options/import": lambda who, b: backend.options_import(who, b.get("options"), bool(b.get("apply")), b.get("profiles")),
        "/api/mark/clip": lambda who, b: backend.clip_ticket(who, str(b.get("id", ""))),
        "/api/missions": lambda who, b: backend.missions_action(who, b),
        "/api/trip/hide": lambda who, b: backend.trip_hide(who, str(b.get("route", "")), b.get("hidden") is not False),
        "/api/profiles/save": lambda who, b: backend.profile_save(who, b.get("name", "")),
        "/api/profiles/delete": lambda who, b: backend.profile_delete(who, str(b.get("name", ""))),
        "/api/update": lambda who, b: backend.update_action(who, str(b.get("action", "")), str(b.get("branch", ""))),
        "/api/phones/revoke": lambda who, b: backend.revoke_phone(who, str(b.get("id", ""))),
        "/api/comma/set": lambda who, b: backend.comma_set(who, str(b.get("key", "")), b.get("value")),
        "/api/comma/action": lambda who, b: backend.comma_action(who, str(b.get("action", ""))),
        "/api/share": lambda who, b: backend.share_add(who, str(b.get("id", "")), b.get("with_note") is True),
        "/api/share/cancel": lambda who, b: backend.share_cancel(who, str(b.get("id", ""))),
        "/api/share/forget": lambda who, b: backend.share_forget(who),
        "/api/errors/preview": lambda who, b: backend.errors_preview(who),
        "/api/errors/send": lambda who, b: backend.errors_send(who),
        "/api/nav/search": lambda who, b: backend.nav_search(who, str(b.get("q", ""))),
        "/api/nav/go": lambda who, b: backend.nav_go(who, b),
        "/api/nav/stop": lambda who, b: backend.nav_stop(who),
        "/api/nav/place": lambda who, b: backend.nav_place(who, b),
        "/api/nav/place/remove": lambda who, b: backend.nav_place_remove(who, str(b.get("name", ""))),
        "/api/nav/token": lambda who, b: backend.nav_token(who, b.get("token", "")),
        "/api/limit/install": lambda who, b: backend.limit_install(who),
        "/api/limit/download": lambda who, b: backend.limit_download(who, str(b.get("area", ""))),
        "/api/limit/cancel": lambda who, b: backend.limit_cancel(who),
      }
      if path not in routes and path != "/api/logout":
        self._json(404, {"error": "Não encontrado."})
        return
      who = self._who()
      if who is None:
        return
      if path == "/api/logout":
        self._json(*backend.logout(who))
        return
      body = self._body(MAX_IMPORT_BODY if path == "/api/radars/import" else MAX_BODY)
      if body is not None:
        self._json(*routes[path](who, body))

  return Handler


def make_server(backend: PanelBackend, host: str = "0.0.0.0", port: int = phone.PORT) -> ThreadingHTTPServer:
  server = ThreadingHTTPServer((host, port), make_handler(backend))
  server.daemon_threads = True
  return server


def serve(backend: PanelBackend, retry: float = SERVE_RETRY) -> None:
  # selfdrived takes a dead process as a system fault and disengages: the panel must never go down with the car
  from openpilot.common.swaglog import cloudlog
  while True:
    try:
      server = make_server(backend)
      cloudlog.info(f"hexaweb: serving on port {phone.PORT}")
      server.serve_forever()
    except Exception:
      cloudlog.exception("hexaweb: server failed, trying again")
      time.sleep(retry)


def main():
  from openpilot.cereal import messaging
  from openpilot.common.params import Params
  from openpilot.common.swaglog import cloudlog

  params = Params()
  sm = messaging.SubMaster(["deviceState", "carState", "selfdriveState", "carControl", "carOutput", "radarState", "gpsLocationExternal",
                            "onroadEvents", "drivingModelData", "driverMonitoringState", "managerState", "lateralTorqueParameters",
                            "vehicleParameters", "lateralDelay", "peripheralState", "navInstruction", "navRoute"])

  def vehicle_state() -> VehicleState:
    sm.update(0)
    if sm.recv_frame["deviceState"] > 0:
      onroad = sm["deviceState"].started
    else:
      onroad = not params.get_bool("IsOffroad")
    if not onroad:
      return VehicleState(onroad=False)
    if not (sm.alive["carState"] and sm.alive["selfdriveState"]):
      return VehicleState(onroad=True)  # no fresh data: read only
    cs = sm["carState"]
    return VehicleState(onroad=True, parked=cs.gearShifter == "park", standstill=cs.standstill,
                        engaged=sm["selfdriveState"].enabled)

  # mapd's messages on their own sockets, decoded from raw bytes: never in the SubMaster, where a mapd of another schema would
  # raise inside sm.update and take the phone panel down (CTO review of codeweb-09)
  mapd_socks = {s: messaging.sub_sock(s, conflate=True) for s in ("mapdOut", "mapdExtendedOut")}
  mapd_last: dict = {"mapdOut": (None, -1e9), "mapdExtendedOut": (None, -1e9)}
  mapd_lock = threading.Lock()

  def limit_view() -> dict:
    with mapd_lock:
      now = time.monotonic()
      for name, sock in mapd_socks.items():
        got = speed_limit.decode(sock.receive(non_blocking=True), name)
        if got is not None:
          mapd_last[name] = (got, now)
      out, at = mapd_last["mapdOut"]
      progress, p_at = mapd_last["mapdExtendedOut"]
      return speed_limit.view(out, now - at, speed_limit.option(params), speed_limit.installed(), progress if now - p_at < 10 else None)

  def unmetered() -> bool:
    sm.update(0)
    ds = sm["deviceState"]
    return sm.recv_frame["deviceState"] > 0 and str(ds.networkType) != "none" and not ds.networkMetered

  def network_kind() -> str:  # why a download can't start: no network at all, or a metered one (UI-19)
    sm.update(0)
    ds = sm["deviceState"]
    if sm.recv_frame["deviceState"] <= 0:
      return ""
    return "none" if str(ds.networkType) == "none" else "metered" if ds.networkMetered else "wifi"

  def free_bytes() -> int:
    import shutil
    try:
      return shutil.disk_usage("/data").free
    except OSError:
      return 0

  def live_state() -> dict:
    sm.update(0)
    now = time.monotonic()
    return hexa_live.snapshot(sm["carState"], sm["selfdriveState"], sm["carControl"], sm["carOutput"], sm["radarState"].leadOne,
                              sm["gpsLocationExternal"], now - sm.recv_time["gpsLocationExternal"], sm["deviceState"],
                              now - sm.recv_time["carState"] < 2., events=sm["onroadEvents"], model=sm["drivingModelData"],
                              monitoring=sm["driverMonitoringState"] if params.get_bool("AutoResume") else None,
                              peripheral=sm["peripheralState"] if sm.recv_frame["peripheralState"] > 0 else None) | \
      ({"limit": limit_view()} if speed_limit.option(params) != speed_limit.OFF else {})

  errors_at: list = [-ERRORS_EVERY_S, []]

  def health_state() -> dict:
    sm.update(0)

    def seen(name: str):
      return sm[name] if sm.recv_frame[name] > 0 else None

    manager = seen("managerState")
    now = time.monotonic()
    if now - errors_at[0] >= ERRORS_EVERY_S:  # the log is read at most every 30 s
      errors_at[0], errors_at[1] = now, hexa_live.recent_errors(Paths.swaglog_root(), limit=hexa_report.REPORT_ERRORS)
    return {"device": hexa_live.device_state(sm["deviceState"]), "down": hexa_live.processes_down(manager) if manager is not None else None,
            "learned": hexa_live.learned(seen("lateralTorqueParameters"), seen("vehicleParameters"), seen("lateralDelay")),
            "errors": errors_at[1], "v12": round(sm["peripheralState"].voltage / 1000., 1) if sm.recv_frame["peripheralState"] > 0 else None}

  def gps_fix() -> tuple[float, float, float, float] | None:
    sm.update(0)
    g = sm["gpsLocationExternal"]
    if not g.hasFix or g.horizontalAccuracy > radars.MIN_FIX_ACCURACY or time.monotonic() - sm.recv_time["gpsLocationExternal"] > hexa_live.GPS_MAX_AGE_S:
      return None
    return g.latitude, g.longitude, g.bearingDeg, g.speed

  def route_state() -> dict:
    with nav_lock:
      sm.update(0)
      alive = sm.recv_frame["navInstruction"] > 0 and time.monotonic() - sm.recv_time["navInstruction"] < 5 and sm.valid["navInstruction"]
      pts = [(c.latitude, c.longitude) for c in sm["navRoute"].coordinates] if sm.recv_frame["navRoute"] > 0 else []
      return hexa_nav.route_view(sm["navInstruction"], pts, alive)

  def car_position() -> tuple[float, float] | None:
    fix = gps_fix()
    return (fix[0], fix[1]) if fix is not None else None

  nav_lock = threading.Lock()
  mapd_pm = messaging.PubMaster(["mapdIn"])
  mapd_pm_lock = threading.Lock()  # sent from the request threads and from the limit worker

  def send_mapd(msg) -> None:
    with mapd_pm_lock:
      mapd_pm.send("mapdIn", msg)
  from openpilot.common.hardware.hw import Paths
  from openpilot.selfdrive.hexa import edition
  backend = PanelBackend(params, vehicle_state, log_event=lambda fields: cloudlog.event("hexa_mark", **fields),
                         log_root=Paths.log_root(), live_state=live_state, radar_path=radars.updated_path(), gps_fix=gps_fix,
                         health_state=health_state, navigator=hexa_nav.Navigator(params, position=car_position), route_state=route_state,
                         sharer=hexa_share.Sharer(params, Paths.log_root(), edition=edition.EDITION,
                                                  offroad=lambda: not backend.vehicle_state().onroad),  # the car on stops a cut
                         limit_state=limit_view, send_mapd=send_mapd, network_ok=unmetered, free_bytes=free_bytes,
                         network_kind=network_kind)

  def share_worker():
    # one cut or one send at a time, car off for the cut, a network that isn't metered for the send
    net = messaging.SubMaster(["deviceState"])
    while True:
      time.sleep(SHARE_EVERY_S)
      try:
        net.update(0)
        ds = net["deviceState"]
        network_ok = net.recv_frame["deviceState"] > 0 and str(ds.networkType) != "none" and not ds.networkMetered
        done = backend.share_step(network_ok)
        if done not in ("nada", "desligado", "esperando rede"):
          cloudlog.info(f"hexaweb: share {done}")
      except Exception:
        cloudlog.exception("hexaweb: share worker")

  def limit_worker():
    while True:
      time.sleep(LIMIT_EVERY_S)
      try:
        if speed_limit.option(params) != speed_limit.OFF and backend.limit_tick():
          cloudlog.info("hexaweb: map download stopped, the car turned on")
      except Exception:
        cloudlog.exception("hexaweb: limit worker")

  def watch_bookmarks():
    # every bookmark in the log becomes a mark: with the reason from the phone, or "sem motivo" to classify later
    watched = messaging.SubMaster(["userBookmark", "selfdriveState", "onroadEvents"], poll="userBookmark")
    while True:
      watched.update(100)
      if watched.updated["userBookmark"]:
        backend.on_bookmark(watched.logMonoTime["userBookmark"])
      backend.on_drive_state(watched["selfdriveState"].enabled, watched["onroadEvents"])

  threading.Thread(target=watch_bookmarks, daemon=True).start()
  threading.Thread(target=share_worker, daemon=True).start()
  threading.Thread(target=limit_worker, daemon=True).start()
  threading.Thread(target=hexa_mdns.serve, daemon=True).start()  # lizpilot.local on the local network
  serve(backend)


if __name__ == "__main__":
  main()
