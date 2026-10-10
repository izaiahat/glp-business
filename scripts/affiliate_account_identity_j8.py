"""Response-owned account identity diagnostic, never a revenue eligibility oracle.

The historical 56-gap counter is composite coverage, not failed account matches.
Bind native ID roles/receipt pointers at the reader/report seam; never infer a
publisher from a program ID, key-slot name or single default Everflow tenant.
"""
import re
from collections import defaultdict

CONTRACTS={
    'digistore24':('api_key_holder_user_id','/data/user_id','https://www.digistore24.com/api/call/getUserInfo','RESPONSE_OWNED_USER_ID:'),
    'maxbounty':('publisher_id','/publisher_id','https://api.maxbounty.com/affiliates/api/affiliate','RESPONSE_OWNED_PUBLISHER_ID:'),
}


def resolve_accounts(names, observations):
    grouped=defaultdict(list)
    for observation in observations:
        grouped[observation.get('network')].append(observation)
    rows=[]
    for network in sorted(set(names)):
        contract=CONTRACTS.get(network)
        matches={}
        invalid=False
        for observation in grouped[network]:
            identity=observation.get('source_identity')
            if not contract or not isinstance(identity,dict):
                continue
            kind,pointer,endpoint,prefix=contract
            account=identity.get('account_id')
            evidence=identity.get('evidence',{})
            sha=evidence.get('response_sha256') if isinstance(evidence,dict) else None
            receipts=[r for r in observation.get('receipts',[]) if
                      isinstance(r,dict) and r.get('endpoint')==endpoint and r.get('http_status')==200
                      and r.get('response_sha256')==sha]
            valid=(isinstance(account,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}',account)
                   and identity.get('native_id_type')==kind and evidence.get('pointer')==pointer
                   and isinstance(sha,str) and re.fullmatch(r'[a-f0-9]{64}',sha)
                   and observation.get('account_binding')==prefix+account and receipts
                   and observation.get('status')=='SUPPORTED')
            if network=='digistore24':
                valid=valid and 'affiliate' in identity.get('roles',[])
            if not valid:
                invalid=True
                continue
            matches[(account,'not_applicable')]=dict(evidence)
        bound=not invalid and len(matches)==1
        account,tenant=next(iter(matches)) if bound else (None,None)
        reasons={o.get('account_binding','UNKNOWN') for o in grouped[network]}
        if invalid or len(matches)>1:
            reason='CONFLICTING_OR_UNAUTHENTICATED_SOURCE_IDENTITY'
        elif bound:
            reason='SOURCE_NATIVE_ACCOUNT_ROLE_AND_RECEIPT_BOUND_NOT_PROGRAM_CLASSIFICATION'
        elif network=='everflow':
            reason='HETEROGENEOUS_TENANTS_NO_SINGLE_DEFAULT_ACCOUNT'
        elif network=='shareasale':
            reason='LEGACY_AWIN_ALIAS_NOT_ANOTHER_FINANCIAL_ACCOUNT'
        elif any(x.startswith('RETAINED_PROFILE_BINDING:') for x in reasons):
            reason='PROFILE_ID_IS_NOT_PUBLISHER_ACCOUNT_ID'
        elif any(x.startswith('RESPONSE_OWNED_') for x in reasons):
            reason='READER_OMITS_TYPED_ID_ROLE_OR_ACCOUNT_RESPONSE_RECEIPT'
        elif reasons != {'UNKNOWN'}:
            reason='CONFIGURED_REQUEST_CREDENTIAL_IS_NOT_RESPONSE_OWNED_ACCOUNT'
        else:
            reason='FINANCIAL_READER_OR_SOURCE_ACCOUNT_ID_MISSING'
        rows.append({'network':network,'account_id':account,'tenant_id':tenant,
                     'state':'MATCHED_ACCOUNT_ONLY' if bound else 'UNRESOLVED_ACCOUNT_IDENTITY',
                     'reason':reason,'evidence':list(matches.values()) if bound else [],
                     'program_identity':'UNKNOWN','financial_classification':'UNKNOWN',
                     'owner':'GLP-334'})
    matched=sum(r['state']=='MATCHED_ACCOUNT_ONLY' for r in rows)
    return {'schema':'account-identity-resolution-v1','network_count':len(rows),
            'matched_network_count':matched,'unmatched_network_count':len(rows)-matched,
            'scope':'ACCOUNT_ONLY_NOT_THE_COMPOSITE_IDENTITY_CLASSIFICATION_GAP_COUNTER',
            'rows':rows}
