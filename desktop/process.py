import json
import logging
import sys
from pathlib import Path
from time import monotonic

import pywintypes
import win32api
import win32con
import win32job
from PySide6.QtCore import QObject, QProcess, QTimer, Signal

logger = logging.getLogger(__name__)


class BotProcess(QObject):
    changed = Signal()
    configuration_changed = Signal()
    exited = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.process = QProcess(self)
        self.process.started.connect(self.worker_started)
        self.process.readyReadStandardOutput.connect(self.read_events)
        self.process.errorOccurred.connect(self.process_error)
        self.process.finished.connect(self.finished)

        self.deadline = QTimer(self, singleShot=True)
        self.deadline.timeout.connect(self.timed_out)
        self.status = "Stopped"
        self.error = ""
        self.started_at = None
        self.configuration = None
        self.restarting = False
        self.exiting = False
        self.output = b""
        self.job = None

    @property
    def active(self):
        return self.process.state() != QProcess.ProcessState.NotRunning

    def start(self):
        if self.active or self.exiting:
            return
        self.status = "Starting"
        self.error = ""
        self.output = b""
        program = Path(sys.executable)
        if getattr(sys, "frozen", False):
            arguments = []
        else:
            program = program.with_name("pythonw.exe")
            arguments = [str(Path(__file__).resolve().parents[1] / "ops_manager.py")]
        self.deadline.start(45_000)
        self.process.start(str(program), [*arguments, "--bot-worker"])
        self.changed.emit()

    def worker_started(self):
        self.started_at = monotonic()
        try:
            self.job = win32job.CreateJobObject(None, "")
            limits = win32job.QueryInformationJobObject(
                self.job, win32job.JobObjectExtendedLimitInformation
            )
            limits["BasicLimitInformation"]["LimitFlags"] = (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                self.job, win32job.JobObjectExtendedLimitInformation, limits
            )
            child = win32api.OpenProcess(
                win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE,
                False,
                self.process.processId(),
            )
            try:
                win32job.AssignProcessToJobObject(self.job, child)
            finally:
                child.Close()
        except (OSError, pywintypes.error):
            self.error = "Windows could not attach the bot worker to its process group."
            self.process.kill()
        else:
            if self.status != "Stopping":
                self.process.write(b"start\n")
        self.changed.emit()

    def read_events(self):
        self.output += bytes(self.process.readAllStandardOutput())
        while b"\n" in self.output:
            line, self.output = self.output.split(b"\n", 1)
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if "configuration" in event:
                self.configuration = event["configuration"]
                self.configuration_changed.emit()
            if "error" in event:
                self.error = event["error"]
                self.process.closeWriteChannel()
            if event.get("ready") and self.status == "Starting":
                self.deadline.stop()
                self.status = "Running"
                logger.info("Bot worker is running (PID %s)", self.process.processId())
        self.changed.emit()

    def stop(self):
        if not self.active or self.status == "Stopping":
            return
        self.status = "Stopping"
        self.process.closeWriteChannel()
        self.deadline.start(15_000)
        self.changed.emit()

    def restart(self):
        if self.active:
            self.restarting = True
            self.stop()
        else:
            self.start()

    def exit(self):
        self.exiting = True
        self.restarting = False
        if self.active:
            self.stop()
        else:
            self.exited.emit()

    def timed_out(self):
        if self.status == "Stopping":
            self.error = "The bot did not stop within 15 seconds and Windows closed it."
        else:
            self.error = "The bot did not start within 45 seconds. See Open Logs."
        if self.job is not None:
            win32job.TerminateJobObject(self.job, 1)
        else:
            self.process.kill()

    def process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.error = "Windows could not start the bot worker."
            self.finished(1)

    def finished(self, code, _exit_status=None):
        self.read_events()
        self.deadline.stop()
        if self.job is not None:
            self.job.Close()
            self.job = None
        self.started_at = None
        self.configuration = None

        if self.status != "Stopping" and not self.error:
            self.error = f"Bot worker exited unexpectedly (code {code}). See Open Logs."
        if self.error:
            self.status = "Error"
            logger.error("%s", self.error)
        else:
            self.status = "Stopped"
            logger.info("Bot worker stopped (exit code %s)", code)
        self.changed.emit()
        self.configuration_changed.emit()

        if self.exiting:
            self.exited.emit()
        elif self.restarting:
            self.restarting = False
            self.start()
