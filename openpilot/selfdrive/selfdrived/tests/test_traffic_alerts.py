import ast
from pathlib import Path

from openpilot.cereal import log
from openpilot.selfdrive.selfdrived.traffic_alerts import ACC_LOST_DRIVEN, ACC_LOST_MIN_SPEED, ACC_LOST_PEDAL_HOLD, ACC_LOST_TIME, AccLostAlert, \
                                                         AUTO_RESUME_MAX_STOP, BUMP_MAX_DROP, BUMP_MAX_SPEED, BUMP_MAX_TIME, BUMP_MIN_SPEED, \
  BUMP_NO_RESPONSE, BumpSlowdown, AUTO_RESUME_REQUEST, LEAD_DEPART_DISTANCE, LEAD_DEPART_SPEED, \
                                                         AutoResume, LeadDepartAlert, StopHoldAlert, driver_attentive

DT = .01
LEAD_DEPART_TIME_MARGIN = .49  # just before the departure counts


class Stop:
  def __init__(self):
    self.alert = LeadDepartAlert(DT)

  def run(self, seconds, d_rel=3.3, v_lead=0., present=True, standstill=True, in_drive=True, gas=False):
    return sum(self.alert.update(standstill, in_drive, gas, present, d_rel, v_lead) for _ in range(int(seconds / DT)))

  def settled(self, d_rel=3.3):
    self.run(3., d_rel=d_rel)
    return self


def test_fires_once_when_the_car_ahead_leaves():
  stop = Stop().settled()
  assert stop.run(5., d_rel=3.3 + LEAD_DEPART_DISTANCE + 1, v_lead=2.) == 1
  assert stop.run(5., d_rel=20., v_lead=4.) == 0


def test_silent_while_the_car_ahead_stays():
  assert Stop().settled().run(60.) == 0


def test_vision_noise_is_not_a_departure():
  stop = Stop().settled()
  assert stop.run(10., d_rel=8., v_lead=0.) == 0  # distance jumps, no speed
  assert stop.run(10., d_rel=4., v_lead=2.) == 0  # speed, but it barely moved
  assert stop.run(.3, d_rel=8., v_lead=2.) == 0  # too short
  assert stop.run(.3) == 0
  assert stop.run(.3, d_rel=8., v_lead=2.) == 0


def test_needs_a_car_at_the_stop():
  stop = Stop()
  assert stop.run(3., present=False) == 0
  assert stop.run(5., d_rel=20., v_lead=3.) == 0


def test_a_far_car_is_not_the_one_we_stopped_behind():
  assert Stop().settled(d_rel=30.).run(5., d_rel=40., v_lead=3.) == 0


def test_needs_two_seconds_stopped():
  stop = Stop()
  assert stop.run(1., d_rel=3.3) == 0
  assert stop.run(.9, d_rel=8., v_lead=3.) == 0


def test_never_while_moving():
  stop = Stop()
  assert stop.run(10., standstill=False, d_rel=20., v_lead=5.) == 0
  stop.settled()
  assert stop.run(.2, standstill=False, d_rel=20., v_lead=5.) == 0
  assert stop.run(1.9, d_rel=20., v_lead=5.) == 0  # moved: this is a new stop, with no reference yet


def test_only_in_drive():
  stop = Stop()
  assert stop.run(10., in_drive=False) == 0
  assert stop.alert.base is None
  stop.settled()
  assert stop.run(5., in_drive=False, d_rel=20., v_lead=5.) == 0


def test_driver_on_the_gas_needs_no_warning():
  assert Stop().settled().run(5., d_rel=20., v_lead=5., gas=True) == 0


def test_next_stop_can_warn_again():
  stop = Stop().settled()
  assert stop.run(1., d_rel=8., v_lead=LEAD_DEPART_SPEED) == 1
  stop.run(1., standstill=False)
  stop.settled()
  assert stop.run(1., d_rel=8., v_lead=LEAD_DEPART_SPEED) == 1


def test_lead_lost_for_a_moment_keeps_the_reference():
  stop = Stop().settled()
  assert stop.run(1., present=False) == 0
  assert stop.run(1., d_rel=8., v_lead=2.) == 1


class Resume:
  GONE = {"d_rel": 3.3 + LEAD_DEPART_DISTANCE + 2, "v_lead": 2.}

  def __init__(self):
    self.auto = AutoResume(DT)

  def run(self, seconds, d_rel=3.3, v_lead=0., present=True, enabled=True, standstill=True, in_drive=True, gas=False, brake=False, attentive=True):
    return sum(self.auto.update(enabled, standstill, in_drive, gas, brake, present, d_rel, v_lead, attentive) for _ in range(int(seconds / DT)))

  def settled(self):
    self.run(3.)
    return self


def test_resume_asks_once_when_the_car_ahead_leaves():
  stop = Resume().settled()
  assert stop.run(5., **Resume.GONE) == int(AUTO_RESUME_REQUEST / DT)
  assert stop.run(5., **Resume.GONE) == 0


def test_resume_stays_quiet_with_the_car_ahead_stopped():
  assert Resume().settled().run(50.) == 0


def test_resume_needs_openpilot_engaged():
  assert Resume().settled().run(5., enabled=False, **Resume.GONE) == 0


def test_resume_needs_the_driver_looking():
  assert Resume().settled().run(5., attentive=False, **Resume.GONE) == 0


def test_resume_not_with_a_pedal():
  assert Resume().settled().run(5., gas=True, **Resume.GONE) == 0
  assert Resume().settled().run(5., brake=True, **Resume.GONE) == 0


def test_a_pedal_during_the_stop_leaves_it_to_the_driver():
  for pedal in ({"gas": True}, {"brake": True}, {"enabled": False}):
    stop = Resume().settled()
    stop.run(.5, **pedal)
    assert stop.run(5., **Resume.GONE) == 0


def test_a_pedal_ends_the_request():
  stop = Resume().settled()
  stop.run(LEAD_DEPART_TIME_MARGIN, **Resume.GONE)
  asked = stop.run(.01, **Resume.GONE)
  assert asked == 1 and stop.run(.5, brake=True, **Resume.GONE) == 0


def test_resume_not_after_a_long_stop():
  stop = Resume()
  stop.run(AUTO_RESUME_MAX_STOP + 1.)
  assert stop.run(5., **Resume.GONE) == 0


def test_resume_just_before_the_limit():
  stop = Resume()
  stop.run(AUTO_RESUME_MAX_STOP - 2.)
  assert stop.run(1.5, **Resume.GONE) > 0


def test_resume_not_in_another_gear():
  assert Resume().settled().run(5., in_drive=False, **Resume.GONE) == 0


def test_moving_ends_the_request_and_a_new_stop_starts_over():
  stop = Resume().settled()
  stop.run(LEAD_DEPART_TIME_MARGIN + .01, **Resume.GONE)
  assert stop.run(.5, standstill=False, **Resume.GONE) == 0
  stop.settled()
  assert stop.run(5., **Resume.GONE) > 0


def test_no_car_ahead_no_resume():
  assert Resume().settled().run(5., present=False, d_rel=0.) == 0
  stop = Resume()
  stop.run(3., present=False, d_rel=0.)
  assert stop.run(5., **Resume.GONE) == 0


def monitoring(policy="vision", alert="none", face=True, distracted=False):
  # a real message: a wrong field here would take selfdrived down on the car
  dm = log.DriverMonitoringState.new_message(activePolicy=policy, alertLevel=alert)
  dm.visionPolicyState.faceDetected = face
  dm.visionPolicyState.isDistracted = distracted
  return dm.as_reader()


def test_attentive_needs_the_camera_seeing_the_driver():
  assert driver_attentive(monitoring())
  assert not driver_attentive(monitoring(face=False))
  assert not driver_attentive(monitoring(distracted=True))
  assert not driver_attentive(monitoring(alert="one"))
  assert not driver_attentive(monitoring(policy="wheeltouch"))


def test_the_phone_panel_never_counts_as_a_driving_process():
  # selfdrived doesn't import on every machine, and this line decides if a dead panel disengages the car
  source = (Path(__file__).parents[1] / "selfdrived.py").read_text()
  # the set may grow (mapd joined it in CRUISE-03): what must hold is the panel staying in it
  sets = [ast.literal_eval(node.value) for node in ast.parse(source).body
          if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "NON_DRIVING_PROCESSES" for t in node.targets)]
  assert len(sets) == 1 and "hexaweb" in sets[0]
  assert "if not p.running and p.shouldBeRunning} - NON_DRIVING_PROCESSES" in source


class Bump:
  def __init__(self, target=30, speed=60.):
    self.b = BumpSlowdown(dt=.01)
    self.started = self.b.start(target, speed)

  def run(self, set_speed, seconds=.01, enabled=True, gas=False, brake=False):
    return sum(self.b.update(enabled, set_speed, gas, brake) for _ in range(round(seconds / .01)))


def test_bump_asks_until_the_target():
  bump = Bump()
  assert bump.started and bump.run(60) == 1 and bump.run(55) == 1 and bump.run(35) == 1
  assert bump.run(30) == 0
  assert bump.run(60) == 0  # done for this bump


def test_bump_never_below_what_the_acc_can_do():
  assert not Bump(target=BUMP_MIN_SPEED - 1).started
  assert Bump(target=BUMP_MIN_SPEED).started


def test_bump_ignores_a_fast_car():
  assert not Bump(speed=BUMP_MAX_SPEED + 1).started


def test_bump_never_drops_more_than_the_panda_allows():
  bump = Bump()
  assert bump.run(90) == 1 and bump.b.target == 90 - BUMP_MAX_DROP


def test_bump_ends_with_a_pedal_or_disengaged():
  for stop in ({"gas": True}, {"brake": True}, {"enabled": False}):
    bump = Bump()
    assert bump.run(60) == 1 and bump.run(55, **stop) == 0
    assert bump.run(55) == 0


def test_bump_ends_when_the_driver_raises_the_speed():
  bump = Bump()
  assert bump.run(60) == 1 and bump.run(65) == 0 and bump.run(60) == 0


def test_bump_gives_up_if_the_acc_does_not_answer():
  bump = Bump()
  assert 0 < bump.run(60, BUMP_NO_RESPONSE + 1) < round((BUMP_NO_RESPONSE + .1) / .01)
  assert bump.run(60) == 0


def test_bump_has_a_time_limit():
  bump = Bump()
  speeds = 0
  for i in range(round((BUMP_MAX_TIME + 1) / .01)):
    speeds += bump.b.update(True, 1000 - i * .01, False, False)
  assert speeds < round(BUMP_MAX_TIME / .01) + 1 and bump.b.target is None


def test_a_second_bump_does_not_restart_the_first():
  bump = Bump()
  frames = bump.b.frames
  bump.run(60, 1.)
  assert bump.b.start(40, 60.) is False and bump.b.target == 30 and bump.b.frames < frames


class Acc:
  def __init__(self):
    self.alert = AccLostAlert(DT)

  def run(self, seconds, enabled=True, acc=True, v=15., gas=False, brake=False):
    return sum(self.alert.update(enabled, acc, v, gas, brake) for _ in range(round(seconds / DT)))

  def driving(self):
    self.run(ACC_LOST_DRIVEN + 1.)
    return self


def test_acc_lost_warns_once():
  acc = Acc().driving()
  assert acc.run(ACC_LOST_TIME - .05, acc=False) == 0
  assert acc.run(.1, acc=False) == 1
  assert acc.run(10., acc=False) == 0


def test_a_blip_of_the_acc_says_nothing():
  acc = Acc().driving()
  assert acc.run(ACC_LOST_TIME - .05, acc=False) == 0
  assert acc.run(1.) == 0
  assert acc.run(ACC_LOST_TIME - .05, acc=False) == 0


def test_warns_again_only_after_the_acc_came_back():
  acc = Acc().driving()
  assert acc.run(2., acc=False) == 1
  assert acc.run(ACC_LOST_DRIVEN - .1) + acc.run(2., acc=False) == 0
  assert acc.run(ACC_LOST_DRIVEN + .1) + acc.run(2., acc=False) == 1


def test_a_pedal_is_the_driver_taking_the_speed():
  for pedal in ({"gas": True}, {"brake": True}):
    assert Acc().driving().run(5., acc=False, **pedal) == 0


def test_the_pedal_counts_for_a_while_after_it_is_released():
  acc = Acc().driving()
  acc.run(.1, gas=True)
  acc.run(ACC_LOST_PEDAL_HOLD - .5)
  assert acc.run(5., acc=False) == 0

  acc = Acc().driving()
  acc.run(.1, brake=True)
  acc.run(ACC_LOST_PEDAL_HOLD + ACC_LOST_DRIVEN)
  assert acc.run(5., acc=False) == 1


def test_slow_traffic_and_stops_say_nothing():
  assert Acc().driving().run(5., acc=False, v=ACC_LOST_MIN_SPEED - .1) == 0
  assert Acc().driving().run(5., acc=False, v=0.) == 0


def test_nothing_with_openpilot_off():
  assert Acc().driving().run(5., acc=False, enabled=False) == 0
  acc = Acc()
  acc.run(10., enabled=False)
  assert acc.run(5., acc=False) == 0


def test_the_acc_has_to_have_been_driving():
  acc = Acc()
  acc.run(ACC_LOST_DRIVEN - .1)
  assert acc.run(5., acc=False) == 0


class TestStopHoldAlert:
  def test_once_per_hold(self):
    a = StopHoldAlert()
    assert [a.update(x) for x in (False, True, True, True)] == [False, True, False, False]

  def test_the_next_hold_tells_again(self):
    a = StopHoldAlert()
    assert [a.update(x) for x in (True, False, True)] == [True, False, True]
