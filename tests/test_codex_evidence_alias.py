"""Live-producer alias regression; loopback ephemeral test only."""
import importlib.util,sys,threading,http.client,json
from pathlib import Path
P=Path(__file__).resolve().parents[1]/'reports/ops/codex-cloud-tailscale-20261009/T3/evidence_server.py'
sys.path.insert(0,str(P.parent));spec=importlib.util.spec_from_file_location('r1_evidence_source',P);source=importlib.util.module_from_spec(spec);spec.loader.exec_module(source)
source.snapshot=lambda:{'schema':'codex-evidence-v1','fixture':True}
def test_exact_readonly_alias_and_denials():
 server=source.ThreadingHTTPServer(('127.0.0.1',0),source.Handler)
 t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
 try:
  for path in ('/evidence/codex.json','/v1/fleet-status.json'):
   c=http.client.HTTPConnection('127.0.0.1',server.server_port);c.request('GET',path);r=c.getresponse();assert r.status==200;assert json.loads(r.read())['schema']=='codex-evidence-v1';c.close()
  for path in ('/v1/fleet-status.json?x=1','/v1/../v1/fleet-status.json','/v1/fleet-status.json/','/','/wrong'):
   c=http.client.HTTPConnection('127.0.0.1',server.server_port);c.request('GET',path);r=c.getresponse();assert r.status==404;r.read();c.close()
  for verb in ('HEAD','POST','PUT','DELETE','OPTIONS'):
   c=http.client.HTTPConnection('127.0.0.1',server.server_port);c.request(verb,'/v1/fleet-status.json');r=c.getresponse();assert r.status==405;r.read();c.close()
 finally:server.shutdown();server.server_close();t.join()
