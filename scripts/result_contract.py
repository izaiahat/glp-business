#!/usr/bin/env python3
"""Canonical skill-owner resolution and the existing RESULT CLI.

CLI: result_contract.py RESULT.json --owners owner-a owner-b
Or: result_contract.py RESULT.json --brief BRIEF.md
Owner resolution remains available to brief validation. RESULT correctness lives
only in result_lint; skill-accounting observations no longer veto closeout.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import sys

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
EXCLUDED = {"references", "templates", "assets", "scripts", ".git", ".github", ".archive", "_archived", ".archived", ".hub", ".venv", "venv", "node_modules", "site-packages", "__pycache__", ".tox", ".nox", ".pytest_cache", ".mypy_cache", ".ruff_cache"}

class ResultDefect(ValueError):
    """An incomplete/invalid receipt, not permission to retry its effects."""


def default_skills_root():
    return Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))) / 'skills'


def owners_from_brief(path):
    return owners_from_brief_text(Path(path).read_text(encoding='utf-8'))


def owners_from_brief_text(text):
    """The same owner grammar, without temporary files during lint."""
    match = re.search(r'^TOOLS\s*\n(.*?)(?=^(?:DELEGATION|OUTPUT|VERIFY|STOP)\s*$|\Z)', text, re.M | re.S)
    rows = re.findall(r'^Owning skills:\s*(.+)$', match.group(1) if match else '', re.M)
    if len(rows) != 1:
        raise ResultDefect('brief_requires_one_owning_skills_line')
    # Existing briefs put a separate Tools sentence on this same line.
    owner_text = rows[0].split('. Tools:', 1)[0]
    if owner_text.endswith('.'):
        owner_text = owner_text[:-1]
    names = [x.strip().strip('`') for x in owner_text.split(',')]
    if not names or any(not NAME.fullmatch(x) for x in names) or len(names) != len(set(names)):
        raise ResultDefect('brief_owners_must_be_unique_exact_skill_names')
    return names


def catalog_names(skills_root):
    root = Path(skills_root)
    if not root.is_dir():
        raise ResultDefect('skills_catalog_unavailable')
    names = set()
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not d.startswith('.'))
        if 'SKILL.md' not in files:
            continue
        text = (Path(directory) / 'SKILL.md').read_text(encoding='utf-8-sig')
        # Runtime metadata names; exact basename alone is not evidence.
        match = re.search(r'^name:\s*[\"\']?([a-z0-9][a-z0-9_-]{0,63})[\"\']?\s*$', text.split('---', 2)[1] if text.startswith('---') else '', re.M)
        if match:
            names.add(match.group(1))
    return names


def owner_names(root):
    owners = set()
    for filename in ('CAPABILITY-MAP.md', 'EXPERT-ROOTS.md'):
        path = root / filename
        if not path.is_file():
            raise ResultDefect('ownership_map_unavailable:' + filename)
        for line in path.read_text(encoding='utf-8').splitlines():
            cells = [x.strip().strip('`') for x in line.split('|')[1:-1]]
            if len(cells) < 2:
                continue
            # Expert table names the track in column 1; map names owner in 2.
            value = cells[0] if filename == 'EXPERT-ROOTS.md' else cells[1]
            if NAME.fullmatch(value) and (root / value / 'SKILL.md').is_file():
                owners.add(value)
    return owners


def read_result(path, *, expected_owners=None, brief_path=None, skills_root=None,
                expected_issue=None, require_complete=False):
    """Physical new receipt reader; legacy owner args remain non-gating metadata."""
    from result_lint import lint
    problems = lint(path, brief_path, expected_issue=expected_issue, require_complete=require_complete)
    if problems:
        raise ResultDefect('; '.join(problems))
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResultDefect('result_unreadable:' + type(exc).__name__) from exc
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('result')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--owners', nargs='+')
    group.add_argument('--brief')
    parser.add_argument('--skills-root', type=Path)
    parser.add_argument('--expected-issue')
    parser.add_argument('--require-complete', action='store_true')
    args = parser.parse_args(argv)
    try:
        read_result(args.result, expected_owners=args.owners, brief_path=args.brief, skills_root=args.skills_root,
                    expected_issue=args.expected_issue, require_complete=args.require_complete)
    except (ResultDefect, OSError, UnicodeError) as exc:
        print('RESULT_DEFECT:' + str(exc), file=sys.stderr)
        return 1
    print('RESULT_CONTRACT_PASS')
    return 0

if __name__ == '__main__':
    sys.exit(main())
