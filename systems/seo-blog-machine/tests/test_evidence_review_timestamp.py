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
