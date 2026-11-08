import hashlib
import hmac

from openpilot.selfdrive.hexa import phone
from openpilot.selfdrive.hexa.options import HexaOption
from openpilot.selfdrive.hexa.phone import VehicleState

DRIVING = HexaOption("A", "direcao", "a", "d", driving=True)
COMFORT = HexaOption("B", "avisos", "b", "d", driving=False)
DEVICE_ONLY = HexaOption("C", "celular", "c", "d", driving=False, phone_writable=False)

OFF = VehicleState(onroad=False)
PARKED = VehicleState(onroad=True, parked=True, standstill=True, engaged=False)
MOVING = VehicleState(onroad=True, parked=False, standstill=False, engaged=True)


def test_can_write_driving_option():
  assert phone.can_write(DRIVING, OFF)[0]
  assert phone.can_write(DRIVING, PARKED)[0]
  assert not phone.can_write(DRIVING, MOVING)[0]
  # every way of not being safely parked is read only
  for vs in (VehicleState(True, parked=True, standstill=False), VehicleState(True, parked=False, standstill=True),
             VehicleState(True, parked=True, standstill=True, engaged=True), VehicleState(onroad=True)):
    ok, reason = phone.can_write(DRIVING, vs)
    assert not ok and reason


def test_can_write_comfort_and_device_only():
  assert phone.can_write(COMFORT, MOVING)[0]
  for vs in (OFF, PARKED, MOVING):
    assert not phone.can_write(DEVICE_ONLY, vs)[0]


def test_local_addresses():
  for ip in ("192.168.43.20", "192.168.0.10", "10.0.0.5", "172.16.3.4", "127.0.0.1", "::1", "fe80::1%wlan0", "fd00::1"):
    assert phone.is_local_address(ip), ip
  for ip in ("8.8.8.8", "100.64.0.1", "2001:4860::8888", "", None, "not-an-ip"):
    assert not phone.is_local_address(ip), ip


def test_pin_lifecycle():
  stored = phone.new_pin(now=1000.)
  pin = phone.parse_pin(stored, now=1000.)
  assert pin is not None and len(pin) == 6 and pin.isdigit()
  assert phone.pin_matches(stored, pin, now=1000. + phone.PIN_TTL - 1)
  assert phone.pin_matches(stored, f" {pin} ", now=1001.)
  assert not phone.pin_matches(stored, pin, now=1000. + phone.PIN_TTL + 1)  # expired
  assert not phone.pin_matches(stored, "000000" if pin != "000000" else "111111", now=1001.)
  assert not phone.pin_matches(None, pin)
  assert phone.parse_pin("12:34") is None
  assert phone.parse_pin("abcdef:9999999999") is None


def test_tokens():
  tokens: list[dict] = []
  issued = []
  for i in range(phone.MAX_TOKENS + 2):
    token, tokens = phone.issue_token(tokens, f"phone {i}", now=float(i))
    issued.append(token)
  assert len(tokens) == phone.MAX_TOKENS
  assert phone.find_token(tokens, issued[0]) is None  # oldest dropped
  entry = phone.find_token(tokens, issued[-1])
  assert entry is not None and entry["name"] == f"phone {phone.MAX_TOKENS + 1}"
  assert all(issued[-1] not in str(t) for t in tokens), "only the hash is stored"
  assert phone.find_token(tokens, None) is None and phone.find_token(tokens, "wrong") is None
  tokens = phone.revoke_token(tokens, entry["id"])
  assert phone.find_token(tokens, issued[-1]) is None



def _mac(token: str, nonce: str) -> str:  # the protocol written out, as the LizPilot app computes it (Ident.java)
  key = hashlib.sha256(token.encode()).hexdigest().encode()
  return hmac.new(key, ("lizpilot-ident|v1|" + nonce).encode(), hashlib.sha256).hexdigest()


def test_ident_only_the_comma_that_paired_the_phone_can_answer():  # APP-02
  token, tokens = phone.issue_token([], "celular", now=1.0)
  other, tokens = phone.issue_token(tokens, "outro", now=2.0)
  me = hashlib.sha256(hashlib.sha256(token.encode()).hexdigest().encode()).hexdigest()[:16]
  nonce = "n" * 22
  assert phone.ident_answer(tokens, me, nonce) == {"v": 1, "mac": _mac(token, nonce)}
  assert phone.ident_answer(tokens, me, "m" * 22)["mac"] != _mac(token, nonce)  # a new nonce, a new answer
  _, elsewhere = phone.issue_token([], "celular", now=1.0)  # another comma: never paired with this phone
  assert phone.ident_answer(elsewhere, me, nonce) == {"v": 1, "mac": None}
  assert phone.ident_answer(phone.revoke_token(tokens, tokens[0]["id"]), me, nonce) == {"v": 1, "mac": None}
  assert token not in str(phone.ident_answer(tokens, me, nonce)) and _mac(other, nonce) != _mac(token, nonce)


def test_ident_refuses_a_malformed_question():
  token, tokens = phone.issue_token([], "celular", now=1.0)
  me = phone.ident_phone(phone.hash_token(token))
  for bad_phone, bad_nonce in ((me, "short"), (me, "n" * 65), (me, "n" * 20 + "/"), (me, None), ("ABCDEF0123456789", "n" * 22),
                               (me[:15], "n" * 22), (None, "n" * 22)):
    assert phone.ident_answer(tokens, bad_phone, bad_nonce) is None


def test_a_car_challenge_is_good_once_and_for_a_while():  # NAV-03 step 2, Android Auto
  now = [0.]
  ch = phone.Challenges(lambda: now[0])
  app = "192.168.43.20"
  c = ch.issue(app)
  assert phone.IDENT_NONCE.fullmatch(c)
  assert ch.redeem(c, app) and not ch.redeem(c, app)  # once
  late = ch.issue(app)
  now[0] += phone.CHALLENGE_TTL_S
  assert not ch.redeem(late, app)
  many = [ch.issue(app) for _ in range(phone.CHALLENGE_PER_ADDRESS + 5)]  # asking in a loop: its own oldest leave
  assert not ch.redeem(many[0], app) and ch.redeem(many[-1], app)
  assert not ch.redeem(None, app) and not ch.redeem("", app) and not ch.redeem("x" * 22, app)


def test_another_device_can_neither_push_out_nor_use_up_the_apps_challenge():  # CTO review of navcel-02, item 3
  ch = phone.Challenges(lambda: 0.)
  app, other = "192.168.43.20", "192.168.43.66"
  c = ch.issue(app)
  for _ in range(phone.CHALLENGE_KEEP):  # a whole pool's worth of asking from one other device
    ch.issue(other)
  assert not ch.redeem(c, other)  # handed to the app: another address can't use it up
  assert ch.redeem(c, app)
  for i in range(phone.CHALLENGE_KEEP // phone.CHALLENGE_PER_ADDRESS + 1):  # only many addresses at once push the oldest out
    for _ in range(phone.CHALLENGE_PER_ADDRESS):
      ch.issue(f"10.0.{i}.1")
  assert len(ch._open) <= phone.CHALLENGE_KEEP


def test_the_car_reader_is_a_protocol_of_its_own():
  token, tokens = phone.issue_token([], "celular", now=1.0)
  _, tokens = phone.issue_token(tokens, "outro", now=2.0)
  h = phone.hash_token(token)
  me, c = phone.ident_phone(h), "c" * 22
  mac = hmac.new(h.encode(), ("lizpilot-car|v1|" + c).encode(), hashlib.sha256).hexdigest()  # written out, as the app computes it
  assert phone.car_reader(tokens, me, c, mac)["id"] == tokens[0]["id"]
  assert phone.car_reader(tokens, me, c, phone.ident_answer(tokens, me, c)["mac"]) is None  # /api/ident answers anyone: not here
  assert phone.car_reader(phone.revoke_token(tokens, tokens[0]["id"]), me, c, mac) is None  # taken off the comma
  for bad in ((me, c, mac.upper()), (me, c, mac[:63]), (me, "short", mac), ("0" * 16, c, mac), (None, c, mac), (me, c, None), (me, None, mac)):
    assert phone.car_reader(tokens, *bad) is None


def test_a_full_list_drops_the_phone_unused_the_longest_not_the_first_paired():
  # owner, 04/11: with 5 the phone in use fell off the list. Now 10, and the one used last stays
  tokens: list[dict] = []
  issued = []
  for i in range(phone.MAX_TOKENS):
    token, tokens = phone.issue_token(tokens, f"phone {i}", now=float(i))
    issued.append(token)
  first = phone.find_token(tokens, issued[0])
  tokens = phone.touch_token(tokens, first["id"], now=1e5)  # the first one paired is the one in use
  assert phone.touch_token(tokens, first["id"], now=1e5 + 60) is None  # written at most once an hour
  before = tokens
  _, tokens = phone.issue_token(tokens, "new", now=2e5)
  assert len(tokens) == phone.MAX_TOKENS and phone.find_token(tokens, issued[0]) is not None
  assert phone.find_token(tokens, issued[1]) is None and [t["name"] for t in phone.dropped(before, tokens)] == ["phone 1"]
  assert phone.touch_token(tokens, "nope", now=3e5) is None


def test_token_name_is_bounded():
  _, tokens = phone.issue_token([], "x" * 200)
  assert len(tokens[0]["name"]) == 40
  _, tokens = phone.issue_token([], "  ")
  assert tokens[0]["name"] == "celular"


def test_audit_is_capped():
  audit = None
  for i in range(phone.MAX_AUDIT + 10):
    audit = phone.add_audit(audit, {"event": "set", "n": i}, now=float(i))
  assert len(audit) == phone.MAX_AUDIT
  assert audit[-1]["n"] == phone.MAX_AUDIT + 9 and audit[-1]["t"] == phone.MAX_AUDIT + 9


def test_panel_url():
  assert phone.panel_url("192.168.0.10") == f"http://192.168.0.10:{phone.PORT}"
  assert phone.local_ip()  # never empty: falls back to the hotspot address


def test_pair_url_has_no_secret():
  url = phone.pair_url("192.168.0.10")
  assert url == f"http://192.168.0.10:{phone.PORT}/#parear"
