#!/usr/bin/env python3
from __future__ import annotations
import datetime,html.parser,json,re,socket,ssl,urllib.error,urllib.parse,urllib.request
from pathlib import Path
HOST='glpclinicscout.com';ROOT=Path('/home/ubuntu/business/reports/glp-clinic-scout/excellence-pass-20260801/monitoring/affiliate-link-rot')
class Links(html.parser.HTMLParser):
 def __init__(self):super().__init__();self.links=[]
 def handle_starttag(self,tag,attrs):
  if tag!='a':return
  a=dict(attrs);href=a.get('href','')
  if href and (a.get('data-analytics-event')=='affiliate_click' or a.get('rel') and 'sponsored' in a.get('rel','').split()):self.links.append(href)
def fetch(url,method='GET'):
 req=urllib.request.Request(url,method=method,headers={'User-Agent':'ClinicScoutLinkRot/1.0','Accept':'text/html,application/xhtml+xml'})
 try:
  with urllib.request.urlopen(req,timeout=20) as r:return int(r.status),r.geturl(),r.read(2_000_000) if method=='GET' else b''
 except urllib.error.HTTPError as e:return int(e.code),e.geturl(),b''
def tls(host):
 try:
  addrs=sorted({x[4][0] for x in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)});ctx=ssl.create_default_context()
  with socket.create_connection((host,443),timeout=15) as raw:
   with ctx.wrap_socket(raw,server_hostname=host) as s:cert=s.getpeercert()
  return {'ok':True,'addresses':addrs,'certificate_subject':dict(x[0] for x in cert.get('subject',[]))}
 except Exception as exc:return {'ok':False,'error':f'{type(exc).__name__}:transport_failed'}
def main():
 now=datetime.datetime.now(datetime.timezone.utc);s,burl,body=fetch(f'https://{HOST}/sitemap.xml');failures=[];pages=[];links=[]
 if s!=200:failures.append(f'sitemap_http_{s}')
 else:
  routes=[urllib.parse.urlparse(x.decode()).path for x in re.findall(rb'<loc>\s*([^<]+)\s*</loc>',body)]
  routes=[x for x in routes if x.startswith(('/best/','/cost/','/compare/','/reviews/'))]
  for route in routes:
   ps,final,pbody=fetch(f'https://{HOST}{route}');pages.append({'route':route,'status':ps,'final':final})
   if ps!=200:failures.append(f'page_http_{ps}:{route}');continue
   parser=Links();parser.feed(pbody.decode('utf-8','replace'))
   for href in parser.links:
    absolute=urllib.parse.urljoin(f'https://{HOST}{route}',href);u=urllib.parse.urlparse(absolute)
    if u.hostname==HOST:
     if u.path.startswith('/go/'):failures.append(f'opaque_go_route_present:{route}:{u.path}')
     continue
    links.append({'page':route,'url':absolute,'host':u.hostname})
  unique={x['url']:x for x in links};host_transport={}
  for host in sorted({x['host'] for x in unique.values() if x['host']}):host_transport[host]=tls(host)
  checks=[]
  for url,item in sorted(unique.items()):
   hs,final,_=fetch(url,method='HEAD');alive=200<=hs<500 and (host_transport.get(item['host']) or {}).get('ok') is True
   checks.append({**item,'head_status':hs,'head_final':final,'transport':host_transport.get(item['host']),'alive':alive})
   if not alive:failures.append(f'link_rot:{item["page"]}:{item["host"]}:http_{hs}')
 status='PASS' if not failures else 'FAIL';receipt={'schema':'glp-affiliate-link-rot-v1','checked_at_utc':now.replace(microsecond=0).isoformat().replace('+00:00','Z'),'status':status,'sitemap_status':s,'pages_checked':len(pages),'affiliate_anchors':len(links),'unique_links':len({x['url'] for x in links}),'failures':failures,'pages':pages,'checks':checks if 'checks' in locals() else [],'writes':0,'redirect_gets':0,'method':'HEAD plus DNS/TLS only; no affiliate GET requests','secrets_printed':False}
 ROOT.mkdir(parents=True,exist_ok=True);stamp=now.strftime('%Y%m%dT%H%M%SZ');p=ROOT/f'{stamp}.json';p.write_text(json.dumps(receipt,indent=2)+'\n');p.chmod(0o600);latest=ROOT/'latest.json';latest.write_text(json.dumps(receipt,indent=2)+'\n');latest.chmod(0o600)
 if status!='PASS':
  print(f'🔴 **GLP Link Rot** — {len(failures)} live-link failures\n\nchecked: **{len({x["url"] for x in links})} unique links**\n→ Repair or hold the failed affiliate links before routing traffic.\n-# receipt: {p}')
 return 0
if __name__=='__main__':raise SystemExit(main())
