"""
The phone panel (hexaweb) or the map (mapd) going down never disengages openpilot; a driving process going down still does.

Nothing restarts a dead process, so this is checked on behaviour, not on the text of selfdrived.py: the real SelfdriveD gets
the same inputs over and over, and only managerState changes. With the panel or the map down, the events are exactly those
of everything running, so the state machine can't do anything else. With a driving process down, processNotRunning comes
back (NO_ENTRY and SOFT_DISABLE), and the state machine leaves enabled.
"""
import openpilot.cereal.messaging as messaging
from openpilot.cereal import log
from opendbc.car.structs import car
from openpilot.common.test import OpenpilotTestCase
from opendbc.car.gwm.interface import CarInterface
from opendbc.car.gwm.values import CAR
from openpilot.selfdrive.selfdrived.events import ET
from openpilot.selfdrive.selfdrived.selfdrived import SelfdriveD
from openpilot.selfdrive.selfdrived.state import StateMachine
from openpilot.system.manager.process_config import managed_processes

EventName = log.OnroadEvent.EventName
State = log.SelfdriveState.OpenpilotState

NON_DRIVING = ("hexaweb", "mapd")
DRIVING = ("controlsd", "card")
DT = 0.01


def manager_state(down=()):
  msg = messaging.new_message('managerState', valid=True)
  msg.managerState.processes = [{'name': name, 'running': name not in down, 'shouldBeRunning': True} for name in managed_processes]
  return msg.as_reader()


class TestNonDrivingProcesses(OpenpilotTestCase):
  def setup_method(self):
    CP = CarInterface.get_non_essential_params(CAR.GWM_HAVAL_H6)
    self.sd = SelfdriveD(CP=messaging.log_from_bytes(CP.to_bytes(), car.CarParams))
    # data_sample() sets this once every service checks out, or after 6 s at the latest; before it, update_events() stops
    # at selfdriveInitializing and checks no process at all
    self.sd.initialized = True
    self.CS = car.CarState.new_message(canValid=True).as_reader()
    self.t = 0.
    for _ in range(100):  # one second of everything running, so nothing is a first frame any more
      self.events_with()

  def events_with(self, down=()):
    self.t += DT
    self.sd.sm.update_msgs(self.t, [manager_state(down)])
    self.sd.update_events(self.CS)
    return sorted(self.sd.events.names)

  def state_after(self, names):
    sm = StateMachine()
    sm.state = State.enabled
    events = type(self.sd.events)()
    for name in names:
      events.add(name)
    sm.update(events)
    return sm.state

  def test_the_processes_are_real(self):
    # a renamed process would make the rest pass for nothing
    for name in NON_DRIVING + DRIVING:
      assert name in managed_processes

  def test_the_panel_or_the_map_down_changes_nothing(self):
    for down in [(p,) for p in NON_DRIVING] + [NON_DRIVING]:
      before = self.events_with()
      during = [self.events_with(down) for _ in range(50)]  # half a second down
      after = self.events_with()
      assert before == after
      for names in during:
        assert EventName.processNotRunning not in names, down
        assert names == before, down  # the state machine only sees the events: the same events, the same state

  def test_a_driving_process_down_still_disengages(self):
    for p in DRIVING:
      before = self.events_with()
      names = self.events_with((p,))
      assert EventName.processNotRunning not in before
      assert EventName.processNotRunning in names, p
      assert self.sd.events.contains(ET.SOFT_DISABLE) and self.sd.events.contains(ET.NO_ENTRY)
      assert self.state_after([EventName.processNotRunning]) == State.softDisabling
      self.events_with()  # back up

  def test_the_panel_down_does_not_hide_a_driving_process_down(self):
    assert EventName.processNotRunning in self.events_with(("hexaweb", "mapd", "controlsd"))
