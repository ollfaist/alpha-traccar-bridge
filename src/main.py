import argparse
import logging
import os
import time
import yaml
from ant_listener import start as ant_start, dump as ant_dump
from traccar_client import send_position, hund_id

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

    # Admin-API:et (för att auto-registrera nya halsband) är separat från
    # OsmAnd-adressen ovan och kräver inloggning.
    admin_cfg = config["traccar"].get("admin") or {}
    # Flera adresser tillåts och provas i tur och ordning. Bryggan flyttar
    # mellan hemmanätet och en delad uppkoppling i skogen: LAN-adressen är
    # snabbast hemma men finns inte alls på väg, och en enda adress gjorde att
    # registreringen tyst misslyckades varje gång bryggan var borta.
    admin_urls = admin_cfg.get("urls") or ([admin_cfg["url"]] if admin_cfg.get("url") else [])
    admin = None
    if admin_urls and admin_cfg.get("user") and admin_cfg.get("password"):
        admin = {"urls": list(admin_urls),
                 "auth": (admin_cfg["user"], admin_cfg["password"])}
        logger.info("Admin-API för registrering: %s", ", ".join(admin_urls))
    else:
        logger.warning("Ingen traccar.admin i config.yaml — nya halsband måste "
                        "fortfarande läggas till manuellt i Traccar.")

    last_state = {}
    last_sent = {}
    # När vi först såg en slot utan att ha fått namnet. Identifikationssidorna
    # dröjer några sekunder efter att hunden dykt upp — under tiden skickar vi
    # inget, hellre det än en enhet som heter "Dog 98".
    first_seen = {}
    warned_namnlos = set()
    NAME_GRACE = 30.0
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

        # Id:t härleds ur namnet Alphan gett hunden — även "Hundar 2" duger,
        # den blir bara en enhet som heter så. hund_id() ger None bara medan
        # bryggan ännu inte hört namnet; då väntar vi en kort stund hellre än
        # att skapa "Dog 98" i Traccar.
        slot = data["device_id"]
        unique_id = hund_id(data["name"])
        if unique_id is None:
            forst = first_seen.setdefault(slot, now)
            if (now - forst) < NAME_GRACE:
                return
            # Halsbandet skickar aldrig sitt namn. Sällsynt — men då får det
            # ett id på platsnumret så det åtminstone syns på kartan.
            unique_id = "hund-namnlos-" + slot
            if slot not in warned_namnlos:
                warned_namnlos.add(slot)
                logger.warning("Halsbandet på plats %s skickar inget namn ens "
                               "efter %d s — visas som '%s'. Kontrollera att "
                               "hunden finns med i handenhetens lista.",
                               slot, int(NAME_GRACE), unique_id)
        dog_name = data["name"] if unique_id != "hund-namnlos-" + slot else unique_id

        logger.info("Hund '%s' [%s -> %s]: %.6f, %.6f  %s  dist=%dm%s",
                    dog_name, slot, unique_id,
                    data["lat"], data["lon"], data["situation"],
                    data["distance"], "  LÅGT BATT" if low else "")
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
        extras["dogName"] = dog_name
        if alarm:
            extras["alarm"] = alarm

        send_position(traccar_url, unique_id, data["lat"], data["lon"], extras, admin=admin)

    logger.info("Starting — device_id=%s", device_id)
    ant_start(device_id, on_position)


if __name__ == "__main__":
    main()
