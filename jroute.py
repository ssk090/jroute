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


def resolve(stage, ok_ids, config):
    """First model in the stage chain that survived the gates. Deterministic, no Jev."""
    for mid in config["stages"][stage]:
        if mid in ok_ids:
            return mid
    return None


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


def jev_ask(state, questions):
    """One batched systemone call. Returns None when unavailable, which is not an error."""
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


# --------------------------------------------------------------------------------------
# token accounting
# --------------------------------------------------------------------------------------

def parse_pi_usage(text):
    """Sum usage blocks from a pi session JSONL. cacheRead dominates, so keep it separate."""
    tot = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0, "turns": 0}

    def walk(node):
        if isinstance(node, dict):
            usage = node.get("usage")
            if isinstance(usage, dict):
                tot["turns"] += 1
                for src, dst in (("input", "input"), ("output", "output"),
                                 ("cacheRead", "cache_read"), ("cacheWrite", "cache_write")):
                    val = usage.get(src)
                    if isinstance(val, (int, float)):
                        tot[dst] += val
                cost = usage.get("cost")
                if isinstance(cost, (int, float)):
                    tot["cost"] += cost
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
    "plan": """Write an implementation plan. Do not write product code.

Task: {task}

Steps:
1. If a ticket reference appears above, fetch it with gh-axi.
2. Read only what you need to write a precise plan.

Write the plan to {plan_path}, strictly under {cap} lines, with exactly these sections:
## acceptance criteria
## files to change
## test seams
## suggested skills

No preamble, no summary, no restating the task. Name real file paths and real test seams.
Reply with only the file path.""",
    "execute": """Implement the plan at {plan_path}. Load the skills it lists under
"suggested skills". Follow its test seams. Run typecheck and the test suite, then commit to
the current branch. Do not rewrite the plan. Reply with only the commit sha.""",
    "review": """Run /code-review since the merge-base, checking the diff against the
acceptance criteria in {plan_path}. Append your findings under a "## review findings"
heading in that file. Report only actionable findings.""",
}


def run_stage(stage, task, route, config, dry_run=False, keep_panes=False, timeout_s=900):
    plan_path = plan_path_for(task)
    brief = BRIEFS[stage].format(task=task, plan_path=plan_path,
                                 cap=config["plan_line_cap"])
    if stage != "plan" and not plan_path.exists():
        print(f"  blocked: {plan_path} does not exist, run the plan stage first")
        return None

    if dry_run:
        print(f"  [{stage}] {route}  (brief -> {plan_path})")
        return {"stage": stage, "model": route, "dry_run": True}

    name = f"jroute-{stage}"
    pane = herdr_json("pane", "split", "--current", "--direction", "right",
                      "--cwd", os.getcwd(), "--no-focus")
    pane_id = pane["result"]["pane"]["pane_id"]
    owned = [pane_id]
    try:
        kind, argv = launch_argv(stage, route, config["effort"].get(stage))
        started = herdr_json("agent", "start", name, "--kind", kind, "--pane", pane_id,
                             "--", *argv)
        session = started["result"]["agent"]["agent_session"].get("value")
        if not dismiss_dialogs(name, pane_id):
            print(f"  {stage}: a dialog is still up, leaving the pane open")
            owned = []
            return None
        herdr("agent", "prompt", name, brief, "--wait", "--timeout", str(timeout_s * 1000))
        usage = read_pi_usage(session) if kind == "pi" and session else None
        record = {"stage": stage, "model": route, "pane": pane_id, "session": session,
                  "usage": usage, "plan": str(plan_path)}
        if usage:
            total = usage["total"]
            record["tokens_total"] = total
            warn = config["token_warn_per_stage"]
            flag = "  OVER BUDGET" if total > warn else ""
            print(f"  {stage}: in {usage['input']} out {usage['output']} "
                  f"cacheRead {usage['cache_read']} total {total}{flag}")
            if total > warn:
                print(f"  warning: {stage} used {total} tokens, over the {warn} budget")
        if stage == "plan" and not plan_path.exists():
            print(f"  {stage}: finished but {plan_path} was not written")
            owned = []
            return record
        return record
    finally:
        if not keep_panes:
            for pane in owned:
                subprocess.run(["herdr", "pane", "close", pane],
                               capture_output=True, text=True)


# --------------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------------

def fmt_reset(seconds):
    if not seconds:
        return ""
    mins = int(seconds // 60)
    return f"{mins // 60}h{mins % 60:02d}m" if mins >= 60 else f"{mins}m"


def cmd_status(config, json_mode=False):
    snaps, errors = probe_all()
    all_models = [m for chain in config["stages"].values() for m in chain]
    ok_ids, excluded = eligible(snaps, config, all_models)

    if json_mode:
        serializable = {}
        for name, snap in snaps.items():
            snap = dict(snap)
            if "auto_bucket" in snap:
                snap["auto_bucket"] = sorted(snap["auto_bucket"])
            serializable[name] = snap
        print(json.dumps({
            "snapshots": serializable,
            "errors": errors,
            "routes": {stage: {"model": resolve(stage, ok_ids, config),
                               "effort": config["effort"].get(stage, "")}
                       for stage in config["stages"]},
            "exclusions": [{"model": mid, "reason": reason} for mid, reason in excluded],
        }))
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


def cmd_run(task, config, args):
    snaps, errors = probe_all()
    for name, err in errors.items():
        print(f"warn: {name} probe failed: {err}", file=sys.stderr)

    stages = [args.stage] if args.stage else ["plan", "execute", "review"]
    all_models = [m for chain in config["stages"].values() for m in chain]
    ok_ids, excluded = eligible(snaps, config, all_models)

    print(f"jroute: {task}\n")
    records = []
    for stage in stages:
        route = resolve(stage, ok_ids, config)
        if not route:
            print(f"  {stage}: NO ELIGIBLE MODEL")
            for mid, reason in excluded:
                if mid in config["stages"][stage]:
                    print(f"      {mid}: {reason}")
            break
        if args.dry_run:
            pass
        print(f"  {stage:8} -> {route}  ({config['effort'].get(stage, '')})  "
              f"pane jroute-{stage}")
        if args.dry_run:
            print(f"      brief -> {plan_path_for(task)}")
            continue
        rec = run_stage(stage, task, route, config,
                        keep_panes=args.keep_panes, timeout_s=args.timeout)
        if rec:
            records.append(rec)
        else:
            print(f"  {stage}: aborted, stopping the pipeline")
            break

    if records:
        HOME.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as fh:
            for rec in records:
                rec["task"] = task
                rec["ts"] = time.time()
                fh.write(json.dumps(rec) + "\n")
        print(f"\nlogged {len(records)} stage(s) to {LOG_PATH}")


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

    run = sub.add_parser("run", help="route and run the pipeline")
    run.add_argument("task")
    run.add_argument("--stage", choices=["plan", "execute", "review"])
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--keep-panes", action="store_true")
    run.add_argument("--timeout", type=int, default=900, help="per-stage seconds")

    log = sub.add_parser("log", help="recent decisions")
    log.add_argument("-n", type=int, default=20)

    args = ap.parse_args()
    config = json.loads(CONFIG_PATH.read_text())

    if args.cmd == "status":
        cmd_status(config, args.json)
    elif args.cmd == "run":
        if not args.dry_run and not inside_herdr():
            sys.exit("jroute run needs $HERDR_ENV=1; use --dry-run outside Herdr")
        cmd_run(args.task, config, args)
    elif args.cmd == "log":
        cmd_log(config, args.n)


if __name__ == "__main__":
    main()
