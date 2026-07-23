"""
ANT+ listener for Garmin Alpha 100 dog tracking data.

Protocol verified against live Alpha 100 hardware.
Byte layout confirmed via raw capture session and the ANT+ Tracker Device Profile.
"""

import logging
import os
import time
from openant.easy.node import Node
from openant.easy.channel import Channel

logger = logging.getLogger(__name__)

# ANT+ Managed Network Key — licensed under the ANT+ Adopter's Agreement, must
# not be committed to source. Set via env var; get your own from thisisant.com.
_NETWORK_KEY_HEX = os.environ.get("ANT_NETWORK_KEY", "")
if not _NETWORK_KEY_HEX:
    raise RuntimeError(
        "ANT_NETWORK_KEY env var not set. Provide the ANT+ Managed Network Key "
        "as 16 hex chars, e.g. ANT_NETWORK_KEY=XXXXXXXXXXXXXXXX"
    )
NETWORK_KEY = [int(_NETWORK_KEY_HEX[i:i + 2], 16) for i in range(0, 16, 2)]

SITUATIONS = {
    0: "Sitting",
    1: "Moving",
    2: "Pointed",
    3: "Treed",
    4: "Unknown",
    7: "Not connected",
}

BATTERY_STATUS = {0: "New", 1: "Good", 2: "Ok", 3: "Low", 4: "Critical"}

sync_buffer = {}


def _semi_to_deg(semi):
    if semi > 0x7FFFFFFF:
        semi -= 0x100000000
    return (float(semi) / 2147483648.0) * 180.0


def _update_name(asset_id):
    p1 = sync_buffer.get(str(asset_id) + "_name1")
    p2 = sync_buffer.get(str(asset_id) + "_name2")
    if p1 is not None and p2 is not None:
        name = (p1 + p2).strip()
        if name and sync_buffer.get(str(asset_id) + "_name") != name:
            sync_buffer[str(asset_id) + "_name"] = name
            logger.info("Dog %d name: %s", asset_id, name)


# Identification pages (0x10/0x11, carrying the Garmin dog name) are only sent by
# the Alpha in response to a data-request page (0x46). Without asking we never see
# the name and stay stuck on "Dog N". Payload requests page 0x10, 4 times.
_REQUEST_ID_PAYLOAD = [0x46, 0xFF, 0xFF, 0xFF, 0xFF, 0x04, 0x10, 0x04]


def _maybe_request_name(channel, asset_id):
    if channel is None or sync_buffer.get(str(asset_id) + "_name"):
        return
    now = time.time()
    if now - sync_buffer.get(str(asset_id) + "_name_req", 0) < 5:
        return
    sync_buffer[str(asset_id) + "_name_req"] = now
    try:
        channel.send_acknowledged_data(_REQUEST_ID_PAYLOAD)
        logger.debug("Requested identification for asset %d", asset_id)
    except Exception as exc:
        logger.debug("Identification request failed: %s", exc)


def _on_data(data, on_position, channel=None):
    page = data[0]
    asset_id = int(data[1])
    # Only the low 5 bits are the asset index; the upper bits differ between
    # page types (location byte1=0x64→100, identification byte1=0xE4→228, same
    # dog). Name/identification state is keyed by this masked index so it joins
    # with the location stream. device_id keeps the raw location byte for a
    # stable Traccar id. See mikkosh/AntAssetTracker parseAssetIdx (&0x1F).
    idx = data[1] & 0x1F

    if page in (0x01, 0x02):
        _maybe_request_name(channel, idx)

    if page == 0x01:
        sync_buffer[asset_id] = data
        distance   = data[2] | (data[3] << 8)
        bearing    = (data[4] / 256.0) * 360.0
        status_raw = data[5] & 0x07
        low_bat    = bool((data[5] >> 3) & 1)
        gps_lost   = bool((data[5] >> 4) & 1)
        comm_lost  = bool((data[5] >> 5) & 1)

        logger.debug("Dog %d: %s dist=%dm bearing=%.0fdeg low_bat=%s gps_lost=%s",
                     asset_id, SITUATIONS.get(status_raw, str(status_raw)),
                     distance, bearing, low_bat, gps_lost)

        if not gps_lost:
            sync_buffer[str(asset_id) + "_meta"] = {
                "distance": distance,
                "bearing": bearing,
                "situation": SITUATIONS.get(status_raw, "Code {}".format(status_raw)),
                "low_battery": low_bat,
                "comm_lost": comm_lost,
            }

    elif page == 0x02:
        # Consume the paired page 0x01 so a rebroadcast 0x02 can't re-emit with
        # a stale latitude low-half. Page 0x02 is broadcast more often than 0x01;
        # emitting on every 0x02 froze the latitude between 0x01 updates and drew
        # a right-angle staircase on the map. One position per matched 1:2 pair.
        p1 = sync_buffer.pop(asset_id, None)
        if p1 is not None:
            meta = sync_buffer.get(str(asset_id) + "_meta", {})

            lat_semi = p1[6] | (p1[7] << 8) | (data[2] << 16) | (data[3] << 24)
            lon_semi = data[4] | (data[5] << 8) | (data[6] << 16) | (data[7] << 24)
            lat = _semi_to_deg(lat_semi)
            lon = _semi_to_deg(lon_semi)

            # 0x80000000 semicircles = ±180° — Garmin's "no GPS fix" sentinel
            if abs(lat) > 179.9 or abs(lon) > 179.9:
                logger.debug("Dog %d: invalid coords %.1f,%.1f — skipping", asset_id, lat, lon)
                return

            on_position({
                "device_id": str(asset_id),
                "name": sync_buffer.get(str(idx) + "_name", "Dog {}".format(asset_id)),
                "lat": lat,
                "lon": lon,
                "situation": meta.get("situation", "Unknown"),
                "distance": meta.get("distance", 0),
                "bearing": meta.get("bearing", 0.0),
                "low_battery": meta.get("low_battery", False),
                "battery_voltage": sync_buffer.get("collar_battery_voltage"),
                "battery_status": sync_buffer.get("collar_battery_status"),
            })

    elif page == 0x10:
        # Asset Identifier page 1: color + first 5 chars of the name set in the Alpha 100
        name_part = bytes(data[3:8]).decode("ascii", errors="ignore").strip("\x00 ")
        sync_buffer[str(idx) + "_name1"] = name_part
        _update_name(idx)

    elif page == 0x11:
        # Asset Identifier page 2: type + last 5 chars of the name
        name_part = bytes(data[3:8]).decode("ascii", errors="ignore").strip("\x00 ")
        sync_buffer[str(idx) + "_name2"] = name_part
        _update_name(idx)

    elif page == 0x52:
        coarse = data[7] & 0x0F
        fractional = data[6] / 256.0
        voltage = round(coarse + fractional, 2)
        status = BATTERY_STATUS.get((data[7] >> 4) & 0x07, "Unknown")
        sync_buffer["collar_battery_voltage"] = voltage
        sync_buffer["collar_battery_status"] = status
        logger.debug("Collar battery: %.2fV (%s)", voltage, status)


def _open_channel(node, device_id, on_position):
    channel = node.new_channel(Channel.Type.BIDIRECTIONAL_RECEIVE)
    channel.on_broadcast_data = lambda data: _on_data(data, on_position, channel)
    channel.on_burst_data = lambda data: _on_data(data, on_position, channel)
    channel.on_close = lambda: logger.warning("ANT+ channel closed")
    # 2048 = 16 Hz, matches the Asset Tracker master's transmit rate. At 8192
    # (4 Hz) we downsampled the stream and phase-locked onto page 0x02, so page
    # 0x01 (latitude low bits) only slipped through ~every 2 min — that starved
    # the 1:2 pairing and froze the latitude. See mikkosh/AntAssetTracker.
    channel.set_period(2048)
    channel.set_rf_freq(57)
    channel.set_id(device_id, 41, 0)
    channel.open()
    logger.info("ANT+ channel open — listening for Alpha 100")
    return channel


def start(device_id: int, on_position, reconnect_delay: int = 5):
    """Listen for Alpha 100 broadcasts and call on_position(dict) on each fix.

    Automatically reconnects if the ANT+ node drops.
    """
    while True:
        node = None
        try:
            node = Node()
            node.set_network_key(0x00, NETWORK_KEY)
            _open_channel(node, device_id, on_position)
            node.start()
            # node.start() returns normally only on clean shutdown
            logger.warning("ANT+ node.start() returned — reconnecting in %ds", reconnect_delay)
        except KeyboardInterrupt:
            logger.info("Shutting down ANT+ listener")
            break
        except Exception as exc:
            logger.error("ANT+ error: %s — reconnecting in %ds", exc, reconnect_delay)
        finally:
            if node is not None:
                try:
                    node.stop()
                except Exception:
                    pass
        time.sleep(reconnect_delay)


def dump():
    """Raw page dump — prints all received pages for protocol analysis."""
    def on_data(data):
        page = data[0]
        if page != 0x03:
            print("P{:02x}: {}".format(page, " ".join("{:02x}".format(b) for b in data)))

    node = Node()
    node.set_network_key(0x00, NETWORK_KEY)
    channel = node.new_channel(Channel.Type.BIDIRECTIONAL_RECEIVE)
    channel.on_broadcast_data = on_data
    channel.set_period(2048)  # 16 Hz — match master rate (see _open_channel)
    channel.set_rf_freq(57)
    channel.set_id(0, 41, 0)

    print("Raw dump — Ctrl-C to stop")
    try:
        channel.open()
        node.start()
    except KeyboardInterrupt:
        pass
    finally:
        channel.close()
        node.stop()
