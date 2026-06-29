"""
Main daemon: listens for Alpha 100 dog positions and forwards to Traccar.
"""

import argparse
import time
import yaml
import logging
import signal
import sys
from ant_listener import start as start_ant, scan as ant_scan
from traccar_client import send_position

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def load_config(path="config/config.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan", action="store_true", help="Scan for nearby ANT+ devices")
    args = parser.parse_args()

    if args.scan:
        ant_scan()
        return

    config = load_config()
    traccar_url = config["traccar"]["url"]
    serial_port = config["ant"]["serial_port"]
    device_id = config["ant"].get("device_id", 0)

    def on_position(data):
        logger.info("Dog '%s' [%s]: %.6f, %.6f  %s",
                    data["name"], data["device_id"], data["lat"], data["lon"], data["situation"])
        send_position(traccar_url, data["device_id"], data["lat"], data["lon"])

    logger.info("Starting ANT+ listener on %s (device_id=%s)", serial_port, device_id)
    start_ant(serial_port, device_id, on_position)


if __name__ == "__main__":
    main()
