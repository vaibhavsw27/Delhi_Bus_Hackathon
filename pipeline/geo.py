"""
pipeline/geo.py - geometry helpers shared by the pipeline, the planner, and validation.

Purpose: degree <-> metre conversion around Delhi (SPEC 6), haversine distance, and the
numba point-to-polyline projection used by the matcher (SPEC 6.4 step 3).
Inputs: numpy arrays of coordinates.
Outputs: numpy arrays of metres, distances, along-distances and offsets.
"""

import math

import numpy as np
from numba import njit, prange

import config

# Scale factor of longitude degrees at the reference latitude, computed once.
COS_LAT0 = math.cos(math.radians(config.LAT0))
EARTH_RADIUS_M = 6371008.8


def to_xy(lat: np.ndarray, lon: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Convert degrees to local metres around (LAT0, LON0) with the SPEC 6 formula.

    Args:
        lat: latitudes in degrees.
        lon: longitudes in degrees.
    Returns:
        Tuple (x, y) of float64 arrays in metres.
    """
    x = (np.asarray(lon, dtype=np.float64) - config.LON0) * COS_LAT0 * config.METRES_PER_DEG
    y = (np.asarray(lat, dtype=np.float64) - config.LAT0) * config.METRES_PER_DEG
    return x, y


def haversine_m(lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray,
                lon2: np.ndarray) -> np.ndarray:
    """Great-circle distance in metres, vectorized.

    Args:
        lat1, lon1: first points in degrees.
        lat2, lon2: second points in degrees (broadcast against the first).
    Returns:
        Array of distances in metres.
    """
    phi1 = np.radians(lat1)
    phi2 = np.radians(lat2)
    dphi = phi2 - phi1
    dlam = np.radians(np.asarray(lon2) - np.asarray(lon1))
    # Standard haversine term, clipped so rounding never pushes it above 1.
    a = np.sin(dphi / 2.0) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlam / 2.0) ** 2
    a = np.clip(a, 0.0, 1.0)
    return 2.0 * EARTH_RADIUS_M * np.arcsin(np.sqrt(a))


@njit(cache=True)
def project_one(px: float, py: float, vx: np.ndarray, vy: np.ndarray,
                vcum: np.ndarray, lo: int, hi: int) -> tuple[float, float]:
    """Project one point onto polyline edges lo..hi-1 and return the nearest hit.

    Args:
        px, py: point in metres.
        vx, vy: polyline vertices in metres.
        vcum: cumulative distance at each vertex.
        lo, hi: vertex range; edges run from vertex i to i+1 for lo <= i < hi.
    Returns:
        Tuple (along_distance_m, offset_m) of the nearest point on those edges.
    """
    best_off2 = 1e30
    best_d = 0.0
    for i in range(lo, hi):
        ax = vx[i]
        ay = vy[i]
        ex = vx[i + 1] - ax
        ey = vy[i + 1] - ay
        len2 = ex * ex + ey * ey
        # Parameter of the foot point along the edge, clamped to the edge ends.
        t = 0.0
        if len2 > 0.0:
            t = ((px - ax) * ex + (py - ay) * ey) / len2
            if t < 0.0:
                t = 0.0
            elif t > 1.0:
                t = 1.0
        fx = ax + t * ex
        fy = ay + t * ey
        off2 = (px - fx) * (px - fx) + (py - fy) * (py - fy)
        if off2 < best_off2:
            best_off2 = off2
            best_d = vcum[i] + t * (vcum[i + 1] - vcum[i])
    return best_d, math.sqrt(best_off2)


@njit(cache=True, parallel=True)
def project_points(px: np.ndarray, py: np.ndarray, vx: np.ndarray, vy: np.ndarray,
                   vcum: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Project many points onto one whole polyline (SPEC 6.4 step 3).

    Args:
        px, py: points in metres.
        vx, vy: polyline vertices in metres (at least 2).
        vcum: cumulative distance at each vertex.
    Returns:
        Tuple (d, off): along-distance and offset in metres for every point.
    """
    n = px.shape[0]
    d = np.empty(n, dtype=np.float64)
    off = np.empty(n, dtype=np.float64)
    last_edge_start = vx.shape[0] - 1
    # Points are independent, so the loop runs in parallel over points.
    for k in prange(n):
        result = project_one(px[k], py[k], vx, vy, vcum, 0, last_edge_start)
        d[k] = result[0]
        off[k] = result[1]
    return d, off
