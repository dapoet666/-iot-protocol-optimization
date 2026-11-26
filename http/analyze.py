from __future__ import annotations

import argparse
import csv
import math
import statistics
import struct
import sys
from pathlib import Path
from typing import Any


T_CRITICAL_95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
    6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
    11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
    16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
    26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}


def read_sensor_logs(run_dir: Path) -> dict[tuple[int, int], int]:
    sent: dict[tuple[int, int], int] = {}
    files = sorted(run_dir.glob("sensor_h*.csv"))
    if not files:
        raise FileNotFoundError(f"No sensor_h*.csv files found in {run_dir}")

    for path in files:
        with path.open("r", newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            required = {"sensor_id", "seq", "send_epoch_ns"}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ValueError(
                    f"{path.name} must contain columns: {', '.join(sorted(required))}"
                )
            for row in reader:
                try:
                    key = (int(row["sensor_id"]), int(row["seq"]))
                    timestamp = int(row["send_epoch_ns"])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"Invalid send-log row in {path.name}: {row}") from exc
                # A sensor ID + sequence pair identifies exactly one message.
                sent.setdefault(key, timestamp)
    return sent


def read_receive_log(path: Path) -> dict[tuple[int, int], int]:
    if not path.is_file():
        raise FileNotFoundError(f"Receive log not found: {path}")
    received: dict[tuple[int, int], int] = {}
    with path.open("r", newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        required = {"sensor_id", "seq", "receive_epoch_ns"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(
                f"{path.name} must contain columns: {', '.join(sorted(required))}"
            )
        for row in reader:
            try:
                key = (int(row["sensor_id"]), int(row["seq"]))
                timestamp = int(row["receive_epoch_ns"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid receive-log row: {row}") from exc
            # Duplicates count once for delivery ratio; keep first arrival for delay.
            received.setdefault(key, timestamp)
    return received


def read_classic_pcap_stats(path: Path) -> dict[str, int] | None:
    
    if not path.is_file():
        return None

    magic_to_endian = {
        b"\xd4\xc3\xb2\xa1": "<",  # little-endian, microseconds
        b"\xa1\xb2\xc3\xd4": ">",  # big-endian, microseconds
        b"\x4d\x3c\xb2\xa1": "<",  # little-endian, nanoseconds
        b"\xa1\xb2\x3c\x4d": ">",  # big-endian, nanoseconds
    }

    packets = captured_bytes = wire_bytes = 0
    with path.open("rb") as file:
        magic = file.read(4)
        if magic not in magic_to_endian:
            raise ValueError(
                f"{path.name} is not a supported classic PCAP file; "
                "use tcpdump -w output.pcap rather than PCAP-NG"
            )
        endian = magic_to_endian[magic]
        global_header = file.read(20)
        if len(global_header) != 20:
            raise ValueError(f"Truncated PCAP global header: {path}")
        # Version, thiszone, sigfigs, snaplen, linktype.
        struct.unpack(endian + "HHiIII", global_header)

        while True:
            record_header = file.read(16)
            if not record_header:
                break
            if len(record_header) != 16:
                raise ValueError(f"Truncated PCAP packet header: {path}")
            _seconds, _fraction, included_length, original_length = struct.unpack(
                endian + "IIII", record_header
            )
            packet_data = file.read(included_length)
            if len(packet_data) != included_length:
                raise ValueError(f"Truncated PCAP packet data: {path}")
            packets += 1
            captured_bytes += included_length
            wire_bytes += original_length

    return {
        "pcap_packets": packets,
        "pcap_captured_bytes": captured_bytes,
        "pcap_wire_bytes": wire_bytes,
    }


def percentile_nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def mean_ci95(values: list[float]) -> tuple[float | None, float | None]:
    
    n = len(values)
    if n < 2:
        return None, None
    mean = statistics.mean(values)
    std = statistics.stdev(values)
    df = n - 1
    t_value = T_CRITICAL_95.get(df, 1.96)
    margin = t_value * std / math.sqrt(n)
    return mean - margin, mean + margin


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def make_plots(output_dir: Path, delays_ms: list[float], summary: dict[str, Any]) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed; skipping charts (pip install matplotlib)", file=sys.stderr)
        return

    if delays_ms:
        plt.figure()
        plt.hist(delays_ms, bins=20)
        plt.xlabel("One-way application delay (ms)")
        plt.ylabel("Received messages")
        plt.title("HTTP message delay distribution")
        plt.tight_layout()
        plt.savefig(output_dir / "latency_hist.png", dpi=150)
        plt.close()

    plt.figure()
    # This chart compares delay values only; delivery ratio is shown in the title.
    delay_labels = ["Average delay", "P95 delay"]
    delay_values = [summary.get("avg_delay_ms"), summary.get("p95_delay_ms")]
    delay_values = [float(value) if value not in (None, "") else 0.0 for value in delay_values]
    plt.bar(delay_labels, delay_values)
    plt.ylabel("Milliseconds")
    plt.title(f"Delay summary (delivery {summary.get('delivery_ratio_pct', 0):.2f}%)")
    plt.tight_layout()
    plt.savefig(output_dir / "delay_summary.png", dpi=150)
    plt.close()


def analyze_run(run_dir: Path, output_dir: Path, pcap_path: Path | None = None,
                create_plots: bool = True) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    sent = read_sensor_logs(run_dir)
    received = read_receive_log(run_dir / "receive.csv")

    matched_keys = sorted(set(sent).intersection(received))
    delays_ms = [
        (received[key] - sent[key]) / 1_000_000.0
        for key in matched_keys
    ]
    sent_count = len(sent)
    received_count = len(matched_keys)
    missing_count = max(0, sent_count - received_count)
    unmatched_receive_count = len(set(received) - set(sent))
    delivery_ratio = (received_count / sent_count * 100.0) if sent_count else 0.0

    avg_delay = statistics.mean(delays_ms) if delays_ms else None
    p50_delay = percentile_nearest_rank(delays_ms, 0.50)
    p95_delay = percentile_nearest_rank(delays_ms, 0.95)
    ci_low, ci_high = mean_ci95(delays_ms)

    if pcap_path is None:
        pcap_path = run_dir / "capture.pcap"
    pcap_stats = read_classic_pcap_stats(pcap_path)

    summary: dict[str, Any] = {
        "run_dir": str(run_dir),
        "sent_messages": sent_count,
        "received_messages": received_count,
        "missing_messages": missing_count,
        "unmatched_receive_messages": unmatched_receive_count,
        "delivery_ratio_pct": round(delivery_ratio, 6),
        "avg_delay_ms": round(avg_delay, 6) if avg_delay is not None else "",
        "p50_delay_ms": round(p50_delay, 6) if p50_delay is not None else "",
        "p95_delay_ms": round(p95_delay, 6) if p95_delay is not None else "",
        "delay_ci95_low_ms": round(ci_low, 6) if ci_low is not None else "",
        "delay_ci95_high_ms": round(ci_high, 6) if ci_high is not None else "",
        "delay_samples": len(delays_ms),
        "pcap_packets": pcap_stats["pcap_packets"] if pcap_stats else "",
        "pcap_captured_bytes": pcap_stats["pcap_captured_bytes"] if pcap_stats else "",
        "pcap_wire_bytes": pcap_stats["pcap_wire_bytes"] if pcap_stats else "",
    }

    summary_fields = list(summary.keys())
    write_csv(output_dir / "summary.csv", summary_fields, [summary])

    per_sensor_rows: list[dict[str, Any]] = []
    sensor_ids = sorted({key[0] for key in sent})
    for sensor_id in sensor_ids:
        sensor_sent_keys = {key for key in sent if key[0] == sensor_id}
        sensor_received_keys = sensor_sent_keys.intersection(received)
        sensor_delays = [
            (received[key] - sent[key]) / 1_000_000.0
            for key in sorted(sensor_received_keys)
        ]
        count_sent = len(sensor_sent_keys)
        count_received = len(sensor_received_keys)
        per_sensor_rows.append(
            {
                "sensor_id": sensor_id,
                "sent_messages": count_sent,
                "received_messages": count_received,
                "delivery_ratio_pct": round(count_received / count_sent * 100, 6)
                if count_sent else 0,
                "avg_delay_ms": round(statistics.mean(sensor_delays), 6)
                if sensor_delays else "",
                "p95_delay_ms": round(percentile_nearest_rank(sensor_delays, 0.95), 6)
                if sensor_delays else "",
            }
        )
    write_csv(
        output_dir / "per_sensor.csv",
        ["sensor_id", "sent_messages", "received_messages", "delivery_ratio_pct",
         "avg_delay_ms", "p95_delay_ms"],
        per_sensor_rows,
    )

    delay_rows = [
        {
            "sensor_id": key[0],
            "seq": key[1],
            "send_epoch_ns": sent[key],
            "receive_epoch_ns": received[key],
            "delay_ms": round((received[key] - sent[key]) / 1_000_000.0, 6),
        }
        for key in matched_keys
    ]
    write_csv(
        output_dir / "delays.csv",
        ["sensor_id", "seq", "send_epoch_ns", "receive_epoch_ns", "delay_ms"],
        delay_rows,
    )

    if create_plots:
        make_plots(output_dir, delays_ms, summary)

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze one IoT experiment folder")
    parser.add_argument("--run-dir", type=Path, required=True,
                        help="Folder containing sensor_h*.csv, receive.csv and optionally capture.pcap")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Output folder; default <run-dir>/analysis")
    parser.add_argument("--pcap", type=Path, default=None,
                        help="Optional explicit classic PCAP path")
    parser.add_argument("--no-plots", action="store_true")
    args = parser.parse_args()

    output_dir = args.output_dir or (args.run_dir / "analysis")
    summary = analyze_run(args.run_dir, output_dir, args.pcap, not args.no_plots)
    print("Analysis completed.")
    print(f"Sent: {summary['sent_messages']}")
    print(f"Received: {summary['received_messages']}")
    print(f"Delivery ratio: {summary['delivery_ratio_pct']}%")
    print(f"Average delay: {summary['avg_delay_ms']} ms")
    print(f"P95 delay: {summary['p95_delay_ms']} ms")
    print(f"PCAP wire bytes: {summary['pcap_wire_bytes']}")
    print(f"Outputs: {output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
