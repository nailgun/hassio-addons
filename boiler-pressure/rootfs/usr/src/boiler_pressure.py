"""Publish the pressure read from an analog boiler gauge."""

import io
import json
import logging
import os
import re
import signal
import threading
import urllib.request
from pathlib import Path

import numpy as np
import paho.mqtt.client as mqtt
from PIL import Image

LOGGER = logging.getLogger(__name__)
STATE_TOPIC = 'boiler_pressure/state'
AVAILABILITY_TOPIC = 'boiler_pressure/availability'
DISCOVERY_TOPIC = 'homeassistant/sensor/boiler_pressure/config'
MAX_FRAME_BYTES = 8 * 1024 * 1024


class Gauge:
    """Measure an angle counterclockwise from the left in image coordinates."""

    def __init__(self, options):
        self.width = options['frame_width']
        self.height = options['frame_height']
        self.one_bar = options['angle_at_one_bar']
        self.two_bars = options['angle_at_two_bars']
        if not 10 <= self.two_bars - self.one_bar <= 180:
            raise ValueError('Calibration angles must span 10 to 180 degrees')
        cx = options['center_x']
        cy = options['center_y']
        if not (29 <= cx < self.width - 29 and 29 <= cy < self.height - 29):
            raise ValueError('Gauge center is too close to the frame edge')

        self.angles = np.arange(self.one_bar - 8, self.two_bars + 8, 0.1)
        radians = np.deg2rad(self.angles[:, None])
        radius = np.arange(10, 26, 0.5)
        dx, dy = -np.cos(radians), -np.sin(radians)
        x, y = cx + radius * dx, cy + radius * dy
        self.centerline = x, y
        self.left = x + 2.5 * dy, y - 2.5 * dx
        self.right = x - 2.5 * dy, y + 2.5 * dx

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

    def pressure(self, frame):
        with Image.open(io.BytesIO(frame)) as source:
            if source.size != (self.width, self.height):
                return None
            gray = np.asarray(source.convert('L'), dtype=np.float32)

        center = self.sample(gray, self.centerline)
        left = self.sample(gray, self.left)
        right = self.sample(gray, self.right)
        score = (np.minimum(left, right) - center).mean(axis=1)
        best = int(score.argmax())
        angle = self.angles[best]
        alternatives = score[np.abs(self.angles - angle) > 8]
        if score[best] < 35 or score[best] - alternatives.max() < 18:
            return None
        if not self.one_bar <= angle <= self.two_bars:
            return None
        return 1 + (angle - self.one_bar) / (self.two_bars - self.one_bar)


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


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    options = json.loads(Path('/data/options.json').read_text())
    gauge = Gauge(options)
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

    client.on_connect = on_connect
    client.connect_async(os.environ['MQTT_HOST'], int(os.environ['MQTT_PORT']))
    client.loop_start()
    try:
        while not stop.is_set():
            if not client.is_connected():
                stop.wait(1)
                continue
            try:
                pressure = gauge.pressure(get_frame(entity, token))
            except (OSError, ValueError) as error:
                LOGGER.warning('Camera frame unavailable: %s', error)
                pressure = None
            if pressure is None:
                LOGGER.warning('No confident in-range pressure reading')
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
