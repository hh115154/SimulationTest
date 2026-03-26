#!/usr/bin/env python3
"""
SimulationTest — SOME/IP 可视化监控服务端
监听 UDP 30501（SOME/IP VehicleSignalService），解析帧，
通过 WebSocket 广播给前端面板；同时提供 AP/CP 进程控制接口。

架构：
  ┌──────────────────┐    UDP:30501     ┌──────────────────┐
  │ MyAutoSarCP (MCU)│ ───────────────▶ │                  │
  └──────────────────┘                  │  monitor_server  │
  ┌──────────────────┐    UDP:30501     │  (本文件)         │
  │ MyAutoSarAp (SOC)│ ◀─────────────── │  ws://8765        │
  └──────────────────┘                  │                  │
                                        └────────┬─────────┘
                                                 │ WebSocket
                                        ┌────────▼─────────┐
                                        │   前端面板         │
                                        │ http://8080       │
                                        └──────────────────┘
"""

import asyncio
import json
import socket
import struct
import subprocess
import sys
import os
import time
import signal
import psutil
import threading
from pathlib import Path
from datetime import datetime

# ─── 路径配置 ───────────────────────────────────────────────────────────────
BASE = Path(__file__).resolve().parent.parent
AP_BIN  = BASE.parent / "MyAutoSarAp"  / "build_someip" / "src" / "application" / "MyAutoSarAp"
CP_BIN  = BASE.parent / "MyAutoSarCP"  / "build_someip" / "MyAutoSarCP"
LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ─── SOME/IP 协议常量（对齐 someip_proto.h）────────────────────────────────
SOMEIP_HDR_SIZE      = 16
VEHICLE_SIGNAL_PORT  = 30501
SVC_ID_VEHICLE_SIGNAL = 0x1001
EVT_ID_VEHICLE_SIGNAL = 0x8001

# VehicleSignalPayload_t 布局: f f B f B f B B = 20 bytes
# vehicle_speed_kmh(4f) engine_rpm(4f) brake_pedal(1B) steering_angle_deg(4f)
# door_status(1B) fuel_level_pct(4f) e2e_crc(1B) e2e_counter(1B)
PAYLOAD_FMT = ">ffBfBfBB"  # big-endian
PAYLOAD_SIZE = struct.calcsize(PAYLOAD_FMT)   # should be 20

# ─── 全局状态 ────────────────────────────────────────────────────────────────
processes: dict = {"ap": None, "cp": None}   # subprocess.Popen
proc_logs: dict = {"ap": [], "cp": []}        # 最近 200 行日志
ws_clients: set = set()

stats = {
    "total_frames": 0,
    "e2e_errors": 0,
    "session_gaps": 0,
    "start_time": time.time(),
    "last_frame_time": None,
    "last_session_id": -1,
}

# 最近 300 个数据点（用于时序图）
MAX_HISTORY = 300
history: list = []

# 当前订阅开关（前端可控）
subscriptions = {
    "vehicle_speed": True,
    "engine_rpm":    True,
    "brake_pedal":   True,
    "steering_angle":True,
    "door_status":   True,
    "fuel_level":    True,
}


# ═══════════════════════════════════════════════════════════════════════════════
# SOME/IP 解析
# ═══════════════════════════════════════════════════════════════════════════════

def parse_someip_header(data: bytes) -> dict | None:
    """解析 16 字节 SOME/IP 头，返回字段字典，失败返回 None"""
    if len(data) < SOMEIP_HDR_SIZE:
        return None
    svc_id, method_id, length, client_id, session_id, proto_ver, iface_ver, msg_type, rc = \
        struct.unpack_from(">HHIHHBBBB", data, 0)
    return {
        "service_id": svc_id,
        "method_id":  method_id,
        "length":     length,
        "client_id":  client_id,
        "session_id": session_id,
        "proto_ver":  proto_ver,
        "iface_ver":  iface_ver,
        "msg_type":   msg_type,
        "return_code":rc,
    }


def crc8(data: bytes) -> int:
    """E2E Profile 2 CRC8，多项式 0x1D"""
    crc = 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc ^ 0xFF


def parse_vehicle_signal(payload: bytes) -> dict | None:
    """解析 VehicleSignalPayload_t（20B），做 E2E 校验"""
    if len(payload) < PAYLOAD_SIZE:
        return None
    speed, rpm, brake, steer, door, fuel, crc, counter = struct.unpack_from(PAYLOAD_FMT, payload, 0)
    # E2E: CRC 覆盖 payload[0..17]（不含 crc 和 counter 本身计算方式依实现，
    # 这里与 C 端一致：对 payload[:-2] 计算）
    expected_crc = crc8(payload[:18])
    e2e_ok = (crc == expected_crc)
    return {
        "vehicle_speed_kmh":  round(speed, 2),
        "engine_rpm":         round(rpm, 1),
        "brake_pedal":        int(brake),
        "steering_angle_deg": round(steer, 2),
        "door_status":        int(door),
        "fuel_level_pct":     round(fuel, 2),
        "e2e_crc":            crc,
        "e2e_counter":        counter,
        "e2e_ok":             e2e_ok,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# UDP Sniffer（独立线程）
# ═══════════════════════════════════════════════════════════════════════════════

def udp_sniffer_thread(loop: asyncio.AbstractEventLoop):
    """在独立线程中监听 UDP 30501，解析后 push 到事件循环"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except AttributeError:
        pass
    sock.bind(("127.0.0.1", VEHICLE_SIGNAL_PORT))
    sock.settimeout(0.5)
    print(f"[Sniffer] Listening on UDP 127.0.0.1:{VEHICLE_SIGNAL_PORT}", flush=True)

    while True:
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except Exception as e:
            print(f"[Sniffer] recvfrom error: {e}", flush=True)
            continue

        hdr = parse_someip_header(data)
        if hdr is None:
            continue
        if hdr["service_id"] != SVC_ID_VEHICLE_SIGNAL:
            continue

        payload_data = data[SOMEIP_HDR_SIZE:]
        sig = parse_vehicle_signal(payload_data)
        if sig is None:
            continue

        # 统计
        stats["total_frames"] += 1
        now = time.time()
        stats["last_frame_time"] = now
        if not sig["e2e_ok"]:
            stats["e2e_errors"] += 1
        sid = hdr["session_id"]
        if stats["last_session_id"] >= 0 and sid != (stats["last_session_id"] + 1) & 0xFFFF:
            stats["session_gaps"] += 1
        stats["last_session_id"] = sid

        # 构造事件
        event = {
            "type":      "frame",
            "ts":        now,
            "ts_str":    datetime.fromtimestamp(now).strftime("%H:%M:%S.%f")[:-3],
            "header":    hdr,
            "signal":    {k: v for k, v in sig.items() if subscriptions.get(k.replace("_kmh","").replace("_deg",""), True)},
            "stats": {
                "total_frames":   stats["total_frames"],
                "e2e_errors":     stats["e2e_errors"],
                "session_gaps":   stats["session_gaps"],
                "elapsed_sec":    round(now - stats["start_time"], 1),
                "fps":            round(stats["total_frames"] / max(1, now - stats["start_time"]), 1),
            }
        }

        # 追加历史
        history.append(event)
        if len(history) > MAX_HISTORY:
            history.pop(0)

        # 线程安全地推给 asyncio loop
        asyncio.run_coroutine_threadsafe(broadcast(json.dumps(event)), loop)


# ═══════════════════════════════════════════════════════════════════════════════
# 进程管理
# ═══════════════════════════════════════════════════════════════════════════════

def _is_running(key: str) -> bool:
    p = processes.get(key)
    return p is not None and p.poll() is None


def _get_cpu_mem(key: str) -> dict:
    p = processes.get(key)
    if p is None or p.poll() is not None:
        return {"cpu": 0.0, "mem_mb": 0.0, "pid": None}
    try:
        proc = psutil.Process(p.pid)
        cpu = proc.cpu_percent(interval=0.05)
        mem = proc.memory_info().rss / 1024 / 1024
        return {"cpu": round(cpu, 1), "mem_mb": round(mem, 1), "pid": p.pid}
    except Exception:
        return {"cpu": 0.0, "mem_mb": 0.0, "pid": p.pid}


def start_process(key: str) -> str:
    if _is_running(key):
        return f"{key} already running"
    bin_map = {"ap": AP_BIN, "cp": CP_BIN}
    bin_path = bin_map.get(key)
    if not bin_path or not Path(bin_path).exists():
        return f"Binary not found: {bin_path}"
    log_file = open(LOG_DIR / f"{key}.log", "w")
    proc = subprocess.Popen(
        [str(bin_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    processes[key] = proc
    proc_logs[key].clear()
    # 后台读日志线程
    loop = asyncio.get_event_loop()
    threading.Thread(target=_log_reader, args=(key, proc, log_file, loop), daemon=True).start()
    return f"{key} started (pid={proc.pid})"


def stop_process(key: str) -> str:
    p = processes.get(key)
    if p is None or p.poll() is not None:
        return f"{key} not running"
    p.terminate()
    try:
        p.wait(timeout=3)
    except subprocess.TimeoutExpired:
        p.kill()
    return f"{key} stopped"


def _log_reader(key: str, proc: subprocess.Popen, log_file, loop):
    for line in proc.stdout:
        line = line.rstrip()
        proc_logs[key].append(line)
        if len(proc_logs[key]) > 200:
            proc_logs[key].pop(0)
        log_file.write(line + "\n")
        log_file.flush()
        msg = json.dumps({"type": "log", "process": key, "line": line,
                          "ts_str": datetime.now().strftime("%H:%M:%S.%f")[:-3]})
        asyncio.run_coroutine_threadsafe(broadcast(msg), loop)
    log_file.close()


# ═══════════════════════════════════════════════════════════════════════════════
# WebSocket 服务端
# ═══════════════════════════════════════════════════════════════════════════════

async def broadcast(message: str):
    dead = set()
    for ws in list(ws_clients):
        try:
            await ws.send(message)
        except Exception:
            dead.add(ws)
    ws_clients.difference_update(dead)


async def handle_client(websocket):
    ws_clients.add(websocket)
    remote = websocket.remote_address
    print(f"[WS] Client connected: {remote}", flush=True)

    # 发送欢迎包：当前状态 + 历史数据
    welcome = {
        "type":         "welcome",
        "stats":        stats.copy(),
        "subscriptions": subscriptions.copy(),
        "processes": {
            "ap": {"running": _is_running("ap"), "logs": proc_logs["ap"][-50:], **_get_cpu_mem("ap")},
            "cp": {"running": _is_running("cp"), "logs": proc_logs["cp"][-50:], **_get_cpu_mem("cp")},
        },
        "history": history[-100:],
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
        print(f"[WS] Client {remote} error: {e}", flush=True)
    finally:
        ws_clients.discard(websocket)
        print(f"[WS] Client disconnected: {remote}", flush=True)


async def handle_command(ws, cmd: dict):
    action = cmd.get("action")
    resp = {"type": "ack", "action": action}

    if action == "start":
        key = cmd.get("process")
        msg = start_process(key)
        resp["message"] = msg
        resp["running"] = _is_running(key)

    elif action == "stop":
        key = cmd.get("process")
        msg = stop_process(key)
        resp["message"] = msg
        resp["running"] = _is_running(key)

    elif action == "restart":
        key = cmd.get("process")
        stop_process(key)
        await asyncio.sleep(0.3)
        msg = start_process(key)
        resp["message"] = f"restarted: {msg}"
        resp["running"] = _is_running(key)

    elif action == "start_demo":
        # 先停再启
        stop_process("cp"); stop_process("ap")
        await asyncio.sleep(0.5)
        # 重置统计
        stats.update({"total_frames":0,"e2e_errors":0,"session_gaps":0,
                       "start_time":time.time(),"last_frame_time":None,"last_session_id":-1})
        history.clear()
        r_ap = start_process("ap")
        await asyncio.sleep(0.4)
        r_cp = start_process("cp")
        resp["message"] = f"Demo started: {r_ap} | {r_cp}"

    elif action == "stop_demo":
        stop_process("cp"); stop_process("ap")
        resp["message"] = "Demo stopped"

    elif action == "set_subscription":
        key = cmd.get("key")
        val = cmd.get("value", True)
        if key in subscriptions:
            subscriptions[key] = bool(val)
        resp["subscriptions"] = subscriptions.copy()

    elif action == "get_status":
        resp["processes"] = {
            "ap": {"running": _is_running("ap"), **_get_cpu_mem("ap")},
            "cp": {"running": _is_running("cp"), **_get_cpu_mem("cp")},
        }
        resp["stats"] = stats.copy()

    elif action == "clear_stats":
        stats.update({"total_frames":0,"e2e_errors":0,"session_gaps":0,
                       "start_time":time.time(),"last_frame_time":None,"last_session_id":-1})
        history.clear()
        resp["message"] = "Stats cleared"

    await ws.send(json.dumps(resp))

    # 广播进程状态变更
    if action in ("start","stop","restart","start_demo","stop_demo"):
        await broadcast(json.dumps({
            "type": "process_status",
            "processes": {
                "ap": {"running": _is_running("ap"), **_get_cpu_mem("ap")},
                "cp": {"running": _is_running("cp"), **_get_cpu_mem("cp")},
            }
        }))


# ═══════════════════════════════════════════════════════════════════════════════
# 定时心跳（每秒推送进程状态）
# ═══════════════════════════════════════════════════════════════════════════════

async def heartbeat():
    while True:
        await asyncio.sleep(1.0)
        if not ws_clients:
            continue
        msg = json.dumps({
            "type": "heartbeat",
            "ts":   time.time(),
            "processes": {
                "ap": {"running": _is_running("ap"), **_get_cpu_mem("ap")},
                "cp": {"running": _is_running("cp"), **_get_cpu_mem("cp")},
            },
            "stats": {
                "total_frames":  stats["total_frames"],
                "e2e_errors":    stats["e2e_errors"],
                "session_gaps":  stats["session_gaps"],
                "elapsed_sec":   round(time.time() - stats["start_time"], 1),
                "fps":           round(stats["total_frames"] / max(1, time.time() - stats["start_time"]), 1),
            }
        })
        await broadcast(msg)


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP 文件服务（提供 frontend/index.html）
# ═══════════════════════════════════════════════════════════════════════════════

async def http_handler(reader, writer):
    """极简 HTTP/1.0 服务器，只服务 frontend/ 目录"""
    try:
        req = await reader.read(4096)
        line = req.decode(errors="replace").split("\r\n")[0]
        method, path, *_ = line.split()
        if path == "/" or path == "":
            path = "/index.html"
        file_path = BASE / "frontend" / path.lstrip("/")
        if file_path.exists() and file_path.is_file():
            content = file_path.read_bytes()
            ctype = "text/html" if path.endswith(".html") else \
                    "application/javascript" if path.endswith(".js") else \
                    "text/css" if path.endswith(".css") else "application/octet-stream"
            resp = (f"HTTP/1.0 200 OK\r\nContent-Type: {ctype}; charset=utf-8\r\n"
                    f"Content-Length: {len(content)}\r\nConnection: close\r\n\r\n").encode() + content
        else:
            body = b"404 Not Found"
            resp = b"HTTP/1.0 404 Not Found\r\nContent-Length: 13\r\n\r\n" + body
        writer.write(resp)
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════════════════

async def main():
    import websockets

    WS_PORT   = 8765
    HTTP_PORT = 8080

    loop = asyncio.get_running_loop()

    # 启动 UDP sniffer 线程
    t = threading.Thread(target=udp_sniffer_thread, args=(loop,), daemon=True)
    t.start()

    # 启动 HTTP 服务
    http_server = await asyncio.start_server(http_handler, "127.0.0.1", HTTP_PORT)
    print(f"[HTTP] Frontend served at http://127.0.0.1:{HTTP_PORT}", flush=True)

    # 启动 WebSocket 服务
    ws_server = await websockets.serve(handle_client, "127.0.0.1", WS_PORT)
    print(f"[WS]   WebSocket server at ws://127.0.0.1:{WS_PORT}", flush=True)
    print(f"[INFO] AP binary: {AP_BIN}", flush=True)
    print(f"[INFO] CP binary: {CP_BIN}", flush=True)
    print("=" * 60, flush=True)
    print("  SimulationTest Monitor Server Ready", flush=True)
    print(f"  Open: http://127.0.0.1:{HTTP_PORT}", flush=True)
    print("=" * 60, flush=True)

    # 心跳任务
    asyncio.create_task(heartbeat())

    # 保持运行
    async with http_server, ws_server:
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[INFO] Shutting down...", flush=True)
        stop_process("ap")
        stop_process("cp")
