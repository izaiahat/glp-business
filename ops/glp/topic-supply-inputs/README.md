# Maintained topic-supply inputs

These are genuine strategy inputs recovered from the verified local archive
`reports/glp-clinic-scout/seo-blog-machine-20260804.tar.zst`, whose SHA256 is
`a12e3d1ff1bc137326a89ea602e9e6ef5b14c2714d22d56847bf5966a0a38779`.
The archive and its inventory remain unchanged.

- `cluster-map.json` is the exact member `G12-cluster-map-20260804.json`, SHA256
  `05d215c1bd3a8656f4bc5f5534a1ad5e13934b94a649492743fd975533f2818b`.
- `strategy-queue.json` is the exact 98-row member
  `topic-queues/20260804T120000Z-cluster-rotation.json`, original SHA256
  `5763fcc1e92fb3cfe5b4175f7c1e2db38044274d3f8c4167333296d88d259ec9`,
  with only `map_path` relocated to the maintained cluster map.

The registered topic minter and replenisher inherit the shared selector's
`DEFAULT_STRATEGY_QUEUE`. Active inputs must not live under age-retained report
folders. No new topics, claim evidence, provider permissions, or publication
approvals are introduced here. All selector joins, live/terminal/held topic
exclusions, and predecessor guards are unchanged.

The existing replenisher may extend the installed queue. Subsequent source
imports must honor the installer's local-drift refusal, not reset that queue.
