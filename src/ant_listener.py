"""
ANT+ listener for Garmin Alpha 100 dog tracking data.

Protocol verified against live Alpha 100 hardware.
Network key and byte layout confirmed via raw capture session.
"""

import logging
from openant.easy.node import Node
from openant.easy.channel import Channel

logger = logging.getLogger(__name__)

NETWORK_KEY = [***REMOVED-ANT-NETWORK-KEY***]

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


def _on_data(data, on_position):
    page = data[0]
    asset_id = int(data[1])

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
        if asset_id in sync_buffer:
            p1 = sync_buffer[asset_id]
            meta = sync_buffer.get(str(asset_id) + "_meta", {})

            lat_semi = p1[6] | (p1[7] << 8) | (data[2] << 16) | (data[3] << 24)
            lon_semi = data[4] | (data[5] << 8) | (data[6] << 16) | (data[7] << 24)
            lat = _semi_to_deg(lat_semi)
            lon = _semi_to_deg(lon_semi)

            on_position({
                "device_id": str(asset_id),
                "name": "Dog {}".format(asset_id),
                "lat": lat,
                "lon": lon,
                "situation": meta.get("situation", "Unknown"),
                "distance": meta.get("distance", 0),
                "bearing": meta.get("bearing", 0.0),
                "low_battery": meta.get("low_battery", False),
                "battery_voltage": sync_buffer.get("collar_battery_voltage"),
                "battery_status": sync_buffer.get("collar_battery_status"),
            })

    elif page == 0x52:
        coarse = data[7] & 0x0F
        fractional = data[6] / 256.0
        voltage = round(coarse + fractional, 2)
        status = BATTERY_STATUS.get((data[7] >> 4) & 0x07, "Unknown")
        sync_buffer["collar_battery_voltage"] = voltage
        sync_buffer["collar_battery_status"] = status
        logger.debug("Collar battery: %.2fV (%s)", voltage, status)


def start(device_id: int, on_position):
    """Listen for Alpha 100 broadcasts and call on_position(dict) on each fix."""
    node = Node()
    node.set_network_key(0x00, NETWORK_KEY)
    channel = node.new_channel(Channel.Type.BIDIRECTIONAL_RECEIVE)
    channel.on_broadcast_data = lambda data: _on_data(data, on_position)
    channel.set_period(8192)
    channel.set_rf_freq(57)
    channel.set_id(device_id, 41, 0)

    logger.info("ANT+ channel open — listening for Alpha 100")
    try:
        channel.open()
        node.start()
    except KeyboardInterrupt:
        pass
    finally:
        channel.close()
        node.stop()


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
    channel.set_period(8192)
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
