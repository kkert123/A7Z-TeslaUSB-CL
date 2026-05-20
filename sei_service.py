#!/usr/bin/env python3
"""
Tesla Dashcam SEI (Supplemental Enhancement Information) Extractor.

Transplanted from teslamotors/dashcam sei_extractor.py.
Extracts frame-synchronized telemetry (speed, gear, steering, GPS, G-force, AP state)
from Tesla dashcam MP4 video files via protobuf-encoded SEI NAL units.

Author: TeslaUSB-Neo project
Version: 1.0.0
"""

import os
import json
import struct
import logging
from pathlib import Path
from typing import Optional, Dict, List, Generator, Tuple

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('sei_service')

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TELEMETRY_CACHE_DIR = '/opt/radxa_data/teslausb/data/telemetry'
SEI_SAMPLE_RATE = 5          # Extract 1 frame per N (reduce JSON from MB to KB)
TESLA_FPS = 30               # Tesla dashcam fixed frame rate

# Camera name mapping for file searching
CAMERA_SUFFIXES = {
    'front': ['-front.mp4', '_front.mp4'],
    'back': ['-back.mp4', '_back.mp4'],
    'left': ['-left_repeater.mp4', '_left_repeater.mp4', '-left.mp4'],
    'right': ['-right_repeater.mp4', '_right_repeater.mp4', '-right.mp4'],
}

# Gear state mapping (protobuf enum → display string)
GEAR_STATE_MAP = {0: 'P', 1: 'D', 2: 'R', 3: 'N'}
# Autopilot state mapping
AP_STATE_MAP = {0: 'NONE', 1: 'SELF_DRIVING', 2: 'AUTOSTEER', 3: 'TACC'}


# ---------------------------------------------------------------------------
# Video file locator
# ---------------------------------------------------------------------------

def find_video_path(folder: str, event_id: str, camera: str) -> Optional[str]:
    """Find a specific camera video file for an event.

    Args:
        folder: 'SentryClips' | 'SavedClips' | 'RecentClips'
        event_id: Event directory name or file prefix
        camera: 'front' | 'back' | 'left' | 'right'

    Returns:
        Absolute path to the MP4 file, or None if not found.
    """
    base = '/mnt/teslacam/TeslaCam'
    event_dir = os.path.join(base, folder, event_id)
    event_parent = os.path.join(base, folder)

    # Strategy 1: Event directory (SentryClips, SavedClips)
    if os.path.isdir(event_dir):
        suffixes = CAMERA_SUFFIXES.get(camera, [f'-{camera}.mp4'])
        for sfx in suffixes:
            for fname in sorted(os.listdir(event_dir)):
                if fname.endswith(sfx) or fname.lower().endswith(sfx.lower()):
                    return os.path.join(event_dir, fname)
        return None

    # Strategy 2: Flat files (RecentClips)
    if os.path.isdir(event_parent):
        suffixes = CAMERA_SUFFIXES.get(camera, [f'-{camera}.mp4'])
        for sfx in suffixes:
            for fname in sorted(os.listdir(event_parent)):
                if fname.startswith(event_id) and (
                    fname.endswith(sfx) or fname.lower().endswith(sfx.lower())
                ):
                    return os.path.join(event_parent, fname)
        return None

    return None


def find_event_cameras(folder: str, event_id: str) -> List[str]:
    """List available cameras for an event."""
    available = []
    for cam in ['front', 'back', 'left', 'right']:
        if find_video_path(folder, event_id, cam):
            available.append(cam)
    return available


# ---------------------------------------------------------------------------
# SEI NAL unit extraction (ported from teslamotors/dashcam)
# ---------------------------------------------------------------------------

def find_mdat(fp) -> Tuple[int, int]:
    """Return (offset, size) for the first mdat atom in an MP4 file."""
    fp.seek(0)
    while True:
        header = fp.read(8)
        if len(header) < 8:
            raise RuntimeError("mdat atom not found")
        size32, atom_type = struct.unpack(">I4s", header)
        if size32 == 1:
            large = fp.read(8)
            if len(large) != 8:
                raise RuntimeError("truncated extended atom size")
            atom_size = struct.unpack(">Q", large)[0]
            header_size = 16
        else:
            atom_size = size32 if size32 else 0
            header_size = 8
        if atom_type == b"mdat":
            payload_size = atom_size - header_size if atom_size else 0
            return fp.tell(), payload_size
        if atom_size < header_size:
            raise RuntimeError("invalid MP4 atom size")
        fp.seek(atom_size - header_size, 1)


def iter_nals(fp, offset: int, size: int) -> Generator[bytes, None, None]:
    """Yield SEI user data unregistered NAL units from the MP4 mdat atom.
    
    Supports both H.264/AVC (NAL type 6) and H.265/HEVC (NAL type 39/40).
    """
    fp.seek(offset)
    consumed = 0
    nal_count = 0
    sei_nals = 0
    while size == 0 or consumed < size:
        header = fp.read(4)
        if len(header) < 4:
            break
        nal_size = struct.unpack(">I", header)[0]
        if nal_size < 2 or nal_size > 500000:
            fp.seek(nal_size, 1)
            consumed += 4 + nal_size
            continue

        first_two = fp.read(2)
        if len(first_two) != 2:
            break

        # Detect HEVC: try parsing as HEVC NAL header (nal_unit_type in upper 6 bits of byte 0)
        hevc_type = (first_two[0] >> 1) & 0x3F
        avc_type = first_two[0] & 0x1F
        nal_count += 1
        
        is_sei = False
        # HEVC: SEI prefix=39, SEI suffix=40; AVC: SEI=6
        if hevc_type in (39, 40) or avc_type == 6:
            is_sei = True
        
        if not is_sei:
            fp.seek(nal_size - 2, 1)
            consumed += 4 + nal_size
            continue

        sei_nals += 1
        rest = fp.read(nal_size - 2)
        if len(rest) != nal_size - 2:
            break
        payload = first_two + rest
        consumed += 4 + nal_size
        
        # Check if this SEI contains protobuf payload
        proto = extract_proto_payload(payload)
        if proto:
            yield payload

    logger.debug(f"iter_nals: {nal_count} NALs, {sei_nals} SEI, returning protobuf payloads")


def strip_emulation_prevention_bytes(data: bytes) -> bytes:
    """Remove H.264 emulation prevention bytes (0x03 following 0x00 0x00)."""
    stripped = bytearray()
    zero_count = 0
    for byte in data:
        if zero_count >= 2 and byte == 0x03:
            zero_count = 0
            continue
        stripped.append(byte)
        zero_count = 0 if byte != 0 else zero_count + 1
    return bytes(stripped)


def extract_proto_payload(nal: bytes) -> Optional[bytes]:
    """Extract protobuf payload from SEI NAL unit (H.264 and H.265).
    
    For H.265/HEVC: SEI message with payloadType=5 (user_data_unregistered)
    contains Tesla UUID followed by protobuf data.
    Tesla UUID: f69665f7-1162-4a27-a4bb-2e0a143c5885
    """
    if not isinstance(nal, bytes) or len(nal) < 20:
        return None

    # Try HEVC SEI message parsing first (NAL header is 2 bytes in HEVC)
    uuid_bytes = bytes.fromhex('f69665f711624a27a4bb2e0a143c5885')
    
    # Strip emulation prevention first
    clean = strip_emulation_prevention_bytes(nal)
    
    # Search for Tesla UUID in the SEI payload
    uuid_pos = clean.find(uuid_bytes)
    if uuid_pos >= 0:
        # Protobuf data starts after UUID (16 bytes)
        proto_start = uuid_pos + 16
        if proto_start < len(clean):
            return clean[proto_start:]
    
    # Fallback: old H.264 magic byte search
    for i in range(2, len(nal) - 1):
        byte = nal[i]
        if byte == 0x42:
            continue
        if byte == 0x69:
            if i > 2:
                return strip_emulation_prevention_bytes(nal[i + 1:-1])
            break
        break
    
    return None


# ---------------------------------------------------------------------------
# Telemetry extraction
# ---------------------------------------------------------------------------

def _decode_sei_metadata(payload: bytes) -> Optional[dict]:
    """Decode protobuf SEI payload into a Python dict.

    Uses actual protobuf parsing if dashcam_pb2 is properly compiled,
    falls back to manual parsing if only the stub is available.
    """
    try:
        import dashcam_pb2
        meta = dashcam_pb2.SeiMetadata()
        meta.ParseFromString(payload)
        return _proto_to_dict(meta)
    except NotImplementedError:
        return _manual_parse(payload)
    except Exception as e:
        logger.debug(f"protobuf decode failed: {e}")
        return None


def _proto_to_dict(meta) -> dict:
    """Convert protobuf SeiMetadata to frontend-friendly dict."""
    return {
        'frame_seq_no': meta.frame_seq_no,
        'speed_kmh': round(meta.vehicle_speed_mps * 3.6, 1),
        'gear_state': GEAR_STATE_MAP.get(meta.gear_state, '?'),
        'steering_wheel_angle': round(meta.steering_wheel_angle, 2),
        'accelerator_pedal_position': round(meta.accelerator_pedal_position, 3),
        'brake_applied': meta.brake_applied,
        'blinker_on_left': meta.blinker_on_left,
        'blinker_on_right': meta.blinker_on_right,
        'autopilot_state': AP_STATE_MAP.get(meta.autopilot_state, 'NONE'),
        'latitude_deg': round(meta.latitude_deg, 6),
        'longitude_deg': round(meta.longitude_deg, 6),
        'heading_deg': round(meta.heading_deg, 1),
        'linear_acceleration_mps2_x': round(meta.linear_acceleration_mps2_x, 3),
        'linear_acceleration_mps2_y': round(meta.linear_acceleration_mps2_y, 3),
        'linear_acceleration_mps2_z': round(meta.linear_acceleration_mps2_z, 3),
    }


def _manual_parse(payload: bytes) -> Optional[dict]:
    """Manual protobuf parsing fallback (when protoc-compiled pb2 unavailable).

    This is a simplified parser for the SeiMetadata proto3 message.
    It handles varint, fixed32, fixed64, and length-delimited wire types.
    Only used as a last-resort fallback.
    """
    result = {
        'frame_seq_no': 0, 'speed_kmh': 0.0, 'gear_state': '?',
        'steering_wheel_angle': 0.0, 'accelerator_pedal_position': 0.0,
        'brake_applied': False, 'blinker_on_left': False, 'blinker_on_right': False,
        'autopilot_state': 'NONE',
        'latitude_deg': 0.0, 'longitude_deg': 0.0, 'heading_deg': 0.0,
        'linear_acceleration_mps2_x': 0.0,
        'linear_acceleration_mps2_y': 0.0,
        'linear_acceleration_mps2_z': 0.0,
    }

    pos = 0
    field_map = {
        1: ('version', 'varint'),
        2: ('gear_state', 'varint'),
        3: ('frame_seq_no', 'varint'),
        4: ('vehicle_speed_mps', 'fixed32'),
        5: ('accelerator_pedal_position', 'fixed32'),
        6: ('steering_wheel_angle', 'fixed32'),
        7: ('blinker_on_left', 'varint'),
        8: ('blinker_on_right', 'varint'),
        9: ('brake_applied', 'varint'),
        10: ('autopilot_state', 'varint'),
        11: ('latitude_deg', 'fixed64'),
        12: ('longitude_deg', 'fixed64'),
        13: ('heading_deg', 'fixed64'),
        14: ('linear_acceleration_mps2_x', 'fixed64'),
        15: ('linear_acceleration_mps2_y', 'fixed64'),
        16: ('linear_acceleration_mps2_z', 'fixed64'),
    }

    try:
        while pos < len(payload):
            if pos >= len(payload):
                break
            tag = payload[pos]
            pos += 1
            field_num = tag >> 3
            wire_type = tag & 0x07

            if field_num not in field_map:
                # Skip unknown field
                if wire_type == 0:  # varint
                    while pos < len(payload) and (payload[pos] & 0x80):
                        pos += 1
                    pos += 1
                elif wire_type == 1:  # fixed64
                    pos += 8
                elif wire_type == 2:  # length-delimited
                    if pos >= len(payload):
                        break
                    length = _read_varint(payload, pos)
                    pos = length[0]
                    pos += length[1]
                elif wire_type == 5:  # fixed32
                    pos += 4
                continue

            name, ptype = field_map[field_num]

            if wire_type == 0:  # varint
                val, pos = _read_varint(payload, pos)
                if ptype == 'varint':
                    if name == 'gear_state':
                        result['gear_state'] = GEAR_STATE_MAP.get(val, '?')
                    elif name == 'autopilot_state':
                        result['autopilot_state'] = AP_STATE_MAP.get(val, 'NONE')
                    elif name == 'blinker_on_left':
                        result['blinker_on_left'] = bool(val)
                    elif name == 'blinker_on_right':
                        result['blinker_on_right'] = bool(val)
                    elif name == 'brake_applied':
                        result['brake_applied'] = bool(val)
                    elif name == 'frame_seq_no':
                        result['frame_seq_no'] = val
            elif wire_type == 1:  # fixed64
                if pos + 8 <= len(payload):
                    val = struct.unpack('<d', payload[pos:pos + 8])[0]
                    pos += 8
                    if name == 'latitude_deg':
                        result['latitude_deg'] = round(val, 6)
                    elif name == 'longitude_deg':
                        result['longitude_deg'] = round(val, 6)
                    elif name == 'heading_deg':
                        result['heading_deg'] = round(val, 1)
                    elif name.startswith('linear_acceleration'):
                        result[name] = round(val, 3)
            elif wire_type == 5:  # fixed32
                if pos + 4 <= len(payload):
                    val = struct.unpack('<f', payload[pos:pos + 4])[0]
                    pos += 4
                    if name == 'vehicle_speed_mps':
                        result['speed_kmh'] = round(val * 3.6, 1)
                    elif name == 'steering_wheel_angle':
                        result['steering_wheel_angle'] = round(val, 2)
                    elif name == 'accelerator_pedal_position':
                        result['accelerator_pedal_position'] = round(val, 3)

        return result
    except Exception as e:
        logger.debug(f"manual protobuf parse failed: {e}")
        return None


def _read_varint(data: bytes, pos: int) -> Tuple[int, int]:
    """Read a protobuf varint starting at pos. Returns (value, new_pos)."""
    result = 0
    shift = 0
    while pos < len(data):
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            break
        shift += 7
    return result, pos


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_telemetry(video_path: str) -> Optional[List[dict]]:
    """Extract SEI telemetry from a Tesla dashcam MP4 file.

    Args:
        video_path: Absolute path to the MP4 file.

    Returns:
        List of telemetry frame dicts (sampled at SEI_SAMPLE_RATE),
        or None if no SEI data found or extraction failed.
    """
    if not os.path.isfile(video_path):
        logger.warning(f"Video not found: {video_path}")
        return None

    try:
        with open(video_path, 'rb') as fp:
            offset, size = find_mdat(fp)
            frames = []
            nal_count = 0
            sei_count = 0

            for nal in iter_nals(fp, offset, size):
                nal_count += 1
                payload = extract_proto_payload(nal)
                if not payload:
                    continue

                sei_count += 1

                # Downsample: only keep every Nth frame
                if sei_count % SEI_SAMPLE_RATE != 0:
                    continue

                frame_data = _decode_sei_metadata(payload)
                if frame_data:
                    frames.append(frame_data)

                # Safety: max 10000 frames to prevent memory issues
                if len(frames) >= 10000:
                    logger.warning(f"Truncating at 10000 frames for {video_path}")
                    break

            if sei_count == 0:
                logger.info(f"No SEI data in {os.path.basename(video_path)}")
                return None

            logger.info(
                f"SEI extracted: {sei_count} total, {len(frames)} sampled "
                f"({nal_count} NALs) from {os.path.basename(video_path)}"
            )
            return frames if frames else None

    except RuntimeError as e:
        logger.warning(f"MP4 structure error in {video_path}: {e}")
        return None
    except Exception as e:
        logger.error(f"SEI extraction failed for {video_path}: {e}")
        return None


def get_telemetry(folder: str, event_id: str, camera: str) -> Optional[List[dict]]:
    """Get telemetry for a camera, using JSON cache.

    First checks cache directory. If cache miss, extracts from video
    and saves to cache. Subsequent calls are near-instant.

    Args:
        folder: 'SentryClips' | 'SavedClips' | 'RecentClips'
        event_id: Event identifier
        camera: 'front' | 'back' | 'left' | 'right'

    Returns:
        List of telemetry frames or None if unavailable.
    """
    # Check cache
    cache_dir = os.path.join(TELEMETRY_CACHE_DIR, folder)
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{event_id}_{camera}.json")

    if os.path.exists(cache_path):
        try:
            with open(cache_path, 'r') as f:
                data = json.load(f)
            logger.debug(f"Telemetry cache hit: {cache_path}")
            return data
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Cache read error: {e}, will re-extract")

    # Cache miss — extract from video
    video_path = find_video_path(folder, event_id, camera)
    if not video_path:
        logger.info(f"Video not found for {folder}/{event_id}/{camera}")
        return None

    frames = extract_telemetry(video_path)
    if frames is None:
        return None

    # Write cache
    try:
        with open(cache_path, 'w') as f:
            json.dump(frames, f, separators=(',', ':'))
        logger.info(f"Telemetry cached: {cache_path} ({len(frames)} frames)")
    except IOError as e:
        logger.error(f"Failed to write cache: {e}")

    return frames


def clear_telemetry_cache(folder: Optional[str] = None,
                          event_id: Optional[str] = None,
                          camera: Optional[str] = None) -> int:
    """Clear telemetry cache. Returns number of files removed."""
    removed = 0
    base = TELEMETRY_CACHE_DIR

    if folder and event_id and camera:
        # Clear specific file
        cache_path = os.path.join(base, folder, f"{event_id}_{camera}.json")
        if os.path.exists(cache_path):
            os.remove(cache_path)
            removed = 1
        return removed

    if folder and event_id:
        # Clear all cameras for this event
        for cam in ['front', 'back', 'left', 'right']:
            cache_path = os.path.join(base, folder, f"{event_id}_{cam}.json")
            if os.path.exists(cache_path):
                os.remove(cache_path)
                removed += 1
        return removed

    # Clear all
    for root, dirs, files in os.walk(base):
        for f in files:
            if f.endswith('.json'):
                os.remove(os.path.join(root, f))
                removed += 1
    return removed


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import sys

    if len(sys.argv) < 2:
        print("Usage: python sei_service.py <video.mp4>")
        print("       python sei_service.py <folder> <event_id> <camera>")
        sys.exit(1)

    if len(sys.argv) == 2:
        video_path = sys.argv[1]
        frames = extract_telemetry(video_path)
        if frames:
            print(f"Extracted {len(frames)} telemetry frames")
            for f in frames[:5]:
                print(f"  frame={f.get('frame_seq_no', 0)} "
                      f"speed={f.get('speed_kmh', 0)}km/h "
                      f"gear={f.get('gear_state', '?')} "
                      f"AP={f.get('autopilot_state', 'NONE')}")
        else:
            print("No SEI telemetry found")
    else:
        folder, event_id, camera = sys.argv[1:4]
        frames = get_telemetry(folder, event_id, camera)
        if frames:
            print(f"Telemetry for {folder}/{event_id}/{camera}: {len(frames)} frames")
        else:
            print("No telemetry available")
