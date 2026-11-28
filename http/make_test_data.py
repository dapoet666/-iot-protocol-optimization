from __future__ import annotations

import argparse
import csv
import random
import time
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate fake IoT send/receive logs")
    parser.add_argument("--output-dir", default="results/fake_K1")
    parser.add_argument("--sensors", type=int, default=3)
    parser.add_argument("--messages", type=int, default=10)
    parser.add_argument("--delivery-percent", type=float, default=90.0)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    if args.sensors <= 0 or args.messages <= 0:
        parser.error("--sensors and --messages must be positive")
    if not 0 <= args.delivery_percent <= 100:
        parser.error("--delivery-percent must be between 0 and 100")

    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    base_ns = time.time_ns()

    for sensor_id in range(1, args.sensors + 1):
        path = output / f"sensor_h{sensor_id}.csv"
        with path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(["sensor_id", "seq", "send_epoch_ns", "send_time", "size_bytes"])
            for seq in range(1, args.messages + 1):
                send_ns = base_ns + ((seq - 1) * 1_000_000_000) + (sensor_id * 1_000_000)
                send_time = datetime.fromtimestamp(
                    send_ns / 1_000_000_000, timezone.utc
                ).isoformat(timespec="microseconds")
                writer.writerow([sensor_id, seq, send_ns, send_time, 10])

    receive_path = output / "receive.csv"
    with receive_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow([
            "sensor_id", "seq", "measurement", "receive_epoch_ns",
            "receive_time_utc", "client_ip", "body_bytes",
        ])
        for sensor_id in range(1, args.sensors + 1):
            for seq in range(1, args.messages + 1):
                if rng.random() * 100 > args.delivery_percent:
                    continue
                send_ns = base_ns + ((seq - 1) * 1_000_000_000) + (sensor_id * 1_000_000)
                delay_ns = rng.randint(5_000_000, 50_000_000)
                receive_ns = send_ns + delay_ns
                receive_time = datetime.fromtimestamp(
                    receive_ns / 1_000_000_000, timezone.utc
                ).isoformat(timespec="microseconds")
                writer.writerow([
                    sensor_id, seq, round(rng.gauss(25.0, 2.0), 4), receive_ns,
                    receive_time, "10.0.1.1", 10,
                ])

    print(f"Synthetic test data created in: {output}")
    print("Files: sensor_h*.csv and receive.csv")
    print("Run: python analyze.py --run-dir \"<output-dir>\"")


if __name__ == "__main__":
    main()
