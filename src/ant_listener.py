"""
ANT+ listener for Garmin Alpha 100 / Astro dog tracking data.
Protocol reverse-engineered from AntAssetTracker by mikkosh:
https://github.com/mikkosh/AntAssetTracker
"""

import logging
import struct
from openant.easy.node import Node
from openant.devices.scanner import Scanner
from openant.devices import ANTPLUS_NETWORK_KEY

logger = logging.getLogger(__name__)

DEVICE_TYPE = 41       # ANT+ Asset Tracker profile
RF_FREQUENCY = 57
PERIOD = 2048

PAGE_LOCATION_FIRST      = 0x01
PAGE_LOCATION_SECOND     = 0x02
PAGE_IDENT_FIRST         = 0x10
PAGE_IDENT_SECOND        = 0x11
PAGE_NO_ASSETS           = 0x03
PAGE_DISCONNECT          = 0x20

SITUATION = {0: "sitting", 1: "moving", 2: "pointed", 3: "treed", 4: "unknown"}


def _semicircle_to_deg(value):
    return (value / 2**31) * 180.0


def _signed32(b0, b1, b2, b3):
    val = b0 | (b1 << 8) | (b2 << 16) | (b3 << 24)
    if val >= 2**31:
        val -= 2**32
    return val


class AssetTracker:
    """Tracks state for a single dog asset across paired ANT+ pages."""

    def __init__(self):
        self.name = ""
        self.color = 0
        self.asset_type = 0
        self.lat = None
        self.lon = None
        self.distance = 0
        self.bearing = 0.0
        self.situation = "unknown"
        self.low_battery = False
        self.gps_lost = False
        self.comm_lost = False


class AlphaListener:
    def __init__(self, on_position, on_disconnect=None):
        self.on_position = on_position
        self.on_disconnect = on_disconnect
        self._assets = {}           # index -> AssetTracker
        self._prev_payload = None
        self._subseq_first = 0

    def handle_broadcast(self, payload):
        page = payload[0] & 0xFF
        idx = payload[1] & 0x1F

        if page == PAGE_LOCATION_FIRST:
            if self._subseq_first == 0:
                self._subseq_first = 1
            else:
                self._subseq_first += 1
                if self._subseq_first > 3:
                    logger.info("Asset %d disconnected (no page 2)", idx)
                    self._assets.pop(idx, None)
                    if self.on_disconnect:
                        self.on_disconnect(idx)

        elif page == PAGE_LOCATION_SECOND and self._prev_payload is not None:
            self._subseq_first = 0
            prev = self._prev_payload
            if (prev[0] & 0xFF) == PAGE_LOCATION_FIRST and (prev[1] & 0x1F) == idx:
                asset = self._assets.get(idx, AssetTracker())
                self._parse_location(asset, prev, payload)
                self._assets[idx] = asset
                if not asset.gps_lost and asset.lat is not None:
                    self.on_position({
                        "device_id": str(idx),
                        "name": asset.name or f"Dog {idx}",
                        "lat": asset.lat,
                        "lon": asset.lon,
                        "situation": asset.situation,
                        "low_battery": asset.low_battery,
                        "bearing": asset.bearing,
                        "distance": asset.distance,
                    })

        elif page == PAGE_IDENT_FIRST:
            self._subseq_first = 0

        elif page == PAGE_IDENT_SECOND and self._prev_payload is not None:
            self._subseq_first = 0
            prev = self._prev_payload
            if (prev[0] & 0xFF) == PAGE_IDENT_FIRST and (prev[1] & 0x1F) == idx:
                asset = self._assets.get(idx, AssetTracker())
                self._parse_identification(asset, prev, payload)
                self._assets[idx] = asset
                logger.info("Identified asset %d: '%s'", idx, asset.name)

        elif page == PAGE_NO_ASSETS:
            self._assets.clear()

        elif page == PAGE_DISCONNECT:
            self._assets.clear()
            logger.info("Disconnect page received")

        self._prev_payload = payload

    @staticmethod
    def _parse_location(asset, p1, p2):
        asset.distance = p1[2] | (p1[3] << 8)
        bearing_brad = p1[4] & 0xFF
        asset.bearing = (bearing_brad / 256.0) * 360.0

        status_byte = p1[5] & 0xFF
        asset.situation    = SITUATION.get(status_byte & 0x7, "unknown")
        asset.low_battery  = bool((status_byte >> 3) & 0x1)
        asset.gps_lost     = bool((status_byte >> 4) & 0x1)
        asset.comm_lost    = bool((status_byte >> 5) & 0x1)
        should_remove      = bool((status_byte >> 6) & 0x1)

        if should_remove:
            asset.lat = None
            asset.lon = None
            return

        lat_semi = _signed32(p1[6], p1[7], p2[2], p2[3])
        lon_semi = _signed32(p2[4], p2[5], p2[6], p2[7])
        asset.lat = _semicircle_to_deg(lat_semi)
        asset.lon = _semicircle_to_deg(lon_semi)

    @staticmethod
    def _parse_identification(asset, p1, p2):
        asset.color = p1[2] & 0xFF
        asset.asset_type = p2[2] & 0xFF
        name_bytes = bytes([p1[3], p1[4], p1[5], p1[6], p1[7],
                            p2[3], p2[4], p2[5], p2[6], p2[7]])
        asset.name = name_bytes.rstrip(b'\x00').decode("utf-8", errors="replace")


def scan(timeout=30):
    """Scan for nearby ANT+ devices. Use this to find device_id for Alpha 100."""
    node = Node()
    node.set_network_key(0x00, ANTPLUS_NETWORK_KEY)
    scanner = Scanner(node)

    def on_found(device_tuple):
        device_id, device_type, trans = device_tuple
        print(f"Found  id={device_id}  type={device_type}  trans={trans}")

    scanner.on_found = on_found
    print(f"Scanning {timeout}s — power on your Alpha 100 handheld now...")
    try:
        node.start()
    except KeyboardInterrupt:
        pass
    finally:
        scanner.close_channel()
        node.stop()


def start(serial_port: str, device_id: int, on_position, on_disconnect=None):
    """
    Start listening for Alpha 100 broadcasts.
    Calls on_position(dict) with keys: device_id, name, lat, lon, situation, low_battery.
    """
    listener = AlphaListener(on_position, on_disconnect)

    node = Node()
    node.set_network_key(0x00, ANTPLUS_NETWORK_KEY)
    scanner = Scanner(node, device_id=device_id, device_type=DEVICE_TYPE)

    def on_device_data(device_tuple, page_name, data):
        # data here is the raw 8-byte payload from the scanner callback
        if hasattr(data, '__iter__'):
            listener.handle_broadcast(list(data))

    scanner.on_update = on_device_data

    logger.info("Listening — device_id=%s device_type=%s", device_id, DEVICE_TYPE)
    try:
        node.start()
    except KeyboardInterrupt:
        pass
    finally:
        scanner.close_channel()
        node.stop()
