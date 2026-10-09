from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import opus_review_loop as review


def test_reviewer_title_repairs_must_preserve_deterministic_bounds(tmp_path, monkeypatch):
    rubric = tmp_path / 'rubric.md'
    rubric.write_text('All five clinical gates A-E remain mandatory.')
    monkeypatch.setattr(review, 'RUBRIC', rubric)
    monkeypatch.setattr(review.review_transports, 'transport', lambda _: {})
    prompt = review.build_prompt({}, {'manifest_sha256':'a' * 64}, tmp_path / 'manifest.json', 1, tmp_path).read_text()
    assert '50–60 characters' in prompt
    assert 'Never prescribe an exact out-of-range' in prompt
    assert 'original clinical and claim findings remain blocking' in prompt
    assert 'Regenerate derived article and QA' in prompt
