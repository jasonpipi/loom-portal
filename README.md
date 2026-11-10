# loom-portal

The LOOM access portal: task-scoped, expiring access to a person's health
context for agents. LOOM decides which tier of the record an agent may
receive (L0 raw signal, L1 readings, L2 patterns, L3 constraints), for how
long, and with how much friction. Standard library only.

```bash
pip install "git+https://github.com/jasonpipi/loom-portal@v0.1.0"
loom-portal --port 8787 --seed-credit cambridge-clinic=85
```

## Rules (`loom-policy-0.1.0`)

| Task | Deepest tier | Fields | Validity | Category |
|---|---|---|---|---|
| shopping_list | L3 | constraints | once | agent_for_user |
| meal_plan | L2 | glucose_patterns, constraints | 7 days | agent_for_user |
| clinical_visit | L2 | glucose_patterns, alerts, constraints | 14 days | care_provider |
| anything else | | | | denied by default, no penalty |

- Overreach (deeper tier or extra field): blocked, publisher −20.
- Fetch after expiry: denied, −5. Clean grant: +1. Baseline credit 60.
- Credit sets friction only. At 80 or above: auto-renew, confirm only beyond the rules. From 50 to 79: confirm on first request. Below 50: confirm every time, one-time snapshot.
- Same `request_id` with the same content (receive time excluded) is a replay. Different content is a 409 conflict.

## API

`GET /health`, `POST /v1/<ns>/requests`, `POST /v1/<ns>/fetch`,
`GET /v1/<ns>/credit`, `GET /v1/<ns>/log`. A namespace is one isolated
state, created on first use.

The record is synthetic. Used by the Nanda Town integration in
`examples/loom/` (MIT AI Studio Fall 2026, HW8).

```bash
pip install -e '.[dev]' && python -m pytest -q
```
