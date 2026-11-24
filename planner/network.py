"""
planner/network.py - load data/model/network.npz into a Network object (SPEC 4, 6.1).

Inputs: data/model/network.npz.
Outputs: a Network instance whose attributes are the numpy arrays of SPEC 6.1.
"""

from pathlib import Path

import numpy as np

import config


class Network:
    """Static network arrays: stops, routes, patterns, polylines, segments, footpaths."""

    def __init__(self, path: Path = config.NETWORK_PATH) -> None:
        """Load every array of network.npz as an attribute.

        Args:
            path: location of network.npz.
        Returns:
            None.
        """
        with np.load(path, allow_pickle=False) as data:
            for name in data.files:
                setattr(self, name, data[name])
        self.n_stops = int(self.stop_id.shape[0])
        self.n_routes = int(self.route_id.shape[0])
        self.n_patterns = int(self.pat_route.shape[0])
        self.n_segments = int(self.seg_from.shape[0])
        # Number of (pattern, position) entries per stop, used by search ranking.
        self.stop_n_patterns = np.diff(self.sp_ptr).astype(np.int32)

    def pattern_stops(self, pattern: int) -> np.ndarray:
        """Stop indices of a pattern in order.

        Args:
            pattern: pattern index.
        Returns:
            int32 array.
        """
        return self.pat_stops[self.pat_ptr[pattern]:self.pat_ptr[pattern + 1]]

    def pattern_segs(self, pattern: int) -> np.ndarray:
        """Segment index of each hop of a pattern (-1 at the last position).

        Args:
            pattern: pattern index.
        Returns:
            int32 array.
        """
        return self.pat_seg[self.pat_ptr[pattern]:self.pat_ptr[pattern + 1]]
