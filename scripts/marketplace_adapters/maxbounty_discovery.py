"""Bounded read-only MaxBounty UI search/category discovery.

Legacy Q40 helpers retain Bookmarked UNION Approved scope; the S15 entry uses
the observed UI transport below. Neither proves hidden global inventory.
Browse is only a navigation bridge. Labels and synthetic test routes are not
proof of the current portal UI. Missing explicit terminal evidence fails closed.
"""
from datetime import datetime, timezone
import re
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, unquote

SCHEMA = 'glp.maxbounty.bookmarked-enumeration.v1'
COVERAGE_SCHEMA = 'glp.maxbounty.discovery-coverage.v1'
HOST = 'affiliates.maxbounty.com'


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def safe_url(url):
    if not isinstance(url, str) or re.search(r'[\\%\s\x00-\x1f]', url):
        return False
    parsed = urlparse(url)
    return (parsed.scheme == 'https' and parsed.netloc == HOST
            and not parsed.username and not parsed.password and not parsed.fragment
            and not re.search(r'track|click|redirect|logout|signout|apply|delete|mint|build', parsed.path, re.I)
            and (not parsed.query or re.fullmatch(r'page=[0-9]+', parsed.query)))


def campaign_id(url):
    parsed = urlparse(url)
    match = re.fullmatch(r'/campaign/([0-9]+)', parsed.path)
    # Only the observed first-party detail route, never /track or arbitrary id=.
    return match.group(1) if safe_url(url) and match and not parsed.query else ''


def collect(observe, navigate, capture, *, previous_approved_ids=None,
            baseline_sources=None, max_pages=20, max_campaigns=2000):
    started = now()
    if previous_approved_ids is None or not baseline_sources:
        return {'schema': SCHEMA, 'status': 'INCOMPLETE', 'captured_at_utc': now(),
                'complete': False, 'programs': [], 'enumerated': 0, 'discovered_links': 0,
                'blockers': [{'error': 'maxbounty_previous_approval_baseline_missing'}]}
    if (type(max_pages) is not int or not 1 <= max_pages <= 50
            or type(max_campaigns) is not int or not 1 <= max_campaigns <= 2000
            or any(not isinstance(pid, str) or not re.fullmatch('[0-9]+', pid)
                   for pid in previous_approved_ids)):
        raise ValueError('maxbounty_discovery_budget_or_identity_invalid')
    blockers, navigation, scopes = [], [], {}
    links, duplicate_links = {}, 0
    targets, ambiguous = {}, set()
    def observe_nav():
        page = observe()
        if not safe_url(page['url']):
            raise ValueError('maxbounty_discovery_origin_invalid')
        navigation.append({'url': page['url'], 'observed_at_utc': now(),
                           'links': [row for row in page.get('links', [])
                                     if row.get('text') in ('Bookmarked', 'Approved', 'Browse')
                                     and safe_url(row.get('href', ''))]})
        for row in page.get('links', []):
            label, href = row.get('text', ''), row.get('href', '')
            if label in ('Bookmarked', 'Approved', 'Browse') and safe_url(href):
                if label in targets and targets[label] != href:
                    ambiguous.add(label)
                targets.setdefault(label, href)
        return page
    observe_nav()
    if ('Bookmarked' not in targets or 'Approved' not in targets) and 'Browse' in targets and 'Browse' not in ambiguous:
        navigate(targets['Browse'])
        observe_nav()
    for label in ('Bookmarked', 'Approved'):
        if label in ambiguous:
            blockers.append({'error': 'maxbounty_navigation_ambiguous:' + label})
            continue
        if label not in targets:
            blockers.append({'error': 'maxbounty_' + label.lower() + '_scope_missing'})
            continue
        navigate(targets[label])
        pages, scope_ids, terminal = [], set(), False
        visited = set()
        for number in range(max_pages):
            page = observe_nav()
            if page['url'] in visited:
                blockers.append({'error': 'maxbounty_pagination_cycle:' + label})
                break
            visited.add(page['url'])
            page_ids, raw_ids = set(), []
            for row in page.get('links', []):
                pid = campaign_id(row.get('href', ''))
                if not pid:
                    continue
                page_ids.add(pid)
                raw_ids.append(pid)
                if pid in links:
                    duplicate_links += 1
                else:
                    links[pid] = {'program_id': pid, 'name': row.get('text', ''), 'detail_url': row['href']}
            scope_ids.update(page_ids)
            pages.append({'url': page['url'], 'observed_at_utc': now(), 'program_ids': sorted(page_ids), 'raw_campaign_ids': raw_ids, 'terminal': page.get('terminal') is True})
            if len(links) > max_campaigns:
                blockers.append({'error': 'maxbounty_campaign_budget_exhausted'})
                break
            if page.get('terminal') is True:
                if page.get('next_href'):
                    blockers.append({'error': 'maxbounty_pagination_conflicting_terminal:' + label})
                    break
                terminal = True
                break
            nxt = page.get('next_href')
            if not nxt or not safe_url(nxt):
                blockers.append({'error': 'maxbounty_pagination_terminal_unproven:' + label})
                break
            if number + 1 == max_pages:
                blockers.append({'error': 'maxbounty_pagination_exhausted:' + label})
                break
            navigate(nxt)
        scopes[label.lower()] = {'url': targets[label], 'pages': pages, 'terminal': terminal, 'program_ids': sorted(scope_ids)}
    previous = sorted(set(previous_approved_ids or []))
    missing = sorted(set(previous) - set(links))
    blockers.extend({'error': 'campaign_absent_from_current_enumeration:' + pid, 'program_id': pid} for pid in missing)
    programs = []
    for pid, row in sorted(links.items())[:max_campaigns]:
        try:
            program = capture(row)
            if program.get('program_id') != pid:
                raise ValueError('campaign_detail_identity_mismatch:' + pid)
            programs.append(program)
        except Exception as exc:
            blockers.append({'error': 'maxbounty_campaign_capture_failed:' + pid, 'exception_type': type(exc).__name__})
    complete = not blockers and len(programs) == len(links)
    return {'schema': SCHEMA, 'status': 'PASS' if complete else 'INCOMPLETE',
            'captured_at_utc': now(), 'started_at_utc': started,
            'bookmarked_url': targets.get('Bookmarked'), 'complete': complete,
            'enumerated': len(programs), 'discovered_links': len(links),
            'programs': programs, 'blockers': blockers,
            'coverage': {'schema': COVERAGE_SCHEMA, 'scope': 'bookmarked+approved',
                         'scopes': scopes, 'navigation': navigation,
                         'program_ids': sorted(links), 'duplicate_links': duplicate_links,
                         'previous_approved_ids': previous, 'missing_previous_approved_ids': missing,
                         'baseline_sources': baseline_sources or [],
                         'max_pages_per_scope': max_pages, 'max_campaigns': max_campaigns},
            'effects': {'provider_mutations': 0, 'tracking_links_minted': 0,
                        'applications_submitted': 0, 'tracking_clicks': 0}}


def require_coverage(data):
    """Reject legacy self-counts and recompute the declared bounded union."""
    coverage = data.get('coverage') or {}
    if coverage.get('schema') != COVERAGE_SCHEMA or coverage.get('scope') != 'bookmarked+approved':
        raise ValueError('maxbounty_enumeration_scope_unproven')
    for blocker in data.get('blockers') or []:
        reason = str(blocker.get('error', ''))
        if reason.startswith('campaign_absent_from_current_enumeration:'):
            raise ValueError(reason)
    if data.get('status') != 'PASS' or data.get('complete') is not True or data.get('blockers'):
        raise ValueError('maxbounty_enumeration_incomplete')
    union, raw_count = set(), 0
    for label in ('bookmarked', 'approved'):
        scope = coverage.get('scopes', {}).get(label, {})
        pages = scope.get('pages', [])
        if not pages or scope.get('terminal') is not True or pages[-1].get('terminal') is not True:
            raise ValueError('maxbounty_pagination_terminal_unproven:' + label)
        if len(pages) > coverage.get('max_pages_per_scope', 0):
            raise ValueError('maxbounty_pagination_exhausted:' + label)
        page_ids = set()
        urls = []
        for page in pages:
            if not safe_url(page.get('url', '')):
                raise ValueError('maxbounty_discovery_origin_invalid')
            urls.append(page['url'])
            raw_ids = page.get('raw_campaign_ids')
            if not isinstance(raw_ids, list) or sorted(set(raw_ids)) != page.get('program_ids'):
                raise ValueError('maxbounty_page_duplicate_evidence_missing')
            raw_count += len(raw_ids)
            page_ids.update(page.get('program_ids', []))
        if len(urls) != len(set(urls)):
            raise ValueError('maxbounty_pagination_cycle:' + label)
        if sorted(page_ids) != scope.get('program_ids'):
            raise ValueError('maxbounty_scope_id_disagreement:' + label)
        union.update(page_ids)
    if sorted(union) != coverage.get('program_ids'):
        raise ValueError('maxbounty_union_id_disagreement')
    if raw_count - len(union) != coverage.get('duplicate_links'):
        raise ValueError('maxbounty_duplicate_count_disagreement')
    programs = data.get('programs', [])
    ids = [p.get('program_id') for p in programs]
    if any(not isinstance(pid, str) or not re.fullmatch('[0-9]+', pid) for pid in ids):
        raise ValueError('maxbounty_campaign_identity_invalid')
    if len(ids) != len(set(ids)) or set(ids) != union:
        raise ValueError('maxbounty_campaign_identity_disagreement')
    if (type(data.get('enumerated')) is not int or type(data.get('discovered_links')) is not int
            or len(ids) != data['enumerated'] or len(ids) != data['discovered_links']):
        raise ValueError('maxbounty_enumeration_count_disagreement')
    if not coverage.get('baseline_sources') or not isinstance(coverage.get('previous_approved_ids'), list):
        raise ValueError('maxbounty_previous_approval_baseline_missing')
    missing = sorted(set(coverage['previous_approved_ids']) - union)
    if missing:
        raise ValueError('campaign_absent_from_current_enumeration:' + ','.join(missing))
    if coverage.get('missing_previous_approved_ids') != []:
        raise ValueError('maxbounty_previous_approval_gap_disagreement')
    return coverage


def safe_search_url(url):
    """Only first-party observed search form query fields; no auth/link URLs."""
    if not isinstance(url,str) or re.search(r'[\\\\\s\x00-\x1f]',url):
        return False
    p=urlparse(url)
    if p.scheme!='https' or p.netloc!=HOST or p.path!='/search' or p.fragment:
        return False
    pairs=parse_qsl(p.query,keep_blank_values=True)
    q=dict(pairs)
    if len(q)!=len(pairs):
        return False
    traffic={'ep','cp','bp','sp','ip','lp','np','mp','bbp'}
    off={'ff','rf','ar'}
    zeros={'tstrict','grstrict'}
    blank={'gr','ct','prp','cpn','dl'}
    if set(q)-(traffic|off|zeros|blank|{'st','ob','m','c','pn','ad','as','af'}):
        return False
    if q.get('af','false') not in ('true','false'):
        return False
    if 'as' in q and q['as'] not in ('true','false'):
        return False
    if any(q[k]!='true' for k in traffic & q.keys()) or any(q[k]!='false' for k in off & q.keys()):
        return False
    if any(q[k]!='0' for k in zeros & q.keys()) or any(q[k]!='' for k in blank & q.keys()):
        return False
    if any(not re.fullmatch('[0-9]+',q[k]) for k in {'m','c','pn'} & q.keys()):
        return False
    return (q.get('ob','date')=='date' and q.get('ad','undefined')=='undefined' and len(q.get('st',''))<=200
            and not re.search(r'[%\\\\\x00-\x1f]',unquote(q.get('st',''))))


def project_ui_snapshot(snapshot, scope, *, source, query_response=None):
    """Project parent's authenticated table/pager receipts, never sidebar links.

    Grounding: S15/scopes/category-health/page-1..7.json and search-glp-probe.
    Published category count remains independent even when traversal disagrees.
    """
    url=snapshot.get('url','')
    if not safe_search_url(url):
        raise ValueError('search_url_unsafe')
    q=dict(parse_qsl(urlparse(url).query,keep_blank_values=True))
    expected_term=scope['value'] if scope['kind']=='search' else ''
    expected_category=scope['value'] if scope['kind']=='category' else '0'
    if (q.get('st','')!=expected_term or q.get('m','0')!=expected_category or q.get('c','0')!='0'
            or q.get('af','false')!=('true' if scope['kind']=='approved' else 'false')):
        raise ValueError('search_scope_mismatch')
    number=snapshot['page_number']
    numbers=snapshot.get('visible_page_numbers',[])
    active=snapshot.get('active_page_numbers',[])
    if type(number) is not int or str(number)!=q.get('pn','1'):
        raise ValueError('search_page_number_mismatch')
    if numbers:
        last=max(numbers)
        if sorted(set(numbers))!=list(range(1,last+1)) or sorted(set(active))!=[number]:
            raise ValueError('search_pager_state_unproven')
        basis='numeric_active_last'
    else:
        if number!=1 or active:
            raise ValueError('search_single_page_state_unproven')
        last=1
        basis='single_page_no_numeric_pager'
    total=None
    label=snapshot.get('category_label','')
    if scope['kind']=='category':
        match=re.search(r'\(([0-9,]+)\)\s*$',label)
        if match:
            total=int(match.group(1).replace(',',''))
    rows=[{'program_id':r['program_id'],'name':r['name'],'detail_url':r['href'],
           'listing_text':r.get('listing_text','')} for r in snapshot['rows']]
    if not rows and snapshot.get('empty_marker_present') is not True:
        raise ValueError('search_transitional_empty_dom')
    badge_total=total
    response_evidence=None
    if query_response is not None:
        matches=query_response.get('matchingCampaigns')
        if query_response.get('success') is not True or not isinstance(matches,list):
            raise ValueError('search_response_invalid')
        api_ids=[str(r.get('campaign_id','')) for r in matches]
        if sorted(api_ids)!=sorted(r['program_id'] for r in rows):
            raise ValueError('search_response_dom_identity_mismatch')
        totals={str(r.get('total_rows','')) for r in matches}
        if matches and (len(totals)!=1 or not re.fullmatch('[0-9]+',next(iter(totals)))):
            raise ValueError('search_response_total_unproven')
        total=int(next(iter(totals))) if matches else None
        if (str(query_response.get('total_pages'))!=str(last)
                and not (not matches and last==1 and query_response.get('total_pages') in (None,0,'0'))):
            raise ValueError('search_response_pager_mismatch')
        response_evidence={'program_ids':api_ids,'reported_total':total,'total_pages':query_response.get('total_pages'),
            'canonical_payload_sha256':hashlib.sha256(json.dumps(query_response,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            'source_class':'naturally_observed_UI_response'}
    return {'scope_key':scope['key'],'page_key':str(number),'rows':rows,
            'total':total,'terminal':number==last,'next_cursor':str(number+1) if number<last else None,
            'evidence':{'source':source,'observed_at_utc':snapshot.get('observed_at_utc'),
                'url':url,'category_label':label,'total_text':str(total) if total is not None else None,
                'published_badge_total':badge_total,'query_response':response_evidence,
                'badge_query_disagreement':bool(query_response is not None and badge_total is not None and badge_total!=total),
                'terminal_basis':basis,'pagination':{'current':number,'last':last,'page_size':50,'basis':basis}}}


class UIListingTransport:
    """Observed read-only form/pager transport. Never issues an API request.

    Receipts exclude body/sidebar, auth payloads, headers and response extras.
    One instance owns a sequential scope traversal, not a shared browser tab.
    """
    def __init__(self, driver, *, evidence_dir):
        from pathlib import Path
        self.driver, self.page = driver, driver.page
        self.directory = Path(evidence_dir)
        self.scope = None
        self.number = 0
        self.label = ''

    @staticmethod
    def search_response(response, scope, number):
        from urllib.parse import parse_qs
        parsed = urlparse(response.url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        return (parsed.scheme == 'https' and parsed.netloc == 'api.maxbounty.com'
                and parsed.path == '/affiliates/api/search'
                and response.request.method == 'GET' and 'q' not in query
                and query.get('st') == [scope['value'] if scope['kind']=='search' else '']
                and query.get('m') == [scope['value'] if scope['kind']=='category' else '0']
                and query.get('af',['false']) == ['true' if scope['kind']=='approved' else 'false']
                and query.get('pn') == [str(number)])

    def __call__(self, scope, cursor):
        import uuid
        from datetime import datetime, timezone
        page = self.page
        number = int(cursor or '1')
        if cursor is None:
            self.driver.goto('https://' + HOST + '/search')
            page.locator('input[name=st]').wait_for(timeout=15000)
            if page.locator('input[type=password]').count():
                raise ValueError('login_expired_mid_scope')
            if not page.locator('select[name=m]').is_visible():
                page.get_by_role('button', name='Search Options', exact=True).dispatch_event('click')
            page.locator('select[name=m]').select_option(scope['value'] if scope['kind']=='category' else '0')
            # Capture the badge BEFORE Enter: provider resets the selected label.
            self.label = page.locator('select[name=m] option:checked').inner_text()
            page.locator('select[name=c]').select_option('0')
            for name in ['af','ff','rf','ar']:
                page.locator('input[name='+name+']').set_checked(name=='af' and scope['kind']=='approved', force=True)
            for name in ['ep','cp','bp','sp','ip','lp','np','mp','bbp']:
                page.locator('input[name='+name+']').set_checked(True, force=True)
            for name,value in [('trafficstrict','0'),('gr',''),('grstrict','0'),('ct',''),('prp',''),('cpn',''),('deeplink','')]:
                page.locator('select[name='+name+']').select_option(value)
            page.locator('input[name=st]').fill(scope['value'] if scope['kind']=='search' else '')
            with page.expect_response(lambda r: self.search_response(r,scope,number), timeout=15000) as event:
                page.locator('input[name=st]').press('Enter')
            self.scope = dict(scope)
        else:
            if self.scope != scope or number != self.number + 1:
                raise ValueError('ui_cursor_out_of_sequence')
            with page.expect_response(lambda r: self.search_response(r,scope,number), timeout=15000) as event:
                page.locator('button.page-link').filter(has_text=re.compile('^'+str(number)+'$')).first.click(force=True)
        response = event.value
        if response.status != 200:
            raise ValueError('ui_search_http_failure')
        data = response.json()
        if data.get('success') is not True or not isinstance(data.get('matchingCampaigns'),list):
            raise ValueError('ui_search_response_failed')
        expected = [str(row['campaign_id']) for row in data['matchingCampaigns']]
        page.wait_for_function(r"expected=>JSON.stringify(Array.from(document.querySelectorAll('table tbody a[href]')).filter(a=>/\/campaign\/\d+$/.test(a.href)).map(a=>a.href.split('/').pop()))===JSON.stringify(expected)", arg=expected, timeout=15000)
        page.wait_for_function("n=>new URL(location.href).searchParams.get('pn')===String(n)", arg=number, timeout=15000)
        if page.locator('input[type=password]').count():
            raise ValueError('login_expired_mid_scope')
        buttons = page.locator('button.page-link').evaluate_all(r"els=>els.filter(e=>/^\d+$/.test(e.innerText.trim())).map(e=>({number:Number(e.innerText.trim()),active:e.parentElement.classList.contains('active'),disabled:e.disabled}))")
        raw_rows = page.locator('table tbody tr').evaluate_all(r"els=>els.map(e=>({text:e.innerText,cells:Array.from(e.querySelectorAll('td')).map(x=>x.innerText.trim()),links:Array.from(e.querySelectorAll('a[href]')).filter(a=>/^https:\/\/affiliates.maxbounty.com\/campaign\/\d+$/.test(a.href)).map(a=>({name:a.innerText.trim(),href:a.href,program_id:a.href.split('/').pop()}))}))")
        rows = [{**link,'listing_text':row['text'],'cells':row['cells']} for row in raw_rows for link in row['links']]
        if [r['program_id'] for r in rows] != expected:
            raise ValueError('ui_response_identity_mismatch')
        empty = not rows and bool(re.search(r'no campaigns|no results|nothing found|no records',page.locator('body').inner_text(),re.I))
        snapshot = {'url':page.url,'page_number':number,'category_label':self.label,
            'observed_at_utc':datetime.now(timezone.utc).isoformat(),
            'visible_page_numbers':[b['number'] for b in buttons],
            'active_page_numbers':[b['number'] for b in buttons if b['active']],
            'empty_marker_present':empty,'rows':rows}
        # Projection hashes the actual response in memory; persist only allowlisted fields.
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / ('page-'+uuid.uuid4().hex+'.json')
        projected = project_ui_snapshot(snapshot,scope,source=str(target),query_response=data)
        allowed = ('campaign_id','name','status','rate','rate_type','rate_text','total_rows')
        safe_response = {'success':data['success'],'total_pages':data.get('total_pages'),
            'matchingCampaigns':[{k:r[k] for k in allowed if k in r} for r in data['matchingCampaigns']]}
        receipt = {'scope':scope,'snapshot':snapshot,'query_response':safe_response,
            'raw_response_canonical_sha256':projected['evidence']['query_response']['canonical_payload_sha256']}
        with target.open('x', encoding='utf-8') as handle:
            json.dump(receipt,handle,sort_keys=True,indent=2)
        target.chmod(0o400)
        self.number = number
        return projected


def validate_retired404(item, pid, account_context, capture_started_at):
    """Exact current-run account404 evidence, never a removed baseline ID."""
    if item.get('program_id')!=pid or item.get('disposition')!='RETIRED_404':
        raise ValueError('retirement_identity_invalid')
    evidence=item.get('evidence') or {}
    path=Path(evidence.get('path','')).resolve()
    root=Path(account_context['receipts_dir']).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size>100000:
        raise ValueError('retirement_receipt_missing_or_outside_run')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=evidence.get('sha256'):
        raise ValueError('retirement_receipt_hash_invalid')
    data=json.loads(raw)
    response=data.get('response') or {}
    if (data.get('schema')!='glp.maxbounty.retired404.v1' or data.get('program_id')!=pid
            or data.get('disposition')!='RETIRED_404' or data.get('account_context')!=account_context
            or account_context.get('authenticated') is not True
            or data.get('detail_url')!='https://'+HOST+'/campaign/'+pid
            or response.get('url')!='https://api.maxbounty.com/affiliates/api/campaign/'+pid
            or response.get('method')!='GET' or type(response.get('status')) is not int or response['status']!=404
            or not re.fullmatch('[0-9a-f]{64}',str(response.get('body_sha256','')))
            or data.get('classification')!='terminal_provider_unavailable'):
        raise ValueError('retirement_account_response_invalid')
    observed=datetime.fromisoformat(str(data.get('observed_at_utc')).replace('Z','+00:00'))
    started=datetime.fromisoformat(str(capture_started_at).replace('Z','+00:00'))
    if observed.tzinfo is None or started.tzinfo is None or not started<=observed<=datetime.now(timezone.utc):
        raise ValueError('retirement_timestamp_outside_current_run')
    return data


def collect_scopes(fetch, capture, *, scopes, previous_approved_ids=None,
                   baseline_sources=None, direct_recheck=None, max_pages=50,
                   max_campaigns=2000, account_context=None, capture_started_at=None):
    """Read-only injected search/category page transport; no invented UI routes.

    Completeness refers ONLY to supplied search terms/categories, never the
    marketplace. Each fetch must project a scoped result table (not sidebar).
    """
    blockers, proofs, links = [], {}, {}
    account_scope=any(s.get('kind')=='approved' for s in (scopes or []))
    capture_started_at=capture_started_at or now()
    if account_scope and previous_approved_ids is not None and baseline_sources and (not isinstance(account_context,dict)
            or account_context.get('authenticated') is not True
            or not re.fullmatch(r'[^\s@]+@[^\s@]+',str(account_context.get('account','')))
            or account_context.get('portal')!='https://'+HOST
            or not account_context.get('run_id') or not account_context.get('receipts_dir')):
        return {'schema':SCHEMA,'status':'INCOMPLETE','complete':False,'enumerated':None,
                'discovered_links':None,'programs':[], 'blockers':[{'error':'authenticated_account_context_missing'}]}
    if (type(max_pages) is not int or not 1<=max_pages<=1000
            or type(max_campaigns) is not int or not 1<=max_campaigns<=50000):
        raise ValueError('discovery_budget_invalid')
    if previous_approved_ids is None or not baseline_sources:
        return {'schema':SCHEMA,'status':'INCOMPLETE','complete':False,'enumerated':None,
                'discovered_links':None,'programs':[],
                'blockers':[{'error':'previous_approval_baseline_missing'}]}
    if (not isinstance(scopes,list) or not scopes
            or not {'search','category'} <= {s.get('kind') for s in scopes}
            or any(s.get('kind') not in ('search','category','catalogue','approved') or not s.get('key') or not isinstance(s.get('value'),str) for s in scopes)
            or len({s['key'] for s in scopes}) != len(scopes)):
        return {'schema':SCHEMA,'status':'INCOMPLETE','complete':False,'enumerated':None,
                'discovered_links':None,'programs':[], 'blockers':[{'error':'scope_contract_invalid'}]}
    raw_count = 0
    for scope in scopes:
        cursor, visited, pages, ids = None, set(), [], set()
        while True:
            try:
                page = fetch(scope, cursor)
            except Exception as exc:
                blockers.append({'error':'scope_fetch_failed','scope':scope['key'],
                                 'exception_type':type(exc).__name__})
                break
            if not isinstance(page.get('page_key'),str) or not page['page_key']:
                blockers.append({'error':'page_key_invalid','scope':scope['key']})
                break
            if page.get('page_key') in visited:
                blockers.append({'error':'pagination_cycle','scope':scope['key']})
                break
            visited.add(page.get('page_key'))
            page_ids = []
            for row in page['rows']:
                pid = row.get('program_id')
                if (not isinstance(pid,str) or not re.fullmatch('[0-9]+',pid)
                        or campaign_id(row.get('detail_url','')) != pid):
                    blockers.append({'error':'identity_invalid','scope':scope['key']})
                    continue
                page_ids.append(pid)
                links.setdefault(pid, row)
            raw_count += len(page_ids)
            if len(links) > max_campaigns:
                blockers.append({'error':'campaign_budget_exhausted','scope':scope['key']})
                break
            if set(page_ids) & ids:
                blockers.append({'error':'repeated_page_identity','scope':scope['key']})
            ids.update(page_ids)
            if page.get('scope_key') != scope['key']:
                blockers.append({'error':'scope_mismatch','scope':scope['key']})
            if not page.get('evidence'):
                blockers.append({'error':'evidence_missing','scope':scope['key']})
            if scope['kind']=='approved':
                ev=page.get('evidence') or {};q=dict(parse_qsl(urlparse(ev.get('url','')).query,keep_blank_values=True))
                response=ev.get('query_response') or {}
                if (not safe_search_url(ev.get('url','')) or q.get('af')!='true'
                        or q.get('st','')!='' or q.get('m','0')!='0' or q.get('c','0')!='0'
                        or response.get('source_class')!='naturally_observed_UI_response'
                        or response.get('program_ids')!=page_ids or response.get('reported_total')!=page.get('total')):
                    blockers.append({'error':'approved_account_scope_unproven','scope':scope['key']})
            total = page.get('total')
            pagination=(page.get('evidence') or {}).get('pagination',{})
            numbered = (all(type(pagination.get(k)) is int for k in ('current','last','page_size'))
                and 1 <= pagination['current'] <= pagination['last'] <= max_pages
                and 1 <= pagination['page_size'] <= 1000
                and pagination.get('basis') in ('numeric_active_last','single_page_no_numeric_pager'))
            if numbered:
                n,last,size=(pagination[k] for k in ('current','last','page_size'))
                if (str(n)!=page['page_key'] or n!=len(pages)+1
                        or page.get('terminal') is not (n==last)
                        or (n<last and page.get('next_cursor')!=str(n+1))
                        or (pages and (pages[0].get('evidence') or {}).get('pagination',{}).get('last')!=last)
                        or (pagination['basis']=='single_page_no_numeric_pager' and last!=1)):
                    blockers.append({'error':'pagination_state_mismatch','scope':scope['key']})
                if (len(page_ids)!=len(set(page_ids)) or len(page_ids)>size
                        or (n<last and len(page_ids)!=size)
                        or (n>1 and not page_ids)):
                    blockers.append({'error':'pagination_row_count_mismatch','scope':scope['key']})
            if (type(total) is not int or total < 0) and not (total is None and numbered):
                blockers.append({'error':'denominator_unproven','scope':scope['key']})
            if pages and total != pages[0].get('total'):
                blockers.append({'error':'denominator_changed','scope':scope['key']})
            pages.append({**page, 'rows':[r for r in page['rows'] if campaign_id(r.get('detail_url',''))==r.get('program_id') and r.get('program_id') in page_ids],
                          'program_ids':sorted(set(page_ids)), 'raw_campaign_ids':page_ids})
            if page['terminal']:
                if page.get('next_cursor') is not None:
                    blockers.append({'error':'terminal_conflict','scope':scope['key']})
                if type(total) is int and len(ids) != total:
                    blockers.append({'error':'count_mismatch','scope':scope['key']})
                break
            if page.get('next_cursor') is None:
                blockers.append({'error':'terminal_unproven','scope':scope['key']})
                break
            if len(pages) >= max_pages:
                blockers.append({'error':'page_budget_exhausted','scope':scope['key']})
                break
            cursor = page['next_cursor']
        reported_total=pages[0].get('total') if pages else None
        traversal_complete=bool(pages and pages[-1].get('terminal') is True and pages[-1].get('next_cursor') is None
            and not any(b.get('scope')==scope['key'] and b['error'] not in ('count_mismatch','denominator_changed') for b in blockers))
        proofs[scope['key']] = {'definition':scope, 'pages':pages, 'program_ids':sorted(ids),
            'traversal_complete':traversal_complete,'reported_total':reported_total,
            'denominator_basis':('provider_reported_total' if type(reported_total) is int else
                'observed_pagination_exhaustion' if traversal_complete else 'unproven')}
    missing = sorted(set(previous_approved_ids or []) - set(links))
    rechecks, retired = [], []
    for pid in missing:
        item = {'program_id':pid,'disposition':'unknown','reason':'direct_recheck_unavailable'}
        if direct_recheck:
            try:
                observed = direct_recheck(pid)
                if (observed.get('program_id') != pid or not observed.get('evidence')
                        or observed.get('disposition') not in ('available','unavailable','unknown','RETIRED_404')):
                    raise ValueError('direct_recheck_identity_or_evidence_invalid')
                if observed.get('disposition')=='RETIRED_404':
                    if not account_scope:
                        raise ValueError('retirement_requires_approved_account_scope')
                    validate_retired404(observed,pid,account_context,capture_started_at)
                    retired.append(observed)
                item = observed
            except Exception as exc:
                item = {'program_id':pid,'disposition':'unknown','reason':'direct_recheck_failed',
                        'exception_type':type(exc).__name__}
        rechecks.append(item)
        if item.get('disposition')!='RETIRED_404':
            blockers.append({'error':'campaign_absent_from_current_enumeration:'+pid,'program_id':pid})
    programs = []
    for pid,row in sorted(links.items()):
        try:
            program = capture(row)
            if program.get('program_id') != pid:
                raise ValueError('detail_identity_mismatch')
        except Exception as exc:
            program = {**row,'relationship':'unknown','vertical_fit':None,
                       'capture_status':'unknown','capture_error_type':type(exc).__name__}
            blockers.append({'error':'campaign_capture_failed','program_id':pid,
                             'exception_type':type(exc).__name__})
        program.setdefault('vertical_fit',None)
        if re.search(r'\bfella\b',str(program.get('name','')),re.I):
            program['eligibility']='permanently_excluded'
        programs.append(program)
    known_count = len(programs) if programs or not blockers else None
    return {'schema':SCHEMA,'status':'INCOMPLETE' if blockers else 'COMPLETE' if account_scope else 'PASS','complete':not blockers,'captured_at_utc':now(),
            'enumerated':known_count,'discovered_links':len(links) if links or not blockers else None,'programs':programs,'blockers':blockers,
            'coverage':{'schema':'glp.maxbounty.account-catalog-coverage.v2' if account_scope else 'glp.maxbounty.search-category-coverage.v1',
                'scope':'declared-search+categories','global_complete':False,'global_denominator':None,
                'scopes':proofs,'program_ids':sorted(links),'duplicate_links':raw_count-len(links),
                'missing_previous_approved_ids':missing,'direct_rechecks':rechecks,
                'previous_approved_ids':previous_approved_ids,'baseline_sources':baseline_sources,
                **({'account_context':account_context,'capture_started_at':capture_started_at,
                    'retired_404':retired,'unresolved_previous_approved_ids':sorted(set(missing)-{r['program_id'] for r in retired})} if account_scope else {})},
            'effects':{'provider_mutations':0,'tracking_links_minted':0,'applications_submitted':0,'tracking_clicks':0}}


def require_search_coverage(data):
    """Replay page witnesses and exact local retirement files; no network I/O."""
    coverage = data.get('coverage') or {}
    account_scope=coverage.get('schema')=='glp.maxbounty.account-catalog-coverage.v2'
    if (coverage.get('schema') not in ('glp.maxbounty.search-category-coverage.v1','glp.maxbounty.account-catalog-coverage.v2')
            or coverage.get('scope') != 'declared-search+categories'
            or coverage.get('global_complete') is not False
            or coverage.get('global_denominator') is not None
            or data.get('complete') is not True or data.get('status') != ('COMPLETE' if account_scope else 'PASS')
            or data.get('blockers')):
        raise ValueError('search_category_scope_unproven')
    scope_proofs = coverage.get('scopes',{})
    indices = {key:0 for key in scope_proofs}
    def fetch(scope,cursor):
        key=scope['key']
        index=indices[key]
        indices[key]+=1
        page=scope_proofs[key]['pages'][index]
        if index and scope_proofs[key]['pages'][index-1]['next_cursor'] != cursor:
            raise ValueError('pagination_chain_invalid')
        return page
    programs = {row['program_id']:row for row in data.get('programs',[])}
    rechecks={r['program_id']:r for r in coverage.get('direct_rechecks',[])}
    replay = collect_scopes(fetch,lambda row:programs[row['program_id']],
        scopes=[p['definition'] for p in scope_proofs.values()],
        previous_approved_ids=coverage.get('previous_approved_ids'),
        baseline_sources=coverage.get('baseline_sources'),
        **({'direct_recheck':lambda pid:rechecks[pid], 'account_context':coverage.get('account_context'),
            'capture_started_at':coverage.get('capture_started_at')} if account_scope else {}))
    if (not replay['complete'] or replay['coverage'] != coverage
            or replay['enumerated'] != data.get('enumerated')
            or replay['discovered_links'] != data.get('discovered_links')
            or len(programs) != len(data.get('programs',[]))
            or sorted(programs) != coverage.get('program_ids')):
        raise ValueError('search_category_proof_disagreement')
    return coverage


def approval_baseline(paths, sheet_ids, sheet_source, max_captures=1000):
    """Union prior approvals; never use latest-only or infer approval from count.

    Inputs are local authenticated captures and a fresh governed Sheet projection.
    Historical approvals establish a continuity obligation, not current approval.
    Every path/hash/timestamp is retained; corrupt/oversized history blocks.
    """
    paths = sorted(set(Path(p) for p in paths))
    if not paths or len(paths) > max_captures or not sheet_source:
        raise ValueError('maxbounty_previous_approval_history_missing_or_exhausted')
    ids = set(sheet_ids)
    sources = [{**sheet_source, 'program_ids': sorted(ids)}]
    for path in paths:
        if path.stat().st_size > 20_000_000:
            raise ValueError('maxbounty_history_capture_oversized')
        raw = path.read_bytes()
        data = json.loads(raw)
        if data.get('schema') != SCHEMA:
            raise ValueError('maxbounty_history_schema_invalid:' + str(path))
        timestamp = data.get('captured_at_utc')
        # Failed login receipts may contain no capture timestamp or rows; they
        # are never approval evidence, but must not erase older approvals.
        approved = set()
        for program in data.get('programs', []):
            if program.get('relationship') == 'approved':
                pid = program.get('program_id')
                if not isinstance(pid, str) or not re.fullmatch('[0-9]+', pid):
                    raise ValueError('maxbounty_history_campaign_identity_invalid')
                if program.get('relationship_button') != 'Build tracking link':
                    raise ValueError('maxbounty_history_approval_evidence_missing:' + pid)
                approved.add(pid)
        if approved:
            captured = datetime.fromisoformat(str(timestamp).replace('Z', '+00:00'))
            if captured.tzinfo is None:
                raise ValueError('maxbounty_history_timestamp_timezone_missing')
        sources.append({'source': str(path), 'sha256': hashlib.sha256(raw).hexdigest(),
                        'captured_at_utc': timestamp, 'program_ids': sorted(approved)})
        ids.update(approved)
    if any(not isinstance(pid, str) or not re.fullmatch('[0-9]+', pid) for pid in ids):
        raise ValueError('maxbounty_sheet_campaign_identity_invalid')
    return {'previous_approved_ids': sorted(ids), 'baseline_sources': sources}
