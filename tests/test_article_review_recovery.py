"""Recovery never authenticates a missing historical reviewer session."""
import copy
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import article_review_recovery as recovery


def test_cf_normalization_is_narrow():
    key=42
    cipher=bytes([key])+bytes(x^key for x in b'support@glpclinicscout.com')
    text='<a href="/cdn-cgi/l/email-protection#'+cipher.hex()+'"><span class="__cf_email__" data-cfemail="'+cipher.hex()+'">[email&#160;protected]</span></a>'
    assert recovery.normalize_live(text)=='<a href="mailto:support@glpclinicscout.com">support@glpclinicscout.com</a>'
    assert recovery.normalize_live('<script>unsafe()</script>')=='<script>unsafe()</script>'


def test_subject_recovery_requires_all_immutable_original_bytes_and_live_subject(tmp_path):
    original=tmp_path/'old';fresh=tmp_path/'new';original.mkdir();fresh.mkdir()
    rows=[]
    for name in recovery.SUBJECTS:
        (original/name).write_text(name);(fresh/name).write_text(name)
        rows.append({'path':name,'sha256':recovery.sha(original/name)})
    (fresh/'08-live-guide.html').write_text('<html>exact live</html>')
    manifest={'subject_files':rows+[{'path':'08-live-guide.html','sha256':recovery.sha(fresh/'08-live-guide.html')}]}
    recovery.subjects_match(original, fresh, rows, manifest)
    (fresh/'04-draft.md').write_text('changed draft')
    with pytest.raises(ValueError,match='original_subject_drift'):
        recovery.subjects_match(original, fresh, rows, manifest)
    (fresh/'04-draft.md').write_text('04-draft.md')
    for tamper in ('missing_live','extra','rehashed_article'):
        m=copy.deepcopy(manifest)
        if tamper=='missing_live':m['subject_files']=rows
        elif tamper=='extra':m['subject_files'].append({'path':'other','sha256':'a'*64})
        else:m['subject_files'][0]['sha256']='a'*64
        with pytest.raises(ValueError):recovery.subjects_match(original,fresh,rows,m)


def test_natural_cannot_be_recovered(tmp_path):
    with pytest.raises(ValueError,match='postpublication_review_not_natural'):
        recovery.validate_recovery(tmp_path/'entry.json', root=tmp_path, natural=True)
