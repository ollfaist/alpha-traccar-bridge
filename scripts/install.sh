#!/bin/bash
# Setup script for Raspberry Pi Zero 2W
set -e

sudo apt-get update
sudo apt-get install -y python3-pip python3-venv libusb-1.0-0

# udev rule for ANT+ USB dongle (Garmin/Dynastream vendor)
echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="0fcf", MODE="0666"' | sudo tee /etc/udev/rules.d/99-ant.rules
sudo udevadm control --reload-rules

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

echo "Done. Copy config/config.example.yaml to config/config.yaml and edit it."
