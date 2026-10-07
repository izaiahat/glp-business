#!/usr/bin/env python3
"""Firecrawl-backed email-ready clinic target refill for GLP Clinic Scout.

Safe defaults:
- never sends email;
- only queues publicly displayed business emails;
- excludes patient/results/privacy/security/legal/HR/careers/support-style inboxes;
- writes targets/messages/source ledgers + SQLite consistently;
- stays quiet when used by the wrapper and queue health is already sufficient.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Canonical enterprise copy/suppression modules live beside this script.
sys.path.insert(0, '/home/ubuntu/business/glp-clinic-partner-outreach-machine')
from outreach_copy import make_messages_for_target
from outreach_suppression import is_suppressed

ROOT = Path('/home/ubuntu/business/glp-clinic-partner-outreach-machine')
DB = ROOT / 'outreach.sqlite'
REPORT_DIR = Path('/home/ubuntu/business/reports/glp-clinic-partner-supply-acceleration-2026-06-16')
REPORT_DIR.mkdir(parents=True, exist_ok=True)
LOCK_PATH = ROOT / '.refill.lock'
PT = ZoneInfo('America/Los_Angeles')
EMAIL_RE = re.compile(r'(?i)(?<![\w.+-])([a-z0-9][a-z0-9._%+\-]{0,63}@[a-z0-9][a-z0-9.\-]{1,253}\.[a-z]{2,})(?![\w+-])')
FORBIDDEN_PREFIXES = {
    'security', 'privacy', 'abuse', 'legal', 'hr', 'careers', 'career', 'jobs', 'job',
    'billing', 'payments', 'patient', 'patients', 'results', 'records', 'medicalrecords',
    'medical-records', 'appointment', 'appointments', 'support', 'help', 'noreply', 'no-reply',
    'recruitment', 'recruiting', 'editor', 'editors', 'news', 'media', 'press', 'pr', 'dmca', 'feedback'
}
BLOCKED_DOMAINS = (
    'facebook.', 'instagram.', 'tiktok.', 'x.com', 'twitter.', 'linkedin.', 'youtube.',
    'yelp.', 'healthgrades.', 'zocdoc.', 'realself.', 'google.', 'mapquest.',
    'bbb.org', 'chamber', 'directory', 'business.', 'dnb.com', 'zoominfo.',
    'signalhire.', 'apollo.', 'rocketreach.', 'wikipedia.', 'medium.com', 'issuu.',
    'scribd.', 'slideshare.', 'indeed.', 'glassdoor.', 'square.site', 'linktr.ee',
    'everydayhealth.com', 'prunderground.com', 'renewbariatrics.com', 'atriumhealth.org', 'tfhc.org', 'rmc.md',
    'semaglutidenearme.org', 'glp1clinics.org', 'cakesmashmedia.com', 'peptideleads.com',
    'promisepharmacy.com', 'klinic.com', 'lifemediagrp.com', 'gagnebert-evolution.com',
    'photomart.ca', 'chuooo.com', 'norco.top', 'medestheticsmag.', 'texterity.com'
)
PUBLIC_EMAIL_DOMAINS = {'gmail.com', 'yahoo.com', 'outlook.com', 'hotmail.com', 'icloud.com', 'me.com', 'aol.com'}
SERVICE_TERMS = [
    'glp-1', 'glp 1', 'glp1', 'semaglutide', 'tirzepatide', 'medical weight loss',
    'weight loss management', 'weight management', 'weight-loss', 'obesity medicine',
    'medspa', 'medical spa', 'wellness clinic'
]
CORE_SERVICE_TERMS = ('glp', 'semaglutide', 'tirzepatide', 'medical weight loss', 'weight loss', 'weight-loss', 'weight management', 'obesity')
GENERIC_NAME_RE = re.compile(r'(?i)^(home|contact|contact us|top of page|blog|search|medical weight loss|weight loss|weightloss|semaglutide|tirzepatide|prescription weight loss|weight management|services?|doctor-guided programs|medical weight loss that is affordable|google accounts|duration:?|part\s+\d+|treatment comparison|surprise|providence|functional medicine|medical support for weight loss|medical glp-?1 program|med spa|med spa services|medical weight loss injections|in-person medical weight loss|weight loss with prescription weight loss medications|wegovy is here!?|#?1\s+medical weight loss.*)$')
LOCATION_ONLY_NAME_RE = re.compile(r'(?i)^[a-z .\-]+,\s*(al|ak|az|ar|ca|co|ct|de|fl|ga|id|il|in|ia|ks|ky|la|me|ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy)$')
STATE_ONLY_NAME_RE = re.compile(r'(?i)^(alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming)$')
NON_CLINIC_SOURCE_RE = re.compile(r'(?i)(everydayhealth\.com|prunderground\.com|renewbariatrics\.com|phlp\.org|peptideleads\.com|promisepharmacy\.com|klinic\.com|lifemediagrp\.com|gagnebert-evolution\.com|photomart\.ca|chuooo\.com|norco\.top|medestheticsmag|texterity\.com|editor@|dmca@|public media|biosupply trends|news article|magazine|quarterly|press release|industry:\s*healthcare|category/industry|category/local-guides|local guide|publisher|publication|blog\s*\||\bblog\b|medicaid coverage|policy update|legal aid|health law project|provider directory|providers? near you|find .*providers? near you|local providers|research peptide|direct from lab|postal customer|customer service|news & events)')
LARGE_SYSTEM_RE = re.compile(r'(?i)(\bhospital\b|\bhealth system\b|health centers?\b|ohiohealth|erlanger|atrium health|tampa family health|regional medical|sutter health|north memorial health|providence\b)')
WEAK_NAME_RE = re.compile(r'(?i)(\bcall\b|book now|powerful weight loss benefits|we look forward|your weight loss goal is our mission|primary care practice|urgent care|imaging centers|medical weight loss treatment$|medical weight loss treatment plans?$|medical weight loss programs?\b|medical weight loss injections$|weight management programs?\b|weight loss with prescription|semaglutide for fda-approved|^a medical weight loss program built around you$|^expert medical weight loss services$|medicaid ends adult coverage|^wellness clinic$|^glp1\s*&\s*meds$|^orange county,?\s+newport beach$)')
TARGET_STATES = [
    'Texas', 'Florida', 'Georgia', 'North Carolina', 'South Carolina', 'Tennessee', 'Ohio',
    'Pennsylvania', 'New York', 'New Jersey', 'Massachusetts', 'Virginia', 'Maryland',
    'Illinois', 'Michigan', 'Minnesota', 'Missouri', 'Kansas', 'Oklahoma', 'Wisconsin',
    'California', 'Arizona', 'Colorado', 'Nevada', 'Utah', 'Oregon', 'Washington', 'Idaho',
]
CITY_QUERIES = [
    'Austin Texas', 'Dallas Texas', 'Houston Texas', 'Fort Worth Texas', 'San Antonio Texas',
    'Miami Florida', 'Tampa Florida', 'Orlando Florida', 'Jacksonville Florida', 'Fort Lauderdale Florida',
    'Atlanta Georgia', 'Savannah Georgia', 'Charlotte North Carolina', 'Raleigh North Carolina', 'Nashville Tennessee',
    'Columbus Ohio', 'Cincinnati Ohio', 'Philadelphia Pennsylvania', 'Pittsburgh Pennsylvania',
    'New York New York', 'Long Island New York', 'Hoboken New Jersey', 'Boston Massachusetts',
    'Richmond Virginia', 'Bethesda Maryland', 'Chicago Illinois', 'Naperville Illinois',
    'Detroit Michigan', 'Minneapolis Minnesota', 'Kansas City Missouri', 'Overland Park Kansas',
    'Phoenix Arizona', 'Scottsdale Arizona', 'Mesa Arizona', 'Gilbert Arizona', 'Tucson Arizona',
    'Denver Colorado', 'Boulder Colorado', 'Thornton Colorado', 'Colorado Springs Colorado', 'Fort Collins Colorado',
    'Sacramento California', 'San Diego California', 'Orange County California', 'Newport Beach California', 'Palm Springs California', 'Redding California',
    'Portland Oregon', 'Bend Oregon', 'Eugene Oregon', 'Medford Oregon', 'Salem Oregon',
    'Seattle Washington', 'Bellevue Washington', 'Spokane Washington', 'Tacoma Washington', 'Vancouver Washington',
    'Boise Idaho', 'Meridian Idaho', 'Coeur d Alene Idaho', 'Reno Nevada', 'Las Vegas Nevada', 'Henderson Nevada',
    'Salt Lake City Utah', 'Draper Utah', 'St George Utah', 'American Fork Utah',
]
SEARCH_EXCLUDE_SUFFIX = ' -site:everydayhealth.com -site:prunderground.com -site:yelp.com -site:healthgrades.com -site:zocdoc.com -site:facebook.com -site:instagram.com -site:google.com -site:mypeptidematch.com'

QUERY_PATTERNS = []
# Email-specific searches run first because the weekday sender needs actual
# email-ready inventory, not contact-form-only clinics. They still promote rows
# only after the official-site source gate below passes.
for city in CITY_QUERIES:
    QUERY_PATTERNS.extend([
        f'"{city}" "semaglutide" "info@" clinic{SEARCH_EXCLUDE_SUFFIX}',
        f'"{city}" "tirzepatide" "info@" "medical weight loss"{SEARCH_EXCLUDE_SUFFIX}',
        f'"{city}" "GLP-1" "contact@" "med spa"{SEARCH_EXCLUDE_SUFFIX}',
        f'"{city}" "semaglutide" "hello@" medspa{SEARCH_EXCLUDE_SUFFIX}',
        f'"{city}" "semaglutide" "admin@" clinic{SEARCH_EXCLUDE_SUFFIX}',
    ])
# High-cadence refill needs real clinic supply. Put high-yield city/service/contact
# searches first; older exact-email state searches were too restrictive and often
# exhausted max_queries before reaching city-level official clinic sites.
for city in CITY_QUERIES:
    QUERY_PATTERNS.extend([
        f'{city} semaglutide med spa weight loss clinic contact{SEARCH_EXCLUDE_SUFFIX}',
        f'{city} tirzepatide medical weight loss clinic contact{SEARCH_EXCLUDE_SUFFIX}',
        f'{city} GLP-1 weight loss wellness clinic contact{SEARCH_EXCLUDE_SUFFIX}',
        f'{city} medical weight loss semaglutide contact{SEARCH_EXCLUDE_SUFFIX}',
    ])
for state in TARGET_STATES:
    QUERY_PATTERNS.extend([
        f'{state} semaglutide med spa weight loss clinic contact{SEARCH_EXCLUDE_SUFFIX}',
        f'{state} tirzepatide medical weight loss clinic contact{SEARCH_EXCLUDE_SUFFIX}',
        f'{state} GLP-1 weight loss wellness clinic contact{SEARCH_EXCLUDE_SUFFIX}',
        f'{state} medical weight loss semaglutide contact{SEARCH_EXCLUDE_SUFFIX}',
        f'{state} "semaglutide" "info@" clinic{SEARCH_EXCLUDE_SUFFIX}',
        f'{state} "tirzepatide" "contact@" medspa{SEARCH_EXCLUDE_SUFFIX}',
    ])

KNOWN_REPAIRS = {
    'medical_weight_loss_in_university_place_wa_in_university_place_wa': ('University Place Medical Clinic', 'University Place, WA'),
    'semaglutide_tirzepatide_weight_loss_st_george_ut': ('Potere Health MD', 'St. George, UT'),
    'weight_loss_boise_id': ('Spa 35', 'Boise, ID'),
    'home_sparkmobilemedspa_com': ('Spark Functional Medicine & Med Spa', 'North Idaho'),
    'revolution_health_clinic_in_henderson_nv': ('Revolution Health', 'Henderson, NV'),
    'semaglutide_injections_utah_american_fork_ut': ('Beautiful U Med Spa', 'American Fork, UT'),
    'advanced_medical_weightloss_advancedmedicalweightloss_com': ('Advanced Medical Weightloss', ''),
    'semaglutide_tirzepatide_in_pleasant_grove_medical_weight_loss_in_draper_ut': ('Magnifique Medical Spa', 'Pleasant Grove / Draper, UT'),
    'contact_arizona': ('Arizona Health & Wellness Clinic', 'Gilbert, AZ'),
    'trusted_doctors_in_sun_city_az_weight_loss_clinic_in_sun_city_az': ('Sonoran MD', 'Sun City, AZ'),
    'semaglutide_for_weight_loss_colorado_optimal_health_north_washington_st_thornton': ('Colorado Optimal Health North', 'Thornton, CO'),
    'medical_weight_loss_windsor_co': ('NOCO Healthcare', 'Windsor, CO'),
    'prescription_weight_loss_denver_co': ('The Luxe Room', 'Denver / Boulder / Fort Collins, CO'),
    'newport_beach_semaglutide_glp_1_clinic_medical_grade_weight_loss_newport_beach_c': ('Newport Center Urgent Care', 'Newport Beach, CA'),
    'weight_loss_for_portland_or_wellness_serving_portland_or': ('Bridge City Medical', 'Portland, OR'),
    'the_skinny_on_semaglutide_for_weight_management_albany_or': ('OnePeak Medical', 'Albany, OR'),
    'faces_in_bloom_semaglutide_eugene_or': ('Faces in Bloom', 'Eugene, OR'),
    'tirzepatide_in_seattle_at_well_medical_arts_wellmedicalarts_com': ('Well Medical Arts', 'Seattle, WA'),
    'weight_loss_solutions_washington': ('Mahala Aesthetics', 'Washington'),
    'glp_1_medical_weight_loss_salt_lake_city_ut_weight_loss_in_salt_lake_city_ut': ('Saltz Plastic Surgery', 'Salt Lake City, UT'),
    'concierge_medical_weight_loss_treatment_medical_weight_loss_in_bend_or': ('My Bloom Aesthetics', 'Bend, OR'),
    'iow_bellevue_wa_iow_bellevue_wa': ('IOW Medicine', 'Bellevue, WA'),
    'pnw_wellness_weight_loss_l_1560_biddle_rd_suite_a_medford_or_medford_or': ('PNW Wellness & Weight Loss', 'Medford, OR'),
    'rogue_revitalize_beauty_bar_in_medford_or': ('Rogue Revitalize', 'Medford, OR'),
    'sand_plastic_surgery_weight_loss_programs_in_spokane_wa': ('Sand Plastic Surgery', 'Spokane, WA'),
    'tacoma_wa_tacoma_wa': ("Mary's Access Clinic", 'Tacoma, WA'),
    'skip_to_content_https_skinhavenclinic_com_medical_weight_loss_in_seattle_wa_content_seattle_wa': ('SkinHaven Clinic', 'Seattle, WA'),
    'top_of_page_semaglutide_injections_reno_nv': ('Avionce Aesthetics', 'Reno, NV'),
    'utah_county_s_leading_medical_weight_loss_center_utah': ('Luxx Medical Spa', 'Utah County, UT'),
    'vida_aesthetic_medicine_salem_oregon_salem_or': ('VIDA Aesthetic Medicine', 'Salem, OR'),
    'path_to_wellness_your_holistic_health_partner_ptwhealth_com': ('Path to Wellness Integrated Health', 'Fort Worth, TX'),
    'welcome_to_age_better_miami_agebettermiami_com': ('Age Better Miami', 'North Miami, FL'),
    'james_e_hardy_m_d_plastic_surgery_jacksonville_jameshardymd_com': ('James E. Hardy M.D. Plastic Surgery', 'Jacksonville, FL'),
    'medical_spa_skin_care_botox_more_om_med_spa_located_in_boise_id': ('OM Med Spa', 'Boise, ID'),
    'general_and_bariatric_surgeon_optimalwlsc_com': ('Optimal Weight Loss and Surgery Consultants', 'Dallas, TX'),
    'aesthetics_advancedwellnessandrejuvenation_com': ('Advanced Wellness and Rejuvenation', 'Corpus Christi / Portland, TX'),
    'draper_utah_draper_ut': ('Wellness and Neuro Center', 'Draper, UT'),
    'get_draper_ut': ('RR Wellness Clinic', 'Draper, UT'),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def slug(value: str) -> str:
    out = re.sub(r'[^a-z0-9]+', '_', (value or '').lower()).strip('_')
    return out[:96] or hashlib.sha256((value or utc_now()).encode()).hexdigest()[:16]


def clean_email(value: str) -> str:
    e = (value or '').strip().strip('.,;:<>[](){}"\'').lower()
    e = urllib.parse.unquote(e).replace(' ', '')
    return e


def domain_of(url_or_email: str) -> str:
    if '@' in (url_or_email or '') and not url_or_email.startswith('http'):
        return url_or_email.split('@', 1)[1].lower().removeprefix('www.')
    try:
        return urllib.parse.urlparse(url_or_email).netloc.lower().removeprefix('www.')
    except Exception:
        return ''


def root_url(url: str) -> str:
    p = urllib.parse.urlparse(url)
    return f'{p.scheme}://{p.netloc}/'


def env_value(key: str) -> str:
    if os.environ.get(key):
        return os.environ[key]
    for env_path in (Path('/home/ubuntu/.hermes/.env'), Path('/home/ubuntu/business/.env')):
        if not env_path.exists():
            continue
        for raw in env_path.read_text(errors='ignore').splitlines():
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    return ''


def bounded_call(fn, *args, timeout_seconds: int = 25, **kwargs):
    """Fail closed around Firecrawl SDK calls that can hang past API timeouts."""
    def _timeout(_signum, _frame):
        raise TimeoutError(f'bounded_call_timeout_{timeout_seconds}s')
    old_handler = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(timeout_seconds)
    try:
        return fn(*args, **kwargs)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def email_ok(email_addr: str) -> bool:
    e = clean_email(email_addr)
    if not e or '@' not in e:
        return False
    local, dom = e.split('@', 1)
    if local in FORBIDDEN_PREFIXES:
        return False
    if any(local.startswith(p + sep) for p in FORBIDDEN_PREFIXES for sep in ('.', '-', '_')):
        return False
    if re.match(r'^\d+(info|sales|hello|contact|admin|staff|office|support)$', local):
        return False
    if 'foundation' in local or local.startswith('donor'):
        return False
    if any(bad in dom for bad in ('example.com', 'domain.com', 'schema.org', 'wixpress.com', 'wordpress.com', 'sentry.io', 'godaddy.com', 'email.com')):
        return False
    if e.endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg', '.js', '.css')):
        return False
    return True


def email_matches_official_site(email_addr: str, official_url: str, page_text: str) -> bool:
    email_domain = domain_of(email_addr)
    site_domain = domain_of(official_url)
    if not email_domain or not site_domain:
        return False
    if email_domain == site_domain or email_domain.endswith('.' + site_domain) or site_domain.endswith('.' + email_domain):
        return True
    if email_domain in PUBLIC_EMAIL_DOMAINS:
        return False
    return False


def blocked_result_url(url: str) -> bool:
    d = domain_of(url)
    if not d:
        return True
    if urllib.parse.urlparse(url).path.lower().endswith('.pdf'):
        return True
    return any(bad in d for bad in BLOCKED_DOMAINS)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(errors='ignore').splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(''.join(json.dumps(r, ensure_ascii=False, sort_keys=True) + '\n' for r in rows), encoding='utf-8')


def backup_once(label: str) -> Path:
    ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    dest = Path('/home/ubuntu/business/backups') / f'glp-clinic-partner-supply-acceleration-{ts}-{label}'
    dest.mkdir(parents=True, exist_ok=True)
    for name in ['outreach.sqlite', 'targets.jsonl', 'message_ledger.jsonl', 'source_ledger.jsonl', 'send_ledger.jsonl', 'reply_ledger.jsonl', 'suppression_list.jsonl', 'README.md']:
        src = ROOT / name
        if src.exists():
            shutil.copy2(src, dest / name)
    return dest


def unsent_day0_count() -> int:
    con = sqlite3.connect(DB)
    try:
        row = con.execute("""
            select count(*)
            from messages m
            join targets t on t.target_id = m.target_id
            left join sends s on s.message_id = m.message_id and s.status = 'sent'
            where m.channel = 'email'
              and m.step = 'day0_first_touch'
              and t.wave = 'GLP Follow-Up Wave 1'
              and m.send_status = 'draft_not_sent'
              and coalesce(nullif(m.to_email, ''), t.public_email, '') != ''
              and s.message_id is null
        """).fetchone()
        return int(row[0] or 0)
    finally:
        con.close()


def counts_by_wave() -> list[tuple[str, int, int]]:
    con = sqlite3.connect(DB)
    try:
        return con.execute("""
            select t.wave, count(*), sum(case when s.message_id is null then 1 else 0 end)
            from messages m
            join targets t on t.target_id = m.target_id
            left join sends s on s.message_id = m.message_id and s.status = 'sent'
            where m.channel='email' and m.step='day0_first_touch'
            group by t.wave
            order by t.wave
        """).fetchall()
    finally:
        con.close()


def existing_sets() -> dict[str, set[str]]:
    targets = read_jsonl(ROOT / 'targets.jsonl')
    sends = read_jsonl(ROOT / 'send_ledger.jsonl')
    replies = read_jsonl(ROOT / 'reply_ledger.jsonl')
    suppressions = read_jsonl(ROOT / 'suppression_list.jsonl')
    domains, emails, names, ids, sent_tids, replied_tids, suppressed_emails = set(), set(), set(), set(), set(), set(), set()
    for t in targets:
        ids.add(t.get('target_id', ''))
        names.add(slug(t.get('clinic_name', '')))
        if t.get('public_email'):
            emails.add(clean_email(t['public_email']))
        for u in [t.get('url', ''), t.get('glp_page', '')] + list(t.get('source_urls') or []):
            d = domain_of(u)
            if d:
                domains.add(d)
    for s in sends:
        if s.get('target_id'):
            sent_tids.add(s['target_id'])
    for r in replies:
        if r.get('target_id'):
            replied_tids.add(r['target_id'])
        if r.get('from_addr'):
            suppressed_emails.add(clean_email(r['from_addr']))
    for s in suppressions:
        if s.get('email'):
            suppressed_emails.add(clean_email(s['email']))
    return {
        'domains': domains, 'emails': emails, 'names': names, 'ids': ids,
        'sent_tids': sent_tids, 'replied_tids': replied_tids, 'suppressed_emails': suppressed_emails,
    }


def metadata_of(doc) -> dict:
    md = getattr(doc, 'metadata', None)
    if not md:
        return {}
    return {k: getattr(md, k, None) for k in ('title', 'description', 'url', 'og_title', 'og_description')}


def markdown_of(doc) -> str:
    return getattr(doc, 'markdown', None) or ''


def contact_urls(url: str, markdown: str) -> list[str]:
    base = root_url(url).rstrip('/')
    urls = [url]
    for path in ('/contact', '/contact-us', '/contact/', '/contact-us/', '/about', '/about-us', '/locations', '/team'):
        urls.append(base + path)
    for m in re.finditer(r'\[[^\]]{0,100}(contact|about|location|team|office|email)[^\]]{0,100}\]\((https?://[^)\s]+)\)', markdown, re.I):
        urls.append(m.group(2).split('#')[0])
    seen, out = set(), []
    for u in urls:
        u = u.strip().rstrip(')')
        if domain_of(u) == domain_of(url) and u not in seen:
            seen.add(u); out.append(u)
    return out[:8]


def extract_city_state(text: str, url: str = '') -> str:
    state_pairs = [('Oregon','OR'),('Washington','WA'),('Idaho','ID'),('Nevada','NV'),('Utah','UT'),('Arizona','AZ'),('Colorado','CO'),('California','CA')]
    for state, abbr in state_pairs:
        m = re.search(r'([A-Z][A-Za-z .\-]{2,34}),\s*' + re.escape(abbr) + r'\b', text)
        if m:
            city = re.sub(r'\s+', ' ', m.group(1)).strip()
            if not GENERIC_NAME_RE.match(city):
                return f'{city}, {abbr}'
    lower_url = url.lower()
    for city in CITY_QUERIES:
        if city.lower().replace(' ', '-') in lower_url or city.lower().replace(' ', '') in lower_url.replace('-', ''):
            parts = city.rsplit(' ', 1)
            if len(parts) == 2:
                state = parts[1]
                abbr = dict((s, a) for s, a in state_pairs).get(state, state)
                return f'{parts[0]}, {abbr}'
    for state, abbr in state_pairs:
        if state in text[:4000]:
            return state
    return ''


def service_evidence(text: str) -> tuple[list[str], str, str]:
    lower = text.lower()
    hits = []
    for term in SERVICE_TERMS:
        if term in lower:
            hits.append(term)
    unique_hits = []
    for h in hits:
        label = {'glp 1': 'GLP-1', 'glp1': 'GLP-1', 'glp-1': 'GLP-1'}.get(h, h)
        if label not in unique_hits:
            unique_hits.append(label)
    positions = [lower.find(h) for h in SERVICE_TERMS if lower.find(h) >= 0]
    idx = min(positions) if positions else 0
    snippet = re.sub(r'\s+', ' ', text[max(0, idx-160):idx+320]).strip()
    if not snippet:
        snippet = ', '.join(unique_hits[:4])
    if any(h.lower().startswith('glp') for h in unique_hits):
        label = 'GLP-1 / medical weight-loss'
    elif any('semaglutide' in h.lower() or 'tirzepatide' in h.lower() for h in unique_hits):
        label = 'semaglutide/tirzepatide weight-loss'
    elif any('medspa' in h.lower() or 'medical spa' in h.lower() for h in unique_hits):
        label = 'medspa/wellness'
    else:
        label = 'medical weight-loss'
    return unique_hits, snippet[:500], label


def title_parts(title: str) -> list[str]:
    raw = re.split(r'\s+(?:[|–—-]|I)\s+', title or '')
    return [re.sub(r'\s+', ' ', p.strip(' |–—-')).strip() for p in raw if p.strip(' |–—-')]


def looks_like_brand(part: str) -> bool:
    p = part.lower().strip()
    if any(token in p for token in ('http://', 'https://', '](', '![', '.com/', '<base64')):
        return False
    if not p or len(p) < 3 or GENERIC_NAME_RE.match(p):
        return False
    if WEAK_NAME_RE.search(part) or re.search(r'\(\d{3}\)|\d{3}[-.\s]\d{3}[-.\s]\d{4}', part) or len(part) > 65:
        return False
    brand_words = ('clinic', 'med', 'medical', 'wellness', 'spa', 'aesthetic', 'health', 'md', 'urgent care', 'body', 'room', 'center', 'solutions', 'functional')
    service_noise = ('semaglutide', 'tirzepatide', 'weight loss', 'glp', 'prescription')
    if any(b in p for b in brand_words) and not (all(n in p for n in ('semaglutide', 'weight loss')) and len(p) > 70):
        return True
    if not any(n in p for n in service_noise) and len(p) <= 55:
        return True
    return False


def infer_name(url: str, metadata: dict, text: str) -> str:
    title = (metadata.get('title') or metadata.get('og_title') or '').strip()
    parts = title_parts(title)
    for part in reversed(parts):
        part = re.sub(r'(?i)\b(best|trusted|near me|in [a-z ,]+)$', '', part).strip()
        if looks_like_brand(part):
            return part[:90]
    for line in text.splitlines()[:80]:
        clean = re.sub(r'\s+', ' ', line.strip('#* \t')).strip()
        if looks_like_brand(clean):
            return clean[:90]
    d = domain_of(url).split('.')[0]
    return d.replace('-', ' ').title()


def make_messages(target: dict) -> list[dict]:
    """Render the enterprise Cadence A copy for refill-queued targets.

    This preserves deterministic message IDs while inheriting the global postal-address
    send gate. If the postal address is missing, rows are staged as blocked rather than
    send-ready.
    """
    return make_messages_for_target(target)


def qa_target(target: dict, messages: list[dict]) -> list[str]:
    flags = []
    if not email_ok(target.get('public_email', '')):
        flags.append('bad_or_forbidden_email')
    name = target.get('clinic_name', '').strip()
    if GENERIC_NAME_RE.match(name) or LOCATION_ONLY_NAME_RE.match(name) or STATE_ONLY_NAME_RE.match(name) or len(name) < 3:
        flags.append('generic_or_weak_clinic_name')
    if WEAK_NAME_RE.search(name) or re.search(r'\(\d{3}\)|\d{3}[-.\s]\d{3}[-.\s]\d{4}', name) or len(name) > 65:
        flags.append('generic_or_weak_clinic_name')
    parts = name.split()
    if len(parts) >= 4 and len(parts) % 2 == 0 and parts[:len(parts)//2] == parts[len(parts)//2:]:
        flags.append('generic_or_weak_clinic_name')
    name_low = name.lower()
    city_low = (target.get('city_state_service_area') or '').lower()
    if 'skip to content' in name_low or 'reel hooks' in name_low or 'base64' in name_low or name_low == city_low or name_low.startswith('[') or name_low.startswith('!'):
        flags.append('generic_or_weak_clinic_name')
    source_blob = ' '.join(str(target.get(k) or '') for k in ['target_id', 'url', 'glp_page', 'public_email', 'proof_snippet', 'why_selected', 'source_urls'])
    if NON_CLINIC_SOURCE_RE.search(source_blob):
        flags.append('non_clinic_source_or_publication')
    if domain_of(target.get('public_email', '')) in PUBLIC_EMAIL_DOMAINS:
        flags.append('generic_public_email_domain')
    if urllib.parse.urlparse(str(target.get('glp_page') or '')).path.lower().endswith('.pdf'):
        flags.append('non_clinic_source_or_publication')
    if LARGE_SYSTEM_RE.search(name + ' ' + source_blob):
        flags.append('large_health_system_not_high_fit')
    evidence = (target.get('proof_snippet', '') + ' ' + target.get('why_selected', '')).lower()
    if not any(t in evidence for t in SERVICE_TERMS):
        flags.append('weak_service_evidence')
    for m in messages:
        low = m['body'].lower()
        for bad in ('hope this finds you well', 'just checking in', 'revolutionize', 'unlock', 'game-changing', 'guarantee', 'eligible for', 'your dose'):
            if bad in low:
                flags.append('copy_bad_' + bad.replace(' ', '_'))
        if m['step'] == 'day0_first_touch' and not 70 <= m['word_count'] <= 165:
            flags.append('day0_word_count')
    return sorted(set(flags))


def rewrite_target_and_messages(targets: list[dict], messages: list[dict], target: dict) -> None:
    msg_rows = make_messages(target)
    msg_by_id = {m['message_id']: m for m in msg_rows}
    for i, row in enumerate(targets):
        if row.get('target_id') == target['target_id']:
            targets[i] = target
            break
    existing_ids = {m.get('message_id') for m in messages}
    replaced = set()
    for i, row in enumerate(messages):
        if row.get('target_id') == target['target_id'] and row.get('message_id') in msg_by_id:
            messages[i] = msg_by_id[row['message_id']]
            replaced.add(row['message_id'])
    for m in msg_rows:
        if m['message_id'] not in existing_ids and m['message_id'] not in replaced:
            messages.append(m)


def apply_known_repairs(dry_run: bool = False) -> dict:
    targets = read_jsonl(ROOT / 'targets.jsonl')
    messages = read_jsonl(ROOT / 'message_ledger.jsonl')
    sends = read_jsonl(ROOT / 'send_ledger.jsonl')
    sent_tids = {s.get('target_id') for s in sends if s.get('status') == 'sent'}
    repaired = []
    for target in targets:
        tid = target.get('target_id')
        if tid not in KNOWN_REPAIRS or tid in sent_tids:
            continue
        new_name, new_city = KNOWN_REPAIRS[tid]
        changed = False
        if target.get('clinic_name') != new_name:
            target['clinic_name'] = new_name; changed = True
        if new_city and target.get('city_state_service_area') != new_city:
            target['city_state_service_area'] = new_city; changed = True
        if changed:
            target.setdefault('repair_notes', []).append({'created_at_utc': utc_now(), 'reason': 'cleaned generic Firecrawl page-title extraction before first send'})
            target['service_label'] = target.get('service_label') or 'GLP-1 / medical weight-loss'
            rewrite_target_and_messages(targets, messages, target)
            repaired.append({'target_id': tid, 'clinic_name': new_name, 'city_state_service_area': target.get('city_state_service_area', '')})
    if repaired and not dry_run:
        write_jsonl(ROOT / 'targets.jsonl', targets)
        write_jsonl(ROOT / 'message_ledger.jsonl', messages)
        con = sqlite3.connect(DB)
        try:
            for t in targets:
                if t.get('target_id') in {r['target_id'] for r in repaired}:
                    con.execute('update targets set clinic_name=?, city_state_service_area=?, proof_snippet=?, why_selected=?, risk_notes=? where target_id=?',
                                (t.get('clinic_name',''), t.get('city_state_service_area',''), t.get('proof_snippet',''), t.get('why_selected',''), t.get('risk_notes',''), t.get('target_id')))
            for m in messages:
                if m.get('target_id') in {r['target_id'] for r in repaired}:
                    con.execute('insert or replace into messages values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                (m['message_id'], m['target_id'], m['clinic_name'], m['channel'], m['to_email'], m['contact_form_url'], m['step'], m['due_day'], m['subject'], m['body'], m['word_count'], json.dumps(m['qa_flags']), m['send_status'], m['created_at_utc']))
            con.commit()
        finally:
            con.close()
        receipt = {'created_at_utc': utc_now(), 'repaired': repaired}
        (REPORT_DIR / 'target-name-repair-receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'repaired_count': len(repaired), 'repaired': repaired}


def discover(app, target_additions: int, max_urls: int, max_queries: int, dry_run: bool, query_offset: int = 0) -> dict:
    existing = existing_sets()
    accepted, rejects, raw_results = [], [], []
    seen_domains = set(existing['domains'])
    urls = []
    result_context_by_url: dict[str, str] = {}
    seed_source_by_url: dict[str, dict] = {}
    seen_result_domains = set()
    query_slice = QUERY_PATTERNS[max(0, query_offset):]
    for query_index, query in enumerate(query_slice, start=1):
        if query_index > max_queries:
            break
        if len(urls) >= max_urls:
            break
        try:
            res = bounded_call(app.search, query, limit=6, timeout=30000, timeout_seconds=8)
            web_rows = getattr(res, 'web', None) or []
        except Exception as exc:
            raw_results.append({'query': query, 'error': type(exc).__name__})
            continue
        for row in web_rows:
            url = getattr(row, 'url', '') or ''
            if not url.startswith('http'):
                continue
            d = domain_of(url)
            title = getattr(row, 'title', '') or ''
            description = getattr(row, 'description', '') or ''
            context = ' '.join(x for x in [title, description] if x)
            raw_results.append({'query': query, 'url': url, 'title': title, 'description': description})
            result_context_by_url[url] = context
            # Directory/search-result snippets often expose the clinic's own
            # public business email even when the source result itself is a
            # directory. Treat that as a seed only: promote the email-domain
            # homepage/contact pages through the normal official-site gates.
            for seed_email in sorted(set(clean_email(e) for e in EMAIL_RE.findall(context))):
                seed_domain = domain_of(seed_email)
                seed_url = f'https://{seed_domain}/' if seed_domain else ''
                if (
                    seed_url
                    and email_ok(seed_email)
                    and seed_domain not in PUBLIC_EMAIL_DOMAINS
                    and not blocked_result_url(seed_url)
                    and seed_domain not in seen_domains
                    and seed_domain not in seen_result_domains
                ):
                    seen_result_domains.add(seed_domain)
                    urls.append(seed_url)
                    result_context_by_url[seed_url] = context
                    seed_source_by_url[seed_url] = {
                        'query': query,
                        'seed_source_url': url,
                        'seed_email_domain': seed_domain,
                        'seed_title': title,
                        'seed_description': description,
                    }
                    if len(urls) >= max_urls:
                        break
            if len(urls) >= max_urls:
                break
            if blocked_result_url(url) or d in seen_domains or d in seen_result_domains:
                continue
            seen_result_domains.add(d)
            urls.append(url)
            if len(urls) >= max_urls:
                break
    source_rows = []
    for url in urls:
        if len(accepted) >= target_additions:
            break
        d = domain_of(url)
        if blocked_result_url(url) or d in existing['domains']:
            rejects.append({'url': url, 'reason': 'blocked_or_existing_domain'}); continue
        search_context = result_context_by_url.get(url, '')
        seed_source = seed_source_by_url.get(url) or {}
        seed_only_source = False
        try:
            doc = bounded_call(app.scrape, url, formats=['markdown'], only_main_content=True, timeout=30000, timeout_seconds=12)
            text = markdown_of(doc)
            meta = metadata_of(doc)
        except Exception as exc:
            if not seed_source:
                rejects.append({'url': url, 'reason': 'scrape_error', 'error': type(exc).__name__}); continue
            text = search_context
            meta = {'title': seed_source.get('seed_title') or ''}
            seed_only_source = True
        if not text or len(text) < (80 if seed_source else 200):
            rejects.append({'url': url, 'reason': 'weak_scrape_text'}); continue
        scraped_urls = [url]
        combined_text = (search_context + '\n\n' + text).strip()
        email_source_url = url
        emails_seen = set(clean_email(e) for e in EMAIL_RE.findall(combined_text))
        # Always probe likely contact pages once; service pages often do not expose email.
        for cu in contact_urls(url, text)[1:]:
            try:
                cdoc = bounded_call(app.scrape, cu, formats=['markdown'], only_main_content=True, timeout=30000, timeout_seconds=10)
                ctext = markdown_of(cdoc)
            except Exception:
                continue
            if ctext:
                scraped_urls.append(cu)
                combined_text += '\n\n' + ctext[:8000]
                new_emails = set(clean_email(e) for e in EMAIL_RE.findall(ctext))
                if new_emails and not any(email_ok(e) for e in emails_seen):
                    email_source_url = cu
                emails_seen |= new_emails
            if any(email_ok(e) and email_matches_official_site(e, url, combined_text) for e in emails_seen):
                # Keep scraping bounded once a safe business email is found.
                break
        hits, proof, service_label = service_evidence(combined_text)
        if not hits:
            rejects.append({'url': url, 'reason': 'no_clear_service_fit', 'emails_seen': sorted(emails_seen)[:5]}); continue
        if not any(term in combined_text.lower() for term in CORE_SERVICE_TERMS):
            rejects.append({'url': url, 'reason': 'no_core_glp_or_weight_loss_fit', 'emails_seen': sorted(emails_seen)[:5]}); continue
        usable = []
        for e in sorted(emails_seen):
            suppressed, _hits = is_suppressed(email=e, domain=domain_of(e))
            if email_ok(e) and email_matches_official_site(e, url, combined_text) and e not in existing['emails'] and e not in existing['suppressed_emails'] and not suppressed:
                usable.append(e)
        if not usable:
            rejects.append({'url': url, 'reason': 'no_safe_public_email', 'emails_seen': sorted(emails_seen)[:8]}); continue
        name = infer_name(url, meta, combined_text)
        if GENERIC_NAME_RE.match(name):
            rejects.append({'url': url, 'reason': 'generic_name_after_inference', 'name': name}); continue
        city = extract_city_state(combined_text, url)
        target_id = slug(f'{name} {city or d}')
        if target_id in existing['ids'] or slug(name) in existing['names']:
            rejects.append({'url': url, 'reason': 'existing_name_or_id', 'name': name}); continue
        if target_id in existing['sent_tids'] or target_id in existing['replied_tids']:
            rejects.append({'url': url, 'reason': 'prior_send_or_reply', 'target_id': target_id}); continue
        email_addr = usable[0]
        target = {
            'target_id': target_id,
            'clinic_name': name[:90],
            'url': root_url(url),
            'glp_page': url,
            'city_state_service_area': city,
            'market_type': 'local/regional US clinic',
            'public_email': email_addr,
            'public_phone': '',
            'contact_form_url': '',
            'contact_method': 'public business email',
            'contact_safety': 'email_ready_public_business_inbox',
            'decision_maker_guess': 'clinic owner/operator, administrator, or growth/referral lead',
            'proof_snippet': proof,
            'why_selected': f'Public business email displayed on clinic-owned site and page shows {", ".join(hits[:4])} fit; likely growth-sensitive clinic with GLP or weight-loss inquiry follow-up demand.',
            'fit_score': 4.3,
            'contactability_score': 5,
            'risk_notes': 'Public displayed business email only; do not discuss patient intake, medication details, outcomes, eligibility, dosing, or clinical advice.',
            'wave': 'GLP Follow-Up Wave 1',
            'source_ids': [f'FC_AUTO_REFILL_2026_06_16_{len(accepted)+1:03d}'],
            'source_urls': list(dict.fromkeys([url, email_source_url] + scraped_urls)),
            'discovery_seed_url': seed_source.get('seed_source_url', ''),
            'email_source_url': email_source_url,
            'displayed_email': email_addr,
            'service_label': service_label,
            'personalization_facts': [f'{service_label} page', city or 'regional market', 'public email displayed on official clinic site'],
            'refill_scoring_model_version': '2026-06-28.1',
            'source_quality_class': 'official_domain_email_seed_service_directory' if seed_only_source else 'official_clinic_site_confirmed_service_and_email',
            'seed_only_source': seed_only_source,
            'source_policy': 'Search/directory results may seed official-domain emails; rows still require service-fit evidence, non-public-domain business email, sender QA, and Bouncer verification before sending',
        }
        msg_rows = make_messages(target)
        flags = qa_target(target, msg_rows)
        if flags:
            rejects.append({'url': url, 'reason': 'qa_reject', 'flags': flags, 'email': email_addr, 'name': name}); continue
        accepted.append(target)
        existing['domains'].add(d)
        existing['emails'].add(email_addr)
        existing['names'].add(slug(name))
        existing['ids'].add(target_id)
        source_rows.append({
            'created_at': utc_now(),
            'id': target['source_ids'][0],
            'ledger_type': 'target_discovery',
            'type': 'Firecrawl search+scrape',
            'url': url,
            'email_source_url': email_source_url,
            'displayed_email': email_addr,
            'use': 'verified public email-ready GLP/weight-loss/wellness clinic target refill',
            'key_points': [
                f'clinic={target["clinic_name"]}',
                f'email={email_addr}',
                f'city_state={city}',
                f'evidence={proof[:220]}',
            ],
            'safety_notes': target['risk_notes'],
        })
    if accepted and not dry_run:
        insert_rows(accepted, source_rows)
    ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    result = {
        'created_at_utc': utc_now(),
        'queries_available': len(QUERY_PATTERNS),
        'query_offset': query_offset,
        'queries_run': min(max_queries, len(query_slice)),
        'search_results_considered': len(urls),
        'accepted': len(accepted),
        'rejected': len(rejects),
        'accepted_targets': accepted,
        'reject_sample': rejects[:120],
        'raw_search_sample': raw_results[:180],
        'dry_run': dry_run,
    }
    json_path = REPORT_DIR / f'firecrawl-refill-ledger-{ts}.json'
    md_path = REPORT_DIR / f'firecrawl-refill-ledger-{ts}.md'
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# Firecrawl refill ledger — GLP Clinic Scout', '', f'Created: {result["created_at_utc"]}', f'Dry run: {dry_run}', f'Accepted: {len(accepted)}', f'Rejected: {len(rejects)}', '', '## Accepted targets']
    for t in accepted:
        lines.append(f'- **{t["clinic_name"]}** — {t.get("city_state_service_area") or "unknown market"} — `{t["public_email"]}`')
        lines.append(f'  - Source: {t["glp_page"]}')
        lines.append(f'  - Email source: {t["email_source_url"]}')
        lines.append(f'  - Evidence: {t["proof_snippet"]}')
        lines.append(f'  - Fit: {t["why_selected"]}')
        lines.append(f'  - Safety: {t["risk_notes"]}')
    lines += ['', '## Query patterns used'] + [f'- {q}' for q in query_slice[:max_queries]]
    md_path.write_text('\n'.join(lines), encoding='utf-8')
    reject_path = ROOT / 'source_quality_rejects.jsonl'
    if rejects and not dry_run:
        with reject_path.open('a', encoding='utf-8') as f:
            for r in rejects:
                event_id = hashlib.sha256(json.dumps(r, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]
                f.write(json.dumps({'reject_id': event_id, 'created_at_utc': utc_now(), 'source_system': 'refill_email_ready_targets', **r}, ensure_ascii=False, sort_keys=True) + '\n')
    result['ledger_json'] = str(json_path)
    result['ledger_md'] = str(md_path)
    result['reject_ledger_jsonl'] = str(reject_path)
    return result


def insert_rows(targets: list[dict], source_rows: list[dict]) -> None:
    existing_targets = read_jsonl(ROOT / 'targets.jsonl')
    existing_messages = read_jsonl(ROOT / 'message_ledger.jsonl')
    existing_ids = {t.get('target_id') for t in existing_targets}
    msg_ids = {m.get('message_id') for m in existing_messages}
    con = sqlite3.connect(DB)
    try:
        for t in targets:
            if t['target_id'] in existing_ids:
                continue
            msgs = make_messages(t)
            flags = qa_target(t, msgs)
            if flags:
                continue
            existing_targets.append(t)
            con.execute('insert or replace into targets values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                t['target_id'], t['clinic_name'], t['url'], t['glp_page'], t['city_state_service_area'], t['market_type'],
                t['public_email'], t['public_phone'], t['contact_form_url'], t['contact_method'], t['contact_safety'],
                t['decision_maker_guess'], t['proof_snippet'], t['why_selected'], t['fit_score'], t['contactability_score'],
                t['risk_notes'], t['wave'], ' | '.join(t['source_urls'])
            ))
            for m in msgs:
                if m['message_id'] in msg_ids:
                    continue
                existing_messages.append(m)
                msg_ids.add(m['message_id'])
                con.execute('insert or replace into messages values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                    m['message_id'], m['target_id'], m['clinic_name'], m['channel'], m['to_email'], m['contact_form_url'],
                    m['step'], m['due_day'], m['subject'], m['body'], m['word_count'], json.dumps(m['qa_flags']),
                    m['send_status'], m['created_at_utc']
                ))
        con.commit()
    finally:
        con.close()
    write_jsonl(ROOT / 'targets.jsonl', existing_targets)
    write_jsonl(ROOT / 'message_ledger.jsonl', existing_messages)
    with (ROOT / 'source_ledger.jsonl').open('a', encoding='utf-8') as f:
        for row in source_rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--min-unsent', type=int, default=75)
    parser.add_argument('--refill-count', type=int, default=75)
    parser.add_argument('--max-urls', type=int, default=500)
    parser.add_argument('--max-queries', type=int, default=90)
    parser.add_argument('--query-offset', type=int, default=0)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--quiet-healthy', action='store_true')
    args = parser.parse_args()

    with LOCK_PATH.open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'status': 'blocked', 'reason': 'refill_already_running'}, indent=2))
            return 2
        before = unsent_day0_count()
        backup_path = None
        repair = apply_known_repairs(dry_run=args.dry_run)
        if before >= args.min_unsent and not args.force:
            if not args.quiet_healthy:
                print(json.dumps({'status': 'healthy_no_refill_needed', 'unsent_day0': before, 'threshold': args.min_unsent, 'repair': repair}, indent=2, ensure_ascii=False))
            return 0
        if not args.dry_run:
            backup_path = backup_once('pre-refill')
        api_key = env_value('FIRECRAWL_API_KEY')
        if not api_key:
            print(json.dumps({'status': 'error', 'error': 'missing_firecrawl_api_key_in_local_store', 'unsent_day0': before}, indent=2))
            return 2
        from firecrawl import Firecrawl
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
        from firecrawl_client import LedgerSDKClient
        app = LedgerSDKClient(Firecrawl(api_key=api_key), caller='sponsor.refill_email_ready_targets')
        to_add = max(0, args.refill_count)
        result = discover(app, to_add, args.max_urls, args.max_queries, args.dry_run, args.query_offset)
        after = unsent_day0_count() if not args.dry_run else before + result['accepted']
        out = {
            'status': 'queued_refill_targets' if result['accepted'] else 'refill_no_new_safe_public_email_targets',
            'created_at_utc': utc_now(),
            'before_unsent_day0': before,
            'after_unsent_day0': after,
            'threshold': args.min_unsent,
            'accepted': result['accepted'],
            'rejected': result['rejected'],
            'repair': repair,
            'backup_path': str(backup_path) if backup_path else None,
            'ledger_json': result['ledger_json'],
            'ledger_md': result['ledger_md'],
            'counts_by_wave': counts_by_wave(),
            'dry_run': args.dry_run,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        if after < args.min_unsent and not args.dry_run:
            return 1
        return 0


if __name__ == '__main__':
    code = 1
    try:
        code = main()
    except Exception as exc:
        print(json.dumps({
            'status': 'error',
            'component': 'refill_email_ready_targets',
            'error_type': type(exc).__name__,
            'error': repr(exc),
            'created_at_utc': utc_now(),
            'secret_values_printed': False,
        }, indent=2, ensure_ascii=False))
        code = 1
    finally:
        sys.stdout.flush(); sys.stderr.flush()
        os._exit(code)
