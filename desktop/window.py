import logging
import sqlite3
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import monotonic

from pydantic import ValidationError
from PySide6.QtCore import (
    QObject,
    QRunnable,
    QSignalBlocker,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from bot.access import AccessStore
from config.instance_lock import InstanceAlreadyRunning, windows_instance
from config.paths import project_directory
from config.settings import TELEGRAM_USER_ID_LIMIT, Settings
from desktop.process import BotProcess

APP_NAME = "OPS Bot Manager"

STYLE = """
QWidget { background: #f1f4f9; color: #152741; font-family: 'Segoe UI'; font-size: 13px; }
QLabel#title { font-size: 24px; font-weight: 650; }
QLabel#section { font-size: 16px; font-weight: 650; }
QLabel#caption { color: #465a73; }
QLabel#error { color: #9f282f; }
QLabel#status { color: #163962; font-weight: bold; }
QFrame#process { background: #e5ecf7; border-radius: 9px; }
QFrame#process QLabel, QFrame#process QWidget#metrics { background: transparent; }
QPushButton, QLineEdit { background: #ffffff; border: 1px solid #aebed3;
                         border-radius: 5px; padding: 6px 10px; min-height: 20px; }
QPushButton:hover { background: #e0e9f8; }
QPushButton#primary { background: #183b65; color: #ffffff; border-color: #183b65; }
QPushButton#primary:hover { background: #265584; }
QPushButton:disabled, QPushButton#primary:disabled { background: #e7edf5;
                                                    color: #5b6a7d; border-color: #d4deeb; }
QLineEdit:focus, QPushButton:focus { border: 1px solid #3b6798; }
QTableWidget { background: #ffffff; border: 1px solid #becde0; gridline-color: #e5edf7;
               selection-background-color: #234c78; selection-color: #ffffff; }
QHeaderView::section { background: #e5ecf7; padding: 6px; border: 0; color: #253e5e; }
"""


def label(text, style=""):
    widget = QLabel(text)
    widget.setObjectName(style)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    return widget


def default_logs():
    try:
        return project_directory() / "logs"
    except (OSError, ValueError):
        return Path(sys.executable).resolve().parent / "logs"


class AccessResult(QObject):
    ready = Signal(object, str)


class AccessTask(QRunnable):
    def __init__(self, configuration, operation, user_id):
        super().__init__()
        self.configuration = configuration
        self.operation = operation
        self.user_id = user_id
        self.result = AccessResult()

    def run(self):
        message = ""
        try:
            if self.configuration is None:
                settings = Settings()
            else:
                settings = Settings.model_construct(**self.configuration)
                settings.database_path = Path(settings.database_path)
            store = AccessStore(configuration=settings)
            if self.operation == "grant":
                message = (
                    "Access granted."
                    if store.grant_from_desktop(self.user_id)
                    else "This ID already has access."
                )
            elif self.operation == "revoke":
                message = (
                    "Access revoked."
                    if store.revoke_from_desktop(self.user_id)
                    else "This ID no longer has access."
                )
            rows = store.authorized_users()
        except (ValidationError, OSError, ValueError, sqlite3.Error):
            if self.operation:
                message = "Could not change access. Refresh the list and check the configuration."
            else:
                message = (
                    "Could not load authorized users. Check .env and the database."
                )
            rows = None
        self.result.ready.emit(rows, message)


class ManagerWindow(QWidget):
    def __init__(self, app, icon):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(icon)
        self.setMinimumSize(490, 470)
        self.resize(560, 525)
        self.app = app
        self.bot = BotProcess(self)
        self.access_task = None
        self.access_rows = None
        self.access_configuration = None
        self.bot.changed.connect(self.update_process)
        self.bot.configuration_changed.connect(self.refresh_users)
        self.bot.exited.connect(self.finish_exit)

        self.status = label("Stopped", "status")
        self.pid = label("—")
        self.uptime = label("—")
        self.error = label("", "error")
        self.start_button = QPushButton("Start", objectName="primary")
        self.stop_button = QPushButton("Stop")
        self.restart_button = QPushButton("Restart")
        logs = QPushButton("Open Logs")
        self.start_button.clicked.connect(self.bot.start)
        self.stop_button.clicked.connect(self.bot.stop)
        self.restart_button.clicked.connect(self.bot.restart)
        logs.clicked.connect(self.open_logs)

        toolbar = QHBoxLayout()
        for button in (self.start_button, self.stop_button, self.restart_button):
            toolbar.addWidget(button)
        toolbar.addStretch(1)
        toolbar.addWidget(logs)

        process = QFrame(objectName="process")
        process_layout = QVBoxLayout(process)
        process_layout.setContentsMargins(14, 12, 14, 12)
        metrics = QWidget(objectName="metrics")
        grid = QGridLayout(metrics)
        grid.setContentsMargins(0, 0, 0, 6)
        for column, (title, value) in enumerate(
            (("Status", self.status), ("PID", self.pid), ("Uptime", self.uptime))
        ):
            grid.addWidget(label(title, "caption"), 0, column)
            grid.addWidget(value, 1, column)
            grid.setColumnStretch(column, 1)
        process_layout.addWidget(metrics)
        process_layout.addLayout(toolbar)
        process_layout.addWidget(self.error)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Telegram ID", "Name", "Role"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.table.itemSelectionChanged.connect(self.update_access_controls)
        self.user_id = QLineEdit(placeholderText="Telegram user ID")
        self.user_id.setAccessibleName("Telegram user ID")
        self.user_id.setMaxLength(32)
        self.user_id.returnPressed.connect(self.grant)
        self.add_button = QPushButton("Add")
        self.add_button.clicked.connect(self.grant)
        self.revoke_button = QPushButton("Revoke Access")
        self.revoke_button.clicked.connect(self.revoke)
        self.access_message = label("", "caption")
        access_buttons = QHBoxLayout()
        access_buttons.addWidget(self.user_id, 1)
        access_buttons.addWidget(self.add_button)
        access_buttons.addWidget(self.revoke_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(label(APP_NAME, "title"))
        layout.addWidget(process)
        layout.addSpacing(4)
        layout.addWidget(label("Authorized Users", "section"))
        layout.addWidget(self.table, 1)
        layout.addLayout(access_buttons)
        layout.addWidget(
            label(
                "Changes apply immediately. Admin IDs are managed in .env.", "caption"
            )
        )
        layout.addWidget(self.access_message)

        self.menu = QMenu(self)
        self.menu.addAction("Open", self.open_window)
        self.tray_start = self.menu.addAction("Start Bot", self.bot.start)
        self.tray_stop = self.menu.addAction("Stop Bot", self.bot.stop)
        self.tray_restart = self.menu.addAction("Restart Bot", self.bot.restart)
        self.menu.addSeparator()
        self.menu.addAction("Exit", self.begin_exit)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.tray_activated)
        self.tray.show()

        self.clock = QTimer(self, interval=1000)
        self.clock.timeout.connect(self.update_process)
        self.clock.start()
        self.refresh = QTimer(self, interval=3000)
        self.refresh.timeout.connect(self.refresh_users)
        self.refresh.start()
        self.update_process()

    def update_process(self):
        status = self.bot.status
        self.status.setText(status)
        self.tray.setToolTip(f"{APP_NAME} · {status}")
        self.pid.setText(str(self.bot.process.processId() or "—"))
        uptime = self.bot.started_at
        seconds = int(monotonic() - uptime) if uptime is not None else 0
        self.uptime.setText(
            f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
            if uptime is not None
            else "—"
        )
        self.error.setText(self.bot.error)
        self.error.setVisible(bool(self.bot.error))
        start = not self.bot.active and not self.bot.exiting
        stop = self.bot.active and status != "Stopping" and not self.bot.exiting
        self.start_button.setEnabled(start)
        self.stop_button.setEnabled(stop)
        self.restart_button.setEnabled(start or stop)
        self.tray_start.setEnabled(start)
        self.tray_stop.setEnabled(stop)
        self.tray_restart.setEnabled(start or stop)
        self.update_access_controls()

    def selected_user(self):
        row = self.table.currentRow()
        if self.access_rows and 0 <= row < len(self.access_rows):
            return self.access_rows[row]
        return None

    def access_available(self):
        return (
            self.access_task is None
            and not self.bot.exiting
            and (not self.bot.active or self.bot.configuration is not None)
        )

    def update_access_controls(self):
        ready = (
            self.access_available()
            and self.access_rows is not None
            and self.access_configuration is self.bot.configuration
        )
        selected = self.selected_user()
        self.add_button.setEnabled(ready)
        self.revoke_button.setEnabled(
            ready and selected is not None and not selected[2]
        )

    def refresh_users(self):
        if self.isVisible() and self.access_available():
            self.query_access()

    def query_access(self, operation=None, user_id=None):
        configuration = self.bot.configuration
        self.access_task = AccessTask(configuration, operation, user_id)
        self.access_task.result.ready.connect(self.show_users)
        QThreadPool.globalInstance().start(self.access_task)
        self.update_access_controls()

    def show_users(self, rows, message):
        configuration = self.access_task.configuration
        self.access_task = None
        if configuration is not self.bot.configuration:
            self.refresh_users()
            return
        self.access_configuration = configuration
        if self.access_rows != rows:
            selected = self.selected_user()
            with QSignalBlocker(self.table):
                self.table.clearSelection()
                self.table.setCurrentCell(-1, -1)
                self.access_rows = rows
                self.table.setRowCount(len(rows or []))
                self.table.setColumnHidden(
                    1, not any(name for _, name, _ in rows or [])
                )
                for row, (user_id, name, admin) in enumerate(rows or []):
                    for column, value in enumerate(
                        (str(user_id), name, "Admin" if admin else "User")
                    ):
                        self.table.setItem(row, column, QTableWidgetItem(value))
                    if selected is not None and user_id == selected[0]:
                        self.table.selectRow(row)
        if message or rows is None:
            self.access_message.setText(message)
        elif self.access_message.text().startswith("Could not load"):
            self.access_message.clear()
        self.update_access_controls()

    def grant(self):
        if (
            not self.add_button.isEnabled()
            or self.access_configuration is not self.bot.configuration
        ):
            return
        value = self.user_id.text().strip()
        if (
            not value.isascii()
            or not value.isdecimal()
            or not 0 < int(value) < TELEGRAM_USER_ID_LIMIT
        ):
            self.access_message.setText("Enter a valid Telegram user ID.")
            return
        self.query_access("grant", int(value))
        self.user_id.clear()

    def revoke(self):
        selected = self.selected_user()
        if not selected or not self.revoke_button.isEnabled():
            return
        user_id = selected[0]
        yes = QMessageBox.StandardButton.Yes
        self.refresh.stop()
        try:
            answer = QMessageBox.question(
                self,
                "Revoke Access",
                f"Revoke OPS Bot access for Telegram ID {user_id}?",
                yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
        finally:
            self.refresh.start()
        if (
            answer == yes
            and self.access_available()
            and self.access_configuration is self.bot.configuration
        ):
            self.query_access("revoke", user_id)

    def open_logs(self):
        try:
            if self.bot.configuration:
                path = Path(self.bot.configuration["log_dir"])
            else:
                try:
                    path = Settings().log_dir
                except (OSError, ValueError):
                    path = default_logs()
            path.mkdir(parents=True, exist_ok=True)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                raise OSError
        except OSError:
            QMessageBox.warning(
                self, APP_NAME, "Windows could not open the log folder."
            )

    def tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_window()

    def open_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.refresh_users()

    def begin_exit(self):
        self.bot.exit()

    def finish_exit(self):
        self.tray.hide()
        self.app.quit()

    def closeEvent(self, event):
        event.ignore()
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.hide()
        else:
            self.begin_exit()


def run_manager():
    app = QApplication([])
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    icon = QIcon(str(Path(__file__).with_name("ops_bot.ico")))
    app.setWindowIcon(icon)
    try:
        with windows_instance(r"Local\OPSBot.DesktopManager"):
            try:
                try:
                    logs = Settings().log_dir
                except (ValueError, OSError):
                    logs = default_logs()
                logs.mkdir(parents=True, exist_ok=True)
                handler = RotatingFileHandler(
                    logs / "manager.log",
                    maxBytes=1_000_000,
                    backupCount=3,
                    encoding="utf-8",
                )
                handler.setFormatter(
                    logging.Formatter("%(asctime)s %(levelname)s %(message)s")
                )
                logging.basicConfig(handlers=[handler], level=logging.INFO)
            except OSError:
                pass
            window = ManagerWindow(app, icon)
            window.show()
            QTimer.singleShot(0, window.bot.start)
            return app.exec()
    except InstanceAlreadyRunning:
        QMessageBox.information(
            None, APP_NAME, "OPS Bot Manager is already running in the system tray."
        )
        return 0
