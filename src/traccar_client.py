"""
Forwards GPS positions to Traccar via OsmAnd HTTP protocol.
"""

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

    try:
        r = requests.get(f"{server_url}/", params=params, timeout=5)
        r.raise_for_status()
        logger.debug("Sent position for %s: %s, %s", device_id, lat, lon)
    except requests.RequestException as e:
        logger.warning("Failed to send position: %s", e)
