import argparse
import logging
import os
import time
import yaml
from ant_listener import start as ant_start, dump as ant_dump
from traccar_client import send_position

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def load_config(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "..", "config", "config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dump", action="store_true",
                        help="Raw page dump for protocol analysis, no Traccar forwarding")
    args = parser.parse_args()

    if args.dump:
        print("Raw dump mode — Ctrl-C to stop")
        ant_dump()
        return

    config = load_config()
    traccar_url = config["traccar"]["url"]
    device_id = config["ant"].get("device_id", 0)

    last_state = {}
    last_sent = {}
    # ANT+ delivers ~8 fixes/s; that's far more than Traccar needs and would
    # flood the WAN link. Throttle to one send per device per interval, but
    # never throttle a situation change (Treed/Pointed alarms must fire at once).
    MIN_SEND_INTERVAL = 1.5

    def on_position(data):
        dev = data["device_id"]
        prev = last_state.get(dev, {})
        situation = data["situation"]
        low = data.get("low_battery") or data.get("battery_status") == "Critical"
        changed = prev.get("situation") != situation or (low and not prev.get("low"))

        last_state[dev] = {"situation": situation, "low": low}

        now = time.time()
        if not changed and (now - last_sent.get(dev, 0)) < MIN_SEND_INTERVAL:
            return
        last_sent[dev] = now

        logger.info("Dog '%s' [%s]: %.6f, %.6f  %s  bat=%s",
                    data["name"], data["device_id"],
                    data["lat"], data["lon"], data["situation"],
                    data.get("battery_voltage", "?"))
        extras = {
            "bearing": round(data["bearing"]),
            "altitude": 0,
            # Mättidpunkt så Traccar ordnar spåret på fix-tid, inte ankomsttid.
            # Utan detta ger WAN-retries/omordning kryssande linjer på kartan.
            "timestamp": int(time.time()),
        }
        if data.get("battery_voltage") is not None:
            extras["batt"] = data["battery_voltage"]
        if data.get("battery_status"):
            extras["event"] = "{} dist={}m {}".format(
                data["situation"], data["distance"], data["battery_status"])
        if not data["name"].startswith("Dog "):
            extras["dogName"] = data["name"]

        # Alarm only on state transitions so Traccar notifications fire once,
        # not on every position update
        if situation in ("Treed", "Pointed") and prev.get("situation") != situation:
            extras["alarm"] = situation.lower()
        elif low and not prev.get("low"):
            extras["alarm"] = "lowBattery"

        send_position(traccar_url, data["device_id"], data["lat"], data["lon"], extras)

    logger.info("Starting — device_id=%s", device_id)
    ant_start(device_id, on_position)


if __name__ == "__main__":
    main()
