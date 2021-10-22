# Bluetooth provisioning

After a Dream Maker fan has been reset to factory settings it needs to be provisioned
with the WiFi credentials. This can be achieved by sending these credentials through
Bluetooth GATT commands.

## Details

The following four values need to be written to the device:

* handle `0x002a`: The WiFi SSID
* handle `0x002c`: The WiFi password
* handle `0x002e`: A one-time token which the device uses for requesting an access token from the cloud server (12 bytes)
* handle `0x0030`: The 4-byte value `faceaa00` is written to this handle. Probably tells the fan that the provisioning process is done.

A read request of handle `0x0032` needs to be issued **after every single write request**.
