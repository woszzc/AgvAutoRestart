# 智能应用定时重启

基于 PySide6 + PyInstaller 的 Windows GUI 应用。

## 功能

- 默认周一、周三、周五执行。
- 默认 08:00 ~ 15:00 监控。
- 默认每 5 分钟请求：
  `http://127.0.0.1:5001/api/app/agv-task/u-nfinished-task`
- 接口返回 `[]` 时执行一次当天重启。
- 重启流程：
  1. 根据任务配置，结束进程名包含指定关键字的进程。
  2. 等待默认 10 秒。
  3. 以管理员身份启动 EXE 或 BAT。
- 结束进程支持三种匹配方式：进程名 / 窗口标题 / 命令行。
  跑在 .NET Host（dotnet.exe）下的应用（如 AGV）进程名是 dotnet，
  请选择「窗口标题包含」或「命令行包含」；这两种方式区分大小写。
- 当天重启过后，即使接口再次返回空数组，也不会重复重启。
- 配置自动保存，下次打开自动加载。
- 日志按天保存到 `%APPDATA%\AgvAutoRestart\logs\`。
- 可在 GUI 中增加、修改、删除任意数量的重启任务。

## 默认任务

### CNCConsole

- 进程关键字：`HiP.CNCConsole`
- 启动文件：
  `D:\Hip\Hip.CNC.Publish\HiP.CNCConsole.exe`
- 管理员启动：是

### AGV

- 进程关键字：`AGV`
- 启动文件：
  `C:\Users\zhichao.zhu\Documents\service\AGV\restart.bat`
- 管理员启动：是

## 开发运行

```bat
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## 打包

直接双击：

`build.bat`

最终文件：

`dist\AgvAutoRestart.exe`

## 注意

因为程序需要结束其他进程，所以建议把 `AgvAutoRestart.exe` 本身设置为“以管理员身份运行”。

如果不希望每次启动 EXE 都弹 UAC，可以用 Windows 任务计划程序配置“使用最高权限运行”。
