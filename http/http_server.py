from __future__ import annotations

import argparse
import csv
import os
import struct
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TextIO

SENSOR_RECORD = struct.Struct("!HIf")
SENSOR_PACKET_SIZE = SENSOR_RECORD.size


RECEIVE_FIELDS = [
    "sensor_id",
    "seq",
    "recv_epoch_ns",
    "measurement",
    "recv_time_utc",
    "client_ip",
    "body_bytes",
]


class IoTHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class IoTRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "IoTExperimentHTTP/1.0"

    def _reply(self, status: int, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path == "/health":
            self._reply(200, "OK")
        else:
            self._reply(404, "Not Found")

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path != "/telemetry":
            self._reply(404, "Not Found")
            return

        raw_length = self.headers.get("Content-Length")
        try:
            content_length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            content_length = -1

        if content_length != SENSOR_PACKET_SIZE:
            # Close malformed requests so unread bytes cannot be interpreted as
            # another HTTP/1.1 request on the same persistent connection.
            self.close_connection = True
            self._reply(400, f"Expected Content-Length: {SENSOR_PACKET_SIZE}")
            return

        payload = self.rfile.read(content_length)
        if len(payload) != SENSOR_PACKET_SIZE:
            self.close_connection = True
            self._reply(400, "Incomplete request body")
            return

        # Decode the same 10-byte format used by sensor.py: !HIf.
        try:
            sensor_id, sequence_id, measurement = SENSOR_RECORD.unpack(payload)
        except struct.error as exc:
            self.close_connection = True
            self._reply(400, f"Invalid sensor packet: {exc}")
            return

        # Timestamp is recorded after the complete body has arrived.
        recv_epoch_ns = time.time_ns()
        recv_time_utc = datetime.fromtimestamp(
            recv_epoch_ns / 1_000_000_000, timezone.utc
        ).isoformat(timespec="microseconds")

        # One CSV file/writer is shared by request threads. The lock protects
        # each row, and flush makes the row visible during/after a run.
        lock = self.server.log_lock  # type: ignore[attr-defined]
        writer = self.server.log_writer  # type: ignore[attr-defined]
        log_file: TextIO = self.server.log_file  # type: ignore[attr-defined]
        with lock:
            writer.writerow(
                [
                    sensor_id,
                    sequence_id,
                    recv_epoch_ns,
                    f"{measurement:.9g}",
                    recv_time_utc,
                    self.client_address[0],
                    len(payload),
                ]
            )
            log_file.flush()

        self._reply(200, "OK")

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep console output compact; the run controller redirects this to a log.
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="IoT HTTP/1.1 telemetry server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port")
    parser.add_argument("--log", default="logs/receive.csv", help="Receive CSV path")
    args = parser.parse_args()

    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    os.makedirs(os.path.dirname(os.path.abspath(args.log)), exist_ok=True)
    server = IoTHTTPServer((args.host, args.port), IoTRequestHandler)

    # Match the MQTT logger's lifecycle: open once at startup, write the header
    # once, append rows from callbacks/handlers, then close at shutdown.
    log_file = open(args.log, "w", newline="", buffering=1, encoding="utf-8")
    log_writer = csv.writer(log_file)
    log_writer.writerow(RECEIVE_FIELDS)
    log_file.flush()

    server.log_file = log_file  # type: ignore[attr-defined]
    server.log_writer = log_writer  # type: ignore[attr-defined]
    server.log_lock = threading.Lock()  # type: ignore[attr-defined]

    print(f"HTTP/1.1 server listening on {args.host}:{args.port}")
    print(f"Receive log: {os.path.abspath(args.log)}")
    print("Health endpoint: GET /health")
    print("Telemetry endpoint: POST /telemetry (10-byte body)")

    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("Stopping HTTP server...")
    finally:
        server.server_close()
        log_file.flush()
        log_file.close()


if __name__ == "__main__":
    main()
