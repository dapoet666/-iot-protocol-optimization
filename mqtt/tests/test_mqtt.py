"""Offline tests for MQTT packet format, gateway forwarding, and CSV logging.

Uses a fake Paho client; not a substitute for Mininet and Mosquitto.
"""
import csv
import importlib.util
from pathlib import Path
import signal
import socket
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

class Reason:
    is_failure = False
    def __eq__(self, other):
        return other == 0

class FakeInfo:
    rc = 0

class FakeClient:
    instances = []
    behavior = "gateway"
    def __init__(self, *_a, **_kw):
        self.published = []
        FakeClient.instances.append(self)
    def reconnect_delay_set(self, **_kw):
        pass
    def connect(self, *_a, **_kw):
        pass
    def loop_start(self):
        self.on_connect(self, None, None, Reason(), None)
        if FakeClient.behavior == "subscriber":
            self.on_message(self, None, types.SimpleNamespace(payload=struct.pack("!HIf", 7, 12, 25.5)))
            self.on_message(self, None, types.SimpleNamespace(payload=b"bad"))
    def loop_stop(self):
        pass
    def disconnect(self):
        pass
    def subscribe(self, *_a, **_kw):
        self.on_subscribe(self, None, 1, [Reason()], None)
        return (0, 1)
    def publish(self, topic, payload, qos):
        self.published.append((topic, payload, qos))
        return FakeInfo()

fake_client_module = types.ModuleType("paho.mqtt.client")
fake_client_module.Client = FakeClient
fake_client_module.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
fake_client_module.MQTT_ERR_SUCCESS = 0
fake_root = types.ModuleType("paho")
fake_mqtt = types.ModuleType("paho.mqtt")
fake_root.mqtt = fake_mqtt
fake_mqtt.client = fake_client_module

def load(name):
    with patch.dict(sys.modules, {"paho": fake_root, "paho.mqtt": fake_mqtt, "paho.mqtt.client": fake_client_module}):
        spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module

class TestMQTT(unittest.TestCase):
    def setUp(self):
        FakeClient.instances.clear()

    def test_binary_format(self):
        payload = struct.pack("!HIf", 7, 12, 25.5)
        self.assertEqual(len(payload), 10)
        self.assertEqual(struct.unpack("!HIf", payload), (7, 12, 25.5))

    def test_subscriber_csv(self):
        FakeClient.behavior = "subscriber"
        mod = load("srv_mqtt")
        with tempfile.TemporaryDirectory() as d:
            csv_file = str(Path(d) / "rx.csv")
            with patch.object(sys, "argv", ["srv_mqtt.py", "--out", csv_file]), patch.object(mod.signal, "signal"):
                with patch.object(mod.threading.Event, "wait", lambda ev, timeout=None: True if timeout == 0.2 else ev.is_set()):
                    mod.main()
            with open(csv_file, newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["sensor_id"], rows[0]["seq"]), ("7", "12"))
            self.assertGreater(int(rows[0]["recv_epoch_ns"]), 0)

    def test_gateway_forwards_bytes(self):
        FakeClient.behavior = "gateway"
        mod = load("gw_mqtt")
        handlers = {}
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as temp:
            temp.bind(("127.0.0.1", 0))
            port = temp.getsockname()[1]
        errors = []
        def runner():
            try:
                with patch.object(sys, "argv", ["gw_mqtt.py", "--udp-bind", "127.0.0.1", "--udp-port", str(port), "--server", "127.0.0.1", "--qos", "1"]), patch.object(mod.signal, "signal", lambda sig, fn: handlers.__setitem__(sig, fn)):
                    mod.main()
            except Exception as e:
                errors.append(e)
        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        payload = struct.pack("!HIf", 1, 60, 26.0)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            for _ in range(40):
                sock.sendto(payload, ("127.0.0.1", port))
                sock.sendto(b"bad", ("127.0.0.1", port))
                if FakeClient.instances and FakeClient.instances[0].published:
                    break
                time.sleep(0.03)
        for _ in range(50):
            if FakeClient.instances and FakeClient.instances[0].published:
                break
            time.sleep(0.02)
        if signal.SIGTERM in handlers:
            handlers[signal.SIGTERM]()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(FakeClient.instances[0].published)
        self.assertEqual(FakeClient.instances[0].published[0], ("f1/s1/t", payload, 1))

if __name__ == "__main__":
    unittest.main()
