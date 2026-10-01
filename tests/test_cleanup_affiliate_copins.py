import hashlib
import re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def test_relationship_captures_only_live_filter_pin_edit():
    path=ROOT/'ops/hermes-cron-scripts/glp_relationship_hygiene_daily.sh'
    raw=path.read_bytes()
    assert hashlib.sha256(raw).hexdigest()=='f64276e4c789527cfa7b23deb6d4de64c905a482c5af74397134757dc8a5e4c7'
    reverted=raw.replace(b'5ea9f39132b6475809d1616a09d4cb859af22ad38d0b5332047fcf4a2b155b6e',b'95507fb3505cbae8e37775bcf04c628effabde640a617e9807b5ee794e6593dd')
    assert hashlib.sha256(reverted).hexdigest()=='353e382af382dd7535aab4cba9a65fd875d0ca3c367473b12ca13e4719649491'
    assert sha(ROOT/'scripts/affiliate_niche_geo_filter.py')=='5ea9f39132b6475809d1616a09d4cb859af22ad38d0b5332047fcf4a2b155b6e'

def test_reconciliation_refreshes_only_two_reviewed_copins():
    raw=(ROOT/'ops/hermes-cron-scripts/glp_affiliate_reconciliation_honesty.sh').read_bytes()
    pairs=[('wire_writer','scripts/affiliate_sheet_batch_writer.py','f2fb762ae12930de69bd5be1d6c85806f8d192f32e48d00cfe1526d07903eb8c'),
           ('wire_metadata','scripts/affiliate_provider_metadata.py','b41ccfc51bc6d9def1466e6d37d9a326010b3ab16dc35c2a7ab6275bad0c20c4')]
    for variable,rel,old in pairs:
        pin=re.search(rb'^'+variable.encode()+rb'_expected_sha256=[\"\x27]([0-9a-f]{64})',raw,re.M).group(1).decode()
        assert pin==sha(ROOT/rel)
        raw=raw.replace(pin.encode(),old.encode())
    assert hashlib.sha256(raw).hexdigest()=='119fdf2c23d748888cf29e6449e4e7a149826141213e944e38ea8a1e122b0e6d'
