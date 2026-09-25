#!/usr/bin/env python3
"""Disk guard for hermes-01 (persistence doctrine: never a one-time fix).
report  : print usage + the top consumers (always safe)
prune   : remove ONLY known-safe classes when free space is below --min-free-gb (default 40):
          npm/pip caches; copy-on-write build candidates older than 1 day (site-system/flagship-v2/releases/.candidate.cow-*);
          node_modules inside superseded candidate/work trees not modified for 3 days (never inside qa/sealed-*, releases/sealed-*, the active qa/candidate-* bases, or the review server tree);
          then hard-link dedup of the evidence trees (reports/, candidates/) for identical files >64 KB not modified in 6 h.
Never touches: /tmp/tmux-*, chromium/.org.chromium.* dirs, /tmp/claude-1000, /tmp/hermes-results, ~/.hermes/state.db*, browser profiles, secrets, any sealed-* tree.
Writes a receipt to reports/ops/disk-guard/<UTC>.json and prints a one-line summary for the cron's Discord delivery.
"""
import os, sys, json, shutil, subprocess, time, datetime, argparse
BUS = "/home/ubuntu/business"; FLAG = f"{BUS}/site-system/flagship-v2"
RECEIPTS = f"{BUS}/reports/ops/disk-guard"; os.makedirs(RECEIPTS, exist_ok=True)
def free_gb(): st = os.statvfs("/"); return st.f_bavail * st.f_frsize / 1e9
def du(p):
    try: return int(subprocess.run(["du", "-sm", p], capture_output=True, text=True, timeout=600).stdout.split()[0])
    except Exception: return -1
def report():
    tops = []
    for p in [f"{BUS}/reports", f"{FLAG}/qa", f"{FLAG}/candidates", f"{FLAG}/releases", f"{FLAG}/work", os.path.expanduser("~/.hermes"), "/tmp", os.path.expanduser("~/.cache"), os.path.expanduser("~/.npm")]:
        tops.append((du(p), p))
    return {"free_gb": round(free_gb(), 1), "top_mb": sorted(tops, reverse=True)}
def prune(min_free, dry):
    acts = []; now = time.time()
    def rm(p, why):
        acts.append({"path": p, "why": why, "mb": du(p)})
        if not dry: shutil.rmtree(p, ignore_errors=True)
    if free_gb() >= min_free: return acts
    for c in [os.path.expanduser("~/.npm/_cacache"), os.path.expanduser("~/.cache/pip")]:
        if os.path.isdir(c): rm(c, "package cache")
    rel = f"{FLAG}/releases"
    for n in os.listdir(rel) if os.path.isdir(rel) else []:
        p = os.path.join(rel, n)
        if n.startswith(".candidate.cow-") and now - os.stat(p).st_mtime > 86400: rm(p, "stale COW build candidate")
    for base in [f"{FLAG}/candidates", f"{FLAG}/work"]:
        for n in os.listdir(base) if os.path.isdir(base) else []:
            if n == "glp-visual-ultracode-20260821": continue  # review server tree
            nm = os.path.join(base, n, "node_modules")
            if os.path.isdir(nm) and now - os.stat(nm).st_mtime > 3 * 86400: rm(nm, "node_modules in superseded tree (npm ci restores)")
    if free_gb() < min_free:
        dd = f"{BUS}/scripts/hardlink_dedup.py"
        if os.path.exists(dd):
            r = subprocess.run([sys.executable, dd, f"{BUS}/reports/glp-clinic-scout", f"{BUS}/reports/ops", f"{BUS}/reports/boring-ai", f"{FLAG}/candidates"], capture_output=True, text=True, timeout=7200)
            acts.append({"dedup": r.stdout[-400:]})
    return acts
ap = argparse.ArgumentParser(); ap.add_argument("mode", choices=["report", "prune"]); ap.add_argument("--min-free-gb", type=float, default=40); ap.add_argument("--dry-run", action="store_true")
a = ap.parse_args()
before = free_gb(); acts = prune(a.min_free_gb, a.dry_run) if a.mode == "prune" else []
rep = report(); rep.update({"mode": a.mode, "dry_run": a.dry_run, "free_before_gb": round(before, 1), "actions": acts, "at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()})
fn = f"{RECEIPTS}/{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{a.mode}.json"
json.dump(rep, open(fn, "w"), indent=1)
level = "RED" if rep["free_gb"] < 15 else "AMBER" if rep["free_gb"] < 40 else "GREEN"
print(f"[disk-guard {level}] free {rep['free_gb']} GB (before {rep['free_before_gb']}); actions {len(acts)}; receipt {fn}")
