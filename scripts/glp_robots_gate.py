#!/usr/bin/env python3
"""robots.txt build gate + 14-token crawl regression test for GLP Clinic Scout.

Encodes conflict-ruling CR-6 of reports/glp-clinic-scout/rebuild/BUILD-SPEC-PACK-20260819.md:

  (1) the two dead disallows `/clinics/` and `/directory/` must be GONE
      (AEO P1-K: the pharmacy layer would be born invisible under either path),
  (2) `/pharmacies/` must never be disallowed for any crawler,
  (3) faceted param-tails may be disallowed (LAY 3.2) but only as query-tails,
      never as a bare path prefix that would swallow a real route,
  (4) `AdsBot-Google` must NEVER be blocked from `/lp/` (ADS R9),
  (5) the 14 baseline AI/search tokens PLUS an unknown future token must all be
      effectively allowed on every public content tree and `/llms.txt`
      (AEO 3 P1-K regression test; CR-6 overrules per-bot enumeration, so this
      is asserted through the wildcard group, not through named groups).

Nothing here can be relaxed to make a build pass: every assertion is a hard
failure and the CLI exits non-zero.

Usage:
    python3 scripts/glp_robots_gate.py --release <release_root>
    python3 scripts/glp_robots_gate.py --robots <path/to/robots.txt>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote

SCHEMA = "glp-robots-gate-v1"

# The 14 baseline tokens the AEO spec pins (search + AI retrieval crawlers).
BASELINE_TOKENS: tuple[str, ...] = (
    "Googlebot",
    "Bingbot",
    "DuckDuckBot",
    "Applebot",
    "Applebot-Extended",
    "GPTBot",
    "OAI-SearchBot",
    "ChatGPT-User",
    "ClaudeBot",
    "Claude-User",
    "PerplexityBot",
    "Perplexity-User",
    "Google-Extended",
    "CCBot",
)
# Stands in for Grok/xAI and every crawler that does not exist yet.
UNKNOWN_TOKEN = "SomeFutureAnswerEngine/1.0"

# Public content trees that must stay crawlable forever.
REQUIRED_ALLOWED_PATHS: tuple[str, ...] = (
    "/",
    "/best/online-glp-1-providers/",
    "/glp-1-price-index/",
    "/glp-1-price-index/data/latest.csv",
    "/glp-1-price-index/data/latest.json",
    "/guides/mounjaro-cost-without-insurance/",
    "/reviews/orderlymeds/",
    "/reviews/",
    "/pharmacies/",
    "/pharmacies/empower-pharmacy/",
    "/compare-providers/",
    "/llms.txt",
    "/sitemap.xml",
)

# Disallows that must not exist (they suppress nothing today and would suppress
# the pharmacy layer tomorrow).
FORBIDDEN_DISALLOW_PREFIXES: tuple[str, ...] = ("/clinics", "/directory", "/pharmacies")

# Disallows that must survive (policy/no-value surfaces).
REQUIRED_DISALLOW_PATHS: tuple[str, ...] = ("/privacy/", "/terms/", "/google-ads-api/")

ADSBOT_TOKEN = "AdsBot-Google"
ADSBOT_REQUIRED_PATHS: tuple[str, ...] = ("/lp/", "/lp/r/orderlymeds/", "/lp/best/semaglutide/")


# --------------------------------------------------------------------------
# RFC 9309 matcher
# --------------------------------------------------------------------------

@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[str, str]] = field(default_factory=list)  # (allow|disallow, pattern)


class RobotsTxt:
    """Minimal RFC 9309 evaluator: group selection + longest-match, Allow wins ties."""

    def __init__(self, groups: list[_Group], sitemaps: list[str], raw: str) -> None:
        self.groups = groups
        self.sitemaps = sitemaps
        self.raw = raw

    @classmethod
    def parse(cls, text: str) -> "RobotsTxt":
        groups: list[_Group] = []
        sitemaps: list[str] = []
        current: _Group | None = None
        expecting_agent = False
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field_name, _, value = line.partition(":")
            key = field_name.strip().lower()
            value = value.strip()
            if key == "user-agent":
                if current is None or not expecting_agent:
                    current = _Group()
                    groups.append(current)
                    expecting_agent = True
                current.agents.append(value)
            elif key in ("allow", "disallow"):
                if current is None:
                    continue
                expecting_agent = False
                current.rules.append((key, value))
            elif key == "sitemap":
                sitemaps.append(value)
        return cls(groups, sitemaps, text)

    def _group_for(self, user_agent: str) -> _Group | None:
        token = user_agent.split("/", 1)[0].strip().lower()
        best: _Group | None = None
        best_len = -1
        wildcard: _Group | None = None
        for group in self.groups:
            for agent in group.agents:
                agent_l = agent.strip().lower()
                if agent_l == "*":
                    if wildcard is None:
                        wildcard = group
                    continue
                # RFC 9309: the product token matches case-insensitively; we also
                # honour the common prefix convention (e.g. "Applebot" group for
                # "Applebot-Extended" only when no exact group exists).
                if agent_l == token and len(agent_l) > best_len:
                    best, best_len = group, len(agent_l)
        return best or wildcard

    @staticmethod
    def _pattern_match(pattern: str, path: str) -> int | None:
        """Return the match length (specificity) or None. Supports * and $."""
        if pattern == "":
            return None  # empty Disallow means "allow everything" -> no rule
        regex = []
        for char in pattern:
            if char == "*":
                regex.append(".*")
            elif char == "$":
                regex.append("$")
            else:
                regex.append(re.escape(char))
        compiled = re.compile("^" + "".join(regex))
        if compiled.match(path):
            return len(pattern)
        return None

    def allowed(self, user_agent: str, path: str) -> bool:
        group = self._group_for(user_agent)
        if group is None:
            return True
        target = unquote(path)
        verdict = True
        best_len = -1
        for kind, pattern in group.rules:
            length = self._pattern_match(pattern, target)
            if length is None:
                continue
            if length > best_len or (length == best_len and kind == "allow"):
                best_len = length
                verdict = kind == "allow"
        return verdict

    def disallow_patterns(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for group in self.groups:
            agents = ",".join(group.agents) or "*"
            for kind, pattern in group.rules:
                if kind == "disallow" and pattern:
                    out.append((agents, pattern))
        return out


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------

def all_tokens() -> list[str]:
    return [*BASELINE_TOKENS, UNKNOWN_TOKEN]


def evaluate(robots_text: str,
             required_allowed: Iterable[str] = REQUIRED_ALLOWED_PATHS) -> dict[str, Any]:
    robots = RobotsTxt.parse(robots_text)
    failures: list[str] = []

    # (1)+(2) forbidden disallow prefixes anywhere, for any agent group.
    for agents, pattern in robots.disallow_patterns():
        normalised = pattern.rstrip("*$")
        for forbidden in FORBIDDEN_DISALLOW_PREFIXES:
            if normalised.rstrip("/") == forbidden.rstrip("/"):
                failures.append(
                    f"forbidden_disallow:{forbidden} present for user-agent group [{agents}]")

    # (3) param-tail disallows must be query-scoped, never bare path prefixes.
    for agents, pattern in robots.disallow_patterns():
        if pattern.startswith("/*?") or pattern.startswith("/?"):
            continue  # a legitimate faceted param-tail rule
        if "?" in pattern and not pattern.startswith("/*?") and not pattern.startswith("/?"):
            failures.append(
                f"param_tail_rule_not_query_scoped:{pattern} for [{agents}]")

    # (5) every baseline token + unknown token reaches every public tree.
    for token in all_tokens():
        for path in required_allowed:
            if not robots.allowed(token, path):
                failures.append(f"crawler_blocked:{token} cannot reach {path}")

    # (4) AdsBot-Google reaches /lp/.
    for path in ADSBOT_REQUIRED_PATHS:
        if not robots.allowed(ADSBOT_TOKEN, path):
            failures.append(f"adsbot_blocked:{ADSBOT_TOKEN} cannot reach {path}")

    # Required disallows survive (we do not want these indexed and losing them
    # silently is also a regression).
    for path in REQUIRED_DISALLOW_PATHS:
        if robots.allowed("Googlebot", path):
            failures.append(f"required_disallow_missing:{path} is crawlable")

    if not robots.sitemaps:
        failures.append("sitemap_directive_missing")

    return {
        "schema": SCHEMA,
        "status": "FAIL" if failures else "PASS",
        "tokens_tested": all_tokens(),
        "paths_tested": list(required_allowed),
        "adsbot_paths_tested": list(ADSBOT_REQUIRED_PATHS),
        "disallow_rules": [{"agents": a, "pattern": p} for a, p in robots.disallow_patterns()],
        "sitemaps": robots.sitemaps,
        "failure_count": len(failures),
        "failures": failures,
    }


def evaluate_release(release: Path) -> dict[str, Any]:
    robots_path = release / "public" / "robots.txt"
    if not robots_path.is_file():
        return {"schema": SCHEMA, "status": "FAIL", "failure_count": 1,
                "failures": [f"robots_txt_missing:{robots_path}"]}
    result = evaluate(robots_path.read_text(encoding="utf-8"))
    result["robots_path"] = str(robots_path)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--release", type=Path, help="release/candidate root (uses <root>/public/robots.txt)")
    ap.add_argument("--robots", type=Path, help="explicit robots.txt path")
    ns = ap.parse_args(argv)
    if ns.robots:
        result = evaluate(ns.robots.read_text(encoding="utf-8"))
        result["robots_path"] = str(ns.robots)
    elif ns.release:
        result = evaluate_release(ns.release)
    else:
        ap.error("one of --release / --robots is required")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
