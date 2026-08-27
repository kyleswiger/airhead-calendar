# Changelog

## 1.0.0 (2026-08-27)


### Features

* **adapters:** provider-agnostic CalendarSource protocol and contract suite ([3e5e889](https://github.com/kyleswiger/airhead-calendar/commit/3e5e88950f4f0cdf04ef096eecb07f9cc5b092c4))
* **adapters:** provider-agnostic CalendarSource protocol and contract suite ([e8d1c50](https://github.com/kyleswiger/airhead-calendar/commit/e8d1c505727ed07879e703692056069a369ef9e3)), closes [#6](https://github.com/kyleswiger/airhead-calendar/issues/6)
* **agent:** migrate M2 agent from Anthropic API to AWS Bedrock with IAM auth ([#15](https://github.com/kyleswiger/airhead-calendar/issues/15)) ([a9dee05](https://github.com/kyleswiger/airhead-calendar/commit/a9dee05761395d32d0eba9bd55bab9d421171972))
* **api:** minors may propose events for other members ([4c99fb6](https://github.com/kyleswiger/airhead-calendar/commit/4c99fb6900c88d3f26a6335e7fd432931790fe7e))
* **api:** minors may propose events for other members (closes [#4](https://github.com/kyleswiger/airhead-calendar/issues/4)) ([87512a9](https://github.com/kyleswiger/airhead-calendar/commit/87512a97906a99bee60f79da278e17b09b01a2c2))
* CI/CD — OIDC deploy role, push-to-main deploy, gitleaks gate ([3e35a65](https://github.com/kyleswiger/airhead-calendar/commit/3e35a658ece50e3f21075444842b53d30c56a77b))
* **ci:** continuous deploy of Lambda code and site on push to main ([d76aa58](https://github.com/kyleswiger/airhead-calendar/commit/d76aa588d23539ac5369e964954d9e9f4eb34ee5))
* **ci:** pre-merge gitleaks secret scan (pinned 8.30.1, checksum-verified) ([bffff49](https://github.com/kyleswiger/airhead-calendar/commit/bffff490be857daf84fb3985bf937c427d11d39c))
* **infra:** GitHub OIDC deploy role via shared github-oidc-role module ([d3e1c1a](https://github.com/kyleswiger/airhead-calendar/commit/d3e1c1a34343970a35d68556063731adfca1d0c5))
* M1 — data model, repo layer, CRUD API, tiered day view ([9ac6de6](https://github.com/kyleswiger/airhead-calendar/commit/9ac6de618b72d04d35588306b37a813bdf53694a))
* **m1:** control plane infrastructure ([84999c1](https://github.com/kyleswiger/airhead-calendar/commit/84999c10a438e70ec4e3ddd4dc896c8e8c71181a))
* **m1:** CRUD and agenda HTTP API ([26b8e46](https://github.com/kyleswiger/airhead-calendar/commit/26b8e462466a6f428427bf2df170db3fb84febc7))
* **m1:** freeze the repo protocol and wire contract ([9bb4361](https://github.com/kyleswiger/airhead-calendar/commit/9bb4361445d9c6c777338e01938e4f965aaceeac))
* **m1:** recurrence expansion and tiered agenda assembly ([b6942ee](https://github.com/kyleswiger/airhead-calendar/commit/b6942eec390d7f638477cd8465e2638a89299a8a))
* **m1:** SQLite and DynamoDB repositories ([391b922](https://github.com/kyleswiger/airhead-calendar/commit/391b922db19ff014bdff6a6aac3823bda01fb81a))
* **m1:** tiered kitchen day view ([474d454](https://github.com/kyleswiger/airhead-calendar/commit/474d454ebd2f24d9fc51e270b11065fe4f669ef3))
* M2 — agentic interface (tool loop, chat, audit log) ([e01efef](https://github.com/kyleswiger/airhead-calendar/commit/e01efefe82b0c728360bf04c10c02a5cb516c571))
* **m2:** agent Lambda, key in SSM, and role separation ([85e4c79](https://github.com/kyleswiger/airhead-calendar/commit/85e4c79a6d4dba1f526713029fb31b5ff35518eb))
* **m2:** agent tool loop on Opus 5 ([36e318f](https://github.com/kyleswiger/airhead-calendar/commit/36e318f0a31c23b16b6e6fa20d6761696cbc4f6f))
* **m2:** agent turn endpoint and audit log ([600db22](https://github.com/kyleswiger/airhead-calendar/commit/600db22ef0aa66cc4a3341e851937f7ffbe41fe9))
* **m2:** freeze the agent contract and add the SDK ([ac6b476](https://github.com/kyleswiger/airhead-calendar/commit/ac6b47673be52ff3598a8bb6d668ad2a0f7d4182))
* **m2:** on-screen chat with a confirmation gate ([767f835](https://github.com/kyleswiger/airhead-calendar/commit/767f83591238332357e6c36aeccb0cbbb37e649d))
* scaffold Airhead Calendar (M0) ([a6c1959](https://github.com/kyleswiger/airhead-calendar/commit/a6c19598459a7bdec87c3b5b3d1d954f2a5b2923))
* surface proposed status on the display and let adults confirm via the agent ([f8f5704](https://github.com/kyleswiger/airhead-calendar/commit/f8f57041ed5bce5e4c9ef47d72e0f17ffb3cd204))


### Bug Fixes

* **agent:** guard legacy pending gates and report true settled outcomes ([2108f52](https://github.com/kyleswiger/airhead-calendar/commit/2108f5206863084e099635e413bc99d608f9fb38))
* **agent:** legacy Bedrock InvokeModel client + Sonnet 4.6 (Mantle/Opus 5 gated on this account) ([#16](https://github.com/kyleswiger/airhead-calendar/issues/16)) ([a3500ed](https://github.com/kyleswiger/airhead-calendar/commit/a3500ed1fe88c68621aeafd8a561337ee2b22b63))
* **agent:** replay approved gated tool calls in the harness ([f6f3fda](https://github.com/kyleswiger/airhead-calendar/commit/f6f3fdaafc2105581b912ba8bbf875ccc3ea9a62))
* **agent:** replay approved gated tool calls in the harness ([5bafbd6](https://github.com/kyleswiger/airhead-calendar/commit/5bafbd65f1b0b6b60b76c2ecdcff20fa19d0e75e))
* **agent:** serialize SDK content blocks before persisting turn history ([#22](https://github.com/kyleswiger/airhead-calendar/issues/22)) ([4627a11](https://github.com/kyleswiger/airhead-calendar/commit/4627a112b0049398c6c1372fa55a2b663139b257)), closes [#5](https://github.com/kyleswiger/airhead-calendar/issues/5)
* **ci:** address review findings on the CI/CD pipeline ([ad102d4](https://github.com/kyleswiger/airhead-calendar/commit/ad102d4298d61444e4c154c0bf7c618e7ab2d37c))
* **infra:** trust GitHub's immutable OIDC subject claim ([#14](https://github.com/kyleswiger/airhead-calendar/issues/14)) ([9115434](https://github.com/kyleswiger/airhead-calendar/commit/91154349fb8a6e5d07b22e79dda08e573ab58f5d))
* **m1:** answer the CORS preflight, and read the API URL from state ([4a8b83e](https://github.com/kyleswiger/airhead-calendar/commit/4a8b83e8f875e69b76f45f66d77b9ca81575d371))
* make the lambda build reproducible ([6003ad8](https://github.com/kyleswiger/airhead-calendar/commit/6003ad82fc6b78f41c391766e31783ba88157638))
* make the lambda build reproducible ([a8c2676](https://github.com/kyleswiger/airhead-calendar/commit/a8c267660346b227625f5a74549ab30bae3a8722))
