// hroute: a "human-like" grid router for llm-pcb.
//
//  * 0.05 mm grid, 45-degree moves only (no acute turns), turn/via/wrong-direction costs
//    -> few bends, few vias, clean orthogonal + 45 degree geometry like a human layout
//  * clearance-aware: a centreline cell is legal if no foreign copper is within
//    halfwidth + max(own clearance, other net's clearance)
//  * multi-pin nets grown as a tree (nearest unconnected pin first)
//  * rip-up and reroute: a failing connection is retried allowing foreign copper at a
//    high cost, the nets it crosses are ripped up and re-queued
//  * differential pairs routed as ONE coupled centreline (width 2w+gap), then offset into
//    P/N tracks; pair vias are placed as two side-by-side vias; breakouts to pads checked
//
// Input/output: simple whitespace text (see hroute.py).
#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <deque>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <map>
#include <queue>
#include <set>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

using namespace std;

struct Net {
    int id; string name; double hw, clr, via_r, via_drill; int prio; bool route;
    int pair_with = -1; double pair_gap = 0; bool is_p = false;
    vector<int> pins;
};
struct Pin { int net; int layer_mask; double x1, y1, x2, y2, cx, cy; };
struct Seg { int net, layer; double w; vector<pair<double, double>> pts; };
struct ViaO { int net; double x, y, d, drill; };

static double P = 0.05;           // grid pitch (mm)
static double X0 = 0, Y0 = 0;
static int NX, NY, NL;
static vector<int> layer_dir;     // 0 any, 1 horizontal, 2 vertical
static vector<double> layer_cost; // multiplicative cost per layer
static vector<int> occ_base;      // pads/keepouts (-1 free, -2 blocked, >=0 net)
static vector<int> occ_route;     // routed copper (-1 free, >=0 net)
static vector<Net> nets;
static vector<Pin> pins;
static double VIA_COST = 30, TURN45 = 1.5, TURN90 = 5, WRONGDIR = 0.35, PROX = 0.15, PAIR_VIA_COST = 120;
static double EDGE_CLR = 0.3;
static map<int, vector<int>> painted;   // net -> cells painted in occ_route
static vector<Seg> out_segs;
static vector<ViaO> out_vias;
static map<int, vector<int>> seg_of_net, via_of_net;   // indices into out_*
static const int BS = 8;                 // coarse block size (cells)
static int BX, BY;
static vector<int> blk;                  // occupied-cell count per (layer, block)
static vector<int8_t> memo;              // per-search legality cache: 0 unknown, 1 legal, 2 illegal
static vector<int> memo_touched;
static long EXP_CAP = 4000000;
static double HW_A = 1.3;      // weighted A*: heuristic inflation (goal-directed, near-optimal)
static vector<uint8_t> neckz;            // 1 = inside the current net's neck zone (near its own fine pads)
static vector<int> neck_touched;
static double NECK_HW = 0.0635, NECK_ZONE = 0.9;
static vector<float> hist;              // negotiated congestion: history cost per cell
static double PFAC = 2.0;               // present-congestion factor (grows per iteration)
static map<int, vector<int>> path_cells, via_cells;   // per net: centreline cells and via cells (for conflict checks)

inline int idx(int l, int i, int j) { return (l * NY + j) * NX + i; }
inline double cx(int i) { return X0 + i * P; }
inline double cy(int j) { return Y0 + j * P; }
inline int gi(double x) { return (int)lround((x - X0) / P); }
inline int gj(double y) { return (int)lround((y - Y0) / P); }
inline int occ_at(int k) { int r = occ_route[k]; return r != -1 ? r : occ_base[k]; }
inline int bidx(int l, int i, int j) { return (l * BY + j / BS) * BX + i / BS; }
inline void blk_add(int l, int i, int j, int d) { blk[bidx(l, i, j)] += d; }

static double net_clr(int n) { return n >= 0 ? nets[n].clr : EDGE_CLR; }

// legality of a centreline cell for net n (or a pair: own2) with half-width hw
static bool legal(int n, int own2, double hw, double clr, int l, int i, int j) {
    if (i < 0 || j < 0 || i >= NX || j >= NY) return false;
    double rmax = hw + max(clr, 0.2) + P * 0.5;
    int R = (int)ceil(rmax / P);
    {   // coarse test: no copper at all in the blocks covering the disk -> legal
        int bi1 = max(0, i - R) / BS, bi2 = min(NX - 1, i + R) / BS, bj1 = max(0, j - R) / BS, bj2 = min(NY - 1, j + R) / BS;
        if (i - R >= 0 && j - R >= 0 && i + R < NX && j + R < NY) {
            bool any = false;
            for (int bj = bj1; bj <= bj2 && !any; bj++) for (int bi = bi1; bi <= bi2; bi++) if (blk[(l * BY + bj) * BX + bi]) { any = true; break; }
            if (!any) return true;
        }
    }
    for (int dj = -R; dj <= R; dj++) {
        int jj = j + dj; if (jj < 0 || jj >= NY) return false;
        for (int di = -R; di <= R; di++) {
            int ii = i + di; if (ii < 0 || ii >= NX) return false;
            int o = occ_at(idx(l, ii, jj));
            if (o == -1 || o == n || o == own2) continue;
            double need = hw + max(clr, net_clr(o)) + P * 0.5 + 0.01;
            double dist = sqrt((double)(di * di + dj * dj)) * P;
            if (dist < need) return false;
        }
    }
    return true;
}
// pair verification: the partner's TRACK is allowed (it is gap-spaced by construction), the partner's PADS are not
static bool legal_pairv(int n, int partner, double hw, double clr, int l, int i, int j) {
    if (i < 0 || j < 0 || i >= NX || j >= NY) return false;
    double rmax = hw + max(clr, 0.2) + P * 0.5; int R = (int)ceil(rmax / P);
    for (int dj = -R; dj <= R; dj++) { int jj = j + dj; if (jj < 0 || jj >= NY) return false;
        for (int di = -R; di <= R; di++) { int ii = i + di; if (ii < 0 || ii >= NX) return false;
            int k = idx(l, ii, jj); int orr = occ_route[k], ob = occ_base[k];
            int o = orr != -1 ? orr : ob;
            if (o == -1 || o == n) continue;
            if (orr == partner && ob != partner) continue;
            double need = hw + max(clr, net_clr(o)) + P * 0.5 + 0.01;
            if (sqrt((double)(di * di + dj * dj)) * P < need) return false; } }
    return true;
}

static bool legal_m(int n, int own2, double hw, double clr, int l, int i, int j) {
    if (i < 0 || j < 0 || i >= NX || j >= NY) return false;
    int k = idx(l, i, j);
    int8_t m = memo[k];
    if (m) return m == 1;
    bool ok = legal(n, own2, neckz[k] ? min(hw, NECK_HW) : hw, clr, l, i, j);
    memo[k] = ok ? 1 : 2; memo_touched.push_back(k);
    return ok;
}
static bool via_legal(int n, int own2, double vr, double clr, int i, int j) {
    for (int l = 0; l < NL; l++) if (!legal(n, own2, vr, clr, l, i, j)) return false;
    return true;
}
// how many foreign cells near (used for "keep a little distance" cost)
static double prox_cost(int n, int own2, double hw, double clr, int l, int i, int j) {
    double r = hw + clr + 0.15; int R = (int)ceil(r / P); int c = 0;
    if (i - R >= 0 && j - R >= 0 && i + R < NX && j + R < NY) {
        bool any = false;
        for (int bj = (j - R) / BS; bj <= (j + R) / BS && !any; bj++) for (int bi = (i - R) / BS; bi <= (i + R) / BS; bi++) if (blk[(l * BY + bj) * BX + bi]) { any = true; break; }
        if (!any) return 0.0;
    }
    for (int dj = -R; dj <= R; dj += 2) for (int di = -R; di <= R; di += 2) {
        int ii = i + di, jj = j + dj; if (ii < 0 || jj < 0 || ii >= NX || jj >= NY) continue;
        int o = occ_at(idx(l, ii, jj)); if (o >= 0 && o != n && o != own2) c++;
    }
    return c ? PROX * min(c, 6) : 0.0;
}

static void paint_disk(int n, int l, double x, double y, double r) {
    int i0 = gi(x), j0 = gj(y), R = (int)ceil(r / P);
    for (int dj = -R; dj <= R; dj++) for (int di = -R; di <= R; di++) {
        int ii = i0 + di, jj = j0 + dj; if (ii < 0 || jj < 0 || ii >= NX || jj >= NY) continue;
        if (hypot(di * P, dj * P) > r + 1e-9) continue;
        int k = idx(l, ii, jj);
        if (occ_route[k] == -1 && occ_base[k] != n) { occ_route[k] = n; painted[n].push_back(k); if (occ_base[k] == -1) blk_add(l, ii, jj, 1); }
    }
}
static void paint_seg(int n, int l, double x1, double y1, double x2, double y2, double r) {
    double L = hypot(x2 - x1, y2 - y1); int s = max(1, (int)ceil(L / (P * 0.5)));
    for (int t = 0; t <= s; t++) paint_disk(n, l, x1 + (x2 - x1) * t / s, y1 + (y2 - y1) * t / s, r);
}
static void ripup(int n) {
    for (int k : painted[n]) if (occ_route[k] == n) {
        occ_route[k] = -1;
        if (occ_base[k] == -1) { int l = k / (NX * NY), r = k % (NX * NY); blk_add(l, r % NX, r / NX, -1); }
    }
    painted[n].clear();
    for (int s : seg_of_net[n]) out_segs[s].net = -1;
    for (int v : via_of_net[n]) out_vias[v].net = -1;
    seg_of_net[n].clear(); via_of_net[n].clear();
    path_cells[n].clear(); via_cells[n].clear();
}

static void repaint_all() {
    for (size_t k = 0; k < occ_route.size(); k++) if (occ_route[k] != -1) {
        occ_route[k] = -1;
        if (occ_base[k] == -1) { int l = k / (NX * NY), r = k % (NX * NY); blk_add(l, r % NX, r / NX, -1); }
    }
    for (auto& kv : painted) kv.second.clear();
    for (auto& g : out_segs) if (g.net >= 0)
        for (size_t q = 0; q + 1 < g.pts.size(); q++) paint_seg(g.net, g.layer, g.pts[q].first, g.pts[q].second, g.pts[q + 1].first, g.pts[q + 1].second, g.w / 2);
    for (auto& v : out_vias) if (v.net >= 0) for (int l = 0; l < NL; l++) paint_disk(v.net, l, v.x, v.y, v.d / 2);
}

static const int DX[8] = {1, 1, 0, -1, -1, -1, 0, 1};
static const int DY[8] = {0, 1, 1, 1, 0, -1, -1, -1};

struct QE { double f; int s; bool operator<(const QE& o) const { return f > o.f; } };

static vector<float> G; static vector<int> PAR; static vector<int> touched;   // indexed by state = cell*9 + dir

// A* over (cell, arrival-direction) states (dir 8 = start / after via). Returns the path as cells.
// soft: allow foreign copper at high cost (for rip-up discovery), collecting crossed nets
static vector<int> astar(int n, int own2, double hw, double clr, double vr, const vector<int>& src,
                         const vector<char>& is_tgt_vec, const vector<int>& tgt, double tx, double ty,
                         int bx1, int by1, int bx2, int by2, bool soft, set<int>* crossed, bool pair_mode, double pair_off) {
    for (int k : touched) { G[k] = 1e30f; PAR[k] = -1; }
    touched.clear();
    for (int k : memo_touched) memo[k] = 0;
    memo_touched.clear();
    priority_queue<QE> pq;
    auto h = [&](int i, int j) { double dx = fabs(cx(i) - tx), dy = fabs(cy(j) - ty);
        return HW_A * (max(dx, dy) + (sqrt(2.0) - 1) * min(dx, dy)) / P; };
    unordered_map<int, char> tgtset; for (int t : tgt) tgtset[t] = 1;
    for (int c : src) { int s = c * 9 + 8; if (G[s] < 1e29f) continue; G[s] = 0; touched.push_back(s);
        int r = c % (NX * NY), j = r / NX, i = r % NX; pq.push({h(i, j), s}); }
    int found = -1; long it = 0;
    const int NXY = NX * NY;
    while (!pq.empty()) {
        QE q = pq.top(); pq.pop(); int s = q.s;
        int c = s / 9, d0 = s % 9;
        int l = c / NXY, r = c % NXY, j = r / NX, i = r % NX;
        if ((float)(q.f - h(i, j)) > G[s] + 1e-3f) continue;   // stale entry (G is float: compare in float)
        if (tgtset.count(c)) { found = s; break; }
        if (++it > EXP_CAP) break;
        for (int d = 0; d < 8; d++) {
            if (d0 < 8 && !getenv("HROUTE_ANYTURN")) { int diff = abs(d - d0); diff = min(diff, 8 - diff); if (diff > 2) continue; }
            int ii = i + DX[d], jj = j + DY[d];
            if (ii < bx1 || jj < by1 || ii > bx2 || jj > by2) continue;
            int nc = idx(l, ii, jj);
            double extra = 0;
            bool ok = tgtset.count(nc) ? true : legal_m(n, own2, hw, clr, l, ii, jj);
            if (!ok) { if (!soft) continue; extra = PFAC * (1.0 + hist[nc]) * 4.0; }
            double step = (d % 2 ? sqrt(2.0) : 1.0) * layer_cost[l];
            if (layer_dir[l] == 1 && d % 4 != 0) step *= (d % 2 ? 1 + WRONGDIR * 0.5 : 1 + WRONGDIR);
            if (layer_dir[l] == 2 && d % 4 != 2) step *= (d % 2 ? 1 + WRONGDIR * 0.5 : 1 + WRONGDIR);
            if (d0 < 8 && d != d0) { int diff = abs(d - d0); diff = min(diff, 8 - diff); step += diff == 1 ? TURN45 : TURN90; }
            if (!soft) step += prox_cost(n, own2, hw, clr, l, ii, jj);
            int ns = nc * 9 + d;
            double ng = G[s] + step + extra;
            if (ng < G[ns]) {
                if (G[ns] > 1e29f) touched.push_back(ns);
                G[ns] = ng; PAR[ns] = s; pq.push({ng + h(ii, jj), ns});
            }
        }
        // via: change layer at the same xy (not twice in a row)
        bool recent_via = false;
        if (PAR[s] != -1) {      // no second via within 12 cells of the previous one on this path
            int t = s, steps = 0;
            while (PAR[t] != -1 && steps < 12) { int pt = PAR[t]; if (pt / 9 / NXY != t / 9 / NXY) { recent_via = true; break; } t = pt; steps++; }
        }
        if (!recent_via && (d0 != 8 || PAR[s] == -1)) {
            bool vok;
            int keepdir = 8;
            if (pair_mode) {   // two vias side by side, perpendicular to an orthogonal run
                int dd = d0 < 8 ? d0 : 0; double px = -DY[dd], py = DX[dd]; double nn = hypot(px, py); px /= nn; py /= nn;
                int i1 = gi(cx(i) + px * pair_off), j1 = gj(cy(j) + py * pair_off), i2 = gi(cx(i) - px * pair_off), j2 = gj(cy(j) - py * pair_off);
                vok = d0 < 8 && d0 % 2 == 0 && via_legal(n, own2, vr, clr, i1, j1) && via_legal(n, own2, vr, clr, i2, j2);
                keepdir = d0 < 8 ? d0 : 8;
            } else vok = via_legal(n, own2, vr, clr, i, j);
            if (vok || soft) {
                for (int l2 = 0; l2 < NL; l2++) {
                    if (l2 == l || layer_cost[l2] > 50) continue;
                    int nc = idx(l2, i, j);
                    if (!tgtset.count(nc) && !legal_m(n, own2, hw, clr, l2, i, j)) { if (!soft) continue; }
                    int ns = nc * 9 + keepdir;
                    double ng = G[s] + (pair_mode ? PAIR_VIA_COST : VIA_COST) + (vok ? 0 : PFAC * (1.0 + hist[c]) * 20.0);
                    if (ng < G[ns]) {
                        if (G[ns] > 1e29f) touched.push_back(ns);
                        G[ns] = ng; PAR[ns] = s; pq.push({ng + h(i, j), ns});
                    }
                }
            }
        }
    }
    vector<int> path;
    if (found < 0) { if (getenv("HROUTE_DBG")) fprintf(stderr, "    astar fail: it=%ld touched=%zu pq_empty=%d src=%zu tgt=%zu soft=%d\n", it, touched.size(), (int)pq.empty(), src.size(), tgt.size(), (int)soft); return path; }
    for (int s = found; s != -1; s = PAR[s]) { int c = s / 9; if (path.empty() || path.back() != c) path.push_back(c); }
    reverse(path.begin(), path.end());
    if (soft && crossed) {
        for (int c : path) {
            int l = c / NXY, r = c % NXY, j = r / NX, i = r % NX;
            int R = (int)ceil((hw + 0.25) / P);
            for (int dj = -R; dj <= R; dj++) for (int di = -R; di <= R; di++) {
                int ii = i + di, jj = j + dj; if (ii < 0 || jj < 0 || ii >= NX || jj >= NY) continue;
                int o = occ_route[idx(l, ii, jj)]; if (o >= 0 && o != n && o != own2) crossed->insert(o);
            }
        }
    }
    return path;
}

// convert a state path into polylines per layer (+ via positions)
static void emit_path(int n, double hw, double vr, double drill, const vector<int>& path, vector<pair<double, double>>* centre = nullptr) {
    for (int c : path) path_cells[n].push_back(c);
    for (size_t k = 1; k < path.size(); k++) if (path[k] / (NX * NY) != path[k - 1] / (NX * NY)) via_cells[n].push_back(path[k] % (NX * NY));
    vector<pair<double, double>> cur; int curl = -1; double curw = hw;
    auto flush = [&]() {
        if (cur.size() >= 2) {
            vector<pair<double, double>> s; s.push_back(cur[0]);
            for (size_t k = 1; k + 1 < cur.size(); k++) {
                double ax = cur[k].first - s.back().first, ay = cur[k].second - s.back().second;
                double bx = cur[k + 1].first - cur[k].first, by = cur[k + 1].second - cur[k].second;
                if (fabs(ax * by - ay * bx) > 1e-9) s.push_back(cur[k]);
            }
            s.push_back(cur.back());
            Seg g{n, curl, curw * 2, s};
            seg_of_net[n].push_back(out_segs.size()); out_segs.push_back(g);
            for (size_t k = 0; k + 1 < s.size(); k++) paint_seg(n, curl, s[k].first, s[k].second, s[k + 1].first, s[k + 1].second, curw);
        }
        if (!cur.empty()) { auto last = cur.back(); cur.clear(); cur.push_back(last); } 
    };
    for (size_t k = 0; k < path.size(); k++) {
        int s = path[k]; int l = s / (NX * NY), r = s % (NX * NY), j = r / NX, i = r % NX;
        double w = (!neckz.empty() && neckz[s]) ? min(hw, NECK_HW) : hw;
        if (l != curl && curl != -1) {
            flush(); cur.clear();
            ViaO v{n, cx(i), cy(j), vr * 2, drill}; via_of_net[n].push_back(out_vias.size()); out_vias.push_back(v);
            for (int ll = 0; ll < NL; ll++) paint_disk(n, ll, cx(i), cy(j), vr);
        } else if (curl != -1 && fabs(w - curw) > 1e-9) {
            cur.push_back({cx(i), cy(j)});     // close the run at the width change point
            flush();
            curl = l; curw = w;
            if (centre) centre->push_back({cx(i), cy(j)});
            continue;
        }
        curl = l; curw = w; cur.push_back({cx(i), cy(j)});
        if (centre) centre->push_back({cx(i), cy(j)});
    }
    if (cur.size() >= 2) flush();
}

static vector<int> pin_cells(const Pin& p) {
    vector<int> c;
    for (int l = 0; l < NL; l++) {
        if (!(p.layer_mask >> l & 1) || layer_cost[l] > 50) continue;
        for (int j = gj(p.y1); j <= gj(p.y2); j++) for (int i = gi(p.x1); i <= gi(p.x2); i++) {
            if (i < 0 || j < 0 || i >= NX || j >= NY) continue;
            if (cx(i) < p.x1 - 1e-9 || cx(i) > p.x2 + 1e-9 || cy(j) < p.y1 - 1e-9 || cy(j) > p.y2 + 1e-9) continue;
            c.push_back(idx(l, i, j));
        }
        if (c.empty()) c.push_back(idx(l, gi(p.cx), gj(p.cy)));
    }
    return c;
}

static void set_neck(int n) {
    for (int k : neck_touched) neckz[k] = 0;
    neck_touched.clear();
    if (n < 0 || nets[n].hw <= NECK_HW + 1e-9) return;
    int R = (int)ceil(NECK_ZONE / P);
    for (int pi : nets[n].pins) {
        const Pin& p = pins[pi];
        if (min(p.x2 - p.x1, p.y2 - p.y1) > 0.45) continue;      // only fine pads get a neck zone
        for (int l = 0; l < NL; l++) {
            if (!(p.layer_mask >> l & 1)) continue;
            for (int j = gj(p.y1) - R; j <= gj(p.y2) + R; j++) for (int i = gi(p.x1) - R; i <= gi(p.x2) + R; i++) {
                if (i < 0 || j < 0 || i >= NX || j >= NY) continue;
                int k = idx(l, i, j); if (!neckz[k]) { neckz[k] = 1; neck_touched.push_back(k); }
            }
        }
    }
}

static bool NEGOTIATE = false;
static bool route_net(int n, bool soft_allowed, set<int>* blockers) {
    Net& N = nets[n];
    if (N.pins.size() < 2) return true;
    set_neck(n);
    vector<int> tree = pin_cells(pins[N.pins[0]]);
    vector<char> done(N.pins.size(), 0); done[0] = 1;
    double margin = 6.0;
    for (size_t step = 1; step < N.pins.size(); step++) {
        // nearest unconnected pin to the tree (by pin centre to pin0..)
        int best = -1; double bd = 1e30;
        for (size_t k = 0; k < N.pins.size(); k++) if (!done[k]) {
            const Pin& p = pins[N.pins[k]];
            for (size_t q = 0; q < N.pins.size(); q++) if (done[q]) {
                double dd = hypot(p.cx - pins[N.pins[q]].cx, p.cy - pins[N.pins[q]].cy);
                if (dd < bd) { bd = dd; best = k; }
            }
        }
        const Pin& tp = pins[N.pins[best]];
        vector<int> tgt = pin_cells(tp);
        double xs1 = tp.x1, ys1 = tp.y1, xs2 = tp.x2, ys2 = tp.y2;
        for (int s : tree) { int r = s % (NX * NY); xs1 = min(xs1, cx(r % NX)); xs2 = max(xs2, cx(r % NX)); ys1 = min(ys1, cy(r / NX)); ys2 = max(ys2, cy(r / NX)); }
        vector<char> dummy;
        vector<int> path;
        for (double mg : {margin, margin * 3, 1e3}) {
            int bx1 = max(0, gi(xs1 - mg)), by1 = max(0, gj(ys1 - mg)), bx2 = min(NX - 1, gi(xs2 + mg)), by2 = min(NY - 1, gj(ys2 + mg));
            path = astar(n, -3, N.hw, N.clr, N.via_r, tree, dummy, tgt, tp.cx, tp.cy, bx1, by1, bx2, by2, NEGOTIATE, nullptr, false, 0);
            if (!path.empty() || mg > 100) break;
        }
        if (path.empty()) {
            fprintf(stderr, "  %s: cannot reach pin %d at (%.2f,%.2f) mask %d (tree %zu cells)\n", N.name.c_str(), best, tp.cx, tp.cy, tp.layer_mask, tree.size());
            if (getenv("HROUTE_DBG")) {   // direction-free flood from the target: how big is its free region?
                vector<char> seen(NX * NY * NL, 0); deque<int> qq; for (int c : tgt) { seen[c] = 1; qq.push_back(c); }
                set<int> treeset(tree.begin(), tree.end()); long cnt = 0; bool hit = false; double bx = 1e9, by = 1e9, ex = -1e9, ey = -1e9;
                while (!qq.empty()) { int c = qq.front(); qq.pop_front(); cnt++; if (treeset.count(c)) { hit = true; break; }
                    int l = c / (NX * NY), r = c % (NX * NY), j = r / NX, i = r % NX;
                    bx = min(bx, cx(i)); ex = max(ex, cx(i)); by = min(by, cy(j)); ey = max(ey, cy(j));
                    for (int d = 0; d < 8; d++) { int ii = i + DX[d], jj = j + DY[d]; if (ii < 0 || jj < 0 || ii >= NX || jj >= NY) continue;
                        int nc = idx(l, ii, jj); if (seen[nc]) continue;
                        if (!treeset.count(nc) && !legal(n, -3, neckz[nc] ? min(N.hw, NECK_HW) : N.hw, N.clr, l, ii, jj)) continue;
                        seen[nc] = 1; qq.push_back(nc); } }
                fprintf(stderr, "    flood from target: %ld cells, reaches tree=%d, bbox (%.1f,%.1f)-(%.1f,%.1f)\n", cnt, (int)hit, bx, by, ex, ey);
            }
            if (soft_allowed && blockers) {
                astar(n, -3, N.hw, N.clr, N.via_r, tree, dummy, tgt, tp.cx, tp.cy, 0, 0, NX - 1, NY - 1, true, blockers, false, 0);
            }
            return false;
        }
        emit_path(n, N.hw, N.via_r, N.via_drill, path);
        for (int s : path) tree.push_back(s);
        for (int c : tgt) tree.push_back(c);
        done[best] = 1;
    }
    return true;
}

// ---------------- differential pairs
static vector<pair<double, double>> offset_poly(const vector<pair<double, double>>& c, double off) {
    vector<pair<double, double>> o;
    size_t n = c.size();
    for (size_t k = 0; k < n; k++) {
        double nx1 = 0, ny1 = 0, nx2 = 0, ny2 = 0;
        if (k > 0) { double dx = c[k].first - c[k - 1].first, dy = c[k].second - c[k - 1].second, L = hypot(dx, dy); nx1 = -dy / L; ny1 = dx / L; }
        if (k + 1 < n) { double dx = c[k + 1].first - c[k].first, dy = c[k + 1].second - c[k].second, L = hypot(dx, dy); nx2 = -dy / L; ny2 = dx / L; }
        if (k == 0) { nx1 = nx2; ny1 = ny2; }
        if (k + 1 == n) { nx2 = nx1; ny2 = ny1; }
        double mx = nx1 + nx2, my = ny1 + ny2, ml = hypot(mx, my);
        if (ml < 1e-9) { mx = nx1; my = ny1; ml = 1; }
        mx /= ml; my /= ml;
        double cosh = mx * nx1 + my * ny1; if (cosh < 0.3) cosh = 0.3;
        o.push_back({c[k].first + mx * off / cosh, c[k].second + my * off / cosh});
    }
    return o;
}
static vector<pair<double, double>> simplify(const vector<pair<double, double>>& cur) {
    if (cur.size() < 3) return cur;
    vector<pair<double, double>> s; s.push_back(cur[0]);
    for (size_t k = 1; k + 1 < cur.size(); k++) {
        double ax = cur[k].first - s.back().first, ay = cur[k].second - s.back().second;
        double bx = cur[k + 1].first - cur[k].first, by = cur[k + 1].second - cur[k].second;
        if (fabs(ax * by - ay * bx) > 1e-9) s.push_back(cur[k]);
    }
    s.push_back(cur.back());
    return s;
}
static bool seg_legal(int n, double hw, double clr, int l, double x1, double y1, double x2, double y2) {
    double L = hypot(x2 - x1, y2 - y1); int s = max(1, (int)ceil(L / (P * 0.5)));
    for (int t = 0; t <= s; t++) if (!legal(n, -3, hw, clr, l, gi(x1 + (x2 - x1) * t / s), gj(y1 + (y2 - y1) * t / s))) return false;
    return true;
}

// pick an escape point outward from the pad-pair midpoint where the coupled corridor is legal
static bool legal_pairv(int n, int partner, double hw, double clr, int l, int i, int j);
static bool pair_anchor(int a, int b, const Pin& pa, const Pin& pb, double hw2, double clr, int l, double ox, double oy,
                        int& ri, int& rj) {
    // escape point outward from the pad-pair midpoint where the coupled corridor is legal AND each offset
    // track position is clear of the partner's pads (P on P's side, N on N's side)
    double mx = (pa.cx + pb.cx) / 2, my = (pa.cy + pb.cy) / 2;
    double dx = mx - ox, dy = my - oy, L = hypot(dx, dy); if (L < 1e-6) { dx = 1; dy = 0; L = 1; }
    dx /= L; dy /= L;
    double px = -dy, py = dx;                       // perpendicular
    double sP = ((pa.cx - mx) * px + (pa.cy - my) * py) >= 0 ? 1.0 : -1.0;
    double w = nets[a].hw * 2, off = (w + nets[a].pair_gap) / 2.0;
    for (double t = 0.2; t <= 5.0; t += 0.1) {
        double ax = mx + dx * t, ay = my + dy * t;
        int i = gi(ax), j = gj(ay);
        if (!legal(-5, -3, hw2, clr, l, i, j)) continue;     // corridor clear of ALL pads (breakouts reach the pads)
        int pi_ = gi(ax + px * off * sP), pj = gj(ay + py * off * sP), ni = gi(ax - px * off * sP), nj = gj(ay - py * off * sP);
        if (!legal_pairv(a, b, nets[a].hw, clr, l, pi_, pj) || !legal_pairv(b, a, nets[b].hw, clr, l, ni, nj)) continue;
        ri = i; rj = j; return true;
    }
    return false;
}

static bool route_pair(int p, int nn, double ocx_p, double ocy_p, double ocx_e, double ocy_e) {
    Net& A = nets[p]; Net& B = nets[nn];
    if (A.pins.size() != 2 || B.pins.size() != 2) return false;
    double w = A.hw * 2, gap = A.pair_gap; double hw2 = w + gap / 2.0;   // half width of the coupled corridor
    double off = (w + gap) / 2.0 + 0.005;  // +5 um: absorbs grid quantisation at mitred corners
    // match pins: A0<->B0 on the same end (closest)
    const Pin* a0 = &pins[A.pins[0]]; const Pin* a1 = &pins[A.pins[1]];
    const Pin* b0 = &pins[B.pins[0]]; const Pin* b1 = &pins[B.pins[1]];
    if (hypot(a0->cx - b0->cx, a0->cy - b0->cy) + hypot(a1->cx - b1->cx, a1->cy - b1->cy) >
        hypot(a0->cx - b1->cx, a0->cy - b1->cy) + hypot(a1->cx - b0->cx, a1->cy - b0->cy)) swap(b0, b1);
    // start/end layer: a layer both pads of each end share (prefer top)
    int ls = -1, le = -1;
    for (int l = 0; l < NL && ls < 0; l++) if ((a0->layer_mask >> l & 1) && (b0->layer_mask >> l & 1) && layer_cost[l] < 50) ls = l;
    for (int l = 0; l < NL && le < 0; l++) if ((a1->layer_mask >> l & 1) && (b1->layer_mask >> l & 1) && layer_cost[l] < 50) le = l;
    if (ls < 0 || le < 0) return false;
    int si, sj, ei, ej;
    if (!pair_anchor(p, nn, *a0, *b0, hw2, A.clr, ls, ocx_p, ocy_p, si, sj)) { fprintf(stderr, "  pair: no start anchor\n"); return false; }
    if (!pair_anchor(p, nn, *a1, *b1, hw2, A.clr, le, ocx_e, ocy_e, ei, ej)) { fprintf(stderr, "  pair: no end anchor\n"); return false; }
    vector<int> src{idx(ls, si, sj)}, tgt{idx(le, ei, ej)}; vector<char> dummy;
    vector<int> path = astar(-5, -3, hw2, A.clr, A.via_r, src, dummy, tgt, cx(ei), cy(ej), 0, 0, NX - 1, NY - 1, false, nullptr, true, off);
    if (path.empty()) { fprintf(stderr, "  pair: no coupled path\n"); return false; }
    // centreline polylines per layer
    vector<vector<pair<double, double>>> runs; vector<int> run_layer; vector<pair<double, double>> cur; int curl = -1;
    vector<pair<double, double>> via_at;
    for (int s : path) {
        int l = s / (NX * NY), r = s % (NX * NY), j = r / NX, i = r % NX;
        if (curl != -1 && l != curl) { runs.push_back(cur); run_layer.push_back(curl); cur.clear(); via_at.push_back({cx(i), cy(j)}); }
        curl = l; cur.push_back({cx(i), cy(j)});
    }
    runs.push_back(cur); run_layer.push_back(curl);
    // breakouts (pad centre -> offset start/end) must be legal; decide which side P is on
    auto side_of = [&](const vector<pair<double, double>>& c, const Pin* pp) {
        double dx = c.size() > 1 ? c[1].first - c[0].first : 1, dy = c.size() > 1 ? c[1].second - c[0].second : 0;
        return ((pp->cx - c[0].first) * (-dy) + (pp->cy - c[0].second) * dx) > 0 ? 1.0 : -1.0;
    };
    double sgn = side_of(runs[0], a0);
    vector<vector<pair<double, double>>> pr, nr;
    for (auto& c : runs) { auto cs = simplify(c); pr.push_back(offset_poly(cs, sgn * off)); nr.push_back(offset_poly(cs, -sgn * off)); }
    // emit the coupled sections (both nets), then route the 4 breakouts with the normal maze engine
    auto emit = [&](int net, vector<vector<pair<double, double>>>& rr) {
        for (size_t k = 0; k < rr.size(); k++) {
            vector<pair<double, double>> pts = rr[k];
            if (pts.size() < 2) continue;
            Seg g{net, run_layer[k], w, pts};
            seg_of_net[net].push_back(out_segs.size()); out_segs.push_back(g);
            for (size_t q = 0; q + 1 < pts.size(); q++) paint_seg(net, run_layer[k], pts[q].first, pts[q].second, pts[q + 1].first, pts[q + 1].second, w / 2);
            if (k + 1 < rr.size()) {
                ViaO v{net, rr[k].back().first, rr[k].back().second, nets[net].via_r * 2, nets[net].via_drill};
                via_of_net[net].push_back(out_vias.size()); out_vias.push_back(v);
                for (int ll = 0; ll < NL; ll++) paint_disk(net, ll, v.x, v.y, nets[net].via_r);
            }
        }
    };
    auto endcell = [&](pair<double, double> q, int l) { return idx(l, gi(q.first), gj(q.second)); };
    emit(p, pr); emit(nn, nr);
    {   // verify each offset track against everything except itself (catches partner-pad clipping)
        auto ok_poly = [&](int net, vector<vector<pair<double, double>>>& rr) {
            int partner = net == p ? nn : p;
            for (size_t k = 0; k < rr.size(); k++) for (size_t q = 0; q + 1 < rr[k].size(); q++) {
                double x1 = rr[k][q].first, y1 = rr[k][q].second, x2 = rr[k][q + 1].first, y2 = rr[k][q + 1].second;
                double L = hypot(x2 - x1, y2 - y1); int st = max(1, (int)ceil(L / (P * 0.5)));
                for (int t = 0; t <= st; t++) if (!legal_pairv(net, partner, nets[net].hw, nets[net].clr, run_layer[k],
                        gi(x1 + (x2 - x1) * t / st), gj(y1 + (y2 - y1) * t / st))) {
                    int ci = gi(x1 + (x2 - x1) * t / st), cj = gj(y1 + (y2 - y1) * t / st);
                    // report the nearest offending owner
                    int R = 8, bo = -9; double bd = 1e9;
                    for (int dj = -R; dj <= R; dj++) for (int di = -R; di <= R; di++) { int ii = ci + di, jj = cj + dj;
                        if (ii < 0 || jj < 0 || ii >= NX || jj >= NY) continue; int kk = idx(run_layer[k], ii, jj);
                        int o = occ_route[kk] != -1 ? occ_route[kk] : occ_base[kk];
                        if (o == -1 || o == net || (occ_route[kk] == partner && occ_base[kk] != partner)) continue;
                        double dd = hypot(di * P, dj * P); if (dd < bd) { bd = dd; bo = o; } }
                    fprintf(stderr, "    clip: %s at (%.2f,%.2f) L%d seg %zu/%zu pt %d/%d by %s (route=%d) dist %.3f\n", nets[net].name.c_str(), cx(ci), cy(cj),
                            run_layer[k], q, rr[k].size(), t, st, bo >= 0 ? nets[bo].name.c_str() : (bo == -2 ? "BLOCK" : "?"), 0, bd);
                    return false; }
            }
            return true;
        };
        if (!ok_poly(p, pr) || !ok_poly(nn, nr)) { fprintf(stderr, "  pair: offset tracks clip partner/foreign copper\n"); ripup(p); ripup(nn); return false; }
    }
    struct BO { int net; const Pin* pad; pair<double, double> q; int l; };
    vector<BO> bos{{p, a0, pr.front().front(), ls}, {nn, b0, nr.front().front(), ls}, {p, a1, pr.back().back(), le}, {nn, b1, nr.back().back(), le}};
    for (auto& bo : bos) {
        set_neck(bo.net);
        vector<int> src = pin_cells(*bo.pad), tgt{endcell(bo.q, bo.l)}; vector<char> dummy;
        int ti = gi(bo.q.first), tj = gj(bo.q.second); int R = (int)ceil(4.0 / P);
        vector<int> path = astar(bo.net, -3, nets[bo.net].hw, nets[bo.net].clr, nets[bo.net].via_r, src, dummy, tgt,
                                 bo.q.first, bo.q.second, max(0, ti - R), max(0, tj - R), min(NX - 1, ti + R), min(NY - 1, tj + R),
                                 false, nullptr, false, 0);
        if (path.empty()) { fprintf(stderr, "  pair: breakout %s failed\n", nets[bo.net].name.c_str()); ripup(p); ripup(nn); set_neck(-1); return false; }
        emit_path(bo.net, nets[bo.net].hw, nets[bo.net].via_r, nets[bo.net].via_drill, path);
    }
    set_neck(-1);
    return true;
}

int main(int argc, char** argv) {
    if (argc < 3) { fprintf(stderr, "usage: hroute in.txt out.txt\n"); return 2; }
    if (getenv("HROUTE_CAP")) EXP_CAP = atol(getenv("HROUTE_CAP"));
    if (getenv("HROUTE_HW")) HW_A = atof(getenv("HROUTE_HW"));
    ifstream in(argv[1]); string tok;
    vector<array<double, 6>> rects;  // layer, x1,y1,x2,y2, net
    struct Circ { int l; double x, y, r; int net; }; vector<Circ> circs;
    vector<array<double, 4>> fixed_segs_meta; vector<Seg> fixed_segs; vector<ViaO> fixed_vias;
    struct PairD { int p, n; double ox1, oy1, ox2, oy2; }; vector<PairD> pairs_in;
    int max_iters = 4;
    while (in >> tok) {
        if (tok == "GRID") { in >> P >> X0 >> Y0 >> NX >> NY >> NL; layer_dir.assign(NL, 0); layer_cost.assign(NL, 1.0); }
        else if (tok == "LAYER") { int l, d; double c; in >> l >> d >> c; layer_dir[l] = d; layer_cost[l] = c; }
        else if (tok == "COST") { in >> VIA_COST >> TURN45 >> TURN90 >> WRONGDIR >> PROX >> PAIR_VIA_COST >> EDGE_CLR >> max_iters; }
        else if (tok == "NET") { Net n; int r; in >> n.id >> n.name >> n.hw >> n.clr >> n.via_r >> n.via_drill >> n.prio >> r; n.route = r; nets.push_back(n); }
        else if (tok == "PIN") { Pin p; in >> p.net >> p.layer_mask >> p.x1 >> p.y1 >> p.x2 >> p.y2; p.cx = (p.x1 + p.x2) / 2; p.cy = (p.y1 + p.y2) / 2;
            nets[p.net].pins.push_back(pins.size()); pins.push_back(p); }
        else if (tok == "RECT") { array<double, 6> r; for (auto& v : r) in >> v; rects.push_back(r); }
        else if (tok == "CIRC") { Circ c; in >> c.l >> c.x >> c.y >> c.r >> c.net; circs.push_back(c); }
        else if (tok == "FSEG") { Seg s; int k; double x, y; in >> s.net >> s.layer >> s.w >> k; for (int q = 0; q < k; q++) { in >> x >> y; s.pts.push_back({x, y}); } fixed_segs.push_back(s); }
        else if (tok == "FVIA") { ViaO v; in >> v.net >> v.x >> v.y >> v.d >> v.drill; fixed_vias.push_back(v); }
        else if (tok == "PAIR") { PairD pd; double gap; in >> pd.p >> pd.n >> gap >> pd.ox1 >> pd.oy1 >> pd.ox2 >> pd.oy2;
            nets[pd.p].pair_with = pd.n; nets[pd.n].pair_with = pd.p; nets[pd.p].pair_gap = nets[pd.n].pair_gap = gap; nets[pd.p].is_p = true; pairs_in.push_back(pd); }
    }
    size_t NC = (size_t)NX * NY * NL;
    occ_base.assign(NC, -1); occ_route.assign(NC, -1);
    G.assign(NC * 9, 1e30f); PAR.assign(NC * 9, -1);
    // board edge band
    int eb = (int)ceil(EDGE_CLR / P);
    for (int l = 0; l < NL; l++) for (int j = 0; j < NY; j++) for (int i = 0; i < NX; i++)
        if (i < eb || j < eb || i >= NX - eb || j >= NY - eb) occ_base[idx(l, i, j)] = -2;
    for (auto& r : rects) {
        int l = (int)r[0]; int net = (int)r[5];
        for (int j = max(0, gj(r[2])); j <= min(NY - 1, gj(r[4])); j++) for (int i = max(0, gi(r[1])); i <= min(NX - 1, gi(r[3])); i++) {
            if (l < 0) { for (int ll = 0; ll < NL; ll++) occ_base[idx(ll, i, j)] = net; } else occ_base[idx(l, i, j)] = net;
        }
    }
    for (auto& c : circs) {
        int R = (int)ceil(c.r / P);
        for (int dj = -R; dj <= R; dj++) for (int di = -R; di <= R; di++) {
            if (hypot(di * P, dj * P) > c.r) continue;
            int i = gi(c.x) + di, j = gj(c.y) + dj; if (i < 0 || j < 0 || i >= NX || j >= NY) continue;
            if (c.l < 0) { for (int ll = 0; ll < NL; ll++) occ_base[idx(ll, i, j)] = c.net; } else occ_base[idx(c.l, i, j)] = c.net;
        }
    }
    for (auto& s : fixed_segs) for (size_t k = 0; k + 1 < s.pts.size(); k++) {
        double L = hypot(s.pts[k + 1].first - s.pts[k].first, s.pts[k + 1].second - s.pts[k].second); int st = max(1, (int)ceil(L / (P * 0.5)));
        for (int t = 0; t <= st; t++) {
            double x = s.pts[k].first + (s.pts[k + 1].first - s.pts[k].first) * t / st, y = s.pts[k].second + (s.pts[k + 1].second - s.pts[k].second) * t / st;
            int R = (int)ceil(s.w / 2 / P);
            for (int dj = -R; dj <= R; dj++) for (int di = -R; di <= R; di++) { if (hypot(di * P, dj * P) > s.w / 2) continue;
                int i = gi(x) + di, j = gj(y) + dj; if (i >= 0 && j >= 0 && i < NX && j < NY) occ_base[idx(s.layer, i, j)] = s.net; }
        }
    }
    for (auto& v : fixed_vias) { int R = (int)ceil(v.d / 2 / P);
        for (int dj = -R; dj <= R; dj++) for (int di = -R; di <= R; di++) { if (hypot(di * P, dj * P) > v.d / 2) continue;
            int i = gi(v.x) + di, j = gj(v.y) + dj; if (i >= 0 && j >= 0 && i < NX && j < NY) for (int l = 0; l < NL; l++) occ_base[idx(l, i, j)] = v.net; } }

    BX = (NX + BS - 1) / BS; BY = (NY + BS - 1) / BS;
    blk.assign((size_t)BX * BY * NL, 0);
    memo.assign(NC, 0);
    neckz.assign(NC, 0);
    for (int l = 0; l < NL; l++) for (int j = 0; j < NY; j++) for (int i = 0; i < NX; i++)
        if (occ_base[idx(l, i, j)] != -1) blk_add(l, i, j, 1);
    if (getenv("HROUTE_PROBE")) {   // "net layer x y": ASCII legality map around a point
        int pn, pl; double px, py; sscanf(getenv("HROUTE_PROBE"), "%d %d %lf %lf", &pn, &pl, &px, &py);
        set_neck(pn);
        int st = getenv("HROUTE_STEP") ? atoi(getenv("HROUTE_STEP")) : 1, RW = 24 * st, RH = 16 * st;
        for (int j = gj(py) - RH; j <= gj(py) + RH; j += st) {
            for (int i = gi(px) - RW; i <= gi(px) + RW; i += st) {
                int k = idx(pl, i, j); int o = occ_at(k);
                char c = o == pn ? 'o' : o >= 0 ? '#' : o == -2 ? 'X' : (legal(pn, -3, neckz[k] ? min(nets[pn].hw, NECK_HW) : nets[pn].hw, nets[pn].clr, pl, i, j) ? '.' : '-');
                if (i == gi(px) && j == gj(py)) c = '@';
                fputc(c, stderr);
            }
            fputc('\n', stderr);
        }
        return 0;
    }
    // ---- pairs first
    set<int> pair_done;
    for (auto& pd : pairs_in) {
        set_neck(-1);
        bool ok = route_pair(pd.p, pd.n, pd.ox1, pd.oy1, pd.ox2, pd.oy2);
        if (ok) { pair_done.insert(pd.p); pair_done.insert(pd.n); }
        fprintf(stderr, "pair %s/%s: %s\n", nets[pd.p].name.c_str(), nets[pd.n].name.c_str(), ok ? "coupled" : "fallback to single-ended");
    }
    // ---- ordering: priority, then pin bbox size
    auto conflicts_of = [&](int n) -> vector<int> {   // centreline/via cells that violate clearance vs others
        vector<int> bad; set_neck(n);
        for (int c : path_cells[n]) { int l = c / (NX * NY), r = c % (NX * NY), j = r / NX, i = r % NX;
            if (!legal(n, -3, neckz[c] ? min(nets[n].hw, NECK_HW) : nets[n].hw, nets[n].clr, l, i, j)) bad.push_back(c); }
        for (int xy : via_cells[n]) { int j = xy / NX, i = xy % NX;
            if (!via_legal(n, -3, nets[n].via_r, nets[n].clr, i, j)) bad.push_back(xy); }
        return bad;
    };
    vector<int> order;
    for (auto& n : nets) if (n.route && n.pins.size() >= 2 && !pair_done.count(n.id)) order.push_back(n.id);
    auto bbox = [&](int n) { double x1 = 1e9, y1 = 1e9, x2 = -1e9, y2 = -1e9;
        for (int p : nets[n].pins) { x1 = min(x1, pins[p].cx); x2 = max(x2, pins[p].cx); y1 = min(y1, pins[p].cy); y2 = max(y2, pins[p].cy); }
        return (x2 - x1) + (y2 - y1); };
    stable_sort(order.begin(), order.end(), [&](int a, int b) { if (nets[a].prio != nets[b].prio) return nets[a].prio > nets[b].prio; return bbox(a) < bbox(b); });
    set<int> failed;
    if (getenv("HROUTE_SEQ")) {   // legacy: sequential + rip-up
        deque<int> q(order.begin(), order.end());
        map<int, int> rips;
        long guard = 0;
        while (!q.empty() && guard++ < 20000) {
            int n = q.front(); q.pop_front();
            set<int> blockers;
            bool okk = route_net(n, true, &blockers);
            fprintf(stderr, "net %s %s (queue %zu)\n", nets[n].name.c_str(), okk ? "ok" : "FAIL", q.size());
            if (okk) { failed.erase(n); continue; }
            ripup(n);
            vector<int> rip;
            for (int b : blockers) if (!pair_done.count(b) && rips[b] < max_iters) rip.push_back(b);
            if (!rip.empty() && rips[n] < max_iters) {
                for (int b : rip) { ripup(b); rips[b]++; }
                rips[n]++;
                if (route_net(n, false, nullptr)) { for (int b : rip) q.push_back(b); failed.erase(n); continue; }
                ripup(n);
                for (int b : rip) q.push_back(b);
            }
            failed.insert(n);
        }
    } else {
        // ---- negotiated congestion (PathFinder): everybody routes, overlaps get progressively expensive
        hist.assign(NC, 0.0f);
        // pass 0: try clean routes first (hard), so uncontested nets are fixed early
        vector<int> todo;
        map<int, bool> okr;     // last routing outcome per net (a failed negotiated route must not vanish)
        for (int n : order) { okr[n] = route_net(n, false, nullptr); if (!okr[n]) { ripup(n); todo.push_back(n); } }
        fprintf(stderr, "negotiation: %zu nets routed clean, %zu contested\n", order.size() - todo.size(), todo.size());
        NEGOTIATE = true;
        int NIT = max_iters * 6;
        double budget = getenv("HROUTE_BUDGET") ? atof(getenv("HROUTE_BUDGET")) : 1200.0;   // seconds
        auto t_start = chrono::steady_clock::now();
        for (int itn = 0; itn < NIT && !todo.empty(); itn++) {
            double el = chrono::duration<double>(chrono::steady_clock::now() - t_start).count();
            if (el > budget) { fprintf(stderr, "negotiation: time budget reached after %d iterations (%.0fs)\n", itn, el); break; }
            PFAC = 2.0 * pow(getenv("HROUTE_PGROW") ? atof(getenv("HROUTE_PGROW")) : 1.7, itn);
            for (int n : todo) { ripup(n); okr[n] = route_net(n, true, nullptr); }
            // find conflicts among ALL routed nets (contested ones may disturb clean ones)
            repaint_all();
            set<int> conf;
            for (int n : order) {
                auto bad = conflicts_of(n);
                if (!bad.empty()) { conf.insert(n); for (int c : bad) if (c < (int)hist.size()) hist[c] += 1.0f; }
            }
            for (int n : order) if (!okr[n]) conf.insert(n);      // incomplete nets stay in play
            fprintf(stderr, "negotiation iter %d: pfac %.1f, %zu nets in conflict\n", itn, PFAC, conf.size());
            todo.assign(conf.begin(), conf.end());
            // re-order: longer nets later
            sort(todo.begin(), todo.end(), [&](int a, int b) { return bbox(a) < bbox(b); });
        }
        NEGOTIATE = false;
        // still conflicting -> try a final clean reroute, else fail
        for (int n : order) if (!okr[n] && find(todo.begin(), todo.end(), n) == todo.end()) todo.push_back(n);
        for (int n : todo) { ripup(n); }
        for (int n : todo) { if (!route_net(n, false, nullptr)) { ripup(n); failed.insert(n); } }
        // final audit: every net that is not failed must have copper
        for (int n : order) if (!failed.count(n) && nets[n].pins.size() >= 2 && seg_of_net[n].empty() && via_of_net[n].empty()) {
            if (!route_net(n, false, nullptr)) { ripup(n); failed.insert(n); }
        }
    }
    // ---- output
    ofstream out(argv[2]);
    for (auto& s : out_segs) {
        if (s.net < 0 || s.pts.size() < 2) continue;
        out << "SEG " << s.net << " " << s.layer << " " << s.w << " " << s.pts.size();
        for (auto& p : s.pts) out << " " << p.first << " " << p.second;
        out << "\n";
    }
    for (auto& v : out_vias) if (v.net >= 0) out << "VIA " << v.net << " " << v.x << " " << v.y << " " << v.d << " " << v.drill << "\n";
    for (int n : failed) out << "FAIL " << n << "\n";
    for (int n : pair_done) out << "PAIROK " << n << "\n";
    fprintf(stderr, "routed %zu nets, failed %zu\n", order.size() - failed.size(), failed.size());
    return 0;
}
