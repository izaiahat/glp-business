"""Read-only article suite observations; never authorizes publication or replay."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

BUSINESS = Path('/home/ubuntu/business')
SCRIPTS = BUSINESS / 'systems/seo-blog-machine/scripts'
WORK_ROOT = BUSINESS / 'reports/glp-clinic-scout/seo-machine/article-buffer-work'


def unknown(status: str, reason: str) -> dict:
    return {'status': status, 'accepted_inherited_count': None,
            'new_failure_count': None, 'reason': reason,
            'evidence_kind': 'read_only_suite_observation_not_publication'}


def read_classification(path: Path) -> dict:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
        pointer, bound = value['base'], value['binding']
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import accepted_failure_contract as contract
        if contract.descriptor(contract.runtime_binding_path(pointer)) != bound:
            raise ValueError('classification_runtime_binding_drift')
        binding = contract.validate_binding(contract.read_bound(bound), pointer)
        for field, filename in [('suite_stdout', '00-suite-stdout.log'), ('suite_stderr', '00-suite-stderr.log')]:
            if contract.descriptor(path.parent / filename) != value[field]:
                raise ValueError('classification_log_drift')
        expected = contract.classify_suite((path.parent / '00-suite-stdout.log').read_text(),
            value['suite_exit_code'], binding['accepted_names'], minimum_tests=binding['minimum_tests'])
        expected.update(binding=bound, base=pointer, suite_stdout=value['suite_stdout'], suite_stderr=value['suite_stderr'])
        if value != expected:
            raise ValueError('classification_recomputed_mismatch')
        for list_key, count_key in [('accepted_inherited', 'accepted_inherited_count'),
                                    ('new_failures', 'new_failure_count')]:
            names = value[list_key]
            count = value[count_key]
            if not isinstance(names, list) or type(count) is not int or count != len(names):
                raise ValueError('classification_count_mismatch')
        if value['status'] == 'PASS_ACCEPTED_FAILURES' and (
            value['new_failure_count'] or value.get('errors')
        ):
            raise ValueError('classification_false_pass')
        return {key: value.get(key) for key in [
            'status', 'accepted_inherited_count', 'new_failure_count', 'counts',
            'accepted_inherited', 'new_failures', 'binding', 'errors'
        ]} | {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(),
             'evidence_kind': 'read_only_suite_observation_not_publication'}
    except FileNotFoundError:
        return unknown('UNKNOWN_NOT_YET_CLASSIFIED', str(path))
    except (OSError, KeyError, ValueError, TypeError, RuntimeError, ImportError) as error:
        return unknown('UNKNOWN_INVALID_CLASSIFICATION', f'{path}: {type(error).__name__}')


def accepted_failure_health(day: str, *, work_root: Path | None = None,
                            operation_id: str | None = None) -> dict:
    """Follow the same latest hash-sealed materialization as the carrier."""
    from datetime import date
    try:
        date.fromisoformat(day)
        operation = operation_id if operation_id is not None else 'n8n-article-carrier-' + day
        if not isinstance(operation, str) or not operation.startswith('n8n-article-carrier-'):
            raise ValueError('operation_invalid')
        digest = hashlib.sha256(operation.encode()).hexdigest()
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        from article_buffer import _latest_materialization
        generation, materialization_path, record = _latest_materialization((work_root or WORK_ROOT) / digest)
        if record is None:
            return unknown('UNKNOWN_NO_MATERIALIZATION', operation)
        if record.get('operation_id') != operation:
            raise ValueError('operation_mismatch')
        spec = record['materialized']['apply_template']
        template_path = Path(spec['path'])
        raw = template_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != spec['sha256']:
            raise ValueError('apply_template_hash_mismatch')
        template = json.loads(raw)
        result = read_classification(Path(template['report_dir']) / '00-accepted-failures-classification.json')
        return result | {'operation_id': operation, 'generation': generation,
                         'materialization': str(materialization_path)}
    except (OSError, KeyError, ValueError, TypeError, RuntimeError) as error:
        return unknown('UNKNOWN_INVALID_MATERIALIZATION', type(error).__name__)
