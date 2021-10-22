# dmiot2mqtt

This project allows controlling various Dream Maker fans through MQTT. The implementation is rather simple and just bridges between MQTT and the Dream Maker JSON protocol. The fan must be disconnected from the cloud for this.

Supported fans:

* Dream Maker Smart Fan DM-FAN01
* Dream Maker Smart Fan DM-FAN02-W (battery-powered version)

Warning: This is just a proof-of-concept which has been created within a few hours. Don't expect anything fancy: neither in functionality nor in style! :smile:

## How it works

dmiot2mqtt will provide the same interface (with a subset of features) that the official Dream Maker cloud server offers. This way the fans will "think" that they are talking to their usual Chinese control servers. No software modifications on the fan are required.

For this to work you need to have control over your local DNS. You need to alter the DNS record for cloud1.dm-maker.com to return the IP address of the machine running dmiot2mqtt.

## Prerequisites

This is what you need in order to get started:

* Control over your local DNS (e.g. through your router or through Pi Hole)
* Python 3.8 (or later) with amqtt
* A MQTT broker
* Ideally an always-on machine which will run dmiot2mqtt (e.g. a Raspberry Pi)
* A static IP adress for your Dream Maker Smart Fan (IP will be used in MQTT topic)
* Recommended: Any Linux machine with a Bluetooth 4.0+ dongle for provisioning the fan without the Dream Maker app (one-time only)

## How to get this running?

1. Pick a machine which will host `dmiot2mqtt.py`. It's recommended to create a new Python venv with amqtt installed within that environment.
2. Create a DNS record which points `cloud1.dm-maker.com` to the machine running `dmiot2mqtt.py`.
3. Adjust the config file `dmiot2mqtt.ini` according to your needs.
4. Run the service through `python3 dmiot2mqtt.py -c /path/to/dmiot2mqtt.ini`.
5. If your fan is still unprovisioned, use the `provision.py` script from this repository to push your WiFi information to the fan.
6. Wait until your fan connects to the service.
7. Create a systemd unit file for dmiot2mqtt.py according to your needs.

## Home Assistant integration

Once you have your fan talking to dmiot2mqtt you can integrate it into Home Assistant.
You can use this config as an example, just adjust the MQTT topic names according to your fan's IP address:


```yaml
fan:
  - platform: mqtt
    command_topic: "dmiot2mqtt/192.168.100.5/command"
    command_template: '{ "power": {{ value }} }'
    oscillation_command_topic: "dmiot2mqtt/192.168.100.5/command"
    oscillation_command_template: '{ "roll_enable": {{ value }}}'
    oscillation_state_topic: "dmiot2mqtt/192.168.100.5"
    oscillation_value_template: "{{ value_json.roll_enable }}"
    state_topic: "dmiot2mqtt/192.168.100.5"
    state_value_template: "{{ value_json.power }}"
    payload_on: "1"
    payload_off: "0"
    payload_oscillation_on: "1"
    payload_oscillation_off: "0"
    percentage_command_topic: "dmiot2mqtt/192.168.100.5/command"
    percentage_command_template: '{ "speed": {{value}} }'
    percentage_state_topic: "dmiot2mqtt/192.168.100.5"
    percentage_value_template: "{{ value_json.speed }}"
    json_attributes_topic: "dmiot2mqtt/192.168.100.5"
    preset_modes:
      - direct
      - natural
      - smart
    preset_mode_command_template: '{% if value == "natural" %}{"mode":1}{% elif value == "smart" %}{"mode":2}{% else %}{ "mode":0}{% endif %}'
    preset_mode_command_topic: "dmiot2mqtt/192.168.100.5/command"
    preset_mode_state_topic: "dmiot2mqtt/192.168.100.5"
    preset_mode_value_template: '{% if value_json.mode == 1 %}natural{% elif value_json.mode == 2 %}smart{% else %}direct{% endif %}'
    name: "DreamMaker Fan"
    unique_id: "mqtt_fan_dmfan01"
```
