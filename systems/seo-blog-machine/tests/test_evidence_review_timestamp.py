import yaml
from test_publication_date_binding import module, packet

def test_explicit_evidence_review_timestamp_survives_real_compilation(tmp_path):
    p=packet(tmp_path)
    draft=p/'04-draft.md'
    text=draft.read_text().replace("dateModified: '2026-09-05'", "dateModified: '2026-09-05'\nevidenceReviewedAt: '2026-09-05T19:42:11Z'")
    assert 'evidenceReviewedAt:' in text
    draft.write_text(text)
    article=module('compile_review_packet_article').compile_packet(p)
    assert article['evidence_review']['reviewed_at']=='2026-09-05T19:42:11Z'
    assert article['prices_checked_date']=='2026-09-05'
    assert article['date_published'] is None

import pytest

@pytest.mark.parametrize('value', ['not-a-timestamp','2026-09-04T19:42:11Z',
                                 '2026-09-06T19:42:11Z','2026-09-05T19:42:11',
                                 '2026-09-05T19:42:11+01:00'])
def test_explicit_evidence_timestamp_refuses_invalid_or_wrong_day(tmp_path,value):
    p=packet(tmp_path); draft=p/'04-draft.md'
    draft.write_text(draft.read_text().replace("dateModified: '2026-09-05'",f"dateModified: '2026-09-05'\nevidenceReviewedAt: '{value}'"))
    with pytest.raises(RuntimeError,match='evidence_review_timestamp_invalid'):
        module('compile_review_packet_article').compile_packet(p)


@pytest.mark.parametrize('day, reviewed_at', [
    ('2026-09-05', '2026-09-05T12:00:00.000001Z'),
    ('2026-09-06', '2026-09-06T00:00:00Z'),
])
def test_same_day_explicit_timestamp_cannot_exceed_compile_start(tmp_path, monkeypatch, day, reviewed_at):
    import datetime as dt

    compiler = module('compile_review_packet_article')
    clocks = []
    class CompileClock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            # A late re-read must not open a window during packet compilation.
            clocks.append(tz)
            return cls(2026, 9, 5 if len(clocks) == 1 else 7, 12, 0, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(compiler.dt, 'datetime', CompileClock)
    p = packet(tmp_path)
    draft = p / '04-draft.md'
    draft.write_text(draft.read_text().replace("dateModified: '2026-09-05'",
        f"dateModified: '{day}'\nevidenceReviewedAt: '{reviewed_at}'"))
    with pytest.raises(RuntimeError, match='^evidence_review_timestamp_invalid$'):
        compiler.compile_packet(p)
    assert clocks == [dt.timezone.utc]


@pytest.mark.parametrize('reviewed_at', ['2026-09-05T17:59:59Z', '2026-09-05T18:00:00Z'])
def test_explicit_timestamp_cannot_predate_latest_ledger_check(tmp_path, reviewed_at):
    import json

    p = packet(tmp_path)
    ledger = p / '01-evidence-ledger.json'
    # Latest is deliberately in the middle, includes a non-public captured source,
    # and has finer precision than the review. All packet evidence applies.
    ledger.write_text(json.dumps({'sources': [
        {'source_id': 'S1', 'checked_at_utc': '2026-09-04T23:00:00Z'},
        {'source_id': 'S2', 'checked_at_utc': '2026-09-05T18:00:00.500000+00:00'},
        {'source_id': 'S3', 'checked_at_utc': '2026-09-05T12:00:00Z'},
    ]}))
    draft = p / '04-draft.md'
    draft.write_text(draft.read_text().replace("dateModified: '2026-09-05'",
        f"dateModified: '2026-09-05'\nevidenceReviewedAt: '{reviewed_at}'"))
    before = {f: f.read_bytes() for f in p.iterdir()}
    with pytest.raises(RuntimeError, match='^evidence_review_timestamp_invalid$'):
        module('compile_review_packet_article').compile_packet(p)
    assert {f: f.read_bytes() for f in p.iterdir()} == before


@pytest.mark.parametrize('checked_at', [None, '', 42, 'not-a-timestamp',
    '2026-09-05', '2026-09-05T18:00:00', '2026-09-05T18:00:00+01:00'])
def test_explicit_timestamp_refuses_unusable_ledger_clock(tmp_path, checked_at):
    import json

    p = packet(tmp_path)
    (p / '01-evidence-ledger.json').write_text(json.dumps({'sources': [
        {'source_id': 'S1', 'checked_at_utc': checked_at}]}))
    draft = p / '04-draft.md'
    draft.write_text(draft.read_text().replace("dateModified: '2026-09-05'",
        "dateModified: '2026-09-05'\nevidenceReviewedAt: '2026-09-05T19:42:11Z'"))
    with pytest.raises(RuntimeError, match='^evidence_review_timestamp_invalid$'):
        module('compile_review_packet_article').compile_packet(p)


@pytest.mark.parametrize('reviewed_at', ['2026-09-05T18:00:00.500000Z', '2026-09-05T19:42:11Z'])
def test_explicit_timestamp_in_inclusive_ledger_and_compile_bounds_is_preserved(tmp_path, monkeypatch, reviewed_at):
    import datetime as dt
    import json

    compiler = module('compile_review_packet_article')
    class CompileClock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 5, 19, 42, 11, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(compiler.dt, 'datetime', CompileClock)
    p = packet(tmp_path)
    (p / '01-evidence-ledger.json').write_text(json.dumps({'sources': [
        {'source_id': 'S1', 'checked_at_utc': '2026-09-05T18:00:00.500000+00:00'},
        # Date-only evidence remains unknown precision, not a forged end-of-day.
        {'source_id': 'S2', 'checked_date': '2026-09-05'},
    ]}))
    draft = p / '04-draft.md'
    draft.write_text(draft.read_text().replace("dateModified: '2026-09-05'",
        f"dateModified: '2026-09-05'\nevidenceReviewedAt: '{reviewed_at}'"))
    before = {f: f.read_bytes() for f in p.iterdir()}
    article = compiler.compile_packet(p)
    assert article['evidence_review']['reviewed_at'] == reviewed_at
    assert article['prices_checked_date'] == article['source_checked_through'] == '2026-09-05'
    assert {f: f.read_bytes() for f in p.iterdir()} == before


def test_historic_date_fallback_is_not_replaced_with_capture_or_current_time(tmp_path):
    import json

    p = packet(tmp_path)
    (p / '01-evidence-ledger.json').write_text(json.dumps({'sources': [
        {'source_id': 'S1', 'checked_at_utc': '2026-09-05T18:00:00Z'}]}))
    before = {f: f.read_bytes() for f in p.iterdir()}
    article = module('compile_review_packet_article').compile_packet(p)
    # Legacy date precision only: not a newly asserted actual review instant.
    assert article['evidence_review']['reviewed_at'] == '2026-09-05T00:00:00Z'
    assert article['prices_checked_date'] == article['source_checked_through'] == '2026-09-05'
    assert {f: f.read_bytes() for f in p.iterdir()} == before
