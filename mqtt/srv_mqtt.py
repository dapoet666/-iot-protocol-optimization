#!/usr/bin/env python3
"""MQTT subscriber on Mininet node server. Logs received sensor packets to CSV.

CSV columns: sensor_id,seq,recv_epoch_ns. Match sensor_h<id>.csv on
(sensor_id, seq), using send_epoch_ns from the sensor's own log.
Requires paho-mqtt >= 2.0.
"""
import argparse
import csv
import logging
import os
import signal
import struct
import threading
import time

import paho.mqtt.client as mqtt

SENSOR_RECORD = struct.Struct('!HIf')


def main():
    parser = argparse.ArgumentParser(description='MQTT receiver -> CSV logger')
    parser.add_argument('--broker', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=1883)
    parser.add_argument('--topic', default='f1/s1/t')
    parser.add_argument('--run-id', default='m01', help='Optional label for console logs only')
    parser.add_argument('--out', default='m01.server.csv')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)
    stopping = threading.Event()
    subscribed = threading.Event()

    def stop(*_):
        stopping.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    n_received = 0
    n_malformed = 0
    with open(args.out, 'w', newline='', buffering=1, encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['sensor_id', 'seq', 'recv_epoch_ns'])
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='iot-srv-mqtt')

        def on_connect(client, userdata, flags, reason_code, properties):
            if reason_code == 0:
                result, _mid = client.subscribe(args.topic, qos=1)
                if result != mqtt.MQTT_ERR_SUCCESS:
                    logging.error('MQTT subscribe request failed: rc=%s', result)
                else:
                    logging.info('Subscription requested: %s (run=%s)', args.topic, args.run_id)
            else:
                logging.error('Broker rejected subscriber: %s', reason_code)

        def on_subscribe(client, userdata, mid, reason_code_list, properties):
            if all(not reason.is_failure for reason in reason_code_list):
                subscribed.set()
                logging.info('Subscription confirmed for topic %s', args.topic)
            else:
                logging.error('MQTT subscription rejected: %s', reason_code_list)

        def on_message(client, userdata, message):
            nonlocal n_received, n_malformed
            received_ns = time.time_ns()
            data = message.payload
            if not data or len(data) % SENSOR_RECORD.size:
                n_malformed += 1
                logging.warning('Drop malformed MQTT message (%d bytes)', len(data))
                return
            for index in range(0, len(data), SENSOR_RECORD.size):
                sensor_id, seq, _value = SENSOR_RECORD.unpack_from(data, index)
                writer.writerow([sensor_id, seq, received_ns])
                n_received += 1
            handle.flush()

        client.on_connect = on_connect
        client.on_message = on_message
        client.on_subscribe = on_subscribe
        client.reconnect_delay_set(min_delay=1, max_delay=10)
        client.connect(args.broker, args.port, keepalive=60)
        client.loop_start()
        try:
            if not subscribed.wait(timeout=10):
                raise RuntimeError('MQTT subscription was not confirmed within 10 seconds')
            logging.info('Ready; writing %s', args.out)
            while not stopping.wait(0.2):
                pass
        finally:
            client.disconnect()
            client.loop_stop()
            logging.info('CSV records=%d malformed messages=%d', n_received, n_malformed)


if __name__ == '__main__':
    main()
