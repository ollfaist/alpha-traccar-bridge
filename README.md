# alpha-traccar-bridge

Bridges Garmin Alpha 100 dog tracking data to a Traccar server via ANT+.

Runs on a Raspberry Pi Zero 2W with a Garmin ANT+ USB dongle. Receives dog GPS positions broadcast by the Alpha 100 handheld unit and forwards them to Traccar using the OsmAnd HTTP protocol.

## Hardware

- Raspberry Pi Zero 2W
- Garmin/Dynastream ANT+ USB stick (USB-A)
- Garmin Alpha 100 handheld (acting as ANT+ transmitter)

## Setup

```bash
bash scripts/install.sh
cp config/config.example.yaml config/config.yaml
# Edit config/config.yaml with your serial port and Traccar URL
```

## Running

```bash
source venv/bin/activate
cd src && python main.py
```

## References

- [AntAssetTracker](https://github.com/mikkosh/AntAssetTracker) — ANT+ Alpha 100 protocol reference
- [Traccar OsmAnd protocol](https://www.traccar.org/osmand/)
