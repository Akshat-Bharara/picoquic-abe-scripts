#!/bin/bash
# Dumbbell topology with Linux network namespaces.
#
#   s1 ---\                                     /--- c1
#          rl ==[ bn: userspace bottleneck ]== rr
#   s2 ---/      (rate, delay, buffer, AQM/ECN)  \--- c2
#
# s1, s2: picoquic servers (data senders)     c1, c2: picoquic clients (receivers)
# rl, rr: Linux routers (ip_forward=1)        bn: bottleneck emulator netns (L2 bridge)
#
# Usage: topo.sh up | down
set -e
NS="s1 s2 rl bn rr c1 c2"

nsx() { ip netns exec "$@"; }

link() { # link <nsA> <ifA> <nsB> <ifB>
    ip link add "$2" netns "$1" type veth peer name "$4" netns "$3"
    for p in "$1:$2" "$3:$4"; do
        n=${p%%:*}; i=${p##*:}
        # No offloads: the emulator must see real, checksummed, MTU-sized frames.
        nsx "$n" ethtool -K "$i" tx off rx off tso off gso off gro off \
            tx-udp-segmentation off >/dev/null 2>&1 || true
        nsx "$n" ip link set "$i" up
    done
}

down() {
    for n in $NS; do ip netns del "$n" 2>/dev/null || true; done
}

up() {
    down
    for n in $NS; do
        ip netns add "$n"
        nsx "$n" ip link set lo up
    done
    link s1 eth0 rl s1-eth
    link s2 eth0 rl s2-eth
    link rl bn-eth bn ethA
    link bn ethB rr bn-eth
    link rr c1-eth c1 eth0
    link rr c2-eth c2 eth0

    nsx s1 ip addr add 10.0.1.1/24 dev eth0;  nsx rl ip addr add 10.0.1.254/24 dev s1-eth
    nsx s2 ip addr add 10.0.2.1/24 dev eth0;  nsx rl ip addr add 10.0.2.254/24 dev s2-eth
    nsx c1 ip addr add 10.0.11.1/24 dev eth0; nsx rr ip addr add 10.0.11.254/24 dev c1-eth
    nsx c2 ip addr add 10.0.12.1/24 dev eth0; nsx rr ip addr add 10.0.12.254/24 dev c2-eth
    nsx rl ip addr add 10.0.100.1/24 dev bn-eth
    nsx rr ip addr add 10.0.100.2/24 dev bn-eth

    for n in s1 s2; do nsx $n ip route add default via 10.0.${n#s}.254; done
    nsx c1 ip route add default via 10.0.11.254
    nsx c2 ip route add default via 10.0.12.254
    nsx rl sysctl -qw net.ipv4.ip_forward=1
    nsx rr sysctl -qw net.ipv4.ip_forward=1
    nsx rl ip route add 10.0.11.0/24 via 10.0.100.2
    nsx rl ip route add 10.0.12.0/24 via 10.0.100.2
    nsx rr ip route add 10.0.1.0/24 via 10.0.100.1
    nsx rr ip route add 10.0.2.0/24 via 10.0.100.1
    # bn is a pure bridge: no addresses, no forwarding by the kernel.
    nsx bn sysctl -qw net.ipv4.ip_forward=0
}

case "$1" in
    up) up ;;
    down) down ;;
    *) echo "usage: $0 up|down"; exit 1 ;;
esac
