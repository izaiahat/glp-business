"""Read/cite persistent business context and live SEO owner instructions."""
from pathlib import Path
import hashlib
B=Path('/home/ubuntu/business')
CONTEXT_PATH=B/'ops/glp/seo/BUSINESS-CONTEXT.md'
SKILL_ROOT=Path('/home/ubuntu/.hermes/skills')
SEO_SKILLS=('comparison-site-seo','comparison-site-programmatic-seo','gsc-page-two-optimization','intent-internal-linking','provider-review-seo','search-console-bing-operations')

def _read(path):
 path=Path(path)
 if path.is_symlink():raise ValueError('seo_context_symlink_forbidden')
 raw=path.read_bytes()
 if not raw.strip():raise ValueError('seo_context_empty')
 return {'path':str(path.resolve()),'sha256':hashlib.sha256(raw).hexdigest(),'text':raw.decode('utf-8')}

def load_business_context(job_name):
 if not isinstance(job_name,str) or not job_name.strip():raise ValueError('seo_context_job_name_invalid')
 return {**_read(CONTEXT_PATH),'job_name':job_name,'authority':'business_context_not_release_or_clinical_authority'}

def load_seo_skills():
 return {name:_read(SKILL_ROOT/name/'SKILL.md') for name in SEO_SKILLS}
