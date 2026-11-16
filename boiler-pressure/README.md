# Boiler Pressure

Reads a fixed analog pressure gauge from a Home Assistant camera and publishes
the result to MQTT Discovery. The camera integration provides the image; the
add-on uses the Supervisor token and MQTT service without a camera password.

Set `camera_entity` to the camera's entity ID. Calibration uses the frame
dimensions, the gauge center in source pixels, and the 1 and 2 bar marker
angles. Angles are measured upwards from a ray pointing left. The defaults
expect a fixed 1920 × 1080 view with the gauge centered near (1259, 209).
Mount the camera rigidly and verify the calibration against the gauge before
relying on the result. Moving the camera requires recalibration.

Set both optional angles, `night_angle_at_one_bar` and
`night_angle_at_two_bars`, to use separate calibration for monochrome frames.
Color frames and installations without these settings use the regular angles.

Confident readings outside 1–2 bar are linearly extrapolated and published.
A missing frame or an uncertain needle position makes the sensor unavailable.
The two decimal places are display resolution; accuracy depends on the camera,
gauge calibration, and lighting. Home Assistant records history for the
discovered `sensor.boiler_pressure` entity.

The diagnostic entities `sensor.boiler_pressure_band` and
`sensor.boiler_pressure_last_reading` support alerts. A calibrated reading below 1.5 bar or above 2 bar must repeat on three
consecutive captures to publish a low or high band. Each confident reading
updates the timestamp. Configure the angular
search bounds (`scan_from` and `scan_to`) to cover the readable scale.

## Camera framing guard

Capture a full-resolution color frame and a full-resolution infrared frame
while the camera is aligned. Run the following command for each frame with the
same center coordinates as the add-on configuration (requires Pillow):

```shell
python3 generate_framing_reference.py aligned-frame.jpg 1259.2 209.2
```

Put the two outputs in `framing_reference_day` and `framing_reference_night`
and restart the add-on. The guard is optional and requires both references
when enabled. An unverified frame does not produce a pressure reading.

A mismatch on three consecutive captures turns on
`binary_sensor.boiler_pressure_camera_moved`. Use an automation that triggers on
this sensor turning on to notify users. The first matching frame clears the
problem. Inadequate contrast or a missing frame breaks the mismatch streak and
is handled by the existing reading freshness check.
