import copy,hashlib,json,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from build_glp_affiliate_wirein_incremental_v2 import paired_cta_tracking_successor
class TrackingPairTest(unittest.TestCase):
 def setUp(self):
  self.before={'providers':[{'program_id':'one','network_contract':{'network':'MaxBounty','tracking_url':'https://old.test'},'row_contract':{'affiliate_href':'https://old.test'}},{'program_id':'other','network_contract':{},'row_contract':{}}]}
  self.raw=(json.dumps(self.before,indent=2,sort_keys=True)+'\n').encode();self.m={'schema':'glp-provider-cta-identity-v2','metadata_only':True,'baseline_contract_sha256':hashlib.sha256(self.raw).hexdigest(),'programs':{}}
  self.after=copy.deepcopy(self.before);self.after['providers'][0]['network_contract']['tracking_url']='https://new.test';self.after['providers'][0]['row_contract']['affiliate_href']='https://new.test'
 def test_bound_replacement_only(self):
  m=paired_cta_tracking_successor(self.raw,self.after,self.m,{'one':'https://new.test'});self.assertEqual(m['programs'],{});self.assertEqual(m['baseline_contract_sha256'],hashlib.sha256((json.dumps(self.after,indent=2,sort_keys=True)+'\n').encode()).hexdigest())
 def test_unrelated_row_or_metadata_change_refuses(self):
  for change in ('row','metadata'):
   after=copy.deepcopy(self.after)
   if change=='row':after['providers'][1]['new']='unsafe'
   else:after['extra']=True
   with self.assertRaisesRegex(RuntimeError,'cta_tracking_successor_scope_invalid'):paired_cta_tracking_successor(self.raw,after,self.m,{'one':'https://new.test'})
 def test_bad_predecessor_refuses(self):
  self.m['baseline_contract_sha256']='0'*64
  with self.assertRaisesRegex(RuntimeError,'cta_identity_baseline_mismatch'):paired_cta_tracking_successor(self.raw,self.after,self.m,{'one':'https://new.test'})
 def test_identity_overlay_replacement_refuses(self):
  self.m['programs']={'one':{'network':'Affiliati'}}
  with self.assertRaisesRegex(RuntimeError,'cta_tracking_identity_overlap'):paired_cta_tracking_successor(self.raw,self.after,self.m,{'one':'https://new.test'})
if __name__=='__main__':unittest.main()
