#!/usr/bin/env python3
import argparse
import subprocess

DUMMY_TOKEN = "000000"


def get_gatt_values(wifi_ssid, wifi_password):
    return {
      "ssid": "00" + wifi_ssid.encode().hex(),
      "wifi_password": "00" + wifi_password.encode().hex(),
      "token": DUMMY_TOKEN.encode().hex(),
      "finish_sequence": "faceaa00"
    }


def provision(bt_mac, wifi_ssid, wifi_password):
    values = get_gatt_values(wifi_ssid, wifi_password)

    # Write SSID
    print(subprocess.run(["gatttool", "-b", bt_mac, "--char-write-req", "--handle=0x002a", "--value={}".format(values["ssid"])]))
    print(subprocess.run(["gatttool", "-b", bt_mac, "--char-read", "--handle=0x0032"]))

    # Write WiFi password
    print(subprocess.run(["gatttool", "-b", bt_mac, "--char-write-req", "--handle=0x002c", "--value={}".format(values["wifi_password"])]))
    print(subprocess.run(["gatttool", "-b", bt_mac, "--char-read", "--handle=0x0032"]))

    # Write dummy token
    print(subprocess.run(["gatttool", "-b", bt_mac, "--char-write-req", "--handle=0x002e", "--value={}".format(values["token"])]))
    print(subprocess.run(["gatttool", "-b", bt_mac, "--char-read", "--handle=0x0032"]))

    # Write end sequence
    print(subprocess.run(["gatttool", "-b", bt_mac, "--char-write-req", "--handle=0x0030", "--value={}".format(values["finish_sequence"])]))
    print(subprocess.run(["gatttool", "-b", bt_mac, "--char-read", "--handle=0x0032"]))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Provisions a Dream Maker fan over Bluetooth.')
    parser.add_argument(
        '--bt-mac', dest='bt_mac', nargs=1, help='The Bluetooth MAC address of the fan. Can be found through "bluetoothctl scan on" command.', required=True
    )
    parser.add_argument(
        '--ssid', dest='ssid', nargs=1, help='Your local WiFi SSID.', required=True,
    )
    parser.add_argument(
        '--password', dest='password', nargs=1, help='Your WiFi password (pre-shared key).', required=True
    )
    args = parser.parse_args()
    provision(args.bt_mac[0], args.ssid[0], args.password[0])
