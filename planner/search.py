"""
planner/search.py - stop-name search grouped by normalized name (SPEC 9.3).

Inputs: a Network.
Outputs: StopSearch.search(query, limit) -> list of result dicts.
"""

import re

import numpy as np

from planner.network import Network

NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize(text: str) -> str:
    """Lower-case, replace every non-alphanumeric character by a space, collapse spaces.

    Args:
        text: raw name or query.
    Returns:
        Normalized string.
    """
    lowered = text.lower()
    spaced = NON_ALNUM.sub(" ", lowered)
    return " ".join(spaced.split())


class StopSearch:
    """Name index over stop groups that share one normalized name."""

    def __init__(self, net: Network) -> None:
        """Group stops by normalized name and precompute ranking data.

        Args:
            net: the loaded network.
        Returns:
            None.
        """
        self.net = net
        members = {}
        for index in range(net.n_stops):
            key = normalize(str(net.stop_name[index]))
            if key == "":
                continue
            if key not in members:
                members[key] = []
            members[key].append(index)
        self.groups = []
        for key, stop_list in members.items():
            # Distinct patterns serving any member stop.
            patterns = set()
            best_stop = stop_list[0]
            best_count = -1
            for s in stop_list:
                serving = net.sp_pat[net.sp_ptr[s]:net.sp_ptr[s + 1]]
                distinct = np.unique(serving)
                patterns.update(int(p) for p in distinct)
                # Representative = the member served by the most patterns.
                if distinct.shape[0] > best_count:
                    best_count = distinct.shape[0]
                    best_stop = s
            self.groups.append({
                "key": key,
                "tokens": key.split(" "),
                "name": str(net.stop_name[best_stop]),
                "stop": best_stop,
                "n_stops": len(stop_list),
                "n_patterns": len(patterns),
            })

    def search(self, query: str, limit: int) -> list[dict]:
        """Ranked stop groups matching every query token (SPEC 9.3).

        Args:
            query: user text (at least 2 characters, checked by the caller).
            limit: maximum results.
        Returns:
            List of {"name", "lat", "lon", "stop_id", "n_stops", "n_patterns"}.
        """
        q = normalize(query)
        tokens = q.split(" ")
        scored = []
        for group in self.groups:
            key = group["key"]
            # A group matches when every token is a substring of its name.
            matches = True
            for token in tokens:
                if token not in key:
                    matches = False
                    break
            if not matches:
                continue
            rank = 3
            if key == q:
                rank = 0
            elif key.startswith(q):
                rank = 1
            else:
                all_prefix = True
                for token in tokens:
                    found = False
                    for name_token in group["tokens"]:
                        if name_token.startswith(token):
                            found = True
                            break
                    if not found:
                        all_prefix = False
                        break
                if all_prefix:
                    rank = 2
            scored.append((rank, -group["n_patterns"], len(key), key, group))
        scored.sort(key=sort_key)
        results = []
        for item in scored[:limit]:
            group = item[4]
            s = group["stop"]
            results.append({
                "name": group["name"],
                "lat": round(float(self.net.stop_lat[s]), 6),
                "lon": round(float(self.net.stop_lon[s]), 6),
                "stop_id": str(self.net.stop_id[s]),
                "n_stops": group["n_stops"],
                "n_patterns": group["n_patterns"],
            })
        return results


def sort_key(item: tuple) -> tuple:
    """Sort key for scored groups: rank, more patterns, shorter name, then name.

    Args:
        item: (rank, -n_patterns, name length, key, group).
    Returns:
        The first four fields.
    """
    return item[0], item[1], item[2], item[3]
