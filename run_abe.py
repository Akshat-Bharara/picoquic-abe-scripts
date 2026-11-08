#!/usr/bin/env python3
"""Steps 8 and 9: CUBIC with ECN + ABE (RFC 8511), and a check of the result.

Same dumbbell and bottleneck as run_ecn.py. The picoquic server runs CUBIC
with "-H A" (ABE: beta = 0.7 on loss, 0.85 on ECN-CE), which needs the ABE
commit in picoquic.

Usage: sudo python3 run_abe.py --picoquic /path/to/picoquic
Run run_ecn.py first so the ECN run can be compared (otherwise it is run here).
Output: plots/cwnd_abe.png, plots/cwnd_ecn_vs_abe.png and a check printed at the end.
"""
import argparse
import os
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from run_ecn import BDP, PLOTS, RESULTS, goodput, load_cwnd, plot, run, topology_down, topology_up


def backoffs(t, cwnd):
    """cwnd after / cwnd before, for every reduction after start-up."""
    return [cwnd[i] / cwnd[i - 1] for i in range(1, len(cwnd))
            if t[i] > 3 and cwnd[i] < 0.95 * cwnd[i - 1]]


def lowest_after_backoff(t, cwnd):
    return min(c for i, c in enumerate(cwnd) if i and t[i] > 3 and c < 0.95 * cwnd[i - 1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--picoquic", required=True, help="picoquic checkout with ABE, built in build/")
    args = ap.parse_args()

    ecn_dir = os.path.join(RESULTS, "ecn")
    topology_up()
    try:
        abe_dir = run("abe", args.picoquic, ecn=True, cc_option="A")
        if not os.path.exists(os.path.join(ecn_dir, "client.txt")):
            ecn_dir = run("ecn", args.picoquic, ecn=True, cc_option="R")
    finally:
        topology_down()

    t_abe, c_abe = load_cwnd(abe_dir)
    t_ecn, c_ecn = load_cwnd(ecn_dir)
    plot(t_abe, c_abe, f"CUBIC with ECN + ABE ({goodput(abe_dir):.1f} Mbit/s)", "cwnd_abe.png",
         color="tab:green")

    plt.figure(figsize=(9, 4))
    plt.plot(t_ecn, c_ecn, color="tab:orange", drawstyle="steps-post", label="ECN (beta 0.7)")
    plt.plot(t_abe, c_abe, color="tab:green", drawstyle="steps-post", label="ECN + ABE (beta 0.85)")
    plt.axhline(BDP, color="gray", ls="--", lw=1, label="BDP")
    plt.xlim(0, 60)
    plt.ylim(0, max(c for x, c in zip(t_abe, c_abe) if x > 2) * 1.2)
    plt.xlabel("time (s)")
    plt.ylabel("cwnd (packets)")
    plt.title("CUBIC with ECN, with and without ABE")
    plt.legend(loc="lower right")
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(os.path.join(PLOTS, "cwnd_ecn_vs_abe.png"), dpi=120)
    plt.close()

    # Step 9: check the cwnd against what RFC 8511 says should happen
    b_ecn, b_abe = backoffs(t_ecn, c_ecn), backoffs(t_abe, c_abe)
    print()
    print(f"{'':12}{'backoffs':>9}{'median cwnd after/before':>26}{'lowest cwnd':>13}{'goodput':>10}")
    for name, b, t, c, d in (("ECN", b_ecn, t_ecn, c_ecn, ecn_dir), ("ECN + ABE", b_abe, t_abe, c_abe, abe_dir)):
        print(f"{name:12}{len(b):>9}{statistics.median(b):>26.3f}{lowest_after_backoff(t, c):>13.0f}"
              f"{goodput(d):>9.1f}M")
    print(f"BDP is {BDP:.0f} packets")
    print()
    ok_abe = abs(statistics.median(b_abe) - 0.85) < 0.02
    ok_ecn = abs(statistics.median(b_ecn) - 0.70) < 0.02
    print(f"ABE backs off to 0.85 x cwnd on ECN-CE: {'yes' if ok_abe else 'NO'}")
    print(f"plain ECN backs off to 0.70 x cwnd:      {'yes' if ok_ecn else 'NO'}")
    print(f"ABE keeps cwnd near the BDP after a backoff: "
          f"{'yes' if lowest_after_backoff(t_abe, c_abe) > 0.9 * BDP else 'NO'}")
    print(f"ABE gets more throughput than plain ECN:     "
          f"{'yes' if goodput(abe_dir) > goodput(ecn_dir) else 'NO'}")
    if not ok_abe:
        print("(is the ABE commit in the picoquic build? without it -H A does nothing)")
    print("plots: plots/cwnd_abe.png, plots/cwnd_ecn_vs_abe.png")


if __name__ == "__main__":
    main()
