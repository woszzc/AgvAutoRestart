import ctypes
import csv
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime, time as dt_time
from pathlib import Path

try:
    import winreg
except ImportError:
    winreg = None

from PySide6.QtCore import Qt, QTimer, QThread, Signal, QObject, QPointF
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QProxyStyle, QPushButton, QSpinBox, QStyle, QSystemTrayIcon,
    QTabWidget, QVBoxLayout, QWidget
)

APP_NAME = "智能应用定时重启"
APP_VERSION = "1.0.0"
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
    "wecom_key": "",  # 企业微信群机器人 Webhook key
    "update_url": "",  # 更新服务器清单地址
    "log_retention_days": 30,  # 日志保留天数
    "tasks": [
        {
            "enabled": True,
            "name": "CNCConsole重启",
            "process_keyword": "HiP.CNCConsole",
            "match_mode": "name",
            "start_path": r"D:\Hip\Hip.CNC.Publish\HiP.CNCConsole.exe",
            "start_type": "exe",
            "run_as_admin": True,
            "wecom_notify": False,
        },
        {
            "enabled": True,
            "name": "AGV重启",
            "process_keyword": "AGV",
            "match_mode": "window",
            "start_path": r"C:\Users\zhichao.zhu\Documents\service\AGV\restart.bat",
            "start_type": "bat",
            "run_as_admin": True,
            "wecom_notify": False,
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


class AppStyle(QProxyStyle):
    """Keep checkbox indicators clear and consistent across Windows themes."""

    def pixelMetric(self, metric, option=None, widget=None):
        if metric in (QStyle.PixelMetric.PM_IndicatorWidth, QStyle.PixelMetric.PM_IndicatorHeight):
            return 18
        return super().pixelMetric(metric, option, widget)

    def drawPrimitive(self, element, option, painter, widget=None):
        if element != QStyle.PrimitiveElement.PE_IndicatorCheckBox:
            return super().drawPrimitive(element, option, painter, widget)

        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        checked = bool(option.state & QStyle.StateFlag.State_On)
        partial = bool(option.state & QStyle.StateFlag.State_NoChange)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        border = QColor("#2563eb") if checked or partial else QColor("#94a3b8")
        fill = QColor("#2563eb") if checked or partial else QColor("#ffffff")
        if hovered and not checked and not partial:
            border = QColor("#3b82f6")
            fill = QColor("#eff6ff")
        if not enabled:
            border = QColor("#cbd5e1")
            fill = QColor("#e2e8f0") if checked or partial else QColor("#f8fafc")

        rect = option.rect.adjusted(1, 1, -1, -1)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(border, 1.5))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 4, 4)

        if checked:
            tick = QColor("#ffffff") if enabled else QColor("#94a3b8")
            painter.setPen(QPen(tick, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawLine(
                QPointF(rect.left() + 4, rect.center().y()),
                QPointF(rect.left() + 7.5, rect.bottom() - 4),
            )
            painter.drawLine(
                QPointF(rect.left() + 7.5, rect.bottom() - 4),
                QPointF(rect.right() - 3.5, rect.top() + 4),
            )
        elif partial:
            painter.setPen(QPen(QColor("#ffffff"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(
                QPointF(rect.left() + 4, rect.center().y()),
                QPointF(rect.right() - 4, rect.center().y()),
            )
        painter.restore()


class FlatSpinBox(QSpinBox):
    """Numeric input without stepper buttons, with value and suffix left aligned."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


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


AUTOSTART_VALUE_NAME = "AgvAutoRestart"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
LAYERS_KEY_PATH = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"


def get_app_executable():
    """返回当前程序路径：打包后是 EXE，开发模式下是 main.py 脚本。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return Path(__file__).resolve()


def get_launch_command():
    """开机自启的注册表命令行：打包后直接跑 EXE，开发模式用 pythonw 跑脚本。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    interpreter = sys.executable
    if interpreter.lower().endswith("python.exe"):
        interpreter = str(Path(interpreter).with_name("pythonw.exe"))
    return f'"{interpreter}" "{get_app_executable()}"'


def get_autostart():
    """查询是否已开启开机自启（HKCU Run 注册表项）。"""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, AUTOSTART_VALUE_NAME)
            return bool(value)
    except (FileNotFoundError, OSError):
        return False


def set_autostart(enabled):
    """写入/删除 HKCU Run 注册表项，实现开机登录后自动启动。"""
    if winreg is None:
        raise RuntimeError("当前系统不支持注册表操作")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ, get_launch_command())
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_VALUE_NAME)
            except FileNotFoundError:
                pass


def get_run_as_admin_flag():
    """查询程序是否被标记为始终以管理员身份运行（AppCompatFlags Layers）。"""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LAYERS_KEY_PATH, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, str(get_app_executable()))
            return "RUNASADMIN" in (value or "").upper()
    except (FileNotFoundError, OSError):
        return False


def set_run_as_admin_flag(enabled):
    """通过兼容层 RUNASADMIN 标记，让程序每次启动都请求 UAC 管理员权限。"""
    if winreg is None:
        raise RuntimeError("当前系统不支持注册表操作")
    exe_path = str(get_app_executable())
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LAYERS_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, exe_path, 0, winreg.REG_SZ, "~ RUNASADMIN")
        else:
            try:
                winreg.DeleteValue(key, exe_path)
            except FileNotFoundError:
                pass


def send_wecom_notify(wecom_key, content):
    """通过企业微信群机器人 webhook 发送 markdown 消息，返回接口响应。"""
    from urllib.request import Request, urlopen
    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={wecom_key}"
    payload = json.dumps(
        {"msgtype": "markdown", "markdown": {"content": content}}, ensure_ascii=False
    ).encode("utf-8")
    req = Request(url, data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=10) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def parse_version(text):
    """版本号转元组用于比较，非数字部分按 0 处理。"""
    parts = []
    for chunk in str(text or "").strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def fetch_update_info(url):
    """从服务器拉取更新清单：{version, url, notes}。"""
    data = http_get_json(url, timeout=10)
    if not isinstance(data, dict):
        raise ValueError("更新清单格式不正确，应为 JSON 对象")
    return data


def download_file(url, dest, timeout=300):
    """下载文件到指定路径，返回目标路径。"""
    from urllib.request import Request, urlopen
    req = Request(url, method="GET", headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    with urlopen(req, timeout=timeout) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)
    return dest


def cleanup_old_logs(days):
    """删除超过保留天数的历史日志文件。"""
    if days <= 0:
        return
    cutoff = time.time() - days * 86400
    for log_file in LOG_DIR.glob("*.log"):
        try:
            if log_file.stat().st_mtime < cutoff:
                log_file.unlink()
        except OSError:
            continue


class RestartWorker(QObject):
    log_signal = Signal(str)
    status_signal = Signal(str)
    result_signal = Signal(str)
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
            results = [(task, self.start_task(task)) for task in enabled_tasks]
            all_ok = all(ok for _, ok in results)

            # 无论个别启动是否失败，本次重启动作当天只执行一次，避免 5 分钟后反复杀进程。
            self.last_restart_date = today
            self.today_restart_signal.emit(True)

            if all_ok:
                self.status_signal.emit("重启成功")
                self.emit_log("本次重启全部完成")
                self.result_signal.emit("定时重启：全部成功")
            else:
                self.status_signal.emit("部分重启失败，请查看日志")
                self.emit_log("本次重启完成，但存在启动失败项目")
                self.result_signal.emit("定时重启：存在启动失败项目")
            self.notify_results(results, "定时重启")
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

        def start_all():
            results = [(task, self.start_task(task)) for task in tasks]
            ok = all(success for _, success in results)
            self.result_signal.emit(f"{names}：执行成功" if ok else f"{names}：存在启动失败")
            self.notify_results(results, source.rstrip("，") or "手动")

        QTimer.singleShot(delay * 1000, start_all)

    def notify_results(self, results, source):
        """按任务勾选的企业微信通知发送执行结果，key 来自设置。"""
        wecom_key = (self.config.get("wecom_key") or "").strip()
        targets = [(task, ok) for task, ok in results if task.get("wecom_notify")]
        if not targets:
            return
        if not wecom_key:
            self.emit_log("有任务勾选了企业微信通知，但设置里未填写 key，跳过通知")
            return
        try:
            lines = [
                "### 应用重启通知",
                f">来源: {source}",
                f">主机: {socket.gethostname()}",
                f">时间: {datetime.now():%Y-%m-%d %H:%M:%S}",
            ]
            for task, ok in targets:
                color = "info" if ok else "warning"
                text = "成功" if ok else "失败"
                lines.append(f">**{task.get('name', '未命名任务')}**: <font color=\"{color}\">{text}</font>")
            reply = send_wecom_notify(wecom_key, "\n".join(lines))
            if reply.get("errcode") == 0:
                self.emit_log("企业微信通知发送成功")
            else:
                self.emit_log(f"企业微信通知发送失败：{reply.get('errmsg')}")
        except Exception as e:
            self.emit_log(f"企业微信通知发送异常：{e}")


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
        self.kill_port = FlatSpinBox()
        self.kill_port.setRange(1, 65535)
        port = int(task.get("kill_port", 0) or 0)
        self.kill_port.setValue(port if port > 0 else 8080)

        self.admin = QCheckBox("使用管理员身份启动")
        self.admin.setChecked(task.get("run_as_admin", True))

        self.wecom_notify = QCheckBox("企业微信通知（执行结束后发送，key 在设置里填写）")
        self.wecom_notify.setChecked(task.get("wecom_notify", False))

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
        form.addRow("", self.wecom_notify)

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
            "wecom_notify": self.wecom_notify.isChecked(),
        }


class SettingsDialog(QDialog):
    """设置弹窗：开机自启、管理员启动、企业微信 key、更新服务器、日志保留。"""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(620)
        self.imported = False

        self.autostart = QCheckBox("开机后自动启动（登录 Windows 后自动运行本程序）")
        self.autostart.setChecked(get_autostart())

        self.run_admin = QCheckBox("以管理员身份启动（每次启动本程序时请求管理员权限）")
        self.run_admin.setChecked(get_run_as_admin_flag())

        wecom_row = QHBoxLayout()
        self.wecom_key = QLineEdit((config.get("wecom_key") or "").strip())
        self.wecom_key.setPlaceholderText("企业微信群机器人 Webhook 的 key")
        test_btn = QPushButton("发送测试通知")
        test_btn.clicked.connect(self.test_wecom)
        wecom_row.addWidget(self.wecom_key, 1)
        wecom_row.addWidget(test_btn)

        self.update_url = QLineEdit((config.get("update_url") or "").strip())
        self.update_url.setPlaceholderText("更新清单地址，如 http://server/updates/AgvAutoRestart.json")

        self.log_days = FlatSpinBox()
        self.log_days.setRange(1, 365)
        self.log_days.setSuffix(" 天")
        self.log_days.setValue(int(config.get("log_retention_days", 30)))

        form = QFormLayout()
        form.setSpacing(8)
        form.addRow("", self.autostart)
        form.addRow("", self.run_admin)
        form.addRow("企业微信 Key", wecom_row)
        form.addRow("更新服务器", self.update_url)
        form.addRow("日志保留", self.log_days)

        bottom = QHBoxLayout()
        self.version_label = QLabel(f"当前版本：v{APP_VERSION}")
        self.version_label.setObjectName("subtitle")
        bottom.addWidget(self.version_label)
        bottom.addStretch()
        export_btn = QPushButton("导出配置")
        export_btn.clicked.connect(self.export_config)
        import_btn = QPushButton("导入配置")
        import_btn.clicked.connect(self.import_config)
        check_update_btn = QPushButton("检查更新")
        check_update_btn.clicked.connect(self.check_update)
        bottom.addWidget(export_btn)
        bottom.addWidget(import_btn)
        bottom.addWidget(check_update_btn)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        hint = QLabel(
            "提示：勾选「以管理员身份启动」后，程序每次启动都会弹出 UAC 确认；"
            "同时勾选开机自启时，登录后需要点一次确认。"
            "更新清单为 JSON：{\"version\": \"1.2.0\", \"url\": \"下载地址.exe\", \"notes\": \"更新说明\"}。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("subtitle")

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(bottom)
        layout.addWidget(hint)
        layout.addWidget(buttons)

    def get_result(self):
        return {
            "autostart": self.autostart.isChecked(),
            "run_admin": self.run_admin.isChecked(),
            "wecom_key": self.wecom_key.text().strip(),
            "update_url": self.update_url.text().strip(),
            "log_retention_days": self.log_days.value(),
        }

    def test_wecom(self):
        key = self.wecom_key.text().strip()
        if not key:
            QMessageBox.warning(self, "提示", "请先填写企业微信 Key")
            return
        content = (
            "### 测试通知\n"
            f">主机: {socket.gethostname()}\n"
            f">时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f">{APP_NAME} 企业微信通知配置正常"
        )
        try:
            reply = send_wecom_notify(key, content)
            if reply.get("errcode") == 0:
                QMessageBox.information(self, "测试通知", "发送成功，请到企业微信群查看")
            else:
                QMessageBox.warning(self, "测试通知", f"发送失败：{reply.get('errmsg')}")
        except Exception as e:
            QMessageBox.warning(self, "测试通知", f"发送异常：{e}")

    def check_update(self):
        self.parent().check_for_update(url_override=self.update_url.text().strip())

    def export_config(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出配置", "AgvAutoRestart-config.json", "JSON 文件 (*.json)")
        if not path:
            return
        try:
            shutil.copyfile(CONFIG_FILE, path)
            QMessageBox.information(self, "导出配置", f"配置已导出到：\n{path}")
        except Exception as e:
            QMessageBox.warning(self, "导出配置", f"导出失败：{e}")

    def import_config(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入配置", "", "JSON 文件 (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                json.load(f)
            shutil.copyfile(path, CONFIG_FILE)
        except Exception as e:
            QMessageBox.warning(self, "导入配置", f"导入失败：{e}")
            return
        self.imported = True
        QMessageBox.information(self, "导入配置", "配置已导入，窗口将重新加载配置")
        self.accept()


class MainWindow(QMainWindow):
    check_requested = Signal(bool)
    run_tasks_requested = Signal(list, str)

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.worker = RestartWorker(self.config)

        # 重启/检查等耗时操作放到独立线程，避免阻塞 UI（HTTP 请求、taskkill、sleep、UAC 启动等待）
        self.worker_thread = QThread(self)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.start()

        self.monitor_state = {}
        self.quitting = False
        self.tray_notice_shown = False

        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.resize(1180, 860)
        self.setMinimumSize(960, 720)
        self.build_ui()
        self.load_ui_from_config()
        self.setup_tray()

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
        self.worker.result_signal.connect(self.append_log)
        self.worker.today_restart_signal.connect(self.update_today_badge)

        # 队列连接：信号在工作线程的事件循环里执行，不占用 UI 线程
        self.check_requested.connect(self.worker.check_once)
        self.run_tasks_requested.connect(self.worker.run_tasks_once)

        cleanup_old_logs(int(self.config.get("log_retention_days", 30)))

        self.append_log(f"程序启动，配置文件：{CONFIG_FILE}")
        self.set_status("监控已启动")

    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # 状态卡片
        card = QFrame()
        card.setObjectName("card")
        card_layout = QHBoxLayout(card)
        card_layout.setContentsMargins(14, 10, 14, 10)

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

        self.settings_btn = QPushButton("⚙")
        self.settings_btn.setObjectName("iconBtn")
        self.settings_btn.setToolTip("设置")
        self.settings_btn.setFixedSize(36, 32)
        self.settings_btn.clicked.connect(self.open_settings)

        card_layout.addWidget(self.status_label)
        card_layout.addWidget(self.today_badge)
        card_layout.addWidget(self.reset_today_btn)
        card_layout.addStretch()
        card_layout.addWidget(self.clock_label)
        card_layout.addWidget(self.settings_btn)
        root.addWidget(card)

        # 监控配置（紧凑布局）
        config_card = QFrame()
        config_card.setObjectName("card")
        form = QFormLayout(config_card)
        form.setContentsMargins(14, 10, 14, 10)
        form.setSpacing(6)

        self.api_url = QLineEdit()
        self.interval = FlatSpinBox()
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

        self.delay = FlatSpinBox()
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
        pm_card_layout.setContentsMargins(8, 6, 8, 8)
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
            notify_text = "，企业微信通知" if task.get("wecom_notify") else ""
            text = (
                f"{i + 1}. [{enabled}] {task.get('name', '')}{notify_text}\n"
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
            return False

        weekdays = []

        for check in self.week_checks:
            if check.isChecked():
                weekdays.append(check.property("weekday"))

        if not weekdays:
            QMessageBox.warning(self, "配置错误", "至少选择一个执行星期")
            return False

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
                    return False
                if not pm_weekdays:
                    QMessageBox.warning(self, "配置错误", "端口监控：至少选择一个执行星期")
                    return False
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
        return True

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
            self.run_tasks_requested.emit([task], "手动")

    def reset_today_flag(self):
        self.worker.last_restart_date = None
        self.update_today_badge(False)
        self.append_log("已重置今日重启标记，下次检查可再次触发重启")
        self.set_status("今日重启标记已重置")

    def update_today_badge(self, restarted):
        self.today_badge.setVisible(bool(restarted))
        self.reset_today_btn.setVisible(bool(restarted))

    def manual_check(self):
        if self.save_ui_config():
            self.check_requested.emit(True)

    def open_settings(self):
        dlg = SettingsDialog(self.config, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if dlg.imported:
            self.config = load_config()
            self.worker.config = self.config
            self.load_ui_from_config()
            self.append_log("已导入并重新加载配置")
            return
        result = dlg.get_result()
        try:
            set_autostart(result["autostart"])
            set_run_as_admin_flag(result["run_admin"])
        except Exception as e:
            QMessageBox.warning(self, "设置失败", f"写入开机自启/管理员设置失败：{e}")
        self.config["wecom_key"] = result["wecom_key"]
        self.config["update_url"] = result["update_url"]
        self.config["log_retention_days"] = result["log_retention_days"]
        save_config(self.config)
        self.worker.config = self.config
        self.append_log("设置已保存")
        self.set_status("设置已保存")

    def check_for_update(self, silent=False, url_override=None):
        url = (url_override if url_override is not None else self.config.get("update_url", "")).strip()
        if not url:
            if not silent:
                QMessageBox.information(self, "检查更新", "请先在设置里填写更新服务器地址")
            return
        try:
            info = fetch_update_info(url)
        except Exception as e:
            self.append_log(f"检查更新失败：{e}")
            if not silent:
                QMessageBox.warning(self, "检查更新", f"检查更新失败：{e}")
            return

        remote_version = str(info.get("version", "")).strip()
        download_url = (info.get("url") or info.get("download_url") or "").strip()
        notes = str(info.get("notes", "")).strip()
        if not remote_version or not download_url:
            if not silent:
                QMessageBox.warning(self, "检查更新", "更新清单缺少 version 或 url 字段")
            return

        if parse_version(remote_version) <= parse_version(APP_VERSION):
            self.append_log(f"检查更新：当前已是最新版本 v{APP_VERSION}")
            if not silent:
                QMessageBox.information(self, "检查更新", f"当前已是最新版本 v{APP_VERSION}")
            return

        message = f"发现新版本 v{remote_version}（当前 v{APP_VERSION}）"
        if notes:
            message += f"\n\n更新说明：\n{notes}"
        message += "\n\n是否立即下载并更新？"
        if QMessageBox.question(self, "检查更新", message) == QMessageBox.StandardButton.Yes:
            self.apply_update(download_url, remote_version)

    def apply_update(self, url, version):
        if not getattr(sys, "frozen", False):
            QMessageBox.information(self, "提示", "开发模式（python main.py）不支持自动更新，请打包成 EXE 后使用")
            return
        exe_path = Path(sys.executable).resolve()
        tmp_path = exe_path.with_name(f"{exe_path.stem}_{version}_new.exe")
        try:
            self.set_status(f"正在下载新版本 v{version} ...")
            self.append_log(f"开始下载新版本 v{version}：{url}")
            download_file(url, tmp_path)
        except Exception as e:
            self.set_status("新版本下载失败")
            QMessageBox.critical(self, "更新失败", f"下载新版本失败：{e}")
            return

        # 退出后由外部 bat 等待进程结束 -> 替换 EXE -> 重新启动 -> 自删
        bat_path = Path(os.environ.get("TEMP", str(CONFIG_DIR))) / "AgvAutoRestart_updater.bat"
        script = "\r\n".join([
            "@echo off",
            f'set "PID={os.getpid()}"',
            f'set "SRC={tmp_path}"',
            f'set "DST={exe_path}"',
            ":wait",
            'tasklist /FI "PID eq %PID%" | find "%PID%" >nul && (',
            "    timeout /t 1 /nobreak >nul",
            "    goto wait",
            ")",
            'move /y "%SRC%" "%DST%" >nul',
            "if errorlevel 1 exit /b 1",
            'start "" "%DST%"',
            '(goto) 2>nul & del "%~f0"',
        ]) + "\r\n"
        try:
            bat_path.write_text(script, encoding="ascii")
            subprocess.Popen(
                [os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe"), "/c", str(bat_path)],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as e:
            QMessageBox.critical(self, "更新失败", f"启动更新脚本失败：{e}")
            return

        self.append_log(f"新版本 v{version} 下载完成，即将退出并完成更新")
        try:
            self.save_ui_config()
        except Exception:
            pass
        self.quitting = True
        self.tray_icon.hide()
        QApplication.quit()

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
            self.check_requested.emit(False)
        elif current - self.worker.last_check_monotonic >= interval_seconds:
            self.worker.last_check_monotonic = current
            self.check_requested.emit(False)

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

        port = FlatSpinBox()
        port.setRange(0, 65535)
        port.setSpecialValueText("未设置")
        port.setValue(int(monitor.get("port", 0) or 0))

        interval = FlatSpinBox()
        interval.setRange(1, 1440)
        interval.setSuffix(" 分钟")
        interval.setValue(int(monitor.get("interval_minutes", 1)))

        cooldown = FlatSpinBox()
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
            self.run_tasks_requested.emit(tasks, f"端口 {port} 连不上，")

    def update_clock(self):
        self.clock_label.setText(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def set_status(self, text):
        self.status_label.setText(f"● {text}")

    def append_log(self, text):
        self.log_view.setText(text)

    def open_log_dir(self):
        os.startfile(str(LOG_DIR))

    def setup_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(self.windowIcon())
        self.tray_icon.setToolTip(APP_NAME)

        tray_menu = QMenu(self)
        show_action = QAction("显示主窗口", self)
        show_action.triggered.connect(self.show_from_tray)
        quit_action = QAction("退出应用", self)
        quit_action.triggered.connect(self.quit_application)
        tray_menu.addAction(show_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()

    def show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_from_tray()

    def quit_application(self):
        self.quitting = True
        try:
            if not self.save_ui_config():
                self.quitting = False
                self.show_from_tray()
                return
        except Exception:
            pass
        self.tray_icon.hide()
        self.worker_thread.quit()
        self.worker_thread.wait(2000)
        QApplication.quit()

    def closeEvent(self, event):
        if self.quitting:
            event.accept()
            return

        try:
            if not self.save_ui_config():
                event.ignore()
                return
        except Exception:
            pass
        self.hide()
        event.ignore()
        if not self.tray_notice_shown:
            self.tray_icon.showMessage(APP_NAME, "已最小化到系统托盘", QSystemTrayIcon.MessageIcon.Information, 2000)
            self.tray_notice_shown = True


STYLE = """
QMainWindow, QWidget {
    background: #f3f6fa;
    color: #1e293b;
    font-family: "Microsoft YaHei";
    font-size: 14px;
}
QLabel#subtitle {
    color: #64748b;
    font-size: 13px;
}
QLabel#section {
    font-size: 16px;
    font-weight: 700;
    color: #0f172a;
}
QLabel#status {
    color: #15803d;
    font-weight: 700;
}
QLabel#badge {
    background: #fff7ed;
    color: #c2410c;
    border: 1px solid #fdba74;
    border-radius: 7px;
    padding: 3px 9px;
    font-weight: 700;
}
QFrame#card {
    background: #ffffff;
    border: 1px solid #dbe3ee;
    border-radius: 8px;
}
QLineEdit, QSpinBox, QComboBox, QListWidget {
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: #2563eb;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
    border: 1px solid #2563eb;
    background: #ffffff;
}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {
    color: #94a3b8;
    background: #f1f5f9;
    border-color: #e2e8f0;
}
QListWidget {
    padding: 4px;
    outline: none;
}
QListWidget::item {
    padding: 9px;
    border-radius: 6px;
    margin: 2px;
    border: 1px solid transparent;
}
QListWidget::item:selected {
    background: #eff6ff;
    color: #1d4ed8;
    border-color: #bfdbfe;
}
QPushButton {
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 6px;
    padding: 7px 13px;
    min-height: 18px;
}
QPushButton:hover {
    background: #f8fafc;
    border-color: #94a3b8;
}
QPushButton:pressed {
    background: #e2e8f0;
}
QPushButton#primary {
    background: #2563eb;
    color: #ffffff;
    border: 1px solid #2563eb;
    font-weight: 700;
}
QPushButton#primary:hover {
    background: #1d4ed8;
    border-color: #1d4ed8;
}
QPushButton#iconBtn {
    background: transparent;
    border: 1px solid transparent;
    font-size: 18px;
    color: #475569;
    padding: 0px;
    min-height: 0px;
}
QPushButton#iconBtn:hover {
    background: #e9eef5;
    border-color: #cbd5e1;
    color: #1d4ed8;
}
QPushButton#iconBtn:pressed {
    background: #dbe3ee;
}
QLabel#log {
    color: #475569;
    background: #e9eef5;
    border-radius: 6px;
    padding: 8px 10px;
}
QCheckBox {
    spacing: 7px;
    min-height: 22px;
}
QCheckBox:hover {
    color: #1d4ed8;
}
QTabWidget::pane {
    border: 1px solid #dbe3ee;
    background: #ffffff;
    top: -1px;
}
QTabBar::tab {
    background: #e9eef5;
    color: #475569;
    border: 1px solid #dbe3ee;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 6px 14px;
    margin-right: 3px;
}
QTabBar::tab:selected {
    background: #ffffff;
    color: #1d4ed8;
    font-weight: 700;
    border-bottom-color: #ffffff;
}
QTabBar::tab:hover:!selected {
    background: #f1f5f9;
}
QMenu {
    background: #ffffff;
    color: #1e293b;
    border: 1px solid #cbd5e1;
    padding: 5px;
}
QMenu::item {
    padding: 7px 24px;
    border-radius: 5px;
}
QMenu::item:selected {
    background: #eff6ff;
    color: #1d4ed8;
}
QMenu::separator {
    height: 1px;
    background: #e2e8f0;
    margin: 4px 7px;
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
    app.setQuitOnLastWindowClosed(False)
    app.setStyle(AppStyle(app.style()))
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
