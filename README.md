# dmiot2mqtt

This project allows controlling various Dream Maker fans through MQTT. The implementation is rather simple and just bridges between MQTT and the Dream Maker JSON protocol. The fan must be disconnected from the cloud for this.

Supported fans:

* Dream Maker Smart Fan DM-FAN01
* Dream Maker Smart Fan DM-FAN02-W (battery-powered version)

## How it works

dmiot2mqtt will provide the same interface (with a subset of features) that the official Dream Maker cloud server offers. This way the fans will "think" that they are talking to their usual Chinese cloud server. No software modifications on the fan are required.

For this to work you need to have control over your local DNS. You need to alter the DNS record for cloud1.dm-maker.com to return the IP address of the machine running dmiot2mqtt.

## Prerequisites

This is what you need in order to get started:

* Control over your local DNS (e.g. through your router or through Pi Hole)
* Python 3.8 (or later)  with amqtt
* A MQTT broker
* Ideally an always-on machine which will run dmiot2mqtt (e.g. a Raspberry Pi)
* Recommended: Any Linux machine with a Bluetooth 4.0+ dongle for provisioning the fan without the Dream Maker app (one-time only)
