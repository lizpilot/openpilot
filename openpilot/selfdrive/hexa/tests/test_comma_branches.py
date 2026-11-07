"""The comma's own Software page, "target branch" (LIZ-04, QA-01 A1).

In the generic edition the page lists only what LizPilot offers (updates.OFFERED_BRANCHES), so a tester in another car
never installs a build that forces the Haval, even if one comes back to the repository. In the Haval build it lists what
it always did. Both pages are checked: the comma 4's (mici) and the comma 3X's.

The pages need raylib and the comma's screen. Here each runs in a fresh process, with stand-ins for the widgets, and the
real page code builds its list from a made-up set of branches on the remote. The edition is read once, when the modules
load, so each edition gets its own process (as in test_edition.py).
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from openpilot.selfdrive.hexa import updates

UI = Path(__file__).resolve().parents[2] / "ui"
MICI = UI / "mici" / "layouts" / "settings" / "software.py"
TICI = UI / "layouts" / "settings" / "software.py"
REMOTE = "canal-h6gt,lizpilot,main,h6gt,lizpilot-next"  # made up: what a repository could carry, the Haval's build among it

PAGES = textwrap.dedent('''
  import importlib.util, json, sys, types
  from types import SimpleNamespace

  class Params(dict):
    def get(self, k):
      return dict.get(self, k)
    def get_bool(self, k):
      return bool(dict.get(self, k))
    def put(self, k, v, block=False):
      self[k] = v
    put_bool = put

  params = Params(json.loads(sys.argv[1]))
  out, pushed = {}, []

  class Anything:
    def __init__(self, *a, **k):
      self.args = a
    def __getattr__(self, name):
      return lambda *a, **k: None

  class BigButton(Anything):
    def __init__(self, text="", *a, **k):
      self.text = text

  class Scroller:
    def add_widgets(self, widgets):
      out["mici"] = [w.text for w in widgets]

  class NavScroller(Anything):
    def __init__(self, *a, **k):
      self._scroller = Scroller()

  class MultiOptionDialog(Anything):
    def __init__(self, title, options, current, callback=None):
      out["tici"] = list(options)

  def module(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m

  gui_app = SimpleNamespace(texture=lambda *a, **k: None, push_widget=pushed.append)
  module("pyray")
  module("openpilot.common.time_helpers", system_time_valid=lambda: True)
  module("openpilot.selfdrive.ui.ui_state", ui_state=SimpleNamespace(params=params, is_offroad=lambda: True))
  module("openpilot.system.ui.lib.application", gui_app=gui_app, MousePos=object)
  module("openpilot.system.ui.lib.multilang", tr=lambda s: s, trn=lambda s, p, n: s)
  module("openpilot.system.ui.widgets", Widget=Anything, DialogResult=SimpleNamespace(CONFIRM=1))
  module("openpilot.system.ui.widgets.scroller", NavScroller=NavScroller)
  module("openpilot.system.ui.widgets.scroller_tici", Scroller=Anything)
  module("openpilot.system.ui.widgets.confirm_dialog", ConfirmDialog=Anything)
  module("openpilot.system.ui.widgets.list_view", button_item=Anything, text_item=Anything, ListItem=Anything)
  module("openpilot.system.ui.widgets.option_dialog", MultiOptionDialog=MultiOptionDialog)
  module("openpilot.selfdrive.ui.mici.layouts.settings.device.device_layout", EngagedConfirmationButton=Anything)
  module("openpilot.selfdrive.ui.mici.widgets.info", InfoLayoutMici=Anything)
  module("openpilot.selfdrive.ui.mici.widgets.button", BigButton=BigButton)
  module("openpilot.selfdrive.ui.mici.widgets.dialog", BigDialog=Anything)

  def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

  mici = load(sys.argv[2], "mici_software")
  mici.TargetBranchButton._on_click(SimpleNamespace(_on_select=lambda b: None))  # the button: the list, or "Failed to get"
  out["opens"] = "list" if isinstance(pushed[-1], mici.BranchSelectPage) else "message"
  out.setdefault("mici", [])
  tici = load(sys.argv[3], "tici_software")
  tici.SoftwareLayout._on_select_branch(SimpleNamespace(_branch_dialog=None))
  print(json.dumps(out))
''')


def pages(edition: str, available: str, git_branch: str, mici=MICI, tici=TICI) -> dict:
  params = {"UpdaterAvailableBranches": available, "GitBranch": git_branch, "UpdaterTargetBranch": git_branch}
  env = dict(os.environ, LIZPILOT_EDITION=edition)
  out = subprocess.run([sys.executable, "-c", PAGES, json.dumps(params), str(mici), str(tici)], env=env,
                       capture_output=True, text=True, timeout=120)
  assert out.returncode == 0, out.stdout + out.stderr
  return json.loads(out.stdout.strip().splitlines()[-1])


def test_the_generic_comma_lists_only_what_lizpilot_offers():
  out = pages("generic", REMOTE, "lizpilot")
  assert out == {"mici": ["lizpilot"], "tici": ["lizpilot"], "opens": "list"}


def test_the_generic_comma_on_another_branch_can_only_go_to_lizpilot():
  out = pages("generic", REMOTE, "canal-h6gt")
  assert out["mici"] == out["tici"] == ["lizpilot"]


def test_the_generic_comma_without_lizpilot_on_the_remote_says_so_instead_of_an_empty_list():
  out = pages("generic", "canal-h6gt,main", "lizpilot")
  assert out == {"mici": [], "tici": [], "opens": "message"}


def test_the_haval_comma_lists_what_it_always_did():
  out = pages("h6gt", REMOTE, "h6gt")
  every = ["h6gt", "canal-h6gt", "lizpilot", "main", "lizpilot-next"]  # all of them, the comma's own branch first
  assert out == {"mici": every, "tici": every, "opens": "list"}


def test_comma_branches_by_edition():
  # this process is the Haval build (conftest.py): every branch, empty names dropped, as the page always did
  assert updates.comma_branches(REMOTE) == REMOTE.split(",")
  assert updates.comma_branches("a,,b,") == ["a", "b"] and updates.comma_branches(None) == [] and updates.comma_branches("") == []
  assert updates.OFFERED_BRANCHES == ("h6gt",)
