from test_firecrawl_integration import ROOT, fc
import importlib.util
import json
from pathlib import Path

def test_pdf_parse_preserves_original_page(monkeypatch, tmp_path):
    import io
    from pypdf import PdfWriter
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location('article_pdf', ROOT / 'systems/seo-blog-machine/scripts/article_pdf.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    writer = PdfWriter(); writer.add_blank_page(width=100, height=100); writer.add_blank_page(width=100, height=100)
    pdf = io.BytesIO(); writer.write(pdf)
    monkeypatch.setattr(module, 'api_key', lambda: 'fixture')
    monkeypatch.setattr(module, 'firecrawl_post', lambda *a, **kw: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {'success': True, 'data': {'markdown': 'Synthetic label page'}},
        firecrawl_operation_id='fixture-operation'))
    row = module.parse_page(pdf.getvalue(), 2, 'https://www.accessdata.fda.gov/label.pdf', 'fda_label', tmp_path / 'page2.json')
    assert row['original_page'] == 2 and row['ledger_operation_id'] == 'fixture-operation'
    assert row['source_url'].endswith('/label.pdf')
