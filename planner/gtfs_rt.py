"""
planner/gtfs_rt.py - minimal GTFS-Realtime VehiclePositions decoder (no extra packages).

Purpose: decode the protobuf FeedMessage served by otd.delhi.gov.in into plain rows.
Only the fields the live engine needs are read: vehicle id, route id, trip id,
latitude, longitude, timestamp.
Inputs: raw protobuf bytes.
Outputs: (header timestamp, list of vehicle dicts).
"""

import struct

WIRE_VARINT = 0
WIRE_64BIT = 1
WIRE_LEN = 2
WIRE_32BIT = 5


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Read one base-128 varint.

    Args:
        buf: message bytes.
        pos: start offset.
    Returns:
        Tuple (value, next offset).
    """
    result = 0
    shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, pos
        shift += 7


def fields(buf: bytes) -> list[tuple[int, int, object]]:
    """Split one protobuf message into (field number, wire type, raw value) triples.

    Args:
        buf: message bytes.
    Returns:
        List of triples; length-delimited values are bytes, fixed32/64 are bytes,
        varints are ints.
    """
    out = []
    pos = 0
    end = len(buf)
    while pos < end:
        key, pos = read_varint(buf, pos)
        number = key >> 3
        wire = key & 0x7
        if wire == WIRE_VARINT:
            value, pos = read_varint(buf, pos)
        elif wire == WIRE_64BIT:
            value = buf[pos:pos + 8]
            pos += 8
        elif wire == WIRE_LEN:
            length, pos = read_varint(buf, pos)
            value = buf[pos:pos + length]
            pos += length
        elif wire == WIRE_32BIT:
            value = buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        out.append((number, wire, value))
    return out


def decode_vehicle(buf: bytes) -> dict:
    """Decode a VehiclePosition message.

    Args:
        buf: VehiclePosition bytes.
    Returns:
        Dict with trip_id, route_id, vehicle_id, lat, lon, ts (any may be None).
    """
    row = {"trip_id": None, "route_id": None, "vehicle_id": None,
           "lat": None, "lon": None, "ts": None}
    for number, wire, value in fields(buf):
        if number == 1 and wire == WIRE_LEN:
            # TripDescriptor: 1 trip_id, 5 route_id.
            for sub_number, sub_wire, sub_value in fields(value):
                if sub_number == 1 and sub_wire == WIRE_LEN:
                    row["trip_id"] = sub_value.decode("utf-8", "replace")
                elif sub_number == 5 and sub_wire == WIRE_LEN:
                    row["route_id"] = sub_value.decode("utf-8", "replace")
        elif number == 2 and wire == WIRE_LEN:
            # Position: 1 latitude (float), 2 longitude (float).
            for sub_number, sub_wire, sub_value in fields(value):
                if sub_number == 1 and sub_wire == WIRE_32BIT:
                    row["lat"] = struct.unpack("<f", sub_value)[0]
                elif sub_number == 2 and sub_wire == WIRE_32BIT:
                    row["lon"] = struct.unpack("<f", sub_value)[0]
        elif number == 5 and wire == WIRE_VARINT:
            row["ts"] = value
        elif number == 8 and wire == WIRE_LEN:
            # VehicleDescriptor: 1 id, 2 label, 3 license_plate; the first non-empty wins.
            for sub_number, sub_wire, sub_value in fields(value):
                if sub_wire == WIRE_LEN and row["vehicle_id"] is None and len(sub_value) > 0:
                    row["vehicle_id"] = sub_value.decode("utf-8", "replace")
    return row


def decode_feed(buf: bytes) -> tuple[int | None, list[dict]]:
    """Decode a FeedMessage into its header timestamp and vehicle rows.

    Args:
        buf: FeedMessage bytes.
    Returns:
        Tuple (header timestamp or None, list of vehicle dicts).
    """
    header_ts = None
    rows = []
    for number, wire, value in fields(buf):
        if number == 1 and wire == WIRE_LEN:
            for sub_number, sub_wire, sub_value in fields(value):
                if sub_number == 3 and sub_wire == WIRE_VARINT:
                    header_ts = sub_value
        elif number == 2 and wire == WIRE_LEN:
            # FeedEntity: 1 id, 4 vehicle.
            entity_id = None
            vehicle = None
            for sub_number, sub_wire, sub_value in fields(value):
                if sub_number == 1 and sub_wire == WIRE_LEN:
                    entity_id = sub_value.decode("utf-8", "replace")
                elif sub_number == 4 and sub_wire == WIRE_LEN:
                    vehicle = decode_vehicle(sub_value)
            if vehicle is not None:
                if vehicle["vehicle_id"] is None:
                    vehicle["vehicle_id"] = entity_id
                rows.append(vehicle)
    return header_ts, rows
