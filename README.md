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
* Python 3.9 (or later) with aiomqtt (Version 2.2 or later)
* A MQTT broker
* Ideally an always-on machine which will run dmiot2mqtt (e.g. a Raspberry Pi)
* A static IP adress for your Dream Maker Smart Fan (IP will be used in MQTT topic), unless you use `topic_id = device_id` (see below)
* Recommended: Any Linux machine with a Bluetooth 4.0+ dongle for provisioning the fan without the Dream Maker app (one-time only)

## How to get this running?

1. Pick a machine which will host `dmiot2mqtt.py`. It's recommended to create a new Python venv and run `pip3 install -r requirements.txt` inside.
2. Create a DNS record which points `cloud1.dm-maker.com` to the machine running `dmiot2mqtt.py`.
3. Adjust the config file `dmiot2mqtt.ini` according to your needs.
4. Run the service through `python3 dmiot2mqtt.py -c /path/to/dmiot2mqtt.ini`.
5. If your fan is still unprovisioned, use the `provision.py` script from this repository to push your WiFi information to the fan.
6. Wait until your fan connects to the service.
7. Create a systemd unit file for dmiot2mqtt.py according to your needs.

## Configuration

Options in `dmiot2mqtt.ini`:

section | option | default | explanation
--------|--------|---------|------------
`mqtt` | `server` | `localhost` | MQTT broker hostname
`mqtt` | `port` | `1883` | MQTT broker port. With `use_ssl = 1` this is usually `8883`
`mqtt` | `base_topic` | `dmiot2mqtt` | Prefix of all topics
`mqtt` | `user`, `password` | none | Broker credentials
`mqtt` | `use_ssl` | `0` | Connect to the broker over TLS, verifying its certificate against the system CAs
`mqtt` | `retain` | `0` | Publish the fan state as retained message, so Home Assistant knows it right after a restart
`mqtt` | `topic_id` | `ip` | Identify a fan in the topic by `ip` or by `device_id`. See the note below
`bridge` | `read_timeout` | `90` | Seconds without any data before a fan connection is treated as dead, `0` or less disables it. The 10-second heartbeat in doc/protocol.md is not verified for every model: if a fan (e.g. the battery-powered DM-FAN02-W) is dropped every `read_timeout` seconds, raise the value

With `topic_id = device_id` the fan does not need a static IP. This only works for fans that were provisioned through the official Dream Maker app and cloud: they keep the unique device id they got there. Fans provisioned through `provision.py` and this bridge all get the same all-zero device id; for those the bridge falls back to the IP address and logs a warning.

## MQTT topics

Topics per fan (`<id>` is the fan's IP address, or its device id with `topic_id = device_id`):

* `dmiot2mqtt/<id>`: full fan state as JSON
* `dmiot2mqtt/<id>/command`: commands, one JSON object per message, e.g. `{"power": 1}`
* `dmiot2mqtt/<id>/availability`: `online` / `offline`, retained, also set as MQTT last will

The bridge keeps the fan connection open while the broker is unavailable and reconnects after 5 seconds, doubling the delay up to 60 seconds.
The last state received in the meantime is published after the reconnect.

Each fan gets a fixed MQTT client id, `dmiot2mqtt-` followed by its topic with `/` replaced by `-`.
Two bridge instances connected to the same broker therefore need different `base_topic` values.

When a fan reconnects (e.g. after losing WiFi) while its old connection is still open, the old connection is closed.
Two different fans behind one IP address (NAT) share a topic unless `topic_id = device_id` is used. If they report different device ids, the bridge keeps both connections and logs an error, but with `topic_id = ip` commands then reach both fans; use `topic_id = device_id` or give them different IP addresses. Fans provisioned through `provision.py` and this bridge all report the same all-zero device id and cannot be told apart at all, so behind one IP address they replace each other's connection over and over; they need different IP addresses.
A connection without any data for `read_timeout` seconds is treated as dead; the fans send a heartbeat every 10 seconds.

## Home Assistant integration

Once you have your fan talking to dmiot2mqtt you can integrate it into Home Assistant.
You can use this config as an example, just adjust the MQTT topic names according to your fan's IP address:


```yaml
mqtt:
  fan:
    - name: "DreamMaker Fan"
      unique_id: "mqtt_fan_dmfan01"
      availability_topic: "dmiot2mqtt/192.168.100.5/availability"
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

  select:
    - command_topic: "dmiot2mqtt/192.168.100.5/command"
      command_template: '{ "roll_angle": {{value|int}} }'
      name: "DreamMaker Fan Angle"
      options:
        - "30"
        - "60"
        - "90"
        - "120"
        - "140"
      state_topic: "dmiot2mqtt/192.168.100.5"
      value_template: "{{ value_json.roll_angle }}"
      unique_id: mqtt_fan_dmfan01_angle
```
