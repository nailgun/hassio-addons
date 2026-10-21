import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'rootfs/usr/src'))
from dahua_nvr import __main__ as app


def camera(channel, disabled=False):
    return {
        'Type': 'Remote', 'Enable': True, 'UniqueChannel': channel,
        'DeviceInfo': {'Address': '192.0.2.1', 'Port': 37777,
                       'UserName': 'admin_OFF_' if disabled else 'admin'},
    }


def device(cameras, states):
    result = Mock()
    result.secure_request.return_value = {'params': {'camera': copy.deepcopy(cameras)}}
    result.request.return_value = {'params': {'states': states}}
    return result


class AvailabilityTests(unittest.TestCase):
    def tearDown(self):
        app.state_changed_event.clear()

    def test_connected_disabled_and_missing_cameras_are_distinct(self):
        nvr = device([camera(0), camera(1, True), camera(2)], [
            {'channel': 0, 'connectionState': 'Connected'},
            {'channel': 1, 'connectionState': 'Unconnect'},
            # Missing state must also make a camera unavailable.
        ])
        with patch.object(app, 'camera_reachable', return_value=True) as probe:
            state = app.read_state(nvr)
        self.assertEqual([(c['_state'], c['_available']) for c in state.values()],
                         [(True, True), (False, True), (True, False)])
        probe.assert_called_once()

    def test_disabled_camera_without_network_is_unavailable(self):
        nvr = device([camera(0, True)], [])
        with patch.object(app.socket, 'create_connection', side_effect=TimeoutError):
            state = app.read_state(nvr)
        self.assertFalse(state[0]['_state'])
        self.assertFalse(state[0]['_available'])

    def test_onvif_disabled_camera_uses_its_http_port(self):
        cam = camera(0, True)
        cam['DeviceInfo']['HttpPort'] = 80
        with patch.object(app.socket, 'create_connection') as connect:
            self.assertTrue(app.camera_reachable(cam))
        connect.assert_called_once_with(('192.0.2.1', 80), timeout=2)

    def test_reachability_falls_back_to_other_configured_ports(self):
        cam = camera(0, True)
        cam['DeviceInfo']['HttpPort'] = 80
        with patch.object(app.socket, 'create_connection',
                          side_effect=[ConnectionRefusedError(), MagicMock()]) as connect:
            self.assertTrue(app.camera_reachable(cam))
        self.assertEqual([c.args[0][1] for c in connect.call_args_list], [80, 37777])

    def test_discovery_and_states_publish_retained_availability(self):
        client = Mock()
        cam = camera(0)
        cam.update(_name='cam1', _state=True, _available=False)
        app.publish_config(client, {0: cam})
        config = json.loads(client.publish.call_args.args[1])
        self.assertEqual(config['availability_mode'], 'all')
        self.assertEqual(len(config['availability']), 2)
        self.assertTrue(client.publish.call_args.kwargs['retain'])
        app.publish_state(client, {0: cam})
        self.assertEqual(client.publish.call_args.args[1], 'offline')
        self.assertTrue(client.publish.call_args.kwargs['retain'])
        cam['_available'] = True
        app.publish_state(client, {0: cam})
        self.assertEqual(client.publish.call_args.args[1], 'online')

    def test_offline_command_does_not_modify_nvr(self):
        nvr = device([camera(0)], [])
        msg = SimpleNamespace(topic='homeassistant/switch/dahua_nvr/cam1/set', payload=b'OFF')
        app.on_message(Mock(), nvr, msg)
        methods = [c.args[0] for c in nvr.secure_request.call_args_list]
        self.assertNotIn('LogicDeviceManager.secSetCamera', methods)

    def test_deliberately_disabled_camera_can_be_enabled(self):
        nvr = device([camera(0, True)], [])
        msg = SimpleNamespace(topic='homeassistant/switch/dahua_nvr/cam1/set', payload=b'ON')
        with patch.object(app, 'camera_reachable', return_value=True):
            app.on_message(Mock(), nvr, msg)
        method, params = nvr.secure_request.call_args.args
        self.assertEqual(method, 'LogicDeviceManager.secSetCamera')
        updated = params['cameras'][0]
        self.assertEqual(updated['DeviceInfo']['UserName'], 'admin')
        self.assertFalse(any(k.startswith('_') for k in updated))

    def test_nvr_failure_keeps_bridge_offline_and_subscription_active(self):
        client = Mock()
        with patch.object(app, 'read_state', side_effect=OSError('NVR offline')):
            app.on_connect(client, Mock(), None, None, None)
        client.publish.assert_called_once_with(app.AVAILABILITY_TOPIC, 'offline', qos=1, retain=True)
        client.subscribe.assert_called_once()

    def test_removed_or_nvr_disabled_camera_clears_retained_topics(self):
        for disabled in (False, True):
            with self.subTest(disabled=disabled):
                client = Mock()
                retained = {}

                def publish(topic, payload, **kwargs):
                    self.assertEqual(kwargs, {'qos': 1, 'retain': True})
                    if payload:
                        retained[topic] = payload
                    else:
                        retained.pop(topic, None)

                client.publish.side_effect = publish
                nvr = device([camera(0), camera(1)], [])
                state = app.read_state(nvr)
                app.publish_config(client, state)
                app.publish_state(client, state)
                removed = camera(1)
                removed['Enable'] = False
                nvr.secure_request.return_value = {
                    'params': {'camera': [camera(0)] + ([removed] if disabled else [])},
                }
                state = app.read_state(nvr)
                client.publish.reset_mock()
                app.publish_config(client, state, force=False)
                app.publish_state(client, state)
                prefix = 'homeassistant/switch/dahua_nvr/cam2'
                for suffix in ('config', 'state', 'availability'):
                    client.publish.assert_any_call(
                        f'{prefix}/{suffix}', '', qos=1, retain=True)
                    self.assertNotIn(f'{prefix}/{suffix}', retained)
                # Replaying retained messages after HA/broker restart has only cam1.
                self.assertEqual(len(retained), 3)
                self.assertTrue(all('/cam1/' in topic for topic in retained))
                # A channel can be added back with fresh discovery and state.
                app.publish_config(client, app.read_state(
                    device([camera(0), camera(1)], [])), force=False)
                self.assertIn(f'{prefix}/config', retained)

    def test_off_and_disconnected_cameras_keep_discovery(self):
        client = Mock()
        with patch.object(app, 'camera_reachable', return_value=True):
            state = app.read_state(device([camera(0, True), camera(1)], []))
        app.publish_config(client, state)
        client.publish.reset_mock()
        app.publish_config(client, state, force=False)
        client.publish.assert_not_called()
        app.publish_state(client, state)
        client.publish.assert_any_call(
            'homeassistant/switch/dahua_nvr/cam1/state', 'OFF', qos=1, retain=True)
        client.publish.assert_any_call(
            'homeassistant/switch/dahua_nvr/cam1/availability', 'online', qos=1, retain=True)
        client.publish.assert_any_call(
            'homeassistant/switch/dahua_nvr/cam2/availability', 'offline', qos=1, retain=True)

    def test_first_poll_after_initial_failure_publishes_discovery_before_online(self):
        client = Mock()
        nvr = device([camera(0)], [{'channel': 0, 'connectionState': 'Connected'}])
        with patch.object(app, 'read_state', side_effect=OSError('offline')):
            app.on_connect(client, nvr, None, None, None)
        client.publish.reset_mock()
        # Stop the infinite loop after exactly one poll, without sleeping.
        with patch.object(app.state_changed_event, 'wait',
                          side_effect=[False, SystemExit]):
            with self.assertRaises(SystemExit):
                app.UpdateThread(client, nvr).run()
        calls = client.publish.call_args_list
        topics = [c.args[0] for c in calls]
        self.assertLess(topics.index('homeassistant/switch/dahua_nvr/cam1/config'),
                        topics.index(app.AVAILABILITY_TOPIC))
        self.assertEqual(calls[-1].args, (app.AVAILABILITY_TOPIC, 'online'))
        self.assertTrue(all(c.kwargs == {'qos': 1, 'retain': True} for c in calls))

    def test_poll_failure_preserves_inventory_and_recovery_refreshes_discovery(self):
        client = Mock()
        state = app.read_state(device([camera(0)], []))
        with patch.object(app.state_changed_event, 'wait',
                          side_effect=[False, False, False, SystemExit]), patch.object(
                              app, 'read_state', side_effect=[state, OSError('offline'), state]):
            with self.assertRaises(SystemExit):
                app.UpdateThread(client, Mock()).run()
        configs = [c for c in client.publish.call_args_list if c.args[0].endswith('/config')]
        self.assertEqual(len(configs), 2)
        self.assertTrue(all(c.args[1] for c in configs))
        bridge = [c.args[1] for c in client.publish.call_args_list
                  if c.args[0] == app.AVAILABILITY_TOPIC]
        self.assertEqual(bridge, ['online', 'offline', 'online'])


if __name__ == '__main__':
    unittest.main()
