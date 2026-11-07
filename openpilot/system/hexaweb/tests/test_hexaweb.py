import hashlib
import hmac
import json
from pathlib import Path
import re
import threading
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from openpilot.selfdrive.hexa import marks, phone
from openpilot.selfdrive.hexa import versions as hexa_versions
from openpilot.selfdrive.hexa.phone import VehicleState
from openpilot.system.hexaweb import hexaweb


class FakeStore:
  def __init__(self, **values):
    self.d = dict(values)

  def get(self, key):
    return self.d.get(key)

  def put(self, key, value):
    self.d[key] = value

  def get_bool(self, key):
    return bool(self.d.get(key, False))

  def put_bool(self, key, value):
    self.d[key] = bool(value)

  def remove(self, key):
    self.d.pop(key, None)


class Panel:
  def __init__(self):
    self.now = 1000.
    self.vs = VehicleState(onroad=False)
    self.store = FakeStore(GitBranch="h6gt-next", GitCommit="abcdef1234")
    self.logged: list[dict] = []
    self.backend = hexaweb.PanelBackend(self.store, lambda: self.vs, clock=lambda: self.now, mono=lambda: self.now,
                                        log_event=self.logged.append)
    self.server = hexaweb.make_server(self.backend, "127.0.0.1", 0)
    self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
    threading.Thread(target=self.server.serve_forever, daemon=True).start()

  def request(self, path, body=None, token=None, raw=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    req = urllib.request.Request(self.url + path, data=data, method="GET" if data is None else "POST")
    if data is not None:
      req.add_header("Content-Type", "application/json")
    if token:
      req.add_header("Authorization", "Bearer " + token)
    try:
      with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
      return e.code, dict(e.headers), e.read()

  def json(self, path, body=None, token=None, raw=None):
    status, _, data = self.request(path, body, token, raw)
    return status, json.loads(data)

  def show_pin(self) -> str:
    self.store.put(phone.PIN_PARAM, phone.new_pin(self.now))
    return phone.parse_pin(self.store.get(phone.PIN_PARAM), self.now)

  def pair(self, name="celular de teste") -> str:
    status, body = self.json("/api/pair", {"pin": self.show_pin(), "name": name})
    assert status == 200
    return body["token"]


@pytest.fixture
def panel():
  p = Panel()
  yield p
  p.server.shutdown()
  p.server.server_close()


def test_page_is_served_with_security_headers(panel):
  status, headers, body = panel.request("/")
  assert status == 200 and b"<title>LizPilot</title>" in body
  assert headers["X-Frame-Options"] == "DENY"
  assert headers["Cache-Control"] == "no-store"
  assert "default-src 'self'" in headers["Content-Security-Policy"]
  assert "img-src 'self' data: https://tile.openstreetmap.org;" in headers["Content-Security-Policy"]  # only the street map, never a script
  assert headers["Content-Security-Policy"].endswith("connect-src 'self' http://lizpilot.local:8088")  # only the fixed name, to check it
  assert panel.request("/manifest.webmanifest")[0] == 200
  assert panel.request("/../hexaweb.py")[0] == 404


def test_shared_files_are_served(panel):
  # fonts ship with the comma: on its hotspot the phone has no internet
  for path, ctype in (("/lp.css", "text/css"), ("/i18n.js", "text/javascript"), ("/lp-logic.js", "text/javascript"), ("/fonts/barlow-400.woff2", "font/woff2"),
                      ("/fonts/barlow-semi-condensed-700.woff2", "font/woff2"), ("/i18n/en.json", "application/json"), ("/i18n/es.json", "application/json")):
    status, headers, body = panel.request(path)
    assert status == 200 and headers["Content-Type"].startswith(ctype) and body, path
  assert isinstance(json.loads(panel.request("/i18n/es.json")[2]), dict)  # completeness: selfdrive/hexa/tests/test_i18n.py
  assert panel.request("/i18n/pt.json")[0] == 404  # Portuguese is the source: no catalog
  assert panel.request("/i18n/../hexaweb.py")[0] == 404


def test_api_needs_pairing(panel):
  assert panel.json("/api/state")[0] == 401
  assert panel.json("/api/state", token="made-up")[0] == 401
  assert panel.json("/api/option", {"key": "LaneOffsetEnabled", "value": True})[0] == 401
  assert panel.store.get("LaneOffsetEnabled") is None


def test_pairing_needs_a_code_on_the_comma(panel):
  status, body = panel.json("/api/pair", {"pin": "123456", "name": "x"})
  assert status == 403 and "Gere um código" in body["error"]


def test_pairing_code_is_single_use_and_expires(panel):
  pin = panel.show_pin()
  assert panel.json("/api/pair", {"pin": pin, "name": "a"})[0] == 200
  assert panel.json("/api/pair", {"pin": pin, "name": "b"})[0] == 403  # already used
  pin = panel.show_pin()
  panel.now += phone.PIN_TTL + 1
  assert panel.json("/api/pair", {"pin": pin, "name": "c"})[0] == 403  # expired


def test_wrong_codes_lock_the_code(panel):
  pin = panel.show_pin()
  wrong = "000000" if pin != "000000" else "111111"
  for _ in range(phone.MAX_PIN_FAILURES):
    assert panel.json("/api/pair", {"pin": wrong, "name": "x"})[0] == 403
  assert panel.store.get(phone.PIN_PARAM) is None
  assert panel.json("/api/pair", {"pin": pin, "name": "x"})[0] == 403
  assert panel.store.get(phone.AUDIT_PARAM)[-1]["event"] == "pin_locked"


def test_state_lists_options_and_device(panel):
  token = panel.pair("celular do dono")
  status, s = panel.json("/api/state", token=token)
  assert status == 200
  assert s["device"] == {"branch": "h6gt-next", "commit": "abcdef1", "version": "", "edition": "h6gt"}  # the phone's first Usar | Melhorar
  assert s["you"] == "celular do dono"
  keys = {o["key"] for g in s["groups"] for o in g["options"]}
  assert {"LaneOffsetEnabled", "HexaWebEnabled"} <= keys
  assert s["audit"][0]["event"] == "paired"
  assert s["titles"]["LaneOffsetEnabled"] == "posição na faixa pelo volante"


def test_state_says_if_the_comma_longitudinal_is_on_and_never_calibrated(panel):
  token = panel.pair()
  assert panel.json("/api/state", token=token)[1]["longitudinal"] == {"enabled": False, "calibrated": False}
  panel.store.put_bool("AlphaLongitudinalEnabled", True)
  assert panel.json("/api/state", token=token)[1]["longitudinal"] == {"enabled": True, "calibrated": False}
  bump = next(o for g in panel.json("/api/state", token=token)[1]["groups"] for o in g["options"] if o["key"] == "BumpSlowdown")
  assert bump["testing"] is True and bump["value"] is False


def test_set_option_parked_and_audited(panel):
  token = panel.pair()
  panel.vs = VehicleState(onroad=True, parked=True, standstill=True, engaged=False)
  status, body = panel.json("/api/option", {"key": "LaneOffsetEnabled", "value": True}, token=token)
  assert status == 200 and "próximo engate" in body["message"]
  assert panel.store.get("LaneOffsetEnabled") is True
  last = panel.store.get(phone.AUDIT_PARAM)[-1]
  assert last["event"] == "set" and last["key"] == "LaneOffsetEnabled" and last["old"] is False and last["new"] is True


def test_driving_option_is_read_only_while_driving(panel):
  token = panel.pair()
  panel.vs = VehicleState(onroad=True, parked=False, standstill=False, engaged=True)
  status, body = panel.json("/api/option", {"key": "LaneOffsetEnabled", "value": True}, token=token)
  assert status == 409 and "só leitura" in body["error"]
  assert panel.store.get("LaneOffsetEnabled") is None
  assert panel.store.get(phone.AUDIT_PARAM)[-1]["event"] == "denied"
  _, s = panel.json("/api/state", token=token)
  lane = next(o for g in s["groups"] for o in g["options"] if o["key"] == "LaneOffsetEnabled")
  assert lane["writable"] is False and lane["reason"]


def test_unknown_vehicle_state_is_read_only(panel):
  token = panel.pair()

  def broken():
    raise RuntimeError("no messages")
  panel.backend._vehicle_state = broken
  assert panel.json("/api/option", {"key": "LaneOffsetEnabled", "value": True}, token=token)[0] == 409


def test_phone_cannot_turn_on_the_panel(panel):
  token = panel.pair()
  status, _ = panel.json("/api/option", {"key": "HexaWebEnabled", "value": False}, token=token)
  assert status == 409


def test_bad_requests(panel):
  token = panel.pair()
  assert panel.json("/api/option", {"key": "Nope", "value": True}, token=token)[0] == 404
  assert panel.json("/api/option", {"key": "LaneOffsetEnabled", "value": "yes"}, token=token)[0] == 400
  assert panel.json("/api/option", raw=b"not json", token=token)[0] == 400
  assert panel.json("/api/option", raw=b"[1, 2]", token=token)[0] == 400
  assert panel.json("/api/option", raw=b"{" + b" " * (hexaweb.MAX_BODY + 1) + b"}", token=token)[0] == 413
  assert panel.json("/api/nope", {}, token=token)[0] == 404


def test_logout_revokes_only_that_phone(panel):
  a, b = panel.pair("a"), panel.pair("b")
  assert panel.json("/api/logout", {}, token=a)[0] == 200
  assert panel.json("/api/state", token=a)[0] == 401
  assert panel.json("/api/state", token=b)[0] == 200


def test_only_local_network(panel, monkeypatch):
  monkeypatch.setattr(phone, "is_local_address", lambda ip: False)
  assert panel.request("/")[0] == 403
  assert panel.json("/api/pair", {"pin": "123456"})[0] == 403


# *** marks ***

def test_marks_need_pairing(panel):
  assert panel.json("/api/marks")[0] == 401
  assert panel.json("/api/mark", {"reason": "curva"})[0] == 401


def test_phone_mark_while_driving(panel):
  token = panel.pair("passageiro")
  panel.vs = VehicleState(onroad=True, parked=False, standstill=False, engaged=True)  # marks never change driving
  status, body = panel.json("/api/mark", {"reason": "balancou", "note": " no  trânsito "}, token=token)
  assert status == 200
  m = body["mark"]
  assert (m["reason"], m["label"], m["note"], m["source"], m["who"]) == ("balancou", "balançou sem motivo", "no trânsito", "celular", "passageiro")
  assert "_req_mono_ns" not in m
  assert panel.store.get(marks.BOOKMARK_REQUEST_PARAM) is True  # the comma raises its own bookmark
  assert panel.logged[-1]["reason"] == "balancou" and panel.logged[-1]["mono_ns"] is None


def test_mark_rate_limit_and_unknown_reason(panel):
  token = panel.pair()
  assert panel.json("/api/mark", {"reason": "nope"}, token=token)[0] == 400
  assert panel.json("/api/mark", {"reason": "curva"}, token=token)[0] == 200
  assert panel.json("/api/mark", {"reason": "curva"}, token=token)[0] == 429
  panel.now += marks.MIN_MARK_INTERVAL
  assert panel.json("/api/mark", {"reason": "curva"}, token=token)[0] == 200


def test_bookmark_gets_the_phone_reason_with_the_exact_log_time(panel):
  token = panel.pair()
  assert panel.json("/api/mark", {"reason": "freada"}, token=token)[0] == 200
  mono_ns = int(panel.now * 1e9) + 300_000_000  # the comma's bookmark, 0.3 s later
  panel.backend.on_bookmark(mono_ns)
  _, s = panel.json("/api/marks", token=token)
  assert s["pending"] == 0 and len(s["marks"]) == 1
  assert s["marks"][0]["mono_ns"] == mono_ns and s["marks"][0]["reason"] == "freada"
  assert panel.logged[-1]["mono_ns"] == mono_ns


def test_comma_flag_is_classified_later(panel):
  token = panel.pair("dono")
  flag = panel.backend.on_bookmark(123_000_000_000)  # pressed on the comma while driving
  _, s = panel.json("/api/marks", token=token)
  assert s["pending"] == 1 and s["marks"][0]["reason"] is None
  status, body = panel.json("/api/mark/classify", {"id": flag["id"], "reason": "soltou", "note": "saída da curva"}, token=token)
  assert status == 200 and body["mark"]["label"] == "soltou sozinho" and body["mark"]["who"] == "dono"
  _, s = panel.json("/api/marks", token=token)
  assert s["pending"] == 0
  assert panel.logged[-1] == {**panel.logged[-1], "mono_ns": 123_000_000_000, "reason": "soltou"}
  assert panel.json("/api/mark/classify", {"id": "nope", "reason": "soltou"}, token=token)[0] == 404
  assert panel.json("/api/mark/classify", {"id": flag["id"], "reason": "nope"}, token=token)[0] == 400


def test_custom_reasons(panel):
  token = panel.pair()
  status, body = panel.json("/api/reasons", {"label": "Ponte"}, token=token)
  assert status == 200 and body["reason"]["key"] == "c-ponte"
  assert panel.json("/api/reasons", {"label": "ponte"}, token=token)[0] == 400  # duplicate
  _, s = panel.json("/api/marks", token=token)
  assert s["reasons"][-1] == {"key": "c-ponte", "label": "Ponte", "custom": True}
  assert panel.json("/api/mark", {"reason": "c-ponte"}, token=token)[0] == 200
  assert panel.json("/api/reasons/remove", {"key": "balancou"}, token=token)[0] == 404  # defaults stay
  assert panel.json("/api/reasons/remove", {"key": "c-ponte"}, token=token)[0] == 200
  _, s = panel.json("/api/marks", token=token)
  assert all(r["key"] != "c-ponte" for r in s["reasons"])
  assert s["marks"][0]["label"] == "Ponte"  # old marks keep their label


def test_export_marks(panel):
  token = panel.pair()
  assert panel.json("/api/marks/export")[0] == 401
  panel.backend.on_bookmark(42_000_000_000)
  status, body = panel.json("/api/marks/export", token=token)
  assert status == 200 and body["exported_at"] == int(panel.now)
  assert len(body["marks"]) == 1 and body["marks"][0]["mono_ns"] == 42_000_000_000


# *** replay ***

@pytest.fixture
def replay_panel(panel, tmp_path):
  from openpilot.selfdrive.hexa.tests.test_timeline import write_rlog
  seg = tmp_path / "00000007--abc--9"
  seg.mkdir()
  write_rlog(seg / "rlog.zst", seconds=4.0, bookmark_at=2.0)
  panel.backend.log_root = str(tmp_path)
  return panel


def test_replay_page_and_list(replay_panel):
  status, _, body = replay_panel.request("/replay")
  assert status == 200 and b"<title>Hexa \xc2\xb7 Replay</title>" in body
  assert replay_panel.json("/api/replay/list")[0] == 401
  token = replay_panel.pair()
  status, body = replay_panel.json("/api/replay/list", token=token)
  assert status == 200 and body["segments"] == [{"id": "00000007--abc--9", "label": "00000007--abc--9"}]


def test_replay_only_with_the_car_off(replay_panel):
  token = replay_panel.pair()
  replay_panel.vs = VehicleState(onroad=True, parked=True, standstill=True, engaged=False)
  status, body = replay_panel.json("/api/replay/data?id=00000007--abc--9", token=token)
  assert status == 409 and "desligado" in body["error"]
  replay_panel.vs = VehicleState(onroad=False)
  status, body = replay_panel.json("/api/replay/data?id=00000007--abc--9", token=token)
  assert status == 200 and len(body["frames"]) >= 39 and any(e["kind"] == "mark" for e in body["events"])


def test_replay_ids_come_only_from_the_list(replay_panel):
  token = replay_panel.pair()
  for bad in ("nope", "../../etc/passwd", "00000007--abc--9/../x", ""):
    assert replay_panel.json("/api/replay/data?id=" + bad, token=token)[0] == 404, bad


def test_replay_cache_is_bounded(replay_panel, tmp_path):
  from openpilot.selfdrive.hexa.tests.test_timeline import write_rlog
  token = replay_panel.pair()
  for i in range(hexaweb.REPLAY_CACHE + 2):
    d = tmp_path / f"0000000{i}--x--{i}"
    d.mkdir(exist_ok=True)
    write_rlog(d / "rlog.zst", seconds=1.0)
    assert replay_panel.json(f"/api/replay/data?id=0000000{i}--x--{i}", token=token)[0] == 200
  assert len(replay_panel.backend._replay_cache) == hexaweb.REPLAY_CACHE


def test_replay_is_gzipped_when_the_phone_accepts_it(replay_panel):
  import gzip
  token = replay_panel.pair()
  url = replay_panel.url + "/api/replay/data?id=00000007--abc--9"
  plain = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
  with urllib.request.urlopen(plain, timeout=5) as r:
    assert r.headers.get("Content-Encoding") is None
    body = r.read()
  assert len(body) >= hexaweb.GZIP_MIN_BYTES
  zipped = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "Accept-Encoding": "gzip, br"})
  with urllib.request.urlopen(zipped, timeout=5) as r:
    assert r.headers.get("Content-Encoding") == "gzip" and r.headers.get("Vary") == "Accept-Encoding"
    assert int(r.headers["Content-Length"]) < len(body) // 3
    assert gzip.decompress(r.read()) == body
  # small answers stay plain
  small = urllib.request.Request(replay_panel.url + "/api/replay/list", headers={"Authorization": "Bearer " + token, "Accept-Encoding": "gzip"})
  with urllib.request.urlopen(small, timeout=5) as r:
    assert r.headers.get("Content-Encoding") is None and json.loads(r.read())["segments"]


def test_options_missing_from_this_build_are_neither_shown_nor_writable(panel):
  def check_key(key):
    if key == "LaneOffsetEnabled":
      raise KeyError(key)
    return key
  panel.store.check_key = check_key
  token = panel.pair()
  status, s = panel.json("/api/state", token=token)
  keys = {o["key"] for g in s["groups"] for o in g["options"]}
  assert status == 200 and "LaneOffsetEnabled" not in keys and "HexaWebEnabled" in keys
  assert panel.json("/api/option", {"key": "LaneOffsetEnabled", "value": True}, token=token)[0] == 404
  assert panel.store.get("LaneOffsetEnabled") is None


def test_serve_survives_a_busy_port(monkeypatch):
  # a dead process disengages openpilot, so a failed server must keep trying
  calls = []

  def fail(backend):
    calls.append(backend)
    if len(calls) == 3:
      raise KeyboardInterrupt  # how the manager stops a process
    raise OSError("port busy")

  monkeypatch.setattr(hexaweb, "make_server", fail)
  with pytest.raises(KeyboardInterrupt):
    hexaweb.serve("backend", retry=0)
  assert len(calls) == 3


def updater(panel, **values):
  panel.store.d.update({"UpdaterAvailableBranches": "h6gt,h6gt-lat-04,master,release3", "UpdaterTargetBranch": "h6gt",
                        "UpdaterCurrentDescription": "0.11.3 / h6gt / 2fd5622 / Nov 01", "UpdaterState": "idle", **values})
  panel.signals = []
  panel.backend.signal_updater = panel.signals.append
  return panel.pair()


def test_update_state_offers_only_the_cars_branch(panel):
  token = updater(panel, UpdaterNewDescription="0.11.3 / h6gt-lat-04 / 3653abc / Nov 01", UpdateAvailable=True)
  status, u = panel.json("/api/update", token=token)
  assert status == 200 and u["branches"] == ["h6gt"] and u["allowed"] is True
  assert u["current"]["commit"] == "2fd5622" and u["new"]["branch"] == "h6gt-lat-04" and u["ready"] is True


def test_update_steps_with_the_car_off(panel):
  token = updater(panel, UpdateAvailable=True)
  assert panel.json("/api/update", {"action": "check"}, token=token)[0] == 200
  assert panel.json("/api/update", {"action": "download"}, token=token)[0] == 200
  assert panel.signals == ["SIGUSR1", "SIGHUP"]
  panel.store.d["UpdaterTargetBranch"] = "h6gt-next"  # a comma on another branch can come back
  assert panel.json("/api/update", token=token)[1]["branches"] == ["h6gt-next", "h6gt"]
  assert panel.json("/api/update", {"action": "branch", "branch": "h6gt"}, token=token)[0] == 200
  assert panel.store.get("UpdaterTargetBranch") == "h6gt" and panel.signals[-1] == "SIGUSR1"
  assert panel.json("/api/update", {"action": "install"}, token=token)[0] == 200
  assert panel.store.get("DoReboot") is True
  assert panel.store.get(phone.AUDIT_PARAM)[-1]["event"] == "update"


def test_update_refused_with_the_car_on(panel):
  token = updater(panel, UpdateAvailable=True)
  for vs in (VehicleState(onroad=True), VehicleState(onroad=True, parked=True, standstill=True)):
    panel.vs = vs
    for body in ({"action": "check"}, {"action": "download"}, {"action": "install"}, {"action": "branch", "branch": "h6gt"}):
      assert panel.json("/api/update", body, token=token)[0] == 409
  assert panel.signals == [] and panel.store.get("DoReboot") is None
  assert panel.json("/api/update", token=token)[1]["allowed"] is False


def test_update_needs_pairing(panel):
  updater(panel, UpdateAvailable=True)
  assert panel.json("/api/update")[0] == 401
  assert panel.json("/api/update", {"action": "install"})[0] == 401
  assert panel.store.get("DoReboot") is None


def test_update_only_to_a_listed_branch_and_nothing_else(panel):
  token = updater(panel, UpdateAvailable=False)
  assert panel.json("/api/update", {"action": "branch", "branch": "master"}, token=token)[0] == 400
  assert panel.json("/api/update", {"action": "branch", "branch": "h6gt-lat-04"}, token=token)[0] == 400
  assert panel.json("/api/update", {"action": "branch", "branch": "evil; reboot"}, token=token)[0] == 400
  assert panel.json("/api/update", {"action": "uninstall"}, token=token)[0] == 400
  assert panel.json("/api/update", {"action": "install"}, token=token)[0] == 409
  assert panel.store.get("UpdaterTargetBranch") == "h6gt" and panel.store.get("DoReboot") is None


def trip_logs(panel, tmp_path):
  from openpilot.selfdrive.hexa.tests.test_timeline import write_rlog
  for n in (0, 1):
    d = tmp_path / f"0000000f--8784034901--{n}"
    d.mkdir()
    write_rlog(d / "qlog.zst", bookmark_at=3.0)
  panel.backend.log_root = str(tmp_path)


def test_trips_are_listed_and_read_by_minute(panel, tmp_path):
  trip_logs(panel, tmp_path)
  token = panel.pair()
  status, t = panel.json("/api/trips", token=token)
  assert status == 200 and t["trips"][0]["id"] == "0000000f--8784034901" and t["trips"][0]["segments"] == [0, 1]
  status, d = panel.json("/api/trip/segment?route=0000000f--8784034901&n=1", token=token)
  assert status == 200 and d["points"] and d["summary"]["km"] > 0 and d["summary"]["marks"] == 1


def test_each_trip_gets_its_summary_once_with_the_car_off(panel, tmp_path):
  trip_logs(panel, tmp_path)
  jobs = []
  panel.backend._spawn = jobs.append  # run the background job by hand
  token = panel.pair()
  t = panel.json("/api/trips", token=token)[1]
  assert t["pending"] and t["trips"][0]["summary"] is None and len(jobs) == 1
  assert len(panel.json("/api/trips", token=token)[1]) and len(jobs) == 1  # one job at a time
  jobs.pop()()
  t = panel.json("/api/trips", token=token)[1]
  s = t["trips"][0]["summary"]
  assert not t["pending"] and s["segments"] == 2 and abs(s["op_s"] - 15.) < .2 and abs(s["you_s"] - 1.) < .2 and not jobs
  # a drive that grew is summarized again
  d = tmp_path / "0000000f--8784034901--2"
  d.mkdir()
  from openpilot.selfdrive.hexa.tests.test_timeline import write_rlog
  write_rlog(d / "qlog.zst")
  panel.backend._trip_index = (-1e9, [])
  panel.now += 100
  assert panel.json("/api/trips", token=token)[1]["trips"][0]["summary"] is None and len(jobs) == 1


def test_a_summary_from_before_the_versions_is_read_again(panel, tmp_path):
  trip_logs(panel, tmp_path)
  panel.store.put(hexaweb.SUMMARY_PARAM, {"0000000f--8784034901": {"segments": 2, "km": 1.}})  # no "sv": made before
  jobs = []
  panel.backend._spawn = jobs.append
  token = panel.pair()
  t = panel.json("/api/trips", token=token)[1]
  assert t["trips"][0]["summary"] is None and len(jobs) == 1
  jobs.pop()()
  s = panel.json("/api/trips", token=token)[1]["trips"][0]["summary"]
  assert s["sv"] == hexaweb.SUMMARY_VERSION and "alerts" in s and "commit" in s


def test_summaries_stop_when_the_car_turns_on(panel, tmp_path):
  trip_logs(panel, tmp_path)
  jobs, read = [], []
  panel.backend._spawn = jobs.append
  panel.backend._summarize = lambda files, stop: read.append(files) or {"segments": len(files)}
  token = panel.pair()
  panel.json("/api/trips", token=token)
  panel.vs = VehicleState(onroad=True)
  jobs.pop()()
  assert read == [] and panel.backend._summary_busy is False


def test_trips_only_with_the_car_off(panel, tmp_path):
  trip_logs(panel, tmp_path)
  token = panel.pair()
  panel.vs = VehicleState(onroad=True, parked=True, standstill=True)
  assert panel.json("/api/trips", token=token)[0] == 409
  assert panel.json("/api/trip/segment?route=0000000f--8784034901&n=0", token=token)[0] == 409


def test_trip_takes_only_ids_from_the_list(panel, tmp_path):
  trip_logs(panel, tmp_path)
  token = panel.pair()
  for q in ("route=../../etc&n=0", "route=0000000f--8784034901&n=9", "route=0000000f--8784034901&n=../0"):
    assert panel.json("/api/trip/segment?" + q, token=token)[0] == 404
  assert panel.json("/api/trips")[0] == 401


def test_trip_mark_shows_the_reason(panel, tmp_path):
  trip_logs(panel, tmp_path)
  token = panel.pair()
  mono = next(e for e in panel.backend.trip_data("0000000f--8784034901", "0")[1]["events"] if e["kind"] == "mark")
  assert mono["text"] == "bandeira" and "mono_ns" not in mono
  panel.backend._trip_cache.clear()
  segment = hexaweb.timeline.trip_segment(hexaweb.timeline.read_rlog(tmp_path / "0000000f--8784034901--0" / "qlog.zst"))
  ns = next(e for e in segment["events"] if e["kind"] == "mark")["mono_ns"]
  panel.store.put(marks.MARKS_PARAM, [{"id": "a", "mono_ns": ns, "label": "curva difícil", "note": "rotatória"}])
  ev = panel.json("/api/trip/segment?route=0000000f--8784034901&n=0", token=token)[1]["events"]
  assert next(e for e in ev if e["kind"] == "mark")["text"] == "curva difícil: rotatória"


def test_mark_text_can_be_written_changed_and_cleared(panel):
  token = panel.pair()
  mark = panel.json("/api/mark", {"reason": "outro"}, token=token)[1]["mark"]
  body = {"id": mark["id"], "reason": "outro", "note": "moto colou na lateral", "replace_note": True}
  assert panel.json("/api/mark/classify", body, token=token)[1]["mark"]["note"] == "moto colou na lateral"
  body = {"id": mark["id"], "reason": "curva", "note": "buraco na curva", "replace_note": True}
  edited = panel.json("/api/mark/classify", body, token=token)[1]["mark"]
  assert edited["note"] == "buraco na curva" and edited["label"] == "curva difícil"
  body = {"id": mark["id"], "reason": "curva", "note": "", "replace_note": True}
  assert panel.json("/api/mark/classify", body, token=token)[1]["mark"]["note"] == ""


def test_classifying_without_text_keeps_the_note(panel):
  token = panel.pair()
  mark = panel.json("/api/mark", {"reason": "outro", "note": "antes"}, token=token)[1]["mark"]
  body = {"id": mark["id"], "reason": "curva", "note": ""}
  assert panel.json("/api/mark/classify", body, token=token)[1]["mark"]["note"] == "antes"
  # only a real true replaces it
  body = {"id": mark["id"], "reason": "curva", "note": "", "replace_note": "sim"}
  assert panel.json("/api/mark/classify", body, token=token)[1]["mark"]["note"] == "antes"


def test_live_needs_pairing_and_shows_what_the_backend_gives(panel):
  panel.backend._live_state = lambda: {"driving": True, "v": 42}
  token = panel.pair()
  assert panel.json("/api/live")[0] == 401
  assert panel.json("/api/live", token=token) == (200, {"driving": True, "v": 42, "last_off": None})


def test_live_failure_only_empties_the_page(panel):
  def fail():
    raise RuntimeError("no data")
  panel.backend._live_state = fail
  assert panel.json("/api/live", token=panel.pair()) == (200, {"last_off": None})


def test_reboot_only_with_the_car_off(panel):
  token = updater(panel)
  panel.vs = VehicleState(onroad=True, parked=True, standstill=True)
  assert panel.json("/api/update", {"action": "reboot"}, token=token)[0] == 409
  assert panel.store.get("DoReboot") is None
  panel.vs = VehicleState(onroad=False)
  assert panel.json("/api/update", {"action": "reboot"}, token=token)[0] == 200
  assert panel.store.get("DoReboot") is True


def test_mark_keeps_the_moment_of_the_tap(panel):
  token = panel.pair()
  mark = panel.json("/api/mark", {"reason": "balancou", "note": "buraco", "delay_s": 12.34}, token=token)[1]["mark"]
  assert mark["t"] == int(panel.now - 12.3) and mark["delay_s"] == 12.3 and mark["note"] == "buraco"
  assert panel.logged[-1]["delay_s"] == 12.3


def test_mark_keeps_when_it_happened_around_the_tap(panel):
  # owner, 04/11: the instant, the last seconds, or from now on; the bookmark stays at the tap
  token = panel.pair()
  for i, (win, want) in enumerate(((None, None), ([0, 0], None), ([30, 0], [30, 0]), ([0, 30], [0, 30]), ([999, -5], [60, 0]), ("x", None))):
    panel.backend.mono = lambda n=panel.now + i * 10: n
    mark = panel.json("/api/mark", {"reason": "balancou", "window": win}, token=token)[1]["mark"]
    assert mark.get("win") == want and panel.logged[-1].get("win") == want


def test_mark_delay_from_outside_is_checked(panel):
  token = panel.pair()
  for i, bad in enumerate((-1, 1e9, "12", True, None)):
    panel.backend.mono = lambda n=panel.now + i * 10: n
    mark = panel.json("/api/mark", {"reason": "balancou", "delay_s": bad}, token=token)[1]["mark"]
    assert mark["delay_s"] == 0.


def test_radar_list_shows_what_the_comma_has(panel, tmp_path):
  token = panel.pair()
  panel.backend.radar_path = tmp_path / "r.csv"
  status, d = panel.json("/api/radars", token=token)
  assert status == 200 and d["count"] > 1000 and d["date"] is None and d["allowed"] is True
  assert panel.json("/api/radars")[0] == 401


def test_radar_download_only_with_the_car_off(panel, tmp_path, monkeypatch):
  token = panel.pair()
  panel.backend.radar_path = tmp_path / "r.csv"
  done = threading.Event()
  monkeypatch.setattr(hexaweb.radars, "download", lambda p: (p.write_text("# x\\n1,2,60,nan\\n"), done.set()))
  panel.vs = VehicleState(onroad=True, parked=True, standstill=True)
  assert panel.json("/api/radars", {}, token=token)[0] == 409
  assert not done.is_set()
  panel.vs = VehicleState(onroad=False)
  assert panel.json("/api/radars", {}, token=token)[0] == 200
  assert done.wait(2)


def test_radar_download_failure_is_shown(panel, tmp_path, monkeypatch):
  token = panel.pair()
  panel.backend.radar_path = tmp_path / "r.csv"

  def fail(p):
    raise ValueError("bad")
  monkeypatch.setattr(hexaweb.radars, "download", fail)
  assert panel.json("/api/radars", {}, token=token)[0] == 200
  for _ in range(100):
    d = panel.json("/api/radars", token=token)[1]
    if not d["busy"]:
      break
  assert "ValueError" in d["error"]


def radar_panel(panel, tmp_path, fix=True, speed=10.):
  from openpilot.selfdrive.hexa import radars as r
  row = r.load(r.BUNDLED)[0]
  panel.lat, panel.lon = float(row[0]), float(row[1])
  panel.backend.radar_path = tmp_path / "nao-existe.csv"
  panel.fix = (panel.lat - .002, panel.lon, 0., speed) if fix else None
  panel.backend._gps_fix = lambda: panel.fix
  return panel.pair(), r


def add_point(panel, token, kind="radar", **body):
  pin = panel.json("/api/radars/pin", {}, token=token)[1]["pin"]
  return panel.json("/api/radars/add", {"kind": kind, "pin": pin, **body}, token=token)


def test_radar_routes_need_pairing(panel, tmp_path):
  token, r = radar_panel(panel, tmp_path)
  assert panel.json("/api/radars/near")[0] == 401
  for path in ("/api/radars/pin", "/api/radars/add", "/api/radars/edit", "/api/radars/delete"):
    assert panel.json(path, {})[0] == 401


def test_no_position_reaches_the_phone(panel, tmp_path):
  token, r = radar_panel(panel, tmp_path)
  assert add_point(panel, token, limit=50)[0] == 200
  status, d = panel.json("/api/radars/near", token=token)
  assert status == 200 and d["gps"] is True and d["near"] and d["mine"]
  assert re.search(r"-?\d+\.\d{3}", json.dumps(d)) is None


def test_confirm_limit_and_side_of_a_camera(panel, tmp_path):
  token, r = radar_panel(panel, tmp_path)
  p = panel.json("/api/radars/near", token=token)[1]["near"][0]
  body = {"key": p["key"], "limit": 70, "side": "mesmo", "confirmed": True}
  assert panel.json("/api/radars/edit", body, token=token)[0] == 200
  q = next(x for x in panel.json("/api/radars/near", token=token)[1]["near"] if x["limit"] == 70)
  assert q["side"] == "mesmo" and q["confirmed"] is True
  assert panel.store.get(r.EDITS_PARAM)[0]["direction"] == 0.


def test_side_needs_a_moving_car(panel, tmp_path):
  token, r = radar_panel(panel, tmp_path, speed=.5)
  p = panel.json("/api/radars/near", token=token)[1]["near"][0]
  assert panel.json("/api/radars/edit", {"key": p["key"], "side": "mesmo"}, token=token)[0] == 409
  assert panel.json("/api/radars/edit", {"key": p["key"], "limit": 60, "confirmed": True}, token=token)[0] == 200


def test_remove_a_camera_and_undo(panel, tmp_path):
  token, r = radar_panel(panel, tmp_path)
  p = panel.json("/api/radars/near", token=token)[1]["near"][0]
  assert panel.json("/api/radars/edit", {"key": p["key"], "removed": True}, token=token)[0] == 200
  d = panel.json("/api/radars/near", token=token)[1]
  assert p["key"] not in [x["key"] for x in d["near"]] and d["mine"][0]["removed"] is True
  assert panel.json("/api/radars/delete", {"key": p["key"]}, token=token)[0] == 200
  assert p["key"] in [x["key"] for x in panel.json("/api/radars/near", token=token)[1]["near"]]


def test_edit_checks_what_comes_from_outside(panel, tmp_path):
  token, r = radar_panel(panel, tmp_path)
  p = panel.json("/api/radars/near", token=token)[1]["near"][0]
  assert panel.json("/api/radars/edit", {"key": "0,0"}, token=token)[0] == 404
  for bad in (5, 999, "60", True):
    assert panel.json("/api/radars/edit", {"key": p["key"], "limit": bad}, token=token)[0] == 400


def test_add_keeps_the_place_of_the_tap(panel, tmp_path):
  token, r = radar_panel(panel, tmp_path)
  pin = panel.json("/api/radars/pin", {}, token=token)[1]["pin"]
  tap = panel.fix
  panel.fix = (panel.lat - .02, panel.lon, 0., 10.)  # 2 km later
  body = {"kind": "blitz", "limit": 40, "pin": pin, "lat": 1., "lon": 2.}
  assert panel.json("/api/radars/add", body, token=token)[0] == 200
  e = panel.store.get(r.EDITS_PARAM)[0]
  assert (e["lat"], e["lon"], e["direction"]) == (tap[0], tap[1], 0.) and e["kind"] == "blitz"
  assert e["expires"] == panel.now + r.BLITZ_HOURS * 3600
  assert panel.json("/api/radars/add", body, token=token)[0] == 409  # a pin only saves once


def test_a_stopped_car_saves_no_direction(panel, tmp_path):
  token, r = radar_panel(panel, tmp_path, speed=.5)
  assert add_point(panel, token)[0] == 200
  assert panel.store.get(r.EDITS_PARAM)[0]["direction"] is None


def test_the_owner_picks_the_side_of_a_new_point(panel, tmp_path):
  token, r = radar_panel(panel, tmp_path)
  assert add_point(panel, token, limit=60, side="contrario")[0] == 200
  assert panel.store.get(r.EDITS_PARAM)[0]["direction"] == 180.
  panel.fix = (panel.fix[0] - .01, panel.fix[1], 0., panel.fix[3])
  assert add_point(panel, token, "lombada", side="dois")[0] == 200
  e = panel.store.get(r.EDITS_PARAM)[-1]
  assert (e["kind"], e["direction"], e["both"], e["expires"]) == ("lombada", None, True, None)
  near = panel.json("/api/radars/near", token=token)[1]
  assert near["moving"] is True and next(p for p in near["near"] if p["kind"] == "lombada")["side"] == "dois"
  assert next(m for m in near["mine"] if m["kind"] == "lombada")["side"] == "dois"


def test_a_stopped_car_can_only_say_both_ways_and_keeps_the_tap(panel, tmp_path):
  token, r = radar_panel(panel, tmp_path, speed=.5)
  got = panel.json("/api/radars/pin", {}, token=token)[1]
  assert got["moving"] is False
  body = {"kind": "lombada", "pin": got["pin"]}
  assert panel.json("/api/radars/add", {**body, "side": "mesmo"}, token=token)[0] == 409
  assert panel.json("/api/radars/add", {**body, "side": "dois"}, token=token)[0] == 200


def test_edit_a_point_to_both_ways_and_back(panel, tmp_path):
  token, r = radar_panel(panel, tmp_path)
  p = panel.json("/api/radars/near", token=token)[1]["near"][0]
  assert panel.json("/api/radars/edit", {"key": p["key"], "limit": 60, "side": "dois", "confirmed": True}, token=token)[0] == 200
  e = panel.store.get(r.EDITS_PARAM)[0]
  assert (e["direction"], e["both"]) == (None, True)
  panel.backend._radar_rows = ((), None)
  q = panel.json("/api/radars/near", token=token)[1]["near"][0]
  assert q["side"] == "dois"
  assert panel.json("/api/radars/edit", {"key": p["key"], "limit": 60, "confirmed": True}, token=token)[0] == 200
  assert panel.store.get(r.EDITS_PARAM)[0]["both"] is True  # no side sent: nothing changes
  assert panel.json("/api/radars/edit", {"key": p["key"], "limit": 60, "side": "mesmo", "confirmed": True}, token=token)[0] == 200
  e = panel.store.get(r.EDITS_PARAM)[0]
  assert e["direction"] == 0. and e["both"] is False


def test_a_bump_keeps_the_speed_it_was_passed_at_and_the_one_asked(panel, tmp_path):
  token, r = radar_panel(panel, tmp_path)
  got = panel.json("/api/radars/pin", {}, token=token)[1]
  assert got["speed"] == round(panel.fix[3] * 3.6)
  assert panel.json("/api/radars/add", {"kind": "lombada", "pin": got["pin"], "limit": 4, "side": "dois"}, token=token)[0] == 400
  assert panel.json("/api/radars/add", {"kind": "lombada", "pin": got["pin"], "limit": 15, "side": "dois"}, token=token)[0] == 200
  e = panel.store.get(r.EDITS_PARAM)[0]
  assert (e["limit"], e["passed"]) == (15, round(panel.fix[3] * 3.6))
  panel.backend._radar_rows = ((), None)
  near = panel.json("/api/radars/near", token=token)[1]
  p = next(x for x in near["near"] if x["kind"] == "lombada")
  assert p["limit"] == 15 and near["mine"][0]["passed"] == e["passed"]
  assert panel.json("/api/radars/edit", {"key": p["key"], "limit": 20, "confirmed": True}, token=token)[0] == 200
  assert panel.json("/api/radars/edit", {"key": p["key"], "limit": 4, "confirmed": True}, token=token)[0] == 400
  assert panel.store.get(r.EDITS_PARAM)[0]["passed"] == e["passed"]  # an edit keeps what was measured


def test_an_old_tap_expires(panel, tmp_path):
  token, r = radar_panel(panel, tmp_path)
  pin = panel.json("/api/radars/pin", {}, token=token)[1]["pin"]
  panel.now += r.PIN_SECONDS + 1
  assert panel.json("/api/radars/add", {"kind": "radar", "pin": pin}, token=token)[0] == 409


def test_add_refuses_a_point_already_there(panel, tmp_path):
  token, r = radar_panel(panel, tmp_path)
  assert add_point(panel, token)[0] == 200
  assert add_point(panel, token)[0] == 409


def test_add_needs_gps_and_a_known_kind(panel, tmp_path):
  token, r = radar_panel(panel, tmp_path, fix=False)
  assert panel.json("/api/radars/pin", {}, token=token)[0] == 409
  assert panel.json("/api/radars/add", {"kind": "outro"}, token=token)[0] == 400
  assert panel.json("/api/radars/near", token=token)[1]["gps"] is False


def test_delete_own_point(panel, tmp_path):
  token, r = radar_panel(panel, tmp_path)
  add_point(panel, token)
  k = panel.store.get(r.EDITS_PARAM)[0]["key"]
  assert panel.json("/api/radars/delete", {"key": "nada"}, token=token)[0] == 404
  assert panel.json("/api/radars/delete", {"key": k}, token=token)[0] == 200
  assert panel.store.get(r.EDITS_PARAM) == []


def test_openpilot_dropping_out_is_noted(panel):
  token = panel.pair()
  panel.backend.on_drive_state(False, [])
  assert panel.json("/api/live", token=token)[1]["last_off"] is None
  panel.backend.on_drive_state(True, [])
  panel.now = 2000.
  flags = {"userDisable": True, "noEntry": False, "softDisable": False, "immediateDisable": False}
  panel.backend.on_drive_state(False, [SimpleNamespace(name="pcmDisable", **flags)])
  assert panel.json("/api/live", token=token)[1]["last_off"] == {"t": 2000, "why": ["o ACC desligou"]}
  assert panel.json("/api/health", token=token)[1]["last_off"] == {"t": 2000, "why": ["o ACC desligou"]}


def test_health_needs_pairing_and_survives_a_failure(panel):
  panel.backend._health_state = lambda: {"down": ["loggerd"]}
  token = panel.pair()
  assert panel.json("/api/health")[0] == 401
  assert panel.json("/api/health", token=token)[1]["down"] == ["loggerd"]

  def fail():
    raise RuntimeError("no data")
  panel.backend._health_state = fail
  assert panel.json("/api/health", token=token) == (200, {"errors": [], "last_off": None, "reports": {"sent": 0, "receipt": ""}})


def test_phones_show_no_secret_and_mark_this_one(panel):
  mine, other = panel.pair("meu"), panel.pair("outro")
  assert panel.json("/api/phones")[0] == 401
  status, d = panel.json("/api/phones", token=mine)
  assert status == 200 and [(p["name"], p["you"]) for p in d["phones"]] == [("meu", True), ("outro", False)]
  assert "hash" not in json.dumps(d) and other not in json.dumps(d)


def test_the_phone_list_keeps_the_ones_in_use_and_says_who_left(panel):
  # owner, 04/11: with 5 paired the phone in use fell off the list. 10, the last use (at most once an hour), and the one
  # unused the longest leaves, named in the pairing's answer
  mine = panel.pair("meu")
  others = [panel.pair(f"outro {i}") for i in range(phone.MAX_TOKENS - 1)]
  panel.now += 2 * phone.SEEN_EVERY_S
  assert panel.json("/api/state", token=mine)[0] == 200  # in use now
  d = panel.json("/api/phones", token=mine)[1]
  assert d["max"] == phone.MAX_TOKENS and next(p for p in d["phones"] if p["you"])["seen"] is not None
  status, body = panel.json("/api/pair", {"pin": panel.show_pin(), "name": "novo"})
  assert status == 200 and body["removed"] == ["outro 0"]
  assert panel.json("/api/state", token=mine)[0] == 200 and panel.json("/api/state", token=others[0])[0] == 401
  assert panel.store.get(phone.AUDIT_PARAM)[-1] == {**panel.store.get(phone.AUDIT_PARAM)[-1], "event": "paired", "new": "outro 0"}


def test_disconnect_another_phone(panel):
  mine, other = panel.pair("meu"), panel.pair("outro")
  phones = panel.json("/api/phones", token=mine)[1]["phones"]
  assert panel.json("/api/phones/revoke", {"id": phones[1]["id"]}) [0] == 401
  assert panel.json("/api/phones/revoke", {"id": "nada"}, token=mine)[0] == 404
  assert panel.json("/api/phones/revoke", {"id": phones[0]["id"]}, token=mine)[0] == 400  # this one has its own button
  assert panel.json("/api/phones/revoke", {"id": phones[1]["id"]}, token=mine)[0] == 200
  assert panel.json("/api/state", token=other)[0] == 401
  assert panel.json("/api/state", token=mine)[0] == 200
  assert panel.store.get(phone.AUDIT_PARAM)[-1]["event"] == "phone_revoked"


def test_radar_points_go_out_and_come_back(panel, tmp_path):
  token, r = radar_panel(panel, tmp_path)
  add_point(panel, token, limit=50)
  assert panel.json("/api/radars/export")[0] == 401
  saved = panel.json("/api/radars/export", token=token)[1]["edits"]
  assert len(saved) == 1 and saved[0]["lat"] == panel.lat - .002
  panel.store.put(r.EDITS_PARAM, [])
  status, d = panel.json("/api/radars/import", {"edits": saved}, token=token)
  assert status == 200 and "restaurados: 1" in d["message"]
  back = panel.store.get(r.EDITS_PARAM)
  assert [(e["key"], e["lat"], e["limit"], e["kind"]) for e in back] == [(e["key"], e["lat"], e["limit"], e["kind"]) for e in saved]


def test_import_refuses_junk_and_a_running_car(panel, tmp_path):
  token, r = radar_panel(panel, tmp_path)
  assert panel.json("/api/radars/import", {"edits": []})[0] == 401
  assert panel.json("/api/radars/import", {"edits": [{"lat": 999}]}, token=token)[0] == 400
  assert panel.json("/api/radars/import", {"edits": "nada"}, token=token)[0] == 400
  panel.vs = VehicleState(onroad=True)
  assert panel.json("/api/radars/import", {"edits": [{"lat": 1., "lon": 2., "kind": "radar"}]}, token=token)[0] == 409


def test_a_large_copy_fits_but_only_there(panel, tmp_path):
  token, r = radar_panel(panel, tmp_path)
  edits = [{"lat": -20. - i / 1000, "lon": -40., "kind": "radar", "limit": 60, "direction": 10., "who": "x" * 20, "t": 1000} for i in range(500)]
  assert panel.json("/api/radars/import", {"edits": edits}, token=token)[0] == 200
  assert len(panel.store.get(r.EDITS_PARAM)) == 500
  assert panel.json("/api/option", {"key": "AutoResume", "pad": "x" * 8000}, token=token)[0] == 413


def test_icons_are_served_and_named_in_the_manifest(panel):
  manifest = json.loads(panel.request("/manifest.webmanifest")[2])
  for icon in manifest["icons"]:
    status, headers, data = panel.request(icon["src"])
    assert status == 200 and headers["Content-Type"] == "image/png" and data[:4] == b"\x89PNG"
  assert panel.request("/icon-180.png")[0] == 200
  assert 'rel="apple-touch-icon" href="/icon-180.png"' in panel.request("/")[2].decode()


def test_every_element_the_page_asks_for_exists(panel):
  page = panel.request("/")[2].decode()
  assert set(re.findall(r'\$\("([A-Za-z]+)"\)', page)) <= set(re.findall(r'id="([A-Za-z]+)"', page))



def test_options_copy_preview_and_restore(panel):
  token = panel.pair()
  copy = panel.json("/api/options/export", token=token)[1]
  assert copy["options"]["LaneOffsetEnabled"] is False and "HexaWebEnabled" not in copy["options"]  # only what the phone can change
  wanted = {**copy["options"], "LaneOffsetEnabled": True, "AutoLaneChangeDelay": 2, "NotAnOption": True}
  status, preview = panel.json("/api/options/import", {"options": wanted}, token=token)
  assert status == 200 and {c["key"] for c in preview["changes"]} == {"LaneOffsetEnabled", "AutoLaneChangeDelay"}
  assert preview["skipped"] == ["NotAnOption"] and panel.store.get("LaneOffsetEnabled") is None  # a preview writes nothing
  panel.vs = VehicleState(onroad=True, parked=False, standstill=False, engaged=True)
  status, body = panel.json("/api/options/import", {"options": wanted, "apply": True}, token=token)
  assert status == 409 and panel.store.get("LaneOffsetEnabled") is None  # moving: all or nothing
  panel.vs = VehicleState(onroad=False)
  status, body = panel.json("/api/options/import", {"options": wanted, "apply": True}, token=token)
  assert status == 200 and panel.store.get("LaneOffsetEnabled") is True and panel.store.get("AutoLaneChangeDelay") == 2
  assert "partida" in body["message"]  # the delay needs a new start
  assert panel.json("/api/options/import", {"options": "nonsense", "apply": True}, token=token)[1]["changes"] == []


def test_profiles_are_saved_from_now_and_applied_like_a_copy(panel):
  token = panel.pair()
  assert panel.json("/api/profiles/save", {"name": "  Estrada  "}, token=token)[0] == 200
  profile = panel.json("/api/state", token=token)[1]["profiles"]["Estrada"]
  assert profile["LaneOffsetEnabled"] is False
  panel.json("/api/option", {"key": "LaneOffsetEnabled", "value": True}, token=token)
  changes = panel.json("/api/options/import", {"options": profile}, token=token)[1]["changes"]
  assert changes == [{"key": "LaneOffsetEnabled", "old": True, "new": False}]
  assert panel.json("/api/profiles/save", {"name": ""}, token=token)[0] == 400
  for n in range(5):
    panel.json("/api/profiles/save", {"name": f"p{n}"}, token=token)
  assert panel.json("/api/profiles/save", {"name": "sétimo"}, token=token)[0] == 409
  assert panel.json("/api/profiles/delete", {"name": "Estrada"}, token=token)[0] == 200
  assert panel.json("/api/profiles/delete", {"name": "Estrada"}, token=token)[0] == 404
  assert panel.store.get(phone.AUDIT_PARAM)[-1]["event"] == "profile_removed"


def test_a_copy_brings_its_profiles_back(panel):
  token = panel.pair()
  panel.json("/api/profiles/save", {"name": "Estrada"}, token=token)
  copy = panel.json("/api/options/export", token=token)[1]
  panel.json("/api/profiles/delete", {"name": "Estrada"}, token=token)
  panel.json("/api/profiles/save", {"name": "Cidade"}, token=token)
  sent = {**copy["profiles"], "Velho": {"NaoExisteMais": 1}, "": {"LaneOffsetEnabled": True}, "Ruim": "x"}
  status, body = panel.json("/api/options/import", {"options": copy["options"], "profiles": sent}, token=token)
  assert status == 200 and body["changes"] == [] and body["profiles"] == [{"name": "Estrada", "new": True}]
  assert body["profiles_skipped"] == ["Velho", "Ruim"]
  assert set(panel.json("/api/state", token=token)[1]["profiles"]) == {"Cidade"}  # showing what changes writes nothing

  panel.vs = VehicleState(onroad=True, parked=False, standstill=False, engaged=True)
  assert panel.json("/api/options/import", {"options": copy["options"], "profiles": sent, "apply": True}, token=token)[0] == 409
  assert set(panel.store.get(hexaweb.PROFILES_PARAM)) == {"Cidade"}

  panel.vs = VehicleState(onroad=False)
  status, body = panel.json("/api/options/import", {"options": copy["options"], "profiles": sent, "apply": True}, token=token)
  assert status == 200 and body["message"] == "Perfis restaurados."
  kept = panel.store.get(hexaweb.PROFILES_PARAM)
  assert set(kept) == {"Cidade", "Estrada"} and kept["Estrada"] == copy["profiles"]["Estrada"]  # the others stay
  assert panel.store.get(phone.AUDIT_PARAM)[-1] | {"t": 0} == {"event": "profiles_restored", "who": "celular de teste", "new": "Estrada", "t": 0}
  assert panel.json("/api/options/import", {"options": copy["options"], "profiles": sent, "apply": True}, token=token)[1]["message"] == "Nada a mudar."
  assert "profiles" not in panel.json("/api/options/import", {"options": copy["options"]}, token=token)[1]  # a profile sends only options


def test_a_copy_never_goes_past_six_profiles(panel):
  token = panel.pair()
  for n in range(hexaweb.MAX_PROFILES):
    panel.json("/api/profiles/save", {"name": f"p{n}"}, token=token)
  kept = panel.store.get(hexaweb.PROFILES_PARAM)
  sent = {"p0": {**kept["p0"], "LaneOffsetEnabled": not kept["p0"]["LaneOffsetEnabled"]}, "Sétimo": kept["p1"]}
  status, body = panel.json("/api/options/import", {"options": {}, "profiles": sent, "apply": True}, token=token)
  assert status == 200 and body["profiles"] == [{"name": "p0", "new": False}]
  kept = panel.store.get(hexaweb.PROFILES_PARAM)
  assert len(kept) == hexaweb.MAX_PROFILES and "Sétimo" not in kept and kept["p0"] == sent["p0"]


def test_mark_clip_by_ticket_with_the_car_off(panel, tmp_path):
  import os
  end0 = 50_000.
  for n in (0, 1):
    d = tmp_path / f"00000020--abcdef0123--{n}"
    d.mkdir()
    (d / "qlog.zst").write_bytes(b"q")
    (d / "qcamera.ts").write_bytes(f"video{n}".encode())
    for f in ("qlog.zst", "qcamera.ts"):
      os.utime(d / f, (end0 + 60 * n, end0 + 60 * n))
  panel.backend.log_root = str(tmp_path)
  panel.store.put(marks.MARKS_PARAM, [{"id": "m1", "t": end0 + 5, "reason": "curva", "label": "curva difícil"}])  # 5 s into minute 1
  token = panel.pair()
  status, c = panel.json("/api/mark/clip", {"id": "m1"}, token=token)
  assert status == 200 and c["ticket"]
  status, _, playlist = panel.request(c["playlist"])  # no token: the ticket is the key
  text = playlist.decode()
  assert status == 200 and text.count("#EXTINF") == 2 and "TIME-OFFSET=35.0" in text  # minute 0 too: the clip starts 30 s before
  assert (c["start"], c["mark_at"], c["lead"], c["minutes"], c["short"]) == (35., 65., 30., 2, False)
  status, headers, video = panel.request(c["video"])  # the file to download: the same minutes, in one file
  assert status == 200 and video == b"video0video1" and headers["Content-Type"] == "video/mp2t" and "attachment" in headers["Content-Disposition"]
  assert panel.request(c["playlist"].replace("clip.m3u8", "clip.ts") + "&n=1")[2] == b"video1"  # the player: one minute
  assert panel.request(c["playlist"].replace("clip.m3u8", "clip.ts"))[0] == 404  # no minute and no download: nothing
  assert panel.request("/api/clip.ts?ticket=wrong&n=0")[0] == 404
  assert panel.request(c["playlist"].replace(c["ticket"], c["ticket"] + "x"))[0] == 404
  panel.now += hexaweb.CLIP_TICKET_S + 1
  assert panel.request(c["playlist"])[0] == 404  # expired
  assert panel.json("/api/mark/clip", {"id": "nope"}, token=token)[0] == 404
  panel.vs = VehicleState(onroad=True)
  assert panel.json("/api/mark/clip", {"id": "m1"}, token=token)[0] == 409


def clip_drive(panel, tmp_path, video=(0, 1, 2)):
  """Minutes 0..2 of a drive, minute n ending at 50_000 + 60 n; the light video only in the minutes of `video`."""
  import os
  for n in range(3):
    d = tmp_path / f"00000022--abcdef0123--{n}"
    d.mkdir()
    (d / "qlog.zst").write_bytes(b"q")
    files = ["qlog.zst"]
    if n in video:
      (d / "qcamera.ts").write_bytes(f"v{n}".encode())
      files.append("qcamera.ts")
    for f in files:
      os.utime(d / f, (50_000. + 60 * n, 50_000. + 60 * n))
  panel.backend.log_root = str(tmp_path)


# (minute of the mark, seconds into it, Marcar's window) -> (start, where the mark is, minutes, minute before missing)
CLIP_CASES = [
  ((1, 5, None), (35., 65., 2, False)),  # "Agora" at a minute's start: 30 s before, from the minute before
  ((1, 50, None), (20., 50., 2, False)),  # at its end: the next minute too, for the 15 s after
  ((1, 5, [10, 0]), (45., 65., 2, False)),  # "10 s antes": 20 s
  ((1, 25, [10, 0]), (5., 25., 1, False)),
  ((1, 30, [30, 0]), (50., 90., 2, False)),  # "30 s antes": 40 s
  ((1, 5, [0, 30]), (0., 5., 1, False)),  # "30 s depois": from 5 s before the mark
  ((1, 30, [0, 30]), (25., 30., 2, False)),  # ...and the next minute for the 30 s after
  ((1, 59, [0, 0]), (29., 59., 2, False)),  # an old mark with [0, 0]: like "Agora"
  ((0, 5, None), (0., 5., 1, True)),  # the drive's first minute: there is no minute before
  ((2, 30, [0, 30]), (25., 30., 1, False)),  # the drive's last minute: no next one
]


@pytest.mark.parametrize("where,want", CLIP_CASES)
def test_a_clip_starts_before_the_mark(panel, tmp_path, where, want):
  seg, offset, win = where
  clip_drive(panel, tmp_path)
  mark = {"id": "m3", "t": 50_000. + 60 * (seg - 1) + offset, "reason": "curva", "label": "curva"}
  if win is not None:
    mark["win"] = win
  panel.store.put(marks.MARKS_PARAM, [mark])
  status, c = panel.json("/api/mark/clip", {"id": "m3"}, token=panel.pair())
  assert status == 200
  start, mark_at, minutes, short = want
  assert (c["start"], c["mark_at"], c["minutes"], c["short"]) == (start, mark_at, minutes, short)
  assert c["lead"] == mark_at - start and c["seconds"] == 60 * minutes
  text = panel.request(c["playlist"])[2].decode()
  assert text.count("#EXTINF") == minutes and f"TIME-OFFSET={start:.1f}," in text
  first = seg - (1 if mark_at >= 60 else 0)
  assert panel.request(c["video"])[2] == b"".join(f"v{n}".encode() for n in range(first, first + minutes))


def test_a_clip_without_the_minute_before(panel, tmp_path):
  clip_drive(panel, tmp_path, video=(1, 2))  # minute 0's video was cleaned up
  panel.store.put(marks.MARKS_PARAM, [{"id": "m4", "t": 50_005., "reason": "curva", "label": "curva", "win": [30, 0]}])
  status, c = panel.json("/api/mark/clip", {"id": "m4"}, token=panel.pair())
  assert status == 200 and (c["start"], c["lead"], c["minutes"], c["short"]) == (0., 5., 1, True)  # the phone says why it's short
  assert panel.request(c["video"])[2] == b"v1"


def test_no_clip_when_the_marks_own_minute_has_no_video(panel, tmp_path):
  clip_drive(panel, tmp_path, video=(0, 2))
  panel.store.put(marks.MARKS_PARAM, [{"id": "m5", "t": 50_030., "reason": "curva", "label": "curva"}])
  assert panel.json("/api/mark/clip", {"id": "m5"}, token=panel.pair()) == (404, {"error": "Sem vídeo para esta marca."})


def test_clip_window_from_marcar():
  assert hexaweb.clip_window({}) == (30., 15.)
  assert hexaweb.clip_window({"win": [0, 0]}) == (30., 15.)
  assert hexaweb.clip_window({"win": [10, 0]}) == (20., 15.)
  assert hexaweb.clip_window({"win": [30, 0]}) == (40., 15.)
  assert hexaweb.clip_window({"win": [0, 30]}) == (5., 40.)
  assert hexaweb.clip_window({"win": [60, 0]}) == (70., 15.)  # the most marks.py keeps: two minutes back
  assert hexaweb.clip_minutes(5, 5., 70., 15., lambda n: True) == (3, 5)
  assert hexaweb.clip_minutes(5, 5., 70., 15., lambda n: n != 3) == (4, 5)  # a gap stops it: the minutes play in a row
  assert hexaweb.clip_minutes(5, 5., 70., 15., lambda n: n != 4) == (5, 5)


def test_mark_clip_without_video(panel, tmp_path):
  d = tmp_path / "00000021--abcdef0123--0"
  d.mkdir()
  (d / "qlog.zst").write_bytes(b"q")
  panel.backend.log_root = str(tmp_path)
  panel.store.put(marks.MARKS_PARAM, [{"id": "m2", "t": (d / "qlog.zst").stat().st_mtime - 30}])
  token = panel.pair()
  assert panel.json("/api/mark/clip", {"id": "m2"}, token=token) == (404, {"error": "Sem vídeo para esta marca."})


# *** lizpilot.local is this comma ***

def test_the_fixed_name_is_checked_with_a_one_time_code(panel):
  panel.backend.fixed_status = lambda: {"state": "ok", "name": "lizpilot.local"}
  status, body = panel.json("/api/fixed/nonce")  # before pairing: no token
  assert status == 200 and body["state"] == "ok" and len(body["nonce"]) >= 16 and len(body["answer"]) >= 16
  status, headers, data = panel.request("/api/fixed/check?n=" + body["nonce"])
  assert status == 200 and json.loads(data) == {"answer": body["answer"]}
  assert headers["Access-Control-Allow-Origin"] == "*"  # a page served by address reads only this
  assert panel.json("/api/fixed/check?n=" + body["nonce"]) == (200, {"answer": None})  # once
  assert panel.json("/api/fixed/check?n=guess") == (200, {"answer": None})


def test_the_fixed_name_code_expires_and_is_bounded(panel):
  panel.backend.fixed_status = lambda: {"state": "ok", "name": "lizpilot.local"}
  _, old = panel.json("/api/fixed/nonce")
  panel.now += hexaweb.FIXED_NONCE_S + 1
  assert panel.json("/api/fixed/check?n=" + old["nonce"])[1] == {"answer": None}
  codes = [panel.json("/api/fixed/nonce")[1] for _ in range(hexaweb.MAX_FIXED_NONCES + 3)]
  assert len(panel.backend._fixed_nonces) == hexaweb.MAX_FIXED_NONCES
  assert panel.json("/api/fixed/check?n=" + codes[0]["nonce"])[1] == {"answer": None}  # the oldest went first
  assert panel.json("/api/fixed/check?n=" + codes[-1]["nonce"])[1] == {"answer": codes[-1]["answer"]}


def test_no_code_while_another_comma_has_the_name(panel):
  for state in ("conflict", "off", "starting"):
    panel.backend.fixed_status = lambda state=state: {"state": state, "name": "lizpilot.local"}
    assert panel.json("/api/fixed/nonce") == (200, {"nonce": None, "state": state})
  token = panel.pair()
  assert panel.json("/api/state", token=token)[1]["fixed"]["state"] == "starting"


def test_the_fixed_name_check_is_local_only(panel, monkeypatch):
  monkeypatch.setattr(hexaweb.phone, "is_local_address", lambda ip: False)
  assert panel.json("/api/fixed/nonce")[0] == 403
  assert panel.json("/api/fixed/check?n=x")[0] == 403


def test_the_app_checks_this_is_its_comma_without_the_token(panel):  # APP-02: before it opens a page or sends the token
  token = panel.pair()
  key = hashlib.sha256(token.encode()).hexdigest()
  me, nonce = hashlib.sha256(key.encode()).hexdigest()[:16], "abcDEF_-0123456789xyz"
  status, body = panel.json(f"/api/ident?p={me}&n={nonce}")  # no Authorization header
  assert status == 200 and body == {"v": 1, "mac": hmac.new(key.encode(), ("lizpilot-ident|v1|" + nonce).encode(), hashlib.sha256).hexdigest()}
  assert panel.json(f"/api/ident?p={'0' * 16}&n={nonce}") == (200, {"v": 1, "mac": None})  # a phone this comma doesn't know
  assert panel.json(f"/api/ident?p={me}&n=x")[0] == 400 and panel.json("/api/ident")[0] == 400


def test_the_app_check_is_local_only(panel, monkeypatch):
  monkeypatch.setattr(hexaweb.phone, "is_local_address", lambda ip: False)
  assert panel.json(f"/api/ident?p={'0' * 16}&n={'n' * 22}")[0] == 403


def clip_ready(panel, tmp_path):
  import os
  d = tmp_path / "00000020--abcdef0123--0"
  d.mkdir()
  for f in ("qlog.zst", "qcamera.ts"):
    (d / f).write_bytes(b"video")
    os.utime(d / f, (50_000., 50_000.))
  panel.backend.log_root = str(tmp_path)
  panel.store.put(marks.MARKS_PARAM, [{"id": "m1", "t": 49_990., "reason": "curva", "label": "curva"}])
  status, body = panel.json("/api/mark/clip", {"id": "m1"}, token=panel.pair())
  assert status == 200
  return body


def test_a_clip_link_stops_when_the_car_turns_on(panel, tmp_path):
  body = clip_ready(panel, tmp_path)
  assert panel.request(body["video"])[0] == 200
  panel.vs = VehicleState(onroad=True)
  assert panel.request(body["video"])[0] == 404
  assert panel.request(body["playlist"])[0] == 404


def test_a_fake_on_the_network_cant_pass_for_this_comma(panel):
  panel.backend.fixed_status = lambda: {"state": "ok"}
  a = panel.backend.fixed_nonce("192.168.0.10")
  assert a["answer"] and a["answer"] != a["nonce"]
  assert panel.backend.fixed_check(a["nonce"], "192.168.0.66") == {"answer": None}  # relayed by another device
  b = panel.backend.fixed_nonce("192.168.0.10")
  assert panel.backend.fixed_check(b["nonce"], "192.168.0.10") == {"answer": b["answer"]}
  assert panel.backend.fixed_check(b["nonce"], "192.168.0.10") == {"answer": None}  # once


def test_a_summary_stops_inside_a_long_drive(tmp_path):
  from openpilot.selfdrive.hexa import timeline
  files = {n: str(tmp_path / f"none{n}") for n in range(5)}
  seen = []

  def stop():
    seen.append(1)
    return len(seen) > 2

  assert timeline.trip_summary(files, stop) is None
  assert len(seen) == 3


def test_recent_errors_reads_only_the_end_of_a_big_log(tmp_path, monkeypatch):
  from pathlib import Path
  from openpilot.selfdrive.hexa import live
  log = tmp_path / "swaglog.0000000001"
  log.write_bytes(b"x" * (live.ERRORS_TAIL * 3) + b"\n" + json.dumps({"levelname": "ERROR", "levelnum": 40, "msg": "falhou", "created": 5.}).encode() + b"\n")

  def inteiro(self):
    raise AssertionError("read the whole log")

  monkeypatch.setattr(Path, "read_bytes", inteiro)
  assert [e["msg"] for e in live.recent_errors(tmp_path)] == ["falhou"]


def test_a_profile_never_changes_anything_with_the_car_moving(panel):
  token = panel.pair()
  panel.json("/api/profiles/save", {"name": "Estrada"}, token=token)
  profile = panel.json("/api/state", token=token)[1]["profiles"]["Estrada"]
  panel.json("/api/option", {"key": "LaneOffsetEnabled", "value": True}, token=token)
  panel.vs = VehicleState(onroad=True, parked=False, standstill=False, engaged=True)
  status, _ = panel.json("/api/options/import", {"options": profile, "apply": True}, token=token)
  assert status == 409 and panel.store.get_bool("LaneOffsetEnabled") is True


# *** the comma's own settings, from the phone (UI-09) ***

def test_comma_state_lists_levels_and_the_locked_longitudinal(panel):
  token = panel.pair()
  status, body = panel.json("/api/comma", token=token)
  assert status == 200 and [g[0] for g in body["groups"]] == ["prefs", "dev"]
  items = {i["key"]: i for i in body["items"]}
  assert items["AlphaLongitudinalEnabled"]["level"] == "trav" and items["AlphaLongitudinalEnabled"]["writable"] is False
  assert items["IsMetric"]["writable"] is True and items["IsMetric"]["effect"] == "Vale agora."
  assert [s["key"] for s in body["long"]["steps"]] == ["sombra", "aeb", "calibrado", "testes", "pista"]
  assert {a["action"] for a in body["actions"]} == {"reboot", "poweroff", "recalibrate"}
  panel.store.put_bool("IsReleaseBranch", True)
  keys = {i["key"] for i in panel.json("/api/comma", token=token)[1]["items"]}
  assert "AlphaLongitudinalEnabled" not in keys and "AdbEnabled" in keys


def test_a_phone_level_setting_applies_at_once_and_is_audited(panel):
  token = panel.pair()
  assert panel.json("/api/comma/set", {"key": "IsMetric", "value": True}, token=token) == (200, {"ok": True, "message": "Vale agora."})
  assert panel.store.get_bool("IsMetric") is True
  assert panel.store.get(phone.AUDIT_PARAM)[-1]["key"] == "IsMetric"
  assert panel.json("/api/comma/set", {"key": "IsMetric", "value": "yes"}, token=token)[0] == 400
  assert panel.json("/api/comma/set", {"key": "AdbEnabled", "value": True}, token=token)[0] == 409  # only on the comma
  assert panel.json("/api/comma/set", {"key": "AlphaLongitudinalEnabled", "value": True}, token=token)[0] == 409
  assert panel.json("/api/comma/set", {"key": "Nope", "value": True}, token=token)[0] == 404
  panel.vs = VehicleState(onroad=True, parked=False, standstill=False, engaged=True)
  assert panel.json("/api/comma/set", {"key": "IsMetric", "value": False}, token=token)[0] == 409
  assert panel.store.get_bool("IsMetric") is True


def test_device_actions_only_with_the_car_off(panel):
  token = panel.pair()
  for k in ("CalibrationParams", "LiveTorqueParameters", "LiveParametersV2", "LiveDelay"):
    panel.store.put(k, "x")
  assert panel.json("/api/comma/action", {"action": "recalibrate"}, token=token)[0] == 200
  assert all(panel.store.get(k) is None for k in ("CalibrationParams", "LiveTorqueParameters", "LiveParametersV2", "LiveDelay"))
  assert panel.store.get_bool("OnroadCycleRequested")
  panel.vs = VehicleState(onroad=True, parked=True, standstill=True, engaged=False)
  assert panel.json("/api/comma/action", {"action": "reboot"}, token=token)[0] == 409
  assert not panel.store.get_bool("DoReboot")
  panel.vs = VehicleState(onroad=False)
  assert panel.json("/api/comma/action", {"action": "reboot"}, token=token)[0] == 200 and panel.store.get_bool("DoReboot")
  assert panel.json("/api/comma/action", {"action": "uninstall"}, token=token)[0] == 404  # never from the phone


def test_a_red_light_camera_from_the_phone(panel, tmp_path):
  token, r = radar_panel(panel, tmp_path)
  assert add_point(panel, token, kind="sinal")[0] == 200  # no limit: just the light
  assert add_point(panel, token, kind="sinal", limit=10)[0] == 400
  mine = panel.json("/api/radars/near", token=token)[1]["mine"]
  assert mine[0]["kind"] == "sinal"


def test_a_very_hard_brake_found_in_a_drive_becomes_a_mark_once(panel, tmp_path):
  import os
  trip_logs(panel, tmp_path)
  f = tmp_path / "0000000f--8784034901--1" / "qlog.zst"
  os.utime(f, (50_060., 50_060.))  # minute 1 ended at 50 060: it started at 50 000
  jobs = []
  panel.backend._spawn = jobs.append
  panel.backend._summarize = lambda files, stop: {"segments": len(files), "auto_marks": [{"seg": 1, "left": 47.5, "kind": "hardbrake"},
                                                                                     {"seg": 9, "left": 1., "kind": "hardbrake"}]}
  token = panel.pair()
  panel.json("/api/trips", token=token)
  jobs.pop()()
  auto = [m for m in panel.store.get(marks.MARKS_PARAM) or [] if m.get("reason") == "brusca"]
  assert len(auto) == 1 and auto[0]["t"] == 50_012 and auto[0]["label"] == "freada brusca" and auto[0]["source"] == "auto"
  panel.backend._auto_marks(panel.backend._trips()[0], {"auto_marks": [{"seg": 1, "left": 47., "kind": "hardbrake"}]})  # read again
  assert len([m for m in panel.store.get(marks.MARKS_PARAM) if m.get("reason") == "brusca"]) == 1


def test_each_drive_keeps_its_path_for_the_map(panel, tmp_path):
  logs = tmp_path / "realdata"
  logs.mkdir()
  trip_logs(panel, logs)
  jobs = []
  panel.backend._spawn = jobs.append
  path = [[1.0, 2.0, 0, 0], [1.001, 2.001, 3, 10]]  # made-up place
  panel.backend._summarize = lambda files, stop: {"segments": len(files), "path": path}
  token = panel.pair()
  assert panel.json("/api/trip/path?route=0000000f--8784034901", token=token)[0] == 404  # not read yet
  t = panel.json("/api/trips", token=token)[1]
  assert t["pending"] and t["pending_n"] == 1
  jobs.pop()()
  status, body = panel.json("/api/trip/path?route=0000000f--8784034901", token=token)
  assert status == 200 and body["points"] == path
  assert "path" not in panel.json("/api/trips", token=token)[1]["trips"][0]["summary"]  # the list stays light
  assert (tmp_path / hexaweb.PATH_DIR / "0000000f--8784034901.json").is_file()  # next to the logs, never in params
  gone = tmp_path / hexaweb.PATH_DIR / "00000001--0000000000.json"  # a drive the comma already deleted
  gone.write_text("[]")
  panel.backend._paths_pruned = -1e9
  panel.json("/api/trips", token=token)
  assert not gone.exists() and (tmp_path / hexaweb.PATH_DIR / "0000000f--8784034901.json").is_file()
  assert panel.json("/api/trip/path?route=../../etc", token=token)[0] == 404
  assert panel.json("/api/trip/path?route=0000000f--8784034901")[0] == 401
  panel.vs = VehicleState(onroad=True)
  assert panel.json("/api/trip/path?route=0000000f--8784034901", token=token)[0] == 409


def test_the_copilots_events_stay_in_the_newest_drives_only(panel, tmp_path):
  # "A conferir" is about the last drives: older summaries drop "cop", so the param stays small
  logs = tmp_path / "realdata"
  logs.mkdir()
  trip_logs(panel, logs)
  jobs = []
  panel.backend._spawn = jobs.append
  cop = [{"s": 63.1, "kind": "pull_away", "v": 0, "data": {"d": 7, "go_s": 4.0}}]
  panel.backend._summarize = lambda files, stop: {"segments": len(files), "cop": cop}
  old = {f"0000000{i}--0000000000": {"segments": 1, "sv": hexaweb.SUMMARY_VERSION, "cop": cop} for i in range(hexaweb.COP_KEEP)}
  panel.store.put(hexaweb.SUMMARY_PARAM, old)
  token = panel.pair()
  panel.json("/api/trips", token=token)
  jobs.pop()()
  kept = panel.store.get(hexaweb.SUMMARY_PARAM)
  with_cop = sorted(k for k, v in kept.items() if "cop" in v)
  assert len(with_cop) == hexaweb.COP_KEEP and "0000000f--8784034901" in with_cop and "00000000--0000000000" not in with_cop
  trip = next(t for t in panel.json("/api/trips", token=token)[1]["trips"] if t["id"] == "0000000f--8784034901")
  assert trip["summary"]["cop"] == cop  # the phone gets them with the drive


def test_a_drive_can_be_hidden_from_the_lists_and_shown_again(panel, tmp_path):
  trip_logs(panel, tmp_path)
  token = panel.pair()
  route = "0000000f--8784034901"
  assert panel.json("/api/trips", token=token)[1]["trips"][0]["hidden"] is False
  assert panel.json("/api/trip/hide", {"route": route}, token=token)[0] == 200
  assert panel.json("/api/trips", token=token)[1]["trips"][0]["hidden"] is True
  assert (tmp_path / f"{route}--0" / "qlog.zst").is_file()  # nothing is deleted
  assert panel.store.get(phone.AUDIT_PARAM)[-1]["event"] == "trip_hidden"
  assert panel.json("/api/trip/hide", {"route": route, "hidden": False}, token=token)[0] == 200
  assert panel.json("/api/trips", token=token)[1]["trips"][0]["hidden"] is False and panel.store.get(hexaweb.HIDDEN_PARAM) == []
  assert panel.json("/api/trip/hide", {"route": "0000000f--0000000000"}, token=token)[0] == 404  # only drives on the comma
  assert panel.json("/api/trip/hide", {"route": route})[0] == 401
  panel.vs = VehicleState(onroad=True)
  assert panel.json("/api/trip/hide", {"route": route}, token=token)[0] == 409


def test_the_drive_history_never_goes_into_the_logs():
  # loggerd copies every param without DONT_LOG into each segment's initData (system/loggerd/logger.cc), and the qlogs get uploaded
  keys = (Path(hexaweb.__file__).parents[2] / "common" / "params_keys.h").read_text()
  for key in (hexaweb.SUMMARY_PARAM, hexaweb.HIDDEN_PARAM):
    line = next(ln for ln in keys.splitlines() if f'"{key}"' in ln)
    assert "DONT_LOG" in line, key


def test_a_drive_that_lost_minutes_keeps_its_summary(panel):
  # the deleter takes the oldest minutes: the numbers and the path of the whole drive stay
  trip = {"id": "0000000f--8784034901", "files": {5: "a", 6: "b"}}
  kept = {trip["id"]: {"segments": 30, "sv": hexaweb.SUMMARY_VERSION, "km": 30.}}
  assert panel.backend._summary_of(trip, kept, {trip["id"]})["km"] == 30.
  kept[trip["id"]]["segments"] = 1  # one that grew is redone
  assert panel.backend._summary_of(trip, kept, {trip["id"]}) is None


def test_the_list_of_drives_brings_every_mark(panel, tmp_path):
  trip_logs(panel, tmp_path)
  panel.store.put(marks.MARKS_PARAM, [{"id": str(i), "t": 1000 + i, "reason": "outro", "label": "outro", "source": "celular"} for i in range(80)])
  token = panel.pair()
  assert len(panel.json("/api/marks", token=token)[1]["marks"]) == 30  # the screen's list stays short
  assert len(panel.json("/api/trips", token=token)[1]["marks"]) == 80


def test_the_video_of_a_hard_brake_is_kept(panel, tmp_path, monkeypatch):
  trip_logs(panel, tmp_path)
  kept = []
  monkeypatch.setattr(hexaweb.os, "setxattr", lambda path, name, value: kept.append((Path(path).name, name, value)), raising=False)
  panel.backend._auto_marks(panel.backend._trips()[0], {"auto_marks": [{"seg": 1, "left": 5., "kind": "hardbrake"}]})
  assert kept == [("0000000f--8784034901--1", "user.preserve", b"1")]


def test_an_empty_list_of_drives_deletes_no_path(panel, tmp_path):
  # a folder that can't be read right now isn't every drive deleted
  panel.backend.log_root = str(tmp_path / "realdata")
  trips = tmp_path / hexaweb.PATH_DIR
  trips.mkdir()
  kept = trips / "0000000f--8784034901.json"
  kept.write_text("[]")
  panel.backend._prune_paths(set())
  assert kept.exists()


# ----- missions (owner, 02/11)

def test_missions_need_pairing_and_come_with_the_state(panel):
  assert panel.request("/api/missions")[0] == 401
  tok = panel.pair()
  status, body = panel.json("/api/missions", token=tok)
  assert status == 200 and body["state"] == {"active": [], "answers": {}, "pending": []} and len(body["missions"]) > 50
  assert body["route"]["before"] and body["route"]["key"] == "proxima" and body["max_active"] == 12


def test_take_while_driving_but_answer_only_parked(panel):
  tok = panel.pair()
  panel.vs = VehicleState(onroad=True, parked=False, standstill=False, engaged=True)
  status, body = panel.json("/api/missions", {"action": "take", "key": "noite"}, token=tok)
  assert status == 200 and body["state"]["active"] == ["noite"]  # changes nothing in the car
  status, body = panel.json("/api/missions", {"action": "answer", "key": "portas", "ok": True}, token=tok)
  assert status == 409
  panel.vs = VehicleState(onroad=True, parked=True, standstill=True, engaged=False)
  status, body = panel.json("/api/missions", {"action": "answer", "key": "portas", "ok": True}, token=tok)
  assert status == 200 and body["state"]["answers"] == {"portas": 1}
  assert panel.json("/api/missions", {"action": "take", "key": "x"}, token=tok)[0] == 404
  assert panel.json("/api/missions", {"action": "voar", "key": "noite"}, token=tok)[0] == 400
  assert panel.json("/api/missions", {"action": "pack", "key": "proxima"}, token=tok)[0] == 200
  assert panel.json("/api/missions", {"action": "pack", "key": "x"}, token=tok)[0] == 400


def test_did_it_now_is_a_real_mark_with_the_mission(panel):
  tok = panel.pair()
  panel.vs = VehicleState(onroad=True, parked=False, standstill=False, engaged=True)
  status, body = panel.json("/api/missions", {"action": "mark", "key": "fila"}, token=tok)
  assert status == 200
  pend = body["state"]["pending"]
  mk = [m for m in panel.store.get(marks.MARKS_PARAM) if m["id"] == pend[0]["mark"]][0]
  assert pend[0]["key"] == "fila" and mk["reason"] == "missao" and mk["note"] == "Fila atrás de carro" and mk["source"] == "celular"
  assert panel.store.get_bool(marks.BOOKMARK_REQUEST_PARAM)  # the comma raises its own bookmark: the minute is in the log
  assert panel.json("/api/missions", {"action": "mark", "key": "fila"}, token=tok)[0] == 429  # same pace as Marcar
  assert panel.json("/api/missions", {"action": "mark", "key": "portas"}, token=tok)[0] == 400  # done parked, answered, not marked


# ----- sync (APP-01): the phone keeps its own copy and asks only for what's new

def two_old_trips(panel, tmp_path):
  import os
  for route in ("0000000e--1111111111", "0000000f--8784034901"):
    for n in (0, 1):
      d = tmp_path / f"{route}--{n}"
      d.mkdir()
      (d / "qlog.zst").write_bytes(b"")
      os.utime(d / "qlog.zst", (500, 500))  # drives from before the phone's cursor
  panel.backend.log_root = str(tmp_path)
  summary = {"segments": 2, "km": 3., "sv": hexaweb.SUMMARY_VERSION}
  panel.store.put(hexaweb.SUMMARY_PARAM, {"0000000e--1111111111": dict(summary), "0000000f--8784034901": dict(summary)})


class Spy:
  """Counts what a call reads: the summaries param, a log, a trip's path file."""
  def __init__(self, panel, monkeypatch):
    self.reads: list[str] = []
    get = panel.store.get
    monkeypatch.setattr(panel.store, "get", lambda key: self.reads.append(key) or get(key))
    read_text = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda p, *a, **k: self.reads.append(f"file:{p.name}") or read_text(p, *a, **k))
    monkeypatch.setattr(hexaweb.timeline, "read_rlog", lambda *a, **k: self.reads.append("rlog") or [])
    panel.backend._summarize = lambda *a, **k: self.reads.append("summarize")

  def big(self):
    return [r for r in self.reads if r in (hexaweb.SUMMARY_PARAM, marks.MARKS_PARAM, "rlog", "summarize") or r.startswith("file:")]


def test_sync_a_second_call_with_nothing_new_reads_no_log_and_no_summaries(panel, tmp_path, monkeypatch):
  two_old_trips(panel, tmp_path)
  token = panel.pair()
  full = panel.json("/api/trips", token=token)[1]
  assert len(full["trips"]) == 2 and "delta" not in full and full["v"] and not full["pending"]
  spy = Spy(panel, monkeypatch)
  panel.now += 5
  status, again = panel.json("/api/trips?since=" + full["v"], token=token)
  assert status == 200 and again == {"same": True, "v": full["v"]}
  assert spy.big() == []  # a folder listing at most, never a log nor the 300 summaries


def test_sync_brings_only_the_drive_that_changed(panel, tmp_path):
  two_old_trips(panel, tmp_path)
  token = panel.pair()
  v = panel.json("/api/trips", token=token)[1]["v"]
  panel.now += 1000  # later, the comma keeps a new summary of one drive, and the owner hides the other
  kept = panel.store.get(hexaweb.SUMMARY_PARAM)
  kept["0000000f--8784034901"] = dict(kept["0000000f--8784034901"], km=9.)
  panel.backend._put(hexaweb.SUMMARY_PARAM, kept)
  assert panel.json("/api/trip/hide", {"route": "0000000e--1111111111", "hidden": True}, token=token)[0] == 200
  status, d = panel.json("/api/trips?since=" + v, token=token)
  assert status == 200 and d["delta"] is True
  assert [t["id"] for t in d["trips"]] == ["0000000f--8784034901"] and d["trips"][0]["summary"]["km"] == 9.
  assert sorted(d["ids"]) == ["0000000e--1111111111", "0000000f--8784034901"] and d["hidden"] == ["0000000e--1111111111"]
  assert "marks" not in d  # no new mark: the phone keeps its own
  assert d["v"] != v and panel.json("/api/trips?since=" + d["v"], token=token)[1]["same"] is True


def test_sync_a_new_mark_comes_with_the_delta(panel, tmp_path):
  two_old_trips(panel, tmp_path)
  token = panel.pair()
  v = panel.json("/api/trips", token=token)[1]["v"]
  assert panel.json("/api/mark", {"reason": "balancou"}, token=token)[0] == 200
  d = panel.json("/api/trips?since=" + v, token=token)[1]
  assert d["delta"] and d["trips"] == [] and [m["reason"] for m in d["marks"]] == ["balancou"]


def test_sync_a_bad_cursor_or_a_change_number_from_the_future_gets_everything(panel, tmp_path):
  two_old_trips(panel, tmp_path)
  token = panel.pair()
  v = panel.json("/api/trips", token=token)[1]["v"]
  for since in ("x", "../etc", "abc.def", "a.b.123", v.rsplit(".", 1)[0] + ".99999999999"):
    d = panel.json("/api/trips?since=" + since, token=token)[1]
    assert len(d["trips"]) == 2 and "delta" not in d and len(d["marks"]) == 0


def test_sync_a_summary_kept_again_with_the_clock_behind_still_reaches_the_phone(panel, tmp_path):
  two_old_trips(panel, tmp_path)
  token = panel.pair()
  v = panel.json("/api/trips", token=token)[1]["v"]
  panel.now -= 86400  # the comma rebooted before it had the time: its clock is a day behind
  kept = panel.store.get(hexaweb.SUMMARY_PARAM)
  kept["0000000e--1111111111"] = dict(kept["0000000e--1111111111"], km=7.)
  panel.backend._put(hexaweb.SUMMARY_PARAM, kept)
  d = panel.json("/api/trips?since=" + v, token=token)[1]
  assert d["delta"] and [t["id"] for t in d["trips"]] == ["0000000e--1111111111"] and d["trips"][0]["summary"]["km"] == 7.


def test_sync_a_minute_the_deleter_took_reaches_the_phone(panel, tmp_path):
  import shutil
  two_old_trips(panel, tmp_path)
  token = panel.pair()
  v = panel.json("/api/trips", token=token)[1]["v"]
  shutil.rmtree(tmp_path / "0000000f--8784034901--1")  # no write here, and the folder's time is from before the cursor
  panel.now += hexaweb.TRIP_INDEX_S + 1  # the folder listing is read again
  d = panel.json("/api/trips?since=" + v, token=token)[1]
  assert d["delta"] and [t["id"] for t in d["trips"]] == ["0000000f--8784034901"]
  assert d["trips"][0]["minutes"] == 1 and d["trips"][0]["segments"] == [0]


def test_sync_another_phone_asking_first_does_not_hide_a_change(panel, tmp_path):
  two_old_trips(panel, tmp_path)
  token = panel.pair()
  v = panel.json("/api/trips", token=token)[1]["v"]
  kept = panel.store.get(hexaweb.SUMMARY_PARAM)
  kept["0000000f--8784034901"] = dict(kept["0000000f--8784034901"], km=5.)
  panel.backend._put(hexaweb.SUMMARY_PARAM, kept)
  other = panel.json("/api/trips?since=" + v, token=token)[1]  # the other phone (same cursor) takes the change first
  assert [t["id"] for t in other["trips"]] == ["0000000f--8784034901"]
  again = panel.json("/api/trips?since=" + v, token=token)[1]
  assert [t["id"] for t in again["trips"]] == ["0000000f--8784034901"]
  assert panel.json("/api/trips?since=" + again["v"], token=token)[1]["same"] is True


def test_sync_a_restarted_comma_sends_the_whole_list(panel, tmp_path):
  two_old_trips(panel, tmp_path)
  token = panel.pair()
  v = panel.json("/api/trips", token=token)[1]["v"]
  panel.backend._boot = "0badf00d"  # a new process: the change numbers start again
  d = panel.json("/api/trips?since=" + v, token=token)[1]
  assert "delta" not in d and len(d["trips"]) == 2 and ".0badf00d." in d["v"]


def test_sync_drives_still_to_summarize_are_asked_for_again(panel, tmp_path):
  two_old_trips(panel, tmp_path)
  panel.store.put(hexaweb.SUMMARY_PARAM, {})
  jobs = []
  panel.backend._spawn = jobs.append
  token = panel.pair()
  v = panel.json("/api/trips", token=token)[1]["v"]
  assert len(jobs) == 1
  jobs.clear()
  panel.backend._summary_busy = False  # the job stopped (the car turned on) before finishing
  d = panel.json("/api/trips?since=" + v, token=token)[1]
  assert "same" not in d and len(jobs) == 1  # not "nothing new": the summaries start again


def test_sync_trips_never_with_the_car_on(panel, tmp_path):
  two_old_trips(panel, tmp_path)
  token = panel.pair()
  v = panel.json("/api/trips", token=token)[1]["v"]
  panel.vs = VehicleState(onroad=True, parked=True, standstill=True)
  assert panel.json("/api/trips?since=" + v, token=token)[0] == 409


def test_sync_marks_and_missions_answer_same_when_nothing_changed(panel, monkeypatch):
  token = panel.pair()
  m = panel.json("/api/marks", token=token)[1]
  ms = panel.json("/api/missions", token=token)[1]
  assert m["v"] and ms["v"] and ms["missions"]
  spy = Spy(panel, monkeypatch)
  assert panel.json("/api/marks?since=" + m["v"], token=token)[1] == {"same": True, "v": m["v"]}
  assert panel.json("/api/missions?since=" + ms["v"], token=token)[1] == {"same": True, "v": ms["v"]}
  assert spy.big() == [] and "HexaMissions" not in spy.reads
  panel.json("/api/mark", {"reason": "curva"}, token=token)
  m2 = panel.json("/api/marks?since=" + m["v"], token=token)[1]
  assert m2["v"] != m["v"] and m2["marks"][0]["reason"] == "curva"
  key = "noite"
  assert panel.json("/api/missions", {"action": "take", "key": key}, token=token)[0] == 200
  ms2 = panel.json("/api/missions?since=" + ms["v"], token=token)[1]
  assert ms2["v"] != ms["v"] and key in ms2["state"]["active"]


def test_sync_a_path_the_phone_has_is_not_read_again(panel, tmp_path, monkeypatch):
  two_old_trips(panel, tmp_path)
  token = panel.pair()
  panel.backend._keep_path("0000000f--8784034901", [[1.0, 2.0, 0, 0], [1.001, 2.0, 0, 10]])
  p = panel.json("/api/trip/path?route=0000000f--8784034901", token=token)[1]
  assert len(p["points"]) == 2 and p["v"]
  spy = Spy(panel, monkeypatch)
  assert panel.json("/api/trip/path?route=0000000f--8784034901&since=" + p["v"], token=token)[1] == {"same": True, "v": p["v"]}
  assert spy.big() == []


def test_sync_a_param_another_process_wrote_changes_the_version(tmp_path):
  class FileStore(FakeStore):  # as Params: one file per key
    def get_param_path(self, key):
      return str(tmp_path / key)

  store = FileStore()
  b = hexaweb.PanelBackend(store, lambda: VehicleState(onroad=False))
  v = b._stamp(marks.MARKS_PARAM)
  assert b._stamp(marks.MARKS_PARAM) == v
  (tmp_path / marks.MARKS_PARAM).write_text("[]")  # written by someone else: no _put here
  assert b._stamp(marks.MARKS_PARAM) != v
  assert hexaweb.PanelBackend(store, lambda: VehicleState(onroad=False))._stamp(marks.MARKS_PARAM) != b._stamp(marks.MARKS_PARAM)  # a restart


def test_the_copilot_answer_is_a_mark_with_the_result(panel):
  tok = panel.pair()
  panel.vs = VehicleState(onroad=True, parked=False, standstill=False, engaged=True)
  status, body = panel.json("/api/missions", {"action": "mark", "key": "parar-de-vez", "result": "bad"}, token=tok)
  assert status == 200 and body["state"]["pending"][-1]["result"] == "bad"
  mk = [m for m in panel.store.get(marks.MARKS_PARAM) if m["id"] == body["state"]["pending"][-1]["mark"]][0]
  assert mk["note"] == "Sinal vermelho sem carro à frente" and mk["label"] == "missão: não deu" and mk["reason"] == "missao"
  assert panel.logged[-1]["label"] == "missão: não deu"  # the log says how it went, for the CTO
  panel.now += 3
  assert panel.json("/api/missions", {"action": "mark", "key": "aviso-acc", "result": "ok"}, token=tok)[0] == 200  # asked after a drive, answered driving
  panel.now += 3
  assert panel.json("/api/missions", {"action": "mark", "key": "fila", "result": "voar"}, token=tok)[0] == 400


def test_the_copilot_event_answer_carries_the_note_and_goes_back_to_when_it_happened(panel):
  tok = panel.pair()
  panel.vs = VehicleState(onroad=True, parked=False, standstill=True, engaged=True)
  body = {"action": "mark", "key": "sair-sozinho", "result": "bad", "note": "O carro da frente saiu: Não saiu (Lido às 19:02:10) — buzinaram", "delay_s": 42}
  status, res = panel.json("/api/missions", body, token=tok)
  assert status == 200
  mk = [m for m in panel.store.get(marks.MARKS_PARAM) if m["id"] == res["state"]["pending"][-1]["mark"]][0]
  assert mk["note"].startswith("Sair sozinho quando o da frente anda — O carro da frente saiu: Não saiu") and mk["note"].endswith("buzinaram")
  assert mk["delay_s"] == 42 and abs(mk["t"] - (panel.now - 42)) < 1  # the mark at the event, not at Salvar
  panel.now += 3
  status, res = panel.json("/api/missions", {"action": "mark", "key": "fila", "result": "ok", "delay_s": 900}, token=tok)
  mk = [m for m in panel.store.get(marks.MARKS_PARAM) if m["id"] == res["state"]["pending"][-1]["mark"]][0]
  assert status == 200 and mk["delay_s"] == 0 and mk["note"] == "Fila atrás de carro"  # past the 5 min the mark can go back: now, as before


def test_versions_need_pairing_and_come_newest_first(panel):
  assert panel.request("/api/versions")[0] == 401
  status, body = panel.json("/api/versions", token=panel.pair())
  assert status == 200 and body["releases"][0]["commit"] and body["releases"][-1]["commit"] == "7d2bedb"
  assert body["releases"][0]["commit"] in ("abcdef1", hexa_versions.RELEASES[0].commit)  # the top one is what this comma runs


def test_the_home_screen_icon_is_where_phones_look(panel):
  # owner, 03/11: the iPhone saved a plain "L". The icon answers at the linked path and at the ones iOS tries on its own
  for path in ("/icon-180.png", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png", "/favicon.ico"):
    status, headers, body = panel.request(path)
    assert status == 200 and headers["Content-Type"] == "image/png" and body[:8] == b"\x89PNG\r\n\x1a\n", path
    assert "max-age" in headers["Cache-Control"], path  # kept by the home screen, unlike the pages
  req = urllib.request.Request(panel.url + "/apple-touch-icon.png", method="HEAD")
  with urllib.request.urlopen(req, timeout=5) as r:
    assert r.status == 200 and r.read() == b"" and int(r.headers["Content-Length"]) > 1000
  assert panel.request("/")[1]["Cache-Control"] == "no-store"
  for page in ("index.html", "viagens.html", "replay.html"):
    html = (hexaweb.STATIC_DIR / page).read_text()
    assert 'rel="apple-touch-icon" href="/icon-180.png"' in html and 'name="apple-mobile-web-app-title" content="LizPilot"' in html, page


def _drive_ended_at(tmp_path, end):  # trip_logs' two minutes, the second closing at end
  import os
  for n in (0, 1):
    f = tmp_path / f"0000000f--8784034901--{n}" / "qlog.zst"
    os.utime(f, (end - 60 * (1 - n), end - 60 * (1 - n)))


def test_a_review_answer_with_the_car_off_goes_to_when_it_happened(panel, tmp_path):
  # CTO review of app-12: "A conferir" answers after the drive. The mark sits in the drive, at the event, with no bookmark:
  # with the car off no log is being written, and a bookmark asked now would only flag nothing
  trip_logs(panel, tmp_path)
  _drive_ended_at(tmp_path, panel.now - 600)
  token = panel.pair()
  at = panel.now - 600 - 90.5
  mark = panel.json("/api/mark", {"reason": "soltou", "note": "x", "at": at}, token=token)[1]["mark"]
  assert mark["t"] == int(at) and mark["delay_s"] == round(panel.now - at, 1) and mark["mono_ns"] is None
  assert not panel.store.get_bool(marks.BOOKMARK_REQUEST_PARAM)
  assert "_req_mono_ns" not in panel.store.get(marks.MARKS_PARAM)[-1]
  # outside every drive, in the future, not a number, or driving: now, with its bookmark, as before
  for vs, bad in ((VehicleState(onroad=False), panel.now - 4000), (VehicleState(onroad=False), panel.now + 50),
                  (VehicleState(onroad=False), "123"), (VehicleState(onroad=True), at)):
    panel.vs, panel.now = vs, panel.now + 3
    panel.store.remove(marks.BOOKMARK_REQUEST_PARAM)
    mark = panel.json("/api/mark", {"reason": "soltou", "at": bad}, token=token)[1]["mark"]
    assert mark["t"] == int(panel.now) and mark["delay_s"] == 0. and panel.store.get_bool(marks.BOOKMARK_REQUEST_PARAM)


def test_a_mission_answered_in_the_review_is_dated_at_the_event(panel, tmp_path):
  trip_logs(panel, tmp_path)
  _drive_ended_at(tmp_path, panel.now - 600)
  tok = panel.pair()
  at = panel.now - 600 - 30
  status, body = panel.json("/api/missions", {"action": "mark", "key": "fila", "result": "ok", "at": at}, token=tok)
  assert status == 200 and body["state"]["pending"][-1]["t"] == int(at)


def test_a_version_5_summary_still_holds_outside_the_newest_drives(panel):
  # CTO review of app-12: 6 only added the copilot's events, kept in the newest COP_KEEP; reading 300 drives again meant
  # hours with no numbers on the phone and the param rewritten 300 times
  trips = [{"id": f"000000{i:02x}--0000000000", "files": {0: "x"}} for i in range(30, 0, -1)]  # newest first
  newest = [t["id"] for t in trips]
  kept = {t["id"]: {"segments": 1, "sv": 5} for t in trips}
  assert [panel.backend._summary_of(t, kept, newest) is not None for t in trips] == [False] * hexaweb.COP_KEEP + [True] * (30 - hexaweb.COP_KEEP)
  kept = {t["id"]: {"segments": 1, "sv": 4} for t in trips}
  assert not any(panel.backend._summary_of(t, kept, newest) for t in trips)
  kept = {t["id"]: {"segments": 1, "sv": hexaweb.SUMMARY_VERSION} for t in trips}
  assert all(panel.backend._summary_of(t, kept, newest) for t in trips)


def test_each_drive_keeps_its_clock_from_its_last_read_minute(panel, tmp_path):
  trip_logs(panel, tmp_path)
  _drive_ended_at(tmp_path, 900.)
  jobs = []
  panel.backend._spawn = jobs.append
  panel.backend._summarize = lambda files, stop: {"segments": len(files), "cop": [], "end_s": 70.0, "end_seg": 0}  # the last minute unread
  token = panel.pair()
  panel.json("/api/trips", token=token)
  jobs.pop()()
  assert panel.store.get(hexaweb.SUMMARY_PARAM)["0000000f--8784034901"]["t0"] == 840. - 70.


# *** sending a mark's moment to the developers (selfdrive/hexa/share.py) ***

def _sharing_panel(panel, tmp_path, enabled=True):
  from openpilot.selfdrive.hexa import share
  sent = []

  def post(url, body, hdrs):
    sent.append(url)
    return 201, {"id": "s1"}
  panel.backend.sharer = share.Sharer(panel.store, str(tmp_path / "realdata"), url="https://r.example", post=post, clock=lambda: panel.now,
                                      cutter=lambda *a, **k: (b"cut", {"services": {"carState": 1}}), share_dir=tmp_path / "share",
                                      send_jitter_s=0.)
  panel.store.put(share.ENABLED_PARAM, enabled)
  from opendbc.car.structs import car
  panel.store.put("CarParamsPersistent", car.CarParams.new_message(brand="gwm", carFingerprint="GWM_HAVAL_H6").to_bytes())  # only the Haval
  return sent


def test_share_needs_a_paired_phone(panel, tmp_path):
  _sharing_panel(panel, tmp_path)
  assert panel.json("/api/share")[0] == 401
  assert panel.json("/api/share", {"id": "x"})[0] == 401
  assert panel.json("/api/share/forget", {})[0] == 401


def test_share_a_mark_from_the_phone(panel, tmp_path):
  sent = _sharing_panel(panel, tmp_path)
  token = panel.pair()
  status, body = panel.json("/api/mark", {"reason": "curva"}, token)
  mark_id = body["mark"]["id"]
  assert panel.json("/api/share", {"id": "nope"}, token)[0] == 404
  status, body = panel.json("/api/share", {"id": mark_id}, token)
  assert status == 200 and body["item"]["state"] == "fila"
  assert panel.store.get("HexaWebAudit")[-1]["event"] == "share_queued"
  assert panel.backend.share_step(network_ok=True) == "cortado"
  assert panel.backend.share_step(network_ok=True) == "enviado" and sent[-1].endswith("/v1/share")
  status, state = panel.json("/api/share", token=token)
  assert status == 200 and state["enabled"] and state["receiver"] and state["sent"] == 1
  assert state["items"][0]["id"] == mark_id and state["items"][0]["state"] == "enviado"


def test_share_is_not_cut_with_the_car_on(panel, tmp_path):
  _sharing_panel(panel, tmp_path)
  token = panel.pair()
  mark_id = panel.json("/api/mark", {"reason": "curva"}, token)[1]["mark"]["id"]
  panel.json("/api/share", {"id": mark_id}, token)
  panel.vs = VehicleState(onroad=True)
  assert panel.backend.share_step(network_ok=True) == "nada"


def test_share_off_says_where_to_turn_it_on(panel, tmp_path):
  _sharing_panel(panel, tmp_path, enabled=False)
  token = panel.pair()
  mark_id = panel.json("/api/mark", {"reason": "curva"}, token)[1]["mark"]["id"]
  status, body = panel.json("/api/share", {"id": mark_id}, token)
  assert status == 409 and "Ajustes" in body["error"]


def test_share_cancel_and_forget(panel, tmp_path):
  _sharing_panel(panel, tmp_path)
  token = panel.pair()
  mark_id = panel.json("/api/mark", {"reason": "curva"}, token)[1]["mark"]["id"]
  panel.json("/api/share", {"id": mark_id}, token)
  assert panel.json("/api/share/cancel", {"id": mark_id}, token) == (200, {"ok": True})
  assert panel.json("/api/share", token=token)[1]["items"] == []
  panel.json("/api/share", {"id": mark_id}, token)
  assert panel.json("/api/share/forget", {}, token) == (200, {"ok": True})
  assert panel.json("/api/share", token=token)[1]["items"] == []


def test_a_comma_without_sharing_answers_plainly(panel):
  token = panel.pair()
  assert panel.json("/api/share", token=token) == (200, {"enabled": False, "receiver": False, "items": [], "forgetting": False, "sent": 0})
  assert panel.json("/api/share", {"id": "x"}, token)[0] == 404


# *** the route from the phone (selfdrive/hexa/nav.py) ***

def _nav_panel(panel, results=None):
  from openpilot.selfdrive.hexa import nav
  urls = []

  def get(url):
    urls.append(url)
    return results if results is not None else [{"lat": "-25.4429", "lon": "-49.2397", "display_name": "Jardim Botânico, Curitiba"}]
  panel.backend.navigator = nav.Navigator(panel.store, get_json=get, mono=lambda: panel.now, position=lambda: (-25.442912, -49.239713))
  panel.store.put("NavigationEnabled", True)
  return urls


def test_nav_needs_a_paired_phone(panel):
  _nav_panel(panel)
  assert panel.json("/api/nav")[0] == 401
  assert panel.json("/api/nav/go", {"latitude": -22.9, "longitude": -43.1})[0] == 401


def test_nav_search_go_save_and_stop(panel):
  urls = _nav_panel(panel)
  token = panel.pair()
  status, body = panel.json("/api/nav/search", {"q": "jardim botânico"}, token)
  assert status == 200 and body["results"][0]["name"] == "Jardim Botânico, Curitiba" and len(urls) == 1
  place = body["results"][0]
  assert panel.json("/api/nav/go", {**place}, token)[0] == 200
  state = panel.json("/api/nav", token=token)[1]
  assert state["enabled"] and state["destination"]["place_name"] == "Jardim Botânico, Curitiba" and state["route"] == {"active": False}
  assert state["near"] == [-25.4429, -49.2397]  # the car on the phone's Mapa, to 10 m
  assert panel.store.get("HexaWebAudit")[-1]["event"] == "nav_go" and "Jardim" not in str(panel.store.get("HexaWebAudit")[-1])
  assert panel.json("/api/nav/place", {**place, "name": "Praia"}, token)[1]["places"][0]["name"] == "Praia"
  assert panel.json("/api/nav/stop", {}, token) == (200, {"ok": True})
  assert panel.json("/api/nav", token=token)[1]["destination"] is None
  assert panel.json("/api/nav/place/remove", {"name": "praia"}, token)[1]["places"] == []


def test_nav_go_while_driving_is_allowed_it_doesnt_change_the_driving(panel):
  _nav_panel(panel)
  token = panel.pair()
  panel.vs = VehicleState(onroad=True, parked=False, standstill=False, engaged=True)
  assert panel.json("/api/nav/go", {"latitude": -22.9, "longitude": -43.1, "name": "x"}, token)[0] == 200
  assert panel.json("/api/nav/token", {"token": "pk." + "a" * 30}, token)[0] == 409  # the key only with the car off


def test_nav_errors_are_the_phones(panel):
  _nav_panel(panel)
  token = panel.pair()
  panel.store.put("NavigationEnabled", False)
  status, body = panel.json("/api/nav/go", {"latitude": -22.9, "longitude": -43.1}, token)
  assert status == 400 and "navegação" in body["error"]
  assert panel.json("/api/nav/search", {"q": "a"}, token)[0] == 400
# *** the road's limit from the map (selfdrive/hexa/speed_limit.py) ***

def _limit_panel(panel, installed=True, install=None, wifi=True, free=50 * 1024 ** 3, download=None):
  from openpilot.selfdrive.hexa import speed_limit
  sent = []
  panel.limit = {"wifi": wifi, "free": free, "download": download}
  panel.backend._limit_state = lambda: speed_limit.view(None, 1e9, speed_limit.option(panel.store), installed, panel.limit["download"])
  panel.backend._send_mapd = sent.append
  panel.backend._network_ok = lambda: panel.limit["wifi"]
  panel.backend._free_bytes = lambda: panel.limit["free"]
  panel.backend._spawn = lambda job: job()
  if install is not None:
    panel.backend._install_mapd = install
  panel.store.put("HexaSpeedLimit", 1)
  return sent


def test_limit_needs_a_paired_phone(panel):
  _limit_panel(panel)
  assert panel.json("/api/limit")[0] == 401
  assert panel.json("/api/limit/install", {})[0] == 401


def test_limit_state_and_download(panel):
  sent = _limit_panel(panel)
  token = panel.pair()
  state = panel.json("/api/limit", token=token)[1]
  assert state["option"] == 1 and state["installed"] and {a["key"] for a in state["areas"]} == {"br_state.RJ", "nation.BR"}
  assert panel.json("/api/limit/download", {"area": "br_state.RJ"}, token) == (200, {"ok": True})
  assert sent[-1].mapdIn.str == "br_state.RJ"
  assert panel.json("/api/limit/download", {"area": "nation.US"}, token)[0] == 400
  panel.vs = VehicleState(onroad=True)
  assert panel.json("/api/limit/download", {"area": "br_state.RJ"}, token)[0] == 409  # parked only


def test_limit_download_needs_the_option_and_mapd(panel):
  _limit_panel(panel, installed=False)
  token = panel.pair()
  assert panel.json("/api/limit/download", {"area": "br_state.RJ"}, token)[0] == 409


def test_limit_install(panel):
  from openpilot.selfdrive.hexa import speed_limit
  calls = []
  _limit_panel(panel, installed=False, install=lambda: calls.append(1) or "abc")
  token = panel.pair()
  assert panel.json("/api/limit/install", {}, token)[1]["busy"] is True
  assert calls == [1] and panel.json("/api/limit", token=token)[1]["job"] == {"busy": False, "error": "", "ok": True}

  def no_pin():
    raise speed_limit.InstallError("Esta versão ainda não traz o mapd: falta a revisão do CTO.")
  panel.backend._install_mapd = no_pin
  panel.json("/api/limit/install", {}, token)
  assert "falta a revisão" in panel.json("/api/limit", token=token)[1]["job"]["error"]
  panel.vs = VehicleState(onroad=True)
  assert panel.json("/api/limit/install", {}, token)[0] == 409


def test_limit_downloads_only_on_wifi_with_room(panel):
  sent = _limit_panel(panel, wifi=False)
  token = panel.pair()
  status, body = panel.json("/api/limit/download", {"area": "br_state.RJ"}, token)
  assert status == 409 and "Wi-Fi" in body["error"] and sent == []
  status, body = panel.json("/api/limit/install", {}, token)
  assert status == 409 and "Wi-Fi" in body["error"]
  panel.limit.update(wifi=True, free=5 * 1024 ** 3)  # room for Rio, not for Brazil
  assert panel.json("/api/limit/download", {"area": "br_state.RJ"}, token)[0] == 200
  status, body = panel.json("/api/limit/download", {"area": "nation.BR"}, token)
  assert status == 409 and "espaço" in body["error"] and len(sent) == 1
  panel.limit["free"] = 10 * 1024 ** 2
  assert "espaço" in panel.json("/api/limit/install", {}, token)[1]["error"]


def test_the_car_turning_on_stops_a_map_download(panel):
  sent = _limit_panel(panel, download={"active": True, "done": 1, "total": 6})
  assert panel.backend.limit_tick() is False and sent == []  # parked: it goes on
  panel.vs = VehicleState(onroad=True)
  assert panel.backend.limit_tick() is True
  assert str(sent[-1].mapdIn.type) == "cancelDownload"
  assert panel.backend.limit_tick() is False and len(sent) == 1  # asked once per start
  panel.limit["download"] = None
  panel.vs = VehicleState(onroad=False)
  assert panel.backend.limit_tick() is False and len(sent) == 1


def test_the_panel_never_puts_mapd_in_its_submaster():
  src = (Path(hexaweb.__file__)).read_text()
  sub = src.split("sm = messaging.SubMaster(")[1].split("])")[0]
  assert "mapd" not in sub
  assert 'messaging.sub_sock(s, conflate=True) for s in ("mapdOut", "mapdExtendedOut")' in src


# *** the errors card: "Enviar para análise" straight to the receiver (selfdrive/hexa/report.py, ERR-01) ***

def _errors_panel(panel, tmp_path, brand="toyota"):
  from opendbc.car.structs import car
  from openpilot.selfdrive.hexa import share
  sent = []

  def post(url, body, hdrs):
    sent.append((url.rsplit("/v1/", 1)[1], body))
    return 201, {"id": "r1"} if url.endswith("/v1/report") else {}
  panel.backend.sharer = share.Sharer(panel.store, str(tmp_path / "realdata"), url="https://r.example", post=post, clock=lambda: panel.now)
  panel.now = 1_793_000_000.
  panel.store.put("CarParamsPersistent", car.CarParams.new_message(brand=brand, carFingerprint="TOYOTA_RAV4", carVin="2T3ZFREV4JW123456").to_bytes())
  panel.store.put("DongleId", b"0123456789abcdef")
  panel.backend._health_state = lambda: {"errors": [
    {"t": panel.now - 60, "process": "uploader", "msg": "", "error": "FileNotFoundError: /data/media/0/realdata/0000000a--47fe527e33--43/qlog"},
    {"t": 1_790_000_000, "process": "athenad", "msg": "Failed to fetch pairing provider: System time is not valid", "error": ""},
    {"t": panel.now - 30, "process": "selfdrived", "msg": "dongle 0123456789abcdef", "error": "ValueError: boom"}],
    "down": [], "device": {"temp": 51}}
  return sent


def test_the_errors_card_says_which_are_normal_and_which_date_is_false(panel, tmp_path):
  _errors_panel(panel, tmp_path)
  token = panel.pair()
  h = panel.json("/api/health", token=token)[1]
  assert [(e["known"], e["clock_ok"]) for e in h["errors"]] == [("limpeza", True), ("relogio", False), (None, True)]
  assert h["reports"] == {"sent": 0, "receipt": ""}


def test_errors_preview_is_what_goes_and_send_returns_the_code(panel, tmp_path):
  sent = _errors_panel(panel, tmp_path)
  token = panel.pair()
  assert panel.json("/api/errors/preview", {})[0] == 401
  status, rep = panel.json("/api/errors/preview", {}, token)
  assert status == 200 and sent == []  # the preview sends nothing
  assert rep["car"] == {"brand": "toyota", "model": "TOYOTA_RAV4"}  # every car (ERR-01)
  assert "0123456789abcdef" not in json.dumps(rep) and "2T3ZFREV4JW123456" not in json.dumps(rep) and "realdata" not in json.dumps(rep)
  assert panel.json("/api/errors/send", {}, token) == (200, {"ok": True, "receipt": "r1"})
  assert [p for p, _ in sent] == ["register", "report"]
  assert json.loads(sent[-1][1]) == rep  # exactly what the phone showed
  assert panel.store.get(phone.AUDIT_PARAM)[-1]["event"] == "errors_sent"
  assert panel.json("/api/health", token=token)[1]["reports"] == {"sent": 1, "receipt": "r1"}


def test_errors_send_can_be_kept_to_the_haval(panel, tmp_path, monkeypatch):
  from openpilot.selfdrive.hexa import report
  sent = _errors_panel(panel, tmp_path)
  monkeypatch.setattr(report, "BRANDS", ("gwm",))
  token = panel.pair()
  status, body = panel.json("/api/errors/send", {}, token)
  assert status == 409 and "Haval" in body["error"] and sent == []


def test_errors_send_says_why_it_failed(panel, tmp_path):
  _errors_panel(panel, tmp_path)
  panel.backend.sharer.post = panel.backend.sharer.report_post = lambda url, body, hdrs: (503, {})
  token = panel.pair()
  status, body = panel.json("/api/errors/send", {}, token)
  assert status == 502 and "não respondeu" in body["error"]
