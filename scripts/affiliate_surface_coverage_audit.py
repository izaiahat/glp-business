#!/usr/bin/env python3
"""Surface-coverage + research-standard auditor (machine invariant).

Operator 2026-08-06: *"make sure the currently live programs on the site get
the deep research pass... are all wired in to every relevant surface... and
that the affiliate machine updates all relevant parts of the site each time it
adds a new program."*

This is the enforcement tool for that invariant, run three ways:
  * standalone (`--live`): audit the deployed site right now
  * inside the daily backrun (surface section in the coverage receipt)
  * as the post-add assertion the activation lane must pass before a deploy
    that introduces a program is considered complete

For every money-selection program it verifies the registry's OWN surface
claims against reality (sitemap membership, live review route, /go/ route,
calculator dataset, compare/quiz presence) and measures the research-standard
fields (the review facts the SOP requires). Claims without reality = FAIL.
Read-only; receipt 0600.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys
import urllib.request
from pathlib import Path

BUSINESS = Path("/home/ubuntu/business")
MACHINE_DIR = BUSINESS / "reports/glp-clinic-scout/affiliate-machine"
RELEASES = BUSINESS / "site-system/flagship-v2/releases"
SITE = "https://glpclinicscout.com"

# The research standard every program must meet (directive 10, review facts).
RESEARCH_FIELDS = (
    "monthly_price", "first_month_price", "states_served", "insurance",
    "medication_forms", "consultation_fee", "shipping_wait", "cancellation",
    "pharmacy_type", "legitscript",
)


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def latest_release() -> Path:
    dirs = [p for p in RELEASES.iterdir() if (p / "data/program-registry.json").is_file()]
    return max(dirs, key=lambda p: p.stat().st_mtime)


def fetch(url: str, timeout: int = 20) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "glp-surface-audit/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception:
        return 0, ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def head(url: str, timeout: int = 20) -> int:
    """First-hop status only — following a 307 would measure the fallback."""
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": "glp-surface-audit/1.0"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return 0


def redirect_location(url: str, timeout: int = 20) -> tuple[int, str]:
    """First-hop status and Location — a declared alias must answer its review
    route with a permanent redirect onto its canonical record."""
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": "glp-surface-audit/1.0"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, str(resp.headers.get("Location") or "")
    except urllib.error.HTTPError as exc:
        return exc.code, str((exc.headers.get("Location") if exc.headers else "") or "")
    except Exception:
        return 0, ""


# research-standard field -> ledger key aliases (None value = schema gap today)
FIELD_ALIASES = {
    "monthly_price": ("starting_price", "monthly_price"),
    "first_month_price": ("first_month_price", "promo_price"),
    "states_served": ("states_served", "states"),
    "insurance": ("insurance",),
    "medication_forms": ("meds_offered", "medication_forms"),
    "consultation_fee": ("consultation_fee", "membership_fee"),
    "shipping_wait": ("average_wait", "shipping_wait"),
    "cancellation": ("cancellation", "cancellation_policy"),
    "pharmacy_type": ("pharmacy_type", "pharmacy"),
    "legitscript": ("legitscript", "legitscript_status"),
}
EMPTYISH = re.compile(r"^\s*(not stated|unclear|unknown|n/?a|)\s*$", re.I)


# G3 (2026-08-06): research artifacts are NOT in one directory. Each dated pass
# writes into its own mission dir, so this auditor resolves a REGISTRY of dirs:
#   1. whatever the ledger's own research_enrichment records it merged from,
#   2. the statically registered passes below,
#   3. anything passed with --research-dir.
# The prior single hardcoded path is why every program researched by a later
# mission scored a research gap it did not have.
RESEARCH_DIR_REGISTRY = (
    ("deep-research-20260806", "deep-research-20260806"),
    ("g6-r1a-wirein-20260806", "g6-r1a-wirein-20260806/research"),
    ("surface-unification-20260806", "surface-unification-20260806/research"),
)


# Extra monetized click paths that are not per-network renderer hosts.
EXTRA_TRACKING_PATTERNS = (r"everflowclient\.io", r"prf\.hn", r"refersion\.com/l/",
                           r"track\.[a-z0-9.-]+")


def tracking_host_pattern(rel: Path) -> str:
    """Build the monetized-click-path detector from the RENDERER's own allowlist.

    Hand-maintaining a second list here is how trt-nation's live CJ Affiliate
    anchor (www.dpbolvw.net) was read as "no monetized click path" on 2026-08-06
    while the link worked. So the detector is read from the release itself.

    Two shapes, newest first:

      * `data/tracking-allowed-hosts.json` — since 2026-08-20 the renderer's
        allowlist is DERIVED per program from `glp_affiliate_link_rails`
        (Blocker F), and this document is that derivation. Read it when present;
      * `src/provider-review.mjs :: DIRECT_TRACKING_HOSTS` — the frozen literal
        that preceded it. Kept as a fallback so this audit still reads an OLDER
        release tree correctly; it is not a second source of truth for new ones.

    Neither found is still a hard failure: a narrower detector would silently
    report live monetized anchors as unmonetized.
    """
    hosts: set[str] = set()
    derived = rel / "data/tracking-allowed-hosts.json"
    if derived.is_file():
        try:
            document = json.loads(derived.read_text())
        except (OSError, ValueError):
            document = None
        if isinstance(document, dict) and document.get("schema") == "glp.tracking_allowed_hosts.v1":
            for entry in (document.get("programs") or {}).values():
                hosts.update(str(host).casefold() for host in (entry.get("allowed_hosts") or []))
    if not hosts:
        source = rel / "src/provider-review.mjs"
        if source.exists():
            try:
                text = source.read_text()
                block = re.search(r"DIRECT_TRACKING_HOSTS\s*=\s*Object\.freeze\(\{(.*?)\}\);",
                                  text, re.S)
                if block:
                    hosts = set(re.findall(r'"([a-z0-9.-]+\.[a-z]{2,})"', block.group(1)))
            except OSError:
                pass
    if not hosts:  # never silently degrade to a narrower detector
        raise RuntimeError("tracking_host_allowlist_unreadable")
    patterns = [re.escape(host) for host in sorted(hosts)] + list(EXTRA_TRACKING_PATTERNS)
    return "(" + "|".join(patterns) + ")"


def reasoned_surface_exception(row: dict, surface: str) -> bool:
    """True only for an explicit, substantive off-surface declaration."""
    profile = ((row.get("profiles") or {}).get("glp-clinic-scout") or {})
    exception = ((profile.get("exceptions") or {}).get(surface) or {})
    reason = str(exception.get("reason") or "").strip()
    return (exception.get("enabled") is True
            and (profile.get("surfaces") or {}).get(surface) is not True
            and len(reason) > 40)


def quiz_candidate_has_live_link(candidate: dict) -> bool:
    """2026-08-27 successor quiz payload: candidate-data now carries the raw
    quiz-config rows. Link liveness moved from the boolean has_live_link to
    commercial_link_state ("live" = a tracked /go/ held route rendered on the
    result card). Read whichever grammar the production release speaks."""
    if "has_live_link" in candidate:
        return candidate.get("has_live_link") is True
    return (candidate.get("commercial_link_state") == "live"
            and str(candidate.get("held_route") or "").startswith("/go/"))


def dark_quiz_candidate_allowed(candidate: dict, row: dict | None) -> bool:
    """A missing quiz affiliate link is valid only for an exact held registry row."""
    if row is None or not reasoned_surface_exception(row, "go"):
        return False
    return (not quiz_candidate_has_live_link(candidate)
            and candidate.get("commercial_link_state") == "editorial_link_dark"
            and candidate.get("affiliate_url") in (None, ""))


def research_dirs(ledger: dict, extra: list[str] | None = None) -> list[Path]:
    found: list[Path] = []
    enrichment = ledger.get("research_enrichment") or {}
    declared = enrichment.get("research_dirs") or []
    source = enrichment.get("source")
    for raw in declared:
        found.append(Path(raw))
    for label in ([source] if isinstance(source, str) else list(source or [])):
        found.append(MACHINE_DIR / str(label))
    for _label, rel in RESEARCH_DIR_REGISTRY:
        found.append(MACHINE_DIR / rel)
    # B2 and future carriers own durable program-research directories. Discover
    # them by shape so a new lane cannot become invisible to the live audit.
    found.extend(path for path in MACHINE_DIR.glob("*/program-research") if path.is_dir())
    for raw in extra or []:
        found.append(Path(raw))
    seen, out = set(), []
    for path in found:
        resolved = path if path.is_absolute() else BUSINESS / path
        if resolved.is_dir() and str(resolved) not in seen:
            seen.add(str(resolved))
            out.append(resolved)
    return out


def research_completeness(ledger: dict, program_id: str, dirs: list[Path],
                          artifact_ids: tuple[str, ...] = ()) -> dict:
    entries = ledger.get("programs") or []
    if isinstance(entries, dict):
        entry = entries.get(program_id) or {}
    else:
        entry = next((e for e in entries if str(e.get("program_id")) == program_id), {})
    # The deep-research receipt: a field researched and honestly recorded as
    # not_publicly_stated MEETS the standard (we never invent data).
    researched_absent: set[str] = set()
    researched_in: str | None = None
    # A READY6 provider's ledger key is its render slug (sprout-health) while its
    # research artifact is filed under the network registry id (katalys-1286).
    # Looking only under the ledger key found no artifact, so four providers'
    # honestly-recorded "not publicly stated" findings were counted as research
    # gaps and held a complete deploy red (2026-08-06 surface unification).
    candidates = [program_id, *(a for a in artifact_ids if a and a != program_id)]
    for directory in dirs:
        rf = next((directory / f"{name}.json" for name in candidates
                   if (directory / f"{name}.json").exists()), None)
        if rf is None:
            continue
        try:
            rdoc = json.loads(rf.read_text())
        except (OSError, ValueError):
            continue
        researched_in = str(directory)
        for fname, fv in (rdoc.get("fields") or {}).items():
            if isinstance(fv, dict) and fv.get("state") == "not_publicly_stated":
                researched_absent.add(fname)
    present, missing = [], []
    for field, aliases in FIELD_ALIASES.items():
        keys = tuple(aliases) + (f"research_{field}",)
        # First NON-EMPTY value wins — an early alias holding "Not stated"
        # must not shadow a later filled research field (2026-08-06 bug).
        value = None
        for a in keys:
            candidate = entry.get(a)
            if isinstance(candidate, dict):
                candidate = candidate.get("value")
            if candidate is not None and not EMPTYISH.fullmatch(str(candidate)):
                value = candidate
                break
        if value is not None:
            present.append(field)
        elif field in researched_absent:
            present.append(field)  # researched; provider does not publish it
        else:
            missing.append(field)
    return {"present": present, "missing": missing, "artifact_dir": researched_in,
            "score": f"{len(present)}/{len(FIELD_ALIASES)}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="verify against the live site (HTTP)")
    ap.add_argument("--output")
    ap.add_argument("--release", help="release/candidate root to audit (default: newest release)")
    ap.add_argument("--research-dir", action="append", default=[],
                    help="extra research directory (repeatable)")
    ns = ap.parse_args()

    rel = Path(ns.release) if ns.release else latest_release()
    registry = json.loads((rel / "data/program-registry.json").read_text())
    rows = registry if isinstance(registry, list) else registry.get("programs") or []
    ledger = json.loads((rel / "data/program-content-ledger.json").read_text())

    sitemap_routes: set[str] = set()
    fallback_live = None
    if ns.live:
        # The go-system's internal fallback must itself be a live page; a 404
        # fallback strands every non-attributed hit (found live 2026-08-06).
        raw = (rel / "functions/go/program-manifest.js").read_text() if (rel / "functions/go/program-manifest.js").exists() else ""
        m = re.search(r'"internal_fallback":\s*"([^"]+)"', raw)
        if m:
            fallback_live = fetch(m.group(1))[0]
        _, xml = fetch(f"{SITE}/sitemap.xml")
        sitemap_routes = {u.replace(SITE, "").rstrip("/") + "/" for u in re.findall(r"<loc>([^<]+)</loc>", xml)}

    dirs = research_dirs(ledger, ns.research_dir)
    tracking_pattern = tracking_host_pattern(rel)
    # G2 (2026-08-06): the previous filter was `source_kind == "money"`. Programs
    # wired in on the fastpath and approved53_ready6 lanes carry a different
    # source_kind, so the audit never saw them and returned PASS over 13 of 21.
    # Liveness, not lane, decides who is audited.
    def is_live(row) -> bool:
        profile = ((row.get("profiles") or {}).get("glp-clinic-scout") or {})
        return (row.get("commercial_state") == "live" and profile.get("vertical_fit") is True
                and profile.get("wired") is True
                and (profile.get("surfaces") or {}).get("review") is True)

    ledger_entries = ledger.get("programs") or []
    ledger_ids = {str(e.get("program_id")) for e in
                  (ledger_entries if isinstance(ledger_entries, list) else [])}

    programs, failures = {}, []
    # Failures are partitioned, not filtered. Everything live is audited; the
    # PASS gate binds the program register (the content ledger), and anything
    # live but not yet a tracked program is reported as open work with its
    # recorded reason rather than quietly dropped from the denominator.
    outside_failures: list[str] = []
    audited = [r for r in rows if is_live(r)]
    money = [r for r in rows if str(r.get("source_kind")) == "money"]
    outside_register = []
    def note(in_register: bool, message: str) -> None:
        (failures if in_register else outside_failures).append(message)

    review_path_by_pid = {str(x.get("program_id")): str(x.get("review_path") or "") for x in rows}
    for r in audited:
        pid = str(r.get("program_id"))
        # The ledger and the research artifacts are keyed by the RENDER identity
        # (coivas), not the network registry id (awin-128761).
        research_id = str(r.get("review_slug") or pid)
        profile = ((r.get("profiles") or {}).get("glp-clinic-scout") or {})
        exceptions = profile.get("exceptions") or {}
        in_register = research_id in ledger_ids
        if not in_register:
            outside_register.append(pid)
        claimed = profile.get("surfaces") or {}
        checks: dict[str, str] = {"source_kind": str(r.get("source_kind")),
                                  "in_program_register": "TRUE" if in_register else "FALSE"}
        review_path = str(r.get("review_path") or "")
        go_route = str(((r.get("go") or {}).get("route")) or "")

        alias_decl = (profile.get("canonical_identity") or {})
        alias_canonical = str(alias_decl.get("canonical_program_id") or "")
        is_alias = (str(alias_decl.get("role") or "") == "alias" and bool(alias_canonical)
                    and len(str(alias_decl.get("reason") or "").strip()) > 40)
        if ns.live:
            status, review_html = (0, "")
            if review_path:
                status, review_html = fetch(SITE + review_path)
            checks["review_live"] = "PASS" if status == 200 else "FAIL"
            if is_alias:
                # A consolidated alias is one merchant. Its review route must
                # permanently redirect onto the canonical record and must NOT
                # hold a second sitemap slot next to it.
                canonical_path = review_path_by_pid.get(alias_canonical, "")
                hop_status, hop_location = redirect_location(SITE + review_path) if review_path else (0, "")
                checks["alias_review_redirects_to_canonical"] = (
                    "PASS" if hop_status in (301, 308) and canonical_path
                    and hop_location.rstrip("/").endswith(canonical_path.rstrip("/"))
                    else "FAIL")
                checks["review_in_sitemap"] = "PASS" if review_path not in sitemap_routes else "FAIL"
            else:
                checks["review_in_sitemap"] = "PASS" if review_path in sitemap_routes else "FAIL"
            # A bare /go/ hit redirects to the internal fallback BY DESIGN
            # (attribution-gated); liveness = the route answers with a redirect.
            # A declared, reasoned `go` exception is the registry saying this
            # program is monetized by a direct tracked link instead of an
            # attributed /go/ route. Honour it here exactly as the surface
            # claims below do, or coivas fails a route it never claimed.
            go_excused = reasoned_surface_exception(r, "go")
            if go_excused and claimed.get("go") is not True:
                checks["go_route_live"] = "N/A_DECLARED_EXCEPTION"
            else:
                checks["go_route_live"] = "PASS" if go_route and head(SITE + go_route) in (200, 301, 302, 307, 308) else "FAIL"
            # The page must carry a working monetized click path: either the
            # attributed /go/ route or a direct tracked-network link. The
            # 2026-08-06 audit found pages using direct links while the
            # registry claimed go:true — claims must match page reality.
            uses_go = bool(go_route) and (go_route in review_html)
            uses_direct = bool(re.search(tracking_pattern, review_html))
            checks["page_click_path"] = (
                "go" if uses_go else
                ("direct" if uses_direct else
                 ("N/A_DECLARED_DARK" if go_excused else "FAIL")))
            if status == 200 and checks["page_click_path"] == "FAIL":
                note(in_register, f"{pid}:no_monetized_click_path_on_review")
            # CTA density by rendered CTA elements, any style. The old pattern only
            # matched `class="cta "` / `class="cta"`, so the READY6 template's
            # `affiliate-cta` anchors counted as zero. The floor is unchanged.
            cta_count = len(re.findall(r'class="(?:[^"]*\s)?(?:cta|affiliate-cta)(?:\s[^"]*)?"', review_html))
            checks["cta_count"] = str(cta_count)
            if status == 200 and cta_count < 3:
                note(in_register, f"{pid}:cta_density_below_standard:{cta_count}")
            # SEO fundamentals every money page must carry.
            checks["jsonld"] = "PASS" if 'application/ld+json' in review_html else "FAIL"
            checks["indexable"] = "PASS" if re.search(r'name="robots"[^>]*content="[^"]*\bindex\b', review_html) and "noindex" not in review_html else ("NO_ROBOTS_TAG" if status == 200 and 'name="robots"' not in review_html else "FAIL")
            checks["canonical"] = "PASS" if '<link rel="canonical"' in review_html else "FAIL"
            for seo_key in ("jsonld", "canonical"):
                if status == 200 and checks[seo_key] == "FAIL":
                    note(in_register, f"{pid}:seo_{seo_key}_missing")
            if status == 200 and checks["indexable"] == "FAIL":
                note(in_register, f"{pid}:page_noindex_blocks_ranking")
        # A surface may legitimately be off when the registry carries a declared,
        # reasoned exception — the same allowance src/program-registry.mjs makes in
        # exceptionAllows(). Synergy RX is the standing case: its published prices
        # omit cadence and supply period, so the fastpath contract rules
        # FAIL_CLOSED_NO_CALCULATOR_ROW rather than render a misleading calculator
        # row. An UNDOCUMENTED off-claim still fails; only a reasoned one passes.
        for surface, claim in sorted(claimed.items()):
            exception = exceptions.get(surface) or {}
            excused = exception.get("enabled") is True and str(exception.get("reason") or "").strip()
            checks[f"claimed_{surface}"] = (
                "TRUE" if claim else ("FALSE_DECLARED_EXCEPTION" if excused else "FALSE_CLAIMED_OFF")
            )
            if not claim and not excused:
                note(in_register, f"{pid}:surface_claimed_off:{surface}")
        research = research_completeness(ledger, research_id, dirs, (pid,))
        checks["research"] = research["score"]
        checks["research_artifact_dir"] = research["artifact_dir"] or "NONE"
        if research["missing"]:
            note(in_register, f"{pid}:research_gaps:{','.join(research['missing'][:4])}")
        for name, verdict in checks.items():
            if verdict == "FAIL":
                note(in_register, f"{pid}:{name}")
        programs[pid] = {"display_name": r.get("display_name"),
                         "review_path": r.get("review_path"),
                         "source_kind": r.get("source_kind"),
                         "in_program_register": in_register,
                         "checks": checks,
                         "research_missing": research["missing"]}

    if fallback_live is not None and fallback_live != 200:
        failures.append(f"go_internal_fallback_not_live:{fallback_live}")

    # Quiz freshness: the live quiz's embedded candidate set must exactly track
    # the registry money selection (operator 2026-08-06: the quiz is our
    # biggest affiliate channel and must update with every deployment).
    quiz_check = {}
    if ns.live:
        _, quiz_html = fetch(f"{SITE}/quiz/provider-fit/")
        m = re.search(r'id="candidate-data">(.*?)</script>', quiz_html, re.S)
        if not m:
            failures.append("quiz_candidate_data_missing")
        else:
            try:
                cands = json.loads(m.group(1))
                # The quiz candidate set tracks the registry's quiz SURFACE, which is
                # not the same as "every audited program": most fastpath and READY6
                # programs carry a declared quiz exception.
                # Compare on RENDER identity. The registry keys a network program
                # as katalys-1286 and the quiz candidate as sprout-health; matching
                # on registry ids reported all six READY6 providers as both missing
                # and stale at once (2026-08-06 surface unification).
                money_ids = {str(r.get("review_slug") or r.get("program_id")) for r in rows
                             if (((r.get("profiles") or {}).get("glp-clinic-scout") or {})
                                 .get("surfaces") or {}).get("quiz") is True}
                quiz_ids = {str(c.get("program_id")) for c in cands}
                rows_by_render = {str(r.get("review_slug") or r.get("program_id")): r
                                  for r in rows}
                live_links = sum(1 for c in cands if quiz_candidate_has_live_link(c))
                allowed_dark = sorted(
                    str(c.get("program_id")) for c in cands
                    if dark_quiz_candidate_allowed(c, rows_by_render.get(str(c.get("program_id")))))
                unexpected_dark = sorted(
                    str(c.get("program_id")) for c in cands
                    if not quiz_candidate_has_live_link(c)
                    and not dark_quiz_candidate_allowed(
                        c, rows_by_render.get(str(c.get("program_id")))))
                priced = sum(1 for c in cands if c.get("price_line"))
                quiz_check = {"candidates": len(cands), "live_links": live_links,
                              "allowed_editorial_dark": allowed_dark,
                              "unexpected_without_live_link": unexpected_dark,
                              "priced": priced,
                              "missing_from_quiz": sorted(money_ids - quiz_ids),
                              "stale_in_quiz": sorted(quiz_ids - money_ids)}
                if money_ids != quiz_ids:
                    failures.append(f"quiz_candidates_desynced_from_registry:{len(money_ids - quiz_ids)}missing:{len(quiz_ids - money_ids)}stale")
                if unexpected_dark:
                    failures.append(
                        f"quiz_candidates_without_live_link:{len(unexpected_dark)}:"
                        + ",".join(unexpected_dark))
            except ValueError:
                failures.append("quiz_candidate_data_unparseable")
    receipt = {
        "schema": "affiliate-surface-coverage-audit-v1",
        "go_internal_fallback_http": fallback_live,
        "quiz_freshness": quiz_check,
        "captured_at_utc": utc(),
        "release": rel.name,
        "money_programs": len(money),
        "audited_programs": len(audited),
        "program_register_programs": len(audited) - len(outside_register),
        "research_dirs": [str(d) for d in dirs],
        "tracking_host_pattern": tracking_pattern,
        "live_programs_outside_content_ledger": outside_register,
        "programs": programs,
        "failures": failures,
        "failure_count": len(failures),
        "scope_note": ("failure_count and status cover the program register (data/"
                       "program-content-ledger.json). Live review routes not yet in the "
                       "register are audited too; their findings are reported separately as "
                       "open work, never removed from the denominator."),
        "open_findings_outside_register": outside_failures,
        "open_findings_outside_register_count": len(outside_failures),
        "status": "PASS" if not failures else "GAPS_FOUND",
        "effects": {"provider_mutations": 0, "deploys": 0, "sheet_writes": 0},
        "secret_printed": False,
    }
    out = Path(ns.output) if ns.output else MACHINE_DIR / f"surface-coverage-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    out.write_text(body)
    os.chmod(out, stat.S_IRUSR | stat.S_IWUSR)
    print(json.dumps({"status": receipt["status"], "money_programs": len(money),
                      "audited_programs": len(audited),
                      "research_dirs": len(dirs),
                      "failure_count": len(failures),
                      "failures_sample": failures[:12],
                      "open_findings_outside_register_count": len(outside_failures),
                      "open_findings_outside_register_sample": outside_failures[:8],
                      "receipt": str(out),
                      "receipt_sha256": hashlib.sha256(body.encode()).hexdigest()}, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
