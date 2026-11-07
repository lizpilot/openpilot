"""The phone panel's decisions (static/lp-logic.js), run in node: what the band says, what the comma card claims,
the speed bump messages and the passenger question. Skipped where node isn't installed (the comma itself)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

LOGIC = Path(__file__).parents[1] / "static" / "lp-logic.js"
NODE = shutil.which("node") or next((p for p in ("/opt/node22/bin/node", "/usr/local/bin/node") if Path(p).exists()), None)
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def run(fn: str, *args):
  script = f"const L = require({json.dumps(str(LOGIC))}); process.stdout.write(JSON.stringify(L[{json.dumps(fn)}](...{json.dumps(args)})));"
  out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
  return json.loads(out)


T0 = 1_000_000
NOW = {"online": True, "age": 500, "now": T0}
DRIVING = {"driving": True, "v": 62, "steering": True, "engaged": True, "driver": False, "acc": True, "acc_active": True, "acc_set": 70,
           "alert": "", "alert_status": "normal", "why": []}


def live(**kw):
  return {**DRIVING, **kw}


def at(ms):
  return {**NOW, "now": T0 + ms}


# ----- the band

def test_openpilot_driving_with_the_acc_in_control():
  b = run("band", live(), NOW)
  assert b["cls"] == "op" and b["who"] == "openpilot dirigindo" and b["acc"] == 70


def test_acc_number_only_with_the_acc_in_control():
  assert run("band", live(acc_active=False), NOW)["acc"] is None
  assert run("band", live(acc_active=None), NOW)["acc"] is None
  assert run("band", live(acc_set=None), NOW)["acc"] is None
  assert run("band", live(steering=False), NOW)["acc"] == 70  # the driver steering with the ACC on


def test_steering_only_needs_2_s_moving():
  first = run("band", live(acc_active=False), NOW)
  assert first["cls"] == "op" and first["who"] == "openpilot na direção" and first["latSince"] == T0  # foot on the gas: not yet
  assert run("band", live(acc_active=False), at(1500), first["latSince"])["cls"] == "op"
  b = run("band", live(acc_active=False), at(2000), first["latSince"])
  assert b["cls"] == "lat" and b["who"] == "só direção · a velocidade é sua" and b["sub"] == "ACC desligado" and b["acc"] is None
  back = run("band", live(), at(2500), b["latSince"])  # the ACC back in control resets it
  assert back["cls"] == "op" and back["latSince"] is None


def test_stopped_says_nothing_about_the_acc():
  # [log] engaged with acc_active false is mostly stopped behind a car, held by the ACC (state 2)
  b = run("band", live(v=0, acc_active=False), at(10_000), T0)
  assert b["cls"] == "op" and b["who"] == "openpilot na direção" and b["acc"] is None and b["latSince"] is None
  assert run("band", live(v=0), NOW)["who"] == "openpilot na direção"


def test_unknown_acc_state_is_never_claimed():
  assert run("band", live(acc_active=None), NOW)["who"] == "openpilot na direção"


def test_alert_is_amber_and_critical_is_red():
  assert run("band", live(alert="Steer Unavailable", alert_status="userPrompt"), NOW)["cls"] == "warn"
  b = run("band", live(alert="TAKE CONTROL IMMEDIATELY", alert_status="critical"), NOW)
  assert b["cls"] == "fail" and b["who"] == "assuma o volante" and b["sub"] == ""  # the alert below says why
  # an older comma without the status: any alert text is at least amber, never green
  assert run("band", live(alert="TAKE CONTROL IMMEDIATELY", alert_status=None), NOW)["cls"] == "warn"
  assert run("band", live(alert="Pre-Enable", alert_status="normal"), NOW)["cls"] == "op"


def test_no_connection_or_old_data_says_nothing_about_who_drives():
  for link in ({"online": False, "age": 500, "now": T0}, {"online": True, "age": 5000, "now": T0}):
    b = run("band", live(), link)
    assert b["cls"] == "none" and b["who"] == "sem dados" and b["stale"] and b["acc"] is None
  assert run("band", None, NOW)["cls"] == "none"


def test_you_driving_and_why_it_does_not_engage():
  b = run("band", live(steering=False, engaged=False, why=["pedal pisado"]), NOW)
  assert b["cls"] == "you" and b["who"] == "você dirigindo" and b["sub"] == "não engata: {motivos}" and b["why"] == ["pedal pisado"]
  assert run("band", {"driving": False}, NOW)["who"] == "carro desligado"


def test_the_band_says_what_the_driver_took():
  assert run("band", live(acc_active=True, v=60, gas=True), NOW)["sub"] == "você no acelerador"
  assert run("band", live(acc_active=True, v=60, brake=True), NOW)["sub"] == "você no freio"
  assert run("band", live(acc_active=True, v=60, driver=True), NOW)["sub"] == "você no volante"
  assert run("band", live(acc_active=True, v=60, brake=True, gas=True), NOW)["sub"] == "você no freio"
  assert run("band", live(acc_active=True, v=60), NOW)["sub"] == "volante ativo"
  assert run("band", live(steering=False, gas=True), NOW)["sub"] == "openpilot desligado"  # nothing to take from


# ----- the comma card, car off

HEALTH = {"device": {"temp": 48, "thermal": "ok", "free": 57}, "down": [], "learned": {}, "commit": "abc1234"}


def test_comma_ok_says_what_it_checked_and_never_that_the_car_is_ready():
  c = run("commaChecks", True, HEALTH)
  assert c["title"] == "Comma ok" and c["cls"] == "ok" and "câmeras" in c["sub"].lower()
  assert "pronto" not in json.dumps(c, ensure_ascii=False).lower()
  info = [i for i in c["items"] if i["ok"] is None]
  assert {i["text"] for i in info} == {"Versão {versao}", "Aprendizado aparece depois de dirigir"}  # not checks


def test_comma_temperature_follows_the_status_of_the_comma():
  for thermal, ok in (("ok", True), ("overheated", False), ("critical", False)):
    c = run("commaChecks", True, {**HEALTH, "device": {"temp": 69, "thermal": thermal, "free": 57}})
    assert next(i["ok"] for i in c["items"] if "{graus}" in i["text"]) is ok


def test_comma_problems():
  c = run("commaChecks", True, {**HEALTH, "down": ["modeld"], "device": {"temp": 80, "thermal": "overheated", "free": 4}})
  assert c["title"] == "Atenção no comma" and c["cls"] == "att"
  assert [i["ok"] for i in c["items"] if i["ok"] is not None] == [False, False, False]
  assert run("commaChecks", False, HEALTH)["title"] == "Sem dados do comma"
  assert run("commaChecks", True, None)["items"] == []


# ----- lane text

def test_centimeters_only_with_both_lines():
  assert run("laneText", {"left": -1.2, "right": 1.86, "left_ok": True, "right_ok": True}) == {"text": "Na faixa · {cm} cm à esquerda", "vars": {"cm": 33}}
  assert run("laneText", {"left": -1.2, "right": 1.86, "left_ok": True, "right_ok": False})["text"] == "Só a linha da esquerda está visível"
  assert run("laneText", {"left": -1.5, "right": 1.52, "left_ok": True, "right_ok": True})["text"] == "No centro da faixa"


def test_each_option_says_when_it_applies():
  assert run("applies", {"restart_needed": True}) == "Vale na próxima partida do carro."
  assert run("applies", {"restart_needed": False}) == "Vale no próximo engate."


# ----- speed bump

def test_bump_offers_to_keep_the_speed_it_was_passed_at():
  c = run("speedChoices", "lombada", 22, None)
  assert [x["v"] for x in c] == [None, 22, 10, 15, 20, 25, 30, 40, 50] and c[1]["label"] == "Manter {n}"
  assert [x["v"] for x in run("speedChoices", "lombada", 90, 35)] == [None, 10, 15, 20, 25, 30, 35, 40, 50]  # 90 is not a bump speed
  assert [x["v"] for x in run("speedChoices", "radar", 50, None)][-1] == 120
  assert run("keptSpeed", "lombada", 22) == 22 and run("keptSpeed", "lombada", 3) is None and run("keptSpeed", "radar", 22) is None


ON = {"alert": True, "slowdown": False, "long": False, "calibrated": False}


def test_bump_message_by_speed_never_says_the_car_slows_where_nothing_does():
  assert run("bumpMessage", 40, ON) == "Hoje só avisa. Ligue reduzir na lombada em Ajustes."
  assert run("bumpMessage", 30, {**ON, "slowdown": True}) == "Em teste: o comma toca o − do ACC até essa velocidade."
  assert run("bumpMessage", 20, ON).startswith("O ACC da Haval não vai abaixo de 30 km/h.")
  assert run("bumpMessage", 20, {**ON, "slowdown": True}).startswith("O ACC da Haval não vai abaixo de 30 km/h.")
  assert run("bumpMessage", 20, {**ON, "long": True}).startswith("O longitudinal do comma ainda não foi testado")
  assert run("bumpMessage", 20, {**ON, "alert": False}).startswith("Com o aviso de radar e lombada desligado")
  assert run("bumpMessage", None, ON) == ""


# ----- the passenger question

def test_passenger_question():
  parked, moving, off = {"on": True, "moving": False}, {"on": True, "moving": True}, {"on": False, "moving": False}
  assert run("passenger", {"ok": False}, parked) == {"ok": False, "ask": False}  # parked: never asks
  assert run("passenger", {"ok": False}, moving) == {"ok": False, "ask": True}   # moving, not confirmed
  assert run("passenger", {"ok": True}, moving) == {"ok": True, "ask": False}    # moving, confirmed
  assert run("passenger", {"ok": True}, parked)["ok"] is True                    # stopping in traffic keeps it
  gone = run("passenger", {"ok": True}, off)                                     # turning the car off ends it
  assert gone == {"ok": False, "ask": False}
  assert run("passenger", gone, moving)["ask"] is True                           # next trip asks once again


# ----- the fixed address

def test_fixed_address_plan():
  assert run("addressPlan", "lizpilot.local", False, True) == "fixed"
  assert run("addressPlan", "LizPilot.local", True, False) == "fixed"
  assert run("addressPlan", "192.168.0.23", True, False) == "move"   # not paired: go there before the code
  assert run("addressPlan", "192.168.0.23", True, True) == "offer"   # paired by address: suggest, pairs again once
  assert run("addressPlan", "192.168.0.23", False, True) == "ip"     # the name doesn't answer here


# ----- who had the wheel

SUMMARY = {"moving_s": 31 * 60, "op_s": 23 * 60, "help_s": 3 * 60, "you_s": 5 * 60, "stop_s": 6 * 60,
           "op_km": 23.4, "help_km": 2.1, "you_km": 3.5, "km": 29.0, "acc_s": 27 * 60}


def test_trip_split_in_minutes_km_and_shares():
  t = run("tripSplit", SUMMARY)
  assert [(p["key"], p["min"], p["km"]) for p in t["parts"]] == [("op", 23, 23.4), ("help", 3, 2.1), ("you", 5, 3.5)]
  assert abs(sum(p["pct"] for p in t["parts"]) - 100) < .01
  assert t["big"] == {"text": "{m} min", "vars": {"m": 23}} and t["line"] == "de volante por conta do openpilot"
  assert t["sub"]["vars"] == {"ajudou": 3, "dirigiu": 5, "parado": 6} and "Parado" in t["sub"]["text"]
  assert t["accPct"] == 87 and t["short"]["vars"] == {"op": 23, "andando": 31}


def test_trip_split_when_you_drove_and_without_data():
  t = run("tripSplit", {**SUMMARY, "op_s": 10, "help_s": 0, "you_s": 31 * 60, "stop_s": 0, "acc_s": 0})
  assert t["line"] == "você dirigiu nesta viagem" and t["big"]["vars"] == {"m": 31} and t["accPct"] is None
  assert "Parado" not in t["sub"]["text"]
  assert run("tripSplit", None) is None and run("tripSplit", {"moving_s": 0}) is None


def test_week_total_is_a_sum_of_the_last_7_days():
  now = 1_800_000_000
  trips = [{"mtime": now - 3600, "summary": SUMMARY}, {"mtime": now - 86400, "summary": SUMMARY},
           {"mtime": now - 8 * 86400, "summary": SUMMARY}, {"mtime": now - 60, "summary": None}]
  w = run("weekTotal", trips, now)
  assert w["trips"] == 2 and abs(w["km"] - 58.0) < .01 and w["op"] == {"text": "{m} min", "vars": {"m": 46}}
  assert run("duration", 130) == {"text": "{h} h {m} min", "vars": {"h": 2, "m": 10}}
  assert run("weekTotal", [], now) is None


def test_week_shadow_adds_up_drives_that_have_it():
  now = 1_800_000_000
  sh = {"sh_on_s": 600., "sh_hard": 2, "sh_earlier": 1, "sh_weaker": 1, "sh_fcw": 0, "sh_gap_s": 300., "sh_gap_t": 200.}
  trips = [{"mtime": now - 3600, "summary": {**SUMMARY, **sh}}, {"mtime": now - 7200, "summary": {**SUMMARY, **sh, "sh_gap_s": 150., "sh_gap_t": 100.}},
           {"mtime": now - 60, "summary": SUMMARY}, {"mtime": now - 9 * 86400, "summary": {**SUMMARY, **sh}}]
  w = run("weekShadow", trips, now)
  assert (w["hard"], w["earlier"], w["weaker"], w["trips"], w["minutes"]) == (4, 2, 2, 2, 20)
  assert abs(w["gap"] - 1.5) < 1e-9  # time-weighted: 450 / 300
  assert run("weekShadow", [{"mtime": now, "summary": SUMMARY}], now) is None  # older summaries don't have it
  assert run("gapMeters", 1.45, 60) == 24


def test_week_car_takes_maxima_minima_and_rates():
  now = 1_800_000_000
  a = {**SUMMARY, "km": 20., "se_grade_up": 8.2, "se_grade_down": 4., "se_v12_min": 12.3,
       "se_decel": 3.1, "se_lat": 2.4, "se_roll": 3., "se_wheel_s": 120., "se_wheel_t": 100.}
  b = {**SUMMARY, "km": 10., "se_grade_up": 11.5, "se_grade_down": 6.5, "se_v12_min": 12.6,
       "se_decel": 2.0, "se_lat": 1.0, "se_roll": 5., "se_wheel_s": 0., "se_wheel_t": 0.}
  w = run("weekCar", [{"mtime": now - 60, "summary": a}, {"mtime": now - 120, "summary": b}, {"mtime": now - 9 * 86400, "summary": a}], now)
  assert (w["up"], w["down"], w["v12"], w["decel"], w["roll"]) == (11.5, 6.5, 12.3, 3.1, 5)
  assert abs(w["wheels"] - 1.2) < 1e-9
  assert run("weekCar", [{"mtime": now, "summary": SUMMARY}], now) is None


# ----- Viagens: totals and versions (owner's decisions on the sweep, 02/11)

def test_totals_add_every_drive_with_a_summary_and_give_rates_from_1_km():
  trips = [{"mtime": 100, "summary": {"km": 30., "moving_s": 1800, "op_s": 1500, "off": 3, "alerts": 6}},
           {"mtime": 200, "summary": {"km": 20., "moving_s": 1200, "op_s": 600, "off": 2, "alerts": 0}},
           {"mtime": 300, "summary": None}]
  t = run("totals", trips, 0)
  assert t["trips"] == 2 and t["km"] == 50. and t["opPct"] == 70 and t["off"] == 5
  assert t["per100"] == {"off": 10., "alerts": 12.}
  assert run("totals", trips, 150)["trips"] == 1
  assert run("totals", [{"mtime": 1, "summary": {"km": .4, "moving_s": 30, "op_s": 0, "off": 1}}], 0)["per100"] is None
  assert run("totals", [], 0) is None


def test_versions_newest_first_without_the_unknown():
  trips = [{"summary": {"commit": "b2"}}, {"summary": {"commit": "a1"}}, {"summary": {"commit": "b2"}}, {"summary": {}}, {"summary": None}]
  assert run("versions", trips) == ["b2", "a1"]
  assert run("versionOf", {"summary": None}) == ""


# ----- Início: the whole history, the map (owner, 02/11 01:53) and hidden drives (02:02)

SAT = 1_793_448_000  # 2026-10-31 12:00 UTC, a Saturday


def drive(end, minutes=20, **summary):
  s = {"km": 12., "moving_s": 1000, "off": 0, "alerts": 0, **summary}
  return {"id": f"r{end}", "mtime": end, "minutes": minutes, "summary": s, "hidden": False}


def test_idle_drives_and_accents():
  assert run("idle", drive(SAT, km=.1, moving_s=20)) is True
  assert run("idle", drive(SAT, km=.1, moving_s=300)) is False  # a long time crawling is a drive
  assert run("idle", {"mtime": SAT, "minutes": 1, "summary": None}) is False  # not read yet: never hidden for it
  assert run("fold", "Sábado ÇÃO") == "sabado cao"


def test_history_search_finds_day_time_version_and_marks():
  a, b = drive(SAT, commit="abc1234"), drive(SAT - 86400 * 3, off=2, alerts=1)
  marks = [{"t": SAT - 300, "text": "Curva fechada"}]
  o = {"marks": marks, "locale": "pt-BR", "nowS": SAT + 3600, "words": {"today": "hoje", "yesterday": "ontem"}}

  def ids(q, **kw):
    return [t["id"] for t in run("history", [a, b], {**o, "query": q, **kw})["list"]]

  assert ids("") == [a["id"], b["id"]]
  assert ids("sabado") == [a["id"]] and ids("hoje") == [a["id"]] and ids("31/10") == [a["id"]]
  assert ids("ABC12") == [a["id"]] and ids("curva") == [a["id"]] and ids("curva quarta") == []  # every word must be found
  assert ids("quarta") == [b["id"]]
  h = run("history", [a, b], {**o, "filter": "desengates"})
  assert [t["id"] for t in h["list"]] == [b["id"]] and h["counts"] == {"todas": 2, "marcas": 1, "desengates": 1, "alertas": 1}
  assert ids("", filter="nada") == [a["id"], b["id"]]


def test_history_leaves_out_hidden_and_idle_drives_unless_asked():
  shown, hid, still = drive(SAT), {**drive(SAT - 600), "hidden": True}, drive(SAT - 1200, km=0., moving_s=0)
  h = run("history", [shown, hid, still], {})
  assert [t["id"] for t in h["list"]] == [shown["id"]] and (h["hidden"], h["idle"]) == (1, 1)
  assert len(run("history", [shown, hid, still], {"showHidden": True})["list"]) == 3


def test_history_by_day():
  days = run("byDay", [drive(SAT), drive(SAT - 3600, km=3.), drive(SAT - 86400)])
  assert [(d["key"], len(d["trips"]), d["km"], d["minutes"]) for d in days] == [("2026-10-31", 2, 15., 40), ("2026-10-30", 1, 12., 20)]


def test_map_fits_the_whole_path_and_lists_its_tiles():
  assert run("mercator", 0, 0, 1) == [256, 256]
  pts = [[1.0, 2.0, 0, 0], [1.01, 2.02, 3, 60], [1.005, 2.03, 1, 120]]
  f = run("mapFit", pts, 360, 220, 18)
  assert all(17.99 <= x <= 342.01 and 17.99 <= y <= 202.01 for x, y in f["xy"])  # the path touches the margin, never past it
  bigger = run("mapFit", pts, 720, 440, 18)
  assert bigger["z"] == f["z"] + 1  # twice the room, one zoom closer
  tiles = run("mapTiles", f, 360, 220)
  assert all(t["z"] == f["z"] for t in tiles) and min(t["left"] for t in tiles) <= 0 and max(t["left"] + t["size"] for t in tiles) >= 360
  xs, ys = [x for x, _ in f["xy"]], [y for _, y in f["xy"]]
  assert 256 <= f["size"] < 512 and max(max(xs) - min(xs), max(ys) - min(ys)) > 160  # fills the box
  assert run("mapFit", [[1.0, 2.0, 0, 0]], 360, 220, 18)["z"] == 17  # a single point: close in, not the world
  assert run("mapFit", [], 360, 220, 18) is None
  assert run("nearestAt", pts, 70) == 1


def test_trip_numbers_like_a_trip_computer():
  n = run("tripNumbers", {"km": 30., "moving_s": 1800, "stop_s": 300, "v_max": 98.4, "run_km": 12.34, "run_s": 600})
  assert (n["avg"], n["vmax"], n["runKm"], n["runMin"], n["movingMin"], n["stopMin"]) == (60, 98, 12.34, 10, 30, 5)
  old = run("tripNumbers", {"km": .5, "moving_s": 40})
  assert old["avg"] is None and old["vmax"] is None and old["runKm"] is None  # too short for an average; older summaries
  assert run("tripNumbers", None) is None


def test_weekly_trend_leaves_hidden_and_idle_drives_out():
  now = SAT  # a Saturday: the week started on Monday 26/10
  trips = [drive(now, km=20., moving_s=1000, op_s=800, off=1), drive(now - 3 * 86400, km=10., moving_s=1000, op_s=200),
           {**drive(now - 3600, km=50.), "hidden": True}, drive(now - 7200, km=0., moving_s=0), drive(now - 8 * 86400, km=40., moving_s=2000, op_s=1000, off=2)]
  w = run("weekly", trips, now, 3)
  assert [x["trips"] for x in w] == [0, 1, 2]
  assert w[2]["km"] == 30. and w[2]["opPct"] == 50 and abs(w[2]["off100"] - 100 / 30) < 1e-9
  assert w[1]["off100"] == 5. and w[0]["opPct"] is None


def summed(**kw):
  return {"id": "a", "mtime": 1000, "minutes": 10, "hidden": False,
          "summary": {"km": 10., "moving_s": 600., "op_s": 300., "off": 1, "alerts": 1, "sh_on_s": 60., "sh_hard": 1, "se_grade_up": 3.,
                      **kw.pop("summary", {})}, **kw}


def test_a_hidden_drive_leaves_the_sums_too():
  shown, hidden, idle = summed(id="a"), summed(id="b", hidden=True), summed(id="c", summary={"km": .1, "moving_s": 10.})
  trips = [shown, hidden, idle]
  assert [t["id"] for t in run("listed", trips)] == ["a"]
  assert run("weekTotal", trips, 1000)["trips"] == 1
  assert run("totals", trips, 0)["trips"] == 1
  assert run("weekShadow", trips, 1000)["trips"] == 1
  assert run("totals", [hidden], 0) is None


def test_a_row_finds_its_drive_in_the_list_of_now():
  # the list is replaced on each refresh: the row may hold the object of an older one
  old, new = {"id": "a", "hidden": False, "n": 1}, {"id": "a", "hidden": False, "n": 2}
  assert run("currentTrip", [new], old)["n"] == 2
  assert run("currentTrip", [], old)["n"] == 1


def test_the_newest_marks_win_over_the_list_of_drives():
  recent = [{"id": "1", "t": 10, "reason": "faixa"}]
  older = [{"id": "1", "t": 10, "reason": ""}, {"id": "2", "t": 5, "reason": "outro"}]
  got = run("mergeMarks", recent, older)
  assert sorted(m["id"] for m in got) == ["1", "2"] and next(m for m in got if m["id"] == "1")["reason"] == "faixa"
  assert run("mergeMarks", recent, None) == recent


def test_the_pages_finish_what_hiding_a_drive_starts():
  static = Path(__file__).parents[1] / "static"
  html, trip = (static / "index.html").read_text(), (static / "viagens.html").read_text()
  assert 'trip_hidden: "ocultou uma viagem"' in html and 'trip_shown: "mostrou uma viagem de novo"' in html
  assert "Todas as viagens estão ocultas." in trip and "if (!order.length)" in trip  # with all of them hidden it stayed on loading
  assert "Desde sempre" not in html  # the sums only reach the drives whose logs are still on the comma


def test_a_hard_brake_shows_on_its_trip_page():
  trip = (Path(__file__).parents[1] / "static" / "viagens.html").read_text()
  assert 'const SHOWN = ["mark", "off", "hardbrake"];' in trip and 'e.kind !== "mark" && e.kind !== "off"' not in trip


def test_mapux_rever_viagem_reads_like_the_inicio_card():
  # owner's D6 (MAP-UX-01, 06/11): one language for a drive. Who drove in Início's three colours; the bar and the list in clock time
  assert [run("rideWho", p) for p in ({"who": "op"}, {"who": "wheel"}, {"who": "gas"}, {"who": "brake"}, {"who": "pedal"}, {"who": "you"},
                                      {"on": True}, {"on": False}, None)] == ["op", "help", "help", "help", "help", "you", "op", "you", "you"]
  trip = {"mtime": 10_000, "minutes": 10, "segments": [2, 3]}  # the deleter took minutes 0 and 1: the list starts at minute 2
  assert run("rideClock", trip, 120) == 10_000 - 600 and run("rideClock", trip, 150) == 10_000 - 600 + 30
  assert run("rideClock", trip, run("pathSecond", trip, 9_700)) == 9_700  # pathSecond's inverse
  page = (Path(__file__).parents[1] / "static" / "viagens.html").read_text()
  assert "toFixed(" not in page and "<select" not in page  # numbers in the phone's language; ‹ i de N › as on Início, no list box
  assert "LPL.listed(all)" in page  # the same drives as Início's ‹ 1 de N ›: not hidden, not idle
  assert '<i class="sw help"></i><span data-t>com a sua ajuda</span>' in page and "LPL.rideWho(points[i])" in page
  assert 'localStorage.getItem("lpStreets") === "1"' in page and 'localStorage.setItem("lpStreets"' in page  # Ruas: one switch for both pages
  assert "LPL.mapFit(ll, w, h, 26)" in page and 'im.referrerPolicy = "strict-origin"' in page
  # hiding is a change: it asks in the page first
  assert '$("hideTrip").addEventListener("click", () => {' in page and '$("hideOk").addEventListener("click", async () => {' in page
  assert page.index('$("hideTrip").addEventListener') < page.index('fetch("/api/trip/hide"') and "hideAsk" in page
  html = (Path(__file__).parents[1] / "static" / "index.html").read_text()
  assert '<a class="btn dark" id="tripOpen" href="/viagens">' in html  # Rever viagem is a button on Início's card


def test_unpairing_leaves_no_list_of_drives_on_the_phone():
  html = (Path(__file__).parents[1] / "static" / "index.html").read_text()
  assert 'if (!v) wipeBase(); }' in html  # APP-01: the whole copy leaves, not only the list
  wipe = html[html.index("async function wipeBase()"):]
  wipe = wipe[:wipe.index("\n}\n")]
  assert "DB.clear()" in wipe and "await done" in wipe and "mQueue = []" in wipe  # the missions waiting for the comma too
  assert "LPL.DATA_KEYS" in wipe and "trips = null" in wipe and "paths.clear()" in wipe
  keys = subprocess.run([NODE, "-e", f"process.stdout.write(JSON.stringify(require({json.dumps(str(LOGIC))}).DATA_KEYS))"],
                        capture_output=True, text=True, timeout=30, check=True).stdout
  want = {"lpTrips", "lpMissionSnap", "lpCopEvents", "lpCopAnswers", "lpCopTally", "lpNavRecent"}
  assert set(json.loads(keys)) == want  # data only: the language and the view stay


def test_a_401_erases_nothing_without_asking():
  # CTO review, 06/11 (patch 0002): another comma at the same address answers 401 to this phone's token. That alone must not
  # erase the history and the pairing: read only, and the person chooses between trying again and erasing to pair again
  for path in ("/api/state", "/api/trips", "/api/missions", "/api/marks"):
    assert run("strangerAnswer", 401, path) is True
  assert run("strangerAnswer", 401, "/api/pair") is False and run("strangerAnswer", 401, "/api/logout") is False  # their own answers
  assert not any(run("strangerAnswer", s, "/api/state") for s in (0, 200, 403, 404, 429, 500, 503))
  html = (LOGIC.parent / "index.html").read_text()
  api = html[html.index("async function api(path, body, waitMs)"):]
  api = api[:api.index("\n}\n")]
  assert "saveToken(" not in api and "wipeBase(" not in api and 'show("pair")' not in api
  assert "if (LPL.strangerAnswer(r.status, path)) unknownHere();" in api
  # once refused, this phone's token goes nowhere until the person chooses: the guard comes before the request
  assert api.index('if (stranger && path !== "/api/pair") return') < api.index("fetch(path, opts)")
  assert "if (document.hidden || !token || stranger) return;" in html  # and no polling either
  # an answer to a request sent before the 401 came back doesn't make the comma "online" again
  assert api.index("fetch(path, opts)") < api.index("if (stranger) return", api.index("fetch(path, opts)")) < api.index("setOnline(true)")
  assert '$("offlineCard").classList.toggle("hidden", on || stranger);' in html  # one card at a time: this one says read only
  here = html[html.index("function unknownHere() {"):]
  here = here[:here.index("\n}\n")]
  assert "setOnline(false)" in here and "setStranger(true)" in here and "saveToken" not in here and "wipeBase" not in here
  # the only ways that erase: the two buttons, each after a yes
  erase = [i for i in range(len(html)) if html.startswith("saveToken(null)", i)]
  assert len(erase) == 2
  for i in erase:
    handler = html[html.rindex("addEventListener(\"click\"", 0, i):i]
    assert "if (!confirm(t(" in handler
  assert html.index('$("strangerPair").addEventListener') < erase[0] < html.index('$("logoutBtn").addEventListener') < erase[1]
  assert html.count("wipeBase()") == 3  # its definition, saveToken(null) and "Apagar dados", which asks first too
  card = html[html.index('id="strangerCard"'):html.index('id="strangerPair"')]
  assert 'role="alert"' in card and 'id="strangerRetry"' in card and "Nada foi apagado" in card


def test_no_path_yet_is_not_no_gps():
  # right after an update the phone has summaries from its cache and the comma is still reading the drives
  drive_read = {"summary": {"km": 5.}}
  assert run("pathSettled", drive_read, 0) is True
  assert run("pathSettled", drive_read, 12) is False
  assert run("pathSettled", {"summary": None}, 0) is False


def test_a_mark_finds_its_place_on_the_path_of_a_trimmed_drive():
  # the path counts from minute 0 of the route, and the deleter may have taken the first minutes of the list
  whole = {"mtime": 1000, "minutes": 10, "segments": list(range(10))}
  trimmed = {"mtime": 1000, "minutes": 2, "segments": [8, 9]}
  assert run("pathSecond", whole, 700) == 300  # 5 min into the drive
  assert run("pathSecond", trimmed, 900) == 8 * 60 + 20  # 20 s into what's left, which starts at minute 8


# ----- the KPIs (owner, 02/11 14:54): every number with its context

def kdrive(end, **s):
  base = {"km": 20., "moving_s": 1200, "op_s": 900, "off": 1, "driver": 1, "alerts": 2, "wob_ss": 400., "wob_n": 1000, "wob_s": 100.,
          "lane_n": 100, "lane_ok": 90}
  return {"id": f"k{end}", "mtime": end, "minutes": 25, "summary": {**base, **s}, "hidden": False}


def test_kpi_of_a_set_of_drives():
  k = run("kpiOf", [kdrive(SAT), kdrive(SAT - 3600, km=10., off=0, driver=0)])
  assert "kmPerInt" not in k  # a hand on the wheel isn't an intervention (CTO review of 05)
  assert abs(k["opPct"] - 75) < 1e-9 and abs(k["wobble"] - (800 / 2000) ** .5) < 1e-9
  assert k["lanePct"] == 90 and abs(k["alerts100"] - 400 / 30) < 1e-9
  thin = run("kpiOf", [kdrive(SAT, km=.5, moving_s=20, wob_s=12., lane_n=3)])
  assert thin["opPct"] is None and thin["wobble"] is None and thin["lanePct"] is None  # under 30 s of straight: no wobble
  old = run("kpiOf", [kdrive(SAT, wob_s=None)])
  assert old["wobble"] is None  # a summary from before the straights-only wobble measured curves
  assert run("kpiOf", [{**kdrive(SAT), "hidden": True}])["trips"] == 0


def test_kpis_trend_says_better_or_worse_the_right_way():
  now = [kdrive(SAT - 86400 * i, wob_ss=200.) for i in range(3)]
  before = [kdrive(SAT - 86400 * (30 + i), wob_ss=400.) for i in range(3)]
  k = run("kpis", now + before, SAT + 60, 28)
  assert k["wobble"]["trend"] == 1  # less wobble: better
  assert k["opPct"]["trend"] == 0 and k["alerts100"]["trend"] == 0  # the same: about the same
  worse = run("kpis", [kdrive(SAT, alerts=9), kdrive(SAT - 3600, alerts=9)] + before, SAT + 60, 28)
  assert worse["alerts100"]["trend"] == -1
  thin = run("kpis", [kdrive(SAT, km=12.)] + before, SAT + 60, 28)  # 12 km in 4 weeks: no number, no arrow
  assert thin["now"]["few"] and thin["now"]["opPct"] is None and thin["opPct"] is None


def test_version_compare_says_nothing_under_the_minimum():
  trips = [kdrive(SAT, commit="b2", wob_ss=200.), kdrive(SAT - 7200, commit="b2", wob_ss=200.), kdrive(SAT - 86400, commit="a1")]
  v = run("versionCompare", trips)
  assert (v["now"], v["before"]) == ("b2", "a1") and v["few"] is True
  assert v["better"] is None and v["judged"] is None and all(r["delta"] is None for r in v["rows"])  # the numbers, no verdict
  row = next(r for r in v["rows"] if r["key"] == "wobble")
  assert row["now"] < row["before"]
  many = [kdrive(SAT - 3600 * i, commit="b2", km=25., wob_ss=200.) for i in range(9)] + [kdrive(SAT - 86400 * (2 + i), commit="a1", km=25.) for i in range(9)]
  v = run("versionCompare", many)
  assert not v["few"] and v["judged"] >= 1 and v["better"] >= 1
  assert run("versionCompare", [kdrive(SAT, commit="b2")]) is None


def test_version_compare_is_against_the_last_tested():  # owner, 03/11: several updates went out untested
  rel = [{"commit": c} for c in ("e5", "d4", "c3", "b2", "a1")]
  trips = [kdrive(SAT, commit="e5"), kdrive(SAT - 3600, commit="d4", km=5.), kdrive(SAT - 86400, commit="b2", km=40.),
           kdrive(SAT - 2 * 86400, commit="a1", km=300.)]
  v = run("versionCompare", trips, rel)
  assert (v["now"], v["before"]) == ("e5", "b2")  # d4 had a 5 km drive: not tested
  assert v["between"] == ["c3"] and v["short"] == ["d4"] and v["b"]["km"] == 40  # d4's 5 km: on neither side, and said so
  only_short = [kdrive(SAT, commit="e5"), kdrive(SAT - 3600, commit="d4", km=5.)]
  assert run("versionCompare", only_short, rel)["before"] == "d4"  # nothing tested: the last with any drive
  assert run("versionCompare", trips, None)["between"] is None  # no notes yet: nothing to name
  back = [kdrive(SAT, commit="e5"), kdrive(SAT - 86400, commit="b2", km=40.), kdrive(SAT - 7 * 86400, commit="d4", km=300.)]
  v = run("versionCompare", back, rel)  # went back to b2 yesterday after d4 last week
  assert v["before"] == "b2" and v["between"] == ["c3"] and v["short"] == [] and v["earlier"] == ["d4"]
  assert run("versionCompare", trips + [kdrive(SAT + 60, commit="zz")], rel)["between"] is None  # a version the notes don't know


def test_a_drive_against_its_average():
  trips = [kdrive(SAT - 86400 * i) for i in range(1, 4)]
  me = kdrive(SAT, wob_ss=100.)
  v = run("tripVsAverage", me, trips + [me], SAT + 60)
  assert v["wobble"]["word"] == "melhor que a sua média" and v["opPct"]["word"] == "na sua média" and "kmPerInt" not in v


# ----- missions

def _m(key, how, total=1, done=0, lock="", **kw):
  return {"key": key, "trail": "a", "title": key.capitalize(), "total": total, "how": how, "done": done, "lock": lock, **kw}


CAT = {"levels": [[0, "Garagem"], [.5, "Cidade"], [1, "Pronta"]], "trails": [{"key": "a", "title": "A"}],
       "missions": [_m("lento", "auto", detect="slow10"), _m("noite", "auto", detect="night30"),
                    _m("inst", "auto", detect="installed", arg="b3ac6fe"), _m("fila", "marca", 3, 1),
                    _m("portas", "parado", 4), _m("aeb", "cto", lock="Só no log")]}


def trip(km=10, moving=900, slow=0, commit="a635314", hour_end=12, hidden=False):
  import datetime
  end = datetime.datetime(2026, 11, 2, hour_end, 0).timestamp()
  return {"mtime": end, "minutes": round(moving / 60), "hidden": hidden,
          "summary": {"km": km, "moving_s": moving, "slow_s": slow, "commit": commit, "v_max": 60, "marks": 0}}


def test_missions_seen_in_the_summaries_and_the_owners_marks_count_at_once():
  st = {"active": ["fila"], "answers": {"portas": 2}, "pending": [{"key": "fila", "mark": "x", "t": 1}, {"key": "fila", "mark": "y", "t": 2}]}
  p = run("missionProgress", CAT, st, [trip(slow=650, commit="b3ac6fe1234")])
  assert p["lento"]["full"] and p["inst"]["full"] and not p["noite"]["full"]
  # two layers (owner, 05/11): 1 accepted by the CTO + 2 done by the owner = full now, and still to check
  f = p["fila"]
  assert f["d"] == 3 and f["full"] and f["accepted"] == 1 and f["byYou"] == 2 == f["toCheck"] and not f["checked"] and f["active"]
  assert p["portas"]["d"] == 2 and p["aeb"]["locked"]
  assert not run("missionProgress", CAT, st, [trip(slow=650, hidden=True)])["lento"]["full"]  # a hidden drive doesn't count
  assert run("missionProgress", CAT, st, [trip(moving=1900, hour_end=21)])["noite"]["full"]
  assert not run("missionProgress", CAT, st, [trip(moving=1200, hour_end=21)])["noite"]["full"]


def test_the_approval_checks_tick_from_the_drive_summary():  # MISS-01: the log does the Marcar's job too
  ms = [_m("cinto", "parado", detect="belt"), _m("portas", "parado", 4, detect="doors"), _m("cambio", "parado", detect="gears"),
        _m("pedais", "parado", 2, detect="pedals"), _m("piscas", "parado", 3, detect="blinkers"), _m("cego", "marca", 2, detect="bsm"),
        _m("ligar", "parado", 3, detect="ign3")]
  cat = {"levels": CAT["levels"], "trails": [], "missions": ms}
  assert all(p["d"] == 0 for p in run("missionProgress", cat, {}, [trip()]).values())

  def sig(end_min=0, **sg):
    t = trip()
    t["mtime"] += end_min * 60
    t["summary"].update(sg)
    return t
  one = sig(sg_belt=1, sg_door=3, sg_gears=15, sg_brake=2, sg_left=1, sg_hazard=1, sg_bsm_r=1)
  p = run("missionProgress", cat, {}, [one])
  assert p["cinto"]["full"] and p["cambio"]["full"] and p["portas"]["d"] == 3 and p["pedais"]["d"] == 1
  assert p["piscas"]["d"] == 2 and p["cego"]["d"] == 1 and p["cinto"]["checked"]  # the log is the proof: accepted at once
  assert not run("missionProgress", cat, {}, [sig(sg_gears=13)])["cambio"]["full"]  # no reverse
  # the doors in one stop; three starts within 15 minutes
  assert run("missionProgress", cat, {}, [sig(sg_door=2), sig(1, sg_door=2)])["portas"]["d"] == 2
  starts = [sig(0), sig(5), sig(10)]
  assert run("missionProgress", cat, {}, starts)["ligar"]["full"]
  assert run("missionProgress", cat, {}, [sig(0), sig(20), sig(40)])["ligar"]["d"] == 0  # one start is any drive
  assert run("missionProgress", cat, {}, [sig(0), sig(5)])["ligar"]["d"] == 2
  hidden = dict(one, hidden=True)
  assert not run("missionProgress", cat, {}, [hidden])["cinto"]["full"]  # a hidden drive doesn't count


def test_a_new_cars_approval_and_its_report():  # MISS-01 B, the generic edition
  ms = [dict(_m("ap-cinto", "parado", detect="belt"), trail="ap-sinais"), dict(_m("ap-portas", "parado", 4, detect="doors"), trail="ap-sinais"),
        dict(_m("ap-engatar", "auto", detect="engaged"), trail="ap-engate"), dict(_m("ap-freio-desliga", "auto", detect="brake_off"), trail="ap-engate"),
        dict(_m("ap-cancelar", "marca"), trail="ap-engate"), dict(_m("ap-sem-falha", "auto", detect="lat30"), trail="ap-direcao"),
        dict(_m("ap-freio-mao", "parado"), trail="ap-sinais"), dict(_m("outra", "parado"), trail="celular")]
  cat = {"levels": CAT["levels"], "trails": [], "missions": ms}

  def drive(**sg):
    t = trip()
    t["summary"].update(sg)
    return t
  trips = [drive(sg_belt=1, sg_door=2, on_s=2000, brake=1, sg_steer_fault=0)]
  p = run("missionProgress", cat, {"answers": {"ap-freio-mao": 1}, "pending": [{"key": "ap-cancelar", "t": 1}]}, trips)
  assert p["ap-engatar"]["full"] and p["ap-freio-desliga"]["full"] and p["ap-sem-falha"]["full"]
  assert not run("missionProgress", cat, {}, [drive(on_s=2000, sg_steer_fault=1)])["ap-sem-falha"]["full"]  # a fault in it
  assert not run("missionProgress", cat, {}, [drive(on_s=2000)])["ap-sem-falha"]["full"]  # an old summary, faults not counted
  assert not run("missionProgress", cat, {}, [drive(on_s=30)])["ap-engatar"]["full"]  # half a minute
  rep = run("approvalReport", cat, p, trips)
  assert rep.startswith("# comma porting checklist") and "outra" not in rep and "Outra" not in rep
  lines = rep.split("\n")
  assert "- [x] Seatbelt: seen in the drive logs" in lines
  assert "- [~] Doors (2/4)" in lines
  assert "- [x] Parking brake: confirmed by the driver" in lines
  assert "- [x] The cancel button disengages: marked while driving, to check in the logs" in lines
  assert "- [x] 30 min of openpilot steering with no steering fault: seen in the drive logs" in lines
  assert "## Vehicle state" in lines and "## Lateral control" in lines and "## Submission" not in lines  # no mission of it here
  assert run("approvalReport", {"missions": [_m("x", "parado")]}, {}, []) == ""  # the Haval build: no approval trails


def test_the_calibration_trail_from_what_the_comma_learned():  # MISS-01 C
  ms = [_m("cam", "auto", detect="calibrated"), _m("rel", "auto", detect="ratio"), _m("tq", "auto", detect="torque"),
        _m("rodas", "auto", detect="wheels_gps")]
  cat = {"levels": CAT["levels"], "trails": [], "missions": ms}

  def drive(**cb):
    t = trip()
    t["summary"].update(cb)
    return t
  p = run("missionProgress", cat, {}, [drive(cb_calibrated=1, cb_sr=18.4, cb_sr_km=25, cb_tq_perc=100, cb_v_n=900, cb_v_wheel=18000,
                                             cb_v_gps=17800)])
  assert all(p[k]["full"] for k in ("cam", "rel", "tq", "rodas"))
  p = run("missionProgress", cat, {}, [drive(cb_calibrated=0, cb_sr=18.4, cb_sr_km=10, cb_tq_perc=96, cb_v_n=900, cb_v_wheel=18000,
                                             cb_v_gps=17000)])
  assert not any(p[k]["full"] for k in ("cam", "rel", "tq", "rodas"))  # not yet; 10 km; 96%; the wheels 5.9% over the GPS
  assert not run("missionProgress", cat, {}, [drive(cb_v_n=100, cb_v_wheel=2000, cb_v_gps=2000)])["rodas"]["full"]  # 10 s only


def test_the_level_and_what_moved_after_a_drive():
  before = run("missionProgress", CAT, {}, [])
  after = run("missionProgress", CAT, {"pending": [{"key": "fila"}]}, [trip(slow=650, commit="b3ac6fe")])
  lv = run("missionLevel", CAT, after)
  assert lv["n"] == 2 and lv["i"] == 0 and lv["next"] == {"name": "Cidade", "left": 1}
  moved = {d["key"]: d for d in run("missionDiff", before, after)}
  assert set(moved) == {"lento", "inst", "fila"} and moved["fila"]["checks"] == 1 and moved["lento"]["full"]
  order = [m["key"] for m in run("missionOrder", CAT["missions"], after)]
  assert order[0] == "fila" and order[-2:] == ["lento", "inst"] and order.index("aeb") > order.index("portas")
  tr = run("missionTrails", CAT, after)[0]
  assert tr["d"] == 2 and tr["next"] == "Fila" and not tr["seal"]


def test_the_first_trails_come_first_whatever_is_under_way():  # owner, 06/11: the approval by comma leads
  ms = [dict(_m("dir", "marca", 3, 1), trail="direcao"), dict(_m("cinto", "parado"), trail="coleta"),
        dict(_m("trava", "parado", lock="x"), trail="coleta"), dict(_m("x", "parado"), trail="outra")]
  prog = run("missionProgress", {"levels": CAT["levels"], "trails": [], "missions": ms}, {}, [])
  assert [m["key"] for m in run("missionOrder", ms, prog, ["coleta", "versao", "direcao"])] == ["cinto", "trava", "dir", "x"]
  assert [m["key"] for m in run("missionOrder", ms, prog)] == ["dir", "cinto", "x", "trava"]  # no trails given: as before


# ----- the phone's own copy (APP-01)

def ctrip(i, km=1., hidden=False):
  return {"id": i, "mtime": 100, "summary": {"km": km}, "hidden": hidden}


def test_copy_nothing_new_keeps_everything():
  base = {"trips": [ctrip("b"), ctrip("a")], "marks": [{"id": "m1"}], "v": "v1"}
  out = run("applyTrips", base, {"same": True, "v": "v1"})
  assert out == {"trips": base["trips"], "marks": base["marks"], "v": "v1"}


def test_copy_a_delta_replaces_only_the_drive_that_changed_and_keeps_the_comma_order():
  base = {"trips": [ctrip("b"), ctrip("a")], "marks": [{"id": "m1"}], "v": "v1"}
  out = run("applyTrips", base, {"delta": True, "trips": [ctrip("c"), ctrip("a", 9.)], "ids": ["c", "b", "a"], "hidden": ["b"], "v": "v2"})
  assert [t["id"] for t in out["trips"]] == ["c", "b", "a"] and out["trips"][2]["summary"]["km"] == 9.
  assert [t["hidden"] for t in out["trips"]] == [False, True, False]
  assert out["marks"] == [{"id": "m1"}] and out["v"] == "v2" and out["missing"] == 0  # no marks in the answer: the copy's stay


def test_copy_a_drive_the_comma_deleted_leaves_and_new_marks_replace():
  base = {"trips": [ctrip("b"), ctrip("a")], "marks": [{"id": "m1"}], "v": "v1"}
  out = run("applyTrips", base, {"delta": True, "trips": [], "ids": ["b"], "hidden": [], "marks": [{"id": "m2"}], "v": "v2"})
  assert [t["id"] for t in out["trips"]] == ["b"] and out["marks"] == [{"id": "m2"}]


def test_copy_cut_short_asks_for_the_whole_list():
  out = run("applyTrips", {"trips": [ctrip("a")], "v": "v1"}, {"delta": True, "trips": [], "ids": ["z", "a"], "hidden": [], "v": "v2"})
  assert out["missing"] == 1


def test_copy_a_whole_answer_replaces_it():
  out = run("applyTrips", {"trips": [ctrip("a")], "marks": [{"id": "m1"}], "v": "v1"}, {"trips": [ctrip("x")], "marks": [], "v": "v9"})
  assert [t["id"] for t in out["trips"]] == ["x"] and out["marks"] == [] and out["v"] == "v9"
  assert run("applyTrips", None, None) == {"trips": [], "marks": [], "v": ""}


def test_copy_marks_and_missions_whole_or_kept():
  base = {"marks": [1], "v": "a"}
  assert run("applyWhole", base, {"same": True, "v": "a"}) == base
  assert run("applyWhole", base, {"marks": [2], "v": "b"}) == {"marks": [2], "v": "b"}
  assert run("applyWhole", None, {"same": True, "v": "a"}) is None
  assert run("since", "a.b.1") == "since=a.b.1" and run("since", "") == ""


def test_copy_keeps_a_path_only_with_the_maps_option_on():  # owner, 03/11 (D2): off by default, and off keeps nothing
  got = {"points": [[1, 2, 0, 0]], "v": "p1"}
  assert run("keepPath", got, False) is None and run("keepPath", got, None) is None
  assert run("keepPath", got, True) == got and run("keepPath", {"status": "none"}, True) is None


TRIP = {"id": "00000040--a1", "segments": [0, 1, 2]}
SUM = {"km": 3.2, "s": 180, "moving_s": 150, "on_s": 120, "driver": 1, "gas": 0, "brake": 2, "off": 1, "marks": 1}
EVENTS = [{"t": 61.5, "kind": "mark", "text": "bandeira", "mono_ns": 123, "n": 1}, {"t": 70, "kind": "off", "text": "desengatou", "v": 31},
          {"t": 80, "kind": "driver", "text": "você no volante"}, {"t": 90, "kind": "hardbrake", "text": "freada forte"}]
POINTS = [{"t": 0, "v": 10, "on": True, "who": "op", "step": 1, "dpsi": 0, "lat": 0.001, "lon": 0.002, "n": 0},
          {"t": 1, "v": 11, "on": True, "who": "op", "step": 1, "dpsi": 0, "lat": 0.0011, "lon": 0.002, "n": 0}]


def test_ride_keeps_the_numbers_and_the_shown_events_never_a_position():  # APP-02: Rever viagem without the comma
  kept = run("rideKeep", TRIP, SUM, EVENTS, POINTS, False, 1000)
  assert kept["map"] is None
  ride = kept["ride"]
  assert ride["v"] == "0,1,2" and ride["at"] == 1000 and ride["summary"] == SUM
  assert ride["events"] == [{"t": 61.5, "kind": "mark", "text": "bandeira"}, {"t": 70, "kind": "off", "text": "desengatou"},
                            {"t": 90, "kind": "hardbrake", "text": "freada forte"}]  # what viagens.html lists (SHOWN), nothing else
  assert "lat" not in json.dumps(ride) and "points" not in ride


def test_ride_keeps_the_map_only_with_the_maps_option_on():  # D2: the minutes have positions
  assert run("rideKeep", TRIP, SUM, EVENTS, POINTS, True, 1)["map"] == {"v": "0,1,2", "points": POINTS}
  assert run("rideKeep", TRIP, SUM, EVENTS, POINTS[:1], True, 1)["map"] is None  # one point draws nothing
  assert run("rideKeep", TRIP, SUM, EVENTS, None, True, 1)["map"] is None
  assert run("rideKeep", None, {}, None, None, True, 1)["ride"] == {"v": "", "at": 1, "summary": {}, "events": []}


def test_ride_map_key_leaves_with_the_other_maps():  # index.html drops every "path:" key when the option goes off
  keys = run("rideKeys", "00000040--a1")
  assert keys == {"ride": "ride:00000040--a1", "map": "path:00000040--a1#min"}
  assert keys["map"].startswith("path:") and not keys["ride"].startswith("path:")


def test_ride_order_newest_first_and_the_oldest_leave():
  assert run("rideOrder", None, "a") == {"order": ["a"], "drop": []}
  assert run("rideOrder", ["b", "a", "c"], "a") == {"order": ["a", "b", "c"], "drop": []}
  assert run("rideOrder", ["c", "b", "a"], "d", 3) == {"order": ["d", "c", "b"], "drop": ["a"]}
  assert run("rideOrder", list(map(str, range(30))), "x")["drop"] == ["29"]  # RIDE_KEEP: 30 drives kept


def test_map_count_one_per_drive():
  assert run("mapCount", ["trips", "path:a", "path:a#min", "path:b#min", "ride:a", "rides"]) == 2
  assert run("mapCount", []) == 0 and run("mapCount", None) == 0


def test_copy_read_only_without_the_comma_or_before_its_first_answer():
  assert run("readOnly", True, False) is False
  assert run("readOnly", False, False) is True and run("readOnly", True, True) is True


# ----- the passenger's copilot (owner, 03/11)

def test_copilot_reads_the_moment_from_the_live_data():
  assert run("copilotMoment", None, 0) == [] and run("copilotMoment", {"driving": False}, 0) == []
  assert run("copilotMoment", {"driving": True, "v": 0, "lead": None}, 5) == ["stop_free"]
  assert run("copilotMoment", {"driving": True, "v": 0, "lead": {"d": 6}}, 90) == ["stop_queue", "stop_long"]
  assert run("copilotMoment", {"driving": True, "v": 50, "lead": {"d": 30}}, 0) == ["moving", "city", "follow_mid"]
  assert run("copilotMoment", {"driving": True, "v": 20, "lead": {"d": 10}}, 0) == ["moving", "city", "slow_follow"]
  assert run("copilotMoment", {"driving": True, "v": 90, "lead": None}, 0) == ["moving"]  # the wheel test is for the city only


def test_copilot_puts_what_fits_now_first_and_skips_what_is_done():
  cat = {"missions": [_m("a", "marca", when="follow_mid"), _m("b", "marca", when="stop_free"), _m("c", "marca", when="stop_free"),
                      _m("d", "marca"), _m("e", "cto", lock="x")]}
  prog = run("missionProgress", cat, {}, [])
  prog["c"]["full"] = True
  order = run("copilotOrder", cat, ["a", "b", "c", "d", "e"], prog, ["stop_free"])
  assert order == [{"key": "b", "fits": True}, {"key": "a", "fits": False}, {"key": "d", "fits": False}]


def test_copilot_old_live_data_never_says_now():  # CTO review of app-08: 60 s old still said "Agora: … / Dá para fazer agora"
  slow = {"driving": True, "v": 20, "lead": {"d": 10}}
  assert run("copilotNow", slow, {"online": True, "age": 500}, 0) == {"state": "live", "moment": ["moving", "city", "slow_follow"]}
  assert run("copilotNow", slow, {"online": True, "age": 60000}, 0) == {"state": "stale", "moment": [], "age": 60}
  assert run("copilotNow", {"driving": False}, {"online": True, "age": 500}, 0) == {"state": "off", "moment": []}
  assert run("copilotNow", None, {"online": True, "age": 500}, 0)["state"] == "none"
  assert run("copilotNow", slow, {"online": False, "age": 500}, 0)["state"] == "none"


def test_copilot_stop_clock_goes_on_through_an_old_tick():
  stopped, moving = {"driving": True, "v": 0}, {"driving": True, "v": 30}
  assert run("stopClock", None, stopped, False, 1000) == 1000
  assert run("stopClock", 1000, stopped, False, 5000) == 1000
  assert run("stopClock", 1000, stopped, True, 6000) == 1000  # one old tick: still stopped since 1000
  assert run("stopClock", None, stopped, True, 6000) is None
  assert run("stopClock", 1000, moving, False, 7000) is None and run("stopClock", 1000, {"driving": False}, False, 7000) is None


def test_copilot_always_asks_the_passenger_and_closes_when_moving_without_one():
  parked, moving, off = {"on": True, "moving": False}, {"on": True, "moving": True}, {"on": False, "moving": False}
  assert run("copilotGate", {"ok": False}, parked) == {"ok": False, "ask": True, "close": False}  # parked too
  assert run("copilotGate", {"ok": True}, parked) == {"ok": True, "ask": False, "close": False}
  assert run("copilotGate", {"ok": False}, moving)["close"] is True
  assert run("copilotGate", {"ok": True}, moving) == {"ok": True, "ask": False, "close": False}
  assert run("copilotGate", {"ok": True}, off) == {"ok": False, "ask": True, "close": True}  # a new drive asks again
  assert run("passenger", {"ok": False}, parked)["ask"] is False  # Marcar parked still asks nothing


def test_copilot_keeps_what_was_answered_on_this_drive():
  pend = [{"key": "a", "t": 50}, {"key": "b", "t": 150, "result": "bad"}, {"key": "c", "t": 160, "result": "ok"}, {"key": "c", "t": 170}]
  assert run("tripAnswers", pend, 100) == {"b": "bad", "c": "done"}


def test_a_mark_that_did_not_work_never_counts_as_progress():  # CTO review of app-08: "Não deu" showed as "1 missão avançou"
  cat = {"missions": [_m("x", "marca", 3), _m("y", "marca", 1)]}
  before = run("missionProgress", cat, {}, [])
  st = {"pending": [{"key": "x", "result": "bad"}, {"key": "x", "result": "stop"}, {"key": "y", "result": "ok"}]}
  after = run("missionProgress", cat, st, [])
  assert after["x"]["toCheck"] == 0 and after["x"]["fails"] == 2 and after["y"]["toCheck"] == 1 and after["y"]["fails"] == 0
  rows = {r["key"]: r for r in run("missionDiff", before, after)}
  assert rows["x"]["moved"] is False and rows["x"]["fails"] == 2 and rows["x"]["checks"] == 0
  assert rows["y"]["moved"] is True and rows["y"]["checks"] == 1
  assert run("missionProgress", cat, {"pending": [{"key": "x"}]}, [])["x"]["toCheck"] == 1  # an older mark, no result: "Fiz agora"


CAT_Q = {"max_active": 12, "route": {"key": "v1", "old": ["v0"]},
         "missions": [_m("a", "marca"), _m("b", "auto"), _m("d", "parado", drive=True, route=2), _m("c", "marca", route=1),
                                        _m("e", "marca", route=3, lock="x"), _m("f", "parado", route=4), _m("g", "marca", 2, route=5)]}


def test_missions_chosen_away_from_the_comma_wait_in_order():  # owner, 03/11 (D3)
  q = run("queueAdd", [], {"action": "take", "key": "a"})
  q = run("queueAdd", q, {"action": "take", "key": "b"})
  assert q == [{"action": "take", "key": "a"}, {"action": "take", "key": "b"}]
  assert run("queueAdd", q, {"action": "take", "key": "a"}) == q  # twice: once
  assert run("queueAdd", q, {"action": "drop", "key": "a"}) == [{"action": "take", "key": "b"}]  # taken and dropped: nothing to send
  # the route goes after what was picked, once: a second one takes the first one's place at the end
  assert run("queueAdd", q, {"action": "pack", "key": "v1"}) == q + [{"action": "pack", "key": "v1"}]
  assert run("queueAdd", [{"action": "pack", "key": "v1"}, {"action": "take", "key": "a"}], {"action": "pack", "key": "v1"}) == \
    [{"action": "take", "key": "a"}, {"action": "pack", "key": "v1"}]
  for bad in ({"action": "answer", "key": "a"}, {"action": "mark", "key": "a"}, {"action": "take"}, None):  # only missions wait
    assert run("queueAdd", q, bad) == q
  after_pack = run("queueAdd", [{"action": "pack", "key": "v1"}], {"action": "drop", "key": "c"})
  assert after_pack == [{"action": "pack", "key": "v1"}, {"action": "drop", "key": "c"}]


def test_missions_waiting_show_on_top_of_the_comma_state():
  st = {"active": ["a"], "answers": {}, "pending": []}
  assert run("applyQueue", st, [{"action": "take", "key": "b"}, {"action": "drop", "key": "a"}], CAT_Q)["active"] == ["b"]
  assert run("applyQueue", st, [{"action": "pack", "key": "v1"}], CAT_Q)["active"] == ["a", "c", "d", "g"]  # after what was picked, in order
  assert run("applyQueue", dict(st, answers={"g": 2}), [{"action": "pack", "key": "v1"}], CAT_Q)["active"] == ["a", "c", "d"]  # answered: out
  assert run("applyQueue", st, [], CAT_Q) == st
  assert sorted(run("queuedKeys", [{"action": "pack", "key": "v1"}, {"action": "take", "key": "a"}], CAT_Q)) == ["a", "c", "d", "g"]
  # what waits, as it will land: what the comma already has, or the answers took out, isn't "vai para o comma"
  assert run("queuedKeys", [{"action": "pack", "key": "v1"}], CAT_Q, {"active": ["c"], "answers": {"g": 2}}) == ["d"]
  assert run("applyQueue", st, [{"action": "pack", "key": "zzz"}], CAT_Q) == st  # the comma refuses a route it doesn't know
  assert run("applyQueue", st, [{"action": "pack", "key": "v0"}], CAT_Q)["active"] == ["a", "c", "d", "g"]  # an old one is this one
  full = {"active": [f"x{i}" for i in range(12)]}
  assert run("applyQueue", full, [{"action": "take", "key": "a"}], CAT_Q) == full  # never past the 12 the comma takes
  assert [m["key"] for m in CAT_Q["missions"] if run("canTake", m)] == ["a", "b", "d", "c", "g"]


def test_the_phone_takes_the_same_missions_as_the_comma_and_the_same_results_count():
  from openpilot.selfdrive.hexa import missions as M
  pub = M.public()["missions"]
  script = f"const L = require({json.dumps(str(LOGIC))}); const ms = {json.dumps(pub)};" + \
    "process.stdout.write(JSON.stringify([ms.map(L.canTake), L.ADVANCED]));"
  took, advanced = json.loads(subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout)
  assert took == [M.can_take(m) for m in M.CATALOG] and took == [M.can_mark(m) for m in M.CATALOG]
  assert advanced == list(M.ADVANCED) and set(M.RESULTS) == {"done", "ok", "bad", "stop"}


def test_the_phone_counts_the_same_route_as_the_comma():
  from openpilot.selfdrive.hexa import missions as M
  cat = {k: v for k, v in M.public().items() if k in ("missions", "route")}
  for answers in ({}, {"fila": 1}, {"parar-perto": 5, "sempre-ativa": 4}):
    assert run("routeKeys", cat, answers) == M.route_keys(answers), answers
    st = M.clean({"active": ["noite", "fila"], "answers": answers})
    for key in (M.ROUTE_KEY, *M.OLD_ROUTES):
      assert run("applyQueue", st, [{"action": "pack", "key": key}], cat)["active"] == M.take_pack(st, key)["active"], (answers, key)


def test_the_history_tells_each_try_and_which_marks_it_checked():  # owner, 03/11
  import time

  def day(iso, h):
    return int(time.mktime(time.strptime(f"{iso} {h}", "%Y-%m-%d %H:%M")))
  m = _m("x", "marca", 3, 1, tries=[{"date": "2026-11-01", "route": "rota 16", "version": "a1", "result": "ok", "measured": "4 de 4"},
                                    {"date": "2026-11-03", "route": "rota 31", "version": "b2", "result": "partial"}])
  pend = [{"key": "x", "t": day("2026-11-01", "10:00"), "result": "ok"}, {"key": "x", "t": day("2026-11-03", "22:00"), "result": "bad"},
          {"key": "x", "t": day("2026-11-04", "08:00"), "result": "done"}, {"key": "y", "t": 5}]
  h = run("missionHistory", m, pend)
  assert [x["route"] for x in h["tries"]] == ["rota 31", "rota 16"]  # newest first
  assert [(x["result"], x["checked"]) for x in h["marks"]] == [("done", False), ("bad", True), ("ok", True)]
  p = run("missionProgress", {"missions": [m]}, {"pending": pend}, [])["x"]
  assert p["toCheck"] == 1 and p["fails"] == 0  # what a try checked waits for nothing
  assert run("missionProgress", {"missions": [dict(m, tries=[])]}, {"pending": pend}, [])["x"]["toCheck"] == 2  # no try: all wait
  # a reference measured before the change checks none; a try with an hour checks only up to it
  base = dict(m, tries=[{"date": "2026-11-03", "result": "base"}])
  assert [x["checked"] for x in run("missionHistory", base, pend)["marks"]] == [False, False, False]
  timed = dict(m, tries=[{"date": "2026-11-03T12:00", "result": "ok"}])
  assert [x["checked"] for x in run("missionHistory", timed, pend)["marks"]] == [False, False, True]


def test_done_lists_what_was_checked_latest_first():
  cat = {"missions": [_m("a", "marca", tries=[{"date": "2026-11-01", "result": "ok"}]), _m("b", "marca"), _m("c", "cto", 1, 1),
                      _m("d", "marca", tries=[{"date": "2026-11-02", "result": "bad"}]), _m("e", "marca", tries=[{"date": "2026-11-03", "result": "base"}])]}
  prog = run("missionProgress", cat, {}, [])
  assert [m["key"] for m in run("missionsChecked", cat, prog)] == ["d", "a", "c"]  # a reference alone isn't done


# ----- versions (owner, 03/11): fewer chips, every version on its own screen

def test_version_chips_show_the_newest_few_and_how_many_more():
  seen = ["f2", "a6", "3b", "00", "d5"]
  assert run("versionChips", seen, None) == {"chips": [None, "f2", "a6", "3b"], "more": 2}
  assert run("versionChips", seen, "d5") == {"chips": [None, "f2", "a6", "3b", "d5"], "more": 1}  # the one picked stays visible
  assert run("versionChips", ["f2"], None) == {"chips": [None, "f2"], "more": 0}
  assert run("versionChips", seen, "zz")["more"] == 2  # a pick its drives no longer show still counts what's hidden


def test_version_list_has_the_notes_and_your_drives():
  rel = [{"commit": "b2", "date": "03/11", "title": "Nova", "changes": ["x"]}, {"commit": "a1", "date": "02/11", "title": "Velha", "changes": ["y"]}]
  trips = [kdrive(SAT, commit="b2"), kdrive(SAT - 3600, commit="b2"), kdrive(SAT - 7200, commit="zz9")]
  out = run("versionList", trips, rel)
  assert [r["commit"] for r in out] == ["b2", "a1", "zz9"]  # the channel's order, then what only the drives know
  assert out[0]["trips"] == 2 and out[0]["km"] == 40 and out[0]["before"] == "a1"
  assert out[1]["trips"] == 0 and out[1]["before"] is None and out[2]["title"] is None


# ----- the copilot's events (owner, 03/11): what just happened, with the time it was read

def run_drive(samples, st=None, t0=0):
  """samples: (seconds, live) in order; returns every event fired and the updates, by running eventStep like the page does."""
  script = f"""const L = require({json.dumps(str(LOGIC))}); let st = null; const evs = [], ups = [];
  for (const [s, l] of {json.dumps(samples)}) {{ const r = L.eventStep(st, l, {t0} + s * 1000); st = r.st; evs.push(...r.events); ups.push(...r.updates); }}
  process.stdout.write(JSON.stringify({{ evs, ups }}));"""
  out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
  return json.loads(out)


def car(v, lead=None, **kw):
  return {"driving": True, "v": v, "engaged": True, "steering": True, "lead": lead, "blinker": "", **kw}


def test_the_car_ahead_pulling_away_is_an_event_and_you_leaving_completes_it():
  stop = [(i, car(0, {"d": 6, "v": 0})) for i in range(5)]
  r = run_drive([(-2, car(40)), (-1, car(20)), *stop, (5, car(0, {"d": 7, "v": 8})), (6, car(0, {"d": 10, "v": 12})), (7, car(0, {"d": 12, "v": 13})),
                 (8, car(5, {"d": 14, "v": 15}))])  # a reading a second, as the page reads
  kinds = [e["kind"] for e in r["evs"]]
  assert kinds == ["stop_lead", "pull_away"]  # the stop behind it, then the car ahead leaving: once
  pull = r["evs"][1]
  assert pull["t"] == 5000 and pull["data"]["d"] == 6
  assert r["ups"] == [{"id": pull["id"], "data": {"go_s": 3.0}}]  # you left 3 s after it


def test_a_lead_that_does_not_move_is_no_event_and_a_short_stop_does_not_arm():
  r = run_drive([(-1, car(30)), *[(i, car(0, {"d": 6, "v": 0})) for i in range(30)]])
  assert [e["kind"] for e in r["evs"]] == ["stop_lead"]
  r = run_drive([(0, car(0, {"d": 6, "v": 0})), (1, car(0, {"d": 9, "v": 6}))])  # 1 s behind it: not armed yet
  assert r["evs"] == []


def test_the_car_ahead_braking_needs_its_deceleration_close_and_a_real_drop():  # route 32 (CTO review of app-11)
  follow = [(i, car(50, {"d": 30, "v": 50, "a": 0.})) for i in range(3)]
  r = run_drive([*follow, (3, car(50, {"d": 28, "v": 45, "a": -0.6})), (4, car(48, {"d": 25, "v": 40, "a": -0.9}))])
  assert [e["kind"] for e in r["evs"]] == ["lead_brake"]
  assert r["evs"][0]["data"]["from"] == 50 and r["evs"][0]["data"]["to"] == 40
  # what fired on route 32: the speed of a car 35–60 m ahead swinging, with no deceleration behind it
  swing = [(3, car(50, {"d": 30, "v": 38, "a": 0.})), (4, car(50, {"d": 30, "v": 52, "a": -0.1})), (5, car(50, {"d": 30, "v": 40, "a": 0.}))]
  noise = run_drive([*follow, *swing])
  assert noise["evs"] == []
  far = run_drive([(i, car(70, {"d": 55, "v": 70 - 6 * i, "a": -1.})) for i in range(5)])  # beyond 40 m: too noisy to tell
  assert far["evs"] == []
  other = run_drive([*follow, (3, car(50, {"d": 15, "v": 38, "a": -0.6})), (4, car(50, {"d": 13, "v": 37, "a": -0.6}))])  # another car:
  assert [e["kind"] for e in other["evs"]] == ["cut_in"]  # it cut in; its speed isn't the first one's braking
  old = run_drive([*follow, (3, car(50, {"d": 28, "v": 44})), (4, car(48, {"d": 25, "v": 38}))])  # a comma that doesn't send a
  assert old["evs"] == []
  slow = run_drive([(i, car(50, {"d": 30, "v": 50 - 2 * i, "a": -0.6})) for i in range(10)])  # 2 km/h a second: not braking hard
  assert slow["evs"] == []
  crawl = run_drive([(0, car(15, {"d": 10, "v": 30, "a": -2.})), (1, car(15, {"d": 9, "v": 10, "a": -2.}))])  # you below 20 km/h
  assert crawl["evs"] == []


def test_a_cut_in_and_not_your_own_lane_change():
  r = run_drive([(0, car(60)), (1, car(60)), (2, car(60, {"d": 18, "v": 55})), (3, car(60, {"d": 17, "v": 55}))])
  assert [e["kind"] for e in r["evs"]] == ["cut_in"]
  assert r["evs"][0]["t"] == 2000  # it counts from when it showed up, once the next reading saw it too
  closer = run_drive([(0, car(60, {"d": 45, "v": 60})), (1, car(60, {"d": 20, "v": 55})), (2, car(60, {"d": 19, "v": 55}))])
  assert [e["kind"] for e in closer["evs"]] == ["cut_in"]
  # CTO review of app-11: closing in on a slower or stopped car is no cut-in, nor is a reading that flickers closer
  approach = run_drive([(0, car(50, {"d": 45, "v": 0})), (1, car(50, {"d": 31, "v": 0})), (2, car(50, {"d": 17, "v": 0}))])
  assert approach["evs"] == []
  flicker = run_drive([(0, car(50, {"d": 19, "v": 48})), (1, car(50, {"d": 8, "v": 48})), (2, car(50, {"d": 20, "v": 48}))])
  assert flicker["evs"] == []
  mine = run_drive([(0, car(60, blinker="esquerda")), (2, car(60)), (3, car(60, {"d": 18, "v": 55}))])  # you changed lanes
  assert mine["evs"] == []
  far = run_drive([(0, car(60)), (1, car(60, {"d": 60, "v": 50}))])  # catching up with a far car
  assert far["evs"] == []
  blink = run_drive([(0, car(60, {"d": 20, "v": 58})), (1, car(60)), (2, car(60, {"d": 19, "v": 58}))])  # the same car, lost for a reading
  assert blink["evs"] == []
  first = run_drive([(0, car(60, {"d": 18, "v": 55}))])  # the first reading of the page: nothing before it to compare
  assert first["evs"] == []


def test_stops_long_stops_and_disengaging():
  r = run_drive([(0, car(40)), (1, car(0)), *[(1 + i, car(0)) for i in range(1, 62)], (63, car(5))])
  assert [e["kind"] for e in r["evs"]] == ["stop_free", "stop_long", "go_free"]
  r = run_drive([(0, car(50)), (1, car(50, steering=False, brake=True))])
  assert [(e["kind"], e["data"]["by"]) for e in r["evs"]] == [("disengage", "freio")]
  assert run_drive([(0, car(3)), (1, car(3, steering=False))])["evs"] == []  # parking speed: not this
  # always-on steering: the brake pauses the speed control, the steering stays, `enabled` stays: nothing let go (route 32)
  assert run_drive([(0, car(50)), (1, car(50, brake=True))])["evs"] == []
  # the car off resets everything: a stop after it is not an arrival
  assert run_drive([(0, car(40)), (1, {"driving": False}), (2, car(0))])["evs"] == []


def test_after_a_gap_nothing_fires_late_as_now():  # CTO review of app-11: page hidden, Wi-Fi gone
  assert run_drive([(0, car(40)), (150, car(0))])["evs"] == []  # it stopped some time in those 150 s: not an arrival now
  assert run_drive([(0, car(50)), (150, car(50, steering=False, gas=True))])["evs"] == []
  assert [e["kind"] for e in run_drive([(0, car(40)), (2, car(40)), (4, car(0))])["evs"]] == ["stop_free"]  # 2 s apart: as always


def test_the_car_ahead_leaving_with_you_creeping_or_in_the_same_reading():  # CTO review of app-11
  stop = [(-1, car(40)), *[(i, car(0, {"d": 6, "v": 0})) for i in range(5)]]
  r = run_drive([*stop, (5, car(5, {"d": 10, "v": 12}))])  # you and it in the same reading
  assert [(e["kind"], e["data"].get("go_s")) for e in r["evs"]] == [("stop_lead", None), ("pull_away", 0)]
  r = run_drive([*stop, (5, car(2, {"d": 7, "v": 8})), (8, car(5, {"d": 14, "v": 15}))])  # creeping at 2 km/h is still stopped
  assert [e["kind"] for e in r["evs"]] == ["stop_lead", "pull_away"] and r["ups"][0]["data"] == {"go_s": 3.0}
  r = run_drive([*stop, (5, car(0)), (6, car(0, {"d": 6, "v": 0}))])  # it vanished for one reading: still there
  assert [e["kind"] for e in r["evs"]] == ["stop_lead"]


def test_a_stale_reading_is_not_fed():
  html = (LOGIC.parent / "index.html").read_text()
  feed = html[html.index("function renderDrive()"):]
  feed = feed[:feed.index("renderCop();")]
  assert "if (!b.stale) { const r = LPL.eventStep(" in feed  # old data neither fires nor resets an event


def test_a_point_seen_close_ahead_then_behind_was_passed():
  p = {"key": "k1", "kind": "radar", "limit": 60, "ahead": True, "distance": 120, "confirmed": False, "mine": False}
  script = f"""const L = require({json.dumps(str(LOGIC))}); const P = {json.dumps(p)};
  let a = L.pointPassed(null, [P], 0); a = L.pointPassed(a.st, [{{ ...P, distance: 45 }}], 4000);
  const b = L.pointPassed(a.st, [{{ ...P, ahead: false, distance: 40 }}], 9000);
  const far = L.pointPassed(null, [{{ ...P, distance: 2000 }}], 0);
  const c = L.pointPassed(far.st, [{{ ...P, ahead: false }}], 9000);
  const away = L.pointPassed(L.pointPassed(null, [{{ ...P, distance: 250 }}], 0).st, [{{ ...P, ahead: false, distance: 255 }}], 9000);
  const still = L.pointPassed(a.st, [{{ ...P, ahead: false, distance: 40 }}], 9000, false);
  process.stdout.write(JSON.stringify([a.events, b.events, c.events, away.events, still.events]));"""
  a, b, c, away, still = json.loads(subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout)
  assert a == [] and c == []  # seen only far away: nothing to ask
  assert away == [] and still == []  # never closer than 250 m (another street), or the car not moving (the heading wanders)
  assert len(b) == 1 and b[0]["kind"] == "radar"
  assert b[0]["data"] == {"key": "k1", "kind": "radar", "limit": 60, "side": None, "confirmed": False, "mine": False}


def test_the_big_card_is_the_newest_recent_event_and_ages_go_by_stage():
  evs = [{"id": "a", "kind": "stop_lead", "t": 0}, {"id": "b", "kind": "lead_brake", "t": 100_000},
         {"id": "c", "kind": "cut_in", "t": 110_000, "answer": {"r": "ok"}}]
  v = run("copilotView", evs, 120_000, None, 0)
  assert v["main"]["id"] == "b" and v["stage"] == "live" and not v["review"]  # c is answered: never back on screen
  assert [(e["id"], e["stage"]) for e in v["pending"]] == [("a", "past")]  # 2 min old: grey, in the list
  later = run("copilotView", evs, 300_000, None, 0)
  assert later["main"] is None and [e["stage"] for e in later["pending"]] == ["past", "past"]  # waiting: nothing to skip
  picked = run("copilotView", evs, 300_000, "a", 290_000)
  assert picked["main"]["id"] == "a" and picked["stage"] == "past" and picked["review"]
  newer = run("copilotView", evs + [{"id": "d", "kind": "pull_away", "t": 295_000}], 300_000, "a", 290_000)
  assert newer["main"]["id"] == "d"  # a newer event takes the card back from the review
  assert run("eventClock", {"t": 0}, 64_000) == {"short": True, "m": 1, "s": 4}


def test_an_event_answers_an_open_mission_first_on_the_drive():
  cat = {"missions": [{"key": "sair-sozinho"}, {"key": "fila"}, {"key": "frear-antes", "lock": "x"}]}
  ev = {"kind": "pull_away"}
  assert run("eventMission", ev, cat, ["fila"], {}) == "fila"
  assert run("eventMission", ev, cat, [], {}) is None  # not taken on this drive: its option may be off (CTO review of app-11)
  assert run("eventMission", ev, cat, ["sair-sozinho"], {}) == "sair-sozinho"
  assert run("eventMission", ev, cat, [], {"sair-sozinho": {"full": True}, "fila": {"full": True}}) is None
  assert run("eventMission", {"kind": "lead_brake"}, cat, [], {}) is None  # locked: not this one
  assert run("eventMission", {"kind": "cut_in"}, cat, [], {}) is None


def test_the_log_keeps_the_newest_and_merges_updates():
  evs = [{"id": str(i), "kind": "cut_in", "t": i, **({"answer": {"r": "ok"}} if i % 2 else {})} for i in range(40)]
  out = run("eventsMerge", evs, [{"id": "new", "kind": "stop_long", "t": 99}], [{"id": "0", "data": {"go_s": 2}}])
  assert len(out) == 40 and out[-1]["id"] == "new" and out[0]["data"]["go_s"] == 2
  assert "1" not in [e["id"] for e in out]  # the oldest answered one went first


def test_every_event_kind_has_its_texts_and_the_card_keeps_one_height():
  html = (LOGIC.parent / "index.html").read_text()
  kinds = ["lead_brake", "pull_away", "stop_lead", "stop_free", "go_free", "stop_long", "cut_in", "disengage", "bump", "radar"]
  table = html[html.index("const EVENT_UI = {"):html.index("const STAGE_TEXT")]
  assert all(f"  {k}: {{ title:" in table for k in kinds)
  css = html[html.index(".evcard {"):]
  assert css.index("height: 392px") < css.index("}")  # fixed: the answers stay where the finger is, whatever happened
  assert "evbtns" in html and "evthird" in html
  # the clock ticks in place: the card is built again only when what it says changes (a tap is never lost to a rebuild)
  render = html[html.index("function renderCop()"):html.index("function renderDriveMissions(")]
  assert "main.dataset.sig !== msig" in render and "evClock(main.firstChild" in render
  # an answer only opens the note: Salvar is what saves
  tap = html[html.index("function evTap("):html.index("function openEv(")]
  assert "api(" not in tap


def test_events_from_the_drives_log_join_the_phones_once():
  # timeline.py "cop": seconds from the drive's start, the start being its end minus how long it ran (owner, 03/11)
  trip = {"id": "0000000f--8784034901", "mtime": 10_000, "minutes": 12, "hidden": False,
          "summary": {"s": 700, "cop": [{"s": 63.1, "kind": "pull_away", "v": 0, "data": {"d": 7, "go_s": 4.0}},
                                        {"s": 200.0, "kind": "stop_lead", "v": 0, "data": {"d": 2}}]}}
  phone = [{"id": "p1", "kind": "stop_lead", "t": (10_000 - 700 + 210) * 1000, "answer": {"r": "ok"}}]  # the phone saw it, 10 s off
  out = run("logEvents", [trip], phone, 0)
  assert [(e["kind"], e["src"], e["t"]) for e in out] == [("pull_away", "log", round((10_000 - 700 + 63.1) * 1000))]
  assert out[0]["id"] == "0000000f--8784034901-63.1-pull_away" and out[0]["data"] == {"d": 7, "go_s": 4.0}
  assert run("logEvents", [trip], phone + out, 0) == []  # once
  assert run("logEvents", [{**trip, "hidden": True}], [], 0) == [] and run("logEvents", [trip], [], 10_000_000) == []  # hidden, older


def test_the_review_asks_about_letting_go_only_without_your_pedal():
  html = (LOGIC.parent / "index.html").read_text()
  rel = html[html.index("function evRelevant(e)"):]
  rel = rel[:rel.index("\n}\n")]
  assert '["freio", "acelerador"].includes' in rel and "evMission(e)" in rel


def test_the_drives_clock_starts_where_its_last_minute_ended():
  # CTO review of app-12: "s" is the time driven, so a minute missing in the middle moved every event of the drive
  trip = {"id": "r", "mtime": 10_000, "minutes": 11, "hidden": False,
          "summary": {"s": 600, "end_s": 720, "cop": [{"s": 100.0, "kind": "stop_lead", "v": 0, "data": {}}]}}
  assert run("logEvents", [trip], [], 0)[0]["t"] == (10_000 - 720 + 100) * 1000


def _drive(rid, end_ms, n):
  cop = [{"s": float(60 * (i + 1)), "kind": ("stop_lead", "pull_away", "cut_in", "lead_brake")[i % 4], "v": 30, "data": {}} for i in range(n)]
  return {"id": rid, "mtime": end_ms / 1000, "minutes": n + 2, "hidden": False, "summary": {"s": 60 * (n + 2), "end_s": 60 * (n + 2), "cop": cop}}


def test_the_review_keeps_its_answers_with_more_events_than_the_phone_keeps():
  # CTO review of app-12: the log's events merged into the phone's list (EV_KEEP 40) swapped one drive for the other on
  # every render, and the answers went with them. The list is built on the fly, the answers kept apart
  now, H = 1_800_000_000_000, 3_600_000
  trips = [_drive("r2", now - H, 40), _drive("r1", now - 20 * H, 40)]
  since = now - 48 * H
  first = run("reviewList", [], trips, {}, since)
  assert len(first) == 80 and len({e["id"] for e in first}) == 80
  answers = {}
  for e in first[-3:]:
    answers = run("answerKeep", answers, e, "ok", now, since)
  again = run("reviewList", [], trips, answers, since)
  assert len(again) == 77 and not any(e["id"] in answers for e in again)  # answered: never back as pending
  assert run("reviewList", [], trips, answers, since) == again  # the same list on every render


def test_a_log_copy_of_an_event_answered_on_the_phone_and_dropped_there_never_comes_back():
  now = 1_800_000_000_000
  trip = _drive("r", now, 2)
  t0 = now - trip["summary"]["end_s"] * 1000
  phone = {"id": "p1", "kind": "stop_lead", "t": t0 + 65_000, "v": 0, "data": {}}  # the log's first, 5 s off
  answers = run("answerKeep", {}, phone, "ok", now, 0)
  out = run("reviewList", [], [trip], answers, 0)  # the phone's list no longer has it (EV_KEEP_H, EV_KEEP)
  assert [e["kind"] for e in out] == ["pull_away"]
  live = run("reviewList", [{**phone, "answer": None}], [trip], answers, 0)  # still on the phone: it shows answered
  assert live[0]["id"] == "p1" and live[0]["answer"]["r"] == "ok"


def test_the_answers_kept_apart_stay_bounded():
  answers = run("answerKeep", {}, {"id": "old", "kind": "stop_lead", "t": 1_000}, "ok", 5, 0)
  answers = run("answerKeep", answers, {"id": "new", "kind": "stop_lead", "t": 1_449}, "ok", 5, 1_100)
  assert set(answers) == {"new"}  # older than the window: out
  big = {f"e{i}": {"id": f"e{i}", "kind": "x", "t": i, "r": "ok", "at": 0} for i in range(500)}
  kept = run("answerKeep", big, {"id": "new", "kind": "x", "t": 1_000}, "ok", 0, 0)
  assert len(kept) == 400 and "new" in kept and "e0" not in kept


def test_the_review_lists_never_store_the_logs_events_with_the_phones():
  html = (LOGIC.parent / "index.html").read_text()
  assert "evAddLog" not in html and "LPL.reviewList(evList, trips, evAnswers" in html
  done = html[html.index("function evDone(id, r)"):]
  done = done[:done.index("\n}\n")]
  assert "LPL.answerKeep(" in done and "ansSave()" in done
  assert '"lpCopAnswers"' in LOGIC.read_text() and 'EV_ANS_STORE = "lpCopAnswers"' in html


def test_the_review_keeps_the_event_you_picked_with_the_car_off():
  html = (LOGIC.parent / "index.html").read_text()
  render = html[html.index("function render(s, copy, ep)"):]
  render = render[:render.index("\n}\n")]
  assert "if (!copReview) evPick = null;" in render and "passSt = null; evPick = null;" not in render


def test_two_close_events_of_the_log_stay_two_and_a_phone_event_takes_one():
  # CTO review of app-12: the log's events went into the comparison list as they were added, so two cut-ins 12 s apart (the
  # detector's cooldown is 10 s) became one; a phone event stands for one of the log's, the nearest
  trip = {"id": "r", "mtime": 10_000, "minutes": 5, "hidden": False,
          "summary": {"t0": 9_700, "cop": [{"s": 100.0, "kind": "cut_in", "v": 40, "data": {"d": 9}},
                                           {"s": 112.0, "kind": "cut_in", "v": 40, "data": {"d": 11}}]}}
  assert [e["t"] for e in run("logEvents", [trip], [], 0)] == [9_800_000, 9_812_000]
  phone = [{"id": "p", "kind": "cut_in", "t": 9_813_000}]
  assert [e["t"] for e in run("logEvents", [trip], phone, 0)] == [9_800_000]


def test_the_drives_start_is_the_commas_clock_first():
  assert run("driveStart", {"mtime": 10_000, "minutes": 5, "summary": {"t0": 9_650.5, "end_s": 300, "s": 290}}) == 9_650.5
  assert run("driveStart", {"mtime": 10_000, "minutes": 5, "summary": {"end_s": 300, "s": 290}}) == 9_700
  assert run("driveStart", {"mtime": 10_000, "minutes": 5, "summary": {}}) == 9_700


def test_the_review_answers_and_fiz_agora_keep_their_moment():
  html = (LOGIC.parent / "index.html").read_text()
  render = html[html.index("function render(s, copy, ep)"):]
  render = render[:render.index("\n}\n")]
  assert "if (evAns && carNow().moving && !passengerOk) closeEv();" in render  # a note never rides along unconfirmed
  save = html[html.index("async function saveEv()"):]
  save = save[:save.index("\n}\n")]
  assert "Date.now() - a.tapped" in save and "!carOn ? { at: Math.round(ev.t / 100) / 10 }" in save
  assert html.count("openEv(null, \"done\", EV_BUTTONS.done, m.key, at)") == 2
  cop = html[html.index("function renderCop()"):]
  assert '$("copTitle").textContent = t(copReview ? "A conferir" : "Copiloto")' in cop[:cop.index("\n}\n")]


def test_a_kind_turned_off_and_a_confirmed_point_stay_out_of_the_big_card_but_are_kept():
  # owner, 04/11: "registra, identifica, mas só deixa de aparecer no quadrado grande"
  evs = [{"id": "a", "kind": "stop_lead", "t": 100_000}, {"id": "b", "kind": "radar", "t": 101_000, "data": {"confirmed": True}},
         {"id": "c", "kind": "radar", "t": 102_000, "data": {"confirmed": False}}, {"id": "d", "kind": "pull_away", "t": 103_000}]
  v = run("copilotView", evs, 105_000, None, 0, ["pull_away", "stop_lead"])
  assert v["main"]["id"] == "c"  # the newest that isn't quiet: the point still to confirm
  assert [e["id"] for e in v["quiet"]] == ["d", "b", "a"] and v["pending"] == []
  assert run("copilotView", evs, 105_000, "d", 104_000, ["pull_away"])["main"]["id"] == "d"  # picked from the list: shown
  assert run("copilotView", evs, 105_000, None, 0, [])["main"]["id"] == "d"  # nothing turned off: as before
  assert run("mutedKinds", ["stop_lead", "nope", "stop_lead", "radar"]) == ["stop_lead", "radar"]


def test_no_function_is_declared_twice_in_the_page():
  # a merge that brings two "function evTitle" keeps only the last one, silently (05/11: the channel's evTitle(ev) and app-13's evTitle(kind))
  import collections
  import re
  names = re.findall(r"^\s*(?:async\s+)?function\s+(\w+)\s*\(", (LOGIC.parent / "index.html").read_text(), re.M)
  assert [n for n, c in collections.Counter(names).items() if c > 1] == []


def test_a_kind_is_titled_by_kind_title_and_an_event_by_ev_title():
  # evTitle(ev) reads ev.kind and ev.data: given a kind (a string) it says nothing. The "Silenciados" list holds kinds (CTO, 05/11)
  html = (LOGIC.parent / "index.html").read_text()
  muted = html[html.index('tf("Silenciados · {n}"'):]
  muted = muted[:muted.index("out.push(list)")]
  assert "kindTitle(k)" in muted and "evTitle(" not in muted
  assert "evTitle(k)" not in html and "evTitle(kind)" not in html and "evTitle(a.kind)" not in html


def test_skip_and_turning_a_kind_off_never_mark_and_turning_off_asks_first():
  html = (LOGIC.parent / "index.html").read_text()
  skip = html[html.index("function evSkip(ev)"):html.index("function openMute(")]
  assert "api(" not in skip and 'evDone(ev.id, "skip")' in skip and "Desfazer" not in skip  # the button text lives in the HTML
  ask = html[html.index("function openMute("):html.index("function closeMute(")]
  assert "evMuted =" not in ask and "mutedSave" not in ask  # the box only
  save = html[html.index("function saveMute()"):html.index("function copRadarStrip(")]
  assert "mutedSave()" in save
  assert '"lpCopMuted"' not in (LOGIC.read_text())  # a preference: "Apagar dados" leaves it


def test_marking_is_two_steps_with_nothing_to_scroll_and_the_text_box_on_top():
  # owner, 04/11: "tudo ao alcance do dedo, sem ter que rolar a tela"; the keyboard lost the scroll in the old box
  html = (LOGIC.parent / "index.html").read_text()
  sheet = html[html.index('id="markSheet"'):]
  sheet = sheet[:sheet.index("</section>")]
  one, two = sheet[sheet.index('id="markStep1"'):sheet.index('id="markStep2"')], sheet[sheet.index('id="markStep2"'):]
  assert "<textarea" not in one and 'id="reasons"' in one and "<textarea" in two and 'id="markWin"' in two  # no keyboard on step 1
  assert two.index('id="markSave"') > two.index("<textarea") and 'class="duo"' in two
  pick = html[html.index("function drawReasons()"):html.index("function drawPointKinds(")]
  assert "markStep(2, !tile && markTyping())" in pick  # one tap on a reason: on to the text; a group asks which first
  # the keyboard opens by itself only parked or in the copilot; moving, the box still leaves by itself untouched (CTO, 05/11)
  assert "function markTyping() { return carStopped || copOpen; }" in html
  opening = html[html.index("function openMarkNow("):html.index("const MARK_WIN =")]
  assert "markStep(2, !editing.id && markTyping())" in opening and "if (!markTyping()) markTimer = setTimeout(closeMark, EDITOR_MS)" in opening
  step = html[html.index("function markStep("):html.index("// Nine buttons")]
  assert "picked = k; markStep(2, markTyping())" in step  # the second tap, on "Qual?", says which and opens the text
  assert "\n  clearTimeout(markTimer);" not in step  # opening step 2 alone doesn't stop the timer: a tap or the text's focus does
  assert '$("markSheet").classList.toggle("up", two)' in html and 'class="sheet up hidden" id="evSheet"' in html  # above the keyboard
  assert "window: MARK_WIN_S[markWin]" in html


def test_marking_has_nine_buttons_and_what_comes_in_pairs_asks_which():
  # owner, 05/11: "alguns que são dois botões, a gente pode fazer um botão e a segunda etapa faz a diferença"
  html = (LOGIC.parent / "index.html").read_text()
  tiles = 'mapa: "Radar ou lombada", estrada_g: "Na estrada", balanco_g: "Balançou", balancou: "sem motivo", estrada: "estrada ruim"'
  assert "const TILE_TEXT = { " + tiles + " };" in html
  grid = html[html.index("function drawReasons()"):html.index("function drawPointKinds(")]
  assert "!r.group && !inTile.has(r.key)" in grid  # a reason inside a button never shows twice, and "anotação" (its own group) stays out
  sheet = html[html.index('id="markSheet"'):]
  one, two = sheet[sheet.index('id="markStep1"'):sheet.index('id="markStep2"')], sheet[sheet.index('id="markStep2"'):sheet.index("</section>")]
  assert 'id="pointKinds"' in two and 'id="pointKinds"' not in one and 'id="markSub"' in two  # the points and "Qual?" are the second step
  assert 'const MARK_WIN = { now: "Agora", b10: "10 s antes", b30: "30 s antes", a30: "30 s depois" };' in html  # one line each


def test_to_review_shows_the_newest_three_and_the_rest_behind_one_tap():
  # owner, 04/11: "tem que ser só 3 e o outro um 3 pontinhos que você abre o resto"
  evs = [{"id": f"e{i}", "kind": "stop_lead", "t": 1000 - i} for i in range(7)]
  g = run("groupPending", list(reversed(evs)), None)
  assert [e["id"] for e in g["shown"]] == ["e0", "e1", "e2"]  # newest first, whatever the order they came in
  assert [e["id"] for e in g["rest"]] == ["e3", "e4", "e5", "e6"]
  assert run("groupPending", evs[:2], None)["rest"] == []
  html = (LOGIC.parent / "index.html").read_text()
  assert 'tf("··· mais {n}"' in html and "evShowRest" in html



# ----- two layers (owner, 05/11): the owner's part counts at once; the CTO accepts or sends it back

def _fila(tries, pending):
  return run("missionProgress", {"missions": [_m("fila", "marca", 2, 0) | {"tries": tries}]}, {"pending": pending}, [])["fila"]


def test_a_try_ok_accepts_and_a_back_returns_with_its_reason():
  mark = {"key": "fila", "mark": "x", "t": 1_762_300_000}  # 2025-11-04
  f = _fila([], [mark])
  assert f["d"] == 1 and f["byYou"] == 1 and f["accepted"] == 0 and f["returned"] is None
  back = {"date": "2026-11-05T12:00", "route": "rota 40", "version": "abc", "result": "back", "measured": "o log não tem a parada"}
  f = _fila([back], [mark])
  assert f["d"] == 0 and f["byYou"] == 0 and f["returned"] == {"date": "2026-11-05T12:00", "reason": "o log não tem a parada"}
  f = _fila([back], [mark, {"key": "fila", "mark": "y", "t": 1_800_000_000}])  # done again after it came back
  assert f["returned"] is None and f["byYou"] == 1
  bad = dict(back, result="bad", measured="freou tarde")
  assert _fila([bad], [mark])["returned"]["reason"] == "freou tarde"
  ok = dict(back, result="ok")
  assert _fila([ok], [mark])["returned"] is None
  base = dict(back, result="base")
  assert _fila([back, base], [mark])["returned"] is not None  # a reference is no verdict


def test_a_no_deu_mark_never_counts():
  f = _fila([], [{"key": "fila", "mark": "x", "t": 1, "result": "bad"}])
  assert f["d"] == 0 and f["fails"] == 1


def test_200_km_in_one_week_whatever_the_versions():
  m = {"missions": [_m("km", "auto", detect="km200")]}
  week = [trip(km=70, commit=c) for c in ("a1", "b2", "c3")]  # 210 km, three versions, one day
  assert run("missionProgress", m, {}, week)["km"]["full"]
  assert not run("missionProgress", m, {}, week[:2])["km"]["full"]


def test_the_page_behind_a_layer_stays_put():
  # owner, 05/11: in the copilot the page behind scrolled (iOS moves the document under a fixed layer whose content doesn't scroll)
  html = (LOGIC.parent / "index.html").read_text()
  assert ".cop, .sheet { overscroll-behavior: contain; }" in html and "body.locked { position: fixed;" in html
  lock = html[html.index("function syncLock()"):]
  lock = lock[:lock.index("layerWatch.observe")]
  assert 'document.querySelectorAll(".cop, .sheet")' in lock and "window.scrollTo(0, y)" in lock  # every layer; back to the same place
  layers = re.findall(r'<section class="(?:sheet|cop)[^"]*" id="(\w+)"', html)
  assert len(layers) >= 8 and "copSheet" in layers and "markSheet" in layers  # the observer watches them all, present and future, by class
  assert 'class="row cophead"' in html  # the copilot's title and Fechar stay on top while its list scrolls


def test_the_map_centres_and_reads_back_the_place_under_the_pin():
  v = run("mapCenter", -25.3848, -49.2763, 16, 430, 600)
  assert v["z"] == 16 and abs(v["size"] - 256) < 1e-6
  js = "const L = require(" + json.dumps(str(LOGIC)) + ");" + """ const v = L.mapCenter(-25.3848, -49.2763, 16.5, 430, 600);
  const c = L.mapXY(v, -25.3848, -49.2763); const back = L.mapLatLon(v, c[0], c[1]); const moved = L.mapLatLon(v, c[0] + 100, c[1]);
  process.stdout.write(JSON.stringify({ c, back, moved }));"""
  out = json.loads(subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30, check=True).stdout)
  assert [round(x) for x in out["c"]] == [215, 300]  # the centre is the middle of the box
  assert out["back"] == [-25.3848, -49.2763] and out["moved"][1] > -49.2763  # dragged right of the middle: further east
  assert 1100 < run("distanceM", [-25.3848, -49.2763], [-25.3948, -49.2763]) < 1120  # 0.01° of latitude, ~1.1 km


def test_a_place_reads_short():
  # the shape Nominatim gave on 05/11, with a public place and a placeholder CEP: the whole name over the title
  long = "Jardim Botânico de Curitiba, Rua Engenheiro Ostoja Roguski, Região Geográfica Imediata de Curitiba, Curitiba, Paraná, Região Sul, 00000-000, Brasil"
  assert run("shortPlace", long) == {"title": "Jardim Botânico de Curitiba", "sub": "Rua Engenheiro Ostoja Roguski, Curitiba"}
  assert run("shortPlace", "Praia de Icaraí, Icaraí, Niterói") == {"title": "Praia de Icaraí", "sub": "Icaraí, Niterói"}
  assert run("shortPlace", "-25.38480, -49.27630") == {"title": "-25.38480, -49.27630", "sub": ""}
  assert [run("maneuverKind", *a) for a in [("turn", "right"), ("turn", "slight left"), ("arrive", ""), ("turn", "uturn"), ("depart", "")]] == \
    ["right", "slight-left", "arrive", "uturn", "straight"]


def test_the_route_lives_on_the_mapa_and_every_change_asks():
  # owner, 05/11: the route card had taken Início's top and hid the driver's panel; "toque e segure" doesn't exist on iOS
  html = (LOGIC.parent / "index.html").read_text()
  assert 'id="navCard"' not in html and 'id="homeSeg"' in html and 'data-home="mapa"' in html
  assert "contextmenu" not in html and "prompt(" not in html[html.index("// ----- the route"):html.index("// ----- the road's limit")]
  js = html[html.index("// ----- the route"):html.index("// ----- the road's limit")]
  assert js.count("navConfirm(") >= 3 and "openGo(" in js  # change, end; "Ir para cá" asks which app; andando the passenger first
  assert 'askPassenger(() => askSheet(title, what, ok, run, passengerFirst), false, passengerFirst === "mapa" ? "mapa" : undefined)) return;' in js
  assert 'askSheet(title, what, ok, run, "mapa")' in js
  # owner's D2 (MAP-UX-01, 06/11): a saved place goes straight to "Quem mostra o caminho", the box that confirms; never straight to a route
  assert 'b.addEventListener("click", () => goFromList(p));' in js and "openGo({ ...navPickP });" in js[js.index("function goFromList("):]
  assert 'id="navToken"' in html[html.index('data-subview="comma"'):]  # the Mapbox key went to Ajustes → Comma


def test_usar_and_melhorar_one_app_and_the_sending_follows():
  # owner, 05/11: a switch, not a level; Usar shows the panel, the map and the drives; Melhorar the whole app; the sending follows
  assert [run("appMode", m, e) for m, e in [(None, "generic"), (None, "h6gt"), ("usar", "h6gt"), ("melhorar", "generic"), ("x", None)]] == \
    ["usar", "melhorar", "usar", "melhorar", "melhorar"]
  opt = {"key": "HexaShareEnabled", "value": False, "writable": True}
  assert run("shareFor", "melhorar", opt) is True and run("shareFor", "usar", opt) is None
  assert run("shareFor", "usar", {**opt, "value": True}) is False and run("shareFor", "melhorar", None) is None  # no such option: nothing
  assert run("shareFor", "melhorar", {**opt, "writable": False}) is None
  html = (LOGIC.parent / "index.html").read_text()
  assert "body.usar nav.tabs .fab, body.usar nav.tabs [data-go=\"pontos\"], body.usar .melhorar-only { display: none !important; }" in html
  for idn in ("pendingCard", "missionCard", "kpiBox", "driveMissions", "driveMore", "nextActs", "marksJump"):
    tag = html[html.rindex("<", 0, html.index(f'id="{idn}"')):html.index(">", html.index(f'id="{idn}"'))]
    assert "melhorar-only" in tag, idn
  js = html[html.index("// ----- Usar | Melhorar"):html.index("// ----- the route")]
  assert 'if (m === "usar") { setMode("usar"); return; }' in js and "askSheet(t(\"Ajudar a melhorar o LizPilot?\")" in js  # in asks, out doesn't


def test_the_copilot_is_a_panel_and_the_avisos_count_the_answers():
  # owner, 05/11: with no event the copilot shows how the car is driving; "pode silenciar" after 20 answers, all "deu certo"
  t = run("tallyAdd", {}, "stop_lead", "ok")
  for _ in range(19):
    t = run("tallyAdd", t, "stop_lead", "ok")
  assert t == {"stop_lead": {"ok": 20, "bad": 0}} and run("canSilence", t["stop_lead"]) is True
  assert run("canSilence", {"ok": 30, "bad": 1}) is False and run("canSilence", {"ok": 19, "bad": 0}) is False
  assert run("tallyAdd", t, "stop_lead", "nao") == t and run("tallyAdd", {}, "x", "ok") == {}  # "Não aconteceu" and unknown kinds count nothing
  html = (LOGIC.parent / "index.html").read_text()
  assert 'renderCopLive(v.main);' in html and ': copReview ? waitCard(now) : el("span", "")' in html  # no event: the panel, not "Esperando"
  assert 'sw.addEventListener("click", () => { closeAvisos(); openMute(kind, on); });' in html  # on or off, it asks first
  keys = json.loads(subprocess.run([NODE, "-e", f"process.stdout.write(JSON.stringify(require({json.dumps(str(LOGIC))}).DATA_KEYS))"],
                                   capture_output=True, text=True, timeout=30, check=True).stdout)
  assert "lpCopTally" in keys  # "Apagar dados deste celular" takes the counts too


def test_pular_is_in_the_card_and_the_list_is_one_line_while_driving():
  # owner, 05/11: "tem que ter um pular" in the card; "a lista embaixo de eventos antigos [...] está muito grande"
  html = (LOGIC.parent / "index.html").read_text()
  card = html[html.index("function evCard("):html.index("function waitCard(")]
  assert 'skip = el("button", "evthird", t("Pular"))' in card and "evSkip(ev)" in card and "EV_TAP_GUARD_MS" in card
  assert 'id="copSkip"' not in html  # one Pular, in the card, next to the third answer
  cop = html[html.index("function renderCop()"):]
  assert "if (v.pending.length && !copReview && !evListOpen)" in cop and "evListOpen, L()]" in cop


def _js(expr: str):
  script = f"const L = require({json.dumps(str(LOGIC))}); process.stdout.write(JSON.stringify({expr}));"
  return json.loads(subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout)


def test_mapux_quem_mostra_o_caminho_puts_the_last_choice_first_and_says_why_the_comma_is_off():
  # owner's D4 and D5 (MAP-UX-01, 06/11): asked every time, the last choice first and the only green one; the comma always listed
  first = run("navOptions", None, True)
  assert [o["app"] for o in first] == ["waze", "google", "comma"] and [o["main"] for o in first] == [True, False, False]
  assert not any(o["last"] for o in first)  # nothing chosen yet: no "da última vez"
  last = run("navOptions", "google", True)
  assert [o["app"] for o in last] == ["google", "waze", "comma"] and last[0]["last"] and last[0]["main"] and not last[1]["main"]
  off = run("navOptions", "comma", False)  # the comma's route was the last one, but its navigation is off now: listed, off, not first
  assert [o["app"] for o in off] == ["waze", "google", "comma"] and off[2] == {"app": "comma", "on": False, "last": False, "main": False}
  assert run("navOptions", "outro", True)[0]["app"] == "waze" and all(o["on"] for o in run("navOptions", None, True))
  assert run("navChoice", "google", True) is None  # still asks every time (05/11)


def test_mapux_the_same_place_is_within_20_m_and_a_saved_one_is_known():
  a = {"latitude": -15.8052, "longitude": -47.8924}
  assert run("samePoint", a, {"latitude": -15.80525, "longitude": -47.89245}) is True
  assert run("samePoint", a, {"latitude": -15.8062, "longitude": -47.8924}) is False and run("samePoint", a, None) is False
  places = [{"name": "Casa", "latitude": -15.7801, "longitude": -47.9292}]
  assert run("savedPlace", places, {"name": "Rua X, Asa Norte", "latitude": -15.78011, "longitude": -47.92921})["name"] == "Casa"
  assert run("savedPlace", places, a) is None and run("savedPlace", None, a) is None


def test_mapux_the_mapa_shows_the_recent_places_not_the_saved_ones():
  # step 1: the saved places are the chips on top; the card lists the last places gone to, at most 3, and the way to edit the saved ones
  casa = {"name": "Casa", "latitude": -15.7801, "longitude": -47.9292}
  rec = [{"name": "Casa, Asa Norte", "latitude": -15.7801, "longitude": -47.9292}] + \
        [{"name": f"R{i}", "latitude": -15.7 - i / 100, "longitude": -47.9} for i in range(4)]
  m = run("mapCard", rec, [casa])
  assert [p["name"] for p in m["recent"]] == ["R0", "R1", "R2"] and m["edit"] is True and m["empty"] is False
  assert run("mapCard", [], []) == {"recent": [], "edit": False, "empty": True}  # nothing yet: the card says what can be done
  assert run("mapCard", None, [casa]) == {"recent": [], "edit": True, "empty": False}


def test_mapux_the_pin_sits_where_the_card_leaves_room():
  # step 4: the place in the middle of what the bar on top and the card below leave, never under the card; the pin is 52 px tall
  y = run("pinSpot", 600, 62, 220)
  assert y == 62 + (600 - 62 - 220) // 2 + 26 and 62 + 52 <= y <= 600 - 220
  assert run("pinSpot", 600, 300, 260) == 340  # a small phone, 40 px between them: the tip on the card's edge, never under it
  assert run("pinSpot", 600, 300, 320) == 300  # nothing of the map shows: the middle, as before
  c = run("centerAt", -15.8052, -47.8924, 16.5, 390, 600, 195, y)
  xy = _js(f"L.mapXY(L.mapCenter({c['lat']}, {c['lon']}, {c['zf']}, 390, 600), -15.8052, -47.8924)")
  assert abs(xy[0] - 195) < 1 and abs(xy[1] - y) < 1  # the map is centred so that the place is right under the pin's tip


def test_mapux_the_route_fits_where_nothing_covers_it():
  # step 6: the whole route between the bar on top, the card below and the zoom buttons on the right
  path = [[-15.7939, -47.8828], [-15.7939, -47.8924], [-15.8052, -47.8924]]
  band = {"top": 140, "bottom": 230, "right": 64}
  fit = f"L.fitBand({json.dumps(path)}, 390, 600, {json.dumps(band)}, 26)"
  xy = _js(f"((f) => {json.dumps(path)}.map((p) => L.mapXY(L.mapCenter(f.lat, f.lon, f.zf, 390, 600), p[0], p[1])))({fit})")
  assert all(25 <= x <= 390 - 64 - 25 and 140 + 25 <= y <= 600 - 230 - 25 for x, y in xy), xy
  assert run("fitBand", [], 390, 600, band, 26) is None


def test_mapux_the_mapa_is_the_passengers_and_every_change_still_confirms():
  html = (LOGIC.parent / "index.html").read_text()
  js = html[html.index("// ----- the route"):html.index("// ----- the road's limit")]
  # owner's D1: the Mapa's tab and the route's line ask first, once per trip, with the Mapa's words; Marcar keeps its own
  assert "e.target.closest('[data-home=\"mapa\"], [data-navline]')" in html and 'askPassenger(() => b.click(), false, "mapa")' in html
  assert 't("Com o carro andando, o mapa é do passageiro")' in html and 'id="passWhat"' in html
  assert 't("Quem dirige olha a estrada. O que você tocou já está guardado: a hora ou o lugar.")' in html
  # andando, everything that changes something asks first too (the phone may already be on the Mapa when the car leaves)
  for guard in ('if (askPassenger(() => openGo(p), false, "mapa")) return;', 'if (askPassenger(() => goFromList(p), false, "mapa")) return;',
                'if (askPassenger(openSwap, false, "mapa")) return;', 'if (askPassenger(save, false, "mapa")) return;',
                'function openPlacesAsk() { if (!askPassenger(openPlacesAsk, false, "mapa")) openPlaces(); }',
                'if (askPassenger(() => navSearch(), false, "mapa")) return;'):
    assert guard in js, guard
  # owner's D3: dragging only looks around; the destination moves only under "Ajustar o ponto"
  assert js.count("navPickP.latitude = at[0]") == 1 and "if (navAdjust) { const at = LPL.mapLatLon(v, w / 2, navAdjust.y);" in js
  # owner's D5: nothing over the map when the comma's navigation is off: Quem mostra o caminho says it, the option off
  assert 'id="mapMsg"' not in html and "b.disabled = !o.on;" in js
  assert ".mapfull.tight .mside { display: none; }" in html  # a small phone: + − ◎ never over the search bar
  # the destination already set isn't "changed" to itself; Trocar destino lists the saved and the recent places
  assert 'if (dest && LPL.samePoint(dest, p)) { toast(t("Já é o destino.")); closePick(); return; }' in js
  assert '$("navChange").addEventListener("click", openSwap);' in js and 'id="swapSheet"' in html


def test_ir_para_ca_asks_which_app_every_time():
  # owner, 05/11: Waze or Google Maps navigate (free, no key); the comma's route only with its navigation on; "toda vez [...] pergunta"
  assert run("navTargets", False) == ["waze", "google"] and run("navTargets", True) == ["waze", "google", "comma"]
  assert run("navChoice", "waze", True) is None  # NAV_REMEMBER is off: never straight on
  assert run("navLink", "waze", -25.3848, -49.2763) == "https://waze.com/ul?ll=-25.384800%2C-49.276300&navigate=yes"
  assert run("navLink", "google", -25.3848, -49.2763) == \
    "https://www.google.com/maps/dir/?api=1&destination=-25.384800%2C-49.276300&travelmode=driving"
  rec = run("navRecent", [{"name": "A", "latitude": -25.4, "longitude": -49.3}, {"name": "B", "latitude": -25.3, "longitude": -49.2}],
            {"name": "A de novo", "latitude": -25.40001, "longitude": -49.30002})
  assert [r["name"] for r in rec] == ["A de novo", "B"]  # the same place once, the newest first
  keys = json.loads(subprocess.run([NODE, "-e", f"process.stdout.write(JSON.stringify(require({json.dumps(str(LOGIC))}).DATA_KEYS))"],
                                   capture_output=True, text=True, timeout=30, check=True).stdout)
  assert "lpNavRecent" in keys  # where you went: "Apagar dados" takes it
  html = (LOGIC.parent / "index.html").read_text()
  assert '$("pickGo").addEventListener("click", () => { if (navPickP) openGo({ ...navPickP }); });' in html
  assert 'if (askPassenger(() => openGo(p), false, "mapa")) return;' in html


# ----- Navegar (NAV-03): the route to glance at with the phone on its mount

def test_navegar_opens_when_the_car_starts_moving_with_a_route():
  # owner, 06/11: moving with a route, Início goes to Navegar; without one, the Mapa gives way to the Painel as before
  def go(pane, **o):
    return run("homeFor", pane, {"hasRoute": True, "driving": True, "wasDriving": False, **o})
  assert go("painel") == "navegar" and go("mapa") == "navegar" and go("navegar") == "navegar"
  assert go("mapa", hasRoute=False) == "painel" and go("painel", hasRoute=False) == "painel"
  assert go("mapa", hasRoute=None) == "painel"  # the route not known yet at the start: as before
  assert go("painel", wasDriving=None) == "painel"  # the page's first look is not a start
  assert go("painel", wasDriving=True) == "painel" and go("mapa", wasDriving=True) == "mapa"  # already moving: what was picked stays
  assert go("navegar", hasRoute=False, driving=False) == "painel"  # the route ended
  assert go("navegar", hasRoute=None, driving=False, wasDriving=None) == "navegar"  # a reload before the comma answered stays


def test_navegar_distances_are_big_and_round():
  got = [run("navDist", m) for m in (0, 4, 346, 994, 996, 1049, 1250, 9949, 9951, 23400, None, -5)]
  assert [(d["n"], d["unit"], d["dec"]) for d in got] == [
    (0, "m", 0), (0, "m", 0), (350, "m", 0), (990, "m", 0), (1, "km", 1), (1, "km", 1), (1.3, "km", 1), (9.9, "km", 1),
    (10, "km", 0), (23, "km", 0), (0, "m", 0), (0, "m", 0)]


def test_navegar_shows_navd_route_and_waits_without_it():
  assert run("navView", {"active": True}, False, T0) is None  # no destination: no Navegar
  assert run("navView", None, True, T0) == {"wait": True, "turn": None, "arrive": None}
  assert run("navView", {"active": False}, True, T0) == {"wait": True, "turn": None, "arrive": None}  # navd still computing
  rt = {"active": True, "next": "Rua Inventada", "then": "Avenida Dois", "type": "turn", "modifier": "slight right",
        "in_m": 346, "left_m": 8420, "left_s": 754}
  v = run("navView", rt, True, T0)
  assert v["wait"] is False
  assert v["turn"] == {"kind": "slight-right", "dist": {"n": 350, "unit": "m", "dec": 0}, "street": "Rua Inventada", "then": "Avenida Dois"}
  assert v["arrive"] == {"at": T0 + 754_000, "min": 13, "dist": {"n": 8.4, "unit": "km", "dec": 1}}
  odd = run("navView", {"active": True, "left_s": -3, "in_m": None, "left_m": None}, True, T0)
  assert odd["arrive"]["at"] == T0 and odd["arrive"]["min"] == 0 and odd["turn"]["street"] == "" and odd["turn"]["kind"] == "straight"


def test_navegar_the_car_ahead_slower_faster_or_the_same():
  assert run("leadView", live(lead=None)) is None and run("leadView", None) is None
  slower = run("leadView", live(v=62, lead={"d": 34, "v": 58, "gap": 2.0}))
  assert slower == {"gap": 2.0, "d": 34, "v": 58, "dv": -4, "how": "{n} km/h mais devagar"}
  assert run("leadView", live(v=62, lead={"d": 40, "v": 70, "gap": 2.3}))["how"] == "{n} km/h mais rápido"
  assert run("leadView", live(v=62, lead={"d": 40, "v": 62.4, "gap": 2.3}))["how"] == "mesma velocidade"
  assert run("leadView", live(v=2, lead={"d": 6, "v": 0, "gap": None}))["gap"] is None  # stopped: no time gap, the distance shows


def test_navegar_zooms_out_with_speed():
  assert [run("navZoom", v) for v in (0, 49, 50, 89, 90, 130)] == [16.5, 16.5, 15.5, 15.5, 14.5, 14.5]


def test_navegar_only_reads_and_the_streets_need_the_option():
  # the rules that stay (NAV-03): no keyboard driving, the street map only with "Mostrar ruas", no camera nor microphone
  html = (LOGIC.parent / "index.html").read_text()
  block = html[html.index('<div id="navDrive"'):html.index('<div id="garage">')]
  assert "<input" not in block and "<textarea" not in block and "<select" not in block and "contenteditable" not in block
  buttons = re.findall(r"<button[^>]*>", block)
  assert buttons and all("data-home=" in b for b in buttons)  # the only buttons go to Painel or Mapa
  js = html[html.index("// ----- Navegar (NAV-03)"):html.index("// one finger moves the map")]
  assert "api(" not in js and "addEventListener" not in js and "focus(" not in js  # reads navState and live; asks and sends nothing
  assert "if (v && streets) for (const tl of LPL.mapTiles(v, w, h))" in js
  assert "near.near.find((p) => p.ahead)" in js  # it opens by itself: the Painel's next radar or bump can't disappear with it
  assert "getUserMedia" not in html and "webrtc" not in html.lower()
  seg = html[html.index('id="homeSeg"'):html.index('id="mapPane"')]
  assert seg.index('data-home="painel"') < seg.index('data-home="navegar"') < seg.index('data-home="mapa"')
  assert 'class="hidden" data-home="navegar"' in seg  # shown by renderNav only with a destination
  nav = html[html.index("function renderNav()"):html.index("function mapHome()")]
  assert 'if (homePane === "navegar" && !dest) { showHome("painel"); return; }' in nav
