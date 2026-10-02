"""The minter/selector default must not depend on an archived reports directory."""
import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_topic_supply_default_uses_genuine_maintained_inputs_outside_report_retention():
    # Read the actual selector's path configuration without importing host-only
    # analytics/runtime dependencies into a clean-checkout regression test.
    source = ROOT / 'systems/seo-blog-machine/scripts/select_opus_content_queue.py'
    tree = ast.parse(source.read_text())
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == 'DEFAULT_STRATEGY_QUEUE'
                              for target in node.targets))
    queue_path = eval(compile(ast.Expression(assignment.value), str(source), 'eval'), {'BUSINESS': ROOT})
    assert queue_path == ROOT / 'ops/glp/topic-supply-inputs/strategy-queue.json'
    # The original reports directory deliberately does not exist in this checkout.
    assert not (ROOT / 'reports/glp-clinic-scout/seo-blog-machine-20260804/topic-queues').exists()
    queue = json.loads(queue_path.resolve(strict=True).read_text())
    map_path = Path(queue['map_path'])
    assert map_path == Path('/home/ubuntu/business/ops/glp/topic-supply-inputs/cluster-map.json')
    map_data = (ROOT / map_path.relative_to('/home/ubuntu/business')).resolve(strict=True).read_bytes()
    assert hashlib.sha256(map_data).hexdigest() == '05d215c1bd3a8656f4bc5f5534a1ad5e13934b94a649492743fd975533f2818b'
    assert json.loads(map_data)['status'] == 'PASS_MAP_READY_QUEUE_ROTATION'
    assert queue['schema'] == 'glp-seo-cluster-rotation-queue-v1'
    assert len(queue['queue']) == 98
    # Only the input location changed: all genuine queue bytes, topic rows,
    # holds, and join authority must match the archived member exactly.
    original = queue_path.read_bytes().replace(
        b'/home/ubuntu/business/ops/glp/topic-supply-inputs/cluster-map.json',
        b'/home/ubuntu/business/reports/glp-clinic-scout/seo-blog-machine-20260804/G12-cluster-map-20260804.json',
    )
    assert hashlib.sha256(original).hexdigest() == '5763fcc1e92fb3cfe5b4175f7c1e2db38044274d3f8c4167333296d88d259ec9'
