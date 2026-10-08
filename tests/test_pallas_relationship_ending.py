"""GLP-347 exact operator disposition: denials only, no generic cancellation bypass."""
import copy
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pallas_relationship_ending as ending


def source():
    row = [''] * 27
    row[2] = ending.NAME
    row[4] = 'Katalys'
    row[5] = ending.APPLICATION
    row[10] = 'live_wired_verified'
    row[14] = 'https://track.revoffers.com/aff_c?offer_id=1622&aff_id=13031'
    row[19] = row[20] = True
    row[23] = 'Historical approval and issued link retained.'
    return row


def test_exact_ended_projection_preserves_history():
    before = source()
    cells = ending.cells(before, '2026-10-07T23:00:00Z')
    assert cells['10'] == 'ended_operator_contract_termination'
    assert cells['19'] == cells['20'] == 'FALSE'
    assert cells['23'].startswith(before[23] + '\n')
    assert '2026-09-30' in cells['23']
    assert not ({'14', '16', '17'} & cells.keys())


@pytest.mark.parametrize('column,value', [(4, 'Awin'), (5, ending.APPLICATION.replace('/1622','/1623')), (2, 'Pallas sibling'), (10, 'hold_provider'), (14, '')])
def test_foreign_or_held_identity_refused(column, value):
    row = source(); row[column] = value
    with pytest.raises(ValueError): ending.cells(row, '2026-10-07T23:00:00Z')


def test_full_preimage_or_exact_postimage_only():
    row = source(); projected = ending.cells(row, '2026-10-07T23:00:00Z')
    assert ending.matches(row, row, projected)
    post = copy.deepcopy(row)
    for column, value in projected.items(): post[int(column)] = False if column in ('19','20') else value
    assert ending.matches(post, row, projected)
    post[16] = 'changed commission'
    assert not ending.matches(post, row, projected)
