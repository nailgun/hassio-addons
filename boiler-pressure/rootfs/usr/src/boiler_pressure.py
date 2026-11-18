"""Publish the pressure read from an analog boiler gauge."""

import base64
import binascii
import hashlib
import io
import json
import logging
import os
import re
import signal
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import paho.mqtt.client as mqtt
from PIL import Image

LOGGER = logging.getLogger(__name__)
STATE_TOPIC = 'boiler_pressure/state'
AVAILABILITY_TOPIC = 'boiler_pressure/availability'
DISCOVERY_TOPIC = 'homeassistant/sensor/boiler_pressure/config'
BAND_TOPIC = 'boiler_pressure/band'
LAST_READING_TOPIC = 'boiler_pressure/last_reading'
FRAMING_TOPIC = 'boiler_pressure/camera_moved'
FRAMING_DISCOVERY_TOPIC = 'homeassistant/binary_sensor/boiler_pressure_camera_moved/config'
MAX_FRAME_BYTES = 8 * 1024 * 1024


class Framing:
    radius = 48
    search = 4
    min_contrast = 6
    min_similarity = 0.75

    def __init__(self, day, night, cx, cy, width, height):
        self.x = int(cx)
        self.y = int(cy)
        border = self.radius + self.search
        if not (border <= self.x < width - border and border <= self.y < height - border):
            raise ValueError('Gauge framing reference is too close to the frame edge')
        y, x = np.ogrid[-self.radius:self.radius + 1, -self.radius:self.radius + 1]
        distance = x * x + y * y
        # Exclude the moving needle and compare the gauge rim and its surroundings.
        self.mask = (26 ** 2 <= distance) & (distance <= self.radius ** 2)
        self.day = self.decode(day)
        self.night = self.decode(night)

    def decode(self, encoded):
        size = 2 * self.radius + 1
        try:
            if len(encoded) > 16000:
                raise ValueError('Framing reference exceeds maximum size')
            data = base64.b64decode(encoded, validate=True)
            with Image.open(io.BytesIO(data), formats=('PNG',)) as image:
                if image.size != (size, size):
                    raise ValueError(f'Expected a {size} x {size} reference crop')
                reference = image.convert('L')
        except (binascii.Error, OSError) as error:
            raise ValueError('Invalid base64 PNG framing reference') from error
        center = self.normalized_pixels(np.asarray(reference, dtype=np.float32))
        if center is None:
            raise ValueError('Framing reference needs more contrast')
        rotated = np.stack([
            self.normalized_pixels(np.asarray(
                reference.rotate(angle, resample=Image.Resampling.BILINEAR), dtype=np.float32,
            ), min_contrast=0)
            for angle in (-10, -6, -4, -2, 2, 4, 6, 10)
        ])
        return center, rotated

    def normalized_pixels(self, crop, min_contrast=None):
        if min_contrast is None:
            min_contrast = self.min_contrast
        pixels = crop[self.mask]
        pixels -= pixels.mean()
        norm = np.linalg.norm(pixels)
        if norm <= min_contrast * np.sqrt(pixels.size):
            return None
        return pixels / norm

    def sample(self, gray, dx=0, dy=0):
        x = self.x + dx
        y = self.y + dy
        r = self.radius
        return self.normalized_pixels(gray[y - r:y + r + 1, x - r:x + r + 1])

    def aligned(self, gray, night):
        reference, rotated = self.night if night else self.day
        current = self.sample(gray)
        if current is None:
            return None
        center = current @ reference
        if center < self.min_similarity or np.max(rotated @ current) > center + 0.025:
            return False
        # One pixel of drift is tolerated. Check whether a further shift is
        # clearly better, even when the unshifted score still looks plausible.
        for dy in range(-self.search, self.search + 1):
            for dx in range(-self.search, self.search + 1):
                if max(abs(dx), abs(dy)) <= 1:
                    continue
                shifted = self.sample(gray, dx, dy)
                if shifted is not None and shifted @ reference > center + 0.06:
                    return False
        return True


class Gauge:
    """Measure an angle counterclockwise from the left in image coordinates."""

    def __init__(self, options):
        self.width = options['frame_width']
        self.height = options['frame_height']
        self.one_bar = options['angle_at_one_bar']
        self.two_bars = options['angle_at_two_bars']
        night_one = options.get('night_angle_at_one_bar')
        night_two = options.get('night_angle_at_two_bars')
        if (night_one is None) != (night_two is None):
            raise ValueError('Both night calibration angles are required')
        self.night_calibration = (night_one, night_two) if night_one is not None else None
        cx = options['center_x']
        cy = options['center_y']
        if not (29 <= cx < self.width - 29 and 29 <= cy < self.height - 29):
            raise ValueError('Gauge center is too close to the frame edge')

        lower = options.get('scan_from', -65.0)
        upper = options.get('scan_to', 165.0)
        self.validate_calibration(self.one_bar, self.two_bars, lower, upper)
        if self.night_calibration is not None:
            self.validate_calibration(*self.night_calibration, lower, upper)
        day_ref = options.get('framing_reference_day')
        night_ref = options.get('framing_reference_night')
        if bool(day_ref) != bool(night_ref):
            raise ValueError('Both framing references are required')
        self.framing = Framing(day_ref, night_ref, cx, cy, self.width, self.height) if day_ref else None
        self.gauge_box = (int(cx) - 29, int(cy) - 29, int(cx) + 30, int(cy) + 30)
        self.angles = np.arange(lower, upper + 0.05, 0.1)
        radians = np.deg2rad(self.angles[:, None])
        radius = np.arange(10, 26, 0.5)
        dx, dy = -np.cos(radians), -np.sin(radians)
        x, y = cx + radius * dx, cy + radius * dy
        self.centerline = x, y
        self.left = x + 2.5 * dy, y - 2.5 * dx
        self.right = x - 2.5 * dy, y + 2.5 * dx

    @staticmethod
    def validate_calibration(one_bar, two_bars, lower, upper):
        if not 10 <= two_bars - one_bar <= 180:
            raise ValueError('Calibration angles must span 10 to 180 degrees')
        if not lower + 8 < one_bar < two_bars < upper - 8 or upper - lower > 270:
            raise ValueError('Scan must cover the calibration angles with an 8-degree margin')

    @staticmethod
    def sample(image, coordinates):
        x, y = coordinates
        i, j = x.astype(int), y.astype(int)
        u, v = x - i, y - j
        return (
            (1 - u) * (1 - v) * image[j, i]
            + u * (1 - v) * image[j, i + 1]
            + (1 - u) * v * image[j + 1, i]
            + u * v * image[j + 1, i + 1]
        )

    def read_frame(self, frame, *, enhance=False, check_framing=True):
        with Image.open(io.BytesIO(frame)) as source:
            if source.size != (self.width, self.height):
                return None, None
            gray = np.asarray(source.convert('L'), dtype=np.float32)
            one_bar, two_bars = self.one_bar, self.two_bars
            night = False
            if self.night_calibration is not None or self.framing is not None:
                # The camera encodes its infrared image in monochrome.
                rgb = np.asarray(source.crop(self.gauge_box).convert('RGB'))
                night = bool(np.all(rgb[:, :, :2] == rgb[:, :, 2:]))
            if night and self.night_calibration is not None:
                one_bar, two_bars = self.night_calibration

        aligned = self.framing.aligned(gray, night) if self.framing is not None and check_framing else None
        if self.framing is not None and check_framing and aligned is not True:
            return None, aligned

        if enhance:
            crop = gray[self.gauge_box[1]:self.gauge_box[3], self.gauge_box[0]:self.gauge_box[2]]
            low, high = np.percentile(crop, (10, 90))
            if high - low < 20:
                return None, aligned
            # Keep floating-point samples to avoid clipping the amplified signal.
            gray = gray * min(3.0, max(1.0, 150.0 / (high - low)))

        center = self.sample(gray, self.centerline)
        left = self.sample(gray, self.left)
        right = self.sample(gray, self.right)
        score = (np.minimum(left, right) - center).mean(axis=1)
        best = int(score.argmax())
        angle = self.angles[best]
        alternatives = score[np.abs(self.angles - angle) > 8]
        if score[best] < 35 or score[best] - alternatives.max() < 18:
            return None, aligned
        if angle - self.angles[0] < 4 or self.angles[-1] - angle < 4:
            return None, aligned
        pressure = 1 + (round(float(angle), 10) - one_bar) / (two_bars - one_bar)
        return pressure, aligned

    def read_burst(self, frames):
        if len(frames) != 3 or len({hashlib.sha256(frame).digest() for frame in frames}) != 3:
            return None, None
        pixels = []
        modes = []
        candidates = []
        for frame in frames:
            pressure, aligned = self.read_frame(frame, enhance=True)
            if self.framing is not None and aligned is not True:
                return None, aligned
            with Image.open(io.BytesIO(frame)) as image:
                if image.size != (self.width, self.height):
                    return None, None
                rgb = np.asarray(image.crop(self.gauge_box).convert('RGB'))
                modes.append(bool(np.all(rgb[:, :, :2] == rgb[:, :, 2:])))
                pixels.append(np.asarray(image.convert('RGB'), dtype=np.uint8))
            candidates.append(pressure)
        if len(set(modes)) != 1:
            return None, None

        merged = Image.fromarray(np.median(pixels, axis=0).astype(np.uint8))
        buffer = io.BytesIO()
        merged.save(buffer, format='PNG')
        # Every source frame has already passed the framing guard. Filtering
        # changes texture, so compare the original images with the reference.
        pressure, _ = self.read_frame(buffer.getvalue(), enhance=True, check_framing=False)
        aligned = True if self.framing is not None else None
        if pressure is None:
            return None, aligned
        # Require independent single-frame support for the combined measurement.
        support = sorted(p for p in candidates if p is not None and abs(p - pressure) <= 0.02)
        if not any(b - a <= 0.02 for a, b in zip(support, support[1:])):
            return None, aligned
        return pressure, aligned

    @staticmethod
    def band(pressure):
        if pressure < 1.5:
            return 'low'
        if pressure > 2:
            return 'high'
        return 'normal'


class BandConfirmation:
    # Require three successive confident readings before publishing an alert band.

    def __init__(self):
        self.candidate = None
        self.count = 0

    def update(self, band):
        if band != self.candidate:
            self.candidate = band
            self.count = 0
        self.count = min(3, self.count + 1)
        if band == 'normal' or (band is not None and self.count == 3):
            return band
        return None


def get_frame(camera_entity, token):
    request = urllib.request.Request(
        f'http://supervisor/core/api/camera_proxy/{camera_entity}',
        headers={'Authorization': f'Bearer {token}'},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        frame = response.read(MAX_FRAME_BYTES + 1)
    if len(frame) > MAX_FRAME_BYTES:
        raise ValueError('Camera frame exceeded the maximum size')
    return frame


def discovery(interval):
    return {
        'name': 'Boiler pressure',
        'unique_id': 'boiler_pressure_camera_gauge',
        'default_entity_id': 'sensor.boiler_pressure',
        'device_class': 'pressure',
        'state_class': 'measurement',
        'unit_of_measurement': 'bar',
        'suggested_display_precision': 2,
        'state_topic': STATE_TOPIC,
        'availability_topic': AVAILABILITY_TOPIC,
        'expire_after': max(3 * interval, interval + 30),
    }


def diagnostic_discoveries(interval):
    return {
        'boiler_pressure_band': {
            'name': 'Boiler pressure band',
            'unique_id': 'boiler_pressure_camera_gauge_band',
            'default_entity_id': 'sensor.boiler_pressure_band',
            'device_class': 'enum',
            'options': ['low', 'normal', 'high'],
            'state_topic': BAND_TOPIC,
            'expire_after': max(300, 3 * interval),
            'entity_category': 'diagnostic',
        },
        'boiler_pressure_last_reading': {
            'name': 'Boiler pressure last reading',
            'unique_id': 'boiler_pressure_camera_gauge_last_reading',
            'default_entity_id': 'sensor.boiler_pressure_last_reading',
            'device_class': 'timestamp',
            'state_topic': LAST_READING_TOPIC,
            'entity_category': 'diagnostic',
        },
    }


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    options = json.loads(Path('/data/options.json').read_text())
    gauge = Gauge(options)
    band_confirmation = BandConfirmation()
    framing_confirmation = BandConfirmation()
    entity = options['camera_entity']
    if not re.fullmatch(r'camera\.[a-z0-9_]+', entity):
        raise ValueError('camera_entity must be a Home Assistant camera entity ID')
    token = os.environ['SUPERVISOR_TOKEN']
    interval = options['interval']
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='boiler-pressure')
    client.username_pw_set(os.environ['MQTT_USERNAME'], os.environ['MQTT_PASSWORD'])
    client.will_set(AVAILABILITY_TOPIC, 'offline', qos=1, retain=True)

    def on_connect(client, _userdata, _flags, reason, _properties):
        if reason.is_failure:
            LOGGER.warning('MQTT connection refused: %s', reason)
            return
        client.publish(AVAILABILITY_TOPIC, 'offline', qos=1, retain=True)
        client.publish(DISCOVERY_TOPIC, json.dumps(discovery(interval)), qos=1, retain=True)
        framing_config = {
            'name': 'Boiler pressure camera moved',
            'unique_id': 'boiler_pressure_camera_moved',
            'default_entity_id': 'binary_sensor.boiler_pressure_camera_moved',
            'state_topic': FRAMING_TOPIC,
            'device_class': 'problem',
            'entity_category': 'diagnostic',
            'expire_after': max(3 * interval, interval + 30),
        }
        client.publish(
            FRAMING_DISCOVERY_TOPIC, json.dumps(framing_config) if gauge.framing else '',
            qos=1, retain=True,
        )
        for key, config in diagnostic_discoveries(interval).items():
            client.publish(f'homeassistant/sensor/{key}/config', json.dumps(config), qos=1, retain=True)

    client.on_connect = on_connect
    client.connect_async(os.environ['MQTT_HOST'], int(os.environ['MQTT_PORT']))
    client.loop_start()
    try:
        while not stop.is_set():
            if not client.is_connected():
                band_confirmation.update(None)
                framing_confirmation.update(None)
                stop.wait(1)
                continue
            try:
                captured_at = time.monotonic()
                frame = get_frame(entity, token)
                pressure, aligned = gauge.read_frame(frame)
                if pressure is None and aligned is True:
                    frames = [frame]
                    for _ in range(2):
                        if stop.wait(2):
                            break
                        frames.append(get_frame(entity, token))
                    if len(frames) == 3:
                        # Prefer the newest direct measurement when it is readable.
                        pressure, aligned = gauge.read_frame(frames[-1])
                        if pressure is None and aligned is True:
                            pressure, aligned = gauge.read_burst(frames)
                            if pressure is not None:
                                LOGGER.info('Twilight reading confirmed by three-frame burst')
                    if time.monotonic() - captured_at > 20:
                        pressure, aligned = None, None
            except (OSError, ValueError) as error:
                LOGGER.warning('Camera frame unavailable: %s', error)
                pressure, aligned = None, None

            framing_band = {True: 'normal', False: 'moved', None: None}[aligned]
            framing_state = framing_confirmation.update(framing_band)
            if framing_state is not None:
                client.publish(FRAMING_TOPIC, 'ON' if framing_state == 'moved' else 'OFF', qos=1)
            if aligned is False:
                LOGGER.warning('Camera framing does not match its reference')

            band = band_confirmation.update(None if pressure is None else gauge.band(pressure))
            if pressure is not None:
                timestamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
                client.publish(LAST_READING_TOPIC, timestamp, qos=1, retain=True)
            if band is not None:
                client.publish(BAND_TOPIC, band, qos=0)
            if pressure is None:
                LOGGER.warning('No confident gauge reading')
                client.publish(AVAILABILITY_TOPIC, 'offline', qos=1, retain=True)
            else:
                payload = f'{pressure:.2f}'
                client.publish(STATE_TOPIC, payload, qos=0)
                client.publish(AVAILABILITY_TOPIC, 'online', qos=1, retain=True)
                LOGGER.info('Boiler pressure: %s bar', payload)
            stop.wait(interval)
    finally:
        if client.is_connected():
            offline = client.publish(AVAILABILITY_TOPIC, 'offline', qos=1, retain=True)
            offline.wait_for_publish(timeout=2)
        client.disconnect()
        client.loop_stop()


if __name__ == '__main__':
    main()
