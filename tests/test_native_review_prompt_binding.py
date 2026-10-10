import sqlite3
import pytest
from test_article_reviewer_custody import fixture, review

@pytest.mark.parametrize('tamper', ['none', 'missing', 'changed', 'conflict'])
def test_native_prompt_custody(tmp_path, monkeypatch, tamper):
    a, i, db, *_ = fixture(tmp_path, monkeypatch)
    h = review.sha_bytes(review.SYSTEM_BRIEF.encode())
    with sqlite3.connect(db) as c:
        c.execute('alter table sessions add column system_prompt_hash text')
        c.execute('create table system_prompts(hash text primary key,prompt text not null)')
        c.execute('insert into system_prompts values(?,?)', (h, review.SYSTEM_BRIEF))
        c.execute('update sessions set system_prompt=NULL,system_prompt_hash=?', (h,))
    if tamper != 'none':
        with sqlite3.connect(db) as c:
            if tamper == 'missing':
                c.execute('delete from system_prompts')
            elif tamper == 'changed':
                c.execute("update system_prompts set prompt='counterfeit'")
            else:
                c.execute("update sessions set system_prompt='counterfeit'")
    payload = review.build_payload(a, {})
    if tamper != 'none':
        with pytest.raises(RuntimeError, match='custody'):
            review.run_hermes(payload, a)
        return
    _, _, execution = review.run_hermes(payload, a)
    assert execution['result_custody']['system_prompt_sha256'] == h
