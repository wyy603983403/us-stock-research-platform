# Agent operating rules

Agents may edit research code, tests, documentation and non-secret configuration. They must
preserve reproducibility, cite data sources in study contracts and keep generated data out of
Git. Agents must not add credentials, enable trading, place orders, connect a brokerage account,
weaken risk limits (worst 12-month loss ≤ 50% of principal, raised from 25% by the user on
2026-10-01; only the user may change it) or promote a study without explicit
human review. Study parameters are pre-registered: changing them after seeing results means a
new study version, recorded in `docs/rejected.md` or a new `research/<name>/`.
