import glob
import os
import time

import cereal.messaging as messaging
from openpilot.system.manager.process_config import managed_processes
from openpilot.system.hardware.hw import Paths
from openpilot.common.swaglog import cloudlog, ipchandler


class TestLogmessaged:
  def setup_method(self):
    # clear the IPC buffer in case some other tests used cloudlog and filled it
    ipchandler.close()
    ipchandler.connect()

    managed_processes['logmessaged'].start()
    self.sock = messaging.sub_sock("logMessage", timeout=1000, conflate=False)
    self.error_sock = messaging.sub_sock("errorLogMessage", timeout=1000, conflate=False)

    # ensure sockets are connected
    time.sleep(0.5)
    messaging.drain_sock(self.sock)
    messaging.drain_sock(self.error_sock)

  def teardown_method(self):
    del self.sock
    del self.error_sock
    managed_processes['logmessaged'].stop(block=True)

  def _get_log_files(self):
    return list(glob.glob(os.path.join(Paths.swaglog_root(), "swaglog.*")))

  def test_simple_log(self):
    log_files_before = set(self._get_log_files())
    msgs = [f"abc {i}" for i in range(10)]
    for m in msgs:
      cloudlog.error(m)
    time.sleep(0.5)
    log_messages = messaging.drain_sock(self.sock)
    error_messages = messaging.drain_sock(self.error_sock)
    assert len(log_messages) == len(msgs)
    assert len(error_messages) == len(msgs)
    assert set(self._get_log_files()) == log_files_before

  def test_big_log(self):
    log_files_before = set(self._get_log_files())
    n = 10
    msg = "a"*3*1024*1024
    for _ in range(n):
      cloudlog.info(msg)
    time.sleep(0.5)

    msgs = messaging.drain_sock(self.sock)
    assert len(msgs) == 0
    assert set(self._get_log_files()) == log_files_before

