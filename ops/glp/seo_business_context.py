"""Read/cite persistent business context and live SEO owner instructions."""
from pathlib import Path
import hashlib
B=Path('/home/ubuntu/business')
CONTEXT_PATH=B/'ops/glp/seo/BUSINESS-CONTEXT.md'
SKILL_ROOT=Path('/home/ubuntu/.hermes/skills')
# Preserve the evidence-key contract and load order used by weekly RULES.
# Keys identify procedure coverage, not necessarily a current indexed skill.
# Canonical destinations: ops/linear/SKILL-INDEX.md and each owner's
# references/MERGED-PROCEDURES.md (Bing shares the GSC-owned procedure).
SEO_SKILL_PATHS={
 'comparison-site-seo':'comparison-site-programmatic-seo/references/consolidated/comparison-site-seo/PROCEDURE.md',
 'comparison-site-programmatic-seo':'comparison-site-programmatic-seo/SKILL.md',
 'gsc-page-two-optimization':'gsc-page-two-optimization/SKILL.md',
 'intent-internal-linking':'intent-internal-linking/SKILL.md',
 'provider-review-seo':'provider-review-seo/SKILL.md',
 'search-console-bing-operations':'gsc-page-two-optimization/references/consolidated/search-console-bing-operations/PROCEDURE.md',
}
SEO_SKILLS=tuple(SEO_SKILL_PATHS)

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
 return {name:_read(SKILL_ROOT/SEO_SKILL_PATHS[name]) for name in SEO_SKILLS}
