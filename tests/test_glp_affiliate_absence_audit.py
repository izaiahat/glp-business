import sys,json
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from glp_affiliate_absence_audit import audit

def test_unmapped_offer_requires_absence_from_every_rendered_anchor(tmp_path):
    (tmp_path/'data').mkdir();(tmp_path/'public').mkdir();(tmp_path/'functions/go').mkdir(parents=True)
    for name in ('program-registry','approved-program-registry','affiliate-wire-in-approvals','affiliate-activation-selection','go-selection-receipt'):
        (tmp_path/'data'/f'{name}.json').write_text(json.dumps({'programs':[]}))
    (tmp_path/'functions/go/program-manifest.js').write_text('export const GO_RUNTIME_MANIFEST = Object.freeze({"programs":{}});')
    (tmp_path/'public/index.html').write_text('<a href="/providers/">Providers</a>')
    cells=['']*27;cells[2]='Cancelled fixture';cells[14]='https://network.test/click?offer_id=1'
    report={'unmapped_adverse':[{'network':'katalys','program_id':'1','sheet_row':2,'status':'cancelled_by_network'}]}
    snapshot={'rows':[{'sheet_row':2,'cells':cells}]}
    result=audit(tmp_path,report,snapshot)
    assert result['remaining_unmapped']==result['evidenced_no_live_offer_presence']==1
    (tmp_path/'public/lazy.json').write_text(json.dumps({'html':'<a href="https://network.test/click?offer_id=1&subid=x">Start</a>'}))
    with pytest.raises(ValueError,match='has_site_presence'):audit(tmp_path,report,snapshot)
