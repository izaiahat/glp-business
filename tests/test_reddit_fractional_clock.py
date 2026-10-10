import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path


def test_fractional_clock_replays_exactly(tmp_path):
    path = Path(__file__).resolve().parents[1] / 'scripts/glp_reddit_pack_scout.py'
    spec = importlib.util.spec_from_file_location('reddit_clock', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=654321)
    raw = {'schema':'glp-reddit-firecrawl-search-captures-v1',
           'research_stack':'Firecrawl MCP web search only; no Reddit transport',
           'captured_at_utc':now.replace(microsecond=0).isoformat(),
           'effects':{}, 'captures':[{'group':'fixture', 'request':{
               'query':'site:reddit.com fixture', 'includeDomains':['reddit.com'],
               'sources':[{'type':'web'}]}, 'response':{
                   'success':True,'data':{'web':[{'url':'https://www.reddit.com/r/telehealth/comments/abc123/fixture/','title':'Fixture question','description':'Fixture'}]}}}]}
    capture = tmp_path / 'capture.json'
    capture.write_text(json.dumps(raw))
    sha = hashlib.sha256(capture.read_bytes()).hexdigest()
    result = module.run_discovery(capture_path=capture, expected_capture_sha256=sha,
        as_of=now.isoformat(), db_path=tmp_path/'state.sqlite3', output_root=tmp_path/'out')
    assert result['as_of_utc'] == now.isoformat().replace('+00:00','Z')
    replay = module.run_discovery(capture_path=capture, expected_capture_sha256=sha,
        as_of=now.isoformat(), db_path=tmp_path/'state.sqlite3', output_root=tmp_path/'out')
    assert replay['idempotent_replay'] is True
    assert result['effects']['reddit_posts'] == 0
