import argparse
import logging
import yaml
from ant_listener import start as ant_start, dump as ant_dump
from traccar_client import send_position

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def load_config(path="config/config.yaml"):
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

    def on_position(data):
        logger.info("Dog '%s' [%s]: %.6f, %.6f  %s",
                    data["name"], data["device_id"],
                    data["lat"], data["lon"], data["situation"])
        send_position(traccar_url, data["device_id"], data["lat"], data["lon"])

    logger.info("Starting — device_id=%s", device_id)
    ant_start(device_id, on_position)


if __name__ == "__main__":
    main()
