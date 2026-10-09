"""Bounded PDF typography repair. Raw quoted evidence and hashes never change."""
import json
from pathlib import Path


def normalize_zepbound_display(packet: Path):
    ledger_path = packet / '01-evidence-ledger.json'
    ledger = json.loads(ledger_path.read_text())
    changes = 0
    for source in ledger.get('sources', []):
        for quote in source.get('quotes', []):
            display = quote.get('rendered_quote', '')
            if 'ZEPBOUNDtreated' in display:
                quote['rendered_quote'] = display.replace('ZEPBOUNDtreated', 'ZEPBOUND-treated')
                quote['render_normalization'] = 'PDF line breaks folded to spaces; missing hyphen in ZEPBOUNDtreated restored for display only. Raw quote and quote_sha256 retained.'
                changes += 1
    if changes:
        ledger_path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + '\n')
    for name in ('04-draft.md', '05-claim-source-map.json'):
        path = packet / name
        raw = path.read_text()
        if 'ZEPBOUNDtreated' in raw:
            path.write_text(raw.replace('ZEPBOUNDtreated', 'ZEPBOUND-treated'))
    return changes
