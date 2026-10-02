"""Git-derived generations; historical named adopter contracts stay immutable."""
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from maxbounty_pin_release import read_regular


def json_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def validate_review(proposal, review):
    if proposal['tier'] == 'B':
        return
    preparer = str(proposal.get('prepared_by') or '').strip().casefold()
    reviewer = str((review or {}).get('reviewer') or '').strip().casefold()
    if (not review or proposal['tier'] != 'A' or not preparer or not reviewer
            or preparer == reviewer or review.get('prepared_by') != proposal['prepared_by']
            or review.get('schema') != 'glp-ship-review-v1'
            or review.get('decision') != 'ACCEPT_EXACT_PROPOSAL'):
        raise RuntimeError('independent_review_required')
    if review.get('proposal_sha256') != json_sha(proposal):
        raise RuntimeError('review_proposal_moved')


def fingerprint(path):
    body, mode = read_regular(Path(path))
    return {'sha256': hashlib.sha256(body).hexdigest(), 'mode': mode}


@dataclass(frozen=True)
class GitGeneration:
    source: str
    kind: str
    paths: tuple
    hashes: dict
    proof: dict
    proposal: dict

    @property
    def name(self):
        return 'git-' + self.source

    def verify(self, checkout):
        from glp_ship_provenance import derive_pins
        if derive_pins(checkout, self.source, self.paths) != self.hashes:
            raise RuntimeError('ship_generation_moved')

    def authorize(self, review, root, hermes, window):
        tiers = {'reporting': 'B', 'retention': 'A', 'discovery': 'A'}
        if self.kind not in tiers or self.proposal.get('tier') != tiers[self.kind]:
            raise RuntimeError('ship_registered_tier_moved')
        validate_review(self.proposal, review)
        if (self.proposal.get('source') != self.source
                or self.proposal.get('paths') != list(self.paths)
                or self.proposal.get('hashes') != self.hashes
                or self.proposal.get('kind') != self.kind
                or self.proposal.get('merge_proof') != self.proof):
            raise RuntimeError('ship_generation_proposal_moved')
        if (str(root.resolve()) != self.proposal['root']
                or str(hermes.resolve()) != self.proposal['hermes']):
            raise RuntimeError('ship_target_moved')
        for name, expected in self.proposal['before'].items():
            if expected is None:
                if Path(name).exists() or Path(name).is_symlink():
                    raise RuntimeError('ship_preimage_moved:' + name)
                continue
            if fingerprint(name) != expected:
                raise RuntimeError('ship_preimage_moved:' + name)
        jobs = json.loads(read_regular(hermes / 'cron/jobs.json')[0])['jobs']
        for jid, expected in self.proposal['jobs'].items():
            found = [j for j in jobs if j.get('id') == jid]
            if len(found) != 1:
                raise RuntimeError('ship_job_identity_moved:' + jid)
            actual = window.original_job(jid) if window is not None else found[0]
            if actual != expected:
                raise RuntimeError('ship_job_preimage_moved:' + jid)
