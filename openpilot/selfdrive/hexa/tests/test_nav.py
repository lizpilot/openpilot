"""The route from the phone (nav.py): links, search, destination, saved places, the Mapbox key and what the phone shows."""
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from openpilot.selfdrive.hexa import nav


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


NITEROI = (-22.8832, -43.1036)


@pytest.mark.parametrize("text, expected", [
  ("-25.4103, -49.2675", (-25.4103, -49.2675)),
  ("-25.4103;-49.2675", (-25.4103, -49.2675)),
  ("https://www.google.com/maps/@-22.9519,-43.2105,15z", (-22.9519, -43.2105)),
  ("https://www.google.com/maps/place/Cristo+Redentor/@-22.9519,-43.2129,17z/data=!3m1!4b1!4m6!3m5!1s0x0:0x0!8m2!3d-22.951916!4d-43.210487",
   (-22.9519, -43.2129)),
  ("https://maps.google.com/?q=-22.8832,-43.1036", NITEROI),
  ("https://www.waze.com/ul?ll=-22.8832%2C-43.1036&navigate=yes", NITEROI),
  ("https://maps.apple.com/?daddr=-22.8832,-43.1036", NITEROI),
  ("https://www.openstreetmap.org/?mlat=-22.8832&mlon=-43.1036#map=17/-22.8832/-43.1036", NITEROI),
  ("geo:-22.8832,-43.1036?z=15", NITEROI),
])
def test_points_from_links_and_coordinates(text, expected):
  assert nav.parse_point(text) == expected


@pytest.mark.parametrize("text", ["Rua da Praia 10", "", "0,0", "-95,10", "10,200", "https://maps.app.goo.gl/abc", "a@b,c"])
def test_not_a_point(text):
  assert nav.parse_point(text) is None


class World:
  def __init__(self, results=None, position=NITEROI, fail=False):
    self.t = 100.
    self.urls = []
    self.results = results if results is not None else [
      {"lat": "-25.4429", "lon": "-49.2397", "display_name": "Jardim Botânico, Curitiba, Paraná, Brasil"},
      {"lat": "nope", "lon": "x", "display_name": "broken"},
      {"lat": "0", "lon": "0", "display_name": "null island"},
    ]
    self.fail = fail
    self.store = FakeStore(NavigationEnabled=True)
    self.nav = nav.Navigator(self.store, get_json=self.get, mono=lambda: self.t, position=lambda: position)

  def get(self, url):
    self.urls.append(url)
    if self.fail:
      raise OSError("no network")
    return self.results


def test_search_near_the_car_in_brazil_and_cleans_the_answer():
  w = World()
  out = w.nav.search("  jardim botânico  ")
  assert out == [{"name": "Jardim Botânico, Curitiba, Paraná, Brasil", "latitude": -25.4429, "longitude": -49.2397}]
  q = parse_qs(urlparse(w.urls[0]).query)
  assert urlparse(w.urls[0]).netloc == "nominatim.openstreetmap.org"
  assert q["q"] == ["jardim botânico"] and q["countrycodes"] == ["br"] and q["limit"] == ["5"]
  lon0, lat0, lon1, lat1 = map(float, q["viewbox"][0].split(","))
  assert lon0 < NITEROI[1] < lon1 and lat1 < NITEROI[0] < lat0


def test_search_waits_between_calls_and_remembers_answers():
  w = World()
  w.nav.search("praia de icaraí")
  w.nav.search("praia de icaraí")  # the same: no new request
  assert len(w.urls) == 1
  with pytest.raises(nav.NavError, match="Espere"):
    w.nav.search("rua da conceição")  # Nominatim's policy: one a second
  w.t += 1.2
  w.nav.search("rua da conceição")
  assert len(w.urls) == 2


def test_search_needs_no_network_for_a_link_and_says_why_otherwise():
  w = World(fail=True)
  assert w.nav.search("https://maps.google.com/?q=-22.8832,-43.1036")[0]["latitude"] == NITEROI[0] and w.urls == []
  with pytest.raises(nav.NavError, match="internet"):
    w.nav.search("praia de icaraí")
  with pytest.raises(nav.NavError, match="Link curto"):
    w.nav.search("https://maps.app.goo.gl/AbCdEf")
  with pytest.raises(nav.NavError, match="Escreva"):
    w.nav.search(" a ")


def test_search_without_gps_has_no_viewbox():
  w = World(position=None)
  w.nav.search("praia de icaraí")
  assert "viewbox" not in parse_qs(urlparse(w.urls[0]).query)


def test_go_writes_openpilots_destination_and_stop_clears_it():
  w = World()
  dest = w.nav.go(-25.4429, -49.2397, "Jardim\x1b[31m")
  assert w.store.get("NavDestination") == dest == {"latitude": -25.4429, "longitude": -49.2397, "place_name": "Jardim[31m"}
  assert w.nav.destination() == dest
  w.nav.stop()
  assert w.store.get("NavDestination") is None and w.nav.destination() is None
  for bad in ((0, 0), (None, 1), (True, 1), (float("nan"), 1), (91, 1)):
    with pytest.raises(nav.NavError):
      w.nav.go(*bad)


def test_go_needs_navigation_on_in_the_comma():
  w = World()
  w.store.put("NavigationEnabled", False)
  with pytest.raises(nav.NavError, match="navegação"):
    w.nav.go(-22.9, -43.1)
  assert w.store.get("NavDestination") is None


def test_saved_places():
  w = World()
  w.nav.save_place("Casa", -22.9, -43.1)
  w.nav.save_place("casa", -22.8, -43.0)  # the same name replaces it
  assert w.nav.places() == [{"name": "casa", "latitude": -22.8, "longitude": -43.0}]
  for i in range(nav.MAX_PLACES - 1):
    w.nav.save_place(f"lugar {i}", -22.9, -43.1)
  with pytest.raises(nav.NavError, match="No máximo"):
    w.nav.save_place("mais um", -22.9, -43.1)
  assert len(w.nav.remove_place("CASA")) == nav.MAX_PLACES - 1
  with pytest.raises(nav.NavError):
    w.nav.save_place("  ", -22.9, -43.1)
  w.store.put(nav.PLACES_PARAM, [{"name": "x"}, "junk", {"name": "ok", "latitude": -22.9, "longitude": -43.1}])
  assert [p["name"] for p in w.nav.places()] == ["ok"]


def test_the_mapbox_key():
  w = World()
  assert w.nav.set_token("pk.eyJ1IjoiY29ucmFkbyIsImEiOiJjbHh5ejEyMyJ9.abcdefGHIJ") is True
  assert w.store.get(nav.TOKEN_PARAM).startswith("pk.")
  with pytest.raises(nav.NavError):
    w.nav.set_token("sk.secret-key-must-never-be-here-1234567890")
  with pytest.raises(nav.NavError):
    w.nav.set_token("pk.short")
  assert w.nav.set_token("") is False and w.store.get(nav.TOKEN_PARAM) is None


def test_route_view():
  ins = SimpleNamespace(maneuverPrimaryText="Rua Moreira César", maneuverSecondaryText="", maneuverType="turn", maneuverModifier="right",
                        maneuverDistance=152.4, distanceRemaining=5230.7, timeRemaining=611.2)
  pts = [(-22.9 + i * 1e-4, -43.1) for i in range(1000)]
  v = nav.route_view(ins, pts, True)
  assert v["active"] and v["next"] == "Rua Moreira César" and v["in_m"] == 152 and v["left_m"] == 5231 and v["left_s"] == 611
  assert len(v["path"]) <= 301 and v["path"][-1] == [round(pts[-1][0], 5), -43.1]
  assert nav.route_view(ins, [], False) == {"active": False}


def test_navd_takes_the_owners_key():
  """navd (openpilot's own) uses the key saved from the phone when there is no MAPBOX_TOKEN."""
  from pathlib import Path
  src = (Path(nav.__file__).parents[1] / "navigation/navd.py").read_text()
  assert 'os.environ.get("MAPBOX_TOKEN") or self.params.get("HexaMapboxToken") or None' in src
