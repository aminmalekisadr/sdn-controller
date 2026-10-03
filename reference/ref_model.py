#!/usr/bin/env python3
"""Reference model for docs/SPEC.md: the source of every golden number.

It runs your two topology scripts with stub versions of net_sim_builder and PHY_model,
collapses each NPU (compute die + its IO dies) into one node, adds ports, optical domains,
modules, lanes and the OCS, then computes routes, failures, waves, loops, LFA coverage,
loads, timelines and message counts.

Run:  python3 reference/ref_model.py --out build/ref_out
      (needs networkx, because the two scripts import it; about 80 s)
It writes golden.json (the v1 numbers the tests use) into --out; --full also writes the later-phase
numbers (loads, loop-free backups, rollback, partial restore) to golden_full.json and golden_raw.json.
It never overwrites reference/golden.json unless you point --out at reference/.
This file is only a cross-check. The controller code must not import it.
"""
import collections
import json
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TIER_RANK = {"npu": 0, "l1": 1, "l2": 2}


def devkey(d):
    tier, num = d.split("-")
    return (TIER_RANK[tier], int(num))


def lanekey(x):
    dev, rest = x.split(".", 1)
    p, j = rest.split(".")
    return (devkey(dev), int(p[1:]), int(j))


# ---------------------------------------------------------------- 1. your scripts, run with stubs
def run_script(fname):
    sys.path.insert(0, os.path.join(HERE, "stubs"))
    import net_sim_builder  # the stub
    saved = sys.argv
    sys.argv = [fname, "case"]
    try:
        g = runpy.run_path(os.path.join(HERE, "topologies", fname), run_name="__main__")
    finally:
        sys.argv = saved
    return g, net_sim_builder.LAST


# ---------------------------------------------------------------- 2. collapse to NPU / L1 / L2
class Topo:
    def __init__(self, name, fname, has_ocs, two_plus_two, spare_modules=0, npu_domain_ports=2):
        self.name, self.has_ocs, self.two_plus_two = name, has_ocs, two_plus_two
        g, graph = run_script(fname)
        self.g = g
        T, GS, SWB, HS, NH = (g["TOTAL_COMPUTES"], g["GRID_START"], g["SW_BASE"],
                             g["HRS_START"], g["NUM_HRS"])
        self.script_edges = len(graph.edges)

        def dev_of(x):
            if x < T:
                return f"npu-{x}"
            if GS <= x < SWB:
                return f"npu-{(x - GS) % T}"
            if SWB <= x < HS:
                return f"l1-{x}"
            if HS <= x < HS + NH:
                return f"l2-{x}"
            raise ValueError(x)

        self.devices = {}
        for d in range(T):
            self.devices[f"npu-{d}"] = dict(tier="npu", role="npu",
                                            rack=d // g["COMPUTE_PER_RACK"],
                                            board=d // g["DAVIDS_PER_BOARD"])
        for s in range(SWB, HS):
            r, k = divmod(s - SWB, g["SWITCHES_PER_RACK"])
            plane, idx = divmod(k, g["SW_PER_PLANE"])
            role = "union" if idx < g["L1_UNIONS_PER_PLANE"] else "ext"
            self.devices[f"l1-{s}"] = dict(tier="l1", role=role, rack=r, plane=plane)
        for s in range(HS, HS + NH):
            self.devices[f"l2-{s}"] = dict(tier="l2", role="hrs",
                                           plane=(s - HS) // g["HRS_PER_PLANE"])
        # links, ports in edge-creation order
        self.ports, self.links, nport = {}, [], collections.Counter()
        internal = 0
        for a, b, _bw, _dl in graph.edges:
            da, db = dev_of(a), dev_of(b)
            if da == db:
                internal += 1
                continue
            ta, tb = self.devices[da]["tier"], self.devices[db]["tier"]
            if ta == tb == "npu":
                kind = "d2d" if self.devices[da]["rack"] == self.devices[db]["rack"] else "optical"
            elif {ta, tb} == {"l1", "l2"}:
                kind = "optical"
            else:
                kind = "copper"
            pa, pb = f"{da}.p{nport[da]}", f"{db}.p{nport[db]}"
            nport[da] += 1
            nport[db] += 1
            self.ports[pa] = dict(device=da, peer=pb, kind=kind, spare=False)
            self.ports[pb] = dict(device=db, peer=pa, kind=kind, spare=False)
            self.links.append((pa, pb, kind))
        self.internal_edges = internal
        # lanes: 2 per port, lane j <-> lane j
        self.lanes, self.peer = {}, {}
        for p, info in self.ports.items():
            for j in range(2):
                self.lanes[f"{p}.{j}"] = dict(device=info["device"], port=p, module=None, spare=False)
                self.peer[f"{p}.{j}"] = f"{info['peer']}.{j}"
        # optical domains and modules
        self.modules, self.domains = {}, {}
        for dev in sorted(self.devices, key=devkey):
            opt = [f"{dev}.p{k}" for k in range(nport[dev])
                   if self.ports[f"{dev}.p{k}"]["kind"] == "optical"]
            if not opt:
                continue
            size = npu_domain_ports if self.devices[dev]["tier"] == "npu" else 4
            assert len(opt) % size == 0, (dev, len(opt))
            ndom = len(opt) // size
            for k in range(ndom):
                ports = opt[k * size:(k + 1) * size]
                ma, mb = f"{dev}.m{2 * k}", f"{dev}.m{2 * k + 1}"
                if two_plus_two:
                    la, lb = [f"{p}.0" for p in ports], [f"{p}.1" for p in ports]
                else:
                    h = size // 2
                    la = [f"{p}.{j}" for p in ports[:h] for j in range(2)]
                    lb = [f"{p}.{j}" for p in ports[h:] for j in range(2)]
                dom = f"{dev}.d{k}"
                self.domains[dom] = dict(device=dev, ports=ports, modules=[ma, mb], spares=[],
                                         mapping="2+2" if two_plus_two else "2:1")
                for m, ls in ((ma, la), (mb, lb)):
                    self.modules[m] = dict(device=dev, domain=dom, lanes=ls, spare=False)
                    for x in ls:
                        self.lanes[x]["module"] = m
            # spare modules: only on OCS-attached NPU domains
            if has_ocs and self.devices[dev]["tier"] == "npu" and spare_modules:
                nxt = nport[dev]
                for k in range(ndom):
                    dom = f"{dev}.d{k}"
                    for s in range(spare_modules):
                        m = f"{dev}.m{2 * ndom + k * spare_modules + s}"
                        ls = []
                        for _ in range(size // 2):  # a spare module has as many lanes as a working one
                            p = f"{dev}.p{nxt}"
                            nxt += 1
                            self.ports[p] = dict(device=dev, peer=None, kind="optical", spare=True)
                            for j in range(2):
                                x = f"{p}.{j}"
                                self.lanes[x] = dict(device=dev, port=p, module=m, spare=True)
                                ls.append(x)
                        self.modules[m] = dict(device=dev, domain=dom, lanes=ls, spare=True)
                        self.domains[dom]["spares"].append(m)
        self.nport = nport
        # OCS: rack 0 NPU optical lanes on North, rack 1 on South (topology 2 only)
        self.ocs_port, self.xconnects = {}, []
        if has_ocs:
            north = sorted((x for x, v in self.lanes.items() if self.ports[v["port"]]["kind"] == "optical"
                            and self.devices[v["device"]]["tier"] == "npu"
                            and self.devices[v["device"]]["rack"] == 0), key=lanekey)
            south = sorted((x for x, v in self.lanes.items() if self.ports[v["port"]]["kind"] == "optical"
                            and self.devices[v["device"]]["tier"] == "npu"
                            and self.devices[v["device"]]["rack"] == 1), key=lanekey)
            for side, lst in (("N", north), ("S", south)):
                work = [x for x in lst if not self.lanes[x]["spare"]]
                spare = [x for x in lst if self.lanes[x]["spare"]]
                for i, x in enumerate(work + spare):
                    self.ocs_port[x] = f"{side}{i + 1}"
            for x in north:
                if not self.lanes[x]["spare"]:
                    self.xconnects.append((self.ocs_port[x], self.ocs_port[self.peer[x]]))
        # routing domain: topology 2 excludes L2, as scaleup_paths does
        excl = {"l2"} if has_ocs else set()
        self.routing = sorted((d for d, v in self.devices.items() if v["tier"] not in excl), key=devkey)
        self.npus = sorted((d for d, v in self.devices.items() if v["tier"] == "npu"), key=devkey)
        self.idx = {d: i for i, d in enumerate(self.routing)}

    # live lanes -> groups -> adjacency
    def groups(self, failed, peer=None, active_extra=()):
        """failed: set of lane ids that are dead. Returns {(v,u): sorted lanes at v toward u}."""
        peer = peer or self.peer
        out = collections.defaultdict(list)
        for x, y in peer.items():
            if y is None or x in failed or y in failed:
                continue
            if self.lanes[x]["spare"] and x not in active_extra:
                continue
            if self.lanes[y]["spare"] and y not in active_extra:
                continue
            out[(self.lanes[x]["device"], self.lanes[y]["device"])].append(x)
        return {k: sorted(v, key=lanekey) for k, v in out.items()}

    def adjacency(self, groups):
        adj = {d: [] for d in self.routing}
        for (v, u) in groups:
            if v in self.idx and u in self.idx:
                adj[v].append(u)
        return {d: sorted(set(n), key=devkey) for d, n in adj.items()}


# ---------------------------------------------------------------- 3. Routing Engine (BFS per destination)
def compute_routes(topo, adj):
    """routes[v][t] = (next hops tuple, hops); dist[t][v] = hop count."""
    R = topo.routing
    ix = topo.idx
    nbr = [[ix[u] for u in adj[v]] for v in R]
    routes = {v: {} for v in R}
    dist_all = {}
    for t in topo.npus:
        ti = ix[t]
        dist = [-1] * len(R)
        dist[ti] = 0
        frontier = [ti]
        while frontier:
            nxt = []
            for v in frontier:
                dv = dist[v] + 1
                for u in nbr[v]:
                    if dist[u] < 0:
                        dist[u] = dv
                        nxt.append(u)
            frontier = nxt
        dist_all[t] = dist
        for vi, v in enumerate(R):
            if vi == ti or dist[vi] < 0:
                continue
            want = dist[vi] - 1
            routes[v][t] = (tuple(R[u] for u in nbr[vi] if dist[u] == want), dist[vi])
    return routes, dist_all


def diff_routes(old, new):
    """{device: [(dest, old entry, new entry)]}, compared on next hops and hops."""
    out = {}
    for v in old:
        ch = []
        for t in sorted(set(old[v]) | set(new[v]), key=devkey):
            if old[v].get(t) != new[v].get(t):
                ch.append((t, old[v].get(t), new[v].get(t)))
        if ch:
            out[v] = ch
    return out


def diff_routes_nh_only(old, new):
    n = 0
    for v in old:
        for t in set(old[v]) | set(new[v]):
            a, b = old[v].get(t), new[v].get(t)
            if (a and a[0]) != (b and b[0]):
                n += 1
    return n


def order_waves(changed, endpoints, direction):
    transit = sorted((d for d in changed if d not in endpoints), key=devkey)
    ends = sorted((d for d in changed if d in endpoints), key=devkey)
    waves = [transit, ends] if direction == "down" else [ends, transit]
    return [w for w in waves if w]


# ---------------------------------------------------------------- 4. loop / blackhole checker
def lfa_table(topo, routes, dist, adj):
    """backup[v][t] = sorted neighbors N (not primary) with d(N,t) < d(N,v) + d(v,t) = 1 + d(v,t)."""
    ix = topo.idx
    out = {}
    for v in topo.routing:
        bv = {}
        for t, (nh, h) in routes[v].items():
            d = dist[t]
            prim = set(nh)
            b = tuple(N for N in adj[v] if N not in prim and 0 <= d[ix[N]] < 1 + h)
            if b:
                bv[t] = b
        out[v] = bv
    return out


def check_state(topo, table_of, live, backup_of=None):
    """table_of(v) -> routes dict of v; live: set of (v,u) usable for forwarding.
    backup_of(v) -> LFA dict of v, used only when every primary next hop is dead.
    Returns (looping NPU pairs, blackholed NPU pairs) over all (src NPU, dst NPU), src != dst."""
    R, ix = topo.routing, topo.idx
    n = len(R)
    npu_idx = [ix[s] for s in topo.npus]
    tabs = [table_of(v) for v in R]
    baks = [backup_of(v) if backup_of else {} for v in R]
    loops = holes = 0
    for t in topo.npus:
        ti = ix[t]
        succ = [[] for _ in range(n)]
        drop = [False] * n
        for vi in range(n):
            if vi == ti:
                continue
            e = tabs[vi].get(t)
            nh = [ix[u] for u in (e[0] if e else ()) if (R[vi], u) in live]
            if not nh:
                nh = [ix[u] for u in baks[vi].get(t, ()) if (R[vi], u) in live]
            if nh:
                succ[vi] = nh
            else:
                drop[vi] = True
        # nodes on cycles: iterative Tarjan
        on_cycle = scc_cyclic(succ)
        pred = [[] for _ in range(n)]
        for v in range(n):
            for u in succ[v]:
                pred[u].append(v)
        bad_loop = reach_back(pred, [v for v in range(n) if on_cycle[v]])
        bad_drop = reach_back(pred, [v for v in range(n) if drop[v]])
        for s in npu_idx:
            if s == ti:
                continue
            loops += bad_loop[s]
            holes += bad_drop[s]
    return loops, holes


def scc_cyclic(succ):
    n = len(succ)
    index = [-1] * n
    low = [0] * n
    onstk = [False] * n
    stk, res, counter = [], [False] * n, 0
    for root in range(n):
        if index[root] >= 0:
            continue
        work = [(root, 0)]
        while work:
            v, i = work.pop()
            if i == 0:
                index[v] = low[v] = counter
                counter += 1
                stk.append(v)
                onstk[v] = True
            recurse = False
            while i < len(succ[v]):
                u = succ[v][i]
                i += 1
                if index[u] < 0:
                    work.append((v, i))
                    work.append((u, 0))
                    recurse = True
                    break
                if onstk[u]:
                    low[v] = min(low[v], index[u])
            if recurse:
                continue
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stk.pop()
                    onstk[w] = False
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1 or v in succ[v]:
                    for w in comp:
                        res[w] = True
            if work:
                p, _ = work[-1]
                low[p] = min(low[p], low[v])
    return res


def reach_back(pred, seeds):
    seen = [False] * len(pred)
    st = list(seeds)
    for s in st:
        seen[s] = True
    while st:
        v = st.pop()
        for p in pred[v]:
            if not seen[p]:
                seen[p] = True
                st.append(p)
    return seen


# ---------------------------------------------------------------- 5. load model (uniform all-to-all)
def lane_loads(topo, table_of, live, groups, backup_of=None):
    """1 unit for every ordered NPU pair; equal split over live next hops, then over group lanes.
    Returns (dict lane -> load in the lane's sending direction, dropped units)."""
    R, ix = topo.routing, topo.idx
    n = len(R)
    tabs = [table_of(v) for v in R]
    baks = [backup_of(v) if backup_of else {} for v in R]
    load = collections.defaultdict(float)
    dropped = 0.0
    for t in topo.npus:
        ti = ix[t]
        succ = [None] * n
        indeg = [0] * n
        for vi in range(n):
            if vi == ti:
                continue
            e = tabs[vi].get(t)
            nh = [ix[u] for u in (e[0] if e else ()) if (R[vi], u) in live]
            if not nh:
                nh = [ix[u] for u in baks[vi].get(t, ()) if (R[vi], u) in live]
            succ[vi] = nh
            for u in nh:
                indeg[u] += 1
        flow = [0.0] * n
        for s in topo.npus:
            if s != t:
                flow[ix[s]] += 1.0
        order = [v for v in range(n) if indeg[v] == 0]
        k = 0
        while k < len(order):
            v = order[k]
            k += 1
            if v == ti:
                continue
            nh = succ[v]
            if not nh:
                dropped += flow[v]
                continue
            share = flow[v] / len(nh)
            for u in nh:
                flow[u] += share
                ls = groups[(R[v], R[u])]
                per = share / len(ls)
                for x in ls:
                    load[x] += per
                indeg[u] -= 1
                if indeg[u] == 0:
                    order.append(u)
        assert len(order) == n, "forwarding graph has a loop"
    return load, dropped


def load_summary(topo, load, peer=None):
    """Max lane load per direction class, e.g. 'l1->l2'; d2d and optical NPU links are kept apart."""
    peer = peer or topo.peer
    by = collections.defaultdict(float)
    for x, l in load.items():
        kind = topo.ports[topo.lanes[x]["port"]]["kind"]
        a = topo.devices[topo.lanes[x]["device"]]["tier"]
        b = topo.devices[topo.lanes[peer[x]]["device"]]["tier"]
        key = f"{a}->{b}" + (f" ({kind})" if a == b == "npu" else "")
        by[key] = max(by[key], l)
    return {k: round(v, 6) for k, v in sorted(by.items())}


# ---------------------------------------------------------------- 6. timing and messages
TIMING = {
    "typical": dict(local_prune=1, report=1, plan=1, compute=1, ocs_command=5, mirror_move=25,
                    lane_bringup=50, hello_check=2, error_check=10, lane_up_report=1,
                    device_write=3, correlation_window=5),
    "worst": dict(local_prune=5, report=5, plan=5, compute=5, ocs_command=50, mirror_move=200,
                  lane_bringup=2500, hello_check=200, error_check=2000, lane_up_report=5,
                  device_write=10, correlation_window=5),
}
TIMEOUTS = dict(ack=50, ocs_done=1000, verify=6000)


def ocs_restore_timeline(p):
    t = {}
    t["lane_down_at_controller"] = p["report"]
    t["plan_done"] = t["lane_down_at_controller"] + p["plan"]
    t["prepare_acked"] = t["plan_done"] + p["device_write"]
    t["ocs_applied"] = t["plan_done"] + p["ocs_command"]
    t["ocs_done"] = t["ocs_applied"] + p["mirror_move"]
    t["light"] = t["ocs_done"] + p["lane_bringup"]
    t["hello_ok"] = t["light"] + p["hello_check"]
    t["errors_clean"] = t["hello_ok"] + p["error_check"]
    t["lane_up_at_controller"] = t["errors_clean"] + p["lane_up_report"]
    return t


def repair_timeline(p, t_repair):
    t = {}
    t["light"] = t_repair + p["lane_bringup"]
    t["hello_ok"] = t["light"] + p["hello_check"]
    t["errors_clean"] = t["hello_ok"] + p["error_check"]
    t["lane_up_at_controller"] = t["errors_clean"] + p["lane_up_report"]
    return t


# ---------------------------------------------------------------- 7. scenarios
def counts(topo):
    kinds = collections.Counter(k for _, _, k in topo.links)
    by_pair = collections.Counter()
    for pa, pb, k in topo.links:
        a, b = topo.ports[pa]["device"], topo.ports[pb]["device"]
        ta, tb = sorted((topo.devices[a]["tier"], topo.devices[b]["tier"]), key=TIER_RANK.get)
        ra, rb = topo.devices[a]["role"], topo.devices[b]["role"]
        by_pair[f"{ta}-{tb}" + ("(union-ext)" if {ra, rb} == {"union", "ext"} else "")
                + (f"[{k}]" if k in ("d2d", "optical") else "")] += 1
    tiers = collections.Counter(v["tier"] for v in topo.devices.values())
    roles = collections.Counter(v["role"] for v in topo.devices.values())
    return dict(
        script_edges=topo.script_edges, internal_edges_dropped=topo.internal_edges,
        devices=len(topo.devices), tiers=dict(tiers), roles=dict(roles),
        links=len(topo.links), links_by_type=dict(sorted(by_pair.items())),
        lanes=len(topo.lanes), spare_lanes=sum(1 for v in topo.lanes.values() if v["spare"]),
        optical_domains=len(topo.domains),
        optical_domains_by_tier=dict(sorted(collections.Counter(
            topo.devices[d["device"]]["tier"] for d in topo.domains.values()).items(), key=lambda kv: TIER_RANK[kv[0]])),
        working_modules=sum(1 for m in topo.modules.values() if not m["spare"]),
        spare_modules=sum(1 for m in topo.modules.values() if m["spare"]),
        ocs_xconnects=len(topo.xconnects),
        ocs_ports_north=sum(1 for x in topo.ocs_port.values() if x[0] == "N"),
        routing_devices=len(topo.routing), npus=len(topo.npus),
    )


def capacities(topo, failed, domains=(), pairs=(), devices=(), peer=None, in_service_spares=()):
    """Capacity = in-service lanes (lane and its peer alive; spares count once they are in service)
    / lanes in service at startup, per domain, device pair and device."""
    peer = peer or topo.peer

    def serving(x):
        y = peer.get(x)
        if y is None or x in failed or y in failed:
            return False
        for z in (x, y):
            if topo.lanes[z]["spare"] and z not in in_service_spares:
                return False
        return True

    def startup(x):
        y = topo.peer.get(x)
        return y is not None and not topo.lanes[x]["spare"]

    out = {}
    for d in domains:
        dom = topo.domains[d]
        lanes = [f"{p}.{j}" for p in dom["ports"] for j in range(2)]
        lanes += [x for m in dom["spares"] for x in topo.modules[m]["lanes"]]
        out[d] = sum(serving(x) for x in lanes) / sum(startup(x) for x in lanes)
    for a, b in pairs:
        now = [x for x in topo.lanes if topo.lanes[x]["device"] == a and peer.get(x)
               and topo.lanes[peer[x]]["device"] == b]
        start = [x for x in topo.lanes if topo.lanes[x]["device"] == a and topo.peer.get(x)
                 and topo.lanes[topo.peer[x]]["device"] == b and startup(x)]
        out[f"{a}/{b}"] = sum(serving(x) for x in now) / len(start)
    for v in devices:
        lanes = [x for x in topo.lanes if topo.lanes[x]["device"] == v]
        out[v] = sum(serving(x) for x in lanes) / sum(startup(x) for x in lanes)
    return out


def fail_lanes(topo, modules):
    s = set()
    for m in modules:
        s |= set(topo.modules[m]["lanes"])
    return s


def group_diff(g0, g1):
    """Entries whose lane list changed (non-empty to non-empty or to empty)."""
    out = []
    for k in sorted(set(g0) | set(g1), key=lambda k: (devkey(k[0]), devkey(k[1]))):
        if g0.get(k) != g1.get(k):
            out.append((k, g0.get(k, []), g1.get(k, [])))
    return out


def lfa_report(topo, routes0, dist0, live1, entries):
    """For entries (S, t) whose primary next hops are all dead: count those with a neighbor N
    (live link, not a primary) that meets d(N,t) < d(N,S) + d(S,t) on the pre-failure topology."""
    ix = topo.idx
    covered = 0
    for S, t in entries:
        prim = set(routes0[S][t][0])
        dS = dist0[t][ix[S]]
        ok = False
        for (v, N) in live1:
            if v != S or N in prim or N not in ix:
                continue
            if dist0[t][ix[N]] < 1 + dS:
                ok = True
                break
        covered += ok
    return covered


def global_lfa(topo, routes0, dist0, adj0):
    single = covered = 0
    ix = topo.idx
    for S in topo.routing:
        for t, (nh, h) in routes0[S].items():
            if len(nh) != 1:
                continue
            single += 1
            d = dist0[t]
            if any(N != nh[0] and d[ix[N]] < 1 + h for N in adj0[S]):
                covered += 1
    return single, covered


def table_entries(routes):
    return sum(len(v) for v in routes.values())


def reroute_analysis(topo, failed, label):
    """Neighbor-loss analysis shared by G2, G7, G8."""
    g0 = topo.groups(set())
    adj0 = topo.adjacency(g0)
    routes0, dist0 = topo.r0
    g1 = topo.groups(failed)
    adj1 = topo.adjacency(g1)
    routes1, dist1 = compute_routes(topo, adj1)
    lost = sorted({(v, u) for (v, u) in g0 if (v, u) not in g1 and v in topo.idx and u in topo.idx},
                  key=lambda k: (devkey(k[0]), devkey(k[1])))
    endpoints = sorted({v for v, _ in lost}, key=devkey)
    d = diff_routes(routes0, routes1)
    nh_only = diff_routes_nh_only(routes0, routes1)
    changed = sorted(d, key=devkey)
    waves = order_waves(changed, set(endpoints), "down")
    live0, live1 = set(g0), set(g1)
    # entries at endpoints that lost every primary next hop
    no_nh = [(v, t) for v in endpoints for t, (nh, _) in routes0[v].items()
             if nh and not any((v, u) in live1 for u in nh)]
    lfa_cov = lfa_report(topo, routes0, dist0, live1, no_nh)
    res = dict(label=label)
    res["lost_adjacencies"] = [[v, u] for v, u in lost]
    res["endpoints"] = endpoints
    res["route_entries_changed"] = sum(len(v) for v in d.values())
    res["route_entries_changed_nexthops_only"] = nh_only
    res["devices_changed"] = len(changed)
    res["changed_by_role"] = dict(collections.Counter(topo.devices[v]["role"] for v in changed))
    res["entries_by_device_role"] = dict(collections.Counter(
        topo.devices[v]["role"] for v in changed for _ in d[v]))
    res["wave_sizes_down"] = [len(w) for w in waves]
    res["waves_down"] = waves if sum(len(w) for w in waves) <= 12 else [w[:6] + ["..."] for w in waves]
    res["entries_without_live_nexthop"] = len(no_nh)
    res["lfa_covered"] = lfa_cov
    # dests touched
    res["dests_touched"] = len({t for v in d for t, _, _ in d[v]})

    def mix(updated, new_routes, old_routes):
        return lambda v: new_routes[v] if v in updated else old_routes[v]

    # down: live = post-failure groups throughout (local prune already happened)
    states = {}
    states["before_controller"] = check_state(topo, mix(set(), routes1, routes0), live1)
    cum = set()
    for i, w in enumerate(waves):
        cum |= set(w)
        states[f"after_wave{i + 1}"] = check_state(topo, mix(cum, routes1, routes0), live1)
    wrong = list(reversed(waves))
    cum = set()
    for i, w in enumerate(wrong[:-1]):
        cum |= set(w)
        states[f"wrong_order_after_wave{i + 1}"] = check_state(topo, mix(cum, routes1, routes0), live1)
    res["down_states_(loop_pairs,blackhole_pairs)"] = states
    # the same states with LFA backups installed (used only when all primaries are dead)
    b0 = lfa_table(topo, routes0, dist0, adj0)
    b1 = lfa_table(topo, routes1, dist1, adj1)

    def bmix(updated):
        return lambda v: b1[v] if v in updated else b0[v]

    lstates = {"before_controller": check_state(topo, mix(set(), routes1, routes0), live1, bmix(set()))}
    cum = set()
    for i, w in enumerate(waves):
        cum |= set(w)
        lstates[f"after_wave{i + 1}"] = check_state(topo, mix(cum, routes1, routes0), live1, bmix(cum))
    cum = set()
    for i, w in enumerate(list(reversed(waves))[:-1]):
        cum |= set(w)
        lstates[f"wrong_order_after_wave{i + 1}"] = check_state(topo, mix(cum, routes1, routes0), live1, bmix(cum))
    res["down_states_with_lfa"] = lstates
    res["lfa_entries_old_tables"] = sum(len(v) for v in b0.values())
    topo.lfa0 = b0
    # restore: endpoints' groups come back with their routes (batched, group first)
    rwaves = order_waves(changed, set(endpoints), "up")
    rstates = {}

    def live_restore(updated):
        s = set(live1)
        for (v, u) in lost:
            if v in updated:
                s.add((v, u))
        return s

    cum = set()
    rstates["before_restore"] = check_state(topo, mix(cum, routes0, routes1), live_restore(cum))
    for i, w in enumerate(rwaves):
        cum |= set(w)
        rstates[f"after_wave{i + 1}"] = check_state(topo, mix(cum, routes0, routes1), live_restore(cum))
    cum = set()
    for i, w in enumerate(list(reversed(rwaves))[:-1]):
        cum |= set(w)
        rstates[f"wrong_order_after_wave{i + 1}"] = check_state(
            topo, mix(cum, routes0, routes1), live_restore(cum))
    res["restore_wave_sizes"] = [len(w) for w in rwaves]
    res["restore_states_(loop_pairs,blackhole_pairs)"] = rstates
    topo.cache[label] = dict(routes1=routes1, g1=g1, live1=live1, d=d, waves=waves, rwaves=rwaves,
                             lost=lost, endpoints=endpoints)
    return res


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Reference model: writes golden.json and golden_raw.json")
    ap.add_argument("--out", default="ref_out", help="output folder (default ./ref_out); "
                    "compare its golden.json with reference/golden.json")
    ap.add_argument("--full", action="store_true", help="also write the later-phase numbers "
                    "(loads, loop-free backups, rollback, partial restore) to golden_full.json")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    out = {}
    print("building topologies ...", flush=True)
    variants = {
        "T1_2p2": Topo("T1", "topo1_910d.py", has_ocs=False, two_plus_two=True),
        "T1_2to1": Topo("T1", "topo1_910d.py", has_ocs=False, two_plus_two=False),
        "T2_2p2": Topo("T2", "topo2_ocs_sure.py", has_ocs=True, two_plus_two=True, spare_modules=1),
        "T2_2to1": Topo("T2", "topo2_ocs_sure.py", has_ocs=True, two_plus_two=False, spare_modules=1),
        "T2_2p2_nospare": Topo("T2", "topo2_ocs_sure.py", has_ocs=True, two_plus_two=True, spare_modules=0),
    }
    for k, t in variants.items():
        t.cache = {}
        out[f"counts_{k}"] = counts(t)
    print(json.dumps({k: v for k, v in out.items()}, indent=1))

    # initial tables (route tables do not depend on 2+2 / 2:1)
    for k in ("T1_2p2", "T2_2p2"):
        t = variants[k]
        g0 = t.groups(set())
        t.r0 = compute_routes(t, t.adjacency(g0))
    variants["T1_2to1"].r0 = variants["T1_2p2"].r0
    variants["T2_2to1"].r0 = variants["T2_2p2"].r0
    variants["T2_2p2_nospare"].r0 = variants["T2_2p2"].r0
    for k in ("T1_2p2", "T2_2p2"):
        t = variants[k]
        routes0, dist0 = t.r0
        g0 = t.groups(set())
        adj0 = t.adjacency(g0)
        nh_sizes = collections.Counter(len(nh) for v in routes0 for nh, _ in routes0[v].values())
        hops = collections.Counter(h for v in routes0 for _, h in routes0[v].values())
        npu_pair_hops = collections.Counter(routes0[s][d][1] for s in t.npus for d in t.npus if s != d)
        single, cov = global_lfa(t, routes0, dist0, adj0)
        out[f"tables_{t.name}"] = dict(
            route_entries=table_entries(routes0),
            group_entries=len(g0),
            group_entries_in_routing_domain=sum(1 for (v, u) in g0 if v in t.idx and u in t.idx),
            npu_pair_hops=dict(sorted(npu_pair_hops.items())),
            max_hops=max(hops),
            nexthop_set_sizes=dict(sorted(nh_sizes.items())),
            single_nexthop_entries=single, single_nexthop_with_lfa=cov,
            steady_loops_blackholes=check_state(t, lambda v: routes0[v], set(g0)),
        )
        print(f"tables_{t.name}", json.dumps(out[f'tables_{t.name}']), flush=True)

    # ---------------- G1: T1, 2+2, l1-1536.m0 fails, no OCS
    t = variants["T1_2p2"]
    m = "l1-1536.m0"
    failed = fail_lanes(t, [m])
    g0, g1 = t.groups(set()), t.groups(failed)
    gd = group_diff(g0, g1)
    adj_same = t.adjacency(g0) == t.adjacency(g1)
    reporters = sorted({t.lanes[x]["device"] for x in failed} | {t.lanes[t.peer[x]]["device"] for x in failed},
                       key=devkey)
    G1 = dict(module=m, module_lanes=t.modules[m]["lanes"], domain=t.modules[m]["domain"],
              domain_ports=t.domains[t.modules[m]["domain"]]["ports"],
              domain_port_peers=[t.ports[p]["peer"] for p in t.domains[t.modules[m]["domain"]]["ports"]],
              group_entries_pruned=len(gd), group_devices=len({k[0] for k, _, _ in gd}),
              prunes=[dict(device=k[0], neighbor=k[1], before=a, after=b) for k, a, b in gd],
              neighbors_lost=0 if adj_same else "SOME", reporters=reporters)
    tp = TIMING["typical"]
    rt = repair_timeline(tp, 10000)
    G1["repair_typical"] = dict(rt, groups_acked=rt["lane_up_at_controller"] + tp["device_write"])
    G1["messages"] = dict(LANE_DOWN=len(reporters), LANE_UP=len(reporters), GROUP_SET=len(reporters),
                          ACK=len(reporters))
    G1["messages_total"] = sum(G1["messages"].values())
    # loads
    routes0, _ = t.r0
    l0, dr0 = lane_loads(t, lambda v: routes0[v], set(g0), g0)
    l1, dr1 = lane_loads(t, lambda v: routes0[v], set(g1), g1)
    G1["load_steady_max_by_class"] = load_summary(t, l0)
    G1["load_after_max_by_class"] = load_summary(t, l1)
    halved = [x for x in t.modules["l1-1536.m1"]["lanes"]]
    G1["load_on_surviving_uplink_lanes_before_after"] = [(x, round(l0[x], 3), round(l1[x], 3)) for x in halved]
    G1["dropped_after"] = dr1
    G1["capacity"] = capacities(t, failed, domains=["l1-1536.d0"],
                                pairs=[("l1-1536", f"l2-{2304 + k}") for k in range(4)],
                                devices=["l1-1536", "l2-2304"])
    G1["degraded_waiting_repair_at"] = TIMING["typical"]["report"] + TIMING["typical"]["plan"]
    out["G1"] = G1
    print("G1", json.dumps(G1, indent=1), flush=True)

    # ---------------- G2: T1, 2:1, same module fails, no OCS
    t = variants["T1_2to1"]
    failed = fail_lanes(t, [m])
    g0, g1 = t.groups(set()), t.groups(failed)
    gd = group_diff(g0, g1)
    reporters = sorted({t.lanes[x]["device"] for x in failed} | {t.lanes[t.peer[x]]["device"] for x in failed},
                       key=devkey)
    G2 = dict(module=m, module_lanes=t.modules[m]["lanes"],
              group_entries_emptied=len(gd), prunes=[dict(device=k[0], neighbor=k[1], before=a, after=b) for k, a, b in gd],
              reporters=reporters)
    G2.update(reroute_analysis(t, failed, "G2"))
    c = t.cache["G2"]
    routes0, _ = t.r0
    # loads: steady, gap (before controller, after local prune), final
    lgap, drgap = lane_loads(t, lambda v: routes0[v], c["live1"], c["g1"])
    lfin, drfin = lane_loads(t, lambda v: c["routes1"][v], c["live1"], c["g1"])
    G2["dropped_units_in_gap"] = drgap
    G2["total_units"] = len(t.npus) * (len(t.npus) - 1)
    G2["load_final_max_by_class"] = load_summary(t, lfin)
    G2["dropped_final"] = drfin
    G2["load_on_1536_uplinks_final"] = [(x, round(lfin[x], 3)) for x in
                                        sorted((x for x in lfin if x.startswith("l1-1536.p1")), key=lanekey)]
    # an example entry per role
    ex = {}
    for v in c["d"]:
        r = t.devices[v]["role"]
        if r not in ex:
            dest, a, b = c["d"][v][0]
            ex[r] = (v, dest, a, b)
    ex["hrs"] = next((v, *c["d"][v][0]) for v in c["d"] if t.devices[v]["role"] == "hrs")
    G2["examples"] = {r: f"{v} dest {dest}: {list(a[0])} (hops {a[1]}) -> {len(b[0])} next hops "
                         f"{list(b[0])[:4]}{'...' if len(b[0]) > 4 else ''} (hops {b[1]})"
                      for r, (v, dest, a, b) in ex.items()}
    # timeline
    for prof in ("typical", "worst"):
        p = TIMING[prof]
        tl = dict(lane_down_at_controller=p["report"])
        tl["plan_done"] = tl["lane_down_at_controller"] + p["plan"]
        tl["compute_done"] = tl["plan_done"] + p["compute"]
        tl["wave1_acked"] = tl["compute_done"] + p["device_write"]
        tl["wave2_acked"] = tl["wave1_acked"] + p["device_write"]
        rt = repair_timeline(p, 10000)
        rt["compute_done"] = rt["lane_up_at_controller"] + p["compute"]
        rt["wave1_acked"] = rt["compute_done"] + p["device_write"]
        rt["wave2_acked"] = rt["wave1_acked"] + p["device_write"]
        G2[f"timeline_{prof}"] = dict(down=tl, repair=rt)
    w = [len(x) for x in c["waves"]]
    rw = [len(x) for x in c["rwaves"]]
    G2["messages"] = dict(LANE_DOWN=len(reporters), ROUTE_SET_down=sum(w), ACK_down=sum(w),
                          LANE_UP=len(reporters), SET_restore=sum(rw), ACK_restore=sum(rw))
    G2["messages_total"] = sum(G2["messages"].values())
    G2["entries_per_device"] = {"l1-1536": len(c["d"]["l1-1536"]),
                                "each_other_changed_union": sorted({len(c["d"][v]) for v in c["d"]
                                                                    if t.devices[v]["role"] == "union"
                                                                    and v != "l1-1536"}),
                                "l2-2304": len(c["d"]["l2-2304"]), "l2-2305": len(c["d"]["l2-2305"])}
    G2["degraded_waiting_repair_at"] = G2["timeline_typical"]["down"]["wave2_acked"]
    out["G2"] = G2
    print("G2", json.dumps(G2, indent=1, default=str), flush=True)

    # ---------------- G4: T2, OCS on, 1 spare, module npu-0.m0 fails (2+2 and 2:1)
    for key in ("T2_2p2", "T2_2to1"):
        t = variants[key]
        mm = "npu-0.m0"
        failed = fail_lanes(t, [mm])
        g0, g1 = t.groups(set()), t.groups(failed)
        gd = group_diff(g0, g1)
        same_adj = t.adjacency(g0) == t.adjacency(g1)
        reporters = sorted({t.lanes[x]["device"] for x in failed} | {t.lanes[t.peer[x]]["device"] for x in failed},
                           key=devkey)
        dom = t.modules[mm]["domain"]
        spare = t.domains[dom]["spares"][0]
        sl = t.modules[spare]["lanes"]
        dead = sorted(failed, key=lanekey)
        disconnect = [(t.ocs_port[x], t.ocs_port[t.peer[x]]) for x in dead]
        connect = [(t.ocs_port[s], t.ocs_port[t.peer[x]]) for s, x in zip(sl, dead)]
        newpeer = dict(t.peer)
        for s, x in zip(sl, dead):
            y = t.peer[x]
            newpeer[s], newpeer[y], newpeer[x] = y, s, None
        g2 = t.groups(failed, peer=newpeer, active_extra=set(sl))
        gd2 = group_diff(g1, g2)
        G4 = dict(variant=key, module=mm, module_lanes=t.modules[mm]["lanes"], domain=dom,
                  domain_ports=t.domains[dom]["ports"], spare=spare, spare_lanes=sl,
                  ocs_ports_of_npu0=[(x, t.ocs_port[x]) for x in sorted(
                      (x for x in t.ocs_port if x.startswith("npu-0.")), key=lanekey)],
                  peer_ocs_ports=[(t.peer[x], t.ocs_port[t.peer[x]]) for x in sorted(
                      (x for x in t.ocs_port if x.startswith("npu-0.") and not t.lanes[x]["spare"]), key=lanekey)],
                  group_entries_pruned=len(gd), prunes=[dict(device=k[0], neighbor=k[1], before=a, after=b) for k, a, b in gd],
                  neighbors_lost=0 if same_adj else "SOME", reporters=reporters,
                  ocs_disconnect=disconnect, ocs_connect=connect,
                  group_set=[dict(device=k[0], neighbor=k[1], lanes=b) for k, a, b in gd2],
                  pair_capacity_during=len(g1[("npu-0", "npu-64")]) / len(g0[("npu-0", "npu-64")]))
        G4["capacity_during"] = capacities(t, failed, domains=[dom], pairs=[("npu-0", "npu-64")], devices=["npu-0"])
        G4["capacity_final"] = capacities(t, failed, domains=[dom], pairs=[("npu-0", "npu-64")], devices=["npu-0"],
                                          peer=newpeer, in_service_spares=set(sl))
        G4["end_state"] = "CLOSED" if G4["capacity_final"]["npu-0/npu-64"] == 1.0 else "DEGRADED"
        for prof in ("typical", "worst"):
            p = TIMING[prof]
            tl = ocs_restore_timeline(p)
            tl["groups_acked"] = tl["lane_up_at_controller"] + p["device_write"]
            G4[f"timeline_{prof}"] = tl
        n = len(reporters)
        G4["messages"] = dict(LANE_DOWN=n, PREPARE=n, ACK_prepare=n, OCS_SET=1, OCS_DONE=1, LANE_UP=n,
                              GROUP_SET=n, ACK_group=n)
        G4["messages_total"] = sum(G4["messages"].values())
        out[f"G4_{key}"] = G4
        print(f"G4_{key}", json.dumps(G4, indent=1), flush=True)

    # ---------------- G7 / G8: T2, both modules of npu-0's optical domain fail
    t = variants["T2_2p2"]
    failed = fail_lanes(t, ["npu-0.m0", "npu-0.m1"])
    G7 = reroute_analysis(t, failed, "G7")
    c = t.cache["G7"]
    reporters = sorted({t.lanes[x]["device"] for x in failed} | {t.lanes[t.peer[x]]["device"] for x in failed},
                       key=devkey)
    G7["reporters"] = reporters
    g0 = t.groups(set())
    G7["examples"] = {}
    for v in c["d"]:
        r = t.devices[v]["role"]
        if r not in G7["examples"]:
            dest, a, b = c["d"][v][0]
            G7["examples"][r] = f"{v} dest {dest}: {list(a[0])} (hops {a[1]}) -> {list(b[0])} (hops {b[1]})"
    # spare restores the lanes of the first failed module only
    spare = t.domains["npu-0.d0"]["spares"][0]
    sl = t.modules[spare]["lanes"]
    dead_first = t.modules["npu-0.m0"]["lanes"]
    G7["ocs_disconnect"] = [(t.ocs_port[x], t.ocs_port[t.peer[x]]) for x in dead_first]
    G7["ocs_connect"] = [(t.ocs_port[s], t.ocs_port[t.peer[x]]) for s, x in zip(sl, dead_first)]
    newpeer = dict(t.peer)
    for s, x in zip(sl, dead_first):
        y = t.peer[x]
        newpeer[s], newpeer[y], newpeer[x] = y, s, None
    g3 = t.groups(failed, peer=newpeer, active_extra=set(sl))
    G7["group_after_restore"] = [dict(device=k[0], neighbor=k[1], lanes=g3[k])
                                 for k in (("npu-0", "npu-64"), ("npu-64", "npu-0"))]
    G7["pair_capacity_final"] = len(g3[("npu-0", "npu-64")]) / len(g0[("npu-0", "npu-64")])
    G7["capacity_final"] = capacities(t, failed, domains=["npu-0.d0"], pairs=[("npu-0", "npu-64")],
                                      devices=["npu-0"], peer=newpeer, in_service_spares=set(sl))
    G7["end_state"] = "CLOSED" if G7["capacity_final"]["npu-0/npu-64"] == 1.0 else "DEGRADED"
    routes3, _ = compute_routes(t, t.adjacency(g3))
    G7["final_routes_equal_initial"] = routes3 == t.r0[0]
    for prof in ("typical", "worst"):
        p = TIMING[prof]
        tl = ocs_restore_timeline(p)
        tl["compute_done"] = tl["plan_done"] + p["compute"]
        tl["reroute_wave1_acked"] = tl["compute_done"] + p["device_write"]
        tl["reroute_wave2_acked"] = tl["reroute_wave1_acked"] + p["device_write"]
        tl["restore_compute_done"] = tl["lane_up_at_controller"] + p["compute"]
        tl["restore_wave1_acked"] = tl["restore_compute_done"] + p["device_write"]
        tl["restore_wave2_acked"] = tl["restore_wave1_acked"] + p["device_write"]
        G7[f"timeline_{prof}"] = tl
    w = [len(x) for x in c["waves"]]
    rw = [len(x) for x in c["rwaves"]]
    G7["messages"] = dict(LANE_DOWN=2, ROUTE_SET_down=sum(w), ACK_down=sum(w), PREPARE=2, ACK_prepare=2,
                          OCS_SET=1, OCS_DONE=1, LANE_UP=2, SET_restore=sum(rw), ACK_restore=sum(rw))
    G7["messages_total"] = sum(G7["messages"].values())
    G8 = dict(messages=dict(LANE_DOWN=2, ROUTE_SET_down=sum(w), ACK_down=sum(w), LANE_UP=2,
                            SET_restore=sum(rw), ACK_restore=sum(rw)))
    G8["messages_total"] = sum(G8["messages"].values())
    p = TIMING["typical"]
    rt = repair_timeline(p, 10000)
    rt["compute_done"] = rt["lane_up_at_controller"] + p["compute"]
    rt["wave1_acked"] = rt["compute_done"] + p["device_write"]
    rt["wave2_acked"] = rt["wave1_acked"] + p["device_write"]
    G8["repair_timeline_typical"] = rt
    # 2:1 gives the same neighbor loss (both modules dead)
    t21 = variants["T2_2to1"]
    t21.cache = {}
    g21 = t21.groups(fail_lanes(t21, ["npu-0.m0", "npu-0.m1"]))
    G8["same_groups_as_2p2"] = set(g21) == set(c["g1"])
    routes0, dist0 = t.r0
    l0, _ = lane_loads(t, lambda v: routes0[v], set(g0), g0)
    lgap, drgap = lane_loads(t, lambda v: routes0[v], c["live1"], c["g1"])
    lgapb, drgapb = lane_loads(t, lambda v: routes0[v], c["live1"], c["g1"], backup_of=lambda v: t.lfa0[v])
    lfin, drfin = lane_loads(t, lambda v: c["routes1"][v], c["live1"], c["g1"])
    l3, dr3 = lane_loads(t, lambda v: routes3[v], set(g3), g3)
    G7["total_units"] = len(t.npus) * (len(t.npus) - 1)
    G7["load_steady_max_by_class"] = load_summary(t, l0)
    G7["dropped_units_in_gap"] = drgap
    G7["dropped_units_in_gap_with_lfa"] = drgapb
    G7["load_detour_max_by_class"] = load_summary(t, lfin)
    G7["dropped_detour"] = drfin
    G7["load_after_restore_max_by_class"] = load_summary(t, l3, newpeer)
    G7["load_sure_0_64_lanes_steady"] = {x: round(l0[x], 3) for x in g0[("npu-0", "npu-64")]}
    G7["load_sure_0_64_lanes_after_restore"] = {x: round(l3[x], 3) for x in g3[("npu-0", "npu-64")]}
    out["G7"] = G7
    out["G8"] = G8
    print("G7", json.dumps(G7, indent=1, default=str), flush=True)
    print("G8", json.dumps(G8, indent=1, default=str), flush=True)

    # ---------------- G6: no spare
    t = variants["T2_2p2_nospare"]
    out["G6"] = dict(spares_on_npu0=t.domains["npu-0.d0"]["spares"], messages_total=2,
                     result="DEGRADED_NO_SPARE at plan_done (2 ms typical), pair npu-0/npu-64 stays 0.5")
    # ---------------- G9 / G10 timelines
    p = TIMING["typical"]
    t_set = p["report"] + p["plan"]
    out["G9"] = dict(ocs_set_1=t_set, timeout_1=t_set + TIMEOUTS["ocs_done"],
                     timeout_2=t_set + 2 * TIMEOUTS["ocs_done"],
                     closed=t_set + 2 * TIMEOUTS["ocs_done"] + p["device_write"],
                     messages=dict(LANE_DOWN=2, PREPARE=2, ACK_prepare=2, OCS_SET=2, PREPARE_close=2,
                                   ACK_close=2), messages_total=12)
    tl = ocs_restore_timeline(p)
    gs = tl["lane_up_at_controller"]
    out["G10"] = dict(group_set_sent=gs, npu0_ack=gs + p["device_write"], devices_applied=gs + p["device_write"],
                      timeout=gs + TIMEOUTS["ack"], resend_acked=gs + TIMEOUTS["ack"] + p["device_write"],
                      messages_total=14 + 2)
    print("G6", out["G6"], "\nG9", out["G9"], "\nG10", out["G10"])
    rename = {"G4_T2_2p2": "G4a", "G4_T2_2to1": "G4b", "G6": "G5", "G7": "G6", "G8": "G7", "G9": "G8", "G10": "G9"}
    raw = {rename.get(k, k): v for k, v in out.items()}
    for v in raw.values():
        if isinstance(v, dict):
            v.pop("label", None)
    with open(os.path.join(args.out, "golden.json"), "w") as f:
        json.dump(rounded(make_golden_simple(out)), f, indent=1)
    if args.full:
        with open(os.path.join(args.out, "golden_full.json"), "w") as f:
            json.dump(rounded(make_golden(out)), f, indent=1)
        with open(os.path.join(args.out, "golden_raw.json"), "w") as f:
            json.dump(raw, f, indent=1, default=str)
    print(f"wrote {args.out}/golden.json" + (" (+ golden_full.json, golden_raw.json)" if args.full else ""))


def rounded(x):
    if isinstance(x, float):
        return round(x, 6)
    if isinstance(x, dict):
        return {k: rounded(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [rounded(v) for v in x]
    return x


def pairs(states):
    """{'state': [loops, blackholes]} -> {'state': {'looping_pairs': .., 'blackholed_pairs': ..}}"""
    return {k: {"looping_pairs": v[0], "blackholed_pairs": v[1]} for k, v in states.items()}


def make_golden(o):
    """The numbers docs/SPEC.md Section 11 quotes, with stable key names (spec scenario ids)."""
    def topo_block(c, tab, loads):
        return dict(
            devices=c["devices"], npu=c["tiers"]["npu"], l1=c["tiers"]["l1"], l1_union=c["roles"]["union"],
            l1_ext=c["roles"]["ext"], l2=c["tiers"]["l2"], links=c["links"], links_by_type=c["links_by_type"],
            lanes=c["lanes"], optical_domains=c["optical_domains"],
            optical_domains_by_tier=c["optical_domains_by_tier"], working_modules=c["working_modules"],
            spare_modules=c["spare_modules"], ocs_cross_connects=c["ocs_xconnects"],
            ocs_ports_per_side=c["ocs_ports_north"], routed_devices=c["routing_devices"],
            route_entries=tab["route_entries"], group_entries=tab["group_entries"],
            group_entries_in_routing_domain=tab["group_entries_in_routing_domain"],
            npu_pair_hops=tab["npu_pair_hops"], nexthop_set_sizes=tab["nexthop_set_sizes"],
            single_nexthop_entries=tab["single_nexthop_entries"],
            single_nexthop_entries_with_lfa=tab["single_nexthop_with_lfa"],
            steady_looping_pairs=tab["steady_loops_blackholes"][0],
            steady_blackholed_pairs=tab["steady_loops_blackholes"][1],
            steady_max_lane_load=loads)
    g = {"about": "Generated by reference/ref_model.py. Times in ms. Loads in demand units "
                  "(1 unit from every NPU to every other NPU). Pair counts are ordered (source NPU, "
                  "destination NPU) pairs."}
    g["topology1"] = topo_block(o["counts_T1_2p2"], o["tables_T1"], o["G1"]["load_steady_max_by_class"])
    g["topology2_spare1"] = topo_block(o["counts_T2_2p2"], o["tables_T2"], o["G7"]["load_steady_max_by_class"])
    c0 = o["counts_T2_2p2_nospare"]
    g["topology2_spare0"] = dict(lanes=c0["lanes"], spare_modules=c0["spare_modules"],
                                 ocs_ports_per_side=c0["ocs_ports_north"], ocs_cross_connects=c0["ocs_xconnects"])
    G1 = o["G1"]
    g["G1"] = dict(capacity=G1["capacity"], degraded_waiting_repair_at=G1["degraded_waiting_repair_at"],
                   module_lanes=G1["module_lanes"], group_entries_pruned=G1["group_entries_pruned"],
                   group_devices=G1["group_devices"], prunes=G1["prunes"], route_entries_changed=0,
                   reporters=G1["reporters"], repair=G1["repair_typical"], messages=G1["messages"],
                   messages_total=G1["messages_total"],
                   surviving_uplink_lane_load_before_after=G1["load_on_surviving_uplink_lanes_before_after"])
    G2 = o["G2"]
    g["G2"] = dict(entries_per_device=G2["entries_per_device"],
                   degraded_waiting_repair_at=G2["degraded_waiting_repair_at"],
                   module_lanes=G2["module_lanes"], emptied_groups=G2["prunes"], reporters=G2["reporters"],
                   lost_adjacencies=G2["lost_adjacencies"], endpoints=G2["endpoints"],
                   route_entries_changed=G2["route_entries_changed"], devices_changed=G2["devices_changed"],
                   devices_changed_by_role=G2["changed_by_role"], entries_by_role=G2["entries_by_device_role"],
                   waves_down=G2["wave_sizes_down"], waves_restore=G2["restore_wave_sizes"],
                   entries_without_live_nexthop=G2["entries_without_live_nexthop"],
                   lfa_covered=G2["lfa_covered"],
                   down_states=pairs(G2["down_states_(loop_pairs,blackhole_pairs)"]),
                   restore_states=pairs(G2["restore_states_(loop_pairs,blackhole_pairs)"]),
                   dropped_units_before_controller=G2["dropped_units_in_gap"],
                   max_lane_load_after_reroute=G2["load_final_max_by_class"],
                   timeline_typical=G2["timeline_typical"], timeline_worst=G2["timeline_worst"],
                   messages=G2["messages"], messages_total=G2["messages_total"])
    g["G3"] = dict(T1_down_wrong_order_looping_pairs=G2["down_states_(loop_pairs,blackhole_pairs)"]["wrong_order_after_wave1"][0],
                   T1_restore_wrong_order_looping_pairs=G2["restore_states_(loop_pairs,blackhole_pairs)"]["wrong_order_after_wave1"][0],
                   T2_down_wrong_order_looping_pairs=o["G7"]["down_states_(loop_pairs,blackhole_pairs)"]["wrong_order_after_wave1"][0],
                   T2_restore_wrong_order_looping_pairs=o["G7"]["restore_states_(loop_pairs,blackhole_pairs)"]["wrong_order_after_wave1"][0])
    for sub, key in (("a", "G4_T2_2p2"), ("b", "G4_T2_2to1")):
        G4 = o[key]
        g[f"G4{sub}"] = {k: G4[k] for k in ("capacity_during", "capacity_final", "end_state",
                                             "module_lanes", "spare", "spare_lanes", "prunes", "reporters",
                                             "ocs_disconnect", "ocs_connect", "group_set", "pair_capacity_during",
                                             "timeline_typical", "timeline_worst", "messages", "messages_total")}
    g["G5"] = dict(spares=o["G6"]["spares_on_npu0"], result="DEGRADED_NO_SPARE", decided_at=2, messages_total=2,
                   pair_capacity=0.5)
    G6 = o["G7"]
    g["G6"] = dict(lost_adjacencies=G6["lost_adjacencies"], endpoints=G6["endpoints"], reporters=G6["reporters"],
                   route_entries_changed=G6["route_entries_changed"], devices_changed=G6["devices_changed"],
                   devices_changed_by_role=G6["changed_by_role"], entries_by_role=G6["entries_by_device_role"],
                   waves_down=G6["wave_sizes_down"], waves_restore=G6["restore_wave_sizes"],
                   entries_without_live_nexthop=G6["entries_without_live_nexthop"], lfa_covered=G6["lfa_covered"],
                   down_states=pairs(G6["down_states_(loop_pairs,blackhole_pairs)"]),
                   restore_states=pairs(G6["restore_states_(loop_pairs,blackhole_pairs)"]),
                   dropped_units_before_controller=G6["dropped_units_in_gap"],
                   ocs_disconnect=G6["ocs_disconnect"], ocs_connect=G6["ocs_connect"],
                   groups_after_restore=G6["group_after_restore"], pair_capacity_final=G6["pair_capacity_final"],
                   capacity_final=G6["capacity_final"], end_state=G6["end_state"],
                   final_routes_equal_initial=G6["final_routes_equal_initial"],
                   restored_lane_load=G6["load_sure_0_64_lanes_after_restore"],
                   steady_sure_lane_load=G6["load_sure_0_64_lanes_steady"],
                   max_lane_load_on_detour=G6["load_detour_max_by_class"],
                   timeline_typical=G6["timeline_typical"], timeline_worst=G6["timeline_worst"],
                   messages=G6["messages"], messages_total=G6["messages_total"])
    G7 = o["G8"]
    g["G7"] = dict(degraded_waiting_repair_at=G6["timeline_typical"]["reroute_wave2_acked"],
                   repair_timeline_typical=G7["repair_timeline_typical"], messages=G7["messages"],
                   messages_total=G7["messages_total"], same_as_2to1=G7["same_groups_as_2p2"])
    g["G8"] = dict(o["G9"], result="FAILED_NEEDS_OPERATOR", min_pair_capacity=0.5)
    g["G9"] = dict(o["G10"], resends=1)
    g["G10"] = dict(T1_single_nexthop_entries_with_lfa=o["tables_T1"]["single_nexthop_with_lfa"],
                    T2_single_nexthop_entries_with_lfa=o["tables_T2"]["single_nexthop_with_lfa"],
                    T1_G2_with_lfa=pairs(G2["down_states_with_lfa"]),
                    T2_G6_with_lfa=pairs(G6["down_states_with_lfa"]),
                    T2_G6_dropped_units_before_controller_with_lfa=G6["dropped_units_in_gap_with_lfa"])
    return g


def make_golden_simple(o):
    """v1 (simple version) numbers quoted in docs/SPEC.md Section 11, keyed by the v1 scenario ids."""
    def topo_block(c, tab):
        return dict(
            devices=c["devices"], npu=c["tiers"]["npu"], l1=c["tiers"]["l1"], l1_union=c["roles"]["union"],
            l1_ext=c["roles"]["ext"], l2=c["tiers"]["l2"], links=c["links"], links_by_type=c["links_by_type"],
            lanes=c["lanes"], optical_domains=c["optical_domains"],
            optical_domains_by_tier=c["optical_domains_by_tier"], working_modules=c["working_modules"],
            spare_modules=c["spare_modules"], ocs_cross_connects=c["ocs_xconnects"],
            ocs_ports_per_side=c["ocs_ports_north"], routed_devices=c["routing_devices"],
            route_entries=tab["route_entries"], group_entries=tab["group_entries"],
            group_entries_in_routing_domain=tab["group_entries_in_routing_domain"],
            npu_pair_hops=tab["npu_pair_hops"], nexthop_set_sizes=tab["nexthop_set_sizes"],
            steady_looping_pairs=tab["steady_loops_blackholes"][0],
            steady_blackholed_pairs=tab["steady_loops_blackholes"][1])
    g = {"about": "v1 (simple version). Generated by reference/ref_model.py. Times in ms. Pair counts are "
                  "ordered (source NPU, destination NPU) pairs."}
    g["topology1"] = topo_block(o["counts_T1_2p2"], o["tables_T1"])
    g["topology2_spare1"] = topo_block(o["counts_T2_2p2"], o["tables_T2"])
    c0 = o["counts_T2_2p2_nospare"]
    g["topology2_spare0"] = dict(lanes=c0["lanes"], spare_modules=c0["spare_modules"],
                                 ocs_ports_per_side=c0["ocs_ports_north"], ocs_cross_connects=c0["ocs_xconnects"])
    G1 = o["G1"]
    g["G1"] = dict(module_lanes=G1["module_lanes"], prunes=G1["prunes"],
                   group_entries_pruned=G1["group_entries_pruned"], group_devices=G1["group_devices"],
                   route_entries_changed=0, reporters=G1["reporters"], capacity=G1["capacity"],
                   degraded_waiting_repair_at=G1["degraded_waiting_repair_at"], repair=G1["repair_typical"],
                   messages=G1["messages"], messages_total=G1["messages_total"])
    G2 = o["G2"]
    g["G2"] = dict(module_lanes=G2["module_lanes"], emptied_groups=G2["prunes"], reporters=G2["reporters"],
                   lost_adjacencies=G2["lost_adjacencies"], endpoints=G2["endpoints"],
                   route_entries_changed=G2["route_entries_changed"], devices_changed=G2["devices_changed"],
                   devices_changed_by_role=G2["changed_by_role"], entries_by_role=G2["entries_by_device_role"],
                   entries_per_device=G2["entries_per_device"],
                   waves_down=G2["wave_sizes_down"], waves_restore=G2["restore_wave_sizes"],
                   entries_without_live_nexthop=G2["entries_without_live_nexthop"],
                   down_states=pairs(G2["down_states_(loop_pairs,blackhole_pairs)"]),
                   restore_states=pairs(G2["restore_states_(loop_pairs,blackhole_pairs)"]),
                   degraded_waiting_repair_at=G2["degraded_waiting_repair_at"],
                   timeline_typical=G2["timeline_typical"], timeline_worst=G2["timeline_worst"],
                   messages=G2["messages"], messages_total=G2["messages_total"])
    T2 = o["G7"]          # topology 2: both modules of npu-0's optical domain fail
    g["G3"] = dict(T1_down_wrong_order_looping_pairs=G2["down_states_(loop_pairs,blackhole_pairs)"]["wrong_order_after_wave1"][0],
                   T1_restore_wrong_order_looping_pairs=G2["restore_states_(loop_pairs,blackhole_pairs)"]["wrong_order_after_wave1"][0],
                   T2_down_wrong_order_looping_pairs=T2["down_states_(loop_pairs,blackhole_pairs)"]["wrong_order_after_wave1"][0],
                   T2_restore_wrong_order_looping_pairs=T2["restore_states_(loop_pairs,blackhole_pairs)"]["wrong_order_after_wave1"][0])
    for sub, key in (("a", "G4_T2_2p2"), ("b", "G4_T2_2to1")):
        G4 = o[key]
        g[f"G4{sub}"] = {k: G4[k] for k in ("module_lanes", "spare", "spare_lanes", "prunes", "reporters",
                                             "ocs_disconnect", "ocs_connect", "group_set", "capacity_during",
                                             "capacity_final", "end_state", "timeline_typical", "timeline_worst",
                                             "messages", "messages_total")}
    g["G5"] = dict(spares=o["G6"]["spares_on_npu0"], result="DEGRADED_NO_SPARE", decided_at=2, messages_total=2,
                   pair_capacity=0.5)
    down_msgs = dict(LANE_DOWN=2, ROUTE_SET_down=sum(T2["wave_sizes_down"]), ACK_down=sum(T2["wave_sizes_down"]))
    g["G6"] = dict(lost_adjacencies=T2["lost_adjacencies"], endpoints=T2["endpoints"], reporters=T2["reporters"],
                   route_entries_changed=T2["route_entries_changed"], devices_changed=T2["devices_changed"],
                   devices_changed_by_role=T2["changed_by_role"], entries_by_role=T2["entries_by_device_role"],
                   waves_down=T2["wave_sizes_down"], waves_restore=T2["restore_wave_sizes"],
                   entries_without_live_nexthop=T2["entries_without_live_nexthop"],
                   down_states=pairs(T2["down_states_(loop_pairs,blackhole_pairs)"]),
                   restore_states=pairs(T2["restore_states_(loop_pairs,blackhole_pairs)"]),
                   reroute_timeline_typical={k: T2["timeline_typical"][k] for k in
                                             ("lane_down_at_controller", "plan_done", "compute_done",
                                              "reroute_wave1_acked", "reroute_wave2_acked")},
                   reroute_timeline_worst={k: T2["timeline_worst"][k] for k in
                                           ("lane_down_at_controller", "plan_done", "compute_done",
                                            "reroute_wave1_acked", "reroute_wave2_acked")},
                   degraded_waiting_repair_at=T2["timeline_typical"]["reroute_wave2_acked"],
                   repair_timeline_typical=o["G8"]["repair_timeline_typical"],
                   final_tables_equal_initial=T2["final_routes_equal_initial"],
                   same_as_2to1=o["G8"]["same_groups_as_2p2"],
                   messages=o["G8"]["messages"], messages_total=o["G8"]["messages_total"],
                   variant_b_ocs_on_one_spare=dict(plan="NoRepair(not_enough_spare)", result="DEGRADED_NO_SPARE",
                                                   decided_at=T2["timeline_typical"]["reroute_wave2_acked"],
                                                   messages=down_msgs, messages_total=sum(down_msgs.values())))
    p = TIMING["typical"]
    t_set = p["report"] + p["plan"]
    v1 = dict(LANE_DOWN=2, PREPARE=2, ACK_prepare=2, OCS_SET=2)
    g["G7"] = dict(ocs_set_1=t_set, timeout_1=t_set + TIMEOUTS["ocs_done"], ocs_set_2=t_set + TIMEOUTS["ocs_done"],
                   timeout_2=t_set + 2 * TIMEOUTS["ocs_done"], result="FAILED_NEEDS_OPERATOR",
                   ended_at=t_set + 2 * TIMEOUTS["ocs_done"], min_pair_capacity=0.5,
                   messages=v1, messages_total=sum(v1.values()))
    g["G8"] = dict(o["G10"], resends=1)
    return g


if __name__ == "__main__":
    main()
