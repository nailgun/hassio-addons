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
connection and polling recover.

Removed cameras and cameras with NVR `Enable=False` have their retained
discovery, state and availability topics cleared on the next successful poll.
This does not remove deliberately OFF or temporarily disconnected cameras.
Discovery is republished on the first successful poll after an NVR failure,
before bridge availability becomes online; unchanged discovery still refreshes
at the configured interval.

The published-camera inventory survives MQTT reconnects within the running
add-on, but is not persisted across add-on restarts. Retained orphan topics
from before this process started require manual cleanup.

Run the availability tests with the dependencies from `rootfs/usr/src/requirements.txt`:

```sh
python -m unittest discover -s dahua-nvr/tests -v
```

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
