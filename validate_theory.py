#!/usr/bin/env python3
"""validate_theory.py """
import math
from dataclasses import replace

import numpy as np
from scipy.optimize import brentq

import coap_sim as cs

B_NON, B_CON_OUTLINE = 47, 81


def sim_const_p(p, seed_runs=5, dur=4000.0):
    """CON lien tuc voi p khong doi; tra ve trung binh tren seed_runs lan chay."""
    cfg = replace(cs.Cfg(), phases=((dur, p),), ack_timeout=0.2)  # timeout nho cho chay nhanh;
    # timeout KHONG anh huong cac xac suat/so lan gui, chi anh huong tre.
    att, srv, ack, byt, non = [], [], [], [], []
    for s in range(1, seed_runs + 1):
        import random
        rng = random.Random(s)
        n = 0
        for i in range(20000):
            t = i * 0.05
            deliv, done, nb, a, obs = cs.con_transaction(cfg, rng, t)
            n += 1
            att.append(a)
            srv.append(deliv is not None)
            ack.append(obs[-1][1] == 0)
            byt.append(nb)
            non.append(rng.random() >= p)
    return (np.mean(att), np.mean(srv), np.mean(ack), np.mean(byt), np.mean(non))


print("=== (1) Bang CON: E[N], server nhan, ben gui duoc xac nhan, NON toi noi ===")
print(f"{'p':>4} | {'E[N] sim':>9} {'1/(1-q)':>8} {'chinh xac':>10} | {'server sim':>11} {'1-p^5':>10} |"
      f" {'ack sim':>9} {'1-q^5':>9} | {'NON sim':>8} {'1-p':>6}")
sim_res = {}
for p in (0.01, 0.05, 0.10, 0.20):
    q = 1 - (1 - p) ** 2
    en, srv, ack, byt, non = sim_const_p(p)
    sim_res[p] = (en, byt)
    exact = (1 - q ** 5) / (1 - q)
    print(f"{p * 100:3.0f}% | {en:9.4f} {1 / (1 - q):8.4f} {exact:10.4f} | {srv * 100:10.4f}% {(1 - p ** 5) * 100:9.5f}% |"
          f" {ack * 100:8.4f}% {(1 - q ** 5) * 100:8.4f}% | {non * 100:7.2f}% {(1 - p) * 100:5.1f}%")

print("\n=== (2) Byte/ban tin CON: de cuong 81/(1-p)^2 va cong thuc tinh lai (ACK chi gui khi du lieu toi) ===")
print(f"{'p':>4} | {'sim':>8} {'outline':>9} {'refined':>9} | sai lech outline so voi sim")
for p in (0.01, 0.05, 0.10, 0.20):
    q = 1 - (1 - p) ** 2
    exact_en = (1 - q ** 5) / (1 - q)
    refined = exact_en * (47 + 34 * (1 - p))
    outline = 81 / (1 - p) ** 2
    sim = sim_res[p][1]
    print(f"{p * 100:3.0f}% | {sim:8.2f} {outline:9.2f} {refined:9.2f} | {100 * (outline - sim) / sim:+.1f}%")

print("\n=== (3) Nguong p* (CON re hon NON khi p > p*): C0 = 47 + L*p ; C1 = chi phi CON ===")
print(f"{'L':>6} | {'de cuong':>9} {'p* (81/(1-p)^2)':>16} {'p* (refined)':>14}")
outline_tab = {100: None, 500: 11.1, 1000: 4.1, 2000: 1.9}
for L in (100, 500, 1000, 2000):
    f1 = lambda p: 47 + L * p - 81 / (1 - p) ** 2
    def f2(p):
        q = 1 - (1 - p) ** 2
        return 47 + L * p - (1 - q ** 5) / (1 - q) * (47 + 34 * (1 - p))
    def root(f):
        grid = np.linspace(1e-6, 0.5, 5001)
        v = [f(x) for x in grid]
        for a, b, fa, fb in zip(grid, grid[1:], v, v[1:]):
            if fa < 0 <= fb:
                return brentq(f, a, b)
        return None
    r1, r2 = root(f1), root(f2)
    fmt = lambda r: "khong co" if r is None else f"{r * 100:.2f}%"
    print(f"{L:6d} | {str(outline_tab[L]) + '%' if outline_tab[L] else 'khong co':>9} {fmt(r1):>16} {fmt(r2):>14}")

print("\n=== (4) Gom lo: k* = sqrt(2*lambda*H/alpha), J(k) = H/k + alpha*(k-1)/(2*lambda) ===")
H, alpha = 51, 5
for lam in (20, 100):
    k = math.sqrt(2 * lam * H / alpha)
    J = lambda k_: H / k_ + alpha * (k_ - 1) / (2 * lam)
    print(f"N={lam:3d}: k*={k:5.2f}  J(1)={J(1):.2f}  J(round(k*))={J(round(k)):.2f}  "
          f"overhead/so do: {H}->{H / round(k):.2f} ({100 * (1 - 1 / round(k)):.0f}% giam)  "
          f"cho cho TB={(round(k) - 1) / (2 * lam):.3f}s")
print(f"MTU: so do toi da/goi 1500 B = {(1500 - 40 - 11) // 10} (de cuong ~140)")
