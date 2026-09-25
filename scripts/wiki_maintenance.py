#!/usr/bin/env python3
"""
Wiki Maintenance Script for Kael Business Wiki
Runs daily at 5:00 AM UTC via cron: 0 5 * * *

Checks:
  1. Orphan pages (no inbound wikilinks)
  2. Broken wikilinks (link to non-existent pages)
  3. Missing frontmatter (pages without YAML header)
  4. Stale pages (not updated in 30+ days)
  5. Updates index.md page counts
  6. Rotates log.md if over 500 lines
"""

import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

WIKI_ROOT = Path.home() / "business" / "wiki"
ENTITIES_DIR = WIKI_ROOT / "entities"
CONCEPTS_DIR = WIKI_ROOT / "concepts"
COMPARISONS_DIR = WIKI_ROOT / "comparisons"
QUERIES_DIR = WIKI_ROOT / "queries"
INDEX_FILE = WIKI_ROOT / "index.md"
LOG_FILE = WIKI_ROOT / "log.md"
RAW_DIR = WIKI_ROOT / "raw" / "articles"

STALE_DAYS = 30
LOG_MAX_LINES = 500

WIKILINK_RE = re.compile(r'\[\[([^\]]+)\]\]')
FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---', re.DOTALL)
UPDATED_RE = re.compile(r'^updated:\s*(\d{4}-\d{2}-\d{2})', re.MULTILINE)
TITLE_RE = re.compile(r'^title:\s*(.+)', re.MULTILINE)
STATUS_RE = re.compile(r'^status:\s*(.+)', re.MULTILINE)


def find_all_pages():
    """Find all .md pages in entities/, concepts/, comparisons/, queries/."""
    pages = {}
    for d in [ENTITIES_DIR, CONCEPTS_DIR, COMPARISONS_DIR, QUERIES_DIR]:
        if not d.exists():
            continue
        for f in d.glob("*.md"):
            slug = f.stem
            pages[slug] = f
    return pages


def extract_frontmatter(content):
    """Extract YAML frontmatter from page content."""
    m = FRONTMATTER_RE.match(content)
    return m.group(1) if m else None


def extract_wikilinks(content):
    """Extract all [[wikilink]] targets from content."""
    links = WIKILINK_RE.findall(content)
    # Normalize: strip paths, just get the slug
    slugs = []
    for link in links:
        # Handle [[concepts/foo]] -> foo
        slug = link.split("/")[-1].strip()
        slugs.append(slug)
    return slugs


def check_orphans(pages):
    """Find pages with zero inbound wikilinks."""
    inbound = {slug: 0 for slug in pages}
    for slug, path in pages.items():
        content = path.read_text(errors="replace")
        for target in extract_wikilinks(content):
            if target in inbound:
                inbound[target] += 1
    orphans = [slug for slug, count in inbound.items() if count == 0]
    return orphans


def check_broken_links(pages):
    """Find wikilinks pointing to non-existent pages."""
    broken = []
    for slug, path in pages.items():
        content = path.read_text(errors="replace")
        for target in extract_wikilinks(content):
            if target not in pages:
                broken.append((slug, target))
    return broken


def check_missing_frontmatter(pages):
    """Find pages without valid YAML frontmatter."""
    missing = []
    for slug, path in pages.items():
        content = path.read_text(errors="replace")
        if not extract_frontmatter(content):
            missing.append(slug)
    return missing


def check_stale_pages(pages):
    """Find pages not updated in STALE_DAYS+."""
    stale = []
    cutoff = datetime.now() - timedelta(days=STALE_DAYS)
    for slug, path in pages.items():
        content = path.read_text(errors="replace")
        m = UPDATED_RE.search(content)
        if m:
            try:
                updated = datetime.strptime(m.group(1), "%Y-%m-%d")
                if updated < cutoff:
                    stale.append((slug, m.group(1)))
            except ValueError:
                stale.append((slug, "invalid-date"))
        else:
            stale.append((slug, "no-date"))
    return stale


def update_index_counts(pages):
    """Update the page counts in index.md."""
    if not INDEX_FILE.exists():
        return

    entity_count = len(list(ENTITIES_DIR.glob("*.md"))) if ENTITIES_DIR.exists() else 0
    concept_count = len(list(CONCEPTS_DIR.glob("*.md"))) if CONCEPTS_DIR.exists() else 0
    comparison_count = len(list(COMPARISONS_DIR.glob("*.md"))) if COMPARISONS_DIR.exists() else 0
    query_count = len(list(QUERIES_DIR.glob("*.md"))) if QUERIES_DIR.exists() else 0
    raw_count = len(list(RAW_DIR.glob("*.md"))) if RAW_DIR.exists() else 0
    total = entity_count + concept_count + comparison_count + query_count

    content = INDEX_FILE.read_text()
    today = datetime.now().strftime("%Y-%m-%d")

    # Update counts line
    new_counts = f"Total pages: {total} ({entity_count} entities, {concept_count} concepts, {comparison_count} comparisons, {query_count} queries)"
    content = re.sub(r'Total pages:.*', new_counts, content)

    # Update raw articles count
    content = re.sub(r'Raw articles: \d+', f'Raw articles: {raw_count}', content)

    # Update date
    content = re.sub(r'Last updated: \d{4}-\d{2}-\d{2}', f'Last updated: {today}', content)

    INDEX_FILE.write_text(content)
    return total


def rotate_log():
    """Rotate log.md if over LOG_MAX_LINES."""
    if not LOG_FILE.exists():
        return False
    lines = LOG_FILE.read_text().splitlines()
    if len(lines) > LOG_MAX_LINES:
        # Keep header + last 200 lines
        header = lines[:3]
        recent = lines[-200:]
        rotated = header + ["\n[... older entries rotated out ...]\n"] + recent
        LOG_FILE.write_text("\n".join(rotated))
        return True
    return False


def append_log(message):
    """Append a maintenance entry to log.md."""
    if not LOG_FILE.exists():
        LOG_FILE.write_text("# WIKI CHANGELOG\n\n")
    today = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    with open(LOG_FILE, "a") as f:
        f.write(f"\n## {today} — Maintenance Run\n{message}\n")


def main():
    pages = find_all_pages()
    results = []

    # 1. Orphans
    orphans = check_orphans(pages)
    if orphans:
        results.append(f"- Orphan pages ({len(orphans)}): {', '.join(sorted(orphans))}")
    else:
        results.append("- No orphan pages ✅")

    # 2. Broken links
    broken = check_broken_links(pages)
    if broken:
        broken_str = "; ".join(f"{src}→{tgt}" for src, tgt in broken[:10])
        results.append(f"- Broken wikilinks ({len(broken)}): {broken_str}")
    else:
        results.append("- No broken wikilinks ✅")

    # 3. Missing frontmatter
    missing = check_missing_frontmatter(pages)
    if missing:
        results.append(f"- Missing frontmatter ({len(missing)}): {', '.join(sorted(missing))}")
    else:
        results.append("- All pages have frontmatter ✅")

    # 4. Stale pages
    stale = check_stale_pages(pages)
    if stale:
        stale_str = ", ".join(f"{s[0]} ({s[1]})" for s in stale[:10])
        results.append(f"- Stale pages ({len(stale)}): {stale_str}")
    else:
        results.append("- No stale pages ✅")

    # 5. Update index counts
    total = update_index_counts(pages)
    results.append(f"- Index updated: {total} total pages")

    # 6. Rotate log if needed
    rotated = rotate_log()
    if rotated:
        results.append("- Log rotated (exceeded 500 lines)")

    report = "\n".join(results)
    append_log(report)

    print("=== Wiki Maintenance Report ===")
    print(report)
    print(f"\nTotal pages: {len(pages)}")

    # Exit with error code if issues found
    issues = len(orphans) + len(broken) + len(missing) + len(stale)
    if issues > 0:
        print(f"\n⚠️  {issues} total issues found")
    else:
        print("\n✅ All checks passed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
