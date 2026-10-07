"""Pure copy/evidence helpers for existing weekly SEO owners; no I/O or scheduler."""
from html.parser import HTMLParser
from html import escape, unescape
import hashlib
import json
import re
import math


def metric_number(value):
    """Missing, malformed, nonfinite and negative observations remain unknown."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def permission_held(reg):
    identity = ' '.join(str(reg.get(k) or '') for k in ('program_id', 'display_name', 'review_path'))
    profile = reg.get('profiles', {}).get('glp-clinic-scout', {})
    return ('fella' in identity.lower() or reg.get('commercial_state') != 'live'
            or profile.get('surfaces', {}).get('review') is not True
            or profile.get('wired') is False or profile.get('vertical_fit') is False)


def copy_changed(page, copy):
    return bool(copy and any(page.get(k) != copy[k] for k in ('title', 'h1', 'meta')))


def canonical_page(path, page):
    expected = 'https://' + HOST + str(path)
    return bool(page and page.get('canonical') == [expected]
                and page.get('capture', {}).get('final_url', expected) == expected)
from urllib.parse import urlsplit

HOST = 'glpclinicscout.com'

def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()

def object_digest(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')))

def normalize_query(value):
    return ' '.join(str(value).lower().split())

def public_path(url):
    if not isinstance(url, str): return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in ('https', 'http') or parsed.netloc != HOST or parsed.query or parsed.fragment:
        return None
    return parsed.path or '/'

class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.active = None
        self.depth = 0
        self.items = {'title': [], 'h1': [], 'h2': []}
        self.meta = []
        self.robots = []
        self.canonical = []
        self.links = []
        self.text = []
        self.skip = 0
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style'): self.skip += 1
        if self.active: self.depth += 1
        elif tag in self.items:
            self.active = tag
            self.depth = 1
            self.items[tag].append('')
        if tag == 'meta' and attrs.get('name', '').lower() == 'description': self.meta.append(attrs.get('content', ''))
        if tag == 'meta' and attrs.get('name', '').lower() == 'robots': self.robots.append(attrs.get('content', ''))
        if tag == 'link' and attrs.get('rel') == 'canonical': self.canonical.append(attrs.get('href', ''))
        if tag == 'a': self.links.append(attrs.get('href', ''))
    def handle_endtag(self, tag):
        if tag in ('script', 'style'): self.skip = max(0, self.skip - 1)
        if self.active and tag == self.active:
            self.active = None
    def handle_data(self, data):
        if self.active: self.items[self.active][-1] += data
        if not self.skip: self.text.append(data)

def inspect_html(html):
    p = PageParser()
    p.feed(html)
    return {'title': p.items['title'][0].strip() if len(p.items['title']) == 1 else None,
            'h1': p.items['h1'][0].strip() if len(p.items['h1']) == 1 else None,
            'meta': p.meta[0] if len(p.meta) == 1 else None,
            'h2': p.items['h2'], 'robots': p.robots, 'canonical': p.canonical,
            'links_sha256': object_digest(p.links), 'text': ' '.join(p.text),
            'cardinality': {'title': len(p.items['title']), 'h1': len(p.items['h1']), 'meta': len(p.meta)}}

def intent(query):
    q = normalize_query(query)
    if any(w in q for w in ('review', 'legit', 'trustpilot')): return 'commercial_review'
    if any(w in q for w in ('price', 'cost', 'cheap', 'discount', 'coupon')): return 'commercial_cost'
    if any(w in q for w in (' vs ', 'best ', 'compare', 'alternative')): return 'commercial_comparison'
    if any(w in q for w in ('side effect', 'dose', 'lawsuit', 'safety')): return 'informational_sensitive'
    return 'navigational_or_informational_unknown'

def url_intent_score(query, path, page):
    # Lexical diagnostic, not a SERP judgment, conversion probability or medical fact.
    words = set(re.findall(r'[a-z0-9]+', normalize_query(query))) - {'a', 'the', 'in', 'for', 'and', 'of'}
    target = set(re.findall(r'[a-z0-9]+', (path + ' ' + str(page.get('title', '')) + ' ' + str(page.get('h1', ''))).lower()))
    overlap = len(words & target) / len(words) if words else 0
    kind = intent(query)
    fit = 1 if ((kind == 'commercial_review' and path.startswith('/reviews/')) or (kind == 'commercial_comparison' and path in ('/providers/', '/reviews/'))) else 0
    return {'score': round(80 * overlap + 20 * fit, 2), 'formula': '80*unique_query_token_overlap + 20*explicit_review_or_directory_route_fit', 'token_overlap': overlap, 'intent': kind, 'not_a_measured_ranking_factor': True}

def proposed_copy(path, current, display_name=None):
    if path == '/providers/':
        return {'title': 'Compare GLP-1 Providers: Costs & Program Details | Clinic Scout', 'h1': 'Compare GLP-1 providers', 'meta': 'Compare GLP-1 providers by published costs, program details and available evidence. Review what is included and confirm current terms with each provider.'}
    if path == '/reviews/':
        return {'title': 'GLP-1 Provider Reviews: Pricing & Terms | Clinic Scout', 'h1': 'GLP-1 provider reviews', 'h1_text_segments': ['GLP-1 provider ', 'reviews', ''], 'meta': 'Read source-cited GLP-1 provider reviews. Compare published pricing, service terms and evidence gaps before choosing which provider to research.'}
    subject = display_name or current.get('h1') or ''
    subject = re.split(r'\s+(?:review|reviews)\b', subject, flags=re.I)[0].strip(' :—–-')
    if not subject or len(subject) > 65: return None
    if path.startswith('/reviews/'):
        return {'title': f'{subject} Review: Pricing & Terms | Clinic Scout', 'h1': f'{subject} review: pricing and terms', 'meta': f'Considering {subject}? Review published pricing, program terms and documented evidence gaps. Confirm current costs and eligibility directly with the provider.'}
    if path.startswith('/pharmacies/') and path != '/pharmacies/':
        subject = re.sub(r'\s*[|:].*$', '', subject).strip()
        return {'title': f'{subject}: Pharmacy Evidence | Clinic Scout', 'h1': f'{subject}: pharmacy evidence', 'meta': f'Review the available source evidence for {subject}, including documented provider relationships and information that still needs verification.'}
    return None

def patch_html(html, before_sha256, copy):
    """CAS-protected post-build candidate transform. Links/robots stay byte-equivalent in meaning."""
    if digest(html) != before_sha256: raise ValueError('stale_candidate')
    before = inspect_html(html)
    if before['cardinality'] != {'title': 1, 'h1': 1, 'meta': 1}: raise ValueError('ambiguous_metadata')
    if not {'title', 'h1', 'meta'} <= set(copy) or set(copy) - {'title', 'h1', 'meta', 'h1_text_segments'} or any(not isinstance(copy[k], str) or not copy[k].strip() for k in ('title', 'h1', 'meta')): raise ValueError('invalid_copy')
    out, n = re.subn(r'(<title\b[^>]*>)(.*?)(</title>)', lambda m: m[1] + escape(copy['title']) + m[3], html, flags=re.I | re.S)
    if n != 1: raise ValueError('title_cardinality')
    h1 = re.search(r'(<h1\b[^>]*>)(.*?)(</h1>)', out, re.I | re.S)
    replacement = escape(copy['h1'])
    if re.search(r'<[^>]+>', h1[2]):
        segments = copy.get('h1_text_segments')
        pieces = re.split(r'(<[^>]+>)', h1[2])
        slots = [i for i, value in enumerate(pieces) if value and not value.startswith('<')]
        if not isinstance(segments, list) or any(not isinstance(v, str) for v in segments) or len(segments) != len(slots) or ''.join(segments) != copy['h1']:
            raise ValueError('structured_h1_requires_source_patch')
        for i, value in zip(slots, segments): pieces[i] = escape(value)
        replacement = ''.join(pieces)
    out = out[:h1.start(2)] + replacement + out[h1.end(2):]
    count = 0
    def meta_replace(m):
        nonlocal count
        tag = m[0]
        if not re.search(r'\bname\s*=\s*[\"\']description[\"\']', tag, re.I): return tag
        count += 1
        new, changes = re.subn(r'(\bcontent\s*=\s*)([\"\'])(.*?)\2', lambda c: c[1] + '"' + escape(copy['meta'], quote=True) + '"', tag, flags=re.I | re.S)
        if changes != 1: raise ValueError('meta_content_cardinality')
        return new
    out = re.sub(r'<meta\b[^>]*>', meta_replace, out, flags=re.I | re.S)
    if count != 1: raise ValueError('meta_cardinality')
    # Update only exact old copy strings inside JSON-LD; do not touch facts/entities/links.
    mapping = {before['title']: copy['title'], before['meta']: copy['meta'], before['h1']: copy['h1']}
    def ld_replace(m):
        try: doc = json.loads(m[2])
        except json.JSONDecodeError: raise ValueError('invalid_jsonld')
        def walk(v):
            if isinstance(v, dict): return {k: mapping.get(x, x) if k in ('headline', 'name', 'description') and isinstance(x, str) else walk(x) for k, x in v.items()}
            if isinstance(v, list): return [walk(x) for x in v]
            return v
        return m[1] + json.dumps(walk(doc), ensure_ascii=False).replace('</', '<\\/') + m[3]
    out = re.sub(r'(<script\b[^>]*type=[\"\']application/ld\+json[\"\'][^>]*>)(.*?)(</script>)', ld_replace, out, flags=re.I | re.S)
    after = inspect_html(out)
    if any(after[k] != before[k] for k in ('links_sha256', 'robots', 'canonical', 'cardinality')): raise ValueError('protected_surface_changed')
    if any(after[k] != copy[k] for k in ('title', 'h1', 'meta')): raise ValueError('copy_readback_failed')
    return out
