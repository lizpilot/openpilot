"""The error report (report.py, ERR-01): what goes when the owner taps "Enviar para análise", and what never does."""
import json
from datetime import datetime, UTC

import pytest

from openpilot.selfdrive.hexa import report, share
from openpilot.selfdrive.hexa.tests.test_share import DONGLE, SERIAL, VIN, World

NOW = 1_793_000_000.  # 2026-10-26
ROUTE = "0000000a--47fe527e33"

# the owner's prints of 05/11 [carro], with the identifiers a real log line can carry around them
PRINTS = [
  {"t": NOW - 120, "process": "uploader", "msg": "uploader failed",
   "error": f"FileNotFoundError: [Errno 2] No such file or directory: '/data/media/0/realdata/{ROUTE}--43/qlog.zst'"},
  {"t": NOW - 60, "process": "athenad", "msg": f"athenad.ws_recv {DONGLE} wss://athena.comma.ai/ws/v2/{DONGLE}?token=abc",
   "error": "websocket._exceptions.WebSocketConnectionClosedException: socket is already closed."},
  {"t": NOW - 30, "process": "athenad", "msg": "Connection to remote host was lost.", "error": ""},
  {"t": NOW - 25 * 86400, "process": "athenad", "msg": "athenad.main",
   "error": "ssl.SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate is not yet valid"},
  {"t": NOW - 25 * 86400 + 5, "process": "manage_athenad", "msg": "Failed to fetch pairing provider: System time is not valid", "error": ""},
  {"t": NOW - 25 * 86400 + 40, "process": "controlsd", "msg": "something real right after boot", "error": "ValueError: bad"},
  {"t": NOW - 10, "process": "selfdrived", "msg": f"car {VIN} at -25.44291, -49.23972 from 192.168.1.23 by dono@example.com serial {SERIAL}",
   "error": f"RuntimeError: route {ROUTE}--12 in /data/media/0/realdata/{ROUTE}--12"},
]


def health(errors=PRINTS):
  return {"errors": errors, "last_off": {"t": int(NOW - 300), "why": ["steerOverride"]}, "down": ["mapd"], "device": {"temp": 50},
          "learned": {"torque": {"factor": 2.1}}, "v12": 12.4}


@pytest.mark.parametrize("text, expected", [
  (f"/data/media/0/realdata/{ROUTE}--43/qlog.zst", "<rota>"),
  (f"route {ROUTE}--12 done", "route <rota> done"),
  ("2026-10-12--04-36-11--3", "<rota>"),
  (f"dongle {DONGLE}", "dongle <id>"),
  (f"vin {VIN}.", "vin <vin>."),
  ("at -25.44291, -49.23972 now", "at <posição> now"),
  ("from 192.168.1.23:443", "from <ip>:443"),
  ("mail dono@example.com", "mail <email>"),
  ("wss://athena.comma.ai/ws/v2/abc?token=xyz closed", "wss://athena.comma.ai/… closed"),
  ("line\nbreak\x1b[31m", "line break [31m"),
  ("FileNotFoundError: [Errno 2] No such file", "FileNotFoundError: [Errno 2] No such file"),
  ("x" * 500, "x" * report.TEXT_MAX),
  ("a" * 500, "<id>"),  # a long hex is an id
])
def test_clean(text, expected):
  assert report.clean(text) == expected


def test_clean_takes_the_comma_own_ids_by_value():
  assert report.clean(f"serial {SERIAL.upper()} here", hide=[SERIAL]) == "serial <id> here"


def test_the_prints_are_known_and_harmless():
  codes = [report.known(e) for e in PRINTS]
  assert codes == ["limpeza", "conexao", "conexao", "relogio", "relogio", None, None]
  assert report.known({"process": "controlsd", "msg": "", "error": "FileNotFoundError: x"}) is None  # only the uploader's


def test_an_error_before_the_clock_was_set_is_marked():
  out = report.annotate(PRINTS, report.PLAUSIBLE_AFTER, NOW)
  assert [e["clock_ok"] for e in out] == [True, True, True, False, False, False, True]  # the boot's errors share its wrong clock
  assert report.annotate([{"t": 1_600_000_000}], report.PLAUSIBLE_AFTER, NOW)[0]["clock_ok"] is False  # 2020
  assert report.annotate([{"t": NOW + 3 * 86400}], report.PLAUSIBLE_AFTER, NOW)[0]["clock_ok"] is False  # ahead


def test_the_install_date_raises_the_floor():
  w_store = {"InstallDate": datetime.fromtimestamp(NOW - 86400, tz=UTC)}

  class S(dict):
    def get(self, k):
      return dict.get(self, k)
  assert report.clock_floor(S(w_store), NOW) == pytest.approx(NOW - 86400)
  assert report.clock_floor(S(InstallDate=datetime.fromtimestamp(NOW + 86400, tz=UTC)), NOW) == report.PLAUSIBLE_AFTER  # set by a wrong clock
  assert report.clock_floor(S(InstallDate=b"2026-10-01T00:00:00"), NOW) > report.PLAUSIBLE_AFTER
  assert report.clock_floor(S(InstallDate="junk"), NOW) == report.PLAUSIBLE_AFTER


def test_the_package_has_no_dongle_serial_vin_gps_nor_route(tmp_path):
  w = World(tmp_path)
  rep = report.build(w.store, health(), "h6gt", NOW)
  data = report.encode(rep)
  text = data.decode()
  for secret in (DONGLE, SERIAL, SERIAL.upper(), VIN, ROUTE, "realdata", "-25.4429", "-49.2397", "192.168", "dono@example.com", "token="):
    assert secret not in text, secret
  report.check(w.store, data)  # and the last look agrees
  assert rep["software"] == {"edition": "h6gt", "branch": "h6gt", "commit": "0123456789ab", "version": "0.10.1"}
  assert rep["car"] == {"brand": "gwm", "model": "GWM_HAVAL_H6"}
  assert rep["down"] == ["mapd"] and rep["last_off"] == {"ago_min": 5, "why": ["steerOverride"]}
  assert set(rep) == {"format", "software", "car", "errors", "last_off", "down"}  # no device, temperature, learning or battery
  e = rep["errors"]
  assert [x["ago_min"] for x in e] == [2, 1, 0, None, None, None, 0]  # the false dates don't go
  assert [x["known"] for x in e][:5] == ["limpeza", "conexao", "conexao", "relogio", "relogio"]
  assert all("t" not in x for x in e)
  assert e[0]["error"] == "FileNotFoundError: [Errno 2] No such file or directory: '<rota>'"


def test_check_refuses_what_still_has_an_id(tmp_path):
  w = World(tmp_path)
  with pytest.raises(share.ShareError):
    report.check(w.store, json.dumps({"x": f"..{DONGLE.upper()}.."}).encode())
  with pytest.raises(share.ShareError):
    report.check(w.store, json.dumps({"x": VIN}).encode())


def test_the_report_is_capped(tmp_path):
  w = World(tmp_path)
  many = [{"t": NOW - i, "process": "p" * 99, "msg": "m" * 999, "error": "e" * 999} for i in range(50)]
  rep = report.build(w.store, health(many), "h6gt", NOW)
  assert len(rep["errors"]) == report.REPORT_ERRORS and len(rep["errors"][0]["process"]) == 40
  assert len(report.encode(rep)) < share.REPORT_MAX_BYTES


# *** sending: Sharer.report ***

def reporting(w):
  got = []
  base = w.receiver

  def post(url, body, hdrs):
    status, out = base(url, body, hdrs)
    if url.endswith("/v1/report") and status == 200:
      got.append((hdrs[share.DEVICE_HEADER], json.loads(body)))
      return 201, {"id": f"r{len(got)}"}
    return status, out
  w.sharer.post = w.sharer.report_post = post
  return got


def test_a_report_is_registered_signed_and_sent_at_once(tmp_path):
  w = World(tmp_path)
  w.store.put(share.ENABLED_PARAM, False)  # the clips' option is not needed: the tap and the confirmation are
  got = reporting(w)
  data = report.encode(report.build(w.store, health(), "h6gt", NOW))
  assert w.sharer.report(data) == (200, {"ok": True, "receipt": "r1"})
  dev = w.store.get(share.DEVICE_PARAM)
  assert w.receiver.calls == ["/v1/register", "/v1/report"] and got[0][0] == dev["id"] != DONGLE
  assert dev["registered"] and dev["used"]
  assert w.sharer.reports() == [{"t": int(w.t), "receipt": "r1"}]


def test_reports_have_a_daily_limit(tmp_path):
  w = World(tmp_path)
  reporting(w)
  for _ in range(share.REPORTS_PER_DAY):
    assert w.sharer.report(b"{}")[0] == 200
  assert w.sharer.report(b"{}")[0] == 429
  w.t += 86400
  assert w.sharer.report(b"{}")[0] == 200


def test_a_report_fails_cleanly(tmp_path):
  w = World(tmp_path)
  reporting(w)
  assert w.sharer.report(b"x" * (share.REPORT_MAX_BYTES + 1))[0] == 413
  w.receiver.answer = 503
  status, out = w.sharer.report(b"{}")
  assert status == 502 and "não respondeu" in out["error"] and w.sharer.reports() == []
  w.sharer.url = ""
  assert w.sharer.report(b"{}")[0] == 503


def test_forget_takes_the_reports_too(tmp_path):
  w = World(tmp_path)
  reporting(w)
  w.sharer.report(b"{}")
  w.sharer.forget()
  assert w.sharer.reports() == [] and w.store.get(share.DEVICE_PARAM)["forget"]
  assert w.sharer.report(b"{}")[0] == 409  # not under the old key while it is being deleted
  assert w.sharer.step(True, True, []) == "apagado"
  assert w.receiver.devices == {}
