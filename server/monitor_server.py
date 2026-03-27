#!/usr/bin/env python3
"""
SimulationTest — SOME/IP 可视化监控服务端  v2.0
════════════════════════════════════════════════════════════════════════
架构：
  ┌──────────────────────┐  UDP 40501      ┌─────────────────────────┐
  │ MyAutoSarCP (MCU/CP) │ ──────────────▶ │                         │
  └──────────────────────┘                 │   monitor_server.py     │
                                           │   WebSocket ws://8765   │
  ┌──────────────────────┐  UDP 30501      │   HTTP     http://8080  │
  │ MyAutoSarAp (SOC/AP) │ ◀─────── (CP发) │                         │
  └──────────────────────┘                 └────────────┬────────────┘
                                                        │ WebSocket
                                           ┌────────────▼────────────┐
                                           │  frontend/index.html    │
                                           │  HMI 监控面板            │
                                           └─────────────────────────┘

说明：
  - AP 进程绑定 UDP 30501，接收 CP 发出的 SOME/IP Notification
  - 监控服务同样绑定 UDP 30501（SO_REUSEPORT），"旁路"监听相同帧
  - WebSocket 端口 8765，兼容 websockets 15.x API
  - HTTP 端口 8080，服务 frontend/index.html
════════════════════════════════════════════════════════════════════════
"""

import asyncio
import json
import socket
import struct
import subprocess
import sys
import os
import time
import threading
from pathlib import Path
from datetime import datetime

# ─── 路径配置 ────────────────────────────────────────────────────────────────
BASE    = Path(__file__).resolve().parent.parent          # SimulationTest/
WB_ROOT = BASE.parent                                      # WorkBuddy/
AP_BIN  = WB_ROOT / "MyAutoSarAp" / "build_someip" / "src" / "application" / "MyAutoSarAp"
CP_BIN  = WB_ROOT / "MyAutoSarCP" / "build_someip" / "MyAutoSarCP"
LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ─── SOME/IP 协议常量（对齐 someip_proto.h）─────────────────────────────────
SOMEIP_HDR_SIZE       = 16
VEHICLE_SIGNAL_PORT   = 30502          # 监控专用镜像端口（CP 同时发到此）
                                       # AP 绑 30501，互不干扰
SVC_ID_VEHICLE_SIGNAL = 0x1001
EVT_ID_VEHICLE_SIGNAL = 0x8001
# VehicleSignalPayload_t: f f B f B f B B = 20 bytes
# float 字段由 C 端直接写入（宿主机 Little-Endian），整型头部为 Big-Endian
# payload 内部 float 用小端解析
PAYLOAD_FMT  = "<ffBfBfBB"   # Little-Endian（macOS arm64 / x86_64 宿主机字节序）
PAYLOAD_SIZE = struct.calcsize(PAYLOAD_FMT)   # 20

# HMI 命令转发端口（monitor_server → CP udp:30503）
HMI_CMD_PORT = 30503

# ─── 服务端口 ─────────────────────────────────────────────────────────────────
WS_PORT   = 8765
HTTP_PORT = 8080

# ─── 全局状态（asyncio loop 线程操作需加锁或用 call_soon_threadsafe）──────────
processes: dict = {"ap": None, "cp": None}   # subprocess.Popen | None
proc_logs: dict = {"ap": [], "cp": []}        # 最近 200 行日志

ws_clients: set = set()                        # 已连接 WebSocket 客户端

# HMI 命令转发 socket（复用整个生命周期）
_hmi_cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

stats = {
    "total_frames":    0,
    "e2e_errors":      0,
    "session_gaps":    0,
    "start_time":      time.time(),
    "last_frame_time": None,
    "last_session_id": -1,
}

MAX_HISTORY = 300
history: list = []

subscriptions = {
    "vehicle_speed":  True,
    "engine_rpm":     True,
    "brake_pedal":    True,
    "steering_angle": True,
    "door_status":    True,
    "fuel_level":     True,
}

# FPS 滑动窗口
_fps_frames = 0
_fps_ts     = time.time()


# ════════════════════════════════════════════════════════════════════════════════
# SOME/IP 解析
# ════════════════════════════════════════════════════════════════════════════════

def _crc8(data: bytes) -> int:
    """E2E Profile 2 CRC8，多项式 0x1D（与 C 端一致）"""
    crc = 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc ^ 0xFF


def parse_frame(data: bytes):
    """解析 UDP 数据报 → (header_dict, signal_dict)，失败返回 (None, None)"""
    if len(data) < SOMEIP_HDR_SIZE + PAYLOAD_SIZE:
        return None, None

    svc_id, method_id, length, client_id, session_id, proto_ver, iface_ver, msg_type, rc = \
        struct.unpack_from(">HHIHHBBBB", data, 0)

    if svc_id != SVC_ID_VEHICLE_SIGNAL:
        return None, None

    hdr = {
        "service_id": svc_id,
        "method_id":  method_id,
        "length":     length,
        "client_id":  client_id,
        "session_id": session_id,
        "proto_ver":  proto_ver,
        "iface_ver":  iface_ver,
        "msg_type":   msg_type,
        "return_code": rc,
    }

    payload = data[SOMEIP_HDR_SIZE:]
    speed, rpm, brake, steer, door, fuel, crc, counter = \
        struct.unpack_from(PAYLOAD_FMT, payload, 0)

    expected_crc = _crc8(payload[:18])
    e2e_ok = (crc == expected_crc)

    sig = {
        "vehicle_speed_kmh":  round(speed, 2),
        "engine_rpm":         round(rpm,   1),
        "brake_pedal":        int(brake),
        "steering_angle_deg": round(steer, 2),
        "door_status":        int(door),
        "fuel_level_pct":     round(fuel,  2),
        "e2e_crc":            crc,
        "e2e_counter":        counter,
        "e2e_ok":             e2e_ok,
    }
    return hdr, sig


# ════════════════════════════════════════════════════════════════════════════════
# UDP Sniffer 线程
# ════════════════════════════════════════════════════════════════════════════════

def udp_sniffer_thread(loop: asyncio.AbstractEventLoop):
    """旁路监听 UDP 30501（SO_REUSEPORT），解析后投递给 asyncio loop"""
    global _fps_frames, _fps_ts

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except AttributeError:
        pass
    sock.bind(("127.0.0.1", VEHICLE_SIGNAL_PORT))
    sock.settimeout(0.5)
    print(f"[Sniffer] UDP sniffer on 127.0.0.1:{VEHICLE_SIGNAL_PORT} (monitor mirror port)", flush=True)

    while True:
        try:
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except Exception as e:
            print(f"[Sniffer] recvfrom error: {e}", flush=True)
            continue

        hdr, sig = parse_frame(data)
        if hdr is None:
            continue

        now = time.time()

        # ── 统计 ──────────────────────────────────────────────────────────
        stats["total_frames"]    += 1
        stats["last_frame_time"]  = now
        if not sig["e2e_ok"]:
            stats["e2e_errors"] += 1
        sid = hdr["session_id"]
        if stats["last_session_id"] >= 0:
            expected = (stats["last_session_id"] + 1) & 0xFFFF
            if sid != expected:
                stats["session_gaps"] += 1
        stats["last_session_id"] = sid

        # FPS（滑动 1 秒窗口）
        _fps_frames += 1
        elapsed = now - _fps_ts
        if elapsed >= 1.0:
            fps = round(_fps_frames / elapsed, 1)
            _fps_frames = 0
            _fps_ts     = now
        else:
            fps = round(stats["total_frames"] / max(1.0, now - stats["start_time"]), 1)

        # ── 过滤订阅字段 ─────────────────────────────────────────────────
        # 只保留已订阅的信号，但 E2E 字段始终保留
        filtered_sig = {}
        _sub_key_map = {
            "vehicle_speed_kmh":  "vehicle_speed",
            "engine_rpm":         "engine_rpm",
            "brake_pedal":        "brake_pedal",
            "steering_angle_deg": "steering_angle",
            "door_status":        "door_status",
            "fuel_level_pct":     "fuel_level",
        }
        for k, v in sig.items():
            sub_key = _sub_key_map.get(k)
            if sub_key is None or subscriptions.get(sub_key, True):
                filtered_sig[k] = v

        event = {
            "type":    "frame",
            "source":  "cp_mirror",   # 来自 CP 30502 旁路镜像
            "ts":      now,
            "ts_str":  datetime.fromtimestamp(now).strftime("%H:%M:%S.%f")[:-3],
            "header":  hdr,
            "signal":  filtered_sig,
            "stats": {
                "total_frames":  stats["total_frames"],
                "e2e_errors":    stats["e2e_errors"],
                "session_gaps":  stats["session_gaps"],
                "fps":           fps,
            },
        }

        history.append(event)
        if len(history) > MAX_HISTORY:
            history.pop(0)

        # 线程安全投递
        asyncio.run_coroutine_threadsafe(broadcast(json.dumps(event)), loop)


# ════════════════════════════════════════════════════════════════════════════════
# 进程管理
# ════════════════════════════════════════════════════════════════════════════════

def _is_running(key: str) -> bool:
    p = processes.get(key)
    return p is not None and p.poll() is None


def _get_proc_info(key: str) -> dict:
    p = processes.get(key)
    if p is None or p.poll() is not None:
        return {"running": False, "pid": None, "cpu": 0.0, "mem_mb": 0.0}
    try:
        import psutil
        pp = psutil.Process(p.pid)
        cpu = pp.cpu_percent(interval=0.05)
        mem = pp.memory_info().rss / 1024 / 1024
    except Exception:
        cpu, mem = 0.0, 0.0
    return {"running": True, "pid": p.pid, "cpu": round(cpu, 1), "mem_mb": round(mem, 1)}


def start_process(key: str, loop=None) -> str:
    if _is_running(key):
        return f"{key.upper()} already running"
    bin_map = {"ap": AP_BIN, "cp": CP_BIN}
    bin_path = bin_map.get(key)
    if not bin_path or not Path(bin_path).exists():
        return f"Binary not found: {bin_path}"

    log_file = open(LOG_DIR / f"{key}.log", "w", buffering=1)
    proc = subprocess.Popen(
        [str(bin_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    processes[key] = proc
    proc_logs[key].clear()
    if loop is None:
        loop = asyncio.get_event_loop()
    threading.Thread(
        target=_log_reader, args=(key, proc, log_file, loop), daemon=True
    ).start()
    return f"{key.upper()} started (PID={proc.pid})"


def stop_process(key: str) -> str:
    p = processes.get(key)
    if p is None or p.poll() is not None:
        return f"{key.upper()} not running"
    p.terminate()
    try:
        p.wait(timeout=3)
    except subprocess.TimeoutExpired:
        p.kill()
    return f"{key.upper()} stopped"


def _log_reader(key: str, proc: subprocess.Popen, log_file, loop):
    global _fps_frames, _fps_ts
    for line in proc.stdout:
        line = line.rstrip()
        proc_logs[key].append(line)
        if len(proc_logs[key]) > 200:
            proc_logs[key].pop(0)
        log_file.write(line + "\n")
        log_file.flush()

        # ── AP 结构化信号采集点 ──────────────────────────────────────────────
        # AP 进程每帧输出 "[AP_SIGNAL_JSON] {...}" 日志行
        # 此处解析并广播为 frame 事件，HMI 显示的信号数据来自 AP 侧采集
        if key == "ap" and line.startswith("[AP_SIGNAL_JSON]"):
            try:
                json_str = line[len("[AP_SIGNAL_JSON]"):].strip()
                ap_data  = json.loads(json_str)

                now = time.time()
                stats["total_frames"] += 1
                stats["last_frame_time"] = now

                if not ap_data.get("e2e_ok", 1):
                    stats["e2e_errors"] += 1

                sid = ap_data.get("session", -1)
                if stats["last_session_id"] >= 0:
                    expected = (stats["last_session_id"] + 1) & 0xFFFF
                    if sid != expected:
                        stats["session_gaps"] += 1
                stats["last_session_id"] = sid

                # FPS 滑动窗口
                _fps_frames += 1
                elapsed = now - _fps_ts
                if elapsed >= 1.0:
                    fps = round(_fps_frames / elapsed, 1)
                    _fps_frames = 0
                    _fps_ts     = now
                else:
                    fps = round(stats["total_frames"] / max(1.0, now - stats["start_time"]), 1)

                sig = {
                    "vehicle_speed_kmh":  round(ap_data.get("speed",  0.0), 2),
                    "engine_rpm":         round(ap_data.get("rpm",    0.0), 1),
                    "brake_pedal":        int(ap_data.get("brake",    0)),
                    "steering_angle_deg": round(ap_data.get("steer",  0.0), 2),
                    "door_status":        int(ap_data.get("door",     0)),
                    "fuel_level_pct":     round(ap_data.get("fuel",   0.0), 2),
                    "e2e_crc":            int(ap_data.get("e2e_crc",  0)),
                    "e2e_counter":        int(ap_data.get("e2e_cnt",  0)),
                    "e2e_ok":             bool(ap_data.get("e2e_ok",  1)),
                }
                # 过滤订阅字段
                _sub_key_map = {
                    "vehicle_speed_kmh":  "vehicle_speed",
                    "engine_rpm":         "engine_rpm",
                    "brake_pedal":        "brake_pedal",
                    "steering_angle_deg": "steering_angle",
                    "door_status":        "door_status",
                    "fuel_level_pct":     "fuel_level",
                }
                filtered_sig = {}
                for k, v in sig.items():
                    sub_key = _sub_key_map.get(k)
                    if sub_key is None or subscriptions.get(sub_key, True):
                        filtered_sig[k] = v

                # 合成 SOME/IP 协议信息（来自 AP 侧）
                hdr_fake = {
                    "service_id": 0x1001,
                    "method_id":  0x8001,
                    "length":     28,
                    "client_id":  0,
                    "session_id": sid,
                    "proto_ver":  1,
                    "iface_ver":  1,
                    "msg_type":   2,   # NOTIFICATION
                    "return_code": 0,
                }

                event = {
                    "type":    "frame",
                    "source":  "ap",        # 采集点标识：来自 AP 侧
                    "ts":      now,
                    "ts_str":  datetime.fromtimestamp(now).strftime("%H:%M:%S.%f")[:-3],
                    "header":  hdr_fake,
                    "signal":  filtered_sig,
                    "stats": {
                        "total_frames":  stats["total_frames"],
                        "e2e_errors":    stats["e2e_errors"],
                        "session_gaps":  stats["session_gaps"],
                        "fps":           fps,
                    },
                }

                history.append(event)
                if len(history) > MAX_HISTORY:
                    history.pop(0)

                asyncio.run_coroutine_threadsafe(broadcast(json.dumps(event)), loop)
                continue  # 不把此行当普通日志广播
            except Exception:
                pass  # 解析失败时当普通日志处理

        # ── 普通日志广播 ────────────────────────────────────────────────────
        msg = json.dumps({
            "type":    "log",
            "process": key,
            "line":    line,
            "ts_str":  datetime.now().strftime("%H:%M:%S.%f")[:-3],
        })
        asyncio.run_coroutine_threadsafe(broadcast(msg), loop)
    log_file.close()


# ════════════════════════════════════════════════════════════════════════════════
# WebSocket 广播 / 处理（websockets 15.x API）
# ════════════════════════════════════════════════════════════════════════════════

async def broadcast(message: str):
    dead = set()
    for ws in list(ws_clients):
        try:
            await ws.send(message)
        except Exception:
            dead.add(ws)
    ws_clients.difference_update(dead)


async def ws_handler(websocket):
    """websockets 15.x handler 签名：只有 websocket 参数"""
    ws_clients.add(websocket)
    remote = websocket.remote_address
    print(f"[WS]   Client connected: {remote}", flush=True)

    # ── Welcome 包：当前状态 + 历史 ────────────────────────────────────────
    welcome = {
        "type":          "welcome",
        "subscriptions": subscriptions.copy(),
        "stats": {
            "total_frames":  stats["total_frames"],
            "e2e_errors":    stats["e2e_errors"],
            "session_gaps":  stats["session_gaps"],
            "fps":           0,
        },
        "processes": {
            "ap": {**_get_proc_info("ap"), "logs": proc_logs["ap"][-50:]},
            "cp": {**_get_proc_info("cp"), "logs": proc_logs["cp"][-50:]},
        },
        "history": history[-60:],
    }
    await websocket.send(json.dumps(welcome))

    try:
        async for raw in websocket:
            try:
                cmd = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await handle_command(websocket, cmd)
    except Exception as e:
        print(f"[WS]   Client {remote} error: {type(e).__name__}: {e}", flush=True)
    finally:
        ws_clients.discard(websocket)
        print(f"[WS]   Client disconnected: {remote}", flush=True)


async def handle_command(ws, cmd: dict):
    action = cmd.get("action")
    loop   = asyncio.get_running_loop()
    resp   = {"type": "ack", "action": action, "ok": True}

    if action == "start":
        key = cmd.get("process")
        resp["message"] = start_process(key, loop)

    elif action == "stop":
        key = cmd.get("process")
        resp["message"] = stop_process(key)

    elif action == "restart":
        key = cmd.get("process")
        stop_process(key)
        await asyncio.sleep(0.3)
        resp["message"] = start_process(key, loop)

    elif action == "start_demo":
        # 先停，再重置统计，先启 CP（Provider）再启 AP（Consumer）
        stop_process("cp")
        stop_process("ap")
        await asyncio.sleep(0.5)
        global _fps_frames, _fps_ts
        stats.update({
            "total_frames": 0, "e2e_errors": 0, "session_gaps": 0,
            "start_time": time.time(), "last_frame_time": None, "last_session_id": -1,
        })
        _fps_frames = 0
        _fps_ts     = time.time()
        history.clear()
        r_cp = start_process("cp", loop)
        await asyncio.sleep(0.4)     # 让 CP 先绑端口
        r_ap = start_process("ap", loop)
        resp["message"] = f"{r_cp} | {r_ap}"

    elif action == "stop_demo":
        stop_process("ap")
        stop_process("cp")
        resp["message"] = "Demo stopped"

    elif action == "set_subscription":
        key = cmd.get("key")
        val = bool(cmd.get("value", True))
        if key in subscriptions:
            subscriptions[key] = val
        resp["subscriptions"] = subscriptions.copy()

    elif action == "get_status":
        resp["processes"] = {
            "ap": _get_proc_info("ap"),
            "cp": _get_proc_info("cp"),
        }
        resp["stats"] = {
            "total_frames":  stats["total_frames"],
            "e2e_errors":    stats["e2e_errors"],
            "session_gaps":  stats["session_gaps"],
            "fps":           0,
        }

    elif action == "clear_stats":
        stats.update({
            "total_frames": 0, "e2e_errors": 0, "session_gaps": 0,
            "start_time": time.time(), "last_frame_time": None, "last_session_id": -1,
        })
        _fps_frames = 0
        _fps_ts     = time.time()
        history.clear()
        resp["message"] = "Stats cleared"

    elif action == "hmi_input":
        # HMI 下行指令：将信号设定值转发给 CP（UDP:30503）
        # cmd 格式：{"action":"hmi_input","speed_kmh":60,"rpm":2000,
        #             "steering_deg":15,"brake":0,"door":0,"fuel_pct":75}
        payload_dict = {k: v for k, v in cmd.items() if k != "action"}
        payload_str  = json.dumps(payload_dict).encode()
        try:
            _hmi_cmd_sock.sendto(payload_str, ("127.0.0.1", HMI_CMD_PORT))
            resp["message"] = f"HMI input sent to CP: {payload_dict}"
            resp["forwarded"] = True
        except Exception as e:
            resp["ok"]      = False
            resp["message"] = f"Failed to forward HMI input: {e}"
            resp["forwarded"] = False

    await ws.send(json.dumps(resp))
    resp["processes"] = {
        "ap": _get_proc_info("ap"),
        "cp": _get_proc_info("cp"),
    }

    # 进程操作后广播状态变更
    if action in ("start", "stop", "restart", "start_demo", "stop_demo"):
        await broadcast(json.dumps({
            "type":      "process_status",
            "processes": {
                "ap": _get_proc_info("ap"),
                "cp": _get_proc_info("cp"),
            },
        }))


# ════════════════════════════════════════════════════════════════════════════════
# 心跳（每 1 秒推送进程状态 + 统计）
# ════════════════════════════════════════════════════════════════════════════════

async def heartbeat():
    while True:
        await asyncio.sleep(1.0)
        if not ws_clients:
            continue
        elapsed = max(1.0, time.time() - stats["start_time"])
        msg = json.dumps({
            "type": "heartbeat",
            "ts":   time.time(),
            "processes": {
                "ap": _get_proc_info("ap"),
                "cp": _get_proc_info("cp"),
            },
            "stats": {
                "total_frames": stats["total_frames"],
                "e2e_errors":   stats["e2e_errors"],
                "session_gaps": stats["session_gaps"],
                "fps":          round(stats["total_frames"] / elapsed, 1),
            },
        })
        await broadcast(msg)


# ════════════════════════════════════════════════════════════════════════════════
# 极简 HTTP 服务（服务 frontend/ 目录）
# ════════════════════════════════════════════════════════════════════════════════

async def http_handler(reader, writer):
    try:
        raw = await asyncio.wait_for(reader.read(4096), timeout=3)
        first_line = raw.decode(errors="replace").split("\r\n")[0]
        parts = first_line.split()
        path = parts[1] if len(parts) >= 2 else "/"
        if path in ("/", ""):
            path = "/index.html"
        file_path = (BASE / "frontend" / path.lstrip("/")).resolve()
        # 安全检查：不允许路径逃逸
        if not str(file_path).startswith(str(BASE / "frontend")):
            raise PermissionError("path traversal")
        if file_path.exists() and file_path.is_file():
            content = file_path.read_bytes()
            ext = file_path.suffix
            ctype = {
                ".html": "text/html",
                ".js":   "application/javascript",
                ".css":  "text/css",
                ".json": "application/json",
                ".png":  "image/png",
                ".svg":  "image/svg+xml",
            }.get(ext, "application/octet-stream")
            header = (
                f"HTTP/1.0 200 OK\r\n"
                f"Content-Type: {ctype}; charset=utf-8\r\n"
                f"Content-Length: {len(content)}\r\n"
                f"Cache-Control: no-cache\r\n"
                f"Connection: close\r\n\r\n"
            ).encode()
            writer.write(header + content)
        else:
            writer.write(b"HTTP/1.0 404 Not Found\r\nContent-Length: 9\r\n\r\nNot Found")
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


# ════════════════════════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════════════════════════

async def main():
    import websockets

    loop = asyncio.get_running_loop()

    # ── UDP sniffer（独立线程）────────────────────────────────────────────────
    threading.Thread(
        target=udp_sniffer_thread, args=(loop,), daemon=True
    ).start()

    # ── HTTP 服务 ─────────────────────────────────────────────────────────────
    http_server = await asyncio.start_server(
        http_handler, "127.0.0.1", HTTP_PORT
    )

    # ── WebSocket 服务（websockets 15.x）──────────────────────────────────────
    ws_server = await websockets.serve(ws_handler, "127.0.0.1", WS_PORT)

    # ── 启动信息 ──────────────────────────────────────────────────────────────
    sep = "=" * 60
    print(sep, flush=True)
    print("  SimulationTest Monitor Server  v2.0", flush=True)
    print(sep, flush=True)
    print(f"  HMI 面板:   http://127.0.0.1:{HTTP_PORT}", flush=True)
    print(f"  WebSocket:  ws://127.0.0.1:{WS_PORT}", flush=True)
    print(f"  UDP 监听:   127.0.0.1:{VEHICLE_SIGNAL_PORT} (监控镜像端口，CP 额外复制帧)", flush=True)
    print(f"  AP 二进制:  {'✓ ' + str(AP_BIN) if AP_BIN.exists() else '✗  NOT FOUND: ' + str(AP_BIN)}", flush=True)
    print(f"  CP 二进制:  {'✓ ' + str(CP_BIN) if CP_BIN.exists() else '✗  NOT FOUND: ' + str(CP_BIN)}", flush=True)
    print(sep, flush=True)
    print("  在浏览器打开 http://127.0.0.1:8080 即可查看 HMI", flush=True)
    print("  Ctrl+C 退出", flush=True)
    print(sep, flush=True)

    # ── 心跳任务 ──────────────────────────────────────────────────────────────
    asyncio.create_task(heartbeat())

    # ── 保持运行 ──────────────────────────────────────────────────────────────
    async with http_server, ws_server:
        await asyncio.Future()   # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[INFO] Shutting down...", flush=True)
        stop_process("ap")
        stop_process("cp")
        sys.exit(0)
