"""Grafana annotations for fault injection and recovery, when Grafana is reachable.

Best effort and quiet: the lab must run the same with or without the monitoring profile.
"""

from __future__ import annotations

import os
import time

import httpx

DEFAULT_URL = "http://127.0.0.1:3000"
DASHBOARD_UID = "leaderfall"


class Annotator:
    def __init__(self, url: str | None = None) -> None:
        self.url = (url or os.environ.get("LEADERFALL_GRAFANA", DEFAULT_URL)).rstrip("/")
        self._http = httpx.Client(timeout=2.0, auth=("admin", "admin"))
        self.enabled = self._probe()

    def _probe(self) -> bool:
        try:
            return self._http.get(f"{self.url}/api/health").status_code == 200
        except httpx.HTTPError:
            return False

    def post(self, text: str, tags: list[str]) -> None:
        """Annotate "now" on the Leaderfall dashboard. Failures are ignored."""
        if not self.enabled:
            return
        body = {
            "dashboardUID": DASHBOARD_UID,
            "time": int(time.time() * 1000),
            "tags": ["leaderfall", *tags],
            "text": text,
        }
        try:
            self._http.post(f"{self.url}/api/annotations", json=body)
        except httpx.HTTPError:
            self.enabled = False
