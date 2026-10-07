import hashlib
import importlib.util
import json
from pathlib import Path
import pytest


def test_launcher_requires_complete_exact_pinned_source(tmp_path):
    path=Path(__file__).resolve().parents[1]/'ops/hermes-cron-scripts/pro_route_dispatch.py'
    assert path.is_file(), 'Persistent cron launcher missing'
    spec=importlib.util.spec_from_file_location('pro_dispatch',path)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    checkout=tmp_path/'candidate';(checkout/'scripts').mkdir(parents=True)
    files=['scripts/pro_route.py','scripts/gpt6pro_relay.py','scripts/gpt6pro_packet.py']
    for f in files:(checkout/f).write_text('# fixture\n')
    config=tmp_path/'ops/gpt6pro-relay/route/EXECUTION.json';config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'checkout':str(checkout),'sha256':{f:hashlib.sha256((checkout/f).read_bytes()).hexdigest() for f in files}}))
    assert m.load_launch(tmp_path)==checkout/'scripts/pro_route.py'
    (checkout/files[0]).write_text('changed')
    with pytest.raises(ValueError,match='SOURCE_DRIFT'):m.load_launch(tmp_path)
