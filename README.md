# jroute

Route coding work across three AI subscriptions, with [Jev](https://typesafe.ai) choosing the
pipeline.

One command plans a task on a strong model, hands the plan to a cheap model to execute, and
reviews the result on a third, each stage in its own [Herdr](https://herdr.dev) pane. A simple
question skips all of that and gets answered in about twenty seconds.

<img width="1440" height="1080" alt="jroute running a stage in a Herdr pane" src="https://github.com/user-attachments/assets/aebd25d5-770c-4afc-a08d-165ad5d251ad" />

```
$ jroute run "add a farewell function to demo.py"

  🧭 jroute  ·  add a farewell function to demo.py
  ──────────────────────────────────────────────────────────────────────────

  🧠 jev        implement complexity 0.73 (conf 0.70)
                question 0.03   design 0.28   consequence 0.07
  🧩 shape      execute

  🔨 execute:   opencode-go/deepseek-v4.1-flash         low
     💭 The user wants me to load skills "implement" and "tdd". Let me look at the repo first.
     →  bash: ls -la; git log --oneline -5; git status -s
     →  read: demo.py
     💭 Simple repo. demo.py with greet. Need to add farewell returning "goodbye " + name.
     →  write: test_demo.py
     →  bash: python3 -m unittest test_demo -v          # red
     →  edit: demo.py
     →  bash: python3 -m unittest test_demo -v          # green
     →  bash: git commit -m "add farewell function"
     ✎  de62d650155df74bd2ead3e6092618f517fe5278
  ⏱  jroute-execute finished in 0:42  ↑15.4k ↓2.2k cache 177.5k think 546 $0.0041 9 steps
  execute: in 15409 out 2156 cacheRead 177536 total 195101

  ✔ execute  logged 1 stage(s) to ~/.jroute/decisions.jsonl
```

No plan stage ran, no frontier model was called, and the work landed in 42 seconds. That block
is transcribed from a real run on a scratch repository, with the spinner frames and the home
directory abbreviated.

## What it solves

**Quota stranding.** You have three subscriptions with different limits and no shared view of
them. jroute reads all three before it routes, and a depleted pool is removed from the
candidate set rather than discovered by failing.

**Paying for a plan you do not need.** A three-stage pipeline costs about 450k tokens on the
planner alone. Asking "what does this function do" should not. Jev classifies the task first
and picks the shape, so the expensive path is reserved for work that needs it.

## Requirements

- **Python 3**, standard library only. No dependencies, no build step. Built on 3.14.
- **[Herdr](https://herdr.dev)**, and `jroute run` must be invoked from inside a Herdr pane.
  It splits panes and reads their output. `--dry-run` works anywhere.
- **[pi](https://pi.dev)** as the agent harness, for Codex and OpenCode Go models.
- **`cursor-agent`** for Cursor models, since Cursor is not a pi provider.
- **A Jev API key.** Optional: without one, routing degrades to the first eligible model in
  each chain.
- **At least one supported subscription**, logged in locally (see below).

## Setup

### 1. Credentials

jroute never stores credentials. It reads them from where the vendor tools already put them:

| Subscription | Where jroute reads the token | Where it reads quota |
|---|---|---|
| Codex Plus | `~/.codex/auth.json` | `GET chatgpt.com/backend-api/wham/usage` |
| Cursor Pro | macOS Keychain, `cursor-access-token` | `POST api2.cursor.sh/.../GetCurrentPeriodUsage` |
| OpenCode Go | `~/.local/share/opencode/account.json` | no usage endpoint exists |

The OpenCode Go subscription is flat rate, so its models carry no per-token billing. The
session log still reports a notional cost, which is what those tokens would cost at API rates.

The Cursor Keychain read is macOS-only. Everything else is portable.

### 2. The Jev key

Store it in the Keychain so it never lands in shell history or in the agent's environment:

```sh
security add-generic-password -a "$USER" -s jroute-typesafe -w
```

jroute reads `keychain:jroute-typesafe`, and falls back to `$TYPESAFE_API_KEY`.

### 3. Put it on your PATH

```sh
chmod +x jroute.py
echo 'alias jroute="python3 /path/to/jroute/jroute.py"' >> ~/.zshrc
source ~/.zshrc
```

### 4. Check it

```sh
jroute status
```

```
  🧭 jroute  ·  quota, routes, and what is excluded
  ──────────────────────────────────────────────────────────────────────────

  ✅ codex                 ready
     ░░░░░░░░░░  5h 0%   ⏱ 5h00m left
     ██████░░░░  7d 63%  ⏱ 40h43m left

  ✅ cursor                ready
     ███████░░░  auto 74%  28 models on this bucket
     █░░░░░░░░░  api  9%   frontier tier, nearly idle
     ███████░░░  all  68%

  ♾ opencode-go           flat rate
     33 models, no per-token cost, no usage endpoint

  🧩 routes
  💬 answer:    opencode-go/deepseek-v4.1-flash         low     (opencode-go)
  📋 plan:      openai-codex/gpt-6-astra                medium  (openai-codex)
  🔨 execute:   opencode-go/deepseek-v4.1-flash         low     (opencode-go)
  🔍 review:    opencode-go/glm-5.3                     medium  (opencode-go)
```

If a probe fails, the row shows the error rather than a fabricated number. Quota that cannot
be read is treated as unknown, and unknown excludes.

## Usage

```sh
jroute run "implement CHP-1234 add retry logic"     # full pipeline, routed by Jev
jroute run "what does the retry logic do?"          # answered inline, no pane
jroute run "..." --dry-run                          # decide and print, spend nothing
jroute run "..." --stage plan                       # one stage only
jroute run "..." --keep-panes                       # leave panes open to inspect
jroute run "..." --focus                            # move focus to each stage pane
jroute run "..." --no-jev                           # skip Jev, start chains at their cheapest
jroute status                                       # quota, routes, exclusions
jroute status --json                                # minimal machine-readable schema
jroute status --json --full                         # adds bucket and catalog lists
jroute status --version                             # the running checkout's short sha
jroute log                                          # recent routing decisions
```

**`cd` into the repository you want worked on first.** That directory becomes the working
directory for every pane. A ticket reference (`CHP-1234`, `#42`) in the task text is detected
and becomes the plan filename.

### Run one stage at a time

This is the mode worth knowing. The plan is a file you can read and edit before the executor
ever sees it:

```sh
jroute run "implement CHP-1234" --stage plan
$EDITOR ~/.jroute/plans/chp-1234.md
jroute run "implement CHP-1234" --stage execute
```

If the plan is wrong, fix it or rerun the plan stage. Nothing downstream has burned yet.

## How routing works

Three layers, in this order. The separation is the design.

```
task text
   |
   [1] deterministic gates        code    read quota, exclude the ineligible
   |
   [2] Jev, one batched call      Jev     family, complexity, consequence,
   |                                      is_question, needs_design
   |
   [3] shape, chain, effort       code    policy and arithmetic
   |
   [4] Herdr pane per stage       code    split, launch, stream, gate, close
```

**Quota only excludes. Capability only ranks.** Mixing them is how a router degrades into
sending every task to the weakest model the moment one pool gets hot.

**Jev never picks a model.** It answers five questions in one round trip (about 450 input
tokens), and code turns those answers into a pipeline shape and a starting point in a chain.

### Shapes

| Jev's read | Shape | What runs |
|---|---|---|
| `is_question > 0.6` | `answer` | Headless reply on stdout. No pane, no plan file. |
| routine and `needs_design <= 0.5` | `execute` | Straight to a cheap executor, no plan stage. |
| moderate, or design needed | `plan → execute` | Strong model plans, cheap model executes. |
| complexity `>= 3` or consequence `> 0.7` | `plan → execute → review` | Adds an independent reviewer. |

The cheapest shape wins a tie, and a question is answered even when it is complex to answer,
because explaining something deeply is still not a code change. Low confidence raises
complexity before the shape is chosen, so an uncertain routine task gets a plan rather than a
blind edit.

### The Codex gate

```text
Codex eligible = 5h used < 70%  AND  7d used < 85%
                 AND rate_limit.allowed  AND NOT limit_reached
```

When that fails, every Codex model is deleted from the candidate set **before** Jev is called,
so Jev cannot pick one because it never sees one. There is also a per-model gate:
`model_usage["gpt-6-astra"].available == false` removes only Astra, which is the case its
`gpt-5.6-sol` fallback exists for.

Cursor is gated per bucket, because its `auto` and `api` buckets deplete independently. A model
listed in the API's `autoBucketModels` is gated on `autoPercentUsed`, everything else on
`apiPercentUsed`. This is why the `status` output shows both.

### Stage completion is a gate, not a vibe

A stage is done when its artifact exists:

| Stage | Done when |
|---|---|
| plan | the file exists and contains `## acceptance criteria` |
| execute | `git HEAD` advanced, so something was committed |
| review | a `## review findings` section was appended |

A failed gate stops the pipeline and keeps the pane open, so you can read what happened.

### Review independence

The reviewer is never the executor's model family. A `qwen` executor gets a `kimi` reviewer, so
the review is not the executor grading itself.

## Configuration

Everything tunable lives in `config.json`. The important parts:

| Key | Meaning |
|---|---|
| `stages` | Ordered model chains per stage. First eligible entry wins. |
| `effort` | Reasoning effort per stage, passed as `--thinking`. |
| `shape` | The thresholds Jev's answers are compared against. |
| `skills` | Which skills each stage loads, by name. |
| `gates` | Quota thresholds per provider. |
| `token_warn` | Per-stage token budget. Warns, never aborts. |
| `plan_line_cap` | Plan artifact line budget. |

Chains are ordered, so the first entry is the preference and the last is the backstop. Jev's
complexity offset moves the start point along a chain for `execute` and `review`. The `plan`
chain is pinned, because its order is a deliberate choice rather than a capability ladder.

## Output

Two surfaces, on purpose:

- **Human.** Colour, quota bars, emoji. Colour is gated on a TTY and honours `NO_COLOR=1`.
  `JROUTE_PLAIN=1` drops the emoji for clean logs.
- **Machine.** `jroute status --json` emits a minimal schema (`snapshots`, `errors`, `routes`,
  `exclusions`) with detail lists collapsed to counts. Add `--full` for the bucket and catalog
  lists.

Every stage appends one JSON line to `~/.jroute/decisions.jsonl`: stage, model, effort, token
totals, Jev's answers with confidences, exclusions, and a `task_sha256` rather than the task
text.

## Privacy

Task text is sent to Jev for classification. `redact()` strips credential-shaped strings
(`sk-*`, `ghp_*`, `AKIA*`, bearer tokens, private key blocks) before the call, and a batch of
tests pins that behaviour. The decision log stores a hash of the task, not the task.

Quota probes are read-only GETs using tokens the vendor CLIs already stored. jroute never
writes a credential to disk, and never passes one into an agent's environment.

## Limitations

Honest list, all deliberate:

- **Token budgets and live streaming are pi-only.** Usage is read from pi's session JSONL. A
  Cursor-routed stage runs but reports neither.
- **Reasoning visibility depends on the model.** DeepSeek, GLM, and Kimi emit plain reasoning
  that streams in full. Codex models emit mostly encrypted reasoning, so `💭` lines are sparse
  there even though the plumbing is the same.
- **`cursor-agent`'s busy signature, exit key, and interrupt key are unverified.** The happy
  path relies on Herdr's own status detection.
- **Cursor model ids are not validated against a live catalog.** OpenCode Go ids are. A typo in
  a Cursor id fails at launch.
- **Skills marked `disable-model-invocation`** (`implement`, `to-spec`, `to-tickets`) are read
  as files rather than invoked. The brief says "read and follow" for that reason.
- **No escalation on evidence.** A failed stage stops the pipeline rather than advancing along
  its chain.
- **No stuck-stage recovery.** A wedged stage waits out its timeout; `dismiss_dialogs` handles
  trust prompts, nothing more.
- **Single user, macOS, personal tool.** Written against one person's three subscriptions. The
  gates, chains, and skills are config, so adapting it means editing `config.json`.

## Tests

```sh
python3 -m unittest test_jroute -v
```

140 tests over the pure logic: quota normalizers, gates, chain resolution, the shape policy,
token accounting, redaction, presentation, and config parity. The expensive paths were verified
by live runs rather than mocks; `PLAN.md` section 14 records what was measured and what is still
unproven.

## Design notes

`PLAN.md` is the spec, including the verification log, the corrections where the spec was
wrong, and the deviations from it that were accepted. It is the interesting document if you
want to know why something is the way it is.

The implementation is deliberately one file. A two-axis review argued from the deletion test
that the nine banner-delimited sections are already real seams, that callers and tests cross
them at the same interface, and that splitting would add import friction rather than depth. The
condition to revisit: a second consumer of the probes or the routing.
