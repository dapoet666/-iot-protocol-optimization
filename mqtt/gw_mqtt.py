#!/usr/bin/env python3
"""MQTT adapter on Mininet node hg: sensor UDP/5000 -> MQTT broker on server.

Sensor payload: network byte order !HIf = sensor_id:uint16, seq:uint32,
value:float32 (10 bytes). The same bytes are published without alteration.
Requires paho-mqtt >= 2.0.
"""
import argparse
import logging
import signal
import socket
import struct
import threading
import time

import paho.mqtt.client as mqtt

SENSOR_RECORD = struct.Struct('!HIf')


def main():
    parser = argparse.ArgumentParser(description='UDP sensor -> MQTT gateway adapter')
    parser.add_argument('--server', default='10.0.3.2', help='MQTT broker IP on Mininet server')
    parser.add_argument('--port', type=int, default=1883)
    parser.add_argument('--topic', default='f1/s1/t')
    parser.add_argument('--qos', type=int, choices=(0, 1), default=0)
    parser.add_argument('--udp-port', type=int, default=5000)
    parser.add_argument('--udp-bind', default='0.0.0.0')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    stopped = threading.Event()
    connected = threading.Event()

    def stop(*_):
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='iot-gw-mqtt')

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            connected.set()
            logging.info('Connected to broker %s:%d', args.server, args.port)
        else:
            connected.clear()
            logging.error('MQTT connection rejected: %s', reason_code)

    def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
        connected.clear()
        if not stopped.is_set():
            logging.warning('MQTT disconnected: %s', reason_code)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=10)
    client.connect(args.server, args.port, keepalive=60)
    client.loop_start()

    accepted = 0
    published = 0
    invalid = 0
    publish_errors = 0
    try:
        if not connected.wait(timeout=10):
            raise RuntimeError('MQTT broker did not confirm connection within 10 seconds')
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind((args.udp_bind, args.udp_port))
            sock.settimeout(0.5)
            logging.info('Receiving sensor UDP on %s:%d (MQTT QoS=%d, topic=%s)',
                         args.udp_bind, args.udp_port, args.qos, args.topic)
            while not stopped.is_set():
                try:
                    payload, _source = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                accepted += 1
                if len(payload) != SENSOR_RECORD.size:
                    invalid += 1
                    logging.warning('Drop unexpected sensor payload length: %d bytes', len(payload))
                    continue
                if not connected.is_set():
                    publish_errors += 1
                    logging.warning('Broker offline; dropping sensor message')
                    continue
                info = client.publish(args.topic, payload, qos=args.qos)
                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                    publish_errors += 1
                    logging.warning('MQTT publish queue rejected message (rc=%s)', info.rc)
                else:
                    published += 1
    finally:
        # Allow queued MQTT packets to be transmitted; not a delivery guarantee.
        time.sleep(0.5)
        client.disconnect()
        client.loop_stop()
        logging.info('UDP received=%d MQTT queued=%d invalid=%d publish_errors=%d',
                     accepted, published, invalid, publish_errors)


if __name__ == '__main__':
    main()
