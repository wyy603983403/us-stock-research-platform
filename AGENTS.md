# Agent operating rules

Agents may edit research code, tests, documentation and non-secret configuration. They must
preserve reproducibility, cite data sources in study contracts and keep generated data out of
Git. Agents must not add credentials, enable trading, place orders, connect a brokerage account (the only exception is the Alpaca paper account the user chose on
2026-10-03 for roadmap stage 2: paper endpoint only, switched on by the user in
`configs/paper_broker.yml`),
weaken risk limits (worst 12-month loss ≤ 50% of principal, raised from 25% by the user on
2026-10-01; only the user may change it) or promote a study without explicit
human review. Real money: the user executes order lists by hand in their own Schwab account
(user decision 2026-10-07: first batch ≤ $10,000 per `configs/live.yml`); agents never place
orders, never hold or use brokerage credentials, and never raise that cap. The Schwab Trader API code (`configs/schwab_api.yml`, user decision
2026-10-08 to apply for access) ships with `read_enabled` and `orders_enabled` false; only the user
switches them, keys live only in the server's `.env`, and agents never set or read them. Study parameters are pre-registered: changing them after seeing results means a
new study version, recorded in `docs/rejected.md` or a new `research/<name>/`.
