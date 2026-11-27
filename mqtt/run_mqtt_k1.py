#!/usr/bin/env python3
"""A3/A4/A5: MQTT QoS 0/1 K1 experiment on Ubuntu + Mininet.
Only produces measurements when executed on a real Linux Mininet host.
From repo root: sudo /path/to/venv/bin/python mqtt/run_mqtt_k1.py --qos both --repeats 10
"""
import argparse
import csv
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]

def load_topology():
    p = ROOT / "topology/topo.py"
    s = importlib.util.spec_from_file_location("group11_topology", p)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m.IoTWanTopo

def wait_ready(test, label, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if test():
            return
        time.sleep(.2)
    raise TimeoutError("Not ready: " + label)

def has_text(path, text):
    return path.exists() and text in path.read_text(errors="replace")

def stop(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)

def read_csv(path):
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))

def experiment(args, qos, repeat):
    from mininet.net import Mininet
    from mininet.log import setLogLevel
    setLogLevel("warning")
    output = args.output.resolve() / f"qos{qos}_r{repeat:02d}_{datetime.now():%Y%m%d_%H%M%S_%f}"
    output.mkdir(parents=True)
    procs, files = [], []
    net = None
    succeeded = False
    def launch(node, command, name):
        log = (output / name).open("w")
        files.append(log)
        proc = node.popen(command, stdout=log, stderr=subprocess.STDOUT)
        procs.append(proc)
        return proc
    try:
        net = Mininet(topo=load_topology()(n_sensors=args.sensors),
                      controller=None, autoSetMacs=True)
        net.start()
        hg, r1, server = (net.get(n) for n in ("hg", "r1", "server"))
        hg.cmd("ip route replace 10.0.3.0/24 via 10.0.2.2")
        r1.cmd("ip route replace 10.0.1.0/24 via 10.0.2.1")
        ping = net.get("h1").cmd("ping -c 1 -W 2 10.0.3.2")
        if "1 received" not in ping and "1 packets received" not in ping:
            raise RuntimeError("Routing check failed: " + ping)

        # K1: 20 ms target RTT = 10 ms in each direction, 0% loss, 10 Mbps.
        for node, interface in ((hg, "hg-eth1"), (r1, "r1-eth0")):
            output_tc = node.cmd(f"tc qdisc replace dev {interface} root netem delay 10ms loss 0% rate 10mbit; echo EXIT:$?")
            if "EXIT:0" not in output_tc:
                raise RuntimeError(f"tc failed on {interface}: {output_tc}")

        broker = launch(server, ["mosquitto", "-c", str(ROOT / "mqtt/mosq.conf"), "-v"], "broker.log")
        def broker_ready():
            code = "import socket;s=socket.socket();s.settimeout(.4);print(s.connect_ex(('127.0.0.1',1883))==0);s.close()"
            return broker.poll() is None and server.cmd("python3 -c " + shlex.quote(code)).strip().endswith("True")
        wait_ready(broker_ready, "Mosquitto")
        subscriber = launch(server, [sys.executable, "-u", str(ROOT / "mqtt/srv_mqtt.py"),
                          "--broker", "127.0.0.1", "--out", str(output / "server.csv")], "subscriber.log")
        wait_ready(lambda: subscriber.poll() is None and has_text(output / "subscriber.log", "Ready; writing"), "MQTT subscriber")
        gateway = launch(hg, [sys.executable, "-u", str(ROOT / "mqtt/gw_mqtt.py"),
                           "--server", "10.0.3.2", "--qos", str(qos)], "gateway.log")
        wait_ready(lambda: gateway.poll() is None and has_text(output / "gateway.log", "Receiving sensor UDP"), "MQTT gateway")
        capture = launch(r1, ["tcpdump", "-U", "-i", "r1-eth0", "-s", "0",
                             "-w", str(output / "mqtt.pcap"), "tcp", "port", "1883"], "capture.log")
        time.sleep(.7)
        if capture.poll() is not None:
            raise RuntimeError("tcpdump failed; check capture.log")
        sensors = []
        for i in range(1, args.sensors + 1):
            sensors.append(launch(net.get(f"h{i}"), [
                sys.executable, "-u", str(ROOT / "sensor.py"),
                "--id", str(i), "--gateway", "10.0.1.1", "--port", "5000",
                "--duration", str(args.duration), "--interval", str(args.interval),
                "--log-dir", str(output), "--jitter"], f"sensor_h{i}.err"))
        for i, sensor in enumerate(sensors, 1):
            if sensor.wait(timeout=args.duration + 40) != 0:
                raise RuntimeError(f"Sensor h{i} failed")
        time.sleep(2)
        sent = set()
        for f in output.glob("sensor_h*.csv"):
            sent.update((int(r["sensor_id"]), int(r["seq"])) for r in read_csv(f))
        received_rows = read_csv(output / "server.csv")
        received = {(int(r["sensor_id"]), int(r["seq"])) for r in received_rows}
        matched = len(sent & received)
        summary = {
            "scenario": "K1", "qos": qos, "repeat": repeat,
            "sensor_count": args.sensors, "duration_s": args.duration,
            "rtt_target_ms": 20, "loss_percent_each_direction": 0,
            "bandwidth_each_direction": "10mbit",
            "sent_unique": len(sent), "received_rows": len(received_rows),
            "received_unique": len(received), "matched": matched,
            "delivery_ratio": matched / len(sent) if sent else None,
            "duplicate_rows": len(received_rows) - len(received),
            "pcap": str(output / "mqtt.pcap"),
            "warning": "Actual RTT and throughput must be measured separately; targets are configuration values."
        }
        (output / "summary.json").write_text(json.dumps(summary, indent=2))
        succeeded = True
        print(json.dumps(summary, indent=2))
        return summary
    finally:
        for p in reversed(procs):
            stop(p)
        for f in files:
            f.close()
        if net is not None:
            net.stop()
        if not succeeded:
            (output / "FAILED.txt").write_text("Incomplete run; inspect logs. Not a valid measurement.\n")

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--qos", choices=["0", "1", "both"], default="both")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--sensors", type=int, default=20)
    p.add_argument("--duration", type=float, default=60)
    p.add_argument("--interval", type=float, default=1)
    p.add_argument("--output", type=Path, default=ROOT / "results/mqtt/K1")
    args = p.parse_args()
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        p.error("Run with sudo on Ubuntu/Mininet")
    if not 10 <= args.sensors <= 100 or args.repeats < 1 or args.duration <= 0 or args.interval <= 0:
        p.error("Invalid sensors/repeats/duration/interval")
    if abs(args.duration / args.interval - round(args.duration / args.interval)) > 1e-9:
        p.error("Duration must be an integer multiple of interval")
    for executable in ("mosquitto", "tcpdump", "tc", "ovs-vsctl"):
        if not shutil.which(executable):
            p.error("Missing executable: " + executable)
    try:
        import mininet
        import paho.mqtt.client
    except ImportError as e:
        p.error("Missing Python dependency: " + str(e))
    results = []
    try:
        for qos in ([0, 1] if args.qos == "both" else [int(args.qos)]):
            for run in range(1, args.repeats + 1):
                results.append(experiment(args, qos, run))
    except Exception as e:
        print("Experiment failed: " + str(e), file=sys.stderr)
        return 1
    dest = args.output.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    with (dest / "index.csv").open("w", newline="") as stream:
        fields = ["scenario", "qos", "repeat", "sent_unique", "received_unique",
                  "matched", "delivery_ratio", "duplicate_rows"]
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    print("Result index:", dest / "index.csv")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
