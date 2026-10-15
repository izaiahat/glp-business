# GLP-349 / F06 exact-main release recovery and input reuse

## Reproduced failure

The Oct15 18:08Z terminal publication remains genuine. Site PR119 merged at
19:48:53Z as `9add9230cf86c5905f0959b6f52f828823e3a986`, but its checked head
`9de42f6b1c958fe427ee0871ba8aee5d936dd739` omits PR118's accepted dose-vs-units
article/QA/review and the corresponding accepted-guide catalog update.
The main tree is `86cf1452ec9dde05110015ea39187e7c6a67610f`; the checked head's
tree differs. The exact-head guard correctly refused that stale-base merge.

Recovery must not treat an integration commit as an accepted PR head. The
producer proposes a deterministic marker-only successor PR from exact current
main, waits for its ordinary exact-head CI, verifies its sole-parent/sole-file
scope and marker, merges with an exact SHA guard, then reruns the unchanged
merged-PR/tree-equality gate. An advanced base, mutated proposal, closed proposal,
non-green CI or uncertain ref never grants publication. Unrelated PR116 stays
untouched. The proposal itself is not a release receipt.

## Exact input reuse

A successful publication now retains BUILD-INPUTS.json and SUCCESSFUL-INPUTS.json.
The key covers the full accepted Git tree and revision (HEAD/history is a build
input), including dependencies lockfiles,
all tracked data, taxonomy and manifests, a clean source boundary, the fresh
sole-writer Sheet snapshot, exact namespace/config, every namespace bind's
readable bytes/types/modes/links, all explicitly set environment values (hashes,
not secrets), runtime/tool bytes, producer source and UTC rendering day.
Namespace networking, capabilities and inherited environment remain closed.
Nested symbolic links are interpreted in the namespace, not followed into
unmounted host secrets; inaccessible entries retain their permission identity.
No historical release is assigned invented input provenance.

The full native candidate is hash-sealed as the validation source because the
sanitized package omits Functions source. Its actual deployed `public/` is
mounted read-only over that source. A missing/changed candidate forces a build.
Validation uses the registered `build:validated` post-build tail, not the obsolete
`npm run validate` alias; a real retained read-only probe exercised this path.

A possible hit is checked before cloning. It still reruns exact-head CI,
read-only native validators with current-time freshness/expiry evaluation,
independent raw-held and adverse-absence checks, secrets scan and the live
completeness sensor. Its packaged release closure and all current inputs must
remain unchanged across validation. Only then is SKIPPED_IDENTICAL_INPUTS
recorded, with no clone/build/package/deployment/pointer mutation. The registered
shell receives its existing PASS_ALREADY_CURRENT compatibility status plus
receipt_status=SKIPPED_IDENTICAL_INPUTS; the receipt keeps the precise status.
UTC-day changes force a build; sub-day freshness controls are rerun, not cached.

Missing input history or any input difference requires a real build. STOP92 is
checked before allocating a clone. Source-only fixture tests are not a live skip
or publication proof; installed manual and natural receipts remain required.
