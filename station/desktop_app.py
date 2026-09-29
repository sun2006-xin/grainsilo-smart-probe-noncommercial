"""Simple Windows desktop controller for the GrainSilo local station."""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import tkinter as tk
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

import server


APP_TITLE = "粮仓集成监控中心"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000


class StationRuntime:
    """Own the local HTTP server lifecycle without touching existing processes."""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, *,
                 start_weather_worker=True, server_module=server):
        self.host = host
        self.port = int(port)
        self.start_weather_worker = start_weather_worker
        self.server_module = server_module
        self.httpd = None
        self.thread = None
        self.weather_thread = None

    @property
    def running(self):
        return self.httpd is not None and self.thread is not None and self.thread.is_alive()

    @property
    def local_url(self):
        port = self.httpd.server_address[1] if self.httpd is not None else self.port
        return "http://127.0.0.1:%d/" % port

    def start(self):
        if self.running:
            return self.local_url
        os.makedirs(self.server_module.DATA_ROOT, exist_ok=True)
        self.server_module.init_db()
        httpd = self.server_module.ThreadingHTTPServer(
            (self.host, self.port), self.server_module.Handler)
        thread = threading.Thread(target=httpd.serve_forever,
                                 name="GrainSilo-Station", daemon=True)
        self.httpd = httpd
        self.thread = thread
        thread.start()
        if self.start_weather_worker:
            if self.weather_thread is None or not self.weather_thread.is_alive():
                self.weather_thread = threading.Thread(
                    target=self.server_module._weather_archive_worker,
                    name="GrainSilo-Weather-Archive", daemon=True)
                self.weather_thread.start()
        self.port = httpd.server_address[1]
        return self.local_url

    def stop(self):
        httpd, thread = self.httpd, self.thread
        if httpd is None:
            return
        if thread is not None and thread.is_alive():
            httpd.shutdown()
            thread.join(timeout=5)
        httpd.server_close()
        self.httpd = None
        self.thread = None


class GrainSiloDesktopApp(tk.Tk):
    def __init__(self, port=DEFAULT_PORT):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("520x330")
        self.minsize(480, 300)
        self.resizable(False, False)
        self.runtime = StationRuntime(port=port)
        self.log_file = None
        self.status_text = tk.StringVar(value="正在启动本地中心站…")
        self.url_text = tk.StringVar(value="http://127.0.0.1:%d/" % port)
        self._build_window()
        self._set_window_icon()
        self.protocol("WM_DELETE_WINDOW", self._confirm_close)
        self.after(250, self._start_station)

    def _build_window(self):
        self.configure(bg="#f1f7f4")
        frame = ttk.Frame(self, padding=(28, 24))
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="粮仓集成监控中心",
                  font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(frame, text="GrainSilo · 本地数据中心",
                  font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(3, 18))

        self.status_label = ttk.Label(frame, textvariable=self.status_text,
                                      font=("Microsoft YaHei UI", 11, "bold"))
        self.status_label.pack(anchor="w", pady=(0, 9))
        ttk.Label(frame, text="本机监控页面",
                  font=("Microsoft YaHei UI", 9)).pack(anchor="w")
        url_box = ttk.Entry(frame, textvariable=self.url_text, state="readonly")
        url_box.pack(fill="x", pady=(5, 12))

        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(0, 16))
        self.open_button = ttk.Button(actions, text="打开监控页面",
                                      command=self._open_dashboard, state="disabled")
        self.open_button.pack(side="left")
        ttk.Button(actions, text="复制本机地址",
                   command=self._copy_url).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="退出并停止中心站",
                   command=self._confirm_close).pack(side="right")

        note = ("探杆接入需要电脑与设备处于同一局域网，并由管理员为本程序配置"
                "最小范围的 TCP 8000 入站防火墙规则。此程序不会自动修改防火墙。")
        ttk.Label(frame, text=note, wraplength=455, justify="left",
                  font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(1, 0))
        self.log_path_label = ttk.Label(frame, text="启动日志：等待启动",
                                        font=("Microsoft YaHei UI", 8))
        self.log_path_label.pack(anchor="w", side="bottom", pady=(12, 0))

    def _set_window_icon(self):
        bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        icon_path = bundle_root / "assets" / "grain-silo-icon.ico"
        if icon_path.is_file():
            try:
                self.iconbitmap(default=str(icon_path))
            except tk.TclError:
                pass

    def _prepare_log(self):
        os.makedirs(server.LOG_ROOT, exist_ok=True)
        path = Path(server.LOG_ROOT) / "desktop-station.log"
        self.log_file = path.open("a", encoding="utf-8", buffering=1)
        sys.stdout = self.log_file
        sys.stderr = self.log_file
        self.log_path_label.configure(text="启动日志：%s" % path)

    def _start_station(self):
        try:
            self._prepare_log()
            url = self.runtime.start()
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            self.status_text.set("中心站启动失败")
            self.log_file and self.log_file.write("启动失败：%s\n" % exc)
            messagebox.showerror(
                APP_TITLE,
                "中心站未能启动。端口 8000 可能正被旧版本占用；为避免中断探杆上报，"
                "程序没有关闭或替换已有服务。\n\n详情：%s" % exc,
                parent=self,
            )
            return
        self.status_text.set("● 中心站运行中 · 探杆可向本机 8000 端口上报")
        self.url_text.set(url)
        self.open_button.configure(state="normal")
        webbrowser.open(url)

    def _open_dashboard(self):
        if self.runtime.running:
            webbrowser.open(self.runtime.local_url)
        else:
            messagebox.showwarning(APP_TITLE, "中心站尚未启动。", parent=self)

    def _copy_url(self):
        self.clipboard_clear()
        self.clipboard_append(self.url_text.get())
        self.update_idletasks()

    def _confirm_close(self):
        if self.runtime.running and not messagebox.askyesno(
                APP_TITLE,
                "退出后电脑中心站会停止，S3 暂时无法向电脑上传数据。确认退出吗？",
                parent=self):
            return
        self.runtime.stop()
        if self.log_file is not None:
            self.log_file.flush()
            self.log_file.close()
        self.destroy()


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="中心站监听端口（默认 8000）")
    parser.add_argument("--self-check", action="store_true",
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.self_check:
        return self_check()
    app = GrainSiloDesktopApp(port=args.port)
    app.mainloop()
    return 0


def self_check():
    """Verify a frozen package with an isolated temporary database."""
    old_db, old_data_root = server.DB, server.DATA_ROOT
    with tempfile.TemporaryDirectory(prefix="grainsilo-package-check-") as temp:
        server.DATA_ROOT = temp
        server.DB = os.path.join(temp, "station.db")
        runtime = StationRuntime(host="127.0.0.1", port=0,
                                 start_weather_worker=False)
        try:
            base = runtime.start()
            with urllib.request.urlopen(base + "api/v1/health", timeout=5) as response:
                health = json.loads(response.read().decode("utf-8"))
            if not health.get("ok") or int(health.get("schema_version", 0)) < 9:
                return 1
            with urllib.request.urlopen(base + "index.html", timeout=5) as response:
                page = response.read().decode("utf-8")
            return 0 if "粮仓" in page else 1
        except (OSError, ValueError, sqlite3.Error):
            return 1
        finally:
            runtime.stop()
            server.DB, server.DATA_ROOT = old_db, old_data_root


if __name__ == "__main__":
    raise SystemExit(main())
