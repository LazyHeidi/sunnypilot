#!/usr/bin/env python3
import datetime
import os
import shutil
import signal
import sys
import time
import traceback

from cereal import log
import cereal.messaging as messaging
import openpilot.system.sentry as sentry
from openpilot.common.utils import atomic_write
from openpilot.common.params import Params, ParamKeyFlag
from openpilot.common.text_window import TextWindow
from openpilot.system.hardware import HARDWARE
from openpilot.system.manager.helpers import unblock_stdout, write_onroad_params
from openpilot.system.manager.process import ensure_running
from openpilot.system.manager.process_config import managed_processes
from openpilot.system.athena.registration import UNREGISTERED_DONGLE_ID
from openpilot.common.swaglog import cloudlog
from openpilot.system.version import get_build_metadata
from openpilot.system.hardware.hw import Paths
from openpilot.system.hardware import PC

from openpilot.sunnypilot.system.params_migration import run_migration


def clear_saved_logs() -> None:
  if PC:
    return

  paths = [
    Paths.log_root(),
    Paths.swaglog_root(),
    Paths.stats_root(),
    Paths.stats_sp_root(),
    Paths.crash_log_root(),
    "/data/tombstones",
  ]
  if os.path.ismount(Paths.log_root_external()):
    paths.append(Paths.log_root_external())

  try:
    os.unlink("/tmp/launch_log")
  except FileNotFoundError:
    pass

  for path in paths:
    if not os.path.exists(path):
      continue
    try:
      if os.path.ismount(path):
        for entry in os.scandir(path):
          if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
          else:
            os.unlink(entry.path)
      else:
        shutil.rmtree(path)
    except OSError as e:
      print(f"Failed to clear saved logs from {path}: {e}")


def stop_persisted_daemon(params: Params, pid_param: str, module: str) -> None:
  raw_pid = params.get(pid_param)
  params.remove(pid_param)
  if raw_pid is None:
    return

  try:
    pid = int(raw_pid)
    with open(f"/proc/{pid}/cmdline", "rb") as f:
      cmdline = f.read().replace(b"\0", b" ").decode("utf-8", errors="replace")
    if module not in cmdline:
      return
    os.kill(pid, signal.SIGTERM)
  except (OSError, ValueError):
    return

  for _ in range(20):
    time.sleep(0.1)
    try:
      with open(f"/proc/{pid}/cmdline", "rb") as f:
        cmdline = f.read().replace(b"\0", b" ").decode("utf-8", errors="replace")
      if module not in cmdline:
        return
      os.kill(pid, 0)
    except (OSError, ValueError):
      return

  try:
    with open(f"/proc/{pid}/cmdline", "rb") as f:
      cmdline = f.read().replace(b"\0", b" ").decode("utf-8", errors="replace")
    if module in cmdline:
      os.kill(pid, signal.SIGKILL)
  except (OSError, ValueError):
    pass


def manager_init() -> None:
  clear_saved_logs()

  build_metadata = get_build_metadata()

  params = Params()
  stop_persisted_daemon(params, "AthenadPid", "system.athena.manage_athenad")
  stop_persisted_daemon(params, "SunnylinkdPid", "sunnypilot.sunnylink.athena.manage_sunnylinkd")
  params.clear_all(ParamKeyFlag.CLEAR_ON_MANAGER_START)
  params.clear_all(ParamKeyFlag.CLEAR_ON_ONROAD_TRANSITION)
  params.clear_all(ParamKeyFlag.CLEAR_ON_OFFROAD_TRANSITION)
  params.clear_all(ParamKeyFlag.CLEAR_ON_IGNITION_ON)
  # if build_metadata.release_channel:
  #   params.clear_all(ParamKeyFlag.DEVELOPMENT_ONLY)

  # device boot mode
  if params.get("DeviceBootMode") == 1:  # start in Always Offroad mode
    params.put_bool("OffroadMode", True, block=True)

  # quick boot
  if params.get_bool("QuickBootToggle") and not PC:
    prebuilt_path = "/data/openpilot/prebuilt"
    if not os.path.exists(prebuilt_path):
      open(prebuilt_path, 'x').close()

  if params.get_bool("RecordFrontLock"):
    params.put_bool("RecordFront", True, block=True)

  # Disable network-backed features and all persistent recording.
  params.put_bool("DisableUpdates", True, block=True)
  params.put_bool("OnroadUploads", False, block=True)
  params.put_bool("SunnylinkEnabled", False, block=True)
  params.put_bool("EnableSunnylinkUploader", False, block=True)
  params.put_bool("RecordFront", False, block=True)
  params.put_bool("RecordAudio", False, block=True)
  params.put_bool("RecordAudioFeedback", False, block=True)
  params.put_bool("UpdateAvailable", False, block=True)
  params.put_bool("UpdaterFetchAvailable", False, block=True)
  params.put_bool("RoadNameToggle", False, block=True)
  params.put_bool("SmartCruiseControlMap", False, block=True)
  params.put("SpeedLimitPolicy", 0, block=True)  # car_state_only
  params.remove("Offroad_OSMUpdateRequired")
  params.remove("CurrentBootlog")
  params.remove("AthenadUploadQueue")
  params.remove("AthenadRecentlyViewedRoutes")

  if not PC:
    run_migration(params)

  # set unset params to their default value
  for k in params.all_keys():
    default_value = params.get_default_value(k)
    if default_value is not None and params.get(k) is None:
      params.put(k, default_value, block=True)

  # Create folders needed for msgq
  try:
    os.mkdir(Paths.shm_path())
  except FileExistsError:
    pass
  except PermissionError:
    print(f"WARNING: failed to make {Paths.shm_path()}")

  # set params
  serial = HARDWARE.get_serial()
  params.put("Version", build_metadata.openpilot.version, block=True)
  params.put("GitCommit", build_metadata.openpilot.git_commit, block=True)
  params.put("GitCommitDate", build_metadata.openpilot.git_commit_date, block=True)
  params.put("GitBranch", build_metadata.channel, block=True)
  params.put("GitRemote", build_metadata.openpilot.git_origin, block=True)
  params.put_bool("IsDevelopmentBranch", build_metadata.development_channel, block=True)
  params.put_bool("IsTestedBranch", build_metadata.tested_channel, block=True)
  params.put_bool("IsReleaseBranch", build_metadata.release_channel, block=True)
  params.put_bool("IsReleaseSpBranch", build_metadata.release_sp_channel, block=True)
  params.put("HardwareSerial", serial, block=True)

  # Online device registration is disabled; retain the local unregistered identity.
  dongle_id = params.get("DongleId") or UNREGISTERED_DONGLE_ID
  os.environ['DONGLE_ID'] = dongle_id  # Needed for swaglog
  os.environ['GIT_ORIGIN'] = build_metadata.openpilot.git_normalized_origin # Needed for swaglog
  os.environ['GIT_BRANCH'] = build_metadata.channel # Needed for swaglog
  os.environ['GIT_COMMIT'] = build_metadata.openpilot.git_commit # Needed for swaglog

  if not build_metadata.openpilot.is_dirty:
    os.environ['CLEAN'] = '1'

  # init logging
  sentry.init(sentry.SentryProject.SELFDRIVE)
  cloudlog.bind_global(dongle_id=dongle_id,
                       version=build_metadata.openpilot.version,
                       origin=build_metadata.openpilot.git_normalized_origin,
                       branch=build_metadata.channel,
                       commit=build_metadata.openpilot.git_commit,
                       dirty=build_metadata.openpilot.is_dirty,
                       device=HARDWARE.get_device_type())

  # preimport all processes
  for p in managed_processes.values():
    p.prepare()


def manager_cleanup() -> None:
  # send signals to kill all procs
  for p in managed_processes.values():
    p.stop(block=False)

  # ensure all are killed
  for p in managed_processes.values():
    p.stop(block=True)

  cloudlog.info("everything is dead")


def manager_thread() -> None:
  cloudlog.bind(daemon="manager")
  cloudlog.info("manager start")
  cloudlog.info({"environ": os.environ})

  params = Params()

  ignore: list[str] = []
  if os.getenv("NOBOARD") is not None:
    ignore.append("pandad")
  ignore += [x for x in os.getenv("BLOCK", "").split(",") if len(x) > 0]

  sm = messaging.SubMaster(['deviceState', 'carParams', 'pandaStates'], poll='deviceState')
  pm = messaging.PubMaster(['managerState'])

  write_onroad_params(False, params)
  ensure_running(managed_processes.values(), False, params=params, CP=sm['carParams'], not_run=ignore)

  started_prev = False
  ignition_prev = False

  while True:
    sm.update(1000)

    started = sm['deviceState'].started

    if started and not started_prev:
      params.clear_all(ParamKeyFlag.CLEAR_ON_ONROAD_TRANSITION)
    elif not started and started_prev:
      params.clear_all(ParamKeyFlag.CLEAR_ON_OFFROAD_TRANSITION)

    ignition = any(ps.ignitionLine or ps.ignitionCan for ps in sm['pandaStates'] if ps.pandaType != log.PandaState.PandaType.unknown)
    if ignition and not ignition_prev:
      params.clear_all(ParamKeyFlag.CLEAR_ON_IGNITION_ON)

    # update onroad params, which drives pandad's safety setter thread
    if started != started_prev:
      write_onroad_params(started, params)

    started_prev = started
    ignition_prev = ignition

    ensure_running(managed_processes.values(), started, params=params, CP=sm['carParams'], not_run=ignore)

    running = ' '.join("{}{}\u001b[0m".format("\u001b[32m" if p.proc.is_alive() else "\u001b[31m", p.name)
                       for p in managed_processes.values() if p.proc)
    print(running)
    cloudlog.debug(running)

    # send managerState
    msg = messaging.new_message('managerState', valid=True)
    msg.managerState.processes = [p.get_process_state_msg() for p in managed_processes.values()]
    pm.send('managerState', msg)

    # kick AGNOS power monitoring watchdog
    try:
      if sm.all_checks(['deviceState']):
        with atomic_write("/var/tmp/power_watchdog", "w", overwrite=True) as f:
          f.write(str(time.monotonic()))
    except Exception:
      pass

    # Exit main loop when uninstall/shutdown/reboot is needed
    shutdown = False
    for param in ("DoUninstall", "DoShutdown", "DoReboot"):
      if params.get_bool(param):
        shutdown = True
        params.put("LastManagerExitReason", f"{param} {datetime.datetime.now()}", block=True)
        cloudlog.warning(f"Shutting down manager - {param} set")

    if shutdown:
      break


def main() -> None:
  manager_init()
  if os.getenv("PREPAREONLY") is not None:
    return

  # SystemExit on sigterm
  signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(1))

  try:
    manager_thread()
  except Exception:
    traceback.print_exc()
    sentry.capture_exception()
  finally:
    manager_cleanup()

  params = Params()
  if params.get_bool("DoUninstall"):
    cloudlog.warning("uninstalling")
    HARDWARE.uninstall()
  elif params.get_bool("DoReboot"):
    cloudlog.warning("reboot")
    HARDWARE.reboot()
  elif params.get_bool("DoShutdown"):
    cloudlog.warning("shutdown")
    HARDWARE.shutdown()


if __name__ == "__main__":
  unblock_stdout()

  try:
    main()
  except KeyboardInterrupt:
    print("got CTRL-C, exiting")
  except Exception:
    cloudlog.exception("Manager failed to start")

    try:
      managed_processes['ui'].stop()
    except Exception:
      pass

    # Show last 3 lines of traceback
    error = traceback.format_exc(-3)
    error = "Manager failed to start\n\n" + error
    with TextWindow(error) as t:
      t.wait_for_exit()

    raise

  # manual exit because we are forked
  sys.exit(0)
