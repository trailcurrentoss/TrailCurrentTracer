repo: trailcurrentoss/TrailCurrentHeadwaters
branch: main

## Last sync
date: 2026-09-01T00:10:00Z

### Updated in this project
- Built the PocketTerm35 field-debugger OS mock (boot splash, WiFi/MQTT provisioning wizard, two launcher directions, MQTT Inspector, Device Discovery).
- MQTT topic tree and payload shapes taken from the backend CAN bridge and cloud bridge topic maps.
- Device Discovery cards follow the real `_trailcurrent._tcp` TXT record fields (type, fw, addr, canid, deviceId, onboard confirm/claim).

## Screen map
| Screen | Repo files |
| --- | --- |
| MQTT Inspector | containers/backend/src/services/can-bridge.js, containers/backend/src/services/cloud-bridge.js, local_code/can-to-mqtt.py |
| Device Discovery | local_code/discovery-mdns.py |
| WiFi / MQTT wizard | local_code/provision_wifi_mqtt.py, config/mosquitto/mosquitto.conf |
| Boot splash | CM5/image/generate-splash.sh |
