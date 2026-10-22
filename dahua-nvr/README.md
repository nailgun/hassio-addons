# Home Assistant Add-on: Dahua NVR

_Control cameras connected to Dahua NVR._

Camera switches distinguish a deliberate **OFF** from an unavailable camera.
If an enabled camera loses its NVR connection, its Home Assistant switch becomes
unavailable and cannot be toggled. An unavailable camera also ignores MQTT commands.

The add-on deliberately disables cameras by appending `_OFF_` to their NVR
username. These cameras remain available to turn back on while any of their
configured HTTP, Dahua, HTTPS or RTSP ports is reachable. This is a TCP reachability
check, not an authentication or video-stream check. Disconnected cameras recover
automatically on the next poll (60 seconds by default).

MQTT discovery, state and availability are retained. Loss of the add-on's MQTT
connection or a failed NVR poll makes all camera switches unavailable until the
connection and polling recover. The first successful poll after a failure
refreshes discovery and state before marking the bridge online, without waiting
for the periodic configuration refresh.

Cameras removed from the NVR or disabled with its `Enable` setting have their
retained discovery, state and availability topics cleared on the next successful
poll. A camera turned **OFF** by this add-on, or temporarily disconnected, keeps
its discovery configuration. On MQTT reconnect, the add-on also reads its own
retained camera topics to clean up obsolete entries from previous runs. Cleanup
waits for a successful NVR read; a failed read never counts as an empty inventory.

Run the availability and discovery regression tests from the repository root:

```sh
python -m pip install -r dahua-nvr/rootfs/usr/src/requirements.txt
python -m unittest discover -s dahua-nvr/tests -v
```

The tests use simulated NVR responses and MQTT publications; they do not connect
to a broker or change real cameras.

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]
![Supports armhf Architecture][armhf-shield]
![Supports armv7 Architecture][armv7-shield]
![Supports i386 Architecture][i386-shield]

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
[armhf-shield]: https://img.shields.io/badge/armhf-yes-green.svg
[armv7-shield]: https://img.shields.io/badge/armv7-yes-green.svg
[i386-shield]: https://img.shields.io/badge/i386-yes-green.svg
