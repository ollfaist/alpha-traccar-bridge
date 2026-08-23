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


# systemd-timesyncd creates this once it has actually synchronised.
_TIMESYNC_MARKER = "/run/systemd/timesync/synchronized"
_clock_ok = False


def _clock_is_trustworthy():
    """Whether the system clock can be used to stamp positions.

    The Pi Zero 2W has no RTC: at boot it restores fake-hwclock's last-shutdown
    time, which can be hours or days in the past. Stamping positions with that
    writes the start of a hunt into the past, where Traccar may reorder or drop
    them. Until the clock is confirmed synced we send no timestamp at all and
    let Traccar stamp on arrival, which is at least monotonic.
    """
    global _clock_ok
    if not _clock_ok and os.path.exists(_TIMESYNC_MARKER):
        _clock_ok = True
        logger.info("System clock synchronised — timestamping positions")
    return _clock_ok


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
        # The dog's own low-battery bit (page 0x01) — the only per-asset battery
        # signal the profile has. Handheld battery (page 0x52) must not feed this:
        # it carries no asset index, so it would alarm on every dog at once.
        low = bool(data.get("low_battery"))

        # Alarm only on the rising edge so Traccar notifies once, not per fix.
        alarm = None
        if situation in ("Treed", "Pointed") and prev.get("situation") != situation:
            alarm = situation.lower()
        elif low and not prev.get("low"):
            alarm = "lowBattery"

        last_state[dev] = {"situation": situation, "low": low}

        # Only an alarm may skip the rate limit. Previously *any* situation
        # change did, so a dog hovering at the Sitting/Moving threshold flipped
        # state every fix and sent at the full ~8 Hz pair rate over mobile data.
        now = time.time()
        if alarm is None and (now - last_sent.get(dev, 0)) < MIN_SEND_INTERVAL:
            return
        last_sent[dev] = now

        logger.info("Dog '%s' [%s]: %.6f, %.6f  %s  dist=%dm%s",
                    data["name"], data["device_id"],
                    data["lat"], data["lon"], data["situation"],
                    data["distance"], "  LOW BAT" if low else "")
        extras = {
            "bearing": round(data["bearing"]),
            "altitude": 0,
            # No "batt": page 0x52 reports the *handheld's* battery, not the
            # collar's, so sending it here labelled every dog with the Alpha's
            # charge. The collar exposes only the low_battery bit, sent as an alarm.
            "event": "{} dist={}m".format(data["situation"], data["distance"]),
        }
        # Decode time, not fix time — pages 0x01/0x02 carry no GPS timestamp.
        # It still keeps the track in order, because a retried position would
        # otherwise be stamped on arrival and land after newer ones. Only sent
        # once the clock is trustworthy; see _clock_is_trustworthy().
        if _clock_is_trustworthy():
            extras["timestamp"] = int(time.time())
        if not data["name"].startswith("Dog "):
            extras["dogName"] = data["name"]
        if alarm:
            extras["alarm"] = alarm

        send_position(traccar_url, data["device_id"], data["lat"], data["lon"], extras)

    logger.info("Starting — device_id=%s", device_id)
    ant_start(device_id, on_position)


if __name__ == "__main__":
    main()
