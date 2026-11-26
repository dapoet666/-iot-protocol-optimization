from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def load_topology_class(topo_file: Path):
    if not topo_file.is_file():
        raise FileNotFoundError(f"Topology file not found: {topo_file}")
    spec = importlib.util.spec_from_file_location("group_topology", topo_file)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load topology module: {topo_file}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if not hasattr(module, "IoTWanTopo"):
        raise AttributeError("topo.py must define class IoTWanTopo")
    return module.IoTWanTopo


def wait_for_tcp(node: Any, port: int, timeout: float = 10.0) -> None:
    
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Run the check inside the server's network namespace.
        marker_code = (
            "import socket; s=socket.socket(); s.settimeout(0.4); "
            f"r=s.connect_ex(('127.0.0.1',{port})); s.close(); "
            "print('READY' if r == 0 else 'WAIT')"
        )
        output = node.cmd(f"python3 -c {shlex.quote(marker_code)}")
        if "READY" in output:
            return
        time.sleep(0.2)
    raise TimeoutError(f"HTTP server did not listen on port {port} within {timeout}s")


def stop_process(proc: Any | None, name: str, timeout: float = 5.0) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"WARNING: force-killing {name}", file=sys.stderr)
        proc.kill()
        proc.wait(timeout=2)
    except Exception as exc:  # cleanup should continue for remaining processes
        print(f"WARNING: could not stop {name}: {exc}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    project_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Run one HTTP experiment on Mininet")
    parser.add_argument("--topo-file", type=Path, default=project_dir / "topo.py")
    parser.add_argument("--sensor-script", type=Path, default=project_dir / "sensor.py")
    parser.add_argument("--server-script", type=Path, default=project_dir / "http_server.py")
    parser.add_argument("--adapter-script", type=Path, default=project_dir / "http_adapter.py")
    parser.add_argument("--scenario", choices=("K1",), default="K1",
                        help="This version automates K1 only; K2 needs wan.py integration")
    parser.add_argument("--n-sensors", type=int, default=20)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--udp-port", type=int, default=5000)
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--mode", choices=("keep-alive", "new"), default="keep-alive")
    parser.add_argument("--jitter", action="store_true")
    parser.add_argument("--capture-interface", default="r1-eth0")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Output directory; default results/K1/<timestamp>")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if os.name != "posix":
        raise SystemExit("Run run_one.py inside the Ubuntu/Linux VM, not Windows PowerShell.")
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise SystemExit("Run with sudo: sudo python3 run_one.py ...")
    if not 10 <= args.n_sensors <= 100:
        raise SystemExit("--n-sensors must be between 10 and 100 for the supplied topology")
    if args.interval <= 0 or args.duration <= 0:
        raise SystemExit("--interval and --duration must be positive for this experiment")
    ratio = args.duration / args.interval
    if abs(ratio - round(ratio)) > 1e-9:
        raise SystemExit("--duration must be an integer multiple of --interval")
    if not 1 <= args.udp_port <= 65535 or not 1 <= args.http_port <= 65535:
        raise SystemExit("UDP and HTTP ports must be between 1 and 65535")

    topo_file = args.topo_file.resolve()
    sensor_script = args.sensor_script.resolve()
    server_script = args.server_script.resolve()
    adapter_script = args.adapter_script.resolve()
    for required in (topo_file, sensor_script, server_script, adapter_script):
        if not required.is_file():
            raise SystemExit(f"Required file not found: {required}")

    if args.output_dir is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        output_dir = Path("results") / args.scenario / f"http_{args.mode}_{stamp}"
    else:
        output_dir = args.output_dir
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Mininet is available on the Ubuntu host, not ordinary Windows PowerShell.
    try:
        from mininet.net import Mininet
    except ImportError as exc:
        raise SystemExit(
            "Mininet is not installed in this Python environment. Run this script "
            "inside the Ubuntu VM with Mininet installed."
        ) from exc

    # Ensure local imports from the selected topology directory work.
    sys.path.insert(0, str(topo_file.parent))
    Topology = load_topology_class(topo_file)

    net = None
    server_proc = adapter_proc = capture_proc = None
    server_log = adapter_log = capture_log = None
    sensor_processes: list[tuple[Any, Any]] = []
    run_error: BaseException | None = None

    try:
        from mininet.log import setLogLevel
        setLogLevel("warning")

        print(f"Starting topology with {args.n_sensors} sensors...")
        topo = Topology(n_sensors=args.n_sensors)
        net = Mininet(topo=topo, controller=None, autoSetMacs=True)
        net.start()

        gateway = net.get("hg")
        router = net.get("r1")
        server_node = net.get("server")

        # These are the same static routes configured in the supplied topo.py.
        gateway.cmd("ip route replace 10.0.3.0/24 via 10.0.2.2")
        router.cmd("ip route replace 10.0.1.0/24 via 10.0.2.1")

        # This version is K1: no netem/tbf impairment is applied here.
        metadata = {
            "scenario": args.scenario,
            "protocol": "HTTP/1.1",
            "http_mode": args.mode,
            "n_sensors": args.n_sensors,
            "duration_seconds": args.duration,
            "interval_seconds": args.interval,
            "expected_messages_per_sensor": round(args.duration / args.interval),
            "udp_gateway": "10.0.1.1",
            "udp_port": args.udp_port,
            "http_server": "10.0.3.2",
            "http_port": args.http_port,
            "jitter": args.jitter,
            "capture_interface": args.capture_interface,
            "note": "K1 good network; WAN shaping is not configured by this initial run_one.py",
        }
        (output_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        server_log = open(output_dir / "server.log", "w", encoding="utf-8")
        server_proc = server_node.popen(
            [sys.executable, str(server_script), "--host", "0.0.0.0",
             "--port", str(args.http_port), "--log", str(output_dir / "receive.csv")],
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        wait_for_tcp(server_node, args.http_port)
        if server_proc.poll() is not None:
            raise RuntimeError("HTTP server exited during startup; inspect server.log")

        # Capture the gateway-to-router WAN side. This records forwarded HTTP/TCP.
        capture_log = open(output_dir / "tcpdump.log", "w", encoding="utf-8")
        capture_proc = router.popen(
            ["tcpdump", "-U", "-i", args.capture_interface, "-s", "0", "-w",
             str(output_dir / "capture.pcap"), f"tcp port {args.http_port}"],
            stdout=subprocess.DEVNULL,
            stderr=capture_log,
        )
        time.sleep(0.5)
        if capture_proc.poll() is not None:
            raise RuntimeError("tcpdump exited; check tcpdump installation and interface name")

        adapter_log = open(output_dir / "adapter-process.log", "w", encoding="utf-8")
        ready_file = output_dir / "adapter.ready"
        adapter_proc = gateway.popen(
            [sys.executable, str(adapter_script),
             "--udp-host", "0.0.0.0", "--udp-port", str(args.udp_port),
             "--server-host", "10.0.3.2", "--server-port", str(args.http_port),
             "--mode", args.mode, "--log", str(output_dir / "adapter.csv"),
             "--ready-file", str(ready_file)],
            stdout=adapter_log,
            stderr=subprocess.STDOUT,
        )
        ready_deadline = time.monotonic() + 10.0
        while time.monotonic() < ready_deadline and not ready_file.exists():
            if adapter_proc.poll() is not None:
                raise RuntimeError("HTTP adapter exited during startup; inspect adapter-process.log")
            time.sleep(0.1)
        if not ready_file.exists():
            raise TimeoutError("HTTP adapter did not bind its UDP port within 10 seconds")

        expected_per_sensor = round(args.duration / args.interval)
        print(f"Starting {args.n_sensors} sensor processes...")
        for sensor_id in range(1, args.n_sensors + 1):
            sensor_node = net.get(f"h{sensor_id}")
            sensor_err = open(output_dir / f"sensor_h{sensor_id}.err", "w", encoding="utf-8")
            cmd = [
                sys.executable, str(sensor_script),
                "--id", str(sensor_id),
                "--gateway", "10.0.1.1",
                "--port", str(args.udp_port),
                "--interval", str(args.interval),
                "--duration", str(args.duration),
                "--log-dir", str(output_dir),
            ]
            if args.jitter:
                cmd.append("--jitter")
            proc = sensor_node.popen(cmd, stdout=subprocess.DEVNULL, stderr=sensor_err)
            sensor_processes.append((proc, sensor_err))

        deadline = time.monotonic() + args.duration + 30.0
        for index, (proc, _err) in enumerate(sensor_processes, start=1):
            remaining = max(1.0, deadline - time.monotonic())
            try:
                return_code = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError(f"Sensor process h{index} did not finish before timeout") from exc
            if return_code != 0:
                raise RuntimeError(
                    f"Sensor process h{index} exited with code {return_code}; "
                    f"inspect sensor_h{index}.err"
                )

        # Give the single-threaded UDP adapter time to forward its remaining queue.
        time.sleep(1.0)

        # Summarize how many datagrams the adapter actually received/handled.
        adapter_rows = 0
        adapter_csv = output_dir / "adapter.csv"
        if adapter_csv.exists():
            with adapter_csv.open("r", newline="", encoding="utf-8") as file:
                adapter_rows = max(0, sum(1 for _ in csv.reader(file)) - 1)

        print("Experiment finished.")
        print(f"Output directory: {output_dir}")
        print(f"Expected sensor sends: {args.n_sensors * expected_per_sensor}")
        print(f"UDP datagrams logged by adapter: {adapter_rows}")

    except BaseException as exc:
        run_error = exc
    finally:
        # Stop application processes first, then capture, then the Mininet network.
        stop_process(adapter_proc, "HTTP adapter")
        stop_process(server_proc, "HTTP server")
        stop_process(capture_proc, "tcpdump")
        for index, (proc, err_file) in enumerate(sensor_processes, start=1):
            stop_process(proc, f"sensor h{index}")
            try:
                err_file.close()
            except Exception:
                pass
        for file_obj in (server_log, adapter_log, capture_log):
            if file_obj is not None:
                try:
                    file_obj.close()
                except Exception:
                    pass
        if net is not None:
            try:
                net.stop()
            except Exception as exc:
                print(f"WARNING: Mininet cleanup failed: {exc}", file=sys.stderr)

    if run_error is not None:
        raise run_error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
