"""Stub for dashcam_pb2 — minimal, no protobuf import needed.

On A7Z with protobuf >= 4.x, use:
    pip3 install 'protobuf==3.20.3'
    protoc --python_out=. dashcam.proto

Until then, sei_service uses the manual protobuf parser.
"""

class SeiMetadata:
    """Minimal stub — real parsing uses sei_service._manual_parse()."""
    DESCRIPTOR = None
    
    def ParseFromString(self, data):
        raise NotImplementedError("Use manual parser in sei_service")
