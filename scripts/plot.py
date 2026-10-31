#!/usr/bin/env python3
"""Plot cwnd traces and write a results summary.

Usage: plot.py   (reads results/<scenario>/..., writes plots/*.png and results/summary.md)
"""
import csv
import os
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "results")
PLOTS = os.path.join(ROOT, "plots")
MSS = 1440                      # picoquic send_mtu on this path
BDP_PKTS = 50e6 * 0.080 / 8 / MSS

# colors
C = {"blue": "#2a78d6", "orange": "#eb6834", "aqua": "#1baf7a", "yellow": "#eda100"}
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "text.color": INK, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False, "font.size": 10,
    "axes.titlesize": 11, "axes.titleweight": "bold", "lines.linewidth": 1.4,
})

# Color follows the scenario in every figure
SCOLOR = {"noecn": C["blue"], "ecn": C["orange"], "abe": C["aqua"], "stock_noecn": C["blue"],
          "stock_ecn": C["yellow"], "ecn_2flows": C["orange"], "abe_2flows": C["aqua"]}


TITLES = {
    "noecn": "CUBIC, no ECN (CoDel drops)",
    "ecn": "CUBIC + ECN, classic response (β = 0.7 on CE)",
    "abe": "CUBIC + ECN + ABE (β_ecn = 0.85)",
    "stock_noecn": "Stock picoquic CUBIC (β = 7/8), no ECN",
    "stock_ecn": "Stock picoquic CUBIC (β = 7/8), ECN",
    "ecn_2flows": "Two flows, ECN, classic response",
    "abe_2flows": "Two flows, ECN + ABE",
}


def read_csv(path, skip_comments=True):
    with open(path) as f:
        lines = [l for l in f if not (skip_comments and l.startswith("#"))]
    return list(csv.DictReader(lines))


def load_flow(d):
    rows = read_csv(os.path.join(d, "cwnd.csv"))
    t = [float(r["time_s"]) for r in rows]
    cw = [int(r["cwnd_bytes"]) / MSS for r in rows]
    rtt = [float(r["smoothed_rtt_ms"]) for r in rows]
    ev = read_csv(os.path.join(d, "events.csv"))
    ce = [float(e["time_s"]) for e in ev if e["event"] == "ce_echo"]
    loss = [float(e["time_s"]) for e in ev if e["event"] == "loss"]
    ref = int(open(os.path.join(d, "meta.txt")).read().split("=")[1])
    return dict(t=t, cwnd=cw, rtt=rtt, ce=ce, loss=loss, ref=ref)


def load_queue(sdir, ref_us):
    path = os.path.join(sdir, "queue.csv")
    with open(path) as f:
        t0 = int(f.readline().split("=")[1])
    rows = read_csv(path)
    off = (t0 - ref_us) / 1e6       # bneck time -> flow-1 time
    s = [(float(r["time"]) + off, float(r["sojourn_ms"])) for r in rows if r["event"] == "sample"]
    marks = [float(r["time"]) + off for r in rows if r["event"] == "mark"]
    drops = [float(r["time"]) + off for r in rows if r["event"] in ("drop", "taildrop")]
    return s, marks, drops


def flows_of(sdir):
    out = []
    i = 1
    while os.path.exists(os.path.join(sdir, f"flow{i}", "cwnd.csv")):
        out.append(load_flow(os.path.join(sdir, f"flow{i}")))
        i += 1
    return out


def backoffs(f, t_min=3.0):
    """Multiplicative decreases seen in the cwnd trace: (time, ratio).
    A decrease is a drop of more than 5 % between consecutive samples,
    after the start-up phase."""
    out = []
    t, c = f["t"], f["cwnd"]
    for i in range(1, len(c)):
        if t[i] > t_min and c[i] < 0.95 * c[i - 1] and c[i] > 2:
            out.append((t[i], c[i] / c[i - 1]))
    return out


def throughput(sdir, i):
    for line in open(os.path.join(sdir, f"flow{i}", "client.txt")):
        if line.startswith("Received") and "Mbps" in line:
            return float(line.split(",")[1].split()[0]), float(line.split(" in ")[1].split()[0])
    return None, None


def plot_scenario(name, tmax=60):
    sdir = os.path.join(RES, name)
    flows = flows_of(sdir)
    if not flows:
        return
    q, marks, drops = load_queue(sdir, flows[0]["ref"])
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True,
                                 gridspec_kw={"height_ratios": [3, 1.3]})
    cols = [C["blue"], C["orange"]]
    for i, f in enumerate(flows):
        off = (f["ref"] - flows[0]["ref"]) / 1e6
        a1.plot([x + off for x in f["t"]], f["cwnd"], color=cols[i], label=f"flow {i + 1} cwnd",
                drawstyle="steps-post")
    a1.axhline(BDP_PKTS, color=INK2, lw=1, ls="--")
    a1.text(tmax * 0.995, BDP_PKTS, "BDP ", ha="right", va="bottom", color=INK2, fontsize=9)
    ymax = max(max(c for t, c in zip(f["t"], f["cwnd"]) if t > 2) for f in flows) * 1.15
    a1.set_ylim(0, ymax)
    sig = marks if marks else drops
    a1.plot(sig, [ymax * 0.98] * len(sig), "|", color=C["aqua"] if marks else C["yellow"], ms=6,
            label="CE mark at bottleneck" if marks else "drop at bottleneck")
    a1.set_ylabel("cwnd (packets of 1440 B)")
    a1.set_title(TITLES.get(name, name), loc="left")
    if len(flows) > 1 or sig:
        a1.legend(loc="upper left", frameon=False, ncol=3)
    a2.plot([x for x, _ in q], [y for _, y in q], color=INK2, lw=1)
    a2.set_ylabel("queue delay (ms)")
    a2.set_xlabel("time (s)")
    a2.set_xlim(0, tmax)
    a2.set_ylim(bottom=0)
    fig.tight_layout()
    fig.savefig(os.path.join(PLOTS, f"cwnd_{name}.png"), dpi=130)
    plt.close(fig)


def plot_compare(names, out, tmax=60, zoom=None):
    present = [n for n in names if os.path.exists(os.path.join(RES, n, "flow1", "cwnd.csv"))]
    fig, axes = plt.subplots(len(present), 1, figsize=(10, 2.4 * len(present) + 0.6), sharex=True, sharey=True)
    if len(present) == 1:
        axes = [axes]
    for ax, n in zip(axes, present):
        col = SCOLOR[n]
        f = flows_of(os.path.join(RES, n))[0]
        ax.plot(f["t"], f["cwnd"], color=col, drawstyle="steps-post")
        ax.axhline(BDP_PKTS, color=INK2, lw=1, ls="--")
        ax.set_title(TITLES[n], loc="left", fontsize=10)
        ax.set_ylabel("cwnd (pkts)")
        bo = [r for t, r in backoffs(f) if not zoom or zoom[0] <= t <= zoom[1]]
        if bo:
            ax.text(0.995, 0.04, f"median backoff ratio {statistics.median(bo):.2f}",
                    transform=ax.transAxes, ha="right", va="bottom", color=INK2, fontsize=9)
    axes[-1].set_xlabel("time (s)")
    axes[-1].set_xlim(*(zoom or (0, tmax)))
    top = max(max(c for t, c in zip(ff["t"], ff["cwnd"]) if t > 2)
              for ff in (flows_of(os.path.join(RES, n))[0] for n in present))
    axes[0].set_ylim(0, top * 1.1)
    fig.tight_layout()
    fig.savefig(os.path.join(PLOTS, out), dpi=130)
    plt.close(fig)


def summary(names):
    lines = ["| scenario | flow | goodput (Mbit/s) | duration (s) | backoffs | median ratio | min/max ratio "
             "| CE marks | AQM drops | tail drops | median queue delay (ms) | mean sRTT (ms) |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for n in names:
        sdir = os.path.join(RES, n)
        flows = flows_of(sdir) if os.path.isdir(sdir) else []
        if not flows:
            continue
        stats = dict(ce=0, drop=0, tail=0)
        for line in open(os.path.join(sdir, "bneck.txt")):
            if line.startswith("bneck fwd"):
                kv = dict(x.split("=") for x in line.split()[2:])
                stats = dict(ce=kv["ce_marked"], drop=kv["aqm_drop"], tail=kv["taildrop"])
        q, _, _ = load_queue(sdir, flows[0]["ref"])
        qd = statistics.median([y for t, y in q if 5 < t < 55] or [0])
        for i, f in enumerate(flows):
            gp, dur = throughput(sdir, i + 1)
            bo = [r for _, r in backoffs(f)]
            rtts = [r for t, r in zip(f["t"], f["rtt"]) if t > 5]
            lines.append(f"| {n} | {i + 1} | {gp:.1f} | {dur:.1f} | {len(bo)} | "
                         f"{statistics.median(bo) if bo else float('nan'):.3f} | "
                         f"{(min(bo) if bo else 0):.3f}/{(max(bo) if bo else 0):.3f} | "
                         f"{stats['ce']} | {stats['drop']} | {stats['tail']} | {qd:.1f} | "
                         f"{statistics.mean(rtts) if rtts else 0:.1f} |")
    open(os.path.join(RES, "summary.md"), "w").write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    os.makedirs(PLOTS, exist_ok=True)
    all_names = list(TITLES)
    for n in all_names:
        plot_scenario(n)
    plot_compare(["noecn", "ecn", "abe"], "compare_noecn_ecn_abe.png")
    plot_compare(["ecn", "abe"], "compare_ecn_abe_zoom.png", zoom=(20, 40))
    summary(all_names)
