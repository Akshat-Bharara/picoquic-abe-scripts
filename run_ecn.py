#!/usr/bin/env python3
"""Steps 4 and 5: CUBIC in picoquic on a netns dumbbell, without and with ECN.

    s1 --\\                 /-- c1
          rl ==[50 Mbit/s]== rr
    s2 --/   codel          \\-- c2

The bottleneck is rl -> rr: htb at 50 Mbit/s with codel (ECN on or off).
netem on rr adds 40 ms each way, so the base RTT is 80 ms.
s1 runs the picoquic server (sender), c1 the client.

Usage: sudo python3 run_ecn.py --picoquic /path/to/picoquic
Output: results/<run>/ (qlog, cwnd.csv) and plots/cwnd_noecn.png, plots/cwnd_ecn.png
"""
import argparse
import glob
import os
import re
import subprocess
import time

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
PLOTS = os.path.join(HERE, "plots")

RATE = "50mbit"
DELAY = "40ms"
MSS = 1440
BDP = 50e6 * 0.080 / 8 / MSS    # ~347 packets
SIZE = 375_000_000              # ~60 s at 50 Mbit/s

NAMESPACES = ["s1", "s2", "rl", "rr", "c1", "c2"]


def sh(cmd, check=True):
    return subprocess.run(cmd, shell=True, check=check, capture_output=True, text=True).stdout


def ns(n, cmd, check=True):
    return sh(f"ip netns exec {n} {cmd}", check)


def link(a, ia, b, ib):
    sh(f"ip link add {ia} netns {a} type veth peer name {ib} netns {b}")
    for n, i in ((a, ia), (b, ib)):
        ns(n, f"ethtool -K {i} tso off gso off gro off", check=False)
        ns(n, f"ip link set {i} up")


def topology_down():
    for n in NAMESPACES:
        sh(f"ip netns del {n}", check=False)


def topology_up():
    topology_down()
    for n in NAMESPACES:
        sh(f"ip netns add {n}")
        ns(n, "ip link set lo up")
    link("s1", "eth0", "rl", "s1")
    link("s2", "eth0", "rl", "s2")
    link("rl", "rr", "rr", "rl")
    link("rr", "c1", "c1", "eth0")
    link("rr", "c2", "c2", "eth0")

    ns("s1", "ip addr add 10.0.1.1/24 dev eth0")
    ns("s2", "ip addr add 10.0.2.1/24 dev eth0")
    ns("rl", "ip addr add 10.0.1.254/24 dev s1")
    ns("rl", "ip addr add 10.0.2.254/24 dev s2")
    ns("rl", "ip addr add 10.0.100.1/24 dev rr")
    ns("rr", "ip addr add 10.0.100.2/24 dev rl")
    ns("rr", "ip addr add 10.0.11.254/24 dev c1")
    ns("rr", "ip addr add 10.0.12.254/24 dev c2")
    ns("c1", "ip addr add 10.0.11.1/24 dev eth0")
    ns("c2", "ip addr add 10.0.12.1/24 dev eth0")

    ns("s1", "ip route add default via 10.0.1.254")
    ns("s2", "ip route add default via 10.0.2.254")
    ns("c1", "ip route add default via 10.0.11.254")
    ns("c2", "ip route add default via 10.0.12.254")
    ns("rl", "ip route add 10.0.11.0/24 via 10.0.100.2")
    ns("rl", "ip route add 10.0.12.0/24 via 10.0.100.2")
    ns("rr", "ip route add 10.0.1.0/24 via 10.0.100.1")
    ns("rr", "ip route add 10.0.2.0/24 via 10.0.100.1")
    ns("rl", "sysctl -qw net.ipv4.ip_forward=1")
    ns("rr", "sysctl -qw net.ipv4.ip_forward=1")

    # 40 ms delay on rr for data (to the clients) and acks (back to rl)
    for dev in ("c1", "c2", "rl"):
        ns("rr", f"tc qdisc add dev {dev} root netem delay {DELAY} limit 10000")


def set_bottleneck(ecn):
    ns("rl", "tc qdisc del dev rr root", check=False)
    ns("rl", "tc qdisc add dev rr root handle 1: htb default 10")
    ns("rl", f"tc class add dev rr parent 1: classid 1:10 htb rate {RATE} ceil {RATE}")
    ns("rl", "tc qdisc add dev rr parent 1:10 handle 20: codel limit 1000 target 5ms interval 100ms "
       + ("ecn" if ecn else "noecn"))


def run(name, picoquic, ecn, cc_option=None, size=SIZE):
    """Download `size` bytes from s1 to c1 with CUBIC and keep the server qlog."""
    out = os.path.join(RESULTS, name)
    os.makedirs(out, exist_ok=True)
    for f in glob.glob(os.path.join(out, "*.qlog")):
        os.remove(f)
    sh("pkill -x picoquicdemo", check=False)
    set_bottleneck(ecn)

    demo = os.path.join(picoquic, "build", "picoquicdemo")
    cert = os.path.join(picoquic, "certs")
    opt = f"-H {cc_option}" if cc_option else ""
    print(f"--- {name}: ecn={'on' if ecn else 'off'} cubic option={cc_option or 'default'}", flush=True)

    server = subprocess.Popen(
        f"ip netns exec s1 {demo} -p 4443 -c {cert}/cert.pem -k {cert}/key.pem "
        f"-G cubic {opt} -0 -L -q {out} -1",
        shell=True, cwd=out, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    client = ns("c1", f"timeout 300 {demo} -n test.example.com -D -G cubic -0 10.0.1.1 4443 /{size}",
                check=False)
    server.wait(timeout=60)

    with open(os.path.join(out, "client.txt"), "w") as f:
        f.write(client)
    stats = ns("rl", "tc -s qdisc show dev rr")
    with open(os.path.join(out, "tc_stats.txt"), "w") as f:
        f.write(stats)
    for line in client.splitlines():
        if line.startswith("Received") and "Mbps" in line:
            print(line)
    codel = stats[stats.find("qdisc codel"):]
    drops = re.search(r"dropped (\d+)", codel)
    marks = re.search(r"ecn_mark (\d+)", codel)
    if drops and marks:
        print(f"codel: dropped {drops.group(1)}, ECN marked {marks.group(1)}")
    return out


def load_cwnd(out):
    """Read cwnd over time from the server qlog. Also writes cwnd.csv."""
    qlog = glob.glob(os.path.join(out, "*.server.qlog"))[0]
    t, cwnd = [], []
    pat = re.compile(r'^\[(\d+), "recovery", "metrics_updated", \{.*?"cwnd": (\d+)')
    with open(qlog) as f:
        for line in f:
            m = pat.match(line)
            if m:
                t.append(int(m.group(1)) / 1e6)
                cwnd.append(int(m.group(2)) / MSS)
    with open(os.path.join(out, "cwnd.csv"), "w") as f:
        f.write("time_s,cwnd_packets\n")
        for a, b in zip(t, cwnd):
            f.write(f"{a:.6f},{b:.1f}\n")
    return t, cwnd


def goodput(out):
    for line in open(os.path.join(out, "client.txt")):
        if line.startswith("Received") and "Mbps" in line:
            return float(line.split(",")[1].split()[0])
    return 0.0


def plot(t, cwnd, title, filename, color="tab:blue", tmax=60):
    os.makedirs(PLOTS, exist_ok=True)
    plt.figure(figsize=(9, 4))
    plt.plot(t, cwnd, color=color, drawstyle="steps-post")
    plt.axhline(BDP, color="gray", ls="--", lw=1, label="BDP")
    plt.xlim(0, tmax)
    plt.ylim(0, max(c for x, c in zip(t, cwnd) if x > 2) * 1.2)
    plt.xlabel("time (s)")
    plt.ylabel("cwnd (packets)")
    plt.title(title)
    plt.legend(loc="upper right")
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(os.path.join(PLOTS, filename), dpi=120)
    plt.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--picoquic", required=True, help="picoquic checkout, built in build/")
    args = ap.parse_args()

    topology_up()
    try:
        # -H R: CUBIC with beta = 0.7 (RFC 9438). Use the default option on unpatched picoquic.
        noecn = run("noecn", args.picoquic, ecn=False, cc_option="R")
        ecn = run("ecn", args.picoquic, ecn=True, cc_option="R")
    finally:
        topology_down()

    t, c = load_cwnd(noecn)
    plot(t, c, f"CUBIC without ECN ({goodput(noecn):.1f} Mbit/s)", "cwnd_noecn.png")
    t, c = load_cwnd(ecn)
    plot(t, c, f"CUBIC with ECN ({goodput(ecn):.1f} Mbit/s)", "cwnd_ecn.png", color="tab:orange")
    print("plots: plots/cwnd_noecn.png, plots/cwnd_ecn.png")


if __name__ == "__main__":
    main()
