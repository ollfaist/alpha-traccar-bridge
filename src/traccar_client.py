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

# Halsband som redan bekräftats finnas i Traccar (eller som vi själva just
# skapat) under den här körningen — namn vi känner till per device_id, så vi
# bara registrerar/döper om en gång och inte vid varje position.
_registered_names = {}
_registered_lock = threading.Lock()


def _register_device(admin, device_id, name):
    """Skapar (eller döper om) enheten i Traccar via admin-API:et. Körs bara
    när OsmAnd-porten svarat 400 på ett halsband vi inte känner igen sedan
    tidigare — dvs. aldrig på skräptrafik som redan avvisas där, och aldrig
    om servern inte är nåbar (då är 400 osannolikt ändå)."""
    try:
        r = requests.post(admin["url"] + "/api/devices", auth=admin["auth"],
                           json={"name": name, "uniqueId": str(device_id)}, timeout=5)
        if r.status_code in (200, 201):
            logger.info("Registrerade nytt halsband %s i Traccar som '%s'", device_id, name)
            return True
        if "duplicate" in r.text.lower() or "unique" in r.text.lower():
            # Redan skapad (t.ex. av en tidigare körning av bryggan) — inte
            # ett fel, bara att vi inte visste om det än.
            return True
        logger.warning("Kunde inte registrera halsband %s i Traccar: %s %s",
                        device_id, r.status_code, r.text[:200])
        return False
    except requests.RequestException as e:
        logger.warning("Kunde inte nå Traccars admin-API för att registrera %s: %s", device_id, e)
        return False


def _rename_device(admin, device_id, name):
    try:
        r = requests.get(admin["url"] + "/api/devices", auth=admin["auth"],
                          params={"uniqueId": str(device_id)}, timeout=5)
        r.raise_for_status()
        devices = r.json()
        if not devices:
            return
        dev = devices[0]
        if dev.get("name") == name:
            return
        dev["name"] = name
        requests.put(admin["url"] + "/api/devices/" + str(dev["id"]), auth=admin["auth"],
                     json=dev, timeout=5).raise_for_status()
        logger.info("Döpte om halsband %s till '%s' i Traccar", device_id, name)
    except requests.RequestException as e:
        logger.warning("Kunde inte döpa om halsband %s i Traccar: %s", device_id, e)


def _ensure_registered(admin, device_id, name, is_real_name):
    """name kan vara en platshållare ("Ny hund <id>") innan Garmin-namnet
    hunnit läsas — döper då om automatiskt så fort ett riktigt namn kommer,
    istället för att halsbandet fastnar med ett generiskt namn för alltid."""
    with _registered_lock:
        known = _registered_names.get(device_id)
    if known is None:
        if _register_device(admin, device_id, name):
            with _registered_lock:
                _registered_names[device_id] = name
    elif is_real_name and known != name:
        _rename_device(admin, device_id, name)
        with _registered_lock:
            _registered_names[device_id] = name


def _deliver(server_url, device_id, lat, lon, extras, admin=None):
    params = {"id": device_id, "lat": lat, "lon": lon}
    if extras:
        params.update(extras)

    for attempt in range(3):
        try:
            r = requests.get(server_url, params=params, timeout=5)
            if r.status_code == 400 and admin:
                real_name = (extras or {}).get("dogName")
                name = real_name or "Ny hund {}".format(device_id)
                _ensure_registered(admin, device_id, name, real_name is not None)
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


def send_position(server_url: str, device_id: str, lat: float, lon: float,
                   extras: dict = None, admin: dict = None):
    """Queue a position for delivery. Returns immediately — never blocks the caller.

    admin, if given, is {"url": <Traccar-adress med adminAPI>, "auth": (user, pass)} —
    används bara för att auto-registrera/döpa om okända halsband, aldrig för
    själva positionsleveransen (den går alltid via OsmAnd-porten som förut)."""
    global _dropped
    _ensure_worker()
    item = (server_url, device_id, lat, lon, extras, admin)
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
