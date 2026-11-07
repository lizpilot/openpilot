"""The road's limit from the map (speed_limit.py): the warning rule, mapd's messages from raw bytes, the pinned binary checked
when it runs, the install and download rules, and what the phone shows. CTO review of codeweb-09: one test per finding."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from openpilot.selfdrive.hexa import speed_limit as sl

DT = 0.01
KPH = 1 / 3.6
REPO = Path(sl.__file__).resolve().parents[3]


def run(watch, v_kph, limit_kph, seconds, fresh=True):
  hits = 0
  for _ in range(int(round(seconds / DT))):
    hits += watch.update(v_kph * KPH, limit_kph * KPH, fresh)
  return hits


def settled(limit_kph=60, v_kph=50):
  w = sl.SpeedLimitWatch(DT)
  run(w, v_kph, limit_kph, sl.SETTLE_S + 0.1)
  return w


# *** the warning (findings 4 and 8) ***

def test_tolerance_is_contran_798_on_the_measured_speed():
  assert sl.tolerance_kph(60) == 7 and sl.tolerance_kph(100) == 7
  assert sl.tolerance_kph(110) == pytest.approx(7.7) and sl.tolerance_kph(130) == pytest.approx(9.1)
  assert "CONTRAN 798/2020" in Path(sl.__file__).read_text()


def test_warns_once_above_limit_plus_tolerance_for_two_seconds():
  w = settled(60)
  assert run(w, 66, 60, 10) == 0  # within the tolerance: never
  assert run(w, 68, 60, 1.9) == 0  # above, but not long enough
  assert run(w, 68, 60, 0.2) == 1
  assert run(w, 75, 60, 10) == 0  # once per overshoot


def test_a_limit_counts_only_after_it_held():
  w = sl.SpeedLimitWatch(DT)
  assert run(w, 90, 60, sl.SETTLE_S - 0.1) == 0  # 60 just appeared: not yet the limit
  assert run(w, 90, 60, 2.5) == 1


def test_a_50ms_drop_to_zero_every_2_5s_warns_once():
  w = settled(60)
  hits = 0
  for _ in range(24):  # 60 s
    hits += run(w, 75, 60, 2.45) + run(w, 75, 0, 0.05)
  assert hits == 1


def test_a_limit_flapping_60_61_warns_once():
  w = settled(60)
  hits = 0
  for i in range(60):
    hits += run(w, 80, 60 if i % 2 else 61, 1.)
  assert hits == 1


def test_a_lower_limit_that_holds_warns_again_a_higher_one_doesnt():
  w = settled(80, 70)
  assert run(w, 95, 80, 3) == 1
  assert run(w, 95, 100, sl.SETTLE_S + 1) == 0  # the limit went up: no news, and 95 is under it anyway
  assert run(w, 95, 60, sl.SETTLE_S + 2.5) == 1  # went down to 60 and held: news


def test_under_the_limit_for_a_while_arms_again():
  w = settled(60)
  assert run(w, 75, 60, 3) == 1
  assert run(w, 55, 60, 1) == 0 and run(w, 75, 60, 3) == 0  # a second under it is no new overshoot
  assert run(w, 55, 60, sl.REARM_UNDER_S + 0.1) == 0
  assert run(w, 75, 60, 3) == 1


def test_no_limit_no_warning():
  w = sl.SpeedLimitWatch(DT)
  assert run(w, 120, 0, 10) == 0
  assert run(w, 120, 60, 10, fresh=False) == 0  # mapd stopped: no stale limit
  w = settled(10, 5)
  assert run(w, 40, 10, 5) == 0  # under MIN_LIMIT_KPH


# *** mapd's messages from raw bytes (finding 1) ***

def _event(fill):
  from openpilot.cereal import log
  ev = log.Event.new_message(logMonoTime=1, valid=True)
  fill(ev)
  return ev.to_bytes()


def test_decode_mapd_out_and_progress():
  def out(ev):
    m = ev.init("mapdOut")
    m.speedLimit, m.nextSpeedLimit, m.nextSpeedLimitDistance, m.tileLoaded, m.roadName = 60 * KPH, 40 * KPH, 231.7, True, "Av. Roberto Silveira"
  d = sl.decode(_event(out), "mapdOut")
  assert d["speedLimit"] == pytest.approx(60 * KPH, rel=1e-5) and d["roadName"] == "Av. Roberto Silveira" and d["tileLoaded"]

  def ext(ev):
    p = ev.init("mapdExtendedOut").downloadProgress
    p.active, p.downloadedFiles, p.totalFiles = True, 2, 6
  assert sl.decode(_event(ext), "mapdExtendedOut") == {"active": True, "done": 2, "total": 6}


def test_decode_never_raises_on_another_union_or_bad_bytes():
  other = _event(lambda ev: setattr(ev.init("carState"), "vEgo", 10.))
  assert sl.decode(other, "mapdOut") is None  # another union: dropped, not KeyError
  for bad in (b"", None, b"\x00" * 7, b"not capnp at all" * 4, bytes(range(256)), other[:-5]):
    assert sl.decode(bad, "mapdOut") is None


def test_selfdrived_reads_mapd_on_its_own_socket_inside_the_try():
  src = (REPO / "openpilot/selfdrive/selfdrived/selfdrived.py").read_text()
  assert "'mapdOut'" not in src.split("self.sm = messaging.SubMaster(")[1].split(")")[0]  # never in the SubMaster
  assert "messaging.sub_sock('mapdOut', conflate=True)" in src
  body = src.split("def check_speed_limit")[1].split("def params_thread")[0]
  assert body.index("try:") < body.index("speed_limit.decode(self.mapd_sock.receive(non_blocking=True), 'mapdOut')")
  assert 'NON_DRIVING_PROCESSES = {"hexaweb", "mapd"}' in src
  web = (REPO / "openpilot/system/hexaweb/hexaweb.py").read_text()
  sub = web.split("sm = messaging.SubMaster(")[1].split("])")[0]
  assert "mapd" not in sub and "speed_limit.decode(sock.receive(non_blocking=True), name)" in web


# *** the pinned binary, checked when it runs (finding 2) ***

def test_mapd_runs_only_when_the_binary_is_the_pinned_one(tmp_path, monkeypatch):
  binary = tmp_path / "mapd"
  binary.write_bytes(b"\x7fELF mapd 8e5eee6")
  binary.chmod(0o755)
  good = hashlib.sha256(binary.read_bytes()).hexdigest()
  assert not sl.binary_ok(binary, "")  # no pin: never
  assert sl.binary_ok(binary, good)
  binary.write_bytes(b"\x7fELF another mapd, another schema")  # swapped after the install
  os.utime(binary, ns=(1, 1))
  assert not sl.binary_ok(binary, good)
  binary.chmod(0o644)
  assert not sl.binary_ok(binary, hashlib.sha256(binary.read_bytes()).hexdigest())  # not executable
  monkeypatch.setattr(sl, "MAPD_BIN", binary)
  monkeypatch.setattr(sl, "MAPD_SHA256", "")
  assert not sl.should_run(True, Store(HexaSpeedLimit=1), None)


def test_the_hash_is_cached_by_size_and_mtime(tmp_path, monkeypatch):
  binary = tmp_path / "mapd"
  binary.write_bytes(b"x" * 1000)
  calls = []
  real = hashlib.sha256
  monkeypatch.setattr(sl.hashlib, "sha256", lambda *a: calls.append(1) or real(*a))
  sl._hash_cache.clear()
  for _ in range(5):
    sl.file_sha256(binary)
  assert len(calls) == 1


class Store(dict):
  def get(self, k):
    return dict.get(self, k)


def test_option_values():
  assert sl.option(Store()) == sl.OFF
  assert sl.option(Store(HexaSpeedLimit=2)) == sl.WARN
  assert sl.option(Store(HexaSpeedLimit="1")) == sl.SHOW
  assert sl.option(Store(HexaSpeedLimit=7)) == sl.OFF and sl.option(Store(HexaSpeedLimit="x")) == sl.OFF


def test_install_checks_the_hash_and_writes_for_good(tmp_path, monkeypatch):
  data = b"\x7fELF fake mapd"
  good = hashlib.sha256(data).hexdigest()
  dest = tmp_path / "lizpilot/mapd/mapd"
  with pytest.raises(sl.InstallError, match="falta a revisão"):
    sl.install("", "", dest, fetch=lambda u: data)
  with pytest.raises(sl.InstallError, match="falta a revisão"):
    sl.install("http://insecure/mapd", good, dest, fetch=lambda u: data)
  with pytest.raises(sl.InstallError, match="não é o mapd"):
    sl.install("https://x/mapd", "0" * 64, dest, fetch=lambda u: data)
  assert not dest.exists()
  synced = []
  real_fsync = os.fsync
  monkeypatch.setattr(sl.os, "fsync", lambda fd: synced.append(fd) or real_fsync(fd))
  assert sl.install("https://x/mapd", good, dest, fetch=lambda u: data) == good
  assert synced and dest.read_bytes() == data and os.access(dest, os.X_OK) and sl.binary_ok(dest, good)
  assert json.loads((dest.parent / "VERSION").read_text()) == {"version": sl.MAPD_VERSION, "sha256": good}


def test_the_shipped_pins_are_empty_or_complete():
  assert (sl.MAPD_URL == "" and sl.MAPD_SHA256 == "") or (sl.MAPD_URL.startswith("https://") and len(sl.MAPD_SHA256) == 64)
  assert sl.MAPD_VERSION == "8e5eee6-msgq25"  # the source commit of the schema, plus our msgq patch (MAP-02)


# *** downloads: car off, unmetered network, free space, and stopped when the car turns on (finding 5) ***

def test_install_and_download_conditions():
  sl.check_conditions(True, True, 3 * 1024 ** 3, 2 * 1024 ** 3)
  with pytest.raises(sl.InstallError, match="desligado"):
    sl.check_conditions(False, True, 10 ** 12, 1)
  with pytest.raises(sl.InstallError, match="Wi-Fi"):
    sl.check_conditions(True, False, 10 ** 12, 1)
  with pytest.raises(sl.InstallError, match="espaço"):
    sl.check_conditions(True, True, 10, 1000)
  assert sl.area_need("br_state.RJ") < sl.area_need("nation.BR")
  with pytest.raises(sl.InstallError):
    sl.area_need("nation.US")


def test_the_car_turning_on_stops_a_download_once():
  g = sl.DownloadGuard()
  assert not g.update(False, True)  # parked: let it go
  assert g.update(True, True)  # the car turned on: stop
  assert not g.update(True, True)  # asked once
  assert not g.update(False, False) and not g.update(True, False)  # parked again, then on with nothing downloading
  assert g.update(True, True)  # a download that starts with the car on: stopped too


def test_mapd_in_messages():
  m = sl.download_message("br_state.RJ")
  assert m.which() == "mapdIn" and str(m.mapdIn.type) == "download" and m.mapdIn.str == "br_state.RJ"
  assert str(sl.mapd_in("cancelDownload").mapdIn.type) == "cancelDownload"
  with pytest.raises(sl.InstallError):
    sl.download_message("nation.US;rm -rf")


def test_the_download_menu_has_every_area_offered_and_valid_rows():
  menu = json.loads((REPO / "mapd_download_menu.json").read_text())
  for key, _, _ in sl.AREAS:
    group, code = key.split(".")
    area = menu[group][code]
    box = area["bounding_box"]
    assert box["min_lat"] < box["max_lat"] and box["min_lon"] < box["max_lon"]
    rows = area.get("download_rows", [])
    assert rows == sorted(rows)
    for lat, lon0, lon1 in rows:  # on the 2-degree grid, inside the box rounded outward (mapd's rule)
      assert lat % 2 == 0 and lon0 % 2 == 0 and lon1 % 2 == 0 and lon0 < lon1
      assert box["min_lat"] - 2 <= lat <= box["max_lat"] and box["min_lon"] - 2 <= lon0 and lon1 <= box["max_lon"] + 2
  rj = menu["br_state"]["RJ"]["bounding_box"]
  for lat, lon in ((-22.9519, -43.2105), (-23.1394, -44.1689)):  # Rio (Cristo Redentor) and Ilha Grande
    assert rj["min_lat"] < lat < rj["max_lat"] and rj["min_lon"] < lon < rj["max_lon"]


# *** logs, process and the rest (findings 6, 7, 9) ***

def test_what_is_logged():
  from openpilot.cereal.services import SERVICE_LIST
  assert SERVICE_LIST["mapdOut"].should_log and SERVICE_LIST["mapdOut"].decimation is None  # rlog only, never the qlog
  assert not SERVICE_LIST["mapdExtendedOut"].should_log  # the position and the path ahead: not logged
  assert not SERVICE_LIST["mapdIn"].should_log


def test_mapd_runs_niced_on_two_cores_and_the_alert_yields_to_the_radar():
  pc = (REPO / "openpilot/system/manager/process_config.py").read_text()
  assert '["nice", "-n", "10", "env", "GOMAXPROCS=2", "./mapd"]' in pc
  ev = (REPO / "openpilot/selfdrive/selfdrived/events.py").read_text()
  assert "Priority.LOWEST, VisualAlert.none, AudibleAlert.prompt, 3.)  # under the radar's" in ev


def test_view_for_the_phone():
  out = {"speedLimit": 60 * KPH, "nextSpeedLimit": 40 * KPH, "nextSpeedLimitDistance": 231.7, "tileLoaded": True, "roadName": "Av. X"}
  v = sl.view(out, 0.2, sl.WARN, True, {"active": True, "done": 2, "total": 6})
  assert v["fresh"] and v["limit"] == 60 and v["next"] == 40 and v["next_m"] == 232 and v["road"] == "Av. X"
  assert v["download"] == {"active": True, "done": 2, "total": 6} and v["pinned"] is bool(sl.MAPD_URL and sl.MAPD_SHA256)  # the pins, once the CTO fills them
  assert sl.view(out, 5., sl.SHOW, True)["fresh"] is False
  assert sl.view(dict(out, speedLimit=0.), 0.1, sl.SHOW, True)["limit"] is None


def test_the_cereal_slots_are_mapds():
  from openpilot.cereal import log
  e = log.Event.new_message()
  e.init("mapdOut").speedLimit = 16.7
  assert e.which() == "mapdOut"
  assert log.OnroadEvent.EventName.speedLimitOver == 111


def test_coverage_tool_counts_the_km_with_a_limit():
  import importlib.util
  from openpilot.cereal import log
  spec = importlib.util.spec_from_file_location("cob", REPO / "tools/lizpilot/cobertura_limite.py")
  cob = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(cob)
  evs = []
  for i in range(2000):  # 20 s at 10 m/s = 200 m; the limit known for the second half
    t = 1_000_000_000_000 + i * 10_000_000
    cs = log.Event.new_message(logMonoTime=t)
    cs.init("carState").vEgo = 10.
    evs.append(cs)
    if i % 5 == 0:
      m = log.Event.new_message(logMonoTime=t)
      mo = m.init("mapdOut")
      mo.tileLoaded = True
      mo.speedLimit = 60 / 3.6 if i >= 1000 else 0.
      evs.append(m)
  c = cob.coverage([e.as_reader() for e in evs])
  assert c["km"] == pytest.approx(0.2, abs=1e-3) and c["km_limit"] == pytest.approx(0.1, abs=2e-3) and set(c["limits"]) == {60}


# *** "ajustar": the ACC's set speed follows the limit (CRUISE-01) ***

class Acc:
  """The ACC as LimitFollow sees it: a press of + or - moves the set speed one step after a short delay."""
  def __init__(self, set_kph, delay=20, step=5):
    self.set_kph, self.delay, self.step, self.pending = set_kph, delay, step, []

  def press(self, direction):
    if direction and not any(d == direction for _, d in self.pending):
      self.pending.append((self.delay, direction))

  def tick(self):
    keep = []
    for left, d in self.pending:
      if left <= 1:
        self.set_kph += d * self.step
      else:
        keep.append((left - 1, d))
    self.pending = keep


def follow(lf, acc, limit, seconds, enabled=True, driving=True, moving=True, gas=False, brake=False, bump=False):
  presses = []
  for _ in range(int(round(seconds / DT))):
    d = lf.update(enabled, driving, moving, acc.set_kph, limit, gas, brake, bump)
    presses.append(d)
    acc.press(d)
    acc.tick()
  return presses


def test_a_new_limit_takes_the_set_speed_there_and_stops():
  lf, acc = sl.LimitFollow(DT), Acc(60)
  p = follow(lf, acc, 80, 6)
  assert acc.set_kph == 80 and set(p) == {0, 1} and p[-1] == 0
  assert follow(lf, acc, 80, 5) == [0] * 500  # the same limit: nothing more
  follow(lf, acc, 50, 8)
  assert acc.set_kph == 50  # a lower limit: down with the minus


def test_steps_ceiling_floor_and_most_raise():
  for limit, start, end in ((63, 60, 65), (120, 100, 110), (20, 40, 40), (100, 50, 90), (25, 50, 50)):
    lf, acc = sl.LimitFollow(DT), Acc(start)
    follow(lf, acc, limit, 15)
    assert acc.set_kph == end, (limit, start, acc.set_kph)


def test_off_the_steps_by_less_than_one_is_left_alone():
  lf, acc = sl.LimitFollow(DT), Acc(78)
  assert set(follow(lf, acc, 80, 3)) == {0} and acc.set_kph == 78


def test_the_driver_wins_until_the_next_limit():
  for kw in ({"gas": True}, {"brake": True}):
    lf, acc = sl.LimitFollow(DT), Acc(60)
    follow(lf, acc, 80, .5)
    assert set(follow(lf, acc, 80, .1, **kw)) == {0}
    assert set(follow(lf, acc, 80, 5)) == {0} and acc.set_kph < 80  # same limit: it doesn't come back
    follow(lf, acc, 90, 8)
    assert acc.set_kph == 90  # a new limit: it does


def test_the_driver_moving_the_set_speed_the_other_way_ends_it():
  lf, acc = sl.LimitFollow(DT), Acc(60, delay=10)
  follow(lf, acc, 80, .3)
  acc.set_kph -= 10  # the driver holds the minus while we press the plus
  assert set(follow(lf, acc, 80, 5)) == {0}


def test_it_waits_with_openpilot_or_the_acc_off_stopped_or_a_bump_and_goes_on_after():
  for kw in ({"enabled": False}, {"driving": False}, {"moving": False}, {"bump": True}):
    lf, acc = sl.LimitFollow(DT), Acc(60)
    assert set(follow(lf, acc, 80, 2, **kw)) == {0}, kw
    follow(lf, acc, 80, 6)
    assert acc.set_kph == 80, kw


def test_an_acc_that_doesnt_answer_is_left_alone():
  lf, acc = sl.LimitFollow(DT), Acc(60, delay=10 ** 6)
  p = follow(lf, acc, 80, 5)
  assert p.count(1) == int(round(sl.FOLLOW_NO_RESPONSE_S / DT)) and p[-1] == 0


def test_never_back_and_forth():
  lf, acc = sl.LimitFollow(DT), Acc(60, step=15)  # an ACC that jumps past the target
  p = follow(lf, acc, 70, 6)
  assert -1 not in p and acc.set_kph == 75


def test_no_limit_nothing():
  lf, acc = sl.LimitFollow(DT), Acc(60)
  assert set(follow(lf, acc, 0, 3)) == {0}


def test_ajustar_is_an_option_and_card_asks_for_the_plus_and_the_minus():
  assert sl.option(Store(HexaSpeedLimit=3)) == sl.ADJUST
  from openpilot.selfdrive.hexa.options import get_option
  assert get_option("HexaSpeedLimit").choices[sl.ADJUST] == "ajustar"
  card = (REPO / "openpilot/selfdrive/car/card.py").read_text()
  assert "if speed_limit.option(self.params) == speed_limit.ADJUST:" in card
  assert "GwmSafetyFlags.ACC_PLUS.value | GwmSafetyFlags.ACC_MINUS.value" in card
  ctl = (REPO / "openpilot/selfdrive/controls/controlsd.py").read_text()
  assert "CC.cruiseControl.increase = CC.enabled and any(e.name == 'speedLimitUp'" in ctl
  assert "('bumpSlowdown', 'speedLimitDown')" in ctl
  sd = (REPO / "openpilot/selfdrive/selfdrived/selfdrived.py").read_text()
  assert "GwmSafetyFlags.ACC_PLUS) else None" in sd and "speed_limit.option(self.params) in (speed_limit.WARN, speed_limit.ADJUST)" in sd
  from openpilot.cereal import log
  assert log.OnroadEvent.EventName.speedLimitUp == 112 and log.OnroadEvent.EventName.speedLimitDown == 113
