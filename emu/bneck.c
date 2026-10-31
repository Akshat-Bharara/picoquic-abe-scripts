/*
 * bneck: a userspace bottleneck link emulator for a Linux netns dumbbell.
 *
 * Why userspace? The kernel of the test machine is built without
 * sch_netem, sch_red, sch_codel, sch_fq_codel and sch_pie, so the usual
 * "tc qdisc ... netem delay / codel ecn" recipe is not available. This
 * program plays the role of the bottleneck "wire" between the two routers
 * of the dumbbell. It runs in its own network namespace, owns two veth
 * endpoints (A = left router side, B = right router side), and moves raw
 * Ethernet frames between them through AF_PACKET sockets, i.e. it acts as a
 * transparent L2 bridge with:
 *
 *   A -> B (data direction):  FIFO buffer (tail drop at -q packets)
 *                             + AQM (none | codel | step) at dequeue
 *                             + serialization at -r Mbit/s
 *                             + one-way propagation delay -d ms
 *   B -> A (ack direction):   propagation delay -d ms only
 *
 * AQM signals are ECN CE marks for ECT(0)/ECT(1) IPv4 packets when ECN
 * is enabled (-e 1), and drops otherwise (or for Not-ECT packets).
 * CoDel follows the RFC 8289 pseudocode (as in Linux sch_codel).
 *
 * Usage: bneck -a ifA -b ifB -r mbit -d ms [-q pkts] [-m none|codel|step]
 *              [-t target_ms] [-i interval_ms] [-k step_ms] [-e 0|1]
 *              [-l queue_log.csv] [-T seconds]
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <linux/if_packet.h>
#include <math.h>
#include <net/ethernet.h>
#include <net/if.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define SLOT_SIZE 2048
#define RING_SLOTS 16384
#define MTU_BYTES 1514

#ifndef PACKET_IGNORE_OUTGOING
#define PACKET_IGNORE_OUTGOING 23
#endif

typedef struct {
    uint64_t t;      /* enqueue time (queue) or delivery time (delay line) */
    uint32_t len;
    uint8_t data[SLOT_SIZE];
} pkt_t;

typedef struct {
    pkt_t* slots;
    uint32_t head, count;
    uint64_t bytes;
} ring_t;

enum { AQM_NONE = 0, AQM_CODEL, AQM_STEP };

typedef struct {
    /* configuration */
    double rate_bps;          /* 0 = infinite */
    uint64_t delay_ns;
    uint32_t limit_pkts;
    int aqm;
    int ecn;
    uint64_t target_ns, interval_ns, step_ns;
    /* state */
    ring_t q;                 /* bottleneck buffer */
    ring_t dl;                /* propagation delay line */
    uint64_t link_free_at;
    /* codel state */
    uint64_t first_above_time, drop_next;
    uint32_t count, lastcount;
    int dropping;
    /* stats */
    uint64_t n_in, n_out, n_taildrop, n_aqm_drop, n_mark, n_ce_seen;
} dir_t;

static volatile int stop_flag = 0;
static FILE* qlog = NULL;
static uint64_t t_start = 0;

static void on_signal(int s) { (void)s; stop_flag = 1; }

static uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static int ring_init(ring_t* r)
{
    r->slots = calloc(RING_SLOTS, sizeof(pkt_t));
    r->head = r->count = 0;
    r->bytes = 0;
    return r->slots == NULL ? -1 : 0;
}

static pkt_t* ring_tail_slot(ring_t* r)
{
    return (r->count >= RING_SLOTS) ? NULL : &r->slots[(r->head + r->count) % RING_SLOTS];
}

static void ring_commit(ring_t* r) { r->bytes += r->slots[(r->head + r->count) % RING_SLOTS].len; r->count++; }
static pkt_t* ring_head(ring_t* r) { return r->count ? &r->slots[r->head] : NULL; }
static void ring_pop(ring_t* r) { r->bytes -= r->slots[r->head].len; r->head = (r->head + 1) % RING_SLOTS; r->count--; }

static int open_if(const char* name, int* ifindex)
{
    int fd = socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ALL));
    if (fd < 0) { perror("socket"); return -1; }
    *ifindex = (int)if_nametoindex(name);
    if (*ifindex == 0) { fprintf(stderr, "unknown interface %s\n", name); return -1; }
    struct sockaddr_ll sll = { 0 };
    sll.sll_family = AF_PACKET;
    sll.sll_protocol = htons(ETH_P_ALL);
    sll.sll_ifindex = *ifindex;
    if (bind(fd, (struct sockaddr*)&sll, sizeof(sll)) < 0) { perror("bind"); return -1; }
    int one = 1;
    if (setsockopt(fd, SOL_PACKET, PACKET_IGNORE_OUTGOING, &one, sizeof(one)) < 0) {
        perror("PACKET_IGNORE_OUTGOING");
    }
    int buf = 32 * 1024 * 1024;
    setsockopt(fd, SOL_SOCKET, SO_RCVBUFFORCE, &buf, sizeof(buf));
    setsockopt(fd, SOL_SOCKET, SO_SNDBUFFORCE, &buf, sizeof(buf));
    struct packet_mreq mr = { 0 };
    mr.mr_ifindex = *ifindex;
    mr.mr_type = PACKET_MR_PROMISC;
    setsockopt(fd, SOL_PACKET, PACKET_ADD_MEMBERSHIP, &mr, sizeof(mr));
    return fd;
}

/* Returns pointer to IPv4 header or NULL. Handles one 802.1Q tag. */
static uint8_t* ipv4_hdr(pkt_t* p)
{
    if (p->len < 14 + 20) return NULL;
    uint16_t et = (uint16_t)((p->data[12] << 8) | p->data[13]);
    if (et != 0x0800) return NULL;
    uint8_t* ip = p->data + 14;
    if ((ip[0] >> 4) != 4) return NULL;
    return ip;
}

static int pkt_ecn(pkt_t* p)
{
    uint8_t* ip = ipv4_hdr(p);
    return ip ? (ip[1] & 3) : 0;
}

/* Set CE on an ECT IPv4 packet, with incremental checksum update (RFC 1624) */
static void set_ce(pkt_t* p)
{
    uint8_t* ip = ipv4_hdr(p);
    if (!ip) return;
    uint16_t old_w = (uint16_t)((ip[0] << 8) | ip[1]);
    ip[1] |= 3;
    uint16_t new_w = (uint16_t)((ip[0] << 8) | ip[1]);
    uint32_t sum = (uint16_t)~((ip[10] << 8) | ip[11]);
    sum += (uint16_t)~old_w;
    sum += new_w;
    while (sum >> 16) sum = (sum & 0xffff) + (sum >> 16);
    uint16_t ck = (uint16_t)~sum;
    ip[10] = (uint8_t)(ck >> 8);
    ip[11] = (uint8_t)(ck & 0xff);
}

static void log_event(const char* ev, dir_t* d, uint64_t now, uint64_t sojourn)
{
    if (qlog) {
        fprintf(qlog, "%.6f,%s,%u,%llu,%.3f\n", (double)(now - t_start) / 1e9, ev, d->q.count,
            (unsigned long long)d->q.bytes, (double)sojourn / 1e6);
    }
}

/* AQM signal on packet p. Returns 1 if the packet must be dropped,
 * 0 if it was CE-marked and continues. */
static int aqm_signal(dir_t* d, pkt_t* p, uint64_t now)
{
    int ecn = pkt_ecn(p);
    if (d->ecn && ecn != 0) {
        if (ecn != 3) set_ce(p);
        d->n_mark++;
        log_event("mark", d, now, now - p->t);
        return 0;
    }
    d->n_aqm_drop++;
    log_event("drop", d, now, now - p->t);
    return 1;
}

static uint64_t codel_control_law(dir_t* d, uint64_t t)
{
    return t + (uint64_t)((double)d->interval_ns / sqrt((double)d->count));
}

static int codel_ok_to_drop(dir_t* d, pkt_t* p, uint64_t now)
{
    uint64_t sojourn = now - p->t;
    /* bytes still queued after this packet has left */
    if (sojourn < d->target_ns || d->q.bytes - p->len <= MTU_BYTES) {
        d->first_above_time = 0;
        return 0;
    }
    if (d->first_above_time == 0) {
        d->first_above_time = now + d->interval_ns;
        return 0;
    }
    return now >= d->first_above_time;
}

/* Dequeue one packet to transmit. Returns slot copied into out, or 0 if none. */
static int dequeue(dir_t* d, uint64_t now, pkt_t* out)
{
    for (;;) {
        pkt_t* p = ring_head(&d->q);
        if (!p) {
            if (d->aqm == AQM_CODEL) { d->first_above_time = 0; d->dropping = 0; }
            return 0;
        }
        int drop = 0;
        if (d->aqm == AQM_STEP) {
            if (now - p->t > d->step_ns) {
                drop = aqm_signal(d, p, now);
            }
        }
        else if (d->aqm == AQM_CODEL) {
            int ok = codel_ok_to_drop(d, p, now);
            if (d->dropping) {
                if (!ok) {
                    d->dropping = 0;
                }
                else if (now >= d->drop_next) {
                    d->count++;
                    drop = aqm_signal(d, p, now);
                    d->drop_next = codel_control_law(d, d->drop_next);
                }
            }
            else if (ok) {
                drop = aqm_signal(d, p, now);
                d->dropping = 1;
                uint32_t delta = d->count - d->lastcount;
                if (delta > 1 && now - d->drop_next < 16 * d->interval_ns) {
                    d->count = delta;
                }
                else {
                    d->count = 1;
                }
                d->lastcount = d->count;
                d->drop_next = codel_control_law(d, now);
            }
        }
        if (drop) {
            ring_pop(&d->q);
            continue;
        }
        out->t = p->t;
        out->len = p->len;
        memcpy(out->data, p->data, p->len);
        ring_pop(&d->q);
        return 1;
    }
}

static void enqueue(dir_t* d, const uint8_t* buf, uint32_t len, uint64_t now)
{
    d->n_in++;
    if (d->rate_bps <= 0) {
        /* no bottleneck: straight into delay line */
        pkt_t* s = ring_tail_slot(&d->dl);
        if (!s) { d->n_taildrop++; return; }
        memcpy(s->data, buf, len); s->len = len; s->t = now + d->delay_ns;
        ring_commit(&d->dl);
        return;
    }
    if (d->q.count >= d->limit_pkts) {
        d->n_taildrop++;
        if (qlog) fprintf(qlog, "%.6f,taildrop,%u,%llu,0\n", (double)(now - t_start) / 1e9, d->q.count, (unsigned long long)d->q.bytes);
        return;
    }
    pkt_t* s = ring_tail_slot(&d->q);
    if (!s) { d->n_taildrop++; return; }
    memcpy(s->data, buf, len); s->len = len; s->t = now;
    if (pkt_ecn((pkt_t*)s) == 3) d->n_ce_seen++;
    ring_commit(&d->q);
}

/* Move packets from queue onto the wire, i.e. into the delay line. */
static void service(dir_t* d, uint64_t now)
{
    static pkt_t tmp;
    while (d->rate_bps > 0 && d->link_free_at <= now) {
        /* The link becomes free at link_free_at; the packet starts then. */
        uint64_t start = d->link_free_at;
        if (d->q.count == 0) break;
        pkt_t* h = ring_head(&d->q);
        if (h->t > start) start = h->t;
        if (start > now) break;
        if (!dequeue(d, start, &tmp)) break;
        uint64_t tx = (uint64_t)((double)(tmp.len + 24) * 8.0 * 1e9 / d->rate_bps); /* + preamble/IFG/FCS */
        d->link_free_at = start + tx;
        pkt_t* s = ring_tail_slot(&d->dl);
        if (!s) { d->n_taildrop++; continue; }
        memcpy(s->data, tmp.data, tmp.len); s->len = tmp.len;
        s->t = d->link_free_at + d->delay_ns;
        ring_commit(&d->dl);
    }
}

static void deliver(dir_t* d, int fd, int ifindex, uint64_t now)
{
    pkt_t* p;
    struct sockaddr_ll sll = { 0 };
    sll.sll_family = AF_PACKET;
    sll.sll_ifindex = ifindex;
    sll.sll_halen = 6;
    while ((p = ring_head(&d->dl)) != NULL && p->t <= now) {
        memcpy(sll.sll_addr, p->data, 6);
        if (sendto(fd, p->data, p->len, 0, (struct sockaddr*)&sll, sizeof(sll)) < 0) {
            if (errno != ENOBUFS && errno != EAGAIN) perror("sendto");
        }
        d->n_out++;
        ring_pop(&d->dl);
    }
}

static uint64_t next_event(dir_t* d, uint64_t now)
{
    uint64_t t = UINT64_MAX;
    pkt_t* p = ring_head(&d->dl);
    if (p) t = p->t;
    if (d->rate_bps > 0 && d->q.count > 0) {
        uint64_t s = d->link_free_at > now ? d->link_free_at : now;
        if (s < t) t = s;
    }
    return t;
}

static void usage(void)
{
    fprintf(stderr, "bneck -a ifA -b ifB -r mbit -d ms [-q pkts] [-m none|codel|step] [-t target_ms] [-i interval_ms] [-k step_ms] [-e 0|1] [-l log.csv] [-T sec]\n");
    exit(1);
}

int main(int argc, char** argv)
{
    const char *ifa = NULL, *ifb = NULL, *logf = NULL;
    double rate = 0, delay_ms = 0, target_ms = 5, interval_ms = 100, step_ms = 5, duration = 0;
    int limit = 1000, ecn = 1, aqm = AQM_CODEL, c;
    while ((c = getopt(argc, argv, "a:b:r:d:q:m:t:i:k:e:l:T:")) != -1) {
        switch (c) {
        case 'a': ifa = optarg; break;
        case 'b': ifb = optarg; break;
        case 'r': rate = atof(optarg); break;
        case 'd': delay_ms = atof(optarg); break;
        case 'q': limit = atoi(optarg); break;
        case 'm': aqm = !strcmp(optarg, "codel") ? AQM_CODEL : !strcmp(optarg, "step") ? AQM_STEP : AQM_NONE; break;
        case 't': target_ms = atof(optarg); break;
        case 'i': interval_ms = atof(optarg); break;
        case 'k': step_ms = atof(optarg); break;
        case 'e': ecn = atoi(optarg); break;
        case 'l': logf = optarg; break;
        case 'T': duration = atof(optarg); break;
        default: usage();
        }
    }
    if (!ifa || !ifb || rate <= 0) usage();
    if (limit > RING_SLOTS) limit = RING_SLOTS;

    prctl(PR_SET_TIMERSLACK, 1UL);
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    static dir_t fwd, rev;
    fwd.rate_bps = rate * 1e6; fwd.delay_ns = (uint64_t)(delay_ms * 1e6); fwd.limit_pkts = (uint32_t)limit;
    fwd.aqm = aqm; fwd.ecn = ecn;
    fwd.target_ns = (uint64_t)(target_ms * 1e6); fwd.interval_ns = (uint64_t)(interval_ms * 1e6);
    fwd.step_ns = (uint64_t)(step_ms * 1e6);
    rev.rate_bps = 0; rev.delay_ns = fwd.delay_ns; rev.limit_pkts = RING_SLOTS; rev.aqm = AQM_NONE;
    if (ring_init(&fwd.q) || ring_init(&fwd.dl) || ring_init(&rev.q) || ring_init(&rev.dl)) {
        fprintf(stderr, "out of memory\n");
        return 1;
    }
    int ia, ib;
    int fa = open_if(ifa, &ia), fb = open_if(ifb, &ib);
    if (fa < 0 || fb < 0) return 1;
    if (logf) {
        qlog = fopen(logf, "w");
    }
    t_start = now_ns();
    if (qlog) {
        /* CLOCK_MONOTONIC, same clock as picoquic_current_time() on Linux */
        fprintf(qlog, "# t0_monotonic_us=%llu\n", (unsigned long long)(t_start / 1000));
        fprintf(qlog, "time,event,qlen_pkts,qlen_bytes,sojourn_ms\n");
    }
    fprintf(stderr, "bneck: %s->%s %.1f Mbit/s, delay %.1f ms each way, buffer %d pkts, aqm=%s ecn=%d\n",
        ifa, ifb, rate, delay_ms, limit, aqm == AQM_CODEL ? "codel" : aqm == AQM_STEP ? "step" : "none", ecn);

    static uint8_t buf[65536];
    uint64_t next_sample = t_start;
    struct pollfd pfd[2] = { { fa, POLLIN, 0 }, { fb, POLLIN, 0 } };
    while (!stop_flag) {
        uint64_t now = now_ns();
        if (duration > 0 && now - t_start > (uint64_t)(duration * 1e9)) break;
        uint64_t te = next_event(&fwd, now);
        uint64_t t2 = next_event(&rev, now);
        if (t2 < te) te = t2;
        if (qlog && next_sample < te) te = next_sample;
        struct timespec to;
        uint64_t wait = te > now ? te - now : 0;
        if (wait > 100000000ull) wait = 100000000ull;
        to.tv_sec = (time_t)(wait / 1000000000ull);
        to.tv_nsec = (long)(wait % 1000000000ull);
        int n = ppoll(pfd, 2, &to, NULL);
        if (n < 0 && errno != EINTR) { perror("ppoll"); break; }
        now = now_ns();
        for (int i = 0; i < 2; i++) {
            if (n > 0 && (pfd[i].revents & POLLIN)) {
                for (;;) {
                    struct sockaddr_ll from;
                    socklen_t fl = sizeof(from);
                    ssize_t len = recvfrom(pfd[i].fd, buf, sizeof(buf), MSG_DONTWAIT, (struct sockaddr*)&from, &fl);
                    if (len <= 0) break;
                    if (from.sll_pkttype == PACKET_OUTGOING) continue;
                    if (len > SLOT_SIZE) {
                        fprintf(stderr, "bneck: oversize frame %zd dropped (disable GSO/GRO)\n", len);
                        continue;
                    }
                    enqueue(i == 0 ? &fwd : &rev, buf, (uint32_t)len, now);
                }
            }
        }
        now = now_ns();
        service(&fwd, now);
        service(&rev, now);
        deliver(&fwd, fb, ib, now);
        deliver(&rev, fa, ia, now);
        if (qlog && now >= next_sample) {
            pkt_t* h = ring_head(&fwd.q);
            log_event("sample", &fwd, now, h ? now - h->t : 0);
            next_sample = now + 5000000ull;
        }
    }
    fprintf(stderr, "bneck fwd: in=%llu out=%llu taildrop=%llu aqm_drop=%llu ce_marked=%llu\n",
        (unsigned long long)fwd.n_in, (unsigned long long)fwd.n_out, (unsigned long long)fwd.n_taildrop,
        (unsigned long long)fwd.n_aqm_drop, (unsigned long long)fwd.n_mark);
    fprintf(stderr, "bneck rev: in=%llu out=%llu\n", (unsigned long long)rev.n_in, (unsigned long long)rev.n_out);
    if (qlog) fclose(qlog);
    return 0;
}
