# Agent operating rules

Agents may edit research code, tests, documentation and non-secret configuration. They must
preserve reproducibility, cite data sources in study contracts and keep generated data out of
Git. Agents must not add credentials, enable trading, place orders, connect a brokerage account (the only exception is the Alpaca paper account the user chose on
2026-10-03 for roadmap stage 2: paper endpoint only, switched on by the user in
`configs/paper_broker.yml`),
weaken risk limits (worst 12-month loss ≤ 50% of principal, raised from 25% by the user on
2026-10-01; only the user may change it) or promote a study without explicit
human review. Study parameters are pre-registered: changing them after seeing results means a
new study version, recorded in `docs/rejected.md` or a new `research/<name>/`.
