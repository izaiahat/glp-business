"""Offline GLP-76 scalar/argument regression; never import or run the updater.

The AST slice executes the maintained publication-date assignment and materializer
argument construction. This is not startup, review, build or publication coverage.
The JS fixture is the exact accepted-base resolver, not a relaxed test oracle.
"""
import ast
import datetime as dt
import json
from pathlib import Path
import subprocess
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/glp_seo_updater_run.py"
RESOLVER = Path(__file__).parent / "fixtures/glp76-material-date-resolver.cjs"
ROUTE = "/guides/found-vs-trimi-insurance-self-pay/"


def materializer_args(target, today):
    tree = ast.parse(SOURCE.read_text())
    execute = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "execute")
    date = next(n for n in ast.walk(execute) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "publication_date" for t in n.targets))
    call = next(n for n in ast.walk(execute) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name) and n.func.id == "run_child"
                and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == "materialize")
    clock_calls = []
    scope = {"target_pt_date": target, "pt_date": lambda: clock_calls.append(today) or today}
    exec(compile(ast.Module(body=[date], type_ignores=[]), str(SOURCE), "exec"), scope)
    scope.update({name: "/isolated/" + name for name in (
        "PYTHON", "MATERIALIZER", "packet", "topic", "route", "run_id", "release_root",
        "fresh_selection_path", "fresh_authority_path", "REVIEW_TRANSPORT")})
    args = eval(compile(ast.Expression(call.args[1]), str(SOURCE), "eval"), scope)
    assert scope["target_pt_date"] == target
    return args, clock_calls


def resolve(release, material="2026-09-09"):
    # The only subprocess is a local Node scalar resolver, fed synthetic data.
    runner = RESOLVER.read_text() + '''
const [release, material, route] = JSON.parse(process.argv[1]);
try { console.log(JSON.stringify(resolveMaterialDate(route, material,
  {lastmod: "2026-07-23", action: "catalog_owned_editorial_date"}, release))); }
catch (e) { console.log(JSON.stringify({error: e.message})); }
'''
    return json.loads(subprocess.check_output(
        ["node", "-e", runner, json.dumps([release, material, ROUTE])], text=True))


@pytest.mark.parametrize("target", ["2026-09-03", "2026-09-26", None])
def test_publication_clock_is_not_historical_plan_day(target):
    args, calls = materializer_args(target, "2026-09-26")
    publication = args[args.index("--publication-date") + 1]
    assert publication == "2026-09-26"
    assert args[args.index("--pt-date") + 1] == publication
    assert calls == ["2026-09-26"]
    # September 9 is preserved, not erased to September 3 or stamped September 26.
    assert resolve(publication) == {"lastmod": "2026-09-09", "action": "declared_material_page_edit"}


def test_original_collision_and_genuine_future_dates_still_refuse():
    assert resolve("2026-09-03") == {"error": "lastmod_material_date_future_or_invalid:" + ROUTE}
    assert resolve("2026-09-26", "2026-09-27") == {"error": "lastmod_material_date_future_or_invalid:" + ROUTE}
    assert resolve("2026-09-26", "invalid") == {"error": "lastmod_material_date_future_or_invalid:" + ROUTE}


def test_plan_selection_and_authority_keep_historical_identity():
    tree = ast.parse(SOURCE.read_text())
    execute = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "execute")
    find_due = next(n for n in ast.walk(execute) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name) and n.func.id == "find_due")
    assert any(k.arg == "target_pt_date" and ast.unparse(k.value) == "target_pt_date" for k in find_due.keywords)
    authority = next(n for n in ast.walk(execute) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name) and n.func.id == "current_authority")
    assert any(k.arg == "as_of" and ast.unparse(k.value) == "as_of" for k in authority.keywords)
    as_of = next(n for n in ast.walk(execute) if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "as_of" for t in n.targets))
    scope = {"dt": dt, "plan": {"as_of": "2026-09-03"}}
    exec(compile(ast.Module(body=[as_of], type_ignores=[]), str(SOURCE), "exec"), scope)
    assert scope["as_of"] == dt.date(2026, 9, 3)
    assert scope["plan"] == {"as_of": "2026-09-03"}


def test_publication_clock_uses_pacific_day_at_utc_boundary():
    tree = ast.parse(SOURCE.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "pt_date")
    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 9, 27, 1, 0, tzinfo=dt.timezone.utc)
    from types import SimpleNamespace
    scope = {"dt": SimpleNamespace(datetime=Clock, timezone=dt.timezone), "PT": ZoneInfo("America/Los_Angeles")}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SOURCE), "exec"), scope)
    assert scope["pt_date"]() == "2026-09-26"
