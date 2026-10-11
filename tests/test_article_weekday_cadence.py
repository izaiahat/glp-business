"""Operator weekday publication cadence; no live effects in fixtures."""
import ast
import datetime as dt
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def test_registry_installed_host_body_and_inactive_n8n():
    row = next(w for w in json.loads((ROOT / 'ops/n8n-workflow-registry.json').read_text())['workflows']
               if w['id'] == 'uabEqV8bzEYanLVJ')
    assert row['active'] is False
    assert "--cron '0 11 * * 1-5' --timezone America/Los_Angeles" in row['cron_lines'][0]
    assert len(row['cron_lines']) == 1
    trigger = row['cadence']['triggers'][0]
    assert trigger['disabled'] is True
    assert trigger['interval']['interval'][0]['expression'] == '0 11 * * 1-5'


def test_health_sensor_recognizes_tuesday_thursday_and_weekend():
    tree = ast.parse((ROOT / 'scripts/business_output_health_sensor.py').read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'last_carrier_day')
    scope = {'dt': dt}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), '<cadence>', 'exec'), scope)
    for day in range(12, 17):
        date = dt.date(2026, 10, day)
        assert scope['last_carrier_day'](date) == date
    for day in (17, 18):
        assert scope['last_carrier_day'](dt.date(2026, 10, day)) == dt.date(2026, 10, 16)


def test_failure_sensor_requires_article_each_weekday(monkeypatch):
    path = ROOT / 'ops/hermes-cron-scripts/glp_seo_publish_failure_alert.py'
    spec = importlib.util.spec_from_file_location('a1_alert', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'new_routes_today', lambda day: [])
    import sys
    monkeypatch.setitem(sys.modules, 'daily_outcome_cards', SimpleNamespace(
        r2_refreshes_today=lambda day: {'receipt': None, 'scheduled': False, 'ok': True,
                                     'status': 'TEST', 'required': None, 'completed': None,
                                     'published': None, 'deficit': None}))
    monkeypatch.setitem(sys.modules, 'article_carrier_gate_health', SimpleNamespace(
        accepted_failure_health=lambda day: {}))
    for day in range(12, 19):
        row = module.publication_summary(f'2026-10-{day}')['new_route']
        assert row['scheduled'] is (day < 17)
        assert row['cadence'] == 'Mon-Fri'


def test_refill_prompt_binds_weekly_review_and_five_ready():
    prompt = (ROOT / 'ops/hermes-cron-prompts/glp-seo-buffer-refill.md').read_text()
    assert 'minimum 5, maximum 5' in prompt
    assert 'current Sunday weekly SEO review' in prompt
    assert 'pool.ready >=5' in prompt
    assert 'maximum 3/day are research limits' in prompt
