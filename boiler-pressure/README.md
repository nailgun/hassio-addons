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

The detector only reports values in the calibrated interval of 1–2 bar.
An unreadable, missing, or out-of-range image makes the sensor unavailable.
The two decimal places are display resolution; accuracy depends on the camera,
gauge calibration, and lighting. Home Assistant records history for the
discovered `sensor.boiler_pressure` entity.

The diagnostic entities `sensor.boiler_pressure_band` and
`sensor.boiler_pressure_last_reading` support alerts. A confident needle
angle lower than the 1.5 bar position or higher than the 2 bar marker
must repeat on three consecutive captures to publish a low or high
band. A confident out-of-calibration angle still updates the timestamp,
even though the numeric sensor is unavailable. Configure the angular
search bounds (`scan_from` and `scan_to`) to cover the readable scale.
