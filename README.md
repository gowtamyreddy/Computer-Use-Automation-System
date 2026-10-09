# Computer-use automation: discover once, replay deterministically

An LLM operates a legacy back-office app to accomplish a goal. The successful run is recorded as a typed,
versioned **capability artifact**. That artifact is then **replayed with no model in the loop**, with
explicit handling of business outcomes, recoverable conditions and hard failures. A human can take over
the live session at any point and hand it back. Design and trade-offs are in [REPORT.md](REPORT.md).

- **Target**: *CoreLink Teller*, a deliberately hostile local stand-in for a core-banking UI. It uses
  framesets, table layouts, no test IDs, a session ID in the URL, vendor error codes, `confirm()` dialogs
  and injectable faults. All data is fictional.
- **Stack**: Python 3.11+, Playwright (Chromium), Pydantic, OpenAI function calling (discovery only).

## Setup

```bash
python -m venv .venv && source .venv/bin/activate      # or: uv venv && source .venv/bin/activate
pip install -e '.[dev]'
python -m playwright install chromium
cp .env.example .env                                    # then set OPENAI_API_KEY in .env
```

`.env` holds the OpenAI key (optionally `OPENAI_MODEL`, default `gpt-4.1`) and the *fake* sign-on for
the local demo app. Secrets are read from `CUA_SECRET_<TENANT>_<REF>` environment variables at the moment
of use. They are never written to artifacts or logs.

## Demo path

One command regenerates everything in `/evidence`: two real discovery runs, then a matrix of replays.

```bash
python scripts/run_demo.py
```

Or step by step:

```bash
# 1. start the target app (tenant "demo"); in another terminal:
cua app --port 8765

# 2. discovery: the LLM drives the app and records a capability (operator console on :8780)
cua discover --headed \
  --goal "Look up member {member_id} and read their current Share Savings balance" \
  --param "member_id=12345::5-digit member number" \
  --capability-id corelink.member.read_savings_balance
#   -> artifacts/corelink.member.read_savings_balance/1.0.0.yaml   evidence -> runs/discovery-*/

# 3. deterministic replay (no LLM) with different inputs
cua replay artifacts/corelink.member.read_savings_balance/1.0.0.yaml --param member_id=23456   # succeeded + outputs
cua replay artifacts/corelink.member.read_savings_balance/1.0.0.yaml --param member_id=99999   # business_outcome RECORD_NOT_FOUND

# 4. runtime conditions (fault injection is a demo-harness endpoint, outside the agent's allowlist)
cua faults notice_once=1     && cua replay ... --param member_id=12345   # recovered: INTERSTITIAL_NOTICE
cua faults expire_after=6    && cua replay ... --param member_id=12345   # recovered: SESSION_EXPIRED (re-sign-on + restart)
cua faults error_once=1      && cua replay ... --param member_id=12345   # failed: APP_ERROR (+ screenshot, DOM, expected/observed)

# 5. human handoff: same failure, but escalate to an operator who takes over the live browser
cua faults error_once=1
cua replay artifacts/corelink.member.read_savings_balance/1.0.0.yaml --param member_id=12345 --escalate --headed
#   open http://127.0.0.1:8780 -> "Take control" -> redo the lookup in the browser window -> "skip_step"
#   automation resumes on the same session, extracts the balance and verifies the checkpoint.
#   Headless alternative: add --debug-port 9222 and run
#   python scripts/operator_bot.py --cdp 9222 --lookup 12345 --resolution skip_step

# 6. same capability on another tenant running the same vendor product (relabelled screens)
cua app --port 8766 --variant lakeside
cua replay artifacts/corelink.member.read_savings_balance/1.0.0.yaml --tenant lakeside --param member_id=12345
cua replay artifacts/corelink.member.read_savings_balance/1.0.0.yaml --tenant lakeside-unmapped --param member_id=12345  # works, flags LOCATOR_FALLBACK drift

# 7. agent-facing interface
cua catalog                                                                      # capabilities as tool definitions
cua invoke corelink__member__read_savings_balance --args '{"member_id": "12345"}'
cua approve artifacts/corelink.member.read_savings_balance/1.0.0.yaml --reviewer you   # draft -> approved
```

Replay prints the result contract as JSON (`succeeded | business_outcome | failed | rejected`). The exit
code is 0 for succeeded or business_outcome, 1 for failed and 2 for rejected.

## Running without live services

Only discovery needs a model. Everything else runs locally against the stand-in app:

```bash
pytest -q          # ~50 s: 48 tests, real browser + real app, no API key
```

The tests cover the replay result contract for every runtime condition, redaction of all evidence,
policy, overlays, and the control lease with real takeover of the live browser over CDP. The discovery
loop and recorder are exercised end to end with a *scripted* stand-in for the model (`ScriptedModel`,
tests only). The genuine LLM runs are in `/evidence/discovery/`.

## Layout

```
cua/schema.py            capability artifact schema (the contract)       cua/replay.py        deterministic replay engine
cua/discovery.py         LLM observe->decide->act loop                   cua/recorder.py      trace -> artifact, record-time locator validation
cua/surface/             Surface protocol, Playwright impl, perceive.js  cua/result.py        replay result contract
cua/control.py           control lease + interventions                    cua/operator_console.py  minimal operator UI/API
cua/config.py            policy guard, app profile, tenants, secrets     cua/redact.py        redaction
cua/overlay.py           tenant overlays                                  cua/evidence.py      run logs / screenshots / DOM
profiles/                vendor app profiles   config/  policy + tenants   overlays/  tenant overlays
target_app/server.py     CoreLink stand-in     scripts/  demo + operator bot   evidence/  demo output
```
