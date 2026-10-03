import importlib.util
from pathlib import Path
import copy

spec=importlib.util.spec_from_file_location('registry_sync',Path(__file__).resolve().parents[1]/'scripts/glp_affiliate_registry_sync.py')
sync=importlib.util.module_from_spec(spec);spec.loader.exec_module(sync)


def test_real_shape_pause_denies_without_reclassifying_cancellation_or_sibling():
    registry={'programs':[{'program_id':'alpha','display_name':'Alpha','commercial_state':'live'},
                          {'program_id':'sibling','display_name':'Alpha other rail','commercial_state':'held'}]}
    cells=['']*27
    cells[4]='MaxBounty';cells[5]='https://affiliates.maxbounty.com/campaign/29946';cells[10]='cancelled_by_network'
    cells[16]='';cells[24]='2026-09-11T19:25:14Z'
    after,changes,_=sync.project(registry,{'alpha':('maxbounty','29946'),'sibling':('awin','29946')},
                                  {'rows':[{'sheet_row':724,'cells':cells}]})
    assert len(changes)==1
    assert after['programs'][0]['affiliate_relationship_snapshot']['lifecycle']=='withdrawn'
    assert after['programs'][0]['affiliate_relationship_snapshot']['promotion_denied'] is True
    assert 'terms' not in after['programs'][0]['affiliate_relationship_snapshot']
    assert after['programs'][1]==registry['programs'][1]
    assert sync.project(after,{'alpha':('maxbounty','29946')},{'rows':[{'sheet_row':724,'cells':cells}]})[1]==[]
    cells[10]='paused_network_program_paused'
    after,_,_=sync.project(registry,{'alpha':('maxbounty','29946')},{'rows':[{'sheet_row':724,'cells':cells}]})
    assert after['programs'][0]['affiliate_relationship_snapshot']['lifecycle']=='paused'
    cells[10]='approved_active_tracking_ready'
    after,_,_=sync.project(after,{'alpha':('maxbounty','29946')},{'rows':[{'sheet_row':724,'cells':cells}]})
    assert after['programs'][0]['affiliate_relationship_snapshot']['promotion_denied'] is True


def test_foreign_host_and_conflicting_ids_are_not_relationship_identity():
    cells=['']*27;cells[4]='MaxBounty'
    for u in ['https://example.com/campaign/29946','https://affiliates.maxbounty.com/campaign/29946?campaign_id=1']:
        cells[5]=u
        assert sync.row_identity(cells) is None
