"""dashcam_pb2 — 纯 Python 实现的 SeiMetadata 解码器（零依赖，API 兼容 protoc 生成版）。

数据源：Tesla 官方 dashcam.proto（teslamotors/dashcam，2025-12-06）。
字段编号与官方协议完全一致，见同目录 dashcam.proto。

用法（与 protoc 生成的 pb2 相同）：
    import dashcam_pb2
    m = dashcam_pb2.SeiMetadata()
    m.ParseFromString(payload)      # 解析失败抛 ValueError
    m.vehicle_speed_mps / m.gear_state / ...
"""
import struct


class DecodeError(ValueError):
    """protobuf 解码失败（兼容 google.protobuf.message.DecodeError 语义）"""


class SeiMetadata:
    """Tesla Dashcam SEI 遥测消息（proto3，16 字段）"""

    # 字段默认值（proto3 语义）
    version = 0
    gear_state = 0          # 0=PARK 1=DRIVE 2=REVERSE 3=NEUTRAL
    frame_seq_no = 0
    vehicle_speed_mps = 0.0
    accelerator_pedal_position = 0.0
    steering_wheel_angle = 0.0
    blinker_on_left = False
    blinker_on_right = False
    brake_applied = False
    autopilot_state = 0     # 0=NONE 1=SELF_DRIVING 2=AUTOSTEER 3=TACC
    latitude_deg = 0.0
    longitude_deg = 0.0
    heading_deg = 0.0
    linear_acceleration_mps2_x = 0.0
    linear_acceleration_mps2_y = 0.0
    linear_acceleration_mps2_z = 0.0

    DESCRIPTOR = None  # 保持与 stub 兼容（部分代码检查 DESCRIPTOR.fields）

    def ParseFromString(self, data):
        """解析 protobuf wire 格式。返回解析字节数；解析失败抛 DecodeError。

        proto3 字段类型（来自官方 dashcam.proto）：
          1  version                    uint32  varint
          2  gear_state                 enum    varint
          3  frame_seq_no               uint64  varint
          4  vehicle_speed_mps          float   fixed32
          5  accelerator_pedal_position float   fixed32
          6  steering_wheel_angle       float   fixed32
          7  blinker_on_left            bool    varint
          8  blinker_on_right           bool    varint
          9  brake_applied              bool    varint
          10 autopilot_state            enum    varint
          11-13 latitude/longitude/heading   double  fixed64
          14-16 linear_acceleration_x/y/z    double  fixed64
        """
        pos = 0
        n = len(data)
        while pos < n:
            tag, pos = self._read_varint(data, pos)
            field_no, wt = tag >> 3, tag & 7
            if field_no == 1 and wt == 0:
                v, pos = self._read_varint(data, pos)
                self.version = v
            elif field_no == 2 and wt == 0:
                v, pos = self._read_varint(data, pos)
                self.gear_state = v
            elif field_no == 3 and wt == 0:
                v, pos = self._read_varint(data, pos)
                self.frame_seq_no = v
            elif field_no == 4 and wt == 5:
                self.vehicle_speed_mps, pos = self._read_fixed32(data, pos)
            elif field_no == 5 and wt == 5:
                self.accelerator_pedal_position, pos = self._read_fixed32(data, pos)
            elif field_no == 6 and wt == 5:
                self.steering_wheel_angle, pos = self._read_fixed32(data, pos)
            elif field_no == 7 and wt == 0:
                v, pos = self._read_varint(data, pos)
                self.blinker_on_left = bool(v)
            elif field_no == 8 and wt == 0:
                v, pos = self._read_varint(data, pos)
                self.blinker_on_right = bool(v)
            elif field_no == 9 and wt == 0:
                v, pos = self._read_varint(data, pos)
                self.brake_applied = bool(v)
            elif field_no == 10 and wt == 0:
                v, pos = self._read_varint(data, pos)
                self.autopilot_state = v
            elif field_no in (11, 12, 13) and wt == 1:
                v, pos = self._read_fixed64(data, pos)
                if field_no == 11:
                    self.latitude_deg = v
                elif field_no == 12:
                    self.longitude_deg = v
                else:
                    self.heading_deg = v
            elif field_no in (14, 15, 16) and wt == 1:
                v, pos = self._read_fixed64(data, pos)
                if field_no == 14:
                    self.linear_acceleration_mps2_x = v
                elif field_no == 15:
                    self.linear_acceleration_mps2_y = v
                else:
                    self.linear_acceleration_mps2_z = v
            else:
                # 跳过未知/不匹配 wire type 的字段
                pos = self._skip_field(data, pos, wt)
        return pos

    # ── wire 原语 ──
    @staticmethod
    def _read_varint(data, pos):
        v = 0
        s = 0
        while pos < len(data):
            b = data[pos]
            pos += 1
            v |= (b & 0x7F) << s
            if not (b & 0x80):
                return v, pos
            s += 7
            if s > 63:
                raise DecodeError("varint too long")
        raise DecodeError("truncated varint")

    @staticmethod
    def _read_fixed32(data, pos):
        if pos + 4 > len(data):
            raise DecodeError("truncated fixed32")
        return struct.unpack('<f', data[pos:pos + 4])[0], pos + 4

    @staticmethod
    def _read_fixed64(data, pos):
        if pos + 8 > len(data):
            raise DecodeError("truncated fixed64")
        return struct.unpack('<d', data[pos:pos + 8])[0], pos + 8

    @staticmethod
    def _skip_field(data, pos, wt):
        if wt == 0:
            _, pos = SeiMetadata._read_varint(data, pos)
        elif wt == 1:
            pos += 8
        elif wt == 2:
            ln, pos = SeiMetadata._read_varint(data, pos)
            pos += ln
        elif wt == 5:
            pos += 4
        else:
            raise DecodeError(f"unsupported wire type {wt}")
        if pos > len(data):
            raise DecodeError("field overruns buffer")
        return pos
