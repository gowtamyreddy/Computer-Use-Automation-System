# Design write-up

The model discovers a flow once. The flow is saved as a typed capability artifact. Deterministic replay
is how an agent runs that capability in production. Everything below is built around that.

The target is **CoreLink Teller**, a deliberately hostile local stand-in for a legacy core-banking UI
(`target_app/server.py`). It uses a `<frameset>`, table layouts, labels in neighbouring `<td>`s, no ARIA
and no test IDs. Field names are opaque (`F0231`) and the session ID travels in the URL. Errors are red
text with vendor codes, and it uses native `confirm()` dialogs. Faults can be injected out of band:
interstitial notices, transient "busy" pages, session expiry, host crashes and slowness. It can also be
served as a second tenant with the same product, rebranded and relabelled.

## 1. Architecture

```
            discovery (LLM)                         replay (no LLM)
goal ──► DiscoveryAgent ──► Recorder ──► artifact.yaml ──► ReplayEngine ──► ReplayResult
            │   ▲                             ▲                 │
            ▼   │ observe/act                 │ overlay         ▼
        ┌──────────── Surface protocol ───────┴───────────────────┐
        │ WebSurface (Playwright + perceive.js in every frame)    │ ← DesktopSurface would go here
        └─────────────────────────────────────────────────────────┘
   PolicyGuard (allowlist, risk) · Redactor · RunLog (evidence) · ControlChannel (lease) + operator console
   AppProfile (vendor knowledge: login, state rules, risk rules) · Tenant config (+ overlay)
```

Key decisions:

- **Single process, synchronous, plain files.** Each run is one live session with one control lease.
  Queues and services would add nothing to the decisions being evaluated here. Artifacts are YAML in
  git-friendly paths (`artifacts/<id>/<semver>.yaml`), so review happens as a diff.
- **Perception is accessibility-style, not DOM-selector-based.** `perceive.js` describes each element the
  way an operator perceives it: role, accessible name, the *visual* label beside it (taken from the
  neighbouring table cell), grid coordinates (column header plus row key) and visible text. Discovery
  and replay share this code. Something recorded under these rules is resolved under the same rules,
  and that is the main source of determinism. The same descriptors exist in UIA and AX trees, which is
  why this approach generalises to surfaces without a clean DOM (§4).
- **Text observation by default, screenshots optional (`--vision`).** On this class of app everything
  needed is in the perceived tree. Text is cheaper and auditable, and it lets PII be masked by label
  before anything is sent to the model. Vision becomes the primary channel only for pixel-only surfaces
  such as Citrix or terminal emulators.
- **The model is kept on a short leash.** Each turn is rebuilt from scratch: goal, compact action
  history and current screen. There is no growing chat. The model picks one tool call per turn. It never
  sees credentials, because the app profile signs on. It never sees input values: it fills `param:
  member_id`, and the goal it reads is `Look up member {member_id}…`. Known host states such as
  interstitials and session expiry are handled by the app profile, not the model, so the recorded flow
  contains only the intended path.
- **OpenAI function calling** (`gpt-4.1` by default, configurable). The loop is provider-agnostic behind
  `DecisionModel.decide()`.

Trade-offs: synchronous Playwright makes the "pause and keep the browser alive for a human" seam simple,
but one process holds one session. The recorder keeps every successful action, so a detour the model
takes will appear in the artifact. It isn't minimised automatically; review catches it (§7).

## 2. Artifact schema

`cua/schema.py` (Pydantic, versioned `schema_version: "1.0"`). An artifact is a **capability contract
first, a step list second**:

| Section | Purpose |
|---|---|
| `id`, `version` (semver), `status` (draft → approved → deprecated), `app` (product, version range, surface, profile), `provenance` (run id, model, redacted goal, tenant, human interventions) | What it is, where it applies, and whether it may run unattended |
| `inputs[]`: name, type, pattern/enum, **sensitivity** (default `pii`) | What the caller must supply. Validated before any UI is touched |
| `outputs[]`: name, type (`money`/`decimal`/`integer`/`string`), sensitivity | What the caller gets back, parsed and typed |
| `outcomes[]`: capability-specific state rules | Business outcomes this capability can return, in addition to the app profile's |
| `steps[]`: `id, action, intent, risk, target, value/output, expect[], timeout_ms` | The flow. `intent` is the model's rationale in operator language. `risk` is `read`/`safe`/`irreversible`. `expect` holds post-conditions |
| `success[]` | The checkpoint, verified before outputs are returned |
| `review_notes[]` | Anything the recorder couldn't validate, for the human reviewer |

**Targets** have an ordered list of strategies plus a structural fingerprint:
`role_name` → `label` (visual label) → `table_cell` (column + row key) → `text` → `css`.
The first four are surface-neutral. `css` is a web-only escape hatch: always ranked last, and a target
may not consist of CSS alone (schema-enforced). At record time every candidate locator is resolved
against the live page with the replay resolver. A locator is kept only if it resolves to *exactly* the
element the model acted on, so a locator that is ambiguous at record time is dropped then rather than
discovered in production. Extract targets never get a `text` locator, because their text is the data.

**Values** are `param` / `literal` / `secret`. Secrets are refs that only the app profile's login may use
(schema-enforced). Literals must pass the redactor unchanged, or the artifact is refused at save time.
`expect` holds `url_matches` conditions (frame, path with `{param}`/`*`, and only the query keys that
carry an input, so session IDs never appear), plus `element_present`, `text_present` and `dialog`.
Each capability also renders as an OpenAI tool definition (`cua catalog`, `cua invoke`) that lists
its typed inputs, outputs and possible business-outcome codes.

## 3. Determinism & error handling

The replay engine (`cua/replay.py`) follows four rules on every run:

1. **Resolve, don't search.** Strategies are tried in order. One is accepted only if it yields exactly
   one visible element that matches the fingerprint (tag, input type). Ambiguity never means "pick the
   first". When a fallback strategy wins, the run still succeeds and a `LOCATOR_FALLBACK` warning names
   the step. That warning is the drift signal.
2. **Wait on conditions, not time.** After each action the engine polls until the step's post-conditions
   hold. On *every* poll it also classifies the screen against state rules. A slow host is just a longer
   wait.
3. **Classify every non-happy state.** Rules come from the capability first, then the vendor app
   profile (`profiles/corelink-teller.yaml`), ordered by priority:
   - **business outcome**: `RECORD_NOT_FOUND`, `ACCESS_DENIED`, `VALIDATION_REJECTED`. Stop and
     return `status: business_outcome` with a code. These are answers, not crashes.
   - **recoverable**, with a bounded handler: `dismiss` (interstitial notice), `accept_dialog` (known
     informational `confirm()`), `retry_flow` (transient busy page), `reauthenticate` (session
     expired). Restarts happen only while no irreversible step has executed (`UNSAFE_TO_RESTART`), and
     each rule has a maximum attempt count (`RECOVERY_EXHAUSTED`). Handled conditions appear in
     `recoveries[]`, not as failures.
   - **failure**: `APP_ERROR`, etc. An *unclassified* dialog is dismissed and becomes
     `UNEXPECTED_DIALOG`. A state that matches nothing ends as `POSTCONDITION_TIMEOUT`,
     `TARGET_NOT_FOUND`, `AMBIGUOUS_TARGET` or `FRAME_NOT_FOUND`.
4. **Verify the checkpoint** before returning outputs (`CHECKPOINT_FAILED` otherwise).

**Result contract** (`cua/result.py`): `succeeded | business_outcome | failed | rejected`. `rejected`
means refused before the UI was touched: bad inputs, an irreversible step not permitted, or a deprecated
capability. That's a caller or configuration bug, not an incident. Every `failed` result carries the
step id and intent, `expected[]`, `observed` (frame URLs, a redacted rendering of the screen, and
per-strategy match counts), evidence paths (a masked screenshot and a redacted DOM) and `retryable`.
The full scenario matrix is tested in `tests/test_replay.py`, and the same scenarios run end to end
through the CLI in `evidence/SUMMARY.md`.

Retrying a single step is deliberately not offered. On legacy hosts it means resubmitting a POST or
working from a lost form state. Restarting a flow that has executed no irreversible step is safe and
deterministic.

## 4. Heterogeneity & multi-tenant

**The surface seam.** Discovery, the recorder and replay talk only to the `Surface` protocol
(`cua/surface/base.py`): `observe`, `resolve(Target)`, `click/fill/select/press/read`, `frame_states`,
masked `screenshot`, `ui_snapshot`, `pump` (keep alive while a human drives) and event hooks for dialogs
and human input. The artifact doesn't know how a `Target` is resolved.
- *Legacy web* is the implemented case. Frames are addressed by name chain, labels come from table
  cells, data comes from grid coordinates.
- *Desktop*: a `DesktopSurface` over UIA (Windows) or AX (macOS) maps `FrameRef.path` to a window/pane
  path. `role_name` maps to ControlType + Name and `label` to the labelled-by relation or the nearest
  static-text sibling. `table_cell` maps to the Grid/Table patterns and `css` is unsupported (that's why
  it can't be a target's only strategy). The conditions map to window title, element presence and
  static text.
- *Pixel-only* surfaces (Citrix, emulators): the same locator kinds are resolved by OCR plus layout over
  a screenshot. Bounded LLM grounding would be a last-resort strategy kind, logged and never silent.

**Multi-tenant reuse** uses three layers, so N tenants on one product don't mean N recordings:

1. **App profile**, one per vendor product (and version range): sign-on procedure, state rules (error
   codes, interstitials, dialogs), risk rules. It's written once and reused by every capability and
   tenant.
2. **Capability**, one per product: recorded once, reviewed once, and declaring `app.product` and
   `app.versions`.
3. **Tenant config + overlay** (`config/tenants.yaml`, `overlays/*.yaml`): base URL, secret namespace,
   and a *specialisation* layered over the shared capability at load time. `label_aliases` covers the
   common case (a relabelled screen) and `step_targets` covers structural differences. The base artifact
   is never forked.

This is demonstrated with a second tenant (`--variant lakeside`, CoreLink 4.3) whose screens rename
"Member #" to "Member No.", "Go" to "Search" and "Member Inquiry" to "Member Lookup". *Without* its
overlay, replay still succeeds through the stable vendor field-name fallbacks. It reports
`LOCATOR_FALLBACK` on each affected step, which is graceful degradation plus a drift signal. *With* the
overlay it runs clean. Drift management at scale follows from this: fallback warnings and failure codes
aggregated per (capability, tenant, product version) show which tenants need an overlay entry and which
product versions need a new capability version (semver). A version-range mismatch is caught before a
replay runs.

## 5. Escalation & handoff

**Detecting "stuck".** In discovery: the model calls `request_human`, the screen doesn't change after
N actions (`NO_PROGRESS`), three consecutive rejected actions (`REPEATED_ERRORS`), or the step/time
budget runs out. In replay: any hard failure when `--escalate` is set. Either way: an irreversible step
that needs approval.

**Control model** (`cua/control.py`). Each live session has one **lease**, held by `automation` or
`human`:
`AUTOMATION → raise(intervention) → AWAITING_HUMAN → claim → HUMAN → release(resolution) → AUTOMATION`.
Every automated action calls `require_automation()` first, so automation *cannot* act while a human holds
the session. The seam is enforced, not a convention. The intervention record carries the subject
(capability@version or goal), step id and intent, code, reason, a masked screenshot, redacted frame
URLs and the allowed resolutions. It's persisted as JSON and served by a minimal operator console
(`cua/operator_console.py`: `/api/state`, `/api/claim`, `/api/release`).

**Taking over the live session.** The human drives the *same* browser: the headed window locally, or
remotely over the Chrome DevTools endpoint (`--debug-port`) for a headless run. The automation thread
keeps the browser's event loop running while it waits. The operator's clicks and changes are captured
in-page by the same perception code and attached to the intervention, redacted. A typed value equal to
an input is recorded as `{member_id}`.

**Handing back.** The resolution is typed, which tells the engine *how* to resume: `retry_step`,
`skip_step` ("I did it"; continue with the next step), `completed` (verify the checkpoint and run the
remaining extracts), `resume` (discovery: the agent continues from the new state, with the operator's
actions and note in its history), `approve`/`deny`, or `abort`. Resolutions that claim the human acted
are rejected unless the human actually claimed control. In discovery, operator actions are **folded
into the artifact** as steps flagged `(operator)` with a review note, so the capability stays complete.
The demo and tests use `scripts/operator_bot.py`, which does exactly what a remote human would: claim
through the console API, drive the live page over CDP, then release.

Production design, not built: the intervention goes to a case queue or pager with SLAs and skills
routing, and the console becomes a co-browsing UI (CDP screencast, or VNC for desktop) with
authenticated operators. Every lease change goes to an append-only audit log. Lease expiry triggers
automatic abort.

## 6. Safety

- **Allowlist, enforced twice.** `PolicyGuard` checks every action type and every link target before
  acting, and a network route guard aborts any request outside the tenant origin and allowed path globs
  (`config/policy.yaml`). `/admin/*`, the fault endpoint and sign-off are denied. The demo shows the
  model's click on "System Admin" being rejected.
- **Risky actions.** Risk is classified from the profile's rules (control names, plus *form action*
  targets such as `*/commit`) and a global name pattern (confirm, post, transfer, delete, …).
  Irreversible actions are never silent. In discovery they need operator approval per action (or are
  blocked, by policy). In replay they need `status: approved` **and** an explicit
  `--allow-irreversible`, or a per-run operator approval with `--escalate`. Otherwise the run is
  rejected before touching the UI. I chose *require approval* over *block* because the business value is
  often in the commit. Making the commit a deliberate, audited human decision preserves that value
  without letting a model's guess move money.
- **Data handling.** Credentials live only in the secret provider (env vars here, a vault in
  production) and are registered with the redactor when used. The model never sees credentials or
  input values. PII is masked *by label* (Name, SSN, DOB, Phone …), which catches names that no regex
  would, and by pattern (SSN, Luhn-valid PAN, phone, email, dates, money, `sid=` tokens). One
  redactor sits in front of every log line, transcript, DOM snapshot and result file. Screenshots are
  masked *in-page* before capture. Extracted outputs go to the caller but are persisted only as salted
  fingerprints, so a reviewer can verify that replay matched discovery without the value ever being
  stored. Artifacts pass a persistence guard and are refused if they would store anything the redactor
  would change.

**Limits.** A label-based mask only knows the labels it's told. A grid whose *row key* is a person's name
would expose it in the observation. Balances are visible to the model unless masked as money (they are
here). Discovery still sends screen content to a third-party model, so production needs a zero-retention
or self-hosted endpoint. Risk classification is heuristic plus profile rules, so an unlabelled
"commit" control the rules miss would be classified safe. That's why replay also requires
`approved` status and review of `risk:` on each step. The allowlist is path-based. Apps that multiplex
everything through one `servlet?action=` URL need action-level rules.

## 7. Cuts

Deliberately left out, and what I'd build next:

- **Desktop surface**: designed in §4, not built. Next: a UIA-backed `DesktopSurface` against a
  WinForms sample, reusing the schema unchanged.
- **Trace minimisation**: the recorder keeps every successful action. Next: drop actions with no effect
  on later steps, replay-verify the minimised flow, then emit it.
- **Bounded assisted fallback**: a one-step, policy-checked LLM re-grounding when every strategy fails,
  recorded as evidence and proposed back as a new locator. The `LOCATOR_FALLBACK` plumbing exists.
- **Approval workflow and reliability scoring**: `cua approve` sets status, but there's no signed review,
  and no N-run stability score gating `approved`.
- **Operator console**: functional but bare (polling HTML, no auth). The production design is in §5.
- **Scale plumbing**: no queue, service or multi-session runner, by design (§1).
- **Stretch goals done**: an agent-facing catalog/invoke surface (`cua catalog`, `cua invoke`) and
  cross-tenant reuse with per-tenant overlays (§4).
