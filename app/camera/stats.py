"""Measure real throughput per camera from the RTSP relay.

MediaMTX counts the bytes it pulls from the source and the bytes it hands to
readers. Sampling those counters gives the actual incoming and outgoing bitrate,
and the spread of the incoming samples says whether the source encodes at a
constant or a variable rate.

Its own count of what it handed out is not what left the container, though. The
detector reads the relay, so does the live view, so does every snapshot -- all
over loopback, all counted by the relay as bytes sent. A camera with detection
on would report an outgoing stream even with nothing watching it, and that
number would be wrong by however much work it is doing on its own behalf.

So readers are counted separately by where they came from. The relay's session
list gives each one's address; anything from 127.0.0.1 or ::1 is us. "Out" is
what crossed the network, which is the figure anyone sizing a link needs; what
the camera serves itself is reported beside it, because it is worth seeing and
is not the same thing.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

API_URL = "http://127.0.0.1:9997/v3/paths/list"
# Readers appear in the path list only as a count, so their addresses have to
# come from the session list.
SESSIONS_URL = "http://127.0.0.1:9997/v3/rtspsessions/list"
SAMPLE_SECONDS = 2.0
# Two minutes of history: enough spread for the rate-mode call, small enough to
# follow a scene change within a minute.
WINDOW = 60
# Below this coefficient of variation the source is holding a constant rate.
CBR_THRESHOLD = 0.08
MIN_SAMPLES_FOR_MODE = 15


def is_local(address: str) -> bool:
    """Whether a reader's address is this container talking to itself."""
    host = (address or "").strip()
    if host.startswith("["):            # [::1]:54321
        host = host[1:host.find("]")] if "]" in host else host[1:]
    elif host.count(":") == 1:          # 127.0.0.1:54321
        host = host.split(":")[0]
    return host == "::1" or host == "localhost" or host.startswith("127.")


class Sessions:
    """A monotonic byte total across sessions that come and go.

    Each session carries its own counter starting at zero, so a session that
    ends takes its bytes out of any sum of them. Accumulating the per-session
    differences instead gives a total that only ever rises, which is what the
    rate arithmetic below needs.
    """

    def __init__(self):
        self.total = 0
        self._seen: dict[str, int] = {}

    def update(self, current: dict):
        for key, sent in current.items():
            # A session absent last time started at zero, so all of it is new.
            previous = self._seen.get(key, 0)
            if sent >= previous:
                self.total += sent - previous
        self._seen = dict(current)


class LinkStats:
    """What actually crossed the camera's network interface.

    The relay's counters measure the relay. When a stream is re-encoded, ffmpeg
    pulls from the camera and publishes the *result* into the relay, so what the
    relay received is the encoded rate -- the one figure that is certainly not
    what the camera sent. The kernel's own byte counters do not care how many
    processes are involved: they count what arrived on the wire.

    It is a per-interface figure rather than a per-stream one, so a camera
    offering a main and a sub stream reports them together, and acknowledgements
    for what we are sending out are included -- a few percent at most. It is the
    only measurement available that is not downstream of the encoder.
    """

    def __init__(self, iface: str = ""):
        self.iface = iface or os.environ.get("CAM_IFACE", "eth0")
        self.in_bps = 0.0
        self.out_bps = 0.0
        self.available = False
        self._last: tuple[float, int, int] | None = None

    def path(self, direction: str) -> str:
        return f"/sys/class/net/{self.iface}/statistics/{direction}_bytes"

    def read(self) -> tuple[int, int] | None:
        try:
            with open(self.path("rx")) as fh:
                received = int(fh.read().strip())
            with open(self.path("tx")) as fh:
                sent = int(fh.read().strip())
        except (OSError, ValueError):
            return None
        return received, sent

    def update(self, now: float, counters: tuple[int, int] | None = None):
        counters = self.read() if counters is None else counters
        if counters is None:
            self.available = False
            return
        received, sent = counters
        previous, self._last = self._last, (now, received, sent)
        if previous is None:
            return
        elapsed = now - previous[0]
        if elapsed <= 0:
            return
        if received < previous[1] or sent < previous[2]:
            # The interface was recreated; start again rather than report a spike.
            return
        self.available = True
        self.in_bps = (received - previous[1]) * 8 / elapsed
        self.out_bps = (sent - previous[2]) * 8 / elapsed

    def snapshot(self) -> dict:
        return {
            "available": self.available,
            "iface": self.iface,
            "in_bps": round(self.in_bps),
            "out_bps": round(self.out_bps),
        }


class PathStats:
    """Rolling throughput for one relay path."""

    def __init__(self):
        self.in_bps = 0.0
        self.out_bps = 0.0
        self.local_bps = 0.0
        self.bytes_in = 0
        self.bytes_out = 0
        self.readers = 0
        self.local_readers = 0
        self.ready = False
        self.history: list[float] = []
        self._last: tuple[float, int, int, int] | None = None
        self._remote = Sessions()
        self._local = Sessions()

    def update(self, now: float, bytes_in: int, bytes_out: int, readers: int,
               ready: bool, remote: dict | None = None, local: dict | None = None):
        """remote and local map a reader's session id to its bytes sent.

        With neither given there is no session list to go on -- an older relay,
        or a failed call -- and everything the relay sent counts as outgoing.
        Overstating it is the lesser error: a number that is too low would say a
        link has room it does not have.
        """
        split = remote is not None or local is not None
        self._remote.update(remote or {})
        self._local.update(local or {})
        self.local_readers = len(local or {})
        self.readers = len(remote or {}) if split else readers
        self.ready = ready

        previous = self._last
        out_remote = self._remote.total if split else bytes_out
        self._last = (now, bytes_in, bytes_out, out_remote)
        self.bytes_in = bytes_in
        self.bytes_out = bytes_out
        if previous is None:
            return

        elapsed = now - previous[0]
        if elapsed <= 0:
            return
        # A relay restart resets the counters; treat a decrease as a fresh start.
        if bytes_in < previous[1] or bytes_out < previous[2]:
            self.history.clear()
            return

        self.in_bps = (bytes_in - previous[1]) * 8 / elapsed
        self.out_bps = max(0.0, (out_remote - previous[3]) * 8 / elapsed)
        # Everything the relay handed out that did not leave the container.
        served = (bytes_out - previous[2]) * 8 / elapsed
        self.local_bps = max(0.0, served - self.out_bps)

        if self.ready and self.in_bps > 0:
            self.history.append(self.in_bps)
            del self.history[:-WINDOW]

    def rate_mode(self) -> tuple[str, float]:
        """Classify the source's rate control from the measured spread.

        RTSP carries no rate-control flag, so this is inferred, not reported.
        """
        samples = self.history
        if len(samples) < MIN_SAMPLES_FOR_MODE:
            return "", 0.0
        mean = sum(samples) / len(samples)
        if mean <= 0:
            return "", 0.0
        variance = sum((s - mean) ** 2 for s in samples) / len(samples)
        cv = (variance ** 0.5) / mean
        return ("CBR" if cv < CBR_THRESHOLD else "VBR"), round(cv, 3)

    def snapshot(self) -> dict:
        mode, cv = self.rate_mode()
        return {
            "in_bps": round(self.in_bps),
            "out_bps": round(self.out_bps),
            "local_bps": round(self.local_bps),
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "readers": self.readers,
            "local_readers": self.local_readers,
            "ready": self.ready,
            "rate_mode": mode,
            "rate_cv": cv,
            "measured_kbps": round(sum(self.history) / len(self.history) / 1000)
            if self.history
            else 0,
        }


class StatsCollector(threading.Thread):
    """Polls the relay's API and keeps per-path throughput up to date."""

    def __init__(self, api_url: str = API_URL, sessions_url: str = SESSIONS_URL):
        super().__init__(name="relay-stats", daemon=True)
        self.api_url = api_url
        self.sessions_url = sessions_url
        self.link = LinkStats()
        self.paths: dict[str, PathStats] = {}
        self.available = False
        self.error = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def run(self):
        while not self._stop.wait(SAMPLE_SECONDS):
            # Sampled whatever the relay says: it is the one figure that stays
            # true when a stream is being re-encoded, and it costs two file
            # reads.
            with self._lock:
                self.link.update(time.monotonic())
            try:
                payload = self._fetch()
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                with self._lock:
                    self.available = False
                    self.error = str(exc)[:200]
                continue

            # A relay too old to list sessions, or one that failed the second
            # call, leaves this empty and the split is simply not made.
            try:
                remote, local = self._readers()
            except Exception:  # noqa: BLE001 - the throughput figures still stand
                remote, local = {}, {}

            now = time.monotonic()
            with self._lock:
                self.available = True
                self.error = ""
                for item in payload.get("items", []):
                    name = item.get("name")
                    if not name:
                        continue
                    stats = self.paths.setdefault(name, PathStats())
                    stats.update(
                        now,
                        int(item.get("bytesReceived") or 0),
                        int(item.get("bytesSent") or 0),
                        len(item.get("readers") or []),
                        bool(item.get("ready")),
                        remote.get(name),
                        local.get(name),
                    )

    def _readers(self) -> tuple[dict, dict]:
        """Reading sessions per path, split by whether they came over the network.

        Only sessions in the reading state: a publisher is the transcoder
        handing the relay a stream, which is not an outgoing anything.
        """
        remote: dict[str, dict] = {}
        local: dict[str, dict] = {}
        for item in self._get(self.sessions_url).get("items", []):
            if item.get("state") != "read":
                continue
            path = item.get("path")
            if not path:
                continue
            side = local if is_local(item.get("remoteAddr", "")) else remote
            side.setdefault(path, {})[str(item.get("id"))] = int(
                item.get("bytesSent") or 0
            )
        # Every path that had a session of one kind needs an empty entry for the
        # other, or a reader that has just gone would look like one that is
        # merely unaccounted for.
        for path in set(remote) | set(local):
            remote.setdefault(path, {})
            local.setdefault(path, {})
        return remote, local

    def _fetch(self) -> dict:
        return self._get(self.api_url)

    @staticmethod
    def _get(url: str) -> dict:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=4) as response:
            return json.loads(response.read().decode("utf-8", "replace"))

    def snapshot(self) -> dict:
        """Per-path stats plus a total, shaped for the state file."""
        with self._lock:
            paths = {name: stats.snapshot() for name, stats in self.paths.items()}
            available = self.available
            error = self.error
            link = self.link.snapshot()

        return {
            "available": available,
            "error": error,
            "paths": paths,
            "link": link,
            "in_bps": sum(p["in_bps"] for p in paths.values()),
            "out_bps": sum(p["out_bps"] for p in paths.values()),
            "local_bps": sum(p["local_bps"] for p in paths.values()),
            "readers": sum(p["readers"] for p in paths.values()),
            "local_readers": sum(p["local_readers"] for p in paths.values()),
            "ready": any(p["ready"] for p in paths.values()),
            "rate_mode": paths.get("main", {}).get("rate_mode", ""),
            "rate_cv": paths.get("main", {}).get("rate_cv", 0.0),
            "measured_kbps": paths.get("main", {}).get("measured_kbps", 0),
        }

    def stop(self):
        self._stop.set()
