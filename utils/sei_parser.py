"""
utils/sei_parser.py — H.264/H.265 SEI NAL 单元解析

从 MP4 mdat box 中提取 Dashcam 遥测数据（速度、档位、转向角、自动驾驶状态等）。
支持 Protobuf 解码和手动 Protobuf 解析两种回退方式。
"""

import struct


def _extract_sei_direct(video_path):
    """Scan MP4 mdat for SEI NALs — MP4 box tree traversal (dashcam-mp4.js findBox algorithm)"""
    with open(video_path, 'rb') as f:
        raw = f.read()
    # MP4 box tree traversal to find exact mdat position
    pos, flen = 0, len(raw)
    mdat_start, mdat_size = None, 0
    while pos < flen - 8:
        sz = struct.unpack('>I', raw[pos:pos+4])[0]
        name = raw[pos+4:pos+8]
        hdr = 8
        if sz == 1:
            if pos + 16 > flen: break
            sz = struct.unpack('>Q', raw[pos+8:pos+16])[0]; hdr = 16
        elif sz == 0:
            sz = flen - pos
        if sz < hdr: break
        if name == b'mdat':
            mdat_start, mdat_size = pos + hdr, sz - hdr
            break
        pos += sz
    if mdat_start is None: return None
    # Scan NAL units, track frame count for proper timestamps
    p = mdat_start
    end = mdat_start + mdat_size
    results = []
    frame_idx = 0  # video frame counter (type 1 or 5 NALs)
    while p + 4 <= end:
        nal_sz = struct.unpack('>I', raw[p:p+4])[0]
        p += 4
        if nal_sz < 2 or p + nal_sz > end: break
        hdr_byte = raw[p]
        avc_type = hdr_byte & 0x1F
        hevc_type = (hdr_byte >> 1) & 0x3F
        is_sei = (avc_type == 6) or (hevc_type in (39, 40))
        is_frame = (avc_type in (1, 5)) or (hevc_type in (19, 20, 21, 32, 33, 34))  # IDR/non-IDR
        if is_sei:
            proto = _parse_sei_nal(raw[p:p+nal_sz])
            if proto:
                proto['_frame_idx'] = frame_idx  # save video frame position for timing
                results.append(proto)
                if len(results) >= 2000: break
        if is_frame:
            frame_idx += 1
        p += nal_sz
    if not results: return None
    # Return _frame_idx for client-side proportional timing (aligned to <video>.duration)
    total = max(frame_idx, 1)
    for r in results:
        idx = r.pop('_frame_idx', 0)
        r['timestamp_ms'] = (idx / total) * 1000.0  # in "frame units" (0..1000 scale)
    return results


def _parse_sei_nal(nal):
    if len(nal) < 10:
        return None
    i = 2
    while i < len(nal) and i < 10 and (nal[i] & 0x80):
        i += 1
    i += 1
    while i < len(nal) - 1 and nal[i] == 0x42:
        i += 1
    marker = nal.find(0x69, max(i, 3))
    if marker < 0:
        return None
    i = marker + 1
    if i >= len(nal) - 1:
        return None
    proto = nal[i:-1]
    clean = bytearray()
    z = 0
    for b in proto:
        if z >= 2 and b == 0x03:
            z = 0; continue
        clean.append(b)
        z = 0 if b != 0 else z + 1
    try:
        import dashcam_pb2
        m = dashcam_pb2.SeiMetadata()
        m.ParseFromString(bytes(clean))
        return {'speed_kmh':round(m.vehicle_speed_mps*3.6,1),
            'gear_state':{0:'P',1:'D',2:'R',3:'N'}.get(m.gear_state,'?'),
            'steering_wheel_angle':round(m.steering_wheel_angle,2),
            'accelerator_pedal_position':round(m.accelerator_pedal_position,3),
            'brake_applied':m.brake_applied,'blinker_on_left':m.blinker_on_left,
            'blinker_on_right':m.blinker_on_right,
            'autopilot_state':{0:'NONE',1:'SELF_DRIVING',2:'AUTOSTEER',3:'TACC'}.get(m.autopilot_state,'NONE'),
            'frame_seq_no': m.frame_seq_no}
    except:
        pass
    return _manual_sei_parse(bytes(clean))


def _manual_sei_parse(payload):
    res = {'speed_kmh':0,'gear_state':'?','steering_wheel_angle':0,
        'accelerator_pedal_position':0,'brake_applied':False,
        'blinker_on_left':False,'blinker_on_right':False,'autopilot_state':'NONE',
        'frame_seq_no': 0}
    p = 0
    while p < len(payload):
        tb = payload[p]; p += 1
        fn, wt = tb >> 3, tb & 7
        if wt == 0:  # varint
            v = 0; s = 0
            while p < len(payload):
                b = payload[p]; p += 1; v |= (b & 0x7F) << s; s += 7
                if not (b & 0x80): break
            if fn == 2: res['gear_state'] = {0:'P',1:'D',2:'R',3:'N'}.get(v,'?')
            elif fn == 3: res['frame_seq_no'] = v
            elif fn == 7: res['blinker_on_left'] = bool(v)
            elif fn == 8: res['blinker_on_right'] = bool(v)
            elif fn == 9: res['brake_applied'] = bool(v)
            elif fn == 10: res['autopilot_state'] = {0:'NONE',1:'SELF_DRIVING',2:'AUTOSTEER',3:'TACC'}.get(v,'NONE')
        elif wt == 5:  # fixed32 (float)
            if p + 4 <= len(payload):
                v = struct.unpack('<f', payload[p:p+4])[0]; p += 4
                if fn == 4: res['speed_kmh'] = round(v*3.6,1)
                elif fn == 5: res['accelerator_pedal_position'] = round(v,3)
                elif fn == 6: res['steering_wheel_angle'] = round(v,2)
        elif wt == 2:  # length-delimited
            ln = 0; s = 0
            while p < len(payload):
                b = payload[p]; p += 1; ln |= (b & 0x7F) << s; s += 7
                if not (b & 0x80): break
            p += ln
        else: break
    return res
