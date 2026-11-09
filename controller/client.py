"""Thin HTTP client for the simulator with retry on transport errors / 5xx.

4xx responses are returned to the caller (they carry meaning: 409 stale, 410 lapsed, 402 credits...).
"""
from __future__ import annotations

import json
import logging
import time

import httpx

log = logging.getLogger("controller.client")


class SimClient:
    def __init__(self, url: str, timeout: float = 300.0, retries: int = 8):
        self.http = httpx.Client(base_url=url.rstrip("/"), timeout=timeout)
        self.retries = retries

    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        delay = 0.5
        for attempt in range(self.retries):
            try:
                r = self.http.request(method, path, **kw)
                if r.status_code < 500:
                    return r
                log.warning("%s %s -> %s, retrying", method, path, r.status_code)
            except httpx.TransportError as e:
                log.warning("%s %s transport error %s, retrying", method, path, e)
            time.sleep(delay)
            delay = min(delay * 2, 10)
        raise RuntimeError(f"{method} {path}: simulator unavailable after {self.retries} attempts")

    def get(self, path: str, **params):
        r = self._req("GET", path, params=params or None)
        return r.status_code, (r.json() if r.content else None)

    def post(self, path: str, body: dict):
        r = self._req("POST", path, json=body)
        return r.status_code, (r.json() if r.content else None)

    def put(self, path: str, body: dict):
        r = self._req("PUT", path, json=body)
        return r.status_code, (r.json() if r.content else None)

    def chunk_lines(self, n: int):
        r = self._req("GET", f"/events/chunks/{n}")
        if r.status_code != 200:
            raise RuntimeError(f"chunk {n}: {r.status_code}")
        for line in r.text.splitlines():
            if line:
                yield json.loads(line)
