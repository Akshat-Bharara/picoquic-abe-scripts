#!/usr/bin/env python3
"""Extract the congestion control trace from a picoquic server qlog.

Writes, next to the qlog:
  cwnd.csv    time_s,cwnd_bytes,bytes_in_flight,smoothed_rtt_ms   (one row per cwnd/rtt update)
  events.csv  time_s,event      (event = ce_echo | loss)
  meta.txt    reference_time_us=<CLOCK_MONOTONIC us of time 0>

Times are seconds since the qlog reference time (connection start).
The qlog is read line by line, since it can be large.
"""
import glob
import os
import re
import sys

RE_EVENT = re.compile(r'^\[(\d+), "(\w+)", "(\w+)", (.*)$')
RE_FIELD = re.compile(r'"(cwnd|bytes_in_flight|smoothed_rtt)": (\d+)')
RE_CE = re.compile(r'"frame_type": "ack", [^}]*"ce": (\d+)')
RE_REF = re.compile(r'"reference_time": "(\d+)"')


def parse(qlog):
    d = os.path.dirname(qlog)
    ref = None
    cwnd = bif = srtt = 0
    last_ce = 0
    rows, events = [], []
    t, ev = 0.0, None
    with open(qlog) as f:
        for line in f:
            if ref is None:
                m = RE_REF.search(line)
                if m:
                    ref = int(m.group(1))
            m = RE_EVENT.match(line)
            if not m:
                # continuation line of the previous event (frames are split over lines)
                if ev == "packet_received":
                    for ce in RE_CE.findall(line):
                        ce = int(ce)
                        if ce > last_ce:
                            events.append((t, "ce_echo"))
                            last_ce = ce
                continue
            t = int(m.group(1)) / 1e6
            ev = m.group(3)
            if ev == "metrics_updated":
                changed = False
                for k, v in RE_FIELD.findall(m.group(4)):
                    v = int(v)
                    if k == "cwnd":
                        changed |= v != cwnd
                        cwnd = v
                    elif k == "bytes_in_flight":
                        bif = v
                    else:
                        changed |= v != srtt
                        srtt = v
                if changed or not rows or t - rows[-1][0] > 0.01:
                    rows.append((t, cwnd, bif, srtt / 1000))
            elif ev == "packet_received":
                for ce in RE_CE.findall(m.group(4)):
                    ce = int(ce)
                    if ce > last_ce:
                        events.append((t, "ce_echo"))
                        last_ce = ce
            elif ev == "packet_lost":
                events.append((t, "loss"))
    with open(os.path.join(d, "cwnd.csv"), "w") as f:
        f.write("time_s,cwnd_bytes,bytes_in_flight,smoothed_rtt_ms\n")
        for r in rows:
            f.write("%.6f,%d,%d,%.3f\n" % r)
    with open(os.path.join(d, "events.csv"), "w") as f:
        f.write("time_s,event\n")
        for t, e in events:
            f.write("%.6f,%s\n" % (t, e))
    with open(os.path.join(d, "meta.txt"), "w") as f:
        f.write("reference_time_us=%d\n" % (ref or 0))
    print(f"{qlog}: {len(rows)} cwnd samples, "
          f"{sum(1 for e in events if e[1] == 'ce_echo')} CE echoes, "
          f"{sum(1 for e in events if e[1] == 'loss')} losses")


if __name__ == "__main__":
    for d in sys.argv[1:]:
        for q in glob.glob(os.path.join(d, "**", "*.server.qlog"), recursive=True):
            parse(q)
