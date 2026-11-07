"""Updating the comma from the phone: what the panel may ask of the updater.

Same steps as the Software page on the comma: check, download, install. All of them need the car off, because
installing reboots the comma. Only the car's branch is offered, and uninstalling is never here. Going back to a
version is done by the canal, see docs/STATUS.md.
"""
import subprocess

from openpilot.selfdrive.hexa import edition
from openpilot.selfdrive.hexa.phone import VehicleState

UPDATER_PROC = "openpilot.system.updated.updated"
SIGNALS = {"check": "SIGUSR1", "download": "SIGHUP"}  # the same the Software page sends
# the car uses one branch: the canal for the Haval build, "lizpilot" for the generic edition (CPontes/lizpilot).
# A comma on another one still lists it, to leave it.
OFFERED_BRANCHES = ("lizpilot",) if edition.EDITION == "generic" else ("h6gt",)


def signal_updater(sig: str) -> None:
  subprocess.run(["pkill", f"-{sig}", "-f", UPDATER_PROC], check=False)


def can_update(state: VehicleState) -> tuple[bool, str]:
  if state.onroad:
    return False, "Desligue o carro para atualizar."
  return True, ""


def branches(available: str | None) -> list[str]:
  return [b for b in (available or "").split(",") if b in OFFERED_BRANCHES]


def comma_branches(available: str | None) -> list[str]:
  """What the comma's own Software page lists under "target branch" (selfdrive/ui/mici/layouts/settings/software.py).

  The Haval build: every branch the updater found, as the comma always did. The generic edition: only what LizPilot
  offers, so a tester in another car never picks a build that forces the Haval, even if one comes back to the
  repository (QA-01, A1; LIZ-04)."""
  found = [b for b in (available or "").split(",") if b]
  return [b for b in found if b in OFFERED_BRANCHES] if edition.EDITION == "generic" else found


def describe(desc: str | None) -> dict:
  # "version / branch / commit / date"
  parts = [p.strip() for p in (desc or "").split(" / ")]
  if len(parts) != 4:
    return {}
  return dict(zip(("version", "branch", "commit", "date"), parts, strict=True))


def state(store) -> dict:
  target = store.get("UpdaterTargetBranch") or store.get("GitBranch") or ""
  offered = branches(store.get("UpdaterAvailableBranches"))
  if target and target not in offered:
    offered = [target, *offered]
  return {
    "current": describe(store.get("UpdaterCurrentDescription")),
    "new": describe(store.get("UpdaterNewDescription")),
    "target": target,
    "branches": offered,
    "state": store.get("UpdaterState") or "",
    "fetch_available": bool(store.get_bool("UpdaterFetchAvailable")),
    "ready": bool(store.get_bool("UpdateAvailable")),
    "failed": int(store.get("UpdateFailedCount") or 0),
    "last_check": str(store.get("LastUpdateTime") or ""),
  }
