#!/usr/bin/env python3
import argparse
import subprocess
import sys


def get_node_pid(node_name: str) -> str | None:
    out = subprocess.check_output(["ps", "-ef"], text=True)
    for line in out.splitlines():
        if f"mininet:{node_name}" in line:
            parts = line.split()
            if len(parts) > 1:
                return parts[1]
    return None


def run_tc(node_name: str, tc_args: list[str], check: bool = True, quiet_err: bool = False) -> None:
    pid = get_node_pid(node_name)
    cmd = ["mnexec", "-a", pid, "tc", *tc_args] if pid else ["tc", *tc_args]
    subprocess.run(
        cmd,
        check=check,
        stderr=subprocess.DEVNULL if quiet_err else None,
    )


def clear_iface(node_name: str, iface: str) -> None:
    run_tc(node_name, ["qdisc", "del", "dev", iface, "root"], check=False, quiet_err=True)


def apply_iface(node_name: str, iface: str, delay_ms: float, loss_pct: float, rate: str | None) -> None:
    clear_iface(node_name, iface)

    netem_cmd = ["netem", "delay", f"{delay_ms}ms"]
    if loss_pct > 0:
        netem_cmd.extend(["loss", f"{loss_pct}%"])

    if rate:
        burst = "4kbit" if rate.lower().startswith("64k") else "32kbit"
        latency = "1000ms" if rate.lower().startswith("64k") else "400ms"
        run_tc(
            node_name,
            ["qdisc", "add", "dev", iface, "root", "handle", "1:", "tbf", "rate", rate, "burst", burst, "latency", latency],
        )
        run_tc(
            node_name,
            ["qdisc", "add", "dev", iface, "parent", "1:1", "handle", "10:", *netem_cmd],
        )
    else:
        run_tc(node_name, ["qdisc", "add", "dev", iface, "root", "handle", "10:", *netem_cmd])


def main() -> None:
    parser = argparse.ArgumentParser(description="Cau hinh netem/tbf 2 chieu tren hg-eth1 <-> r1-eth0")
    parser.add_argument("--if-a", default="hg-eth1", help="Giao dien tren hg (mac dinh: hg-eth1)")
    parser.add_argument("--if-b", default="r1-eth0", help="Giao dien tren r1 (mac dinh: r1-eth0)")
    parser.add_argument("--rtt", type=float, default=0.0, help="Tong RTT (ms); moi chieu = RTT/2")
    parser.add_argument("--loss", type=float, default=0.0, help="Ty le mat goi moi chieu (%%)")
    parser.add_argument("--rate", type=str, default=None, help="Gioi han bang thong (vd: 10mbit, 64kbit)")
    parser.add_argument("--clear", action="store_true", help="Xoa cau hinh tren ca 2 chieu")
    args = parser.parse_args()

    if not get_node_pid("hg") or not get_node_pid("r1"):
        print("[!] Khong tim thay tien trinh mininet:hg hoac mininet:r1. Hay chay topo.py truoc.", file=sys.stderr)
        sys.exit(1)

    if args.clear:
        clear_iface("hg", args.if_a)
        clear_iface("r1", args.if_b)
        print(f"[OK] Da xoa cau hinh tren {args.if_a} (hg) va {args.if_b} (r1)")
        return

    if args.rtt < 0 or args.loss < 0:
        print("[ERR] --rtt va --loss phai >= 0", file=sys.stderr)
        sys.exit(1)

    delay_one_way = args.rtt / 2.0
    apply_iface("hg", args.if_a, delay_one_way, args.loss, args.rate)
    apply_iface("r1", args.if_b, delay_one_way, args.loss, args.rate)
    print(
        f"[OK] {args.if_a} (hg) <-> {args.if_b} (r1) | RTT={args.rtt}ms ({delay_one_way}ms/chieu) "
        f"| loss={args.loss}% | rate={args.rate or 'khong gioi han'}"
    )


if __name__ == "__main__":
    main()
