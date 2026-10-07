#!/usr/bin/env python3
"""Read one visible Katalys site relationship using an owned fresh AI driver.

No authentication, API requests, hidden app state, mutations or tracking visits.
Program availability (Active), Manage and Create Link are not approval evidence.
Only the Applied/site/status block on the exact protected detail page is read.
"""
from __future__ import annotations

import re

PUBLISHER = '098e5455-ca72-4099-9ff9-a9dd014148e7'
SITE = 'GLP Clinic Scout Site'
STATUS = {'Approved':'approved', 'Pending':'pending', 'Rejected':'rejected'}


def _name(value):
    return re.sub(r'[^a-z0-9]+', '', str(value).lower())


def relationship_snapshot(program, expected_name, *, fresh_driver):
    if (fresh_driver is None or getattr(fresh_driver, 'fresh', False) is not True
            or not getattr(fresh_driver, 'owned_session', '')):
        raise RuntimeError('fresh_ai_browser_owned_runtime_required')
    program = str(program)
    if not re.fullmatch(r'[1-9][0-9]*', program) or len(_name(expected_name)) < 4:
        raise RuntimeError('katalys_exact_program_required')
    target = f'https://app.katalys.com/pub/{PUBLISHER}/explore/{program}'
    fresh_driver.goto(target)
    lines = [line.strip() for line in fresh_driver.body_text().splitlines() if line.strip()]
    if (fresh_driver.url != target or _name(fresh_driver.title()) != _name(f'Explore: {expected_name}')
            or lines.count(f'Program ID: {program}') != 1):
        raise RuntimeError('katalys_identity_or_session_mismatch')
    identity_end = lines.index(f'Program ID: {program}')
    # The header must supply the protected principal and organization, not an
    # email or account name quoted in marketing prose after the program ID.
    header = lines[:identity_end]
    if any(header.count(value) != 1 for value in
           ('ops@glpclinicscout.com', 'Tilton Group', 'GLP Clinic Scout')):
        raise RuntimeError('katalys_protected_principal_unverified')
    explore = max((i for i, line in enumerate(header) if line == 'Explore'), default=len(header))
    page_header = header[explore + 1:]
    if len(page_header) < 2 or any(_name(line) != _name(expected_name) for line in page_header[:2]):
        raise RuntimeError('katalys_program_name_mismatch')
    tail = lines[identity_end + 1:]
    # Retained UI layout: Payout ... Applied / traffic-source name / status /
    # Default Landing Page. No status anywhere else can establish a relationship.
    if (tail.count('Payout') != 1 or tail.count('Applied') != 1
            or tail.count('Default Landing Page') != 1):
        raise RuntimeError('katalys_visible_relationship_fields_missing')
    payout, applied, end = (tail.index(label) for label in ('Payout', 'Applied', 'Default Landing Page'))
    if not payout < applied < end:
        raise RuntimeError('katalys_visible_relationship_fields_ambiguous')
    fields = tail[applied + 1:end]
    if len(fields) != 2 or fields[0] != SITE or fields[1] not in STATUS:
        raise RuntimeError('katalys_visible_relationship_unknown_or_ambiguous')
    return {'provider':'Katalys', 'program_id':program, 'relationship':STATUS[fields[1]],
            'details':{'name':str(expected_name), 'principal_verified':True,
                       'publisher_id':PUBLISHER, 'relationship_scope':SITE,
                       'visible_status':fields[1], 'inventory_complete':False},
            'terms':'', 'terms_readback':'UNKNOWN', 'tracking_link':'', 'link_generated':False,
            'verified':True, 'provider_readback_verified':True, 'provider_writes':0,
            'read_method':'fresh_ai_browser_vault_visible_relationship'}
