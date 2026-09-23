# jroute v2: a plan-then-execute pipeline across Herdr panes

Supersedes the v1 model-router plan. Target: one Python file, stdlib only, no build step.

v1 answered "which model for this task". v2 answers what you actually asked for: **"implement this feature" runs a pipeline where a strong model writes the plan and different models execute it, each in its own pane.**

---

## 1. Verified facts (tested live on this machine)

### Quota is one read-only call per subscription

| Subscription | Source | Live result |
|---|---|---|
| Codex Plus | `GET https://chatgpt.com/backend-api/wham/usage`, token from `~/.codex/auth.json` | 10% used (5h window), 56% used (7d), reset times |
| Cursor Pro | `POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage`, token from keychain `cursor-access-token` | auto 73.2%, api 8.6%, total 67.3% |
| OpenCode Go | no usage endpoint exists (`/zen/go/v1/*` returns 404 on every path) | no API; model catalog only |

Cursor returns `autoPercentUsed`, `apiPercentUsed`, `totalPercentUsed`, and `autoBucketModels[]` (which models bill against the nearly-dry auto bucket). The API bucket is at **8.6%**, which is where this plan's frontier-model capacity comes from.

### Model catalogs (live)

- Codex Plus, 8: `gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`, `gpt-5.3-codex-spark`
- OpenCode Go, 30: `kimi-k3`, `qwen3.8-max`, `qwen3.7-max`, `glm-5.3`, `grok-4.7`, `minimax-m3`, `deepseek-v4.1-flash`, `gpt-5.6-luna`, and 22 more
- Cursor Pro, ~60: `claude-opus-5-high`, `claude-opus-5-5-max`, `claude-fable-5-thinking-xhigh`, `claude-sonnet-5-thinking-high`, `gpt-5.6-sol-high`, `gpt-5.3-codex-xhigh`, `gemini-3.7-flash-high`, `composer-2.5`, `cursor-grok-4.6/4.7` ladders

### Launch facts (from the verified `harness-adapters` table)

| Harness | Model flag | Effort flag | Busy signature | Exit | Interrupt |
|---|---|---|---|---|---|
| pi | `--model <provider>/<model>` | `--thinking <low\|medium\|high\|xhigh>` | `Working...` | `/quit` | single Escape |
| codex | `--model <model>` | `-c 'model_reasoning_effort="<low\|medium\|high\|xhigh>"'` | `esc to interrupt` | `/quit` | single Escape |
| opencode | `--model <provider>/<model>` | none on interactive launch | `esc interrupt` | `/exit` | double Escape |
| cursor | `--model <id>` (effort baked into the ID) | n/a | NOT VERIFIED | NOT VERIFIED | NOT VERIFIED |

Pi covers both Codex and OpenCode Go (`openai-codex/*`, `opencode-go/*`) and has the skills system this pipeline depends on. Cursor is not a pi provider, so it needs `cursor-agent`. **Two launch adapters, not four.**

### Herdr primitives (from the herdr skill)

```bash
herdr pane split --current --direction right --cwd "$PWD" --no-focus   # new id: .result.pane.pane_id
herdr agent start <name> --kind <kind> --pane <id> -- <agent-args>
herdr agent prompt <target> "<text>" --wait --timeout 300000
herdr agent wait <target> --until blocked
herdr agent send-keys <target> esc
herdr agent read <target> --source recent-unwrapped --lines 120
herdr pane close <pane_id>
```

Rules that matter: `agent start` requires an existing idle shell pane and never creates layout. Panes I did not create must never be closed. Repeated same-direction splits make columns unusably narrow. `idle` means ready for input; `done` is idle after unseen background work; `blocked` is an approval or question prompt.

### Jev

`POST https://api.typesafe.ai/v1/systemone`, `{model, state, questions}`, with `choice` / `score` / `noul` questions evaluated in one round trip. **No key on this machine yet** (no `TYPESAFE_API_KEY`, `AI_GATEWAY_API_KEY`, or `OPENROUTER_API_KEY`). Step 0.

---

## 2. The pipeline

A ticket reference or task text goes in. Three stages, three panes, sequential.

```
jroute run "implement CHP-1234"
   |
   |  ticket ref detected by regex ([A-Z]+-\d+, #\d+)
   |
   +-- STAGE 1: PLAN      pane: jroute-plan
   |     model: gpt-6-astra        (Codex Plus, effort medium)
   |     fallback: gpt-5.6-sol (Codex) then gpt-5.6-sol-high (Cursor api) then glm-5.3
   |     skills: to-spec, to-tickets
   |     in:  ticket text + repo context
   |     out: .jroute/plans/<slug>.md         <-- the handoff artifact, 120-line cap
   |
   +-- STAGE 2: EXECUTE   pane: jroute-exec
   |     model: deepseek-v4.1-flash (OpenCode Go, flat rate, effort low)
   |     fallback: glm-5.3-flash, qwen3.8-flash, then gpt-5.4-mini-medium (Cursor api)
   |     skills: implement, tdd
   |     in:  the PATH to the plan file, not the plan text
   |     out: commits on the feature branch
   |
   +-- STAGE 3: REVIEW    pane: jroute-review
         model: glm-5.3             (OpenCode Go, flat rate, different family than executor)
         fallback: qwen3.7-max, kimi-k3-high, then gpt-5.6-sol-high (Cursor api)
         skills: code-review
         in:  merge-base + plan file path
         out: findings appended to the plan file
```

### The one rule that makes this work: the handoff is a file path, not a conversation

Panes do not share context. Nothing typed into the plan pane reaches the execute pane. So the plan artifact on disk **is** the interface, and every stage brief carries a path, never inlined text. This is exactly the convention the `handoff` skill already uses: write to a file, reference existing artifacts by path instead of duplicating them, and include a **suggested skills** section naming what the next agent should load.

That last part is the elegant bit: the plan file tells the execute pane which skills to invoke, so the router does not need to hardcode it.

### Stage briefs

Stage 1 (plan pane):

```text
Read ticket CHP-1234 with gh-axi. Explore the repo. Produce a plan at
.jroute/plans/chp-1234.md with: acceptance criteria, files to change,
test seams, and a "suggested skills" section for the implementer.
Use /to-spec and /to-tickets. Do not write product code.
```

Stage 2 (execute pane):

```text
Implement the plan at .jroute/plans/chp-1234.md. Load the skills it lists
in "suggested skills". Use /implement and /tdd at the seams it names.
Run typecheck and tests. Commit to the current branch. Do not rewrite the plan.
```

Stage 3 (review pane):

```text
Run /code-review since the merge-base. Compare against the plan's acceptance
criteria at .jroute/plans/chp-1234.md. Append findings to that file.
Report only actionable findings.
```

### Pane lifecycle

- One pane per stage. Created on stage start with `pane split --current --direction right --cwd "$PWD" --no-focus`, so your focus never moves.
- Stage names: `jroute-plan`, `jroute-exec`, `jroute-review` (must match `[a-z][a-z0-9_-]{0,31}`).
- Close on stage success: `herdr pane close <id>`. Keep on failure so you can read the pane.
- `--keep-panes` skips the close, for when you want to watch.
- Because stages are sequential, panes are too. At most two live panes (driver plus current stage). This is also what keeps columns readable, given that repeated same-direction splits get narrow.
- Track every pane ID I created. Never close one I did not create.

### Supervision

Reuse the `stuck-crewmate-recovery` ladder rather than inventing one:

1. `herdr agent get` and `herdr agent read --source recent-unwrapped` to peek.
2. Answer a known question with one corrective line via `agent prompt`.
3. Interrupt with the harness's key (single Escape for pi/codex, double for opencode), then redirect.
4. Genuinely wedged: exit with the adapter's exit command and relaunch that stage with "progress so far" appended to the brief.
5. Second failure: mark the stage failed, print the evidence, leave the pane open for inspection.

Low context is not wedged. Modern harnesses auto-compact.

---

## 3. The quota gate you asked for

```text
Codex eligible = primary_5h_used   < 70
             AND secondary_7d_used < 85
             AND rate_limit.allowed == true
             AND limit_reached == false
```

When false, every Codex model is deleted from the candidate set **before** Jev is called, so Jev cannot pick one because it never sees one.

**Two levels of Codex gating, and why the second one matters.** Codex's `wham/usage` returns both account windows (`primary_window`, `secondary_window`) and a per-model `model_usage` map with an `available` flag and `available_at` timestamp. So there are two distinct gates:

1. **Account gate.** 5h over 70% or 7d over 85% removes every Codex model, Astra and Sol alike.
2. **Per-model gate.** `model_usage["gpt-6-astra"].available == false` removes only Astra, which is the case the `gpt-5.6-sol` fallback exists for. Astra is a premium tier and can be independently capped while the account windows still have headroom, and `credits_would_enable` tells you whether credits would unlock it.

**The gap: Astra and Sol are both Codex.** When gate 1 trips, the requested planning chain has no model left, because both its links live on the same subscription. So the planning chain needs a third link outside Codex:

```
gpt-6-astra            Codex Plus
gpt-5.6-sol            Codex Plus, when Astra's own flag is closed  <-- same subscription
gpt-5.6-sol-high       Cursor api bucket, when the Codex account is gated
glm-5.3                OpenCode Go, flat rate, last resort
```

The third link is the same model you chose as fallback, reachable through a different subscription. That is the point: it survives a Codex-wide lockout without changing model family, and Cursor's API bucket is 91% idle.

---

## 4. Which Pi skills the router uses, and where

80 skills are installed. These are the ones that actually carry weight here, and the mapping is not decoration: each replaces code I would otherwise have to write.

| Skill | Where it lives | What it replaces |
|---|---|---|
| `herdr` | The pane layer | All layout, agent-start, prompt, wait, and close logic. Its rules (do not close panes you did not create, avoid repeated splits, `--no-focus` for background) become the router's invariants. |
| `harness-adapters` | Model and effort flags, busy signatures, exit and interrupt keys | The per-CLI launch matrix. Verified table, so no guessing at effort flags. |
| `handoff` | The plan artifact contract | The whole cross-pane context transfer design. Path-based references plus a "suggested skills" section. |
| `to-spec`, `to-tickets` | Plan pane | Turning the ticket into a plan and tracer-bullet work items with blocking edges. |
| `implement`, `tdd` | Execute pane | The execution brief. `/implement` already says: TDD at agreed seams, typecheck regularly, full suite once, then `/code-review`, then commit. |
| `code-review` | Review pane | Two-axis review (Standards and Spec) as parallel sub-agents, comparing the diff to the originating spec. |
| `stuck-crewmate-recovery` | Supervision loop | The escalation ladder for a stuck stage. |
| `gh-axi` | Plan pane, ticket fetch | Reading the GitHub issue or PR behind the ticket ref. |

Deliberately not used: `ralphex` (CMUX-specific orchestration, this is Herdr), `orchestration` (Orca-specific), `loop-me`, `afk`, everything chip1- and content-related.

Two skills worth noting as *later* additions once the pipeline works: `design-an-interface` (parallel sub-agents proposing radically different interfaces, useful as an optional design stage ahead of planning) and `to-issues` / `to-tickets` fan-out for parallel execution panes.

---

## 5. Your five questions, answered for this project

| Question | Answer |
|---|---|
| Who is it for? | You, one user, personal use. Not a SaaS, not a team platform. |
| How are agents registered? | Predefined. Read from the three live catalogs. No user-configured agents, no external APIs. |
| What does it support initially? | A fixed three-stage pipeline (plan, execute, review) with agent and model selection. Not general decomposition. |
| How do agents execute? | Herdr panes running real agent CLIs, with pi as the default harness for Codex and OpenCode Go, and cursor-agent for Cursor. |
| Success criterion? | No stranded quota, lower cost per feature, and a log that explains every routing decision. Quality is measured later against the eval set. |

---

## 6. What to leave out of the pasted architecture, and why

The plan you pasted is scoped as a product: Next.js frontend, Postgres plus Prisma, BullMQ plus Redis, Langfuse, an evaluation dashboard, a 200 to 500 request eval dataset, and three competing router implementations. Every one of those is defensible for a commercial multi-tenant router. None of them are needed to route your own feature work across three subscriptions, and each one would delay the part that actually pays off.

Specifically dropped:

- **Agent registry as a database-backed subsystem.** Your three catalogs are live HTTP. A JSON file with capability ratings is the registry.
- **Next.js frontend and a dashboard.** The decision card printed in the pane plus `jroute log` covers inspection. A dashboard is not where the value is.
- **Postgres, Prisma, BullMQ, Redis.** One JSONL file and sequential subprocess calls.
- **Three router implementations compared against a 200 to 500 request dataset.** Real. Also premature. Log the first 50 real decisions, then compare. You cannot build a good eval set before you know what your routing mistakes look like.
- **`Router` interface with three implementations.** This one I would keep the *idea* of, in the cheapest form: one function, `decide()`, in its own file, with Jev behind it. Fallback when Jev is down is a real requirement, but it is one branch, not a class hierarchy.
- **Prompt injection, secrets, idempotency, budgets.** Keep: redact task text before sending it to Jev (the state goes to a third party), keep keys in the keychain, and never let a failed stage auto-retry a write. The rest is for multi-tenant.

Also worth flagging honestly: Jev launched recently, so pin a model version (`jev-1.13.0`, not `jev-latest`) and keep the call behind one function. Its API and pricing may move.

---

## 7. Improvements worth taking

Ranked by value per line of code.

1. **Use a different model for plan than for execute.** A plan written and executed by the same model inherits that model's blind spots. Cross-model handoff is free diversity, and it falls out of this pipeline naturally.
2. **Path-based handoffs only.** Already the design. The failure mode to refuse: inlining the plan text into the execute brief. It doubles the cost, loses the artifact, and makes review impossible.
3. **Let the plan file name its own skills.** The "suggested skills" section means the execute pane's skills come from the planner's judgment about the work, not from a hardcoded list in the router.
4. **Gate stages, not requests.** A stage is done when its artifact exists: plan file with acceptance criteria for stage 1, green tests for stage 2, findings appended for stage 3. Without gates, the pipeline happily marches on from an empty plan.
5. **Quota excludes, capability ranks.** Never mix them, or one hot pool silently downgrades everything.
6. **Per-bucket Cursor gating.** auto 73% versus api 8.6% means cheap frontier capacity still exists. Account-level gating throws it away.
7. **Effort is where the cost is.** `low` to `xhigh` on one model is over 10x. One model step down beats an unbounded effort step up.
8. **Do not downgrade when a reset is close.** Codex at 72% with the 5h window resetting in 20 minutes means waiting is strictly better than switching.
9. **Fan out execute panes once the plan has independent tickets.** `to-tickets` produces tracer-bullet slices with blocking edges, so independent ones can run in parallel panes. This is where multiple live panes actually earn their keep. Defer until the sequential pipeline is reliable.
10. **Log every decision from day one.** One JSONL line per stage: task hash, stage, chosen model and effort, Jev answers with confidences, quota snapshot, exclusions. Thresholds tuned on 50 real decisions beat thresholds reasoned from first principles.

---

## 8. Model assignment: Astra for planning, flat rate for everything else

Constraint from the user: do not use top frontier models. Route efficiently. Every entry below is a mid or cheap tier model you already have.

### The pool that matters most

Cursor's `autoBucketModels` lists `composer-1`, `composer-1.5`, `composer-2`, `composer-2.5(-fast)`, `vega*`, `grok-4.5`, `cursor-grok-4.5*`. Everything else bills to the API bucket.

So: the auto bucket is at **73.2%**, and `composer-2.5` lives inside it. The API bucket is at **8.6%**, and `gpt-5.4-mini`, `gpt-5.4-nano`, `gemini-3.6-flash`, `kimi-k3*`, `glm-5.2*`, `claude-4.5-sonnet`, and `gpt-5.1` all sit outside the auto bucket. **The cheap models on Cursor are mostly in the nearly-untouched pool, and the obvious volume pick from v1 (`composer-2.5`) is in the hot one.** That correction alone is worth more than any confidence threshold.

### Pool economics, and why flat rate reorders everything

| Pool | Marginal cost per token | Where it belongs |
|---|---|---|
| OpenCode Go | **zero, flat rate** | The highest-token work. This is the whole reason execute and review live here. |
| Codex Plus | metered against plan windows (56% of 7d used already) | Planning only, and only on Astra plus its fallback. |
| Cursor Pro api bucket | metered, 8.6% used (91% idle) | Overflow when a pool is gated. |
| Cursor Pro auto bucket | metered, 73.2% used | Effectively closed. Avoid. |

A flat-rate pool inverts the usual advice: for OpenCode Go stages, token efficiency stops being about money and becomes about latency and about staying under the plan's undisclosed cap. Token discipline still matters there, just for a different reason.

### Routing chains per stage

```
PLAN
  gpt-6-astra            Codex Plus         effort medium
  gpt-5.6-sol            Codex Plus         Astra's per-model gate closed
  gpt-5.6-sol-high       Cursor api bucket  Codex account gated
  glm-5.3                OpenCode Go        flat, last resort

EXECUTE
  deepseek-v4.1-flash    OpenCode Go   flat, default
  glm-5.3-flash          OpenCode Go   flat
  qwen3.8-flash          OpenCode Go   flat
  gpt-5.4-mini-medium    Cursor api    OpenCode Go cap hit
  gpt-5.6-luna           Codex Plus    last resort

REVIEW
  glm-5.3                OpenCode Go   flat, different family than the executor
  qwen3.7-max            OpenCode Go   flat
  kimi-k3-high           OpenCode Go   flat, when more depth is needed
  gpt-5.6-sol-high       Cursor api    OpenCode Go cap hit
```

The review stage must be a different model family than the executor. `glm-5.3-flash` executing and `glm-5.3` reviewing is the same family twice, so pair `deepseek-v4.1-flash` execution with `glm-5.3` review, or the reverse.

### Excluded

- **Every `claude-*` model, everywhere.** No Anthropic, per your instruction. That covers `claude-opus-5*`, `claude-opus-5-5-*`, `claude-sonnet-5*`, `claude-fable-5*`, `claude-4.5-sonnet`, and `claude-4-sonnet` on Cursor.
- **The frontier shelf, except Astra for planning.** `gpt-6-sol`, `gpt-6-luna`, `gpt-5.3-codex-xhigh`, and the `vega` family on Cursor stay out of the candidate set.
- **The Cursor auto bucket**, since `composer-2.5` and the `grok-4.5` ladders sit inside a pool that is 73% consumed.

### The one-liner

> Plan on `gpt-6-astra` with `gpt-5.6-sol` as the in-subscription fallback and `gpt-5.6-sol-high` on Cursor's idle API bucket as the out-of-Codex one; execute for free on `deepseek-v4.1-flash` or `glm-5.3-flash`; review on `glm-5.3` or `qwen3.7-max` from a different family than the executor.

### Escalation rule: start cheap, escalate on evidence

Do not ask Jev to predict whether a task is hard. Prediction costs a call per stage and is the least reliable part of a router.

1. Start execute and review at their default (tier 0 for execute, mid for review).
2. Escalate one step along that stage's chain only on evidence: tests fail twice, the agent reports `blocked` with a question the brief cannot answer, the plan file lacks acceptance criteria, or Jev answers `noul > 0.7` on the single question "did this attempt fail for a reason a stronger model would fix?".
3. Stop at the end of the chain. Never auto-escalate to a frontier model other than the planning default you specified.

Planning is the deliberate exception: it starts at `gpt-6-astra` rather than cheap, because one shot per feature makes a failed planning attempt expensive. Everything downstream of it starts cheap and climbs only on evidence.

---

## 9. Token efficiency (measured, not assumed)

Measured on the session that produced this plan, read from the pi session JSONL:

```
input        58,976
output       45,623
cacheRead 1,896,576
```

`cacheRead` is 32x the fresh input and 42x the output. The dominant token cost of an agent session is **re-reading its own accumulated context on every turn**, not generating text and not the model's price per token. That reorders every efficiency lever:

1. **Turn count beats model size.** Every turn re-reads the whole prefix. An agent that takes 30 turns to land a change pays 30 prefix reads. So the highest-value lever is making the executor right on its first attempt, which is a plan-quality problem, not a model-choice problem.
2. **Effort level beats model choice.** Thinking tokens are output tokens and are billed as output. `--thinking low` on a tier-1 model usually beats `--thinking xhigh` on a frontier one for bounded work, on both cost and wall clock. Pi exposes this for every provider, which is the main reason to launch Codex and OpenCode Go models through pi rather than through their own CLIs: opencode's interactive launch has no verified effort flag at all, so on that path the effort lever does not exist.
3. **Never inherit a long session across stages.** A fresh pane with a fresh session and a small brief is cheaper than continuing the planner's 40-turn history. There is no cross-model prompt cache sharing, so a new model always re-reads its prefix cold at full input price.
4. **The plan is a cache replacement.** The executor starts cold, so the plan must carry exactly what it needs (file paths, seams, constraints, acceptance criteria) and nothing else. Paying output tokens once for a precise plan saves the executor a dozen exploration turns. A vague plan is the expensive option, and a bloated one is worse still.
5. **Cap the artifact.** Fixed skeleton plus a line budget, about 120 lines. Plans are for decisions, not prose.
6. **Ban filler in every brief.** No preamble, no restating the task, no end-of-turn summary, report the artifact path only. Summary prose is routinely a large share of an agent's output tokens and carries no information.
7. **Skip the Jev call when the route is already determined.** Stage, tier, and gate results usually fix the model on their own. Route to Jev only when the tier is genuinely ambiguous, which for a ticket with written acceptance criteria is rare. Zero routing tokens for the common case.
8. **Cap Jev's state.** Task text plus a short repo digest, never file contents. Jev input is billed and latency scales with it.
9. **Measure per stage.** pi session JSONL exposes per-message usage (input, output, cacheRead, cost) and can be read after each stage. Log it, and a per-stage token budget becomes an enforced gate instead of a hope.

### What this changes in the recommendation

Choosing `gpt-5.4-mini` over `gpt-5.6-sol` saves a few times over on one stage. Keeping the executor at 6 turns instead of 20 saves a few times over on *every* turn of that stage, and the plan is what controls the turn count. Both matter. The second is bigger, and it is the one this pipeline shape uniquely enables.

### Stage config with effort

| Stage | Model | Effort | Why |
|---|---|---|---|
| PLAN | `gpt-6-astra` (Codex Plus) | `medium` | You asked for Astra here. Medium is the token discipline: the plan skeleton constrains the output far more than `xhigh` thinking does, and thinking bills as output. |
| EXECUTE | `deepseek-v4.1-flash` or `glm-5.3-flash` (OpenCode Go) | `low` | Bounded change against a written plan, on a flat-rate pool. Low effort costs nothing to try. |
| REVIEW | `glm-5.3` or `qwen3.7-max` (OpenCode Go) | `medium` | Different family than the executor. `/code-review` is a structured checklist, so medium suffices. |
| escalation | `gpt-5.6-sol` (Codex) or `gpt-5.6-sol-high` (Cursor api) | `high` | Only after two failed attempts or a `blocked` agent. |

### Token budget: warn, do not abort

Per stage, warn at a configurable threshold (start at 150k tokens summed across input plus output plus cacheRead for that stage's session). On breach:

- Print a warning on the stage line and continue the stage.
- Log the actual total to the decision record so the overspend is visible next to the route that produced it.
- Do not interrupt the pane. A hard abort can leave a half-applied edit, which costs far more to recover than the overspend it prevents.

Read the per-stage number from the pi session JSONL (per-message `input`, `output`, `cacheRead`, `cost`), which is verified to exist. Cache read dominates, so report it separately rather than folding it into one total, otherwise a normal long stage looks like a budget breach.

---

## 10. Delivery phases

**Phase 0: Jev access.** Get a key, confirm one successful `systemone` call. Nothing else can be tested without it.

**Phase 1: `jroute status` plus pane smoke test.** Three quota probes in a table with eligibility and reasons. Then prove the pane layer end to end by hand: split a pane, `agent start` pi and cursor-agent with a model flag, prompt, read, close. Also verify the two unknowns below. Check: the Codex row matches raw `wham/usage`.

**Phase 2: gates and candidates.** `--dry-run` prints eligible models per stage with exclusion reasons. Check: set `primary_5h_max: 5`, confirm Codex vanishes and the configured fallbacks move up.

**Phase 3: the plan stage alone.** Ticket ref detection, pane creation, plan brief, wait, verify the plan file exists and has acceptance criteria, close the pane. This is the first genuinely useful slice: one command that plans a ticket in its own pane on a strong model.

**Phase 4: the handoff and execute stage.** Read the plan path, launch the execute pane on a different model, wait for tests, close. Check: the execute brief contains a path and not the plan text.

**Phase 5: review stage, supervision, and the decision log.** Add the `stuck-crewmate-recovery` ladder so an unattended run self-corrects. One JSONL line per stage.

**Phase 6: tuning and optional fan-out.** Revisit thresholds and the capability table using 50 logged decisions. Add parallel execute panes only if sequential execution proves reliable.

### Two things to verify in Phase 1 before relying on them

- `cursor-agent` busy signature, exit command, and interrupt key. The `harness-adapters` table does not cover Cursor, so stage 2 and 3 supervision on a Cursor model is unverified until you check it.
- That `herdr agent start --kind pi -- --model openai-codex/gpt-6-astra --thinking medium` accepts the flags in that position, and the same for `--kind cursor -- --model gpt-5.6-sol-high`.

---

## 11. Files

```
~/Code/jroute/
  jroute.py           # probe, gate, ask Jev, launch stages, supervise, log
  config.json         # gates, per-stage model and fallback, capability table, weights
  .jroute/plans/      # plan artifacts (the handoff interface)
  .jroute/decisions.jsonl
```

```
jroute run "implement CHP-1234"        # full pipeline
jroute run "..." --stage plan          # one stage only
jroute run "..." --no-focus --keep-panes
jroute run "..." --dry-run             # no Jev call, no panes
jroute status                          # three quota pools, resets, eligibility
jroute log                             # last N decisions
```

`--dry-run` is how you tune thresholds without burning quota or spawning panes. `--stage` is how you use the pipeline one piece at a time while building it.

---

## 12. First three actions

1. Get a Jev key and confirm one `systemone` call.
2. Prove the pane layer by hand: split, start pi with a Codex model, prompt, read, close. That validates the riskiest assumption in the whole plan in about five minutes.
3. Write Phase 1 (`jroute status`), about 60 lines, and check the Codex row against raw JSON.
