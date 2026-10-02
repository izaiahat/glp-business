# Combined GLP-333 / GLP-44 reporting successor

`glp333-glp44-reporting-successor-20261002` is one fixed cohort for both
hourly-collector and weekly-reporting publication. It binds main
`9e01dde818016c089df4c6ee4dd667b03fe37a28`, including PR151 and PR153.
The previous PR71, PR119 and collector generations remain immutable; their
old generation names are intentionally not aliases for this one.

The 16 publication members comprise the eight collector/reporting members,
network-access dispatch, the Katalys parser and six new collector modules.
All other reporting pins remain retained inputs and are checked against the
host. Maintenance inventory additionally discovers every new publication member.
No guards, baseline adoption rules, independent review, window checks or
publication/custody behavior are changed.

## Read-only preview

Use a fresh, clean checkout of the reviewed PR head. Both former preview
requests now select the same combined cohort (one publication, not two applies):

```bash
python3 -B "$CHECKOUT/scripts/glp44_scoped_adoption.py" \
  --generation glp333-glp44-reporting-successor-20261002 \
  --checkout "$CHECKOUT" --root /home/ubuntu/business \
  --hermes /home/ubuntu/.hermes
```

The CLI supports a no-window preview. For the next authorized maintenance
window add `--window "$ACTIVE_JOURNAL"`; its proposal must be freshly reviewed.
An outside-window proposal is diagnostic evidence, not runtime acceptance.
Use runtime review schema `glp333-glp44-reporting-successor-runtime-review-v1`.

## Missing predecessor prerequisite — do not skip

The receiving host census found all six `scripts/affiliate_reporting_*.py`
publication members absent. A dry proposal represents them with null preimages.
The unchanged apply guard **will refuse** `missing_predecessor_requires_separate_custody`
until those exact committed leaf files have been separately installed with the
maintained importer/custody procedure. This PR does not authorize, perform or
invent that prerequisite. The coordinator must resolve this before apply,
then regenerate and independently review the exact window-bound proposal.
Do not bypass the refusal or reuse a pre-stage proposal.

## Historical fixtures

PR119 tests keep real committed historical reporting bytes and exact producer
byte equality. An intermediate fixture commit holds the new adopter with the
unchanged historical cohort; the final commit restores every reporting payload
to the main source above. The fixture is test context, not a deployment candidate.
Only the final reviewed head may be used for this new generation.
