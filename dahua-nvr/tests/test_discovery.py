import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_availability import app, camera, device


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.retained = {}
        self.client = self.make_client()
        self.nvr = device([], [])
        self.network_guard = patch.object(
            app.socket, 'create_connection',
            side_effect=AssertionError('Tests must not contact cameras or MQTT'),
        )
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)
        self.addCleanup(app.state_changed_event.clear)

    def make_client(self):
        client = app.DahuaMqttClient()

        def publish(topic, payload, qos=0, retain=False):
            if retain:
                if payload in ('', b''):
                    self.retained.pop(topic, None)
                else:
                    self.retained[topic] = payload
            return SimpleNamespace(rc=app.mqtt.MQTT_ERR_SUCCESS)

        client.publish = Mock(side_effect=publish)
        client.subscribe = Mock(return_value=(app.mqtt.MQTT_ERR_SUCCESS, 1))
        client.is_connected = Mock(return_value=True)
        return client

    @staticmethod
    def topic(name, suffix):
        return f'homeassistant/switch/dahua_nvr/{name}/{suffix}'

    def set_cameras(self, cameras, connected=None):
        if connected is None:
            connected = [cam['UniqueChannel'] for cam in cameras]
        self.nvr.secure_request.side_effect = None
        self.nvr.secure_request.return_value = {
            'params': {'camera': copy.deepcopy(cameras)},
        }
        self.nvr.request.return_value = {
            'params': {'states': [
                {'channel': channel, 'connectionState': 'Connected'}
                for channel in connected
            ]},
        }

    def connect(self):
        app.on_connect(self.client, self.nvr, None, None, None)

    def poll(self, count=1):
        # The exception is raised at the next wait, outside the poll's handler.
        with patch.object(app.state_changed_event, 'wait',
                          side_effect=[False] * count + [StopIteration]):
            with self.assertRaises(StopIteration):
                app.UpdateThread(self.client, self.nvr).run()

    def deliver(self, name, suffix, payload=b'old value', retained=True):
        app.on_message(self.client, self.nvr, SimpleNamespace(
            topic=self.topic(name, suffix), payload=payload, retain=retained,
        ))

    def seed_camera(self, name):
        self.retained.update({
            self.topic(name, 'config'): '{"old": true}',
            self.topic(name, 'state'): 'ON',
            self.topic(name, 'availability'): 'online',
        })

    def assert_removed(self, name):
        for suffix in ('config', 'state', 'availability'):
            topic = self.topic(name, suffix)
            self.assertNotIn(topic, self.retained)
            self.client.publish.assert_any_call(topic, '', qos=1, retain=True)

    def assert_config_before_online(self, name):
        calls = self.client.publish.call_args_list
        online = next(i for i, call in enumerate(calls)
                      if call.args == (app.AVAILABILITY_TOPIC, 'online'))
        for suffix in ('config', 'state', 'availability'):
            index = next(i for i, call in enumerate(calls)
                         if call.args[0] == self.topic(name, suffix)
                         and call.args[1])
            self.assertLess(index, online, suffix)

    def test_removed_camera_is_cleared_on_next_poll(self):
        self.set_cameras([camera(0), camera(1)])
        self.connect()
        surviving_config = self.retained[self.topic('cam2', 'config')]
        # Cleanup must never erase command topics, even if retained elsewhere.
        command_topic = self.topic('cam1', 'set')
        self.retained[command_topic] = 'OFF'
        self.client.publish.reset_mock()

        self.set_cameras([camera(1)])
        self.poll()

        self.assert_removed('cam1')
        self.assertEqual(self.retained[self.topic('cam2', 'config')], surviving_config)
        self.assertEqual(self.retained[self.topic('cam2', 'state')], 'ON')
        self.assertEqual(self.retained[self.topic('cam2', 'availability')], 'online')
        self.assertEqual(self.retained[command_topic], 'OFF')

    def test_partial_cleanup_publish_error_is_retried_on_next_poll(self):
        self.set_cameras([camera(0), camera(1)])
        self.connect()
        self.set_cameras([camera(1)])
        publish_success = self.client.publish.side_effect

        def fail_last_tombstone(topic, payload, **kwargs):
            if topic == self.topic('cam1', 'availability') and payload == '':
                return SimpleNamespace(rc=app.mqtt.MQTT_ERR_QUEUE_SIZE)
            return publish_success(topic, payload, **kwargs)

        self.client.publish.side_effect = fail_last_tombstone
        self.client.publish.reset_mock()
        with self.assertLogs(app.log, level='ERROR'):
            self.poll()

        self.assertNotIn(self.topic('cam1', 'config'), self.retained)
        self.assertNotIn(self.topic('cam1', 'state'), self.retained)
        self.assertEqual(self.retained[self.topic('cam1', 'availability')], 'online')
        self.assertEqual(self.retained[app.AVAILABILITY_TOPIC], 'offline')
        self.assertFalse(any(call.args == (app.AVAILABILITY_TOPIC, 'online')
                             for call in self.client.publish.call_args_list))

        self.client.publish.side_effect = publish_success
        self.client.publish.reset_mock()
        self.poll()

        self.assert_removed('cam1')
        self.assert_config_before_online('cam2')
        self.assertEqual(self.retained[app.AVAILABILITY_TOPIC], 'online')

    def test_camera_disabled_in_nvr_is_removed(self):
        cam = camera(0)
        self.set_cameras([cam])
        self.connect()
        self.client.publish.reset_mock()

        cam['Enable'] = False
        self.set_cameras([cam])
        self.poll()

        self.assert_removed('cam1')
        self.assertEqual(self.retained[app.AVAILABILITY_TOPIC], 'online')

    def test_empty_inventory_clears_every_camera(self):
        self.set_cameras([camera(0), camera(1)])
        self.connect()
        self.client.publish.reset_mock()

        self.set_cameras([])
        self.poll()

        self.assert_removed('cam1')
        self.assert_removed('cam2')
        self.assertEqual(self.retained, {app.AVAILABILITY_TOPIC: 'online'})

    def test_off_and_unavailable_cameras_keep_discovery(self):
        self.set_cameras([camera(0), camera(1)])
        self.connect()
        configs = {name: self.retained[self.topic(name, 'config')]
                   for name in ('cam1', 'cam2')}
        self.client.publish.reset_mock()

        self.set_cameras([camera(0, disabled=True), camera(1)], connected=[])
        with patch.object(app, 'camera_reachable', return_value=True):
            self.poll()

        for name in ('cam1', 'cam2'):
            self.assertEqual(self.retained[self.topic(name, 'config')], configs[name])
            config = json.loads(configs[name])
            self.assertEqual(config['availability_mode'], 'all')
            self.assertEqual(config['availability'], [
                {'topic': app.AVAILABILITY_TOPIC},
                {'topic': self.topic(name, 'availability')},
            ])
        self.assertEqual(self.retained[self.topic('cam1', 'state')], 'OFF')
        self.assertEqual(self.retained[self.topic('cam1', 'availability')], 'online')
        self.assertEqual(self.retained[self.topic('cam2', 'state')], 'ON')
        self.assertEqual(self.retained[self.topic('cam2', 'availability')], 'offline')
        for call in self.client.publish.call_args_list:
            self.assertTrue(call.args[1])
            self.assertEqual(call.kwargs, {'qos': 1, 'retain': True})

    def test_initial_nvr_failure_recovers_on_first_successful_poll(self):
        self.nvr.secure_request.side_effect = OSError('NVR offline')
        with self.assertLogs(app.log, level='ERROR'):
            self.connect()
        self.assertEqual(self.retained, {app.AVAILABILITY_TOPIC: 'offline'})
        self.client.subscribe.assert_called_once_with(
            'homeassistant/switch/dahua_nvr/+/+', qos=1,
        )
        self.client.publish.reset_mock()

        self.set_cameras([camera(0)])
        self.poll()

        self.assert_config_before_online('cam1')
        self.assertEqual(self.retained[app.AVAILABILITY_TOPIC], 'online')

    def test_initial_discovery_publish_error_is_retried_on_first_poll(self):
        self.set_cameras([camera(0)])
        publish_success = self.client.publish.side_effect

        def fail_discovery(topic, payload, **kwargs):
            if topic == self.topic('cam1', 'config'):
                return SimpleNamespace(rc=app.mqtt.MQTT_ERR_NO_CONN)
            return publish_success(topic, payload, **kwargs)

        self.client.publish.side_effect = fail_discovery
        with self.assertLogs(app.log, level='ERROR'):
            self.connect()

        self.assertEqual(self.retained, {app.AVAILABILITY_TOPIC: 'offline'})
        self.assertFalse(any(call.args == (app.AVAILABILITY_TOPIC, 'online')
                             for call in self.client.publish.call_args_list))

        self.client.publish.side_effect = publish_success
        self.client.publish.reset_mock()
        self.poll()

        self.assert_config_before_online('cam1')
        self.assertEqual(self.retained[app.AVAILABILITY_TOPIC], 'online')

    def test_later_nvr_failure_preserves_inventory_and_refreshes_on_recovery(self):
        self.set_cameras([camera(0)])
        self.connect()
        previous_config = self.retained[self.topic('cam1', 'config')]
        self.client.publish.reset_mock()

        self.nvr.secure_request.side_effect = OSError('NVR offline')
        with self.assertLogs(app.log, level='ERROR'):
            self.poll()
        self.assertEqual(self.retained[self.topic('cam1', 'config')], previous_config)
        self.assertEqual(self.retained[app.AVAILABILITY_TOPIC], 'offline')
        self.assertFalse(any(call.args[1] == ''
                             for call in self.client.publish.call_args_list))
        self.client.publish.reset_mock()

        self.set_cameras([camera(0)])
        self.poll()

        self.assert_config_before_online('cam1')
        self.assertEqual(self.retained[app.AVAILABILITY_TOPIC], 'online')

    def test_removal_during_mqtt_disconnect_is_cleaned_on_reconnect(self):
        self.set_cameras([camera(0), camera(1)])
        self.connect()
        self.client.publish.reset_mock()
        self.set_cameras([camera(1)])

        self.connect()

        self.assert_removed('cam1')
        self.assert_config_before_online('cam2')
        self.assertEqual(self.client.subscribe.call_count, 2)

    def test_reconnect_failure_keeps_inventory_until_first_recovered_poll(self):
        self.set_cameras([camera(0), camera(1)])
        self.connect()
        self.nvr.secure_request.side_effect = OSError('NVR offline')
        with self.assertLogs(app.log, level='ERROR'):
            self.connect()
        self.assertIn(self.topic('cam1', 'config'), self.retained)
        self.client.publish.reset_mock()

        self.set_cameras([camera(1)])
        self.poll()

        self.assert_removed('cam1')
        self.assert_config_before_online('cam2')

    def test_unchanged_inventory_keeps_periodic_discovery_refresh(self):
        self.set_cameras([camera(0)])
        self.connect()
        self.client.publish.reset_mock()

        self.poll(count=10)

        configs = [call for call in self.client.publish.call_args_list
                   if call.args[0] == self.topic('cam1', 'config')]
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0].kwargs, {'qos': 1, 'retain': True})

    def test_new_camera_is_discovered_on_next_poll(self):
        self.set_cameras([camera(0)])
        self.connect()
        self.client.publish.reset_mock()
        self.set_cameras([camera(0), camera(1)])

        self.poll()

        self.assert_config_before_online('cam2')

    def test_cold_start_cleans_stale_retained_discovery(self):
        self.seed_camera('cam7')
        self.set_cameras([camera(0)])
        self.connect()
        self.client.publish.reset_mock()

        self.deliver('cam7', 'config', payload=b'{"old": true}')

        self.assert_removed('cam7')
        self.assertIn(self.topic('cam1', 'config'), self.retained)
        self.assertEqual(self.retained[app.AVAILABILITY_TOPIC], 'online')

    def test_cold_start_cleans_orphan_retained_state_and_availability(self):
        self.set_cameras([])
        self.connect()
        self.client.publish.reset_mock()

        for name, suffix in (('cam7', 'state'), ('cam8', 'availability')):
            with self.subTest(suffix=suffix):
                self.retained[self.topic(name, suffix)] = 'old value'
                self.deliver(name, suffix)
                self.assert_removed(name)

    def test_retained_cleanup_waits_for_successful_nvr_inventory(self):
        self.seed_camera('cam7')
        self.nvr.secure_request.side_effect = OSError('NVR offline')
        with self.assertLogs(app.log, level='ERROR'):
            self.connect()
        self.client.publish.reset_mock()

        self.deliver('cam7', 'config')

        self.client.publish.assert_not_called()
        self.assertIn(self.topic('cam7', 'config'), self.retained)
        self.set_cameras([camera(0)])
        self.poll()
        self.assert_removed('cam7')
        self.assert_config_before_online('cam1')

    def test_retained_existing_camera_is_preserved(self):
        self.set_cameras([camera(0)])
        self.connect()
        previous = dict(self.retained)
        self.client.publish.reset_mock()

        for suffix in ('config', 'state', 'availability'):
            self.deliver('cam1', suffix)

        self.client.publish.assert_not_called()
        self.assertEqual(self.retained, previous)

    def test_live_known_topics_and_empty_unknown_topics_do_not_trigger_cleanup(self):
        self.set_cameras([camera(0)])
        self.connect()
        self.client.publish.reset_mock()
        self.nvr.secure_request.reset_mock()

        for suffix in ('config', 'state', 'availability'):
            self.deliver('cam1', suffix, retained=False)
            self.deliver('cam7', suffix, payload=b'')
            self.deliver('cam7', suffix, payload=b'', retained=False)

        self.client.publish.assert_not_called()
        self.nvr.secure_request.assert_not_called()

    def test_live_replay_of_obsolete_camera_topics_is_cleared(self):
        self.set_cameras([camera(0)])
        self.connect()

        for suffix in ('config', 'state', 'availability'):
            with self.subTest(suffix=suffix):
                # Old queued QoS 1 publications can reach the broker after reconnect.
                self.seed_camera('cam7')
                self.client.publish.reset_mock()
                payload = self.retained[self.topic('cam7', suffix)].encode()

                self.deliver('cam7', suffix, payload=payload, retained=False)

                self.assert_removed('cam7')
                self.assertIn(self.topic('cam1', 'config'), self.retained)

    def test_replayed_config_tombstone_is_repaired_for_readded_camera(self):
        self.set_cameras([camera(0)])
        self.connect()
        self.set_cameras([])
        self.poll()
        self.set_cameras([camera(0)])
        self.poll()
        self.client.publish.reset_mock()

        # A delayed deletion from before re-adding the camera overwrites discovery.
        self.retained.pop(self.topic('cam1', 'config'))
        self.deliver('cam1', 'config', payload=b'', retained=False)
        self.client.publish.assert_not_called()

        self.poll()

        self.assert_config_before_online('cam1')
        self.assertIn(self.topic('cam1', 'config'), self.retained)
        self.assertEqual(self.retained[app.AVAILABILITY_TOPIC], 'online')

    def test_failed_poll_invalidates_inventory_for_late_retained_cleanup(self):
        self.set_cameras([])
        self.connect()
        self.nvr.secure_request.side_effect = OSError('NVR offline')
        with self.assertLogs(app.log, level='ERROR'):
            self.poll()
        self.seed_camera('cam7')
        self.client.publish.reset_mock()

        self.deliver('cam7', 'config')

        self.client.publish.assert_not_called()
        self.assertIn(self.topic('cam7', 'config'), self.retained)
        self.set_cameras([camera(6)])
        self.poll()
        self.assertIn(self.topic('cam7', 'config'), self.retained)
        self.assertEqual(self.retained[self.topic('cam7', 'state')], 'ON')
        self.assertFalse(any(call.args[1] == ''
                             for call in self.client.publish.call_args_list))


if __name__ == '__main__':
    unittest.main()
