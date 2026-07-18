# alpha-traccar-bridge

Bridges Garmin Alpha 100 dog tracking data to a Traccar server via ANT+.

Runs on a Raspberry Pi Zero 2W with a Garmin ANT+ USB dongle. Receives dog GPS positions broadcast by the Alpha 100 handheld unit and forwards them to Traccar using the OsmAnd HTTP protocol.

## How it works

The Alpha 100 system uses two separate radio links:

- **320 MHz** — proprietary long-range link between the handheld and the dog collar
- **ANT+ (2.4 GHz)** — the handheld re-broadcasts assembled dog position data on this channel

This project listens on the ANT+ channel. The 320 MHz link is handled entirely by the handheld — our setup only needs to be in ANT+ range of the handheld unit, not the collar.

The ANT+ Asset Tracker profile (device type 41) is used. The network key and period were determined by protocol analysis of the handheld's broadcast.

## Hardware

- Raspberry Pi Zero 2W
- Garmin/Dynastream ANT+ USB stick (USB-A)
- Garmin Alpha 100 handheld (acting as ANT+ transmitter)

## Setup

```bash
bash scripts/install.sh
cp config/config.example.yaml config/config.yaml
# Edit config/config.yaml with your Traccar URL
```

## Running

```bash
source venv/bin/activate
python src/main.py
```

To verify the ANT+ data before enabling Traccar forwarding:

```bash
python src/main.py --dump
```

This prints raw page data from the handheld — useful for verifying the connection and debugging page layouts.

## Traccar setup

Enable the OsmAnd port in your Traccar config (`traccar.xml`):

```xml
<entry key='osmand.port'>5055</entry>
```

Each dog collar appears as a separate device in Traccar, identified by its asset index from the Alpha 100. Collar battery voltage (from ANT+ page 0x52) is forwarded as the `batt` attribute.

## Map (separate project)

The hunting map web app (Lantmäteriet tile proxy, live positions, property
boundaries, geofences) lives in its own repository: **jaktkarta**. It talks to
the same Traccar server but is developed and deployed independently.

## References

- [AntAssetTracker](https://github.com/mikkosh/AntAssetTracker) — ANT+ Asset Tracker protocol reference (ConnectIQ)
- [openant](https://github.com/Tigge/openant) — ANT+ Python library
- [Traccar OsmAnd protocol](https://www.traccar.org/osmand/)
