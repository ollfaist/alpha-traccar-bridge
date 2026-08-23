"""
Forwards GPS positions to Traccar via OsmAnd HTTP protocol.
Traccar endpoint: http://<server>:5055/?id=X&lat=Y&lon=Z

Sending runs on a background thread. send_position() only enqueues, because it
is called from openant's data-dispatch thread: blocking there stops page
decoding for every dog at once, and the retry path below can take ~19s — which
is exactly what happens when the WAN link is flaky in the forest.
"""

import logging
import queue
import threading
import time

import requests

logger = logging.getLogger(__name__)

# Bounded so a long outage cannot grow without limit. When full the oldest
# position is dropped: recent track matters more than a stale backlog.
_QUEUE_MAX = 200

_queue = queue.Queue(maxsize=_QUEUE_MAX)
_worker = None
_worker_lock = threading.Lock()
_dropped = 0


def _deliver(server_url, device_id, lat, lon, extras):
    params = {"id": device_id, "lat": lat, "lon": lon}
    if extras:
        params.update(extras)

    for attempt in range(3):
        try:
            r = requests.get(server_url, params=params, timeout=5)
            r.raise_for_status()
            logger.debug("Sent position for %s: %.6f, %.6f", device_id, lat, lon)
            return
        except requests.RequestException as e:
            if attempt < 2:
                logger.warning("Traccar send failed (attempt %d): %s — retrying", attempt + 1, e)
                time.sleep(2)
            else:
                logger.warning("Failed to send to Traccar: %s", e)


def _run():
    while True:
        item = _queue.get()
        try:
            _deliver(*item)
        except Exception:
            # Never let the worker die — positions would stop silently.
            logger.exception("Unexpected error delivering position")
        finally:
            _queue.task_done()


def _ensure_worker():
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="traccar-sender", daemon=True)
            _worker.start()


def send_position(server_url: str, device_id: str, lat: float, lon: float, extras: dict = None):
    """Queue a position for delivery. Returns immediately — never blocks the caller."""
    global _dropped
    _ensure_worker()
    item = (server_url, device_id, lat, lon, extras)
    try:
        _queue.put_nowait(item)
        return
    except queue.Full:
        pass

    try:
        _queue.get_nowait()
        _queue.task_done()
        _dropped += 1
        if _dropped % 50 == 1:
            logger.warning("Traccar backlog full — dropped %d oldest position(s)", _dropped)
    except queue.Empty:
        pass

    try:
        _queue.put_nowait(item)
    except queue.Full:
        pass


def pending() -> int:
    """Positions waiting to be delivered — useful for diagnosing a stalled link."""
    return _queue.qsize()
