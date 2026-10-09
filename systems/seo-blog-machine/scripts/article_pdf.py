#!/usr/bin/env python3
"""Parse original PDF pages and retain source page citations."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit
import requests
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))
from firecrawl_client import api_key, firecrawl_post


def parse_page(pdf, page, source_url, kind, output):
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(io.BytesIO(pdf))
    if not 1 <= page <= len(reader.pages):
        raise ValueError('pdf_page_out_of_range')
    writer = PdfWriter()
    writer.add_page(reader.pages[page - 1])
    upload = io.BytesIO()
    writer.write(upload)
    response = firecrawl_post('https://api.firecrawl.dev/v2/parse', caller='seo.article_pdf',
        headers={'Authorization': 'Bearer ' + api_key()},
        data={'options': json.dumps({'formats': ['markdown']})},
        files={'file': ('page.pdf', upload.getvalue(), 'application/pdf')}, timeout=180)
    response.raise_for_status()
    payload = response.json()
    markdown = (payload.get('data') or {}).get('markdown', '')
    if not payload.get('success') or not markdown.strip():
        raise ValueError('pdf_parse_empty')
    result = {'source_url': source_url, 'kind': kind, 'original_page': page,
        'source_pdf_sha256': hashlib.sha256(pdf).hexdigest(),
        'markdown': markdown, 'receipt': payload,
        'ledger_operation_id': response.firecrawl_operation_id,
        'status': 'PAGE_SOURCE_REQUIRES_CLAIM_REVIEW'}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x') as handle:
        json.dump(result, handle, indent=2)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--pages', required=True)
    parser.add_argument('--kind', choices=['fda_label', 'trial'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    host = urlsplit(args.url).hostname or ''
    allowed = ('accessdata.fda.gov', 'www.accessdata.fda.gov', 'www.novo-pi.com', 'pi.lilly.com',
               'pmc.ncbi.nlm.nih.gov', 'clinicaltrials.gov', 'cdn.clinicaltrials.gov')
    if host not in allowed:
        raise ValueError('primary_pdf_source_required')
    pages = sorted(set(int(value) for value in args.pages.split(',')))
    if not 1 <= len(pages) <= 4:
        raise ValueError('one_to_four_pages_required')
    response = requests.get(args.url, timeout=60)
    response.raise_for_status()
    if not response.content.startswith(b'%PDF'):
        raise ValueError('source_is_not_pdf')
    rows = [parse_page(response.content, page, args.url, args.kind, args.output / f'page-{page}.json') for page in pages]
    manifest = {'schema': 'article-pdf-page-citations-v1', 'source_url': args.url, 'kind': args.kind,
                'citations': [{'source_url': row['source_url'], 'original_page': row['original_page'],
                               'capture': str(args.output / f"page-{row['original_page']}.json"),
                               'ledger_operation_id': row['ledger_operation_id']} for row in rows]}
    with (args.output / 'pdf-manifest.json').open('x') as handle:
        json.dump(manifest, handle, indent=2)
    print(json.dumps({'status': 'PASS', 'manifest': str(args.output / 'pdf-manifest.json'), 'pages': pages}))


if __name__ == '__main__':
    main()
