"""Weekly review controls supply, not clinical/release authority."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'systems/seo-blog-machine/scripts'))


def test_weekly_governance_only_keeps_named_route_and_bounds_cadence():
    import seo_weekly_governance as g
    strategy = {'week_start': '2026-10-05', 'refresh_list': [
        {'route': '/guides/allowed/', 'priority': 1},
        {'route': '/guides/fella-health/', 'priority': 2}], 'keyword_gaps': None}
    rows = [{'route': '/guides/other/'}, {'route': '/guides/allowed/'}, {'route': '/guides/fella-health/'}]
    assert g.refresh_candidates(rows, strategy) == [rows[1]]
    assert g.refresh_candidates(rows, None) == []
    assert g.CADENCE == {'new_target_per_day': 1, 'new_ceiling_per_day': 1,
                        'research_ceiling_per_day': 3, 'publication_weekdays': [0, 1, 2, 3, 4],
                        'meaningful_updates_min_per_week': 2, 'meaningful_updates_max_per_week': 4,
                        'no_op_allowed': True}


def test_current_week_new_topics_are_exact_and_unknown_is_noop():
    import seo_weekly_governance as g
    rows = [{'target_query': 'oral glp 1', 'target_route': '/guides/oral/'},
            {'target_query': 'stale topic', 'target_route': '/guides/stale/'}]
    strategy = {'keyword_gaps': [{'keyword': 'oral glp 1', 'target_route': '/guides/oral/'}]}
    assert g.new_candidates(rows, strategy) == rows[:1]
    assert g.new_candidates(rows, {'keyword_gaps': None}) == []
    assert g.new_candidates(rows, None) == []


def test_semantic_delta_rejects_date_and_paraphrase_but_accepts_small_answer():
    import existing_article_updates as u
    before = {'sections': [{'heading': 'Supply status', 'paragraphs': [' '.join(['baseline'] * 500)]}],
              'updated_at': '2026-10-01', 'sources': [{'url': 'https://www.fda.gov/old'}]}
    date_only = {**before, 'updated_at': '2026-10-09'}
    assert u.semantic_materiality(before, date_only)['material'] is False
    after = {**before, 'sections': before['sections'] + [{'heading': 'How to check supply status',
             'paragraphs': ['Check the FDA shortage database by the exact medicine and strength, then contact the pharmacy.']}],
             'sources': before['sources'] + [{'url': 'https://www.fda.gov/new', 'primary': True}]}
    assert u.substantive_delta_fraction(before, after) < 0.3
    assert u.semantic_materiality(before, after)['material'] is True


def test_pdf_repair_preserves_raw_evidence_and_hash(tmp_path):
    import json
    from quote_display_repair import normalize_zepbound_display
    raw = {'text': 'ZEPBOUNDtreated', 'quote_sha256': 'raw-hash', 'rendered_quote': 'ZEPBOUNDtreated'}
    (tmp_path/'01-evidence-ledger.json').write_text(json.dumps({'sources':[{'quotes':[raw]}]}))
    (tmp_path/'04-draft.md').write_text('ZEPBOUNDtreated')
    (tmp_path/'05-claim-source-map.json').write_text('{"claim":"ZEPBOUNDtreated"}')
    assert normalize_zepbound_display(tmp_path) == 1
    quote = json.loads((tmp_path/'01-evidence-ledger.json').read_text())['sources'][0]['quotes'][0]
    assert quote['text'] == raw['text'] and quote['quote_sha256'] == raw['quote_sha256']
    assert quote['rendered_quote'] == 'ZEPBOUND-treated'
    assert 'ZEPBOUNDtreated' not in (tmp_path/'04-draft.md').read_text()


def test_noop_receipt_is_not_an_article_selection():
    import selection_receipt as s
    import pytest
    doc = {'schema':s.SCHEMA,'status':'PASS_EMPTY_STRATEGY_SAFE_SET',
           'selected':[],'eligible':[],'weekly_governance':{'governing':True}}
    doc['receipt_sha256'] = s.payload_hash(doc)
    assert s.validate_selection_payload(doc) == doc
    with pytest.raises((RuntimeError, IndexError)):
        s.selection_binding(doc)


def test_sunday_review_window_governs_the_following_week():
    import datetime as dt
    import weekly_strategy_consumer as w
    assert w.review_week_start(dt.date(2026,10,9)) == dt.date(2026,10,5)
    assert w.review_week_start(dt.date(2026,10,11)) == dt.date(2026,10,12)
    assert w.review_week_start(dt.date(2026,10,13)) == dt.date(2026,10,12)


def test_source_check_does_not_invent_a_price_check():
    import compile_review_packet_article as c
    assert c.article_source_dates({'sourceCheckedThrough':'2026-10-09'}) == {
        'source_checked_through':'2026-10-09','prices_checked_date':None}
    assert c.article_source_dates({'pricesCheckedDate':'2026-08-31'}) == {
        'source_checked_through':'2026-08-31','prices_checked_date':'2026-08-31'}


def test_dormant_review_target_has_admitted_parent():
    import ast
    path = Path(__file__).resolve().parents[1] / 'scripts/glp_seo_updater_source.py'
    tree = ast.parse(path.read_text())
    function = next((node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'review_target'), None)
    assert function is not None
    scope = {'ROOT':Path('/home/ubuntu/business')}
    exec(compile(ast.Module(body=[function],type_ignores=[]),str(path),'exec'),scope)
    assert scope['review_target']('x').parent == Path('/home/ubuntu/business/site-system/flagship-v2/releases')
