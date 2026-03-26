"""
data_interface.py — 数据接口层
════════════════════════════════════════════════════════════════════════
★ 后端工程师对接入口 ★

本文件是 GUI 与实际数据源之间的唯一边界。
替换/扩展 DataSource 类即可接入真实数据流：
  - SOME/IP UDP socket（当前默认实现）
  - CAN bus (python-can)
  - SPI (spidev)
  - 共享内存
  - gRPC / REST
  - 硬件 HIL 仿真器

GUI 层只调用：
    ds = DataSource()
    ds.start()
    ds.on_frame = lambda frame: ...   # 注册回调
    ds.subscribe("vehicle_speed", True/False)
    ds.stop()

不需要了解 SOME/IP 帧格式细节。
════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


# ═══════════════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class VehicleSignal:
    """VehicleSignalService 一帧数据（对应 someip_proto.h VehicleSignalPayload_t）"""
    # 信号值
    vehicle_speed_kmh:  float = 0.0   # 车速 km/h
    engine_rpm:         float = 0.0   # 转速 RPM
    brake_pedal:        int   = 0     # 制动踏板 0/1
    steering_angle_deg: float = 0.0   # 方向盘转角 deg
    door_status:        int   = 0     # 车门状态 bitmask
    fuel_level_pct:     float = 0.0   # 燃油 %

    # E2E
    e2e_crc:     int  = 0
    e2e_counter: int  = 0
    e2e_ok:      bool = True

    # SOME/IP 帧头
    service_id:  int = 0
    method_id:   int = 0
    session_id:  int = 0
    length:      int = 0
    msg_type:    int = 0

    # 时间戳
    recv_time: float = field(default_factory=time.time)


@dataclass
class CommStats:
    """通信统计"""
    total_frames:  int   = 0
    e2e_errors:    int   = 0
    session_gaps:  int   = 0
    fps:           float = 0.0
    start_time:    float = field(default_factory=time.time)
    last_recv:     float = 0.0

    def reset(self):
        self.total_frames = 0
        self.e2e_errors   = 0
        self.session_gaps = 0
        self.fps          = 0.0
        self.start_time   = time.time()
        self.last_recv    = 0.0


# ═══════════════════════════════════════════════════════════════════════
# SOME/IP 解析（内部实现，后端工程师一般不需要修改）
# ═══════════════════════════════════════════════════════════════════════

_SOMEIP_HDR_SIZE      = 16
_VEHICLE_SIGNAL_PORT  = 30502   # 监控镜像端口（CP 额外复制帧，不影响 AP 的 30501）
_SVC_ID_VEHICLE       = 0x1001
_PAYLOAD_FMT          = "<ffBfBfBB"   # Little-Endian（宿主机字节序，float 未做网络序转换）
_PAYLOAD_SIZE         = struct.calcsize(_PAYLOAD_FMT)  # 20


def _crc8(data: bytes) -> int:
    """E2E Profile 2 CRC8，多项式 0x1D"""
    crc = 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc ^ 0xFF


def _parse_frame(data: bytes) -> Optional[VehicleSignal]:
    """解析一个 UDP 数据报 → VehicleSignal，失败返回 None"""
    if len(data) < _SOMEIP_HDR_SIZE + _PAYLOAD_SIZE:
        return None

    # 解析 SOME/IP 头（大端）
    svc_id, method_id, length, client_id, session_id, proto_ver, iface_ver, msg_type, rc = \
        struct.unpack_from(">HHIHHBBBB", data, 0)

    if svc_id != _SVC_ID_VEHICLE:
        return None

    payload = data[_SOMEIP_HDR_SIZE:]
    if len(payload) < _PAYLOAD_SIZE:
        return None

    speed, rpm, brake, steer, door, fuel, crc, counter = \
        struct.unpack_from(_PAYLOAD_FMT, payload, 0)

    # E2E 校验
    expected_crc = _crc8(payload[:18])
    e2e_ok = (crc == expected_crc)

    return VehicleSignal(
        vehicle_speed_kmh   = round(speed, 2),
        engine_rpm          = round(rpm,   1),
        brake_pedal         = int(brake),
        steering_angle_deg  = round(steer, 2),
        door_status         = int(door),
        fuel_level_pct      = round(fuel,  2),
        e2e_crc             = crc,
        e2e_counter         = counter,
        e2e_ok              = e2e_ok,
        service_id          = svc_id,
        method_id           = method_id,
        session_id          = session_id,
        length              = length,
        msg_type            = msg_type,
        recv_time           = time.time(),
    )


# ═══════════════════════════════════════════════════════════════════════
# DataSource — 后端工程师的主要对接点
# ═══════════════════════════════════════════════════════════════════════

class DataSource:
    """
    数据源基类 / 默认实现（SOME/IP UDP Sniffer）

    ── 使用方式 ───────────────────────────────────────────────────────
    ds = DataSource()
    ds.on_frame = lambda sig: print(sig.vehicle_speed_kmh)
    ds.start()
    ...
    ds.stop()

    ── 对接真实数据源 ──────────────────────────────────────────────────
    继承 DataSource，重写 _rx_loop() 方法；
    在循环中调用 self._deliver(signal) 推送数据给 GUI。

    或者直接替换整个类，只要保留相同的公开接口即可。
    """

    def __init__(self, bind_addr: str = "127.0.0.1", bind_port: int = _VEHICLE_SIGNAL_PORT):
        self._addr    = bind_addr
        self._port    = bind_port
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._sock:   Optional[socket.socket]    = None

        self._last_session: int = -1
        self.stats = CommStats()

        # ── 订阅开关（GUI 层通过 subscribe() 控制）──
        self.subscriptions: dict[str, bool] = {
            "vehicle_speed":  True,
            "engine_rpm":     True,
            "brake_pedal":    True,
            "steering_angle": True,
            "door_status":    True,
            "fuel_level":     True,
        }

        # ── 回调函数（GUI 层注册）──────────────────────────────────
        # on_frame(signal: VehicleSignal) — 每收到一帧触发
        self.on_frame:  Optional[Callable[[VehicleSignal], None]] = None
        # on_error(msg: str) — 解析或 socket 错误
        self.on_error:  Optional[Callable[[str], None]]           = None

    # ── 公开接口 ────────────────────────────────────────────────────────

    def start(self) -> bool:
        """开始监听，返回 True 表示成功"""
        if self._running:
            return True
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except AttributeError:
                pass
            self._sock.bind((self._addr, self._port))
            self._sock.settimeout(0.5)
        except Exception as e:
            if self.on_error:
                self.on_error(f"Socket bind failed: {e}")
            return False

        self._running = True
        self.stats.reset()
        self._thread = threading.Thread(target=self._rx_loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        """停止监听"""
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if self._thread:
            self._thread.join(timeout=1.5)

    def subscribe(self, signal_key: str, enabled: bool):
        """切换某路信号的订阅状态（GUI 调用）"""
        if signal_key in self.subscriptions:
            self.subscriptions[signal_key] = enabled

    def reset_stats(self):
        """清空统计数据"""
        self.stats.reset()
        self._last_session = -1

    # ── 内部实现 ────────────────────────────────────────────────────────

    def _rx_loop(self):
        """接收线程主循环
        ★ 后端工程师：替换此方法以接入不同数据源 ★
        """
        fps_frames = 0
        fps_ts     = time.time()

        while self._running:
            try:
                data, _addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except Exception as e:
                if self._running and self.on_error:
                    self.on_error(f"recvfrom: {e}")
                break

            sig = _parse_frame(data)
            if sig is None:
                continue

            # 统计
            self.stats.total_frames += 1
            self.stats.last_recv     = sig.recv_time
            if not sig.e2e_ok:
                self.stats.e2e_errors += 1
            if self._last_session >= 0:
                expected = (self._last_session + 1) & 0xFFFF
                if sig.session_id != expected:
                    self.stats.session_gaps += 1
            self._last_session = sig.session_id

            # FPS
            fps_frames += 1
            now = time.time()
            elapsed = now - fps_ts
            if elapsed >= 1.0:
                self.stats.fps = round(fps_frames / elapsed, 1)
                fps_frames     = 0
                fps_ts         = now

            self._deliver(sig)

    def _deliver(self, sig: VehicleSignal):
        """向 GUI 推送数据帧（线程安全调用 on_frame）"""
        if self.on_frame:
            self.on_frame(sig)
