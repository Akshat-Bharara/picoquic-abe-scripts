#!/usr/bin/env python3
"""Run picoquic CUBIC experiments over the netns dumbbell (scripts/topo.sh).

Each scenario starts the userspace bottleneck (emu/bneck) in netns "bn",
one picoquic server per flow in s1/s2 (the data senders, with qlog of every
packet), and the matching clients in c1/c2 which download a large object.

Usage: run_experiments.py [scenario ...]     (default: all)
"""
import os
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PICOQUIC = os.environ.get("PICOQUIC", "/home/user/picoquic")
DEMO = os.path.join(os.environ.get("PICOQUIC_BUILD", os.path.join(PICOQUIC, "build")), "picoquicdemo")
CERT = os.path.join(PICOQUIC, "certs", "cert.pem")
KEY = os.path.join(PICOQUIC, "certs", "key.pem")
BNECK = os.path.join(ROOT, "emu", "bneck")
RESULTS = os.path.join(ROOT, "results")

# Bottleneck: 50 Mbit/s, 80 ms base RTT (BDP ~ 347 packets of 1440 bytes),
# 1000 packet buffer, CoDel (RFC 8289) with 5 ms target / 100 ms interval.
LINK = dict(rate=50, delay=40, buffer=1000, aqm="codel", target=5, interval=100)
OBJECT_SIZE = 375_000_000   # ~60 s at 50 Mbit/s

# name: (ECN at the bottleneck, CUBIC option string, flows)
# flows: list of (server ns, server ip, client ns, start delay s, object size)
ONE_FLOW = [("s1", "10.0.1.1", "c1", 0, OBJECT_SIZE)]
TWO_FLOWS = [("s1", "10.0.1.1", "c1", 0, OBJECT_SIZE),
             ("s2", "10.0.2.1", "c2", 20, OBJECT_SIZE * 2 // 3)]
SCENARIOS = {
    # Step 4: CUBIC without ECN (CoDel drops instead of marking)
    "noecn": (0, "R", ONE_FLOW),
    # Step 5: CUBIC with ECN, classic response (CE treated like a loss)
    "ecn": (1, "R", ONE_FLOW),
    # Step 8: CUBIC with ECN and ABE (RFC 8511)
    "abe": (1, "A", ONE_FLOW),
    # Same three runs with picoquic's stock CUBIC (beta = 7/8 for everything)
    "stock_noecn": (0, None, ONE_FLOW),
    "stock_ecn": (1, None, ONE_FLOW),
    # Dumbbell with two competing flows
    "ecn_2flows": (1, "R", TWO_FLOWS),
    "abe_2flows": (1, "A", TWO_FLOWS),
}


def ns(name, cmd):
    return ["ip", "netns", "exec", name] + cmd


def cleanup():
    for p in ("bneck", "picoquicdemo"):
        subprocess.run(["pkill", "-x", p], stderr=subprocess.DEVNULL)
    time.sleep(1)


def run(name):
    ecn, cc_opt, flows = SCENARIOS[name]
    out = os.path.join(RESULTS, name)
    os.makedirs(out, exist_ok=True)
    cleanup()
    print(f"=== {name}: ecn={ecn} cubic option={cc_opt} flows={len(flows)}", flush=True)

    bneck_log = open(os.path.join(out, "bneck.txt"), "w")
    bneck = subprocess.Popen(ns("bn", [
        BNECK, "-a", "ethA", "-b", "ethB", "-r", str(LINK["rate"]), "-d", str(LINK["delay"]),
        "-q", str(LINK["buffer"]), "-m", LINK["aqm"], "-t", str(LINK["target"]),
        "-i", str(LINK["interval"]), "-e", str(ecn), "-l", os.path.join(out, "queue.csv")]),
        stderr=bneck_log)

    servers = []
    for i, (sns, _, _, _, _) in enumerate(flows):
        qdir = os.path.join(out, f"flow{i + 1}")
        os.makedirs(qdir, exist_ok=True)
        cmd = [DEMO, "-p", "4443", "-c", CERT, "-k", KEY, "-G", "cubic", "-0", "-L", "-q", qdir, "-1"]
        if cc_opt:
            cmd[7:7] = ["-H", cc_opt]
        servers.append(subprocess.Popen(ns(sns, cmd), cwd=qdir,
                                        stdout=open(os.path.join(qdir, "server.txt"), "w"),
                                        stderr=subprocess.STDOUT))
    time.sleep(1)

    clients = []
    t0 = time.time()
    pending = list(enumerate(flows))
    while pending or any(c.poll() is None for c in clients):
        for item in list(pending):
            i, (_, sip, cns, start, size) = item
            if time.time() - t0 >= start:
                qdir = os.path.join(out, f"flow{i + 1}")
                cmd = [DEMO, "-n", "test.example.com", "-D", "-G", "cubic", "-0", sip, "4443", f"/{size}"]
                clients.append(subprocess.Popen(ns(cns, ["timeout", "300"] + cmd), cwd=qdir,
                                                stdout=open(os.path.join(qdir, "client.txt"), "w"),
                                                stderr=subprocess.STDOUT))
                pending.remove(item)
        time.sleep(0.2)

    for s in servers:
        try:
            s.wait(timeout=60)
        except subprocess.TimeoutExpired:
            s.send_signal(signal.SIGINT)
    time.sleep(1)
    bneck.send_signal(signal.SIGTERM)
    bneck.wait()
    bneck_log.close()
    print(open(os.path.join(out, "bneck.txt")).read().strip())
    for i in range(len(flows)):
        for line in open(os.path.join(out, f"flow{i + 1}", "client.txt")):
            if "Mbps" in line and line.startswith("Received"):
                print(f"flow{i + 1}: {line.strip()}")


def main():
    names = sys.argv[1:] or list(SCENARIOS)
    subprocess.run([os.path.join(ROOT, "scripts", "topo.sh"), "up"], check=True)
    try:
        for n in names:
            run(n)
    finally:
        cleanup()


if __name__ == "__main__":
    main()
