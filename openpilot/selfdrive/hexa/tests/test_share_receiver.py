"""The comma's side (share.py) against the receiver itself (tools/lizpilot/receiver/handler.ts), over HTTP.

Runs the receiver's dev_server.ts with Node (22.6 or newer, for TypeScript); skipped where there is none.
"""
import json
import shutil
import subprocess
import urllib.request
from pathlib import Path

import pytest

from openpilot.selfdrive.hexa import report, share
from openpilot.selfdrive.hexa.tests.test_share import DONGLE, MARK, NS, SERIAL, T0, VIN, World, decode, write_segments

RECEIVER = Path(__file__).resolve().parents[4] / "tools/lizpilot/receiver"


def node_ok() -> bool:
  node = shutil.which("node")
  if node is None or not (RECEIVER / "dev_server.ts").is_file():
    return False
  out = subprocess.run([node, "--version"], capture_output=True, text=True)
  major, minor = (int(x) for x in out.stdout.strip().lstrip("v").split(".")[:2])
  return (major, minor) >= (22, 6)


@pytest.fixture
def receiver():
  if not node_ok():
    pytest.skip("no Node 22.6+ here")
  import os
  env = dict(os.environ, LIZPILOT_BUILD_SECRETS=share.BUILD_SECRET)
  p = subprocess.Popen(["node", "--experimental-strip-types", "--no-warnings", str(RECEIVER / "dev_server.ts"), "0"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
  line = p.stdout.readline()
  assert line.startswith("listening "), p.stderr.read()
  url = f"http://127.0.0.1:{int(line.split()[1])}"
  yield url
  p.terminate()
  p.wait(timeout=10)


def held(url) -> dict:
  with urllib.request.urlopen(url + "/_dev/state", timeout=5) as r:
    return json.loads(r.read())


def test_a_real_cut_reaches_the_receiver_and_can_be_forgotten(tmp_path, receiver):
  write_segments(tmp_path / "realdata")
  w = World(tmp_path)
  w.sharer = share.Sharer(w.store, str(tmp_path / "realdata"), url=receiver, share_dir=tmp_path / "share", send_jitter_s=0.)  # real clock, real HTTP
  mark = {**MARK, "t": 10_000 + 60 + 30, "mono_ns": T0 + 90 * NS, "delay_s": 0.}
  assert w.sharer.enqueue(mark)[0] == 200
  assert w.sharer.step(True, True, [mark]) == "cortado"
  assert w.sharer.step(True, True, [mark]) == "enviado"
  state = held(receiver)
  dev = w.store.get(share.DEVICE_PARAM)["id"]
  assert state["devices"] == [dev] and dev != DONGLE
  row = state["shares"][0]
  assert row["mark_id"] == mark["id"] and row["brand"] == "gwm" and row["model"] == "GWM_HAVAL_H6"
  assert row["manifest"]["cut"]["vin_checked"] and row["manifest"]["cut"]["can_addresses_with_vin"] == ["0:0x3e1"]
  text = json.dumps(state)
  for secret in (VIN, DONGLE, SERIAL, "celular do dono"):
    assert secret not in text
  assert w.sharer.state()["items"][0]["state"] == "enviado"

  # sent twice is kept once
  w.store.put(share.QUEUE_PARAM, [])
  w.sharer.enqueue(mark)
  w.sharer.step(True, True, [mark])
  assert w.sharer.step(True, True, [mark]) == "enviado" and len(held(receiver)["shares"]) == 1

  w.sharer.forget()
  assert w.sharer.step(True, True, [mark]) == "apagado"
  assert held(receiver) == {"devices": [], "shares": [], "reports": []}


def test_a_comma_with_its_clock_off_still_gets_through(tmp_path, receiver):
  w = World(tmp_path)
  w.sharer = share.Sharer(w.store, str(tmp_path / "realdata"), url=receiver, share_dir=tmp_path / "share", send_jitter_s=0.,
                          clock=lambda: share.wall_time() - 7200, cutter=w.cutter)
  w.sharer.enqueue(MARK)
  w.sharer.step(True, True, [MARK])
  assert w.sharer.step(True, True, [MARK]) == "enviado"  # two hours off: the receiver says its time and the comma signs again
  assert abs(w.sharer.skew - 7200) < 5
  assert len(held(receiver)["devices"]) == 1


def test_the_package_on_the_receiver_is_the_comma_s(tmp_path, receiver):
  write_segments(tmp_path / "realdata", minutes=2)
  w = World(tmp_path)
  got = {}

  def post(url, body, hdrs):
    got[url.rsplit("/v1/", 1)[1]] = body
    return share.http_post(url, body, hdrs)
  w.sharer = share.Sharer(w.store, str(tmp_path / "realdata"), url=receiver, post=post, share_dir=tmp_path / "share", send_jitter_s=0.)
  mark = {**MARK, "t": 10_000 + 60 + 30, "mono_ns": T0 + 90 * NS, "delay_s": 0.}
  w.sharer.enqueue(mark)
  w.sharer.step(True, True, [mark])
  w.sharer.step(True, True, [mark])
  m, payload = share.unpack(got["share"])
  _, evs = decode(payload)
  assert held(receiver)["shares"][0]["bytes"] == len(got["share"])
  assert {e.which() for e in evs} <= share.SERVICES and m["mark"]["id"] == mark["id"]


def test_the_reader_lists_and_downloads_what_arrived(tmp_path, receiver):
  """envios.py against the receiver itself, with the reader word: the package comes back as the comma packed it."""
  import importlib.util
  write_segments(tmp_path / "realdata", minutes=2)
  w = World(tmp_path)
  w.sharer = share.Sharer(w.store, str(tmp_path / "realdata"), url=receiver, share_dir=tmp_path / "share", send_jitter_s=0.)
  mark = {**MARK, "t": 10_000 + 60 + 30, "mono_ns": T0 + 90 * NS, "delay_s": 0.}
  w.sharer.enqueue(mark, with_note=True)
  w.sharer.step(True, True, [mark])
  assert w.sharer.step(True, True, [mark]) == "enviado"
  spec = importlib.util.spec_from_file_location("envios", RECEIVER.parent / "envios.py")
  envios = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(envios)
  rows = envios.rows(receiver, "dev-reader", brand="gwm")
  assert len(rows) == 1 and "device" not in rows[0] and rows[0]["note"] == "na serra"
  dest = envios.download(receiver, "dev-reader", rows[0], tmp_path / "out")
  assert json.loads((dest / "manifest.json").read_text())["mark"]["id"] == mark["id"]
  try:
    envios.rows(receiver, "wrong")
  except Exception as e:
    assert "403" in str(e)
  else:
    raise AssertionError("read without the word")


def test_an_error_report_reaches_the_receiver_clean_and_can_be_forgotten(tmp_path, receiver):
  from openpilot.selfdrive.hexa.tests.test_report import PRINTS, health
  w = World(tmp_path)
  w.sharer = share.Sharer(w.store, str(tmp_path / "realdata"), url=receiver, share_dir=tmp_path / "share")  # real clock, real HTTP
  data = report.encode(report.build(w.store, health(PRINTS), "h6gt"))
  status, out = w.sharer.report(data)
  assert status == 200 and out["receipt"].startswith("dev-")
  state = held(receiver)
  row = state["reports"][0]
  assert row["id"] == out["receipt"] and row["brand"] == "gwm" and row["errors"] == len(PRINTS)
  text = json.dumps(state["reports"])
  for secret in (VIN, DONGLE, SERIAL, "realdata", "47fe527e33", "-25.4429"):
    assert secret not in text
  w.sharer.forget()
  assert w.sharer.step(True, True, []) == "apagado"
  assert held(receiver) == {"devices": [], "shares": [], "reports": []}
