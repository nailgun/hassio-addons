import re
import time
import json
import logging
import argparse
import threading
import socket
from collections import OrderedDict

import paho.mqtt.client as mqtt

from .device import DahuaDevice


logging.basicConfig(format='%(asctime)s %(levelname)s: %(message)s', level=logging.INFO)
log = logging.getLogger('__name__')


DISCOVERY_PREFIX = 'homeassistant'
NODE_ID = 'dahua_nvr'
SET_RE = re.compile(rf'{DISCOVERY_PREFIX}/switch/{NODE_ID}/cam(\d+)/set')
CAMERA_TOPIC_RE = re.compile(
    rf'{DISCOVERY_PREFIX}/switch/{NODE_ID}/(cam[1-9][0-9]*)/(config|state|availability)'
)
OFF_SUFFIX = '_OFF_'
AVAILABILITY_TOPIC = f'{DISCOVERY_PREFIX}/{NODE_ID}/availability'
DEVICE_INFO = {
    'identifiers': [
        'dahua_nvr',
    ],
    'manufacturer': 'Community',
    'model': 'Dahua NVR',
    'name': 'Dahua NVR',
}

state_changed_event = threading.Event()
device_lock = threading.Lock()
mqtt_lock = threading.Lock()


class DahuaMqttClient(mqtt.Client):
    def __init__(self):
        super().__init__(mqtt.CallbackAPIVersion.VERSION2)
        # Serialize complete NVR snapshots, including command and reconnect paths.
        self.update_lock = threading.RLock()
        self.published_cameras = set()
        self.current_cameras = None
        self.discovery_pending = True


def publish_retained(client, topic, payload):
    with mqtt_lock:
        result = client.publish(topic, payload, qos=1, retain=True)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError(f'Could not publish {topic}: {mqtt.error_string(result.rc)}')


def clear_camera(client, cam_name):
    mqtt_prefix = f'{DISCOVERY_PREFIX}/switch/{NODE_ID}/{cam_name}'
    # Remember partial cleanup so a failed publication is retried on recovery.
    client.published_cameras.add(cam_name)
    for suffix in ('config', 'state', 'availability'):
        publish_retained(client, f'{mqtt_prefix}/{suffix}', '')
    client.published_cameras.discard(cam_name)
    log.info('%s retained topics cleared', cam_name)


def camera_reachable(cam):
    """Check a deliberately disabled camera without changing its NVR credentials."""
    info = cam['DeviceInfo']
    # ONVIF cameras can have a placeholder Dahua service port, while HTTP works.
    ports = []
    for field in ('HttpPort', 'Port', 'HttpsPort', 'RtspPort'):
        try:
            port = int(info.get(field, 0))
        except (TypeError, ValueError):
            continue
        if 0 < port <= 65535 and port not in ports:
            ports.append(port)
    for port in ports:
        try:
            with socket.create_connection((info['Address'], port), timeout=2):
                return True
        except (OSError, KeyError):
            continue
    return False


def read_state(device: DahuaDevice):
    with device_lock:
        cameras = device.secure_request('LogicDeviceManager.secGetCameraAll', None)['params']['camera']
    cameras = filter(lambda c: c['Type'] == 'Remote', cameras)
    cameras_dict = OrderedDict((c['UniqueChannel'], c) for c in cameras if c.get('Enable'))

    with device_lock:
        states = device.request('LogicDeviceManager.getCameraState', {'uniqueChannels': [-1]})['params']['states']
    connected = {s['channel'] for s in states if s['connectionState'] == 'Connected'}

    for cam in cameras_dict.values():
        channel0 = cam['UniqueChannel']
        channel1 = channel0 + 1
        cam['_name'] = f'cam{channel1}'
        # OFF is a deliberate credential change, not a loss of connectivity.
        cam['_state'] = not cam['DeviceInfo']['UserName'].endswith(OFF_SUFFIX)
        cam['_available'] = channel0 in connected or (
            not cam['_state'] and camera_reachable(cam)
        )

    # import pprint
    # pprint.pprint(cameras)
    # pprint.pprint(states)
    # pprint.pprint(cameras_dict)

    return cameras_dict


def publish_config(client: mqtt.Client, state):
    with client.update_lock:
        _publish_config(client, state)


def _publish_config(client, state):
    camera_names = {cam['_name'] for cam in state.values()}
    client.discovery_pending = True
    for cam_name in sorted(client.published_cameras - camera_names):
        clear_camera(client, cam_name)

    for cam in state.values():
        cam_name = cam['_name']
        mqtt_prefix = f'{DISCOVERY_PREFIX}/switch/{NODE_ID}/{cam_name}'

        unique_id = f'{NODE_ID}_{cam_name}'
        ip = cam['DeviceInfo']['Address']

        entity_config = {
            'unique_id': unique_id,
            'name': cam_name.upper(),
            'command_topic': f'{mqtt_prefix}/set',
            'state_topic': f'{mqtt_prefix}/state',
            'availability': [
                {'topic': AVAILABILITY_TOPIC},
                {'topic': f'{mqtt_prefix}/availability'},
            ],
            'availability_mode': 'all',
            'device': dict(DEVICE_INFO),
            'icon': 'mdi:video',
        }

        if ip:
            entity_config['device']['configuration_url'] = f'http://{ip}/'

        client.published_cameras.add(cam_name)
        publish_retained(client, f'{mqtt_prefix}/config', json.dumps(entity_config))
        log.info('%s config published', cam_name)

    client.current_cameras = camera_names
    client.discovery_pending = False


def refresh_config(client, state, force=False):
    camera_names = {cam['_name'] for cam in state.values()}
    if force or client.discovery_pending or camera_names != client.current_cameras:
        publish_config(client, state)


def publish_state(client: mqtt.Client, state):
    for cam in state.values():
        cam_name = cam['_name']
        mqtt_prefix = f'{DISCOVERY_PREFIX}/switch/{NODE_ID}/{cam_name}'

        state_str = 'ON' if cam['_state'] else 'OFF'

        publish_retained(client, f'{mqtt_prefix}/state', state_str)
        publish_retained(client, f'{mqtt_prefix}/availability',
                         'online' if cam['_available'] else 'offline')
        log.info('%s state published', cam_name)


def publish_availability(client, available):
    with client.update_lock:
        if available:
            publish_retained(client, AVAILABILITY_TOPIC, 'online')
        else:
            # Keep the published inventory, but do not clean up from a stale snapshot.
            client.current_cameras = None
            client.discovery_pending = True
            with mqtt_lock:
                client.publish(AVAILABILITY_TOPIC, 'offline', qos=1, retain=True)


def on_connect(client: mqtt.Client, device: DahuaDevice, flags, reason_code, properties):
    with client.update_lock:
        publish_availability(client, False)
        # Also replay our retained topics to find cameras left by a previous process.
        # Subscribe before publishing, including when the initial NVR read fails.
        with mqtt_lock:
            client.subscribe(f'{DISCOVERY_PREFIX}/switch/{NODE_ID}/+/+', qos=1)
        try:
            state = read_state(device)
            publish_config(client, state)
            publish_state(client, state)
            publish_availability(client, True)
        except Exception:
            client.current_cameras = None
            client.discovery_pending = True
            log.exception('Could not read initial NVR state; cameras remain unavailable')


def on_message(client: mqtt.Client, device: DahuaDevice, msg: mqtt.MQTTMessage):
    with client.update_lock:
        match = CAMERA_TOPIC_RE.fullmatch(msg.topic)
        if match:
            cam_name, suffix = match.groups()
            if msg.payload:
                # Paho can replay older QoS 1 publications after on_connect.
                # Their live echoes must not resurrect an already removed camera.
                client.published_cameras.add(cam_name)
                if client.current_cameras is not None and cam_name not in client.current_cameras:
                    clear_camera(client, cam_name)
            elif (suffix == 'config' and client.current_cameras is not None
                  and cam_name in client.current_cameras):
                # An old queued deletion can also arrive after a camera was re-added.
                client.discovery_pending = True
            return
        _on_command(client, device, msg)


def _on_command(client, device, msg):
    match = SET_RE.fullmatch(msg.topic)
    if not match:
        log.error('Got invalid MQTT topic: %s', msg.topic)
        return

    new_state_b = msg.payload
    if new_state_b == b'ON':
        new_state = True
    elif new_state_b == b'OFF':
        new_state = False
    else:
        log.error('Got invalid state: %s', new_state_b)
        return

    channel1 = int(match.group(1))
    channel0 = channel1 - 1

    state = read_state(device)
    refresh_config(client, state)
    cam = state.get(channel0)
    if cam is None:
        log.warning('Ignoring command for unknown channel %s', channel1)
        return
    cam_name = cam['_name']
    if not cam['_available']:
        log.warning('Ignoring command for unavailable camera %s', cam_name)
        publish_state(client, state)
        return

    username = cam['DeviceInfo']['UserName']
    current_state = not username.endswith(OFF_SUFFIX)

    if new_state == current_state:
        publish_state(client, state)
        return

    if new_state:
        username = username.removesuffix(OFF_SUFFIX)
    else:
        username += OFF_SUFFIX
    cam['DeviceInfo']['UserName'] = username

    # unknown field added on `LogicDeviceManager.secSetCamera`
    cam['DeviceInfo']['VideoInputChannels'] = None

    with device_lock:
        device.secure_request('LogicDeviceManager.secSetCamera', {
            'cameras': [{k: v for k, v in cam.items() if not k.startswith('_')}],
        })
    if new_state:
        log.info('%s turned on', cam_name)
    else:
        log.info('%s turned off', cam_name)

    cam['_state'] = new_state
    publish_state(client, state)
    state_changed_event.set()


def on_message_protected(client, device, msg):
    with client.update_lock:
        try:
            return on_message(client, device, msg)
        except Exception:
            publish_availability(client, False)
            log.exception('Got error during message handling')


class UpdateThread(threading.Thread):
    def __init__(self,
                 client: mqtt.Client,
                 device: DahuaDevice,
                 update_interval: float = 60,
                 config_update_every: int = 10):
        super().__init__(daemon=True)
        self.client = client
        self.device = device
        self.update_interval = update_interval
        self.config_update_every = config_update_every

    def run(self):
        counter = 0

        while True:
            if state_changed_event.wait(timeout=self.update_interval):
                state_changed_event.clear()
                log.info('State changed, will wait for 10 seconds and refresh state from NVR')
                # if woken by event, give some time for NVR to connect to new camera
                time.sleep(10)
            else:
                log.info('Refreshing state from NVR by timer')

            counter += 1
            with self.client.update_lock:
                if self.client.is_connected():
                    try:
                        state = read_state(self.device)
                        # Refresh immediately on recovery or inventory changes, and
                        # keep the configured periodic refresh for metadata changes.
                        refresh_config(self.client, state,
                                       force=counter % self.config_update_every == 0)
                        publish_state(self.client, state)
                        publish_availability(self.client, True)
                    except Exception:
                        publish_availability(self.client, False)
                        log.exception('Got error while updating state')
                else:
                    self.client.current_cameras = None
                    self.client.discovery_pending = True


def main():
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument('--mqtt-host', required=True)
    arg_parser.add_argument('--mqtt-port', type=int, default=1883)
    arg_parser.add_argument('--mqtt-username', required=True)
    arg_parser.add_argument('--mqtt-password', required=True)
    arg_parser.add_argument('--dahua-host', required=True)
    arg_parser.add_argument('--dahua-username', required=True)
    arg_parser.add_argument('--dahua-password', required=True)
    arg_parser.add_argument('--update-interval', type=int, default=60)
    arg_parser.add_argument('--config-update-every', type=int, default=10)

    args = arg_parser.parse_args()

    DEVICE_INFO['configuration_url'] = f'http://{args.dahua_host}/'

    log.info('Connecting to Dahua NVR...')
    device = DahuaDevice(args.dahua_host)
    with device_lock:
        device.login(args.dahua_username, args.dahua_password)

    log.info('Connecting to MQTT server...')
    mqttc = DahuaMqttClient()
    mqttc.will_set(AVAILABILITY_TOPIC, 'offline', qos=1, retain=True)
    with mqtt_lock:
        mqttc.user_data_set(device)
        mqttc.on_connect = on_connect
        mqttc.on_message = on_message_protected
        mqttc.username_pw_set(args.mqtt_username, args.mqtt_password)
        mqttc.connect(args.mqtt_host, args.mqtt_port)

    log.info('Starting update thread...')
    update_thread = UpdateThread(mqttc, device, args.update_interval, args.config_update_every)
    update_thread.start()

    log.info('Running...')
    # Blocking call that processes network traffic, dispatches callbacks and
    # handles reconnecting.
    # Other loop*() functions are available that give a threaded interface and a
    # manual interface.
    mqttc.loop_forever()


if __name__ == '__main__':
    main()
