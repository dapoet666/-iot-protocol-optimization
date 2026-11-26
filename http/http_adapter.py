#!/usr/bin/env python3
"""Gateway adapter: receive 10-byte UDP sensor datagrams and forward by HTTP.

Two forwarding modes are supported:
  --mode keep-alive : reuse one HTTP/1.1 TCP connection where possible.
  --mode new        : open and close one HTTP connection for each datagram.
"""

from __future__ import annotations

import argparse
import csv
import http.client
import os
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timezone

from protocol import PACKET_SIZE, decode_packet


class Adapter:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.stop_event = threading.Event()
        self.log_lock = threading.Lock()
        self.http_connection: http.client.HTTPConnection | None = None
        self.log_file = None
        self.log_writer = None

    def _get_keepalive_connection(self) -> http.client.HTTPConnection:
        if self.http_connection is None:
            self.http_connection = http.client.HTTPConnection(
                self.args.server_host,
                self.args.server_port,
                timeout=self.args.timeout,
            )
        return self.http_connection

    def _forward(self, payload: bytes) -> tuple[bool, int | None, str]:
        connection: http.client.HTTPConnection
        owns_connection = self.args.mode == "new"

        if owns_connection:
            connection = http.client.HTTPConnection(
                self.args.server_host,
                self.args.server_port,
                timeout=self.args.timeout,
            )
        else:
            connection = self._get_keepalive_connection()

        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(payload)),
        }
        if owns_connection:
            headers["Connection"] = "close"

        try:
            connection.request("POST", self.args.path, body=payload, headers=headers)
            response = connection.getresponse()
            response_body = response.read()  # Must consume body before reusing connection.
            status = response.status
            response_text = response_body.decode("utf-8", errors="replace")[:100]
            ok = 200 <= status < 300
            if not owns_connection and (response.will_close or connection.sock is None):
                connection.close()
                self.http_connection = None
            return ok, status, response_text
        except (OSError, http.client.HTTPException) as exc:
            connection.close()
            if not owns_connection:
                self.http_connection = None
            return False, None, str(exc)
        finally:
            if owns_connection:
                connection.close()

    def _write_log(
        self,
        sensor_id: int | str,
        sequence_id: int | str,
        source_ip: str,
        source_port: int,
        payload_size: int,
        success: bool,
        http_status: int | None,
        detail: str,
    ) -> None:
        if self.log_writer is None:
            return
        now_ns = time.time_ns()
        with self.log_lock:
            self.log_writer.writerow(
                [
                    now_ns,
                    datetime.fromtimestamp(now_ns / 1_000_000_000, timezone.utc).isoformat(),
                    sensor_id,
                    sequence_id,
                    source_ip,
                    source_port,
                    payload_size,
                    self.args.mode,
                    "OK" if success else "ERROR",
                    http_status if http_status is not None else "",
                    detail.replace("\n", " ")[:200],
                ]
            )
            self.log_file.flush()

    def run(self) -> None:
        if self.args.log:
            os.makedirs(os.path.dirname(os.path.abspath(self.args.log)), exist_ok=True)
            self.log_file = open(self.args.log, "w", newline="", encoding="utf-8")
            self.log_writer = csv.writer(self.log_file)
            self.log_writer.writerow(
                [
                    "adapter_epoch_ns",
                    "adapter_time_utc",
                    "sensor_id",
                    "seq",
                    "sensor_ip",
                    "sensor_port",
                    "udp_payload_bytes",
                    "http_mode",
                    "forward_status",
                    "http_status",
                    "detail",
                ]
            )

        udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp_socket.bind((self.args.udp_host, self.args.udp_port))
        udp_socket.settimeout(0.5)
        if self.args.ready_file:
            ready_path = os.path.abspath(self.args.ready_file)
            os.makedirs(os.path.dirname(ready_path), exist_ok=True)
            with open(ready_path, "w", encoding="utf-8") as ready:
                ready.write("ready\n")

        print(
            f"UDP adapter listening on {self.args.udp_host}:{self.args.udp_port}; "
            f"forwarding to http://{self.args.server_host}:{self.args.server_port}"
            f"{self.args.path} using {self.args.mode}"
        )

        try:
            while not self.stop_event.is_set():
                try:
                    payload, source = udp_socket.recvfrom(65535)
                except socket.timeout:
                    continue

                try:
                    sensor_id, sequence_id, _measurement = decode_packet(payload)
                except ValueError as exc:
                    print(f"Invalid UDP datagram from {source}: {exc}", file=sys.stderr)
                    self._write_log("", "", source[0], source[1], len(payload), False, None, str(exc))
                    continue

                ok, status, detail = self._forward(payload)
                self._write_log(
                    sensor_id,
                    sequence_id,
                    source[0],
                    source[1],
                    len(payload),
                    ok,
                    status,
                    detail,
                )
                if not ok:
                    print(
                        f"Forward failed: sensor={sensor_id}, seq={sequence_id}: {detail}",
                        file=sys.stderr,
                    )
        finally:
            udp_socket.close()
            if self.http_connection is not None:
                self.http_connection.close()
            if self.log_file is not None:
                self.log_file.close()

    def stop(self, *_: object) -> None:
        self.stop_event.set()


def main() -> None:
    parser = argparse.ArgumentParser(description="UDP-to-HTTP IoT gateway adapter")
    parser.add_argument("--udp-host", default="0.0.0.0", help="UDP bind address")
    parser.add_argument("--udp-port", type=int, default=5000, help="UDP listen port")
    parser.add_argument("--server-host", default="10.0.3.2", help="HTTP server IP/host")
    parser.add_argument("--server-port", type=int, default=8080, help="HTTP server port")
    parser.add_argument("--path", default="/telemetry", help="HTTP POST path")
    parser.add_argument(
        "--mode", choices=("keep-alive", "new"), default="keep-alive",
        help="Reuse HTTP connection or open a new one per UDP packet",
    )
    parser.add_argument("--timeout", type=float, default=5.0, help="HTTP timeout in seconds")
    parser.add_argument("--log", default="", help="Optional adapter CSV log path")
    parser.add_argument("--ready-file", default="", help="Optional file touched after UDP bind is ready")
    args = parser.parse_args()

    for name in ("udp_port", "server_port"):
        if not 1 <= getattr(args, name) <= 65535:
            parser.error(f"--{name.replace('_', '-')} must be between 1 and 65535")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    adapter = Adapter(args)
    signal.signal(signal.SIGINT, adapter.stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, adapter.stop)
    try:
        adapter.run()
    except OSError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
