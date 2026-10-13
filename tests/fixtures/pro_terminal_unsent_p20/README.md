# P20 GLP-358 refusal fixture

`mode_receipt.json` is the unchanged `error` subtree of the copied real
`lanes/P20/GLP-358/preimages/00-JOB.json` (SHA256
`ecac50d8ceb0dcbf5878dff96e20334c5d50048e739cf69b9650c92bfb83e543`).
The original and complete P20 evidence packet are copied into
`lanes/P21/fixtures/P20/GLP-358/` for offline custody replay, outside Git.
Only the refusal facts and isolated empty-root witness are published here:
no account identifier, prompt, ledger, sibling URL or credential.
`empty_witness.json` projects `tab`, `first`, `last`, and `state` unchanged
from P20's `OBSERVATION.json` (SHA256
`faf87fefe66767c4d367b8b8d0bfef0ba092cf9cc7ac8a1187b6020b1ef1300b`);
`same_tabs` is derived from the equal before/after target inventories.
Test counterexamples are explicit mutations, not additional live observations.
Actual-CLI tests substitute this receipt's
attributed tab into isolated synthetic packet/report/journal custody.
No live terminalization, browser call, send or GLP-336 action is authorized.
