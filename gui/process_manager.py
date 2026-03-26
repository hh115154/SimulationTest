"""
process_manager.py — AP/CP 进程生命周期管理
后端工程师：修改 AP_BIN / CP_BIN 路径，或重写 ProcessManager 接入 systemd/Docker
"""

from __future__ import annotations
import os
import subprocess
import threading
import time
import psutil
from pathlib import Path
from typing import Callable, Optional

# ── 二进制路径（相对于 WorkBuddy 目录）──────────────────────────────────────
_WORKBUDDY = Path(__file__).resolve().parent.parent.parent

AP_BIN = _WORKBUDDY / "MyAutoSarAp" / "build_someip" / "src" / "application" / "MyAutoSarAp"
CP_BIN = _WORKBUDDY / "MyAutoSarCP" / "build_someip" / "MyAutoSarCP"
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


class ProcessManager:
    """
    管理 AP / CP 两个子进程：启动、停止、重启、日志流、资源监控

    ── 公开回调 ────────────────────────────────────────────────────────
    pm.on_log(key, line, ts)         — 新日志行（key = 'ap' | 'cp'）
    pm.on_status(key, running, pid)  — 进程状态变化
    """

    def __init__(self):
        self._procs:  dict[str, Optional[subprocess.Popen]] = {"ap": None, "cp": None}
        self._lock    = threading.Lock()

        # 回调
        self.on_log:    Optional[Callable[[str, str, str], None]]       = None
        self.on_status: Optional[Callable[[str, bool, Optional[int]], None]] = None

    # ── 公开接口 ─────────────────────────────────────────────────────────

    def start(self, key: str) -> str:
        if self.is_running(key):
            return f"{key.upper()} already running"
        bin_map = {"ap": AP_BIN, "cp": CP_BIN}
        bin_path = bin_map.get(key)
        if not bin_path or not Path(bin_path).exists():
            return f"Binary not found:\n{bin_path}"

        log_path = LOG_DIR / f"{key}.log"
        log_file = open(log_path, "w", buffering=1)

        proc = subprocess.Popen(
            [str(bin_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        with self._lock:
            self._procs[key] = proc

        # 启动日志读取线程
        threading.Thread(
            target=self._log_reader,
            args=(key, proc, log_file),
            daemon=True,
        ).start()

        self._fire_status(key, True, proc.pid)
        return f"{key.upper()} started (PID={proc.pid})"

    def stop(self, key: str) -> str:
        with self._lock:
            proc = self._procs.get(key)
        if proc is None or proc.poll() is not None:
            return f"{key.upper()} not running"
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        self._fire_status(key, False, None)
        return f"{key.upper()} stopped"

    def restart(self, key: str) -> str:
        self.stop(key)
        time.sleep(0.3)
        return self.start(key)

    def is_running(self, key: str) -> bool:
        with self._lock:
            p = self._procs.get(key)
        return p is not None and p.poll() is None

    def get_resource(self, key: str) -> dict:
        """返回 {'pid': int|None, 'cpu': float, 'mem_mb': float}"""
        with self._lock:
            p = self._procs.get(key)
        if p is None or p.poll() is not None:
            return {"pid": None, "cpu": 0.0, "mem_mb": 0.0}
        try:
            pp = psutil.Process(p.pid)
            cpu = pp.cpu_percent(interval=0.05)
            mem = pp.memory_info().rss / 1024 / 1024
            return {"pid": p.pid, "cpu": round(cpu, 1), "mem_mb": round(mem, 1)}
        except Exception:
            return {"pid": p.pid, "cpu": 0.0, "mem_mb": 0.0}

    def stop_all(self):
        for key in ("ap", "cp"):
            self.stop(key)

    @property
    def ap_bin_exists(self) -> bool:
        return Path(AP_BIN).exists()

    @property
    def cp_bin_exists(self) -> bool:
        return Path(CP_BIN).exists()

    # ── 内部 ─────────────────────────────────────────────────────────────

    def _log_reader(self, key: str, proc: subprocess.Popen, log_file):
        try:
            for line in proc.stdout:
                line = line.rstrip()
                log_file.write(line + "\n")
                ts = time.strftime("%H:%M:%S")
                if self.on_log:
                    self.on_log(key, line, ts)
        finally:
            log_file.close()
            # 进程退出后通知状态变化
            self._fire_status(key, False, None)

    def _fire_status(self, key: str, running: bool, pid: Optional[int]):
        if self.on_status:
            self.on_status(key, running, pid)
