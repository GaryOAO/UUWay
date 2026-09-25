import copy
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('confirmation', ROOT / 'scripts/native_display_confirmation.py')
confirmation = importlib.util.module_from_spec(spec); spec.loader.exec_module(confirmation)


class ConfirmationTests(unittest.TestCase):
    def test_changed_pixels_recover_early_new_ack_but_not_previous_observation(self):
        with tempfile.TemporaryDirectory() as name:
            directory=Path(name);instance,evidence=self.make(directory)
            before=copy.deepcopy(instance.topology)
            before['modes']=[dict(width=1920,height=1080)]
            target=copy.deepcopy(instance.topology)
            instance.topology=before
            self.log(directory,dict(event='capture_started',generation=2),evidence)
            instance.consume()
            instance.reset_for_topology(target,evidence['accepted_ns']-1)
            instance.consume();self.assertEqual(instance.evidence,evidence)
            instance.topology=before
            instance.reset_for_topology(target,evidence['accepted_ns']+1)
            instance.consume();self.assertIsNone(instance.evidence)
            # Same pixel-size resets keep the strict tail fence.
            instance.reset_for_topology(target,evidence['accepted_ns']-1)
            instance.consume();self.assertIsNone(instance.evidence)

    def test_topology_reset_requires_new_capture_started_after_current_tail(self):
        with tempfile.TemporaryDirectory() as name:
            directory=Path(name);instance,evidence=self.make(directory)
            self.log(directory,dict(event='capture_started',generation=2),evidence)
            instance.consume();self.assertEqual(instance.evidence,evidence)
            instance.reset_for_topology(dict(generation=9))
            self.log(directory,evidence)
            instance.consume();self.assertIsNone(instance.evidence)
            newer=dict(evidence,generation=3)
            self.log(directory,dict(event='capture_started',generation=3),newer)
            instance.consume();self.assertEqual(instance.evidence,newer)
            self.assertEqual(instance.topology,dict(generation=9))

    def values(self):
        record = dict(version=1, confirmation_policy='gpu_frame', transaction='a' * 32,
            pending=dict(confirmed=False, observed=dict(serial=8, mode='720p', scale=1.),
                         target=dict(mode='720p', width=1280, height=720, scale=1.)))
        topology = dict(generation=8, modes=[dict(width=1280, height=720)],
                        layout=[dict(x=0, y=0, scale=1., transform=0, outputs=1)])
        evidence = dict(event='capture_frame_accepted', generation=2, peer_pid=os.getpid(),
            peer_start_ticks=confirmation.process_start_ticks(os.getpid()), width=1280, height=720,
            sequence=1, accepted_ns=time.monotonic_ns())
        return record, topology, evidence

    def test_request_requires_opt_in_matching_geometry_and_fresh_frame(self):
        record, topology, evidence = self.values()
        expected = dict(version=1, op='confirm', serial=8, transaction='a' * 32)
        self.assertEqual(confirmation.confirmation_request(record, topology, evidence, evidence['accepted_ns']), expected)
        for policy in (None, 'manual'):
            other = dict(record, confirmation_policy=policy)
            self.assertIsNone(confirmation.confirmation_request(other, topology, evidence, evidence['accepted_ns']))
        for offset in (-1, 5_000_000_001):
            self.assertIsNone(confirmation.confirmation_request(record, topology, evidence, evidence['accepted_ns'] + offset))
        for field, value in [('serial', 9), ('mode', '1080p'), ('scale', 2.)]:
            other = copy.deepcopy(record); other['pending']['observed'][field] = value
            self.assertIsNone(confirmation.confirmation_request(other, topology, evidence, evidence['accepted_ns']))
        for field, value in [('width', 1920), ('height', 1080)]:
            self.assertIsNone(confirmation.confirmation_request(record, topology, dict(evidence, **{field: value}), evidence['accepted_ns']))
        other = copy.deepcopy(record); other['pending']['observed'] = None
        self.assertIsNone(confirmation.confirmation_request(other, topology, evidence, evidence['accepted_ns']))

    def make(self, directory):
        record, topology, evidence = self.values()
        instance = confirmation.DisplayConfirmation(directory, directory / 'prefix', directory / 'server.exe',
            directory / 'bundle', topology, lambda unused: record)
        return instance, evidence

    def log(self, directory, *values):
        with (directory / 'capture.log').open('ab') as output:
            for value in values:
                output.write(json.dumps(value).encode() + b'\n')

    def test_ready_required_and_lost_reply_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name); instance, evidence = self.make(directory)
            self.log(directory, dict(event='capture_started', generation=2), evidence)
            with patch.object(confirmation, 'owned_consumer', return_value=True), \
                 patch.object(confirmation, 'confirm_rpc', side_effect=TimeoutError) as rpc:
                self.assertIsNone(instance.tick(False, directory / 'display.sock'))
                rpc.assert_not_called()
                self.assertEqual(instance.tick(True, directory / 'display.sock'), 'confirmation_unavailable')
                self.assertIsNone(instance.tick(True, directory / 'display.sock'))
                rpc.assert_called_once()

    def test_ended_generation_cannot_confirm(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name); instance, evidence = self.make(directory)
            self.log(directory, dict(event='capture_started', generation=2), evidence,
                     dict(event='capture_ended', generation=2), evidence)
            with patch.object(confirmation, 'confirm_rpc') as rpc:
                self.assertIsNone(instance.tick(True, directory / 'display.sock'))
                rpc.assert_not_called()

    def test_owned_consumer_required(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name); instance, evidence = self.make(directory)
            self.log(directory, dict(event='capture_started', generation=2), evidence)
            with patch.object(confirmation, 'owned_consumer', return_value=False), \
                 patch.object(confirmation, 'confirm_rpc') as rpc:
                self.assertEqual(instance.tick(True, directory / 'display.sock'), 'frame_consumer_not_owned')
                rpc.assert_not_called()

    def test_partial_oversized_and_wrong_generation_lines(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name); instance, evidence = self.make(directory)
            (directory / 'capture.log').write_bytes(b'x' * 90000)
            instance.consume(); instance.consume()
            self.log(directory, {}, dict(event='capture_started', generation=3), evidence)
            instance.consume(); self.assertIsNone(instance.evidence)
            self.log(directory, dict(event='capture_started', generation=2))
            with (directory / 'capture.log').open('ab') as output:
                output.write(json.dumps(evidence).encode())
            instance.consume(); self.assertIsNone(instance.evidence)
            with (directory / 'capture.log').open('ab') as output: output.write(b'\n')
            instance.consume(); self.assertEqual(instance.evidence, evidence)

    def test_actual_process_mapping_and_pid_reuse_guards(self):
        _, _, evidence = self.values()
        executable = Path('/proc/self/exe').resolve()
        self.assertTrue(confirmation.mapped_files_match(os.getpid(), (executable,)))
        self.assertFalse(confirmation.owned_consumer(dict(evidence, peer_start_ticks=1), Path('/unused'), executable, executable))
        self.assertFalse(confirmation.owned_consumer(evidence, Path('/unused'), executable, executable))

    def test_real_private_confirmation_rpc(self):
        with tempfile.TemporaryDirectory() as name:
            endpoint = Path(name) / 'display.sock'
            request = dict(version=1, op='confirm', serial=8, transaction='fixture')
            received = []
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as server:
                server.bind(str(endpoint)); endpoint.chmod(0o600); server.listen(1); server.settimeout(3)
                def serve():
                    with server.accept()[0] as client:
                        client.settimeout(3); received.append(json.loads(client.recv(2048)))
                        client.sendall(b'{"ok":true,"confirmed":true}')
                worker = threading.Thread(target=serve); worker.start()
                self.assertTrue(confirmation.confirm_rpc(endpoint, request))
                worker.join(4)
            self.assertFalse(worker.is_alive()); self.assertEqual(received, [request])


if __name__ == '__main__':
    unittest.main()
