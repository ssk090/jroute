#!/usr/bin/env python3
"""jroute: a plan / execute / review pipeline across Codex Plus, OpenCode Go, and Cursor Pro.

Deterministic code owns quota, gating, and model choice. Jev owns judgment, and is
optional: routing chains are ordered, so a missing or failing Jev call degrades to the
first eligible model in the chain instead of failing the run.

Pure functions (normalize_*, eligible, resolve, parse_pi_usage, redact) carry the logic
and are covered by test_jroute.py. Everything else is a thin wrapper over HTTP or herdr.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HOME = Path(os.environ.get("JROUTE_HOME", Path.home() / ".jroute"))
CONFIG_PATH = Path(__file__).with_name("config.json")
PLANS_DIR = HOME / "plans"
LOG_PATH = HOME / "decisions.jsonl"

ANTHROPIC = re.compile(r"claude|anthropic", re.I)
TICKET_REF = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b|#(\d+)")

# Harness-specific gates. A dialog must be dismissed before the first prompt, because
# `herdr agent start` reports interactive_ready before these resolve, and any prompt sent
# meanwhile lands in the composer unsent.
DIALOGS = [
    ("workspace trust required", "a"),   # cursor-agent: [a] trust, [q] quit
    ("trust the contents of this directory", "\r"),  # codex and pi
]


# --------------------------------------------------------------------------------------
# quota probes
# --------------------------------------------------------------------------------------

def http_json(url, headers, data=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    req = Request(url, data=body, headers=headers, method=method)
    with urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def keychain(service):
    p = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                       capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def git_short_sha():
    """The running checkout's short sha, so a status run can name the code it came from."""
    try:
        out = subprocess.run(["git", "-C", str(Path(__file__).parent),
                              "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True)
    except OSError:
        return "unknown"
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else "unknown"


def normalize_codex(raw):
    """Codex Plus: account windows plus a per-model availability map."""
    rl = raw.get("rate_limit") or {}
    primary = rl.get("primary_window") or {}
    secondary = rl.get("secondary_window") or {}
    return {
        "provider": "codex",
        "primary_used": primary.get("used_percent"),
        "secondary_used": secondary.get("used_percent"),
        "reset_primary_s": primary.get("reset_after_seconds"),
        "reset_secondary_s": secondary.get("reset_after_seconds"),
        "allowed": rl.get("allowed"),
        "limit_reached": rl.get("limit_reached"),
        "model_available": {k: bool((v or {}).get("available"))
                            for k, v in (raw.get("model_usage") or {}).items()},
    }


def normalize_cursor(raw):
    """Cursor Pro: auto and api buckets are gated separately, which is the whole point."""
    plan = raw.get("planUsage") or raw.get("plan_usage") or {}
    return {
        "provider": "cursor",
        "auto_used": plan.get("autoPercentUsed"),
        "api_used": plan.get("apiPercentUsed"),
        "total_used": plan.get("totalPercentUsed"),
        "auto_bucket": {str(m).lower() for m in (raw.get("autoBucketModels") or [])},
        "enabled": raw.get("enabled"),
        "display_message": raw.get("displayMessage"),
    }


def probe_codex():
    tok = json.loads((Path.home() / ".codex" / "auth.json").read_text())["tokens"]["access_token"]
    raw = http_json("https://chatgpt.com/backend-api/wham/usage",
                    {"Authorization": f"Bearer {tok}", "User-Agent": "codex-cli"})
    return normalize_codex(raw)


def probe_cursor():
    tok = keychain("cursor-access-token")
    if not tok:
        raise RuntimeError("no cursor-access-token in the keychain")
    raw = http_json("https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage",
                    {"Authorization": f"Bearer {tok}", "Content-Type": "application/json",
                     "User-Agent": "cursor-agent/1.0"}, data={}, method="POST")
    return normalize_cursor(raw)


def probe_opencode_go():
    """Flat rate, and no usage endpoint exists, so this reports the live catalog only."""
    acct = json.loads((Path.home() / ".local" / "share" / "opencode" / "account.json").read_text())
    key = next(a["credential"]["key"] for a in acct["accounts"].values()
               if a.get("serviceID") == "opencode-go")
    raw = http_json("https://opencode.ai/zen/go/v1/models",
                    {"Authorization": f"Bearer {key}", "User-Agent": "Mozilla/5.0"})
    return {"provider": "opencode-go", "kind": "flat",
            "models": [m["id"] for m in raw.get("data", [])]}


PROBES = {"codex": probe_codex, "cursor": probe_cursor, "opencode-go": probe_opencode_go}


def probe_all():
    snaps, errors = {}, {}
    for name, fn in PROBES.items():
        try:
            snaps[name] = fn()
        except (HTTPError, URLError, OSError, KeyError, StopIteration, RuntimeError) as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"
    return snaps, errors


# --------------------------------------------------------------------------------------
# eligibility and routing
# --------------------------------------------------------------------------------------

def eligible(snaps, config, models):
    """Return (eligible_ids, [(id, reason)]). Quota only ever excludes; it never ranks."""
    g = config["gates"]
    ok, excluded = [], []
    for mid in models:
        if ANTHROPIC.search(mid):
            excluded.append((mid, "anthropic model, excluded by policy"))
            continue
        provider, name = mid.split("/", 1) if "/" in mid else ("", mid)
        if provider == "openai-codex":
            c = snaps.get("codex")
            if not c:
                excluded.append((mid, "codex quota unknown (probe failed)"))
            elif c.get("limit_reached") or c.get("allowed") is False:
                excluded.append((mid, "codex account at limit_reached"))
            elif (c.get("primary_used") or 0) >= g["codex"]["primary_5h_max"]:
                excluded.append((mid, f"codex 5h {c['primary_used']:.0f}% "
                                      f">= {g['codex']['primary_5h_max']}%"))
            elif (c.get("secondary_used") or 0) >= g["codex"]["secondary_7d_max"]:
                excluded.append((mid, f"codex 7d {c['secondary_used']:.0f}% "
                                      f">= {g['codex']['secondary_7d_max']}%"))
            elif name in c.get("model_available", {}) and not c["model_available"][name]:
                excluded.append((mid, f"codex model {name} unavailable"))
            else:
                ok.append(mid)
        elif provider == "opencode-go":
            catalog = (snaps.get("opencode-go") or {}).get("models")
            if catalog is not None and name not in catalog:
                excluded.append((mid, "not in the live opencode-go catalog"))
            else:
                ok.append(mid)  # flat rate, no quota gate
        elif provider == "cursor":
            c = snaps.get("cursor")
            if not c:
                excluded.append((mid, "cursor quota unknown (probe failed)"))
            elif c.get("enabled") is False:
                excluded.append((mid, "cursor account disabled"))
            elif (c.get("total_used") or 0) >= g["cursor"]["total_max"]:
                excluded.append((mid, f"cursor total {c['total_used']:.0f}% "
                                      f">= {g['cursor']['total_max']}%"))
            elif name.lower() in c.get("auto_bucket", set()):
                if (c.get("auto_used") or 0) >= g["cursor"]["auto_bucket_max"]:
                    excluded.append((mid, f"cursor auto bucket {c['auto_used']:.0f}% "
                                          f">= {g['cursor']['auto_bucket_max']}%"))
                else:
                    ok.append(mid)
            elif (c.get("api_used") or 0) >= g["cursor"]["api_bucket_max"]:
                excluded.append((mid, f"cursor api bucket {c['api_used']:.0f}% "
                                      f">= {g['cursor']['api_bucket_max']}%"))
            else:
                ok.append(mid)
        else:
            excluded.append((mid, f"unknown provider {provider!r}"))
    return ok, excluded


def resolve(stage, ok_ids, config, start=0, exclude_families=()):
    """Walk the stage chain and take the first eligible hit, offset by `start`.

    Jev never picks a model. It picks how far along the chain to start, so a hard task
    skips the cheapest tier instead of burning a failed attempt there, and the chain
    itself stays the deterministic policy. `exclude_families` keeps the review stage off
    the executor's family, so review is not the executor reviewing itself.
    """
    usable = [m for m in config["stages"][stage]
              if m in ok_ids and family_of(m) not in exclude_families]
    if not usable:
        return None
    return usable[min(max(start, 0), len(usable) - 1)]


def family_of(model_id):
    """Model family prefix, used for review independence. gpt-5.6-sol and gpt-6-astra are
    both 'gpt'; deepseek-v4.1-flash is 'deepseek'; qwen3.8-flash is 'qwen3'."""
    name = model_id.split("/", 1)[1] if "/" in model_id else model_id
    return re.split(r"[-.]", name)[0].lower()


def noul(answers, name):
    """Read a calibrated probability out of a Jev answer, defaulting to zero."""
    value = (answers.get(name) or {}).get("noul")
    return value if isinstance(value, (int, float)) else 0.0


def jev_shape(answers, config):
    """Jev judges the task; code decides the pipeline. Returns the stages to run.

    A question must never pay for a plan, and neither must a routine change with no design
    decisions. Order matters: the cheapest shape wins a tie.
    """
    policy = config.get("shape") or {}
    if not answers:
        return tuple(policy.get("default") or ("plan", "execute"))

    complexity = (answers.get("complexity") or {}).get("score")
    confidence = (answers.get("complexity") or {}).get("confidence") or 0
    consequence = noul(answers, "consequence")
    if isinstance(complexity, (int, float)) and confidence < 0.6:
        complexity += 1  # under-powering costs a failed attempt, so never round down

    if noul(answers, "is_question") > policy.get("question_threshold", 0.6):
        return ("answer",)

    hard = (isinstance(complexity, (int, float))
            and complexity >= policy.get("review_complexity", 3))
    if hard or consequence > policy.get("review_consequence", 0.7):
        return ("plan", "execute", "review")

    needs_design = noul(answers, "needs_design") > policy.get("design_threshold", 0.5)
    routine = isinstance(complexity, (int, float)) and complexity <= 1
    if not needs_design and routine:
        return ("execute",)
    return ("plan", "execute")


def jev_start_index(answers):
    """Map Jev's judgment onto a chain offset. Not a model choice, a starting point.

    Deliberately conservative in one direction only: under-powering a task costs a failed
    attempt plus the escalation, so low confidence rounds complexity up, never down.
    """
    if not answers:
        return 0
    complexity = answers.get("complexity") or {}
    score = complexity.get("score")
    if score is None:
        return 0
    confidence = complexity.get("confidence") or 0
    consequence = (answers.get("consequence") or {}).get("noul") or 0
    effective = score + (1 if confidence < 0.6 else 0)
    if effective >= 3 or consequence > 0.7:
        return 2
    if effective >= 2:
        return 1
    return 0


# --------------------------------------------------------------------------------------
# Jev (optional)
# --------------------------------------------------------------------------------------

def redact(text):
    """Jev state leaves the machine. Strip anything credential-shaped, or refuse."""
    from re import sub
    patterns = [
        r"sk-[A-Za-z0-9_\-]{8,}", r"gh[pousr]_[A-Za-z0-9]{8,}", r"github_pat_[A-Za-z0-9_]+",
        r"AKIA[0-9A-Z]{16}", r"AIza[A-Za-z0-9_\-]{20,}", r"xox[baprs]-[A-Za-z0-9\-]{10,}",
        r"jv_live_[A-Za-z0-9_\-]+", r"Bearer\s+\S+",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    ]
    out = text
    for p in patterns:
        out = sub(p, "<redacted>", out, flags=re.S)
    return out


JEV_QUESTIONS = {
    "family": {
        "type": "choice",
        "instructions": "What kind of work is this?",
        "criteria": {
            "plan": "design, specification, or architecture of new work",
            "implement": "execute an approved or well-specified change",
            "debug": "diagnose a failure or unexpected behaviour",
            "review": "audit or critique existing work",
            "chore": "mechanical, repetitive, or trivial change",
        },
    },
    "complexity": {
        "type": "score",
        "instructions": "How much reasoning does this task need?",
        "criteria": ["trivial", "routine", "moderate", "hard", "frontier"],
    },
    "consequence": {
        "type": "noul",
        "instructions": "Would a wrong answer here be expensive to undo, or does this "
                        "touch high-risk ground such as production, security, money, or "
                        "data loss?",
    },
    "is_question": {
        "type": "noul",
        "instructions": "Is this asking for information, explanation, or analysis rather "
                        "than a change to the repository?",
    },
    "needs_design": {
        "type": "noul",
        "instructions": "Does this require design or specification decisions before code "
                        "can be written, or is the approach already determined?",
    },
    "min_effort": {
        "type": "choice",
        "instructions": "What is the minimum reasoning effort likely to be sufficient?",
        "criteria": {"low": "bounded, well-understood change",
                     "medium": "some judgment required",
                     "high": "subtle, high-consequence, or deeply uncertain"},
    },
}


def jev_ask(state, questions):
    """One batched systemone call. Returns None when unavailable, which is not an error.

    JROUTE_JEV_FIXTURE points at a recorded response, so routing decisions can be replayed
    offline and reproducibly without spending a call or needing a key.
    """
    fixture = os.environ.get("JROUTE_JEV_FIXTURE")
    if fixture:
        try:
            return json.loads(Path(fixture).read_text())
        except (OSError, ValueError) as exc:
            print(f"  jev: fixture unreadable ({exc})", file=sys.stderr)
            return None
    key = os.environ.get("TYPESAFE_API_KEY") or keychain("jroute-typesafe")
    if not key:
        return None
    url = os.environ.get("JEV_URL", "https://api.typesafe.ai/v1/systemone")
    model = os.environ.get("JEV_MODEL", "jev-1.13.0")
    try:
        return http_json(url, {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                         data={"model": model, "state": redact(state), "questions": questions},
                         method="POST")
    except (HTTPError, URLError, OSError) as exc:
        print(f"  jev: unavailable ({type(exc).__name__}), using chain order", file=sys.stderr)
        return None


def classify(task):
    """One batched Jev call for the whole run, reduced to its answers map. Jev is optional:
    a miss is not an error, it just means every chain starts at its cheapest eligible model."""
    spin = Spinner("jev classifying")
    spin.tick()
    try:
        response = jev_ask(task, JEV_QUESTIONS)
    finally:
        spin.done()
    if not response:
        return None
    return response.get("answers") or None


# --------------------------------------------------------------------------------------
# token accounting
# --------------------------------------------------------------------------------------

def absorb_usage(totals, usage):
    """Add one usage block into running totals. pi reports `cost` as a nested dict, so a plain
    numeric check silently reports zero spend."""
    if not isinstance(usage, dict):
        return
    for src, dst in (("input", "input"), ("output", "output"), ("cacheRead", "cache_read"),
                     ("cacheWrite", "cache_write"), ("reasoning", "reasoning")):
        value = usage.get(src)
        if isinstance(value, (int, float)):
            totals[dst] = totals.get(dst, 0) + value
    cost = usage.get("cost")
    if isinstance(cost, (int, float)):
        totals["cost"] = totals.get("cost", 0.0) + cost
    elif isinstance(cost, dict) and isinstance(cost.get("total"), (int, float)):
        totals["cost"] = totals.get("cost", 0.0) + cost["total"]


def new_totals():
    return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
            "reasoning": 0, "cost": 0.0, "turns": 0}


def parse_pi_usage(text):
    """Sum usage blocks from a pi session JSONL. cacheRead dominates, so keep it separate."""
    tot = new_totals()

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("usage"), dict):
                tot["turns"] += 1
                absorb_usage(tot, node["usage"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            walk(json.loads(line))
        except ValueError:
            continue
    tot["total"] = tot["input"] + tot["output"] + tot["cache_read"] + tot["cache_write"]
    return tot


def read_pi_usage(session_path):
    try:
        return parse_pi_usage(Path(session_path).read_text())
    except OSError:
        return None


# --------------------------------------------------------------------------------------
# herdr
# --------------------------------------------------------------------------------------

def herdr(*args, timeout=600):
    proc = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"herdr {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def herdr_json(*args, timeout=600):
    return json.loads(herdr(*args, timeout=timeout))


def inside_herdr():
    return os.environ.get("HERDR_ENV") == "1"


def launch_argv(stage, model, effort):
    """pi carries the effort flag for every provider it serves, which is the main token
    lever. cursor-agent has no effort flag, so effort lives in the model id."""
    provider, name = model.split("/", 1)
    if provider in ("openai-codex", "opencode-go"):
        argv = ["--model", model]
        if effort:
            argv += ["--thinking", effort]
        return "pi", argv
    if provider == "cursor":
        return "cursor-agent", ["--model", name]
    raise RuntimeError(f"no launcher for provider {provider!r}")


def dismiss_dialogs(target, pane_id, max_seconds=45):
    """Trust dialogs block the first prompt. herdr reports interactive_ready before they
    resolve, and text sent meanwhile sits in the composer unsent."""
    deadline = time.time() + max_seconds
    while time.time() < deadline:
        screen = herdr("pane", "read", pane_id, "--source", "visible",
                       "--lines", "40", "--format", "text")
        low = screen.lower()
        for marker, key in DIALOGS:
            if marker in low:
                print(f"  {target}: dismissing dialog ({marker!r})")
                herdr("agent", "send-keys", target, "enter" if key == "\r" else key)
                time.sleep(3)
                break
        else:
            return True
    return False


def plan_path_for(task):
    m = TICKET_REF.search(task)
    slug = (m.group(1) or f"issue-{m.group(2)}").lower() if m else None
    if not slug:
        slug = re.sub(r"[^a-z0-9]+", "-", task.lower())[:40].strip("-") or "task"
    return PLANS_DIR / f"{slug}.md"


BRIEFS = {
    "plan": """Write an implementation plan.

Task: {task}

Steps:
1. If a ticket reference appears above, fetch it with gh-axi.
2. Read what the plan needs to name: the files, the seams, the constraints.
3. Write the plan to {plan_path}, under {cap} lines, with exactly these sections:
   ## acceptance criteria
   ## files to change
   ## test seams
   ## suggested skills

Name real file paths and real test seams. Reply with only the file path.""",
    "execute": """Implement the plan at {plan_path}.

Treat the plan as read-only: follow its acceptance criteria and test seams, and leave the
document itself untouched. Run typecheck and the test suite, then commit to the current
branch. Reply with only the commit sha.""",
    "implement": """Implement this change directly:

{task}

Keep it the smallest change that works. Run typecheck and the test suite, then commit to
the current branch. Reply with only the commit sha.""",
    "review": """Run /code-review since the merge-base, checking the diff against the
acceptance criteria in {plan_path}. Append findings under a "## review findings" heading in
that file, listing only issues that need a change.""",
}


SKILL_LINE = "Load these skills first and follow them: {names}."


def brief_for(stage, task, config, plan_path, has_plan):
    """A routine task skips the plan stage, so the executor must not be told to follow a plan
    that was never written. That is the difference between the two execute briefs.

    Skills are named here rather than passed as --skill so the whole skill is loaded only
    when the stage runs, instead of paying its context load on every stage.
    """
    key = "implement" if stage == "execute" and not has_plan else stage
    brief = BRIEFS[key].format(task=task, plan_path=plan_path,
                               cap=config["plan_line_cap"])
    names = (config.get("skills") or {}).get(stage) or []
    if names:
        brief = SKILL_LINE.format(names=", ".join(names)) + "\n\n" + brief
    return brief


def run_stage(stage, task, route, config, keep_panes=False, focus=False, timeout_s=900,
              has_plan=True):
    """Run one stage in its own pane. The pane is closed on success and kept on failure,
    so a failed stage can be read before deciding what to do with it."""
    plan_path = plan_path_for(task)
    brief = brief_for(stage, task, config, plan_path, has_plan)
    if stage != "plan" and has_plan and not plan_path.exists():
        print(f"  blocked: {plan_path} does not exist, run the plan stage first")
        return None

    name = f"jroute-{stage}"
    split = ["pane", "split", "--current", "--direction", "right",
             "--cwd", os.getcwd()]
    if not focus:
        split.append("--no-focus")
    pane_id = herdr_json(*split)["result"]["pane"]["pane_id"]
    keep = bool(keep_panes)
    try:
        kind, argv = launch_argv(stage, route, config["effort"].get(stage))
        started = herdr_json("agent", "start", name, "--kind", kind, "--pane", pane_id,
                             "--", *argv)
        session = started["result"]["agent"]["agent_session"].get("value")
        if not dismiss_dialogs(name, pane_id):
            print(f"  {stage}: a dialog is still up, leaving the pane open")
            keep = True
            return None
        herdr("agent", "prompt", name, brief)  # submit, then watch for real progress
        settled = wait_for_stage(name, pane_id, timeout_s, session=session)
        if settled == "timeout":
            keep = True
            return None
        usage = read_pi_usage(session) if kind == "pi" and session else None
        record = {"stage": stage, "model": route, "pane": pane_id, "session": session,
                  "usage": usage, "plan": str(plan_path)}
        if usage:
            total = usage["total"]
            record["tokens_total"] = total
            warn = (config.get("token_warn") or {}).get(stage)
            over = warn and total > warn
            print(f"  {stage}: in {usage['input']} out {usage['output']} "
                  f"cacheRead {usage['cache_read']} total {total}"
                  + ("  OVER BUDGET" if over else ""))
            if over:
                print(f"  warning: {stage} used {total} tokens, over the {warn} budget")
        if stage == "plan" and not plan_path.exists():
            print(f"  {stage}: finished but {plan_path} was not written")
            keep = True
            return record
        if stage == "plan" and "## acceptance criteria" not in plan_path.read_text():
            print(f"  {stage}: {plan_path.name} has no '## acceptance criteria' section")
            keep = True
            return record
        return record
    except Exception:
        keep = True  # never close a pane we may need to read after a failure
        raise
    finally:
        if not keep:
            subprocess.run(["herdr", "pane", "close", pane_id],
                           capture_output=True, text=True)


def format_elapsed(seconds, width=60):
    """mm:ss, used for every live progress line."""
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


CHROME = ("esc interrupt", "escape interrupt", "(auto)", "Ctrl+P", "press ctrl",
          "ponytail", "⚡", "🐴",
          "└", "┘", "┌", "┐", "─", "▄", "▀")


class Spinner:
    """A live progress line. Appears only after `delay` seconds, so a fast call shows
    nothing, and only on a TTY, so piped output stays clean."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label, delay=0.8, enabled=None, pad=110):
        self.label = label
        self.delay = delay
        self.enabled = sys.stdout.isatty() if enabled is None else enabled
        self.pad = pad
        self.started = time.time()
        self.shown = False
        self.frame = 0

    def tick(self, detail=""):
        if not self.enabled:
            return
        elapsed = time.time() - self.started
        if not self.shown and elapsed < self.delay:
            return
        self.shown = True
        glyph = self.FRAMES[self.frame % len(self.FRAMES)]
        self.frame += 1
        line = f"  {glyph} {self.label} {format_elapsed(elapsed)}"
        if detail:
            line += f"  {detail}"
        print("\r" + line[:self.pad].ljust(self.pad), end="", flush=True)

    def done(self, detail=""):
        if self.enabled and self.shown:
            print("\r" + " " * self.pad + "\r", end="", flush=True)
        self.shown = False
        if detail:
            print(f"  {detail}", flush=True)

    def __enter__(self):
        self.tick()
        return self

    def __exit__(self, *_exc):
        self.done()


def pane_activity(pane_id, width=64):
    """The agent's current visible activity, scraped from its pane, for the progress line.
    Best effort by design: progress reporting must never be the reason a run fails."""
    try:
        out = herdr("pane", "read", pane_id, "--source", "recent-unwrapped",
                    "--lines", "10", "--format", "text", timeout=20)
    except Exception:
        return ""
    for line in reversed(out.splitlines()):
        line = line.strip()
        if not line or any(marker in line for marker in CHROME):
            continue
        return line[:width]
    return ""


def agent_status(target):
    try:
        return herdr_json("agent", "get", target, timeout=30)["result"]["agent"]["agent_status"]
    except Exception:
        return "unknown"


def wait_for_stage(target, pane_id, timeout_s, session=None, poll=1.0, show_thinking=True):
    """Submit nothing, just watch. Replaces `agent prompt --wait`, which blocks silently for
    minutes; this streams the agent's reasoning, tool calls, and output as they happen, with
    a live status line for elapsed time and token use.

    A settled state is idle, done, or blocked. A submission is accepted before the agent
    flips to working, so the first seconds are spent waiting for that transition."""
    deadline = time.time() + timeout_s
    spin = Spinner(f"{target}")
    stream = SessionStream(session, enabled=spin.enabled, show_thinking=show_thinking)
    saw_working = False
    while time.time() < deadline:
        status = agent_status(target)
        if status in ("working", "blocked"):
            saw_working = True
        elif status in ("idle", "done"):
            if saw_working:
                stream.poll()
                spin.done(f"{target} finished in {format_elapsed(time.time() - spin.started)}"
                          + (f"  {stream.summary()}" if stream.summary() else ""))
                return status
            # It may have finished before the first poll. Give the transition a few seconds
            # before accepting idle, then let the artifact checks decide if anything happened.
            if time.time() - spin.started > 8:
                stream.poll()
                spin.done(f"{target} settled in {format_elapsed(time.time() - spin.started)}")
                return status
        stream.poll()
        detail = stream.summary() or pane_activity(pane_id)
        spin.tick(detail)
        time.sleep(poll)
    spin.done()
    print(f"  {target}: timed out after {format_elapsed(timeout_s)}, leaving the pane open")
    return "timeout"


def short_count(value):
    """18154 -> 18.2k. Token counts get long fast and the progress line is one line."""
    if not isinstance(value, (int, float)) or value < 1000:
        return str(int(value or 0))
    if value < 1_000_000:
        return f"{value / 1000:.1f}k"
    return f"{value / 1_000_000:.2f}M"


def render_event(entry, width=96, thinking_chars=400):
    """Turn one session entry into printable (kind, text) lines. Pure, so it is testable.

    pi records thinking, text, and tool calls as separate content parts, which is why
    tailing the session file beats scraping the rendered pane.
    """
    message = entry.get("message") or {}
    role = message.get("role")
    content = message.get("content")
    out = []
    if not isinstance(content, list):
        return out
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "thinking":
            text = " ".join((part.get("thinking") or "").split())
            if text:
                clipped = text[:thinking_chars] + (" …" if len(text) > thinking_chars else "")
                out.append(("think", clipped))
        elif kind == "text" and role == "assistant":
            text = (part.get("text") or "").strip()
            if text:
                out.append(("say", text[:width * 4]))
        elif kind == "toolCall":
            args = part.get("arguments") or {}
            detail = (args.get("command") or args.get("path") or args.get("pattern")
                      or args.get("file_path") or "")
            out.append(("tool", f"{part.get('name', '?')}: {' '.join(str(detail).split())[:width]}"))
    return out


EVENT_PREFIX = {"think": "  💭 ", "say": "  ✎  ", "tool": "  →  "}


class SessionStream:
    """Tails a pi session JSONL and renders reasoning, tool calls, output, and live token use.

    Reading the session file rather than the pane is deliberate: it is structured, it includes
    thinking blocks that the pane may hide, and every message carries its own usage.
    """

    def __init__(self, path, enabled=None, show_thinking=True):
        self.path = Path(path) if path else None
        self.enabled = sys.stdout.isatty() if enabled is None else enabled
        self.show_thinking = show_thinking
        self.offset = 0
        self.buffer = ""
        self.totals = new_totals()

    def poll(self):
        if not self.enabled or not self.path or not self.path.exists():
            return
        try:
            size = self.path.stat().st_size
            if size < self.offset:
                self.offset = 0  # truncated or replaced
            if size == self.offset:
                return
            with self.path.open() as handle:
                handle.seek(self.offset)
                self.buffer += handle.read()
                self.offset = handle.tell()
        except OSError:
            return
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message") or {}
            if isinstance(message.get("usage"), dict):
                self.totals["turns"] += 1
                absorb_usage(self.totals, message["usage"])
            for kind, text in render_event(entry):
                if kind == "think" and not self.show_thinking:
                    continue
                self.emit(kind, text)

    def emit(self, kind, text):
        print("\r" + " " * 110 + "\r", end="", flush=True)
        print(f"{EVENT_PREFIX.get(kind, '  ')}{text}", flush=True)

    def summary(self):
        t = self.totals
        if not t["turns"] and not t["input"] and not t["output"]:
            return ""  # nothing recorded yet, so let the caller show pane activity instead
        parts = [f"↑{short_count(t['input'])} ↓{short_count(t['output'])}"]
        if t["cache_read"]:
            parts.append(f"cache {short_count(t['cache_read'])}")
        if t["reasoning"]:
            parts.append(f"think {short_count(t['reasoning'])}")
        if t["cost"]:
            parts.append(f"${t['cost']:.4f}")
        if t["turns"]:
            parts.append(f"{t['turns']} steps")
        return "  ".join(parts)


# --------------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------------

def fmt_reset(seconds):
    if not seconds:
        return ""
    mins = int(seconds // 60)
    return f"{mins // 60}h{mins % 60:02d}m" if mins >= 60 else f"{mins}m"


def minimal_snapshots(snaps, full=False):
    """AXI: the default schema carries only what a caller needs to decide what to do next.
    The 28-entry auto bucket and the 33-model catalog are detail views, not decisions, so
    they collapse to a count unless --full asks for them."""
    out = {}
    for name, snap in snaps.items():
        snap = dict(snap)
        if full:
            if "auto_bucket" in snap:
                snap["auto_bucket"] = sorted(snap["auto_bucket"])
        else:
            for key, count_key in (("auto_bucket", "auto_bucket_count"),
                                   ("models", "model_count")):
                if key in snap:
                    snap[count_key] = len(snap[key])
                    del snap[key]
        out[name] = snap
    return out


def cmd_status(config, json_mode=False, full=False):
    snaps, errors = probe_all()
    ok_ids, excluded = eligible(snaps, config, all_chain_models(config))

    if json_mode:
        payload = {
            "snapshots": minimal_snapshots(snaps, full),
            "errors": errors,
            "routes": {stage: {"model": resolve(stage, ok_ids, config),
                               "effort": config["effort"].get(stage, "")}
                       for stage in config["stages"]},
            "exclusions": [{"model": mid, "reason": reason} for mid, reason in excluded],
        }
        if not full:
            payload["help"] = ["Pass --full for the auto-bucket and model lists."]
        print(json.dumps(payload))
        return

    print("jroute status\n")
    codex = snaps.get("codex")
    if codex:
        codex_ok, codex_excluded = eligible(snaps, config, ["openai-codex/gpt-6-astra"])
        print(f"codex        {'OK       ' if codex_ok else 'GATED    '} "
              f"5h {codex['primary_used']:.0f}% (reset {fmt_reset(codex['reset_primary_s'])})  "
              f"7d {codex['secondary_used']:.0f}% (reset {fmt_reset(codex['reset_secondary_s'])})")
        for mid, reason in codex_excluded:
            print(f"             excluded: {reason}")
        for model, avail in (codex.get("model_available") or {}).items():
            if not avail:
                print(f"             model gated: {model}")
    else:
        print(f"codex        ERROR    {errors.get('codex')}")

    cur = snaps.get("cursor")
    if cur:
        print(f"cursor       OK        auto {cur['auto_used']:.1f}%  "
              f"api {cur['api_used']:.1f}%  total {cur['total_used']:.1f}%")
        print(f"             auto bucket contains {len(cur['auto_bucket'])} models "
              f"(composer-2.5, grok-4.5 ladders)")
    else:
        print(f"cursor       ERROR    {errors.get('cursor')}")

    oc = snaps.get("opencode-go")
    if oc:
        print(f"opencode-go  FLAT      {len(oc['models'])} models, no usage endpoint exists")
    else:
        print(f"opencode-go  ERROR    {errors.get('opencode-go')}")

    print("\nstage routes")
    seen_excluded = {}
    for mid, reason in excluded:
        seen_excluded.setdefault(reason, []).append(mid)
    for stage in config["stages"]:
        chosen = resolve(stage, ok_ids, config)
        effort = config["effort"].get(stage, "")
        print(f"  {stage:8} -> {chosen or 'NOTHING ELIGIBLE'}  ({effort})")
    if seen_excluded:
        print("\nexclusions")
        for reason, ids in seen_excluded.items():
            print(f"  {reason}: {', '.join(ids)}")


def answer_argv(route, effort):
    """Headless form of the same launch: pi's print mode answers and exits."""
    provider, name = route.split("/", 1)
    if provider in ("openai-codex", "opencode-go"):
        argv = ["pi", "-p", "--no-session", "--model", route]
        if effort:
            argv += ["--thinking", effort]
        return argv
    if provider == "cursor":
        return ["cursor-agent", "-p", "--model", name]
    raise RuntimeError(f"no headless launcher for provider {provider!r}")


BANNER = ("[pi-web-access]", "[Context]", "[Skills]", "[Prompts]", "[Extensions]")


def cmd_answer(task, route, effort, timeout_s):
    """Questions get an answer on stdout, not a plan artifact and a pane. That is the
    difference between a 300k-token detour through Astra and a ten-second cheap reply."""
    argv = answer_argv(route, effort)
    print(f"  answer   -> {route}  ({effort})  headless", flush=True)
    try:
        proc = subprocess.Popen(argv + ["--", task], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        print(f"  answer: could not launch: {exc}", file=sys.stderr)
        return 1
    spin = Spinner("thinking")
    deadline = time.time() + timeout_s
    while proc.poll() is None and time.time() < deadline:
        spin.tick()
        time.sleep(0.4)
    if proc.poll() is None:
        proc.kill()
        spin.done()
        print(f"  answer: timed out after {format_elapsed(timeout_s)}", file=sys.stderr)
        return 1
    out, err = proc.communicate()
    spin.done()
    if proc.returncode != 0:
        print(f"  answer: exit {proc.returncode}: {err.strip()[:300]}", file=sys.stderr)
        return 1
    print()
    for line in out.splitlines():
        if not any(line.startswith(marker) for marker in BANNER):
            print(line)
    return 0


def all_chain_models(config):
    return [m for chain in config["stages"].values() for m in chain]


def cmd_run(task, config, args):
    snaps, errors = probe_all()
    for name, err in errors.items():
        print(f"warn: {name} probe failed: {err}", file=sys.stderr)

    answers = None if args.no_jev else classify(task)
    start = jev_start_index(answers)
    ok_ids, excluded = eligible(snaps, config, all_chain_models(config))

    print(f"jroute: {task}\n")
    if answers:
        comp = answers.get("complexity") or {}
        print(f"  jev: {(answers.get('family') or {}).get('choice')}  "
              f"complexity {comp.get('score')} (conf {comp.get('confidence')})  "
              f"question {noul(answers, 'is_question'):.2f}  "
              f"needs_design {noul(answers, 'needs_design'):.2f}  "
              f"consequence {noul(answers, 'consequence'):.2f}")
    elif not args.no_jev:
        print("  jev: unavailable, starting each chain at its cheapest eligible model")

    shape = (args.stage,) if args.stage else jev_shape(answers, config)
    print(f"  shape: {' -> '.join(shape)}")

    if shape == ("answer",):
        route = resolve("answer", ok_ids, config, start)
        if not route:
            print("  answer: NO ELIGIBLE MODEL")
            return 1
        effort = config["effort"].get("answer", "low")
        if args.dry_run:
            print(f"  answer   -> {route}  ({effort})  headless")
            return 0
        return cmd_answer(task, route, effort, args.timeout)

    records, ran_families, exit_code = [], [], 0
    for stage in shape:
        # The plan chain is a fixed preference order, not a capability ladder: Astra first,
        # Sol as fallback. Jev's complexity offset must not move it off that order.
        offset = 0 if stage == "plan" else start
        # review must not be the executor's family reviewing itself
        skip = tuple(ran_families) if stage == "review" else ()
        route = resolve(stage, ok_ids, config, offset, skip)
        if not route:
            print(f"  {stage}: NO ELIGIBLE MODEL")
            for mid in config["stages"][stage]:
                reason = dict(excluded).get(mid, "filtered by the review family constraint")
                print(f"      {mid}: {reason}")
            exit_code = 1
            break
        chain = config["stages"][stage]
        note = "" if route == chain[0] else f"  (chain offset {offset})"
        print(f"  {stage:8} -> {route}  ({config['effort'].get(stage, '')})  "
              f"pane jroute-{stage}{note}")
        if args.dry_run:
            print(f"      brief -> {plan_path_for(task)}")
            continue
        rec = run_stage(stage, task, route, config, keep_panes=args.keep_panes,
                        focus=args.focus, timeout_s=args.timeout,
                        has_plan="plan" in shape)
        if not rec:
            print(f"  {stage}: aborted, stopping the pipeline")
            exit_code = 1
            break
        rec["jev"] = answers
        records.append(rec)
        ran_families.append(family_of(route))

    if records:
        HOME.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as fh:
            for rec in records:
                rec["task"] = task
                rec["ts"] = time.time()
                fh.write(json.dumps(rec) + "\n")
        print(f"\nlogged {len(records)} stage(s) to {LOG_PATH}")
    return exit_code


def cmd_log(config, count):
    if not LOG_PATH.exists():
        print("no decisions logged yet")
        return
    for line in LOG_PATH.read_text().splitlines()[-count:]:
        rec = json.loads(line)
        usage = rec.get("usage") or {}
        print(f"{rec.get('stage'):8} {rec.get('model'):38} "
              f"total {rec.get('tokens_total', '-'):>8} {rec.get('plan', '')}")


def main():
    ap = argparse.ArgumentParser(prog="jroute", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    status = sub.add_parser("status", help="quota pools and resolved stage routes")
    status.add_argument("--json", action="store_true",
                        help="emit one machine-readable JSON object instead of text")
    status.add_argument("--version", action="store_true",
                        help="print the git short sha of the running checkout and exit")
    status.add_argument("--full", action="store_true",
                        help="with --json, include the auto-bucket and model lists")

    run = sub.add_parser("run", help="route and run the pipeline")
    run.add_argument("task")
    run.add_argument("--stage", choices=["plan", "execute", "review"])
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--keep-panes", action="store_true")
    run.add_argument("--focus", action="store_true",
                     help="move focus to each stage pane (default keeps your focus)")
    run.add_argument("--no-jev", action="store_true",
                     help="skip the Jev call and start every chain at its cheapest model")
    run.add_argument("--timeout", type=int, default=900, help="per-stage seconds")

    log = sub.add_parser("log", help="recent decisions")
    log.add_argument("-n", type=int, default=20)

    args = ap.parse_args()

    if args.cmd == "status" and args.version:
        print(git_short_sha())
        return

    config = json.loads(CONFIG_PATH.read_text())

    if args.cmd == "status":
        cmd_status(config, args.json, args.full)
    elif args.cmd == "run":
        if not args.dry_run and not inside_herdr():
            sys.exit("jroute run needs $HERDR_ENV=1; use --dry-run outside Herdr")
        sys.exit(cmd_run(args.task, config, args) or 0)
    elif args.cmd == "log":
        cmd_log(config, args.n)


if __name__ == "__main__":
    main()
