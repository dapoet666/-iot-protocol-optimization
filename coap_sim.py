#!/usr/bin/env python3
"""
coap_sim.py - Mo phong Monte Carlo cho CoAP NON/CON tren WAN mat goi 

THIET KE (Buoc 1)
-----------------
Kich ban K7: N = 20 cam bien, moi cam bien 1 ban tin/s (lech pha ngau nhien) -> gateway
nhan lambda = 20 ban tin/s. Gateway gui len server bang CoAP. Mang WAN 3 pha, mat goi
p = 1% -> 15% -> 1% (moi pha 300 s), ap dung doc lap cho MOI goi o ca hai chieu.
RTT mac dinh 100 ms (mot chieu 50 ms). Chang LAN cam bien -> gateway khong mat, bo qua.

Mo hinh goi (khop de cuong muc 2.5b, chua tinh Ethernet):
  - Goi du lieu (NON hoac CON) : 20 (IP) + 8 (UDP) + 4 (header CoAP) + 2 (token)
                                 + 2 (option Uri-Path "t") + 1 (marker 0xFF) + 10 = 47 byte
  - ACK (CON, piggyback 2.04)  : 20 + 8 + 4 + 2 = 34 byte
CON: toi da 1 + MAX_RETRANSMIT(4) = 5 lan gui; timeout dau = ACK_TIMEOUT * U(1, 1.5),
nhan doi sau moi lan. Server loai ban sao theo Message ID (tinh 1 lan nhan).
NON: khong ACK, khong gui lai.

Uoc luong chat luong mang (muc 2.3d): moi LAN GUI CON la 1 phep thu Bernoulli
x = 1 (khong co ACK truoc timeout) / 0 (co ACK); xac suat that bai la
q = 1 - (1-p)^2. EWMA tren q, roi p_hat = 1 - sqrt(1 - q_hat).
O che do NON, gateway gui xen probe_frac ban tin duoi dang CON lam thu (probe).

4 chinh sach (muc 2.3g):
  a) always_non : luon NON
  b) always_con : luon CON
  c) threshold  : quy tac don gian p_hat > eps -> CON, khong EWMA, khong hysteresis,
                  p_hat tinh tren cua so truot W lan thu gan nhat   [GIA DINH - can xac nhan]
  d) adaptive   : EWMA(beta) + hysteresis (eps +/- h) + thoi gian dung toi thieu m lan do
                  + probe. eps = 1 - T.

Tuy chon NSTART: slots = 0 -> khong gioi han so giao dich CON dong thoi
(moi cam bien mot "ket noi"); slots = 1 -> NSTART = 1 nghiem ngat (hang doi FIFO).

Chay:  python3 coap_sim.py --seeds 30 --out k7_summary.csv
"""
import argparse
import csv
import heapq
import math
import random
from collections import deque
from dataclasses import dataclass, replace

import numpy as np
from scipy import stats

B_DATA = 47   # byte goi du lieu CoAP (IP+UDP+CoAP+payload 10)
B_ACK = 34    # byte ACK piggyback


@dataclass(frozen=True)
class Cfg:
    n_sensors: int = 20
    period: float = 1.0
    phases: tuple = ((300.0, 0.01), (300.0, 0.15), (300.0, 0.01))
    rtt: float = 0.100
    ack_timeout: float = 2.0
    ack_random: float = 1.5
    max_retx: int = 4
    slots: int = 0
    t_d: float = 2.0
    T: float = 0.95
    beta: float = 0.02
    h: float = 0.015
    m: int = 20
    probe_frac: float = 0.05
    window: int = 50

    @property
    def eps(self):
        return 1.0 - self.T

    @property
    def duration(self):
        return sum(d for d, _ in self.phases)

    def p_at(self, t):
        acc = 0.0
        for d, p in self.phases:
            acc += d
            if t < acc:
                return p
        return self.phases[-1][1]

    def phase_of(self, t):
        acc = 0.0
        for i, (d, _) in enumerate(self.phases):
            acc += d
            if t < acc:
                return i
        return len(self.phases) - 1


def q_to_p(q):
    return 1.0 - math.sqrt(max(0.0, 1.0 - q))


# ---------------------------------------------------------------- chinh sach
class Fixed:
    uses_probes = False

    def __init__(self, mode):
        self.mode = mode
        self.switches = 0

    def observe(self, x):
        pass


class Threshold:
    uses_probes = True

    def __init__(self, cfg):
        self.cfg = cfg
        self.buf = deque(maxlen=cfg.window)
        self.mode = "NON"
        self.switches = 0

    def observe(self, x):
        self.buf.append(x)
        if len(self.buf) < self.cfg.window:
            return
        p = q_to_p(sum(self.buf) / len(self.buf))
        new = "CON" if p > self.cfg.eps else "NON"
        if new != self.mode:
            self.mode = new
            self.switches += 1


class Adaptive:
    uses_probes = True

    def __init__(self, cfg):
        self.cfg = cfg
        self.q = 0.0
        self.mode = "NON"
        self.cnt = 0
        self.switches = 0

    @property
    def p_hat(self):
        return q_to_p(self.q)

    def observe(self, x):
        c = self.cfg
        self.q = (1 - c.beta) * self.q + c.beta * x
        p = self.p_hat
        cond = p > c.eps + c.h if self.mode == "NON" else p < c.eps - c.h
        self.cnt = self.cnt + 1 if cond else 0
        if self.cnt >= c.m:
            self.mode = "CON" if self.mode == "NON" else "NON"
            self.cnt = 0
            self.switches += 1


def make_policy(name, cfg):
    return {
        "always_non": lambda: Fixed("NON"),
        "always_con": lambda: Fixed("CON"),
        "threshold": lambda: Threshold(cfg),
        "adaptive": lambda: Adaptive(cfg),
    }[name]()


POLICIES = ["always_non", "always_con", "threshold", "adaptive"]


# ---------------------------------------------------------------- giao dich
def con_transaction(cfg, rng, start):
    """Mo phong mot giao dich CON. Tra ve (t_server_nhan|None, t_xong, byte, so_lan_gui, obs)."""
    timeout = cfg.ack_timeout * rng.uniform(1.0, cfg.ack_random)
    t = start
    delivered = None
    nbytes = 0
    obs = []
    for a in range(cfg.max_retx + 1):
        nbytes += B_DATA
        if rng.random() >= cfg.p_at(t):               # goi du lieu toi server
            arr = t + cfg.rtt / 2
            if delivered is None:
                delivered = arr
            nbytes += B_ACK                            # server tra ACK (ca khi la ban sao)
            if rng.random() >= cfg.p_at(arr):         # ACK ve duoc gateway
                done = arr + cfg.rtt / 2
                obs.append((done, 0))
                return delivered, done, nbytes, a + 1, obs
        t_fail = t + timeout
        obs.append((t_fail, 1))
        t = t_fail
        timeout *= 2
    return delivered, t, nbytes, cfg.max_retx + 1, obs


def run_once(cfg, policy_name, seed):
    rng = random.Random(seed * 7919 + POLICIES.index(policy_name))
    arr_rng = random.Random(seed)
    arrivals = []
    for _ in range(cfg.n_sensors):
        t = arr_rng.random() * cfg.period
        while t < cfg.duration:
            arrivals.append(t)
            t += cfg.period
    arrivals.sort()

    pol = make_policy(policy_name, cfg)
    pending = []
    free = [0.0] * cfg.slots if cfg.slots else None
    rec = []   # (phase, kind, delay|None, bytes, attempts)
    for t in arrivals:
        while pending and pending[0][0] <= t:
            pol.observe(heapq.heappop(pending)[1])
        kind = pol.mode
        if kind == "NON" and pol.uses_probes and rng.random() < cfg.probe_frac:
            kind = "PROBE"
        if kind in ("CON", "PROBE"):
            start = t
            if free is not None:
                start = max(t, heapq.heappop(free))
            deliv, done, nb, att, obs = con_transaction(cfg, rng, start)
            if free is not None:
                heapq.heappush(free, done)
            if pol.uses_probes:
                for o in obs:
                    heapq.heappush(pending, o)
            delay = None if deliv is None else deliv - t
        else:
            nb, att = B_DATA, 1
            delay = None if rng.random() < cfg.p_at(t) else t + cfg.rtt / 2 - t
        rec.append((cfg.phase_of(t), kind, delay, nb, att))
    return summarize(cfg, rec, pol)


def summarize(cfg, rec, pol):
    out = {}
    scopes = [("all", rec)] + [(f"phase{i + 1}", [r for r in rec if r[0] == i])
                               for i in range(len(cfg.phases))]
    for name, rs in scopes:
        n = len(rs)
        d = np.array([r[2] for r in rs if r[2] is not None])
        out[f"{name}.delivered"] = len(d) / n
        out[f"{name}.in_time"] = float(np.sum(d <= cfg.t_d)) / n
        out[f"{name}.delay_mean_s"] = float(d.mean()) if len(d) else float("nan")
        out[f"{name}.delay_p95_s"] = float(np.percentile(d, 95)) if len(d) else float("nan")
        out[f"{name}.bytes_per_msg"] = sum(r[3] for r in rs) / n
        out[f"{name}.retx_per_msg"] = sum(r[4] - 1 for r in rs) / n
        out[f"{name}.con_share"] = sum(1 for r in rs if r[1] == "CON") / n
    out["switches"] = pol.switches
    return out


def ci95(vals):
    v = np.array(vals, dtype=float)
    v = v[~np.isnan(v)]
    if len(v) < 2:
        return float(v.mean()) if len(v) else float("nan"), float("nan")
    h = stats.t.ppf(0.975, len(v) - 1) * v.std(ddof=1) / math.sqrt(len(v))
    return float(v.mean()), float(h)


def run_experiment(cfg, seeds, policies=POLICIES):
    table = {}
    for pol in policies:
        runs = [run_once(cfg, pol, s) for s in range(1, seeds + 1)]
        table[pol] = {k: ci95([r[k] for r in runs]) for k in runs[0]}
    return table


def print_table(table, title):
    print(f"\n=== {title} ===")
    hdr = f"{'policy':<11}{'scope':<8}{'delivered':>16}{'in_time(2s)':>16}{'delay_mean(s)':>16}{'delay_p95(s)':>15}{'bytes/msg':>14}{'CON share':>12}"
    print(hdr)
    for pol, m in table.items():
        for sc in ("all", "phase1", "phase2", "phase3"):
            def f(k, pct=False):
                mu, h = m[f"{sc}.{k}"]
                return f"{mu * 100:6.2f}±{h * 100:4.2f}%" if pct else f"{mu:7.3f}±{h:5.3f}"
            print(f"{pol:<11}{sc:<8}{f('delivered', True):>16}{f('in_time', True):>16}"
                  f"{f('delay_mean_s'):>16}{f('delay_p95_s'):>15}{f('bytes_per_msg'):>14}"
                  f"{m[sc + '.con_share'][0] * 100:11.1f}%")
        print(f"{'':<11}switches = {m['switches'][0]:.1f} ± {m['switches'][1]:.1f}")


def write_csv(table, path, tag):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["tag", "policy", "metric", "mean", "ci95_halfwidth"])
        for pol, m in table.items():
            for k, (mu, h) in m.items():
                w.writerow([tag, pol, k, f"{mu:.6g}", f"{h:.6g}"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=30)
    ap.add_argument("--ack-timeout", type=float, default=2.0)
    ap.add_argument("--slots", type=int, default=0)
    ap.add_argument("--rtt", type=float, default=0.1)
    ap.add_argument("--beta", type=float, default=0.02)
    ap.add_argument("--probe-frac", type=float, default=0.05)
    ap.add_argument("--tag", default="base")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    cfg = replace(Cfg(), ack_timeout=a.ack_timeout, slots=a.slots, rtt=a.rtt,
                  beta=a.beta, probe_frac=a.probe_frac)
    tb = run_experiment(cfg, a.seeds)
    print_table(tb, f"K7 [{a.tag}] seeds={a.seeds} ack_timeout={a.ack_timeout}s slots={a.slots}")
    if a.out:
        write_csv(tb, a.out, a.tag)
