# Message for Rork — wiring `/api/brief` + AI Cloud

Copy everything below this line.

---

There's a new backend endpoint. Read **`RORK_BRIEF.md` section 4b** in the repo
for the full contract — this message is just what to build.

## What changed

`GET /api/brief?flight=DL244&date=2026-08-16`

This replaces the "call a bunch of endpoints and figure it out" approach for
anything that needs a **judgement** about a flight. `/api/check` still exists
and still returns raw data from every source, but `/api/brief` is what you
want when the UI is answering "should I worry about this flight?"

It does three things before you ever see it:

1. Works out how many hours until departure.
2. Consults **only** the data sources that still carry signal at that horizon.
3. Runs the delay analysis deterministically and returns a verdict.

The horizon part matters. An FAA ground delay program happening right now says
essentially nothing about a flight leaving in 15 hours — those programs are
same-day. So at long horizons the endpoint deliberately does not fetch live
conditions, and tells you which sources it skipped and why. Don't work around
this by calling the individual endpoints yourself and merging — the exclusions
are the feature.

## Render these directly from JSON — do not send them through a model

- `verdict.departure_risk` — `LOW` / `MODERATE` / `HIGH`
- `verdict.confidence` — `LOW` / `MEDIUM` / `HIGH`
- `verdict.drivers[]` — short plain-language reasons
- `branch_classification.branch` — `A` / `B` / `NOT_APPLICABLE` / `UNDETERMINED`
- `branch_classification.branch_label` — human-readable version
- `horizon.hours_to_departure`, `horizon.band`

These are computed in Python and are always correct. Keep them on screen even
if the model call is slow or fails.

**Important UI distinction:** `LOW` risk at `LOW` confidence does **not** mean
"this flight is fine." It means "nothing is visibly wrong yet, and it's too
early to tell." A flight 15 hours out will almost always be LOW/LOW. Those two
states need to look different, or the app will be quietly reassuring people
about flights nobody has actually assessed yet. Show the confidence and the
horizon band, not just the risk color.

`branch_classification.branch === "NOT_APPLICABLE"` is the explicit "too early"
signal. Treat it as its own state rather than a risk level.

## Wire the AI Cloud for the narrative only

The response includes `llm_payload` with everything needed. All arithmetic is
already done — the model writes prose about numbers it is forbidden to
recompute.

```js
const brief = await fetch(
  `${BASE}/api/brief?flight=${flight}&date=${date}`
).then(r => r.json());

// Render the verdict immediately from brief.verdict — don't wait on the model.

const { system, user, facts } = brief.llm_payload;

const narrative = await ai.generate({
  model: <see below>,
  system,
  messages: [{
    role: "user",
    content: user + JSON.stringify(facts, null, 2),
  }],
});
```

Send `system` verbatim. It embeds the project's analytical methodology plus
guardrails — chiefly "every number in your answer must come from the facts
provided" and "sources marked `not_consulted` were deliberately excluded; do
not speculate about them, and do not treat their absence as reassuring." Don't
edit, summarize, or replace it with your own prompt.

Don't add the raw source data to the prompt either. `facts` already contains
the filtered set. Adding back the excluded sources reintroduces exactly the
error the horizon gating exists to prevent.

## Which model

The hard reasoning is already done in Python, so this does **not** need a
frontier reasoning model. The task is constrained synthesis: read a structured
fact set, write four to six sentences of plain English, respect the guardrails.

Pick a **fast mid-tier text model** — the tier below the flagship. Whatever the
current fast Claude, GPT, or Gemini mid-tier is will all handle this well.
Optimize for latency and cost, because this runs on every flight check.

Reasons not to reach for the biggest model:

- No math to do — the numbers are precomputed and the model is told not to
  derive new ones.
- Input is small: roughly 4–6k tokens of system prompt plus facts.
- Output is short: a few hundred tokens.
- A user is waiting on it, so latency is visible.

Skip "thinking" / extended-reasoning modes. There's nothing to reason about,
and it adds seconds for no gain.

Configure: temperature low (~0.3) for consistency, max output ~500 tokens.

Only move up a tier if you see the model inventing numbers or hedging past what
the guardrails allow. If you do see that, tell me — it more likely means the
prompt payload needs fixing than that the model is too small.

## Failure handling

The model call is **enhancement, not dependency**. If it errors, times out, or
returns empty, show the deterministic verdict and drivers with no narrative.
Never block rendering on it, and never show a spinner where the verdict should
be — the verdict is already available in the same response.

## Cost — don't poll this

`/api/brief` costs 2–4 FlightAware queries per call, and the monthly credit is
small and doesn't roll over. Make it user-initiated (pull to refresh, explicit
"check" action). Do not put it on a timer, and don't fire it for several
flights at once — there's a 10-queries-per-minute upstream limit.

It's cheaper the further out the flight is: 2 queries past 12 hours, 4 inside
12 hours. That's automatic, nothing to configure.
