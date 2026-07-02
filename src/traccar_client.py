"""
Forwards GPS positions to Traccar via OsmAnd HTTP protocol.
Traccar endpoint: http://<server>:5055/?id=X&lat=Y&lon=Z
"""

import time
import requests
import logging

logger = logging.getLogger(__name__)


def send_position(server_url: str, device_id: str, lat: float, lon: float, extras: dict = None):
    params = {
        "id": device_id,
        "lat": lat,
        "lon": lon,
    }
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
