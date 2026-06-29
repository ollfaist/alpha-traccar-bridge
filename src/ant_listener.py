"""
ANT+ listener for Garmin Alpha 100 dog tracking data.

Protocol sources:
- AntAssetTracker (mikkosh): page structure and coordinate parsing
- Working capture session: correct network key, period, and API usage
"""

import logging
from openant.easy.node import Node
from openant.easy.channel import Channel

logger = logging.getLogger(__name__)

# Garmin-specific network key (not standard ANT+ key)
GARMIN_NETWORK_KEY = [***REMOVED-ANT-NETWORK-KEY***]

DEVICE_TYPE = 41
RF_FREQUENCY = 57
PERIOD = 8192

# Page numbers observed from capture session
PAGE_LOCATION_FIRST  = 0x01
PAGE_LOCATION_SECOND = 0x02
PAGE_STATUS          = 0x10
PAGE_POSITION        = 0x11
PAGE_NO_ASSETS       = 0x03
PAGE_DISCONNECT      = 0x20

SITUATION = {0: "sitting", 1: "moving", 2: "pointed", 3: "treed", 4: "unknown"}


def _semicircle_to_deg(value):
    if value >= 2**31:
        value -= 2**32
    return (value / 2**31) * 180.0


class AssetTracker:
    def __init__(self):
        self.name = ""
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
        self._assets = {}
        self._prev_payload = None
        self._subseq_first = 0

    def handle_broadcast(self, data):
        payload = list(data)
        if len(payload) < 8:
            return

        page = payload[0] & 0xFF
        idx  = payload[1] & 0x1F

        logger.debug("Page 0x%02x asset %d: %s", page, idx,
                     ' '.join(f'{b:02x}' for b in payload))

        if page == PAGE_LOCATION_FIRST:
            if self._subseq_first == 0:
                self._subseq_first = 1
            else:
                self._subseq_first += 1
                if self._subseq_first > 3:
                    logger.info("Asset %d disconnect (no page 2)", idx)
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
                    self._emit(idx, asset)

        elif page == PAGE_POSITION and self._prev_payload is not None:
            # Alternative page layout observed in capture — treat as combined position
            self._subseq_first = 0
            prev = self._prev_payload
            if (prev[0] & 0xFF) == PAGE_STATUS and (prev[1] & 0x1F) == idx:
                asset = self._assets.get(idx, AssetTracker())
                self._parse_location(asset, prev, payload)
                self._assets[idx] = asset
                if not asset.gps_lost and asset.lat is not None:
                    self._emit(idx, asset)

        elif page == PAGE_NO_ASSETS:
            self._assets.clear()

        elif page == PAGE_DISCONNECT:
            self._assets.clear()

        self._prev_payload = payload

    def _emit(self, idx, asset):
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

    @staticmethod
    def _parse_location(asset, p1, p2):
        asset.distance = p1[2] | (p1[3] << 8)
        asset.bearing  = ((p1[4] & 0xFF) / 256.0) * 360.0

        s = p1[5] & 0xFF
        asset.situation   = SITUATION.get(s & 0x7, "unknown")
        asset.low_battery = bool((s >> 3) & 0x1)
        asset.gps_lost    = bool((s >> 4) & 0x1)
        asset.comm_lost   = bool((s >> 5) & 0x1)
        should_remove     = bool((s >> 6) & 0x1)

        if should_remove:
            asset.lat = asset.lon = None
            return

        lat_semi = (p1[6] | (p1[7] << 8) | (p2[2] << 16) | (p2[3] << 24))
        lon_semi = (p2[4] | (p2[5] << 8) | (p2[6] << 16) | (p2[7] << 24))
        asset.lat = _semicircle_to_deg(lat_semi)
        asset.lon = _semicircle_to_deg(lon_semi)

    @staticmethod
    def _parse_name(p1, p2):
        name_bytes = bytes([p1[3], p1[4], p1[5], p1[6], p1[7],
                            p2[3], p2[4], p2[5], p2[6], p2[7]])
        return name_bytes.rstrip(b'\x00').decode("utf-8", errors="replace")


def dump(on_data=None):
    """Raw dump mode — prints all pages. Useful for protocol analysis on new firmware."""
    def _default(data):
        page = data[0]
        asset_id = data[1]
        hex_dump = ' '.join(f'{b:02x}' for b in data)
        if page != PAGE_NO_ASSETS:
            label = "POS " if page == PAGE_POSITION else "STAT" if page == PAGE_STATUS else f"0x{page:02x} "
            print(f"[{label}] ID:{asset_id:02x} | {hex_dump}")

    _run(on_data or _default)


def start(device_id: int, on_position, on_disconnect=None):
    """Listen for Alpha 100 broadcasts and call on_position(dict) on each fix."""
    listener = AlphaListener(on_position, on_disconnect)
    _run(listener.handle_broadcast, device_id=device_id)


def _run(on_data, device_id=0):
    node = Node()
    node.set_network_key(0x00, GARMIN_NETWORK_KEY)
    channel = node.new_channel(Channel.Type.BIDIRECTIONAL_RECEIVE)
    channel.on_broadcast_data = on_data
    channel.set_period(PERIOD)
    channel.set_rf_freq(RF_FREQUENCY)
    channel.set_id(device_id, DEVICE_TYPE, 0)

    logger.info("ANT+ channel open — device_id=%s type=%s", device_id, DEVICE_TYPE)
    try:
        channel.open()
        node.start()
    except KeyboardInterrupt:
        pass
    finally:
        channel.close()
        node.stop()
