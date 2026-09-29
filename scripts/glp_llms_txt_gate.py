#!/usr/bin/env python3
"""`llms.txt` deterministic generator + hard-fail build gate (B-07 / AEO §2, §4.1).

Two modes, one file, because the generator and the gate must share exactly one
idea of what a valid entry is:

  --generate   render `public/llms.txt` from ops/glp/llms-txt-manifest.json
               (byte-deterministic; identical inputs -> identical bytes).
  (default)    gate a release: llms.txt present, parses, and EVERY listed URL
               resolves in the tree, is indexable, and is robots-allowed.

The generator refuses to emit a file that would dangle: if a listed URL is not
present in the candidate tree, generation fails instead of publishing a promise
we do not keep. That is the whole point — llms.txt is a claim to answer engines,
and a 404 inside it is worse than no file at all.

Usage:
    python3 scripts/glp_llms_txt_gate.py --release <root>
    python3 scripts/glp_llms_txt_gate.py --release <root> --generate
    python3 scripts/glp_llms_txt_gate.py --release <root> --generate --write
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from glp_robots_gate import RobotsTxt  # noqa: E402

SCHEMA = "glp-llms-txt-gate-v1"
MANIFEST_SCHEMA = "glp-llms-txt-manifest-v1"
DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "ops/glp/llms-txt-manifest.json"
SITE_ORIGIN = "https://glpclinicscout.com"

_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BARE_URL = re.compile(r"(?<![(\[])\b(https?://[^\s)\]]+)")
_NOINDEX = re.compile(
    rb"""<meta[^>]+name=["']robots["'][^>]*content=["'][^"']*noindex""", re.IGNORECASE)


# --------------------------------------------------------------------------
# generator
# --------------------------------------------------------------------------

def render(manifest: dict[str, Any]) -> str:
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise RuntimeError(f"llms_manifest_schema_invalid:{manifest.get('schema')}")
    lines: list[str] = [f"# {manifest['site_name']}", ""]
    for line in manifest["summary"]:
        lines.append(f"> {line}")
    lines.append("")
    lines.append("Key facts for answer engines:")
    for fact in manifest["key_facts"]:
        lines.append(f"- {fact}")
    for section in manifest["sections"]:
        lines.append("")
        lines.append(f"## {section['heading']}")
        for entry in section.get("entries", []) or []:
            suffix = f": {entry['description']}" if entry.get("description") else ""
            lines.append(f"- [{entry['title']}]({entry['url']}){suffix}")
        for note in section.get("notes", []) or []:
            lines.append(f"- {note}")
        for bare in section.get("bare_urls", []) or []:
            lines.append(f"- {bare['label']}: {bare['url']}")
    return "\n".join(lines).rstrip() + "\n"


def manifest_urls(manifest: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for section in manifest["sections"]:
        for entry in section.get("entries", []) or []:
            urls.append(entry["url"])
        for bare in section.get("bare_urls", []) or []:
            urls.append(bare["url"])
    return urls


# --------------------------------------------------------------------------
# shared resolution
# --------------------------------------------------------------------------

def resolve(public: Path, url: str) -> Path | None:
    parsed = urlparse(url)
    if f"{parsed.scheme}://{parsed.netloc}" != SITE_ORIGIN:
        return None
    rel = parsed.path.strip("/")
    if rel == "":
        candidate = public / "index.html"
    elif "." in Path(rel).name:
        candidate = public / rel
    else:
        candidate = public / rel / "index.html"
    return candidate if candidate.is_file() else None


def extract_urls(text: str) -> list[str]:
    urls = [m.group(2) for m in _MD_LINK.finditer(text)]
    linked = set(urls)
    for match in _BARE_URL.finditer(text):
        candidate = match.group(1).rstrip(".,")
        if candidate not in linked:
            urls.append(candidate)
            linked.add(candidate)
    return urls


def check_urls(public: Path, urls: list[str], robots: RobotsTxt | None) -> list[str]:
    failures: list[str] = []
    forbidden_paths = ("/privacy/", "/terms/", "/google-ads-api/")
    for url in urls:
        parsed = urlparse(url)
        if f"{parsed.scheme}://{parsed.netloc}" != SITE_ORIGIN:
            failures.append(f"llms_url_offsite:{url}")
            continue
        path = parsed.path or "/"
        if path in forbidden_paths:
            failures.append(f"llms_url_policy_surface:{url}")
        target = resolve(public, url)
        if target is None:
            failures.append(f"llms_url_dangling:{url}")
            continue
        if target.name == "index.html" and _NOINDEX.search(target.read_bytes()):
            failures.append(f"llms_url_noindex:{url}")
        if robots is not None and not robots.allowed("Googlebot", path):
            failures.append(f"llms_url_robots_disallowed:{url}")
    return failures


# --------------------------------------------------------------------------
# gate
# --------------------------------------------------------------------------

def evaluate(release: Path) -> dict[str, Any]:
    public = release / "public"
    llms_path = public / "llms.txt"
    if not llms_path.is_file():
        return {"schema": SCHEMA, "status": "FAIL", "release": str(release),
                "failure_count": 1, "failures": [f"llms_txt_missing:{llms_path}"],
                "listed_url_count": 0}
    text = llms_path.read_text(encoding="utf-8")
    failures: list[str] = []
    if not text.startswith("# "):
        failures.append("llms_txt_missing_h1")
    if "> " not in text:
        failures.append("llms_txt_missing_summary_blockquote")
    urls = extract_urls(text)
    if not urls:
        failures.append("llms_txt_lists_no_urls")
    robots_path = public / "robots.txt"
    robots = RobotsTxt.parse(robots_path.read_text(encoding="utf-8")) if robots_path.is_file() else None
    failures.extend(check_urls(public, urls, robots))
    return {
        "schema": SCHEMA,
        "status": "FAIL" if failures else "PASS",
        "release": str(release),
        "llms_txt_path": str(llms_path),
        "listed_url_count": len(urls),
        "failure_count": len(failures),
        "failures": failures,
    }


def generate(release: Path, manifest_path: Path, write: bool = False) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    public = release / "public"
    urls = manifest_urls(manifest)
    robots_path = public / "robots.txt"
    robots = RobotsTxt.parse(robots_path.read_text(encoding="utf-8")) if robots_path.is_file() else None
    failures = check_urls(public, urls, robots)
    body = render(manifest)
    result = {
        "schema": SCHEMA,
        "mode": "generate",
        "status": "FAIL" if failures else "PASS",
        "release": str(release),
        "manifest": str(manifest_path),
        "candidate_url_count": len(urls),
        "bytes": len(body.encode("utf-8")),
        "failure_count": len(failures),
        "failures": failures,
        "written": False,
    }
    if failures:
        return result
    if write:
        target = public / "llms.txt"
        target.write_text(body, encoding="utf-8")
        result["written"] = True
        result["llms_txt_path"] = str(target)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--release", type=Path, required=True)
    ap.add_argument("--generate", action="store_true")
    ap.add_argument("--write", action="store_true", help="with --generate, write public/llms.txt")
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ns = ap.parse_args(argv)
    result = generate(ns.release, ns.manifest, ns.write) if ns.generate else evaluate(ns.release)
    printable = dict(result)
    printable["failures"] = result["failures"][:20]
    print(json.dumps(printable, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
