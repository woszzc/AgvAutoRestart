import ctypes
import csv
import json
import os
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime, time as dt_time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
    QSpinBox, QTabWidget, QVBoxLayout, QWidget
)

APP_NAME = "智能应用定时重启"
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ICON_FILE = BASE_DIR / "app.ico"
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "AgvAutoRestart"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_DIR = CONFIG_DIR / "logs"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "enabled": True,
    "api_url": "http://127.0.0.1:5001/api/app/agv-task/u-nfinished-task",
    "interval_minutes": 5,
    "start_time": "08:00",
    "end_time": "15:00",
    "weekdays": [0, 2, 4],  # Monday, Wednesday, Friday
    "restart_delay_seconds": 10,
    "tasks": [
        {
            "enabled": True,
            "name": "CNCConsole重启",
            "process_keyword": "HiP.CNCConsole",
            "match_mode": "name",
            "start_path": r"D:\Hip\Hip.CNC.Publish\HiP.CNCConsole.exe",
            "start_type": "exe",
            "run_as_admin": True,
        },
        {
            "enabled": True,
            "name": "AGV重启",
            "process_keyword": "AGV",
            "match_mode": "window",
            "start_path": r"C:\Users\zhichao.zhu\Documents\service\AGV\restart.bat",
            "start_type": "bat",
            "run_as_admin": True,
        },
    ],
    "port_monitors": [
        {
            "enabled": False,
            "weekdays": [0, 1, 2, 3, 4, 5, 6],
            "interval_minutes": 1,
            "port": 0,
            "cooldown_minutes": 10,
            "task_index": -1,  # -1 表示执行全部启用任务
        }
    ],
}


def load_config():
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
        merged = json.loads(json.dumps(DEFAULT_CONFIG))
        merged.update({k: v for k, v in cfg.items() if k != "tasks"})
        merged["tasks"] = cfg.get("tasks", merged["tasks"])
        raw_tasks = cfg.get("tasks", [])
        for i, task in enumerate(merged["tasks"]):
            if i < len(raw_tasks) and "match_mode" in raw_tasks[i]:
                continue
            # 旧配置没有 match_mode：AGV 这类跑在 .NET Host（dotnet.exe）下的应用，进程名是 dotnet，默认改用窗口标题匹配
            if task.get("process_keyword", "").strip().upper() == "AGV":
                task["match_mode"] = "window"
            else:
                task.setdefault("match_mode", "name")
            task.setdefault("kill_port_enabled", False)
            task.setdefault("kill_port", 0)
        if "port_monitors" not in merged:
            # 旧配置：单个 port_monitor 字典 -> 转成列表
            merged["port_monitors"] = [merged.get("port_monitor", {})]
        merged.pop("port_monitor", None)
        if not merged["port_monitors"]:
            merged["port_monitors"].append({})
        for mon in merged["port_monitors"]:
            for key, value in DEFAULT_CONFIG["port_monitors"][0].items():
                mon.setdefault(key, json.loads(json.dumps(value)))
        return merged
    except Exception:
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg):
    tmp = CONFIG_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    tmp.replace(CONFIG_FILE)


def log(message):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    print(line)
    try:
        with (LOG_DIR / f"{datetime.now():%Y-%m-%d}.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    return line


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_as_admin(executable, args="", working_dir=None):
    shell32 = ctypes.windll.shell32
    result = shell32.ShellExecuteW(
        None,
        "runas",
        executable,
        args,
        working_dir or None,
        1,
    )
    if result <= 32:
        raise RuntimeError(f"管理员启动失败，ShellExecute 返回值：{result}")


def start_target(path, start_type):
    path = os.path.expandvars(os.path.expanduser(path))
    if not os.path.exists(path):
        raise FileNotFoundError(f"启动文件不存在：{path}")

    workdir = str(Path(path).parent)

    if start_type.lower() == "bat" or path.lower().endswith(".bat"):
        # 管理员运行批处理文件：cmd.exe /c "xxx.bat"
        cmd = f'/c "{path}"'
        run_as_admin(os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe"), cmd, workdir)
    else:
        run_as_admin(path, "", workdir)


def get_processes():
    """返回 [(pid, image_name), ...]，兼容中文 Windows。"""
    result = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    processes = []
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) >= 2:
            try:
                processes.append((int(row[1]), row[0]))
            except ValueError:
                continue
    return processes


def get_processes_with_window_titles():
    """返回 [(pid, image_name, window_title), ...]，tasklist /V 的最后一列是窗口标题。

    任务管理器里 .NET Host 下面显示的「AGV」就是窗口标题，用它可以定位 dotnet.exe。
    """
    result = subprocess.run(
        ["tasklist", "/V", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    processes = []
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) >= 9:
            try:
                pid = int(row[1])
            except ValueError:
                continue
            title = row[8].strip()
            if title and title.upper() not in ("N/A", "暂缺"):
                processes.append((pid, row[0], title))
    return processes


def get_process_cmdlines():
    """返回 [(pid, image_name, cmdline), ...]，通过 PowerShell 查询命令行，
    用于匹配 dotnet.exe 这类宿主进程（命令行里包含 AGV.dll / 路径）。"""
    script = "Get-CimInstance Win32_Process | Select-Object ProcessId, Name, CommandLine | ConvertTo-Json -Compress"
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    out = []
    for item in data:
        try:
            out.append((int(item["ProcessId"]), item.get("Name") or "", item.get("CommandLine") or ""))
        except (KeyError, TypeError, ValueError):
            continue
    return out


MATCH_MODE_TEXT = {"name": "进程名", "window": "窗口标题", "cmdline": "命令行"}


def kill_by_keyword(keyword, match_mode="name"):
    keyword = keyword.strip()
    if not keyword:
        return 0

    current_pid = os.getpid()
    targets = []

    if match_mode == "window":
        # 窗口标题区分大小写匹配，避免误杀（如 AGV 与 AgvAutoRestart）
        targets = [
            (pid, f"{image_name}（窗口标题：{title}）")
            for pid, image_name, title in get_processes_with_window_titles()
            if keyword in title
        ]
    elif match_mode == "cmdline":
        # 命令行同样区分大小写匹配
        targets = [
            (pid, f"{image_name}（命令行：{cmdline}）")
            for pid, image_name, cmdline in get_process_cmdlines()
            if cmdline and keyword in cmdline
        ]
    else:
        lower = keyword.lower()
        targets = [(pid, image_name) for pid, image_name in get_processes() if lower in image_name.lower()]

    killed = 0
    for pid, desc in targets:
        if pid == current_pid:
            continue
        p = subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if p.returncode == 0:
            killed += 1
            log(f"已结束进程：{desc}，PID={pid}")
        else:
            log(f"结束进程失败：{desc}，PID={pid}，{p.stderr.strip()}")
    return killed


def get_pids_by_port(port):
    """返回占用指定端口的 PID 列表，解析 netstat -ano。"""
    result = subprocess.run(
        ["netstat", "-ano"],
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    suffix = f":{port}"
    pids = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].upper() in ("TCP", "UDP") and parts[1].endswith(suffix):
            try:
                pid = int(parts[-1])
            except ValueError:
                continue
            if pid > 0:
                pids.add(pid)
    return sorted(pids)


def kill_by_port(port):
    killed = 0
    current_pid = os.getpid()
    for pid in get_pids_by_port(port):
        if pid == current_pid:
            continue
        p = subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if p.returncode == 0:
            killed += 1
            log(f"已结束占用端口 {port} 的进程，PID={pid}")
        else:
            log(f"结束占用端口 {port} 的进程失败，PID={pid}，{p.stderr.strip()}")
    return killed


def http_get_json(url, timeout=10):
    # 只使用 Python 标准库，打包后无需 requests。
    from urllib.request import Request, urlopen
    req = Request(url, method="GET", headers={"User-Agent": "AgvAutoRestart/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def check_port_open(port, host="127.0.0.1", timeout=3):
    """尝试 TCP 连接指定端口，连上返回 True，连不上返回 False。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class RestartWorker(QObject):
    log_signal = Signal(str)
    status_signal = Signal(str)
    today_restart_signal = Signal(bool)

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.last_check_monotonic = 0.0
        self.last_restart_date = None
        self.restarting = False

    def emit_log(self, message):
        line = log(message)
        self.log_signal.emit(line)

    def in_schedule(self, now=None):
        now = now or datetime.now()
        if not self.config.get("enabled", True):
            return False
        if now.weekday() not in self.config.get("weekdays", [0, 2, 4]):
            return False

        start = datetime.strptime(self.config.get("start_time", "08:00"), "%H:%M").time()
        end = datetime.strptime(self.config.get("end_time", "15:00"), "%H:%M").time()
        return start <= now.time() <= end

    def check_once(self, force=False):
        if self.restarting:
            self.status_signal.emit("正在重启，暂不检查")
            return

        now = datetime.now()
        if not force and not self.in_schedule(now):
            self.status_signal.emit("当前不在执行时间段")
            return

        if not force and self.last_restart_date == now.date():
            self.status_signal.emit("今天已经重启过，跳过")
            return

        try:
            self.status_signal.emit("正在检查未完成 AGV 任务...")
            data = http_get_json(self.config["api_url"], timeout=10)

            if not isinstance(data, list):
                raise ValueError(f"接口返回不是数组：{type(data).__name__}")

            self.emit_log(f"接口检查成功，返回任务数：{len(data)}")

            if len(data) == 0:
                self.restart_all()
            else:
                self.status_signal.emit(f"检测到 {len(data)} 个未完成任务，不重启")
        except Exception as e:
            self.status_signal.emit("接口检查失败")
            self.emit_log(f"接口检查失败：{e}")

    def restart_all(self):
        if self.last_restart_date == datetime.now().date():
            self.emit_log("今天已经执行过重启，跳过本次操作")
            return

        enabled_tasks = [x for x in self.config.get("tasks", []) if x.get("enabled", True)]
        if not enabled_tasks:
            self.emit_log("没有启用任何重启任务，跳过")
            return

        self.restarting = True
        today = datetime.now().date()
        self.status_signal.emit("未完成任务为空，开始重启...")
        self.emit_log("接口返回空数组，开始执行应用重启")

        try:
            # 先全部结束
            for task in enabled_tasks:
                self.kill_task(task)

            delay = max(0, int(self.config.get("restart_delay_seconds", 10)))
            self.status_signal.emit(f"进程已结束，{delay} 秒后启动...")
            self.emit_log(f"所有目标进程处理完成，等待 {delay} 秒")
            time.sleep(delay)

            # 再全部启动
            all_ok = True
            for task in enabled_tasks:
                ok = self.start_task(task)
                all_ok = all_ok and ok

            # 无论个别启动是否失败，本次重启动作当天只执行一次，避免 5 分钟后反复杀进程。
            self.last_restart_date = today
            self.today_restart_signal.emit(True)

            if all_ok:
                self.status_signal.emit("重启成功")
                self.emit_log("本次重启全部完成")
            else:
                self.status_signal.emit("部分重启失败，请查看日志")
                self.emit_log("本次重启完成，但存在启动失败项目")
        except Exception as e:
            self.emit_log(f"重启流程异常：{e}")
            self.status_signal.emit("重启流程异常")
        finally:
            self.restarting = False

    def kill_task(self, task):
        """按任务配置结束进程或杀掉端口。"""
        name = task.get("name", "未命名任务")
        try:
            if task.get("kill_port_enabled"):
                port = int(task.get("kill_port", 0))
                count = kill_by_port(port)
                self.emit_log(f"{name}：已结束占用端口 {port} 的进程 {count} 个")
            else:
                count = kill_by_keyword(task.get("process_keyword", ""), task.get("match_mode", "name"))
                self.emit_log(f"{name}：结束进程 {count} 个")
        except Exception as e:
            self.emit_log(f"{name}：结束进程异常：{e}")

    def start_task(self, task):
        """启动单个任务的目标程序，返回是否成功。"""
        name = task.get("name", "未命名任务")
        path = task.get("start_path", "")
        try:
            start_target(path, task.get("start_type", "exe"))
            self.emit_log(f"{name}：启动成功：{path}")
            return True
        except Exception as e:
            self.emit_log(f"{name}：启动失败：{e}")
            return False

    def run_tasks_once(self, tasks, source="手动"):
        """执行一组任务：结束进程/端口 -> 等待 -> 启动，不占用 UI 线程。"""
        names = "、".join(t.get("name", "未命名任务") for t in tasks)
        self.emit_log(f"{source}执行任务：{names}")
        self.status_signal.emit(f"正在执行：{names}")
        for task in tasks:
            self.kill_task(task)
        delay = max(0, int(self.config.get("restart_delay_seconds", 10)))
        self.emit_log(f"{delay} 秒后启动...")
        QTimer.singleShot(delay * 1000, lambda: [self.start_task(t) for t in tasks])


class TaskDialog(QDialog):
    def __init__(self, task=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑重启任务")
        self.setMinimumWidth(560)

        task = task or {}
        self.enabled = QCheckBox("启用此任务")
        self.enabled.setChecked(task.get("enabled", True))

        self.name = QLineEdit(task.get("name", ""))
        self.keyword = QLineEdit(task.get("process_keyword", ""))
        self.path = QLineEdit(task.get("start_path", ""))

        browse = QPushButton("浏览...")
        browse.clicked.connect(self.browse)

        path_layout = QHBoxLayout()
        path_layout.addWidget(self.path)
        path_layout.addWidget(browse)

        self.start_type = QComboBox()
        self.start_type.addItem("EXE 程序", "exe")
        self.start_type.addItem("BAT 批处理", "bat")
        index = self.start_type.findData(task.get("start_type", "exe"))
        if index >= 0:
            self.start_type.setCurrentIndex(index)

        self.match_mode = QComboBox()
        self.match_mode.addItem("进程名包含（如 HiP.CNCConsole.exe）", "name")
        self.match_mode.addItem("窗口标题包含（.NET Host 下的 AGV 选这个）", "window")
        self.match_mode.addItem("命令行包含（dotnet.exe 宿主进程）", "cmdline")
        index = self.match_mode.findData(task.get("match_mode", "name"))
        if index >= 0:
            self.match_mode.setCurrentIndex(index)

        self.kill_port_enabled = QCheckBox("杀掉端口（勾选后代替结束进程）")
        self.kill_port_enabled.setChecked(task.get("kill_port_enabled", False))
        self.kill_port = QSpinBox()
        self.kill_port.setRange(1, 65535)
        port = int(task.get("kill_port", 0) or 0)
        self.kill_port.setValue(port if port > 0 else 8080)

        self.admin = QCheckBox("使用管理员身份启动")
        self.admin.setChecked(task.get("run_as_admin", True))

        self.kill_port_enabled.toggled.connect(self.sync_kill_mode_ui)
        self.sync_kill_mode_ui()

        form = QFormLayout()
        form.addRow("", self.enabled)
        form.addRow("任务名称", self.name)
        form.addRow("进程名称包含", self.keyword)
        form.addRow("匹配方式", self.match_mode)
        form.addRow("", self.kill_port_enabled)
        form.addRow("端口号", self.kill_port)
        form.addRow("启动文件", path_layout)
        form.addRow("文件类型", self.start_type)
        form.addRow("", self.admin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.validate_and_accept)
        buttons.rejected.connect(self.reject)

        hint = QLabel(
            "提示：目标应用如果跑在 .NET Host（dotnet.exe）下，进程名是 dotnet，"
            "请改用「窗口标题」或「命令行」匹配；这两种方式区分大小写，可避免误杀 AgvAutoRestart 等进程。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("subtitle")

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(buttons)

    def sync_kill_mode_ui(self):
        use_port = self.kill_port_enabled.isChecked()
        self.kill_port.setEnabled(use_port)
        self.keyword.setEnabled(not use_port)
        self.match_mode.setEnabled(not use_port)

    def browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择启动文件",
            "",
            "可执行/批处理 (*.exe *.bat);;所有文件 (*.*)"
        )
        if path:
            self.path.setText(path)
            if path.lower().endswith(".bat"):
                self.start_type.setCurrentIndex(self.start_type.findData("bat"))
            else:
                self.start_type.setCurrentIndex(self.start_type.findData("exe"))

    def validate_and_accept(self):
        if not self.name.text().strip():
            QMessageBox.warning(self, "提示", "请输入任务名称")
            return
        if not self.kill_port_enabled.isChecked() and not self.keyword.text().strip():
            QMessageBox.warning(self, "提示", "请输入进程名称包含关键字")
            return
        if not self.path.text().strip():
            QMessageBox.warning(self, "提示", "请选择启动文件")
            return
        self.accept()

    def get_task(self):
        return {
            "enabled": self.enabled.isChecked(),
            "name": self.name.text().strip(),
            "process_keyword": self.keyword.text().strip(),
            "start_path": self.path.text().strip(),
            "start_type": self.start_type.currentData(),
            "match_mode": self.match_mode.currentData(),
            "kill_port_enabled": self.kill_port_enabled.isChecked(),
            "kill_port": self.kill_port.value(),
            "run_as_admin": self.admin.isChecked(),
        }


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.worker = RestartWorker(self.config)

        self.monitor_state = {}

        self.setWindowTitle(APP_NAME)
        self.resize(920, 680)
        self.build_ui()
        self.load_ui_from_config()

        self.timer = QTimer(self)
        self.timer.setInterval(10_000)
        self.timer.timeout.connect(self.on_timer)
        self.timer.start()

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(1000)
        self.refresh_timer.timeout.connect(self.update_clock)
        self.refresh_timer.start()

        self.worker.log_signal.connect(self.append_log)
        self.worker.status_signal.connect(self.set_status)
        self.worker.today_restart_signal.connect(self.update_today_badge)

        self.append_log(f"程序启动，配置文件：{CONFIG_FILE}")
        self.set_status("监控已启动")

    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(14)

        title = QLabel("智能应用定时重启")
        title.setObjectName("title")
        root.addWidget(title)

        subtitle = QLabel("按周一 / 周三 / 周五，在指定时间检查 AGV 未完成任务；任务为空时自动重启配置的应用。")
        subtitle.setObjectName("subtitle")
        root.addWidget(subtitle)

        # 状态卡片
        card = QFrame()
        card.setObjectName("card")
        card_layout = QHBoxLayout(card)
        card_layout.setContentsMargins(18, 14, 18, 14)

        self.status_label = QLabel("监控状态")
        self.status_label.setObjectName("status")
        self.today_badge = QLabel("今日已重启")
        self.today_badge.setObjectName("badge")
        self.today_badge.hide()
        self.reset_today_btn = QPushButton("重置今日标记")
        self.reset_today_btn.hide()
        self.reset_today_btn.clicked.connect(self.reset_today_flag)
        self.clock_label = QLabel()
        self.clock_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        card_layout.addWidget(self.status_label)
        card_layout.addWidget(self.today_badge)
        card_layout.addWidget(self.reset_today_btn)
        card_layout.addStretch()
        card_layout.addWidget(self.clock_label)
        root.addWidget(card)

        # 监控配置（紧凑布局）
        config_card = QFrame()
        config_card.setObjectName("card")
        form = QFormLayout(config_card)
        form.setContentsMargins(14, 10, 14, 10)
        form.setSpacing(6)

        self.api_url = QLineEdit()
        self.interval = QSpinBox()
        self.interval.setRange(1, 1440)
        self.interval.setSuffix(" 分钟")

        self.start_time = QLineEdit()
        self.start_time.setPlaceholderText("HH:mm")
        self.end_time = QLineEdit()
        self.end_time.setPlaceholderText("HH:mm")

        self.week_checks = []

        for i, name in enumerate(["周一", "周二", "周三", "周四", "周五", "周六", "周日"]):
            check = QCheckBox(name)
            check.setProperty("weekday", i)
            self.week_checks.append(check)

        days = QHBoxLayout()

        for check in self.week_checks:
            days.addWidget(check)

        days.addStretch()

        self.delay = QSpinBox()
        self.delay.setRange(0, 3600)
        self.delay.setSuffix(" 秒")

        schedule = QHBoxLayout()
        schedule.addWidget(QLabel("间隔"))
        schedule.addWidget(self.interval)
        schedule.addWidget(QLabel("开始"))
        schedule.addWidget(self.start_time)
        schedule.addWidget(QLabel("结束"))
        schedule.addWidget(self.end_time)
        schedule.addWidget(QLabel("等待"))
        schedule.addWidget(self.delay)
        schedule.addStretch()

        form.addRow("检查接口", self.api_url)
        form.addRow("时间安排", schedule)
        form.addRow("执行星期", days)

        root.addWidget(config_card)

        # 端口监控（多标签页，每页一个端口）
        pm_header = QHBoxLayout()
        pm_title = QLabel("端口监控")
        pm_title.setObjectName("section")
        pm_header.addWidget(pm_title)
        pm_header.addStretch()
        pm_add_btn = QPushButton("+ 新增页")
        pm_add_btn.clicked.connect(lambda: self.add_pm_page())
        pm_del_btn = QPushButton("删除当前页")
        pm_del_btn.clicked.connect(self.remove_pm_page)
        pm_header.addWidget(pm_add_btn)
        pm_header.addWidget(pm_del_btn)
        root.addLayout(pm_header)

        self.pm_tabs = QTabWidget()
        self.pm_pages = []
        pm_card = QFrame()
        pm_card.setObjectName("card")
        pm_card_layout = QVBoxLayout(pm_card)
        pm_card_layout.setContentsMargins(10, 8, 10, 10)
        pm_card_layout.addWidget(self.pm_tabs)
        root.addWidget(pm_card)

        # 任务列表
        task_header = QHBoxLayout()
        task_title = QLabel("重启任务")
        task_title.setObjectName("section")
        task_header.addWidget(task_title)
        task_header.addStretch()

        add_btn = QPushButton("+ 添加任务")
        add_btn.clicked.connect(self.add_task)
        edit_btn = QPushButton("编辑")
        edit_btn.clicked.connect(self.edit_task)
        del_btn = QPushButton("删除")
        del_btn.clicked.connect(self.delete_task)
        run_btn = QPushButton("执行一次")
        run_btn.clicked.connect(self.run_selected_task_once)

        task_header.addWidget(add_btn)
        task_header.addWidget(edit_btn)
        task_header.addWidget(del_btn)
        task_header.addWidget(run_btn)
        root.addLayout(task_header)

        self.task_list = QListWidget()
        self.task_list.setMinimumHeight(150)
        self.task_list.itemDoubleClicked.connect(lambda _: self.edit_task())
        root.addWidget(self.task_list, 1)

        # 底部
        bottom = QHBoxLayout()
        self.enabled = QCheckBox("启用自动监控")
        bottom.addWidget(self.enabled)
        bottom.addStretch()

        save_btn = QPushButton("保存配置")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self.save_ui_config)

        test_btn = QPushButton("立即检查")
        test_btn.clicked.connect(self.manual_check)

        log_btn = QPushButton("打开日志目录")
        log_btn.clicked.connect(self.open_log_dir)

        bottom.addWidget(test_btn)
        bottom.addWidget(log_btn)
        bottom.addWidget(save_btn)
        root.addLayout(bottom)

        self.log_view = QLabel("日志将在文件中持续记录。")
        self.log_view.setWordWrap(True)
        self.log_view.setObjectName("log")
        root.addWidget(self.log_view)

    def load_ui_from_config(self):
        c = self.config
        self.api_url.setText(c.get("api_url", ""))
        self.interval.setValue(int(c.get("interval_minutes", 5)))
        self.start_time.setText(c.get("start_time", "08:00"))
        self.end_time.setText(c.get("end_time", "15:00"))
        weekdays = c.get("weekdays", [0, 2, 4])

        for check in self.week_checks:
            weekday = check.property("weekday")
            check.setChecked(weekday in weekdays)
        self.delay.setValue(int(c.get("restart_delay_seconds", 10)))
        self.enabled.setChecked(c.get("enabled", True))
        self.refresh_task_list()

        while self.pm_tabs.count():
            self.pm_tabs.removeTab(0)
        self.pm_pages = []
        for monitor in c.get("port_monitors", []):
            self.add_pm_page(monitor)

    def refresh_task_list(self):
        self.task_list.clear()
        for i, task in enumerate(self.config.get("tasks", [])):
            enabled = "启用" if task.get("enabled", True) else "停用"
            if task.get("kill_port_enabled"):
                kill_text = f"杀掉端口 {task.get('kill_port', '')}"
            else:
                mode_text = MATCH_MODE_TEXT.get(task.get("match_mode", "name"), "进程名")
                kill_text = f"{mode_text}包含「{task.get('process_keyword', '')}」"
            text = (
                f"{i + 1}. [{enabled}] {task.get('name', '')}\n"
                f"   结束：{kill_text}\n"
                f"   启动：{task.get('start_path', '')}"
            )
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, i)
            self.task_list.addItem(item)
        self.refresh_pm_task_combo()

    def save_ui_config(self):
        try:
            datetime.strptime(self.start_time.text().strip(), "%H:%M")
            datetime.strptime(self.end_time.text().strip(), "%H:%M")
        except ValueError:
            QMessageBox.warning(self, "配置错误", "开始时间和结束时间必须是 HH:mm，例如 08:00")
            return

        weekdays = []

        for check in self.week_checks:
            if check.isChecked():
                weekdays.append(check.property("weekday"))

        if not weekdays:
            QMessageBox.warning(self, "配置错误", "至少选择一个执行星期")
            return

        self.config["enabled"] = self.enabled.isChecked()
        self.config["api_url"] = self.api_url.text().strip()
        self.config["interval_minutes"] = self.interval.value()
        self.config["start_time"] = self.start_time.text().strip()
        self.config["end_time"] = self.end_time.text().strip()
        self.config["weekdays"] = weekdays
        self.config["restart_delay_seconds"] = self.delay.value()

        monitors = []
        for record in self.pm_pages:
            pm_weekdays = [check.property("weekday") for check in record["week_checks"] if check.isChecked()]
            if record["enabled"].isChecked():
                if record["port"].value() <= 0:
                    QMessageBox.warning(self, "配置错误", "端口监控：请填写监控端口号")
                    return
                if not pm_weekdays:
                    QMessageBox.warning(self, "配置错误", "端口监控：至少选择一个执行星期")
                    return
            monitors.append({
                "enabled": record["enabled"].isChecked(),
                "weekdays": pm_weekdays,
                "interval_minutes": record["interval"].value(),
                "port": record["port"].value(),
                "cooldown_minutes": record["cooldown"].value(),
                "task_index": record["task"].currentData(),
            })
        self.config["port_monitors"] = monitors

        save_config(self.config)
        self.worker.config = self.config
        self.worker.last_check_monotonic = 0
        self.append_log("配置已保存")
        self.set_status("配置已保存，监控继续运行")

    def add_task(self):
        dlg = TaskDialog(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.config.setdefault("tasks", []).append(dlg.get_task())
            self.refresh_task_list()
            self.save_ui_config()

    def get_selected_index(self):
        item = self.task_list.currentItem()
        if not item:
            return -1
        return int(item.data(Qt.ItemDataRole.UserRole))

    def edit_task(self):
        index = self.get_selected_index()
        if index < 0:
            QMessageBox.information(self, "提示", "请选择一个任务")
            return

        dlg = TaskDialog(self.config["tasks"][index], self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.config["tasks"][index] = dlg.get_task()
            self.refresh_task_list()
            self.save_ui_config()

    def delete_task(self):
        index = self.get_selected_index()
        if index < 0:
            QMessageBox.information(self, "提示", "请选择一个任务")
            return
        name = self.config["tasks"][index].get("name", "")
        if QMessageBox.question(self, "确认删除", f"确定删除「{name}」吗？") == QMessageBox.StandardButton.Yes:
            self.config["tasks"].pop(index)
            self.refresh_task_list()
            self.save_ui_config()

    def run_selected_task_once(self):
        index = self.get_selected_index()
        if index < 0:
            QMessageBox.information(self, "提示", "请选择一个任务")
            return
        task = self.config["tasks"][index]
        name = task.get("name", "未命名任务")
        delay = self.config.get("restart_delay_seconds", 10)
        if QMessageBox.question(
            self, "确认执行", f"立即执行一次「{name}」？\n将结束进程/端口，{delay} 秒后重新启动。"
        ) == QMessageBox.StandardButton.Yes:
            self.worker.run_tasks_once([task])

    def reset_today_flag(self):
        self.worker.last_restart_date = None
        self.update_today_badge(False)
        self.append_log("已重置今日重启标记，下次检查可再次触发重启")
        self.set_status("今日重启标记已重置")

    def update_today_badge(self, restarted):
        self.today_badge.setVisible(bool(restarted))
        self.reset_today_btn.setVisible(bool(restarted))

    def manual_check(self):
        self.save_ui_config()
        self.worker.check_once(force=True)

    def on_timer(self):
        self.on_port_monitor()

        now = datetime.now()
        if not self.worker.in_schedule(now):
            self.set_status("当前不在执行时间段")
            return

        if self.worker.last_restart_date == now.date():
            self.set_status("今天已经重启过，等待下一工作日")
            return

        interval_seconds = max(60, int(self.config.get("interval_minutes", 5)) * 60)
        current = time.monotonic()
        if self.worker.last_check_monotonic == 0:
            self.worker.last_check_monotonic = current
            self.worker.check_once()
        elif current - self.worker.last_check_monotonic >= interval_seconds:
            self.worker.last_check_monotonic = current
            self.worker.check_once()

    def refresh_pm_task_combo(self):
        for record in self.pm_pages:
            combo = record["task"]
            current = combo.currentData()
            combo.clear()
            combo.addItem("全部启用任务", -1)
            for i, task in enumerate(self.config.get("tasks", [])):
                combo.addItem(task.get("name", f"任务 {i + 1}"), i)
            index = combo.findData(current)
            if index >= 0:
                combo.setCurrentIndex(index)

    def add_pm_page(self, monitor=None):
        """新增一个端口监控标签页。"""
        monitor = monitor or {}
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(12, 10, 12, 10)
        form.setSpacing(6)

        enabled = QCheckBox("启用本页端口监控（端口连不上时自动执行重启任务）")
        enabled.setChecked(monitor.get("enabled", False))

        port = QSpinBox()
        port.setRange(0, 65535)
        port.setSpecialValueText("未设置")
        port.setValue(int(monitor.get("port", 0) or 0))

        interval = QSpinBox()
        interval.setRange(1, 1440)
        interval.setSuffix(" 分钟")
        interval.setValue(int(monitor.get("interval_minutes", 1)))

        cooldown = QSpinBox()
        cooldown.setRange(1, 1440)
        cooldown.setSuffix(" 分钟")
        cooldown.setValue(int(monitor.get("cooldown_minutes", 10)))

        row = QHBoxLayout()
        row.addWidget(QLabel("端口"))
        row.addWidget(port)
        row.addWidget(QLabel("间隔"))
        row.addWidget(interval)
        row.addWidget(QLabel("冷却"))
        row.addWidget(cooldown)
        row.addStretch()

        week_checks = []
        for i, name in enumerate(["周一", "周二", "周三", "周四", "周五", "周六", "周日"]):
            check = QCheckBox(name)
            check.setProperty("weekday", i)
            check.setChecked(i in monitor.get("weekdays", list(range(7))))
            week_checks.append(check)
        days = QHBoxLayout()
        for check in week_checks:
            days.addWidget(check)
        days.addStretch()

        task = QComboBox()

        form.addRow("", enabled)
        form.addRow("检查安排", row)
        form.addRow("执行星期", days)
        form.addRow("执行任务", task)

        record = {
            "widget": page, "enabled": enabled, "port": port,
            "interval": interval, "cooldown": cooldown,
            "week_checks": week_checks, "task": task,
        }
        self.pm_pages.append(record)
        port.valueChanged.connect(
            lambda value, r=record: self.pm_tabs.setTabText(
                self.pm_tabs.indexOf(r["widget"]), f"端口 {value if value else '未设置'}"
            )
        )
        self.pm_tabs.addTab(page, f"端口 {port.value() if port.value() else '未设置'}")
        self.pm_tabs.setCurrentWidget(page)
        self.refresh_pm_task_combo()
        index = task.findData(monitor.get("task_index", -1))
        if index >= 0:
            task.setCurrentIndex(index)
        return record

    def remove_pm_page(self):
        index = self.pm_tabs.currentIndex()
        if index < 0:
            QMessageBox.information(self, "提示", "没有可删除的端口监控页")
            return
        title = self.pm_tabs.tabText(index)
        if QMessageBox.question(self, "确认删除", f"确定删除「{title}」吗？") != QMessageBox.StandardButton.Yes:
            return
        self.pm_tabs.removeTab(index)
        self.pm_pages.pop(index)
        self.monitor_state.clear()
        self.save_ui_config()

    def resolve_pm_tasks(self, pm):
        tasks = self.config.get("tasks", [])
        index = pm.get("task_index", -1)
        if isinstance(index, int) and 0 <= index < len(tasks):
            return [tasks[index]]
        return [t for t in tasks if t.get("enabled", True)]

    def on_port_monitor(self):
        now = datetime.now()
        current = time.monotonic()
        for i, pm in enumerate(self.config.get("port_monitors", [])):
            if not pm.get("enabled"):
                continue
            if now.weekday() not in pm.get("weekdays", list(range(7))):
                continue
            port = int(pm.get("port", 0) or 0)
            if port <= 0:
                continue

            st = self.monitor_state.setdefault(
                i, {"last_check": 0.0, "last_trigger": float("-inf"), "port_up": None}
            )
            interval_seconds = max(30, int(pm.get("interval_minutes", 1)) * 60)
            if st["last_check"] and current - st["last_check"] < interval_seconds:
                continue
            st["last_check"] = current

            up = check_port_open(port)
            if up:
                if st["port_up"] is not True:
                    self.worker.emit_log(f"端口监控：端口 {port} 恢复可达")
                st["port_up"] = True
                continue

            if st["port_up"] is not False:
                self.worker.emit_log(f"端口监控：端口 {port} 连不上")
            st["port_up"] = False

            cooldown_seconds = max(1, int(pm.get("cooldown_minutes", 10))) * 60
            if current - st["last_trigger"] < cooldown_seconds:
                continue

            tasks = self.resolve_pm_tasks(pm)
            if not tasks:
                self.worker.emit_log(f"端口 {port} 连不上，但没有可执行的重启任务")
                continue
            st["last_trigger"] = current
            self.worker.run_tasks_once(tasks, source=f"端口 {port} 连不上，")

    def update_clock(self):
        self.clock_label.setText(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def set_status(self, text):
        self.status_label.setText(f"● {text}")

    def append_log(self, text):
        self.log_view.setText(text)

    def open_log_dir(self):
        os.startfile(str(LOG_DIR))

    def closeEvent(self, event):
        try:
            self.save_ui_config()
        except Exception:
            pass
        event.accept()


STYLE = """
QMainWindow, QWidget {
    background: #f5f7fb;
    color: #202938;
    font-family: "Microsoft YaHei";
    font-size: 14px;
}
QLabel#title {
    font-size: 28px;
    font-weight: 700;
    color: #172033;
}
QLabel#subtitle {
    color: #687386;
    font-size: 13px;
}
QLabel#section {
    font-size: 18px;
    font-weight: 700;
}
QLabel#status {
    color: #16834a;
    font-weight: 700;
}
QLabel#badge {
    background: #fff4e5;
    color: #d46b08;
    border: 1px solid #ffd591;
    border-radius: 10px;
    padding: 3px 10px;
    font-weight: 700;
}
QFrame#card {
    background: white;
    border: 1px solid #e4e8ef;
    border-radius: 12px;
}
QLineEdit, QSpinBox, QComboBox, QListWidget {
    background: white;
    border: 1px solid #d7dde8;
    border-radius: 7px;
    padding: 7px 9px;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
    border: 1px solid #5b8def;
}
QListWidget {
    padding: 5px;
}
QListWidget::item {
    padding: 10px;
    border-radius: 7px;
    margin: 2px;
}
QListWidget::item:selected {
    background: #e9f0ff;
    color: #1e56c8;
}
QPushButton {
    background: white;
    border: 1px solid #d5dbe5;
    border-radius: 7px;
    padding: 8px 15px;
}
QPushButton:hover {
    background: #f0f4fa;
}
QPushButton#primary {
    background: #3678f6;
    color: white;
    border: none;
    font-weight: 700;
}
QPushButton#primary:hover {
    background: #2868df;
}
QLabel#log {
    color: #687386;
    background: #eef2f7;
    border-radius: 7px;
    padding: 9px;
}
QCheckBox {
    spacing: 6px;
}
QTabWidget::pane {
    border: none;
    background: transparent;
}
QTabBar::tab {
    background: #eef2f7;
    border: 1px solid #e4e8ef;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
    padding: 5px 14px;
    margin-right: 4px;
}
QTabBar::tab:selected {
    background: white;
    color: #1e56c8;
    font-weight: 700;
}
"""


def main():
    if sys.platform != "win32":
        QMessageBox.critical(None, "系统不支持", "此程序的进程结束和管理员启动功能针对 Windows。")
        return

    # 声明 AppUserModelID，让任务栏/窗口显示自己的图标而不是 Python 图标
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AgvAutoRestart.App")
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(STYLE)
    if ICON_FILE.exists():
        app.setWindowIcon(QIcon(str(ICON_FILE)))

    window = MainWindow()
    window.show()

    if not is_admin():
        log("提示：当前 GUI 未以管理员身份运行。结束其他程序时可能因权限不足而失败；目标程序启动仍会请求管理员权限。")

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
