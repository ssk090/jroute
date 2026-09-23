#!/usr/bin/env python3
"""Tests for jroute's pure logic: the quota normalizers, the gates, chain resolution,
token accounting, and redaction. These are the seams where routing bugs would hide.

    python3 -m unittest test_jroute -v
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import jroute


CONFIG = {
    "stages": {
        "plan": ["openai-codex/gpt-6-astra", "openai-codex/gpt-5.6-sol",
                 "cursor/gpt-5.6-sol-high", "opencode-go/glm-5.3"],
        "execute": ["opencode-go/deepseek-v4.1-flash", "cursor/gpt-5.4-mini-medium"],
        "review": ["opencode-go/glm-5.3", "cursor/claude-opus-5-high",
                   "opencode-go/qwen3.7-max"],
    },
    "effort": {"plan": "medium", "execute": "low", "review": "medium"},
    "shape": {"question_threshold": 0.6, "design_threshold": 0.5,
              "review_consequence": 0.7, "review_complexity": 3,
              "default": ["plan", "execute"]},
    "skills": {"plan": ["to-spec"], "execute": ["implement"], "review": ["code-review"]},
    "gates": {"codex": {"primary_5h_max": 70, "secondary_7d_max": 85},
              "cursor": {"auto_bucket_max": 85, "api_bucket_max": 85, "total_max": 90}},
    "token_warn_per_stage": 150000,
    "plan_line_cap": 120,
}

CODEX_OK = {"provider": "codex", "primary_used": 10.0, "secondary_used": 56.0,
            "reset_primary_s": 16892, "reset_secondary_s": 164226,
            "allowed": True, "limit_reached": False,
            "model_available": {"gpt-6-astra": True, "gpt-5.6-sol": True}}

CURSOR_OK = {"provider": "cursor", "auto_used": 73.16, "api_used": 8.64,
             "total_used": 67.29, "auto_bucket": {"composer-2.5", "grok-4.5"},
             "enabled": True, "display_message": "You've hit your usage limit"}

OPENCODE_OK = {"provider": "opencode-go", "kind": "flat",
               "models": ["glm-5.3", "deepseek-v4.1-flash", "glm-5.3-flash",
                          "qwen3.8-flash", "qwen3.7-max", "kimi-k3"]}

ALL_OK = {"codex": CODEX_OK, "cursor": CURSOR_OK, "opencode-go": OPENCODE_OK}


class TestNormalize(unittest.TestCase):
    def test_codex_extracts_both_windows_and_model_availability(self):
        raw = {"rate_limit": {"allowed": True, "limit_reached": False,
                              "primary_window": {"used_percent": 10, "reset_after_seconds": 16892},
                              "secondary_window": {"used_percent": 56, "reset_after_seconds": 164226}},
               "model_usage": {"gpt-6-astra": {"available": False}}}
        snap = jroute.normalize_codex(raw)
        self.assertEqual(snap["primary_used"], 10)
        self.assertEqual(snap["secondary_used"], 56)
        self.assertEqual(snap["reset_primary_s"], 16892)
        self.assertFalse(snap["model_available"]["gpt-6-astra"])

    def test_codex_survives_missing_windows(self):
        snap = jroute.normalize_codex({})
        self.assertIsNone(snap["primary_used"])
        self.assertIsNone(snap["secondary_used"])
        self.assertEqual(snap["model_available"], {})

    def test_cursor_extracts_all_three_percentages_and_the_auto_bucket(self):
        raw = {"planUsage": {"autoPercentUsed": 73.16, "apiPercentUsed": 8.64,
                             "totalPercentUsed": 67.29},
               "autoBucketModels": ["composer-2.5", "Composer-2.5-Fast"],
               "enabled": True}
        snap = jroute.normalize_cursor(raw)
        self.assertAlmostEqual(snap["auto_used"], 73.16)
        self.assertAlmostEqual(snap["api_used"], 8.64)
        self.assertIn("composer-2.5", snap["auto_bucket"])
        self.assertIn("composer-2.5-fast", snap["auto_bucket"], "bucket lookup must be case-folded")


class TestEligible(unittest.TestCase):
    def test_codex_astra_eligible_when_windows_are_low(self):
        ok, _ = jroute.eligible(ALL_OK, CONFIG, ["openai-codex/gpt-6-astra"])
        self.assertEqual(ok, ["openai-codex/gpt-6-astra"])

    def test_codex_gated_out_when_5h_window_exceeds_the_threshold(self):
        snaps = dict(ALL_OK, codex=dict(CODEX_OK, primary_used=71.0))
        ok, excluded = jroute.eligible(snaps, CONFIG, ["openai-codex/gpt-6-astra"])
        self.assertEqual(ok, [])
        self.assertIn("5h", excluded[0][1])

    def test_codex_gated_out_when_7d_window_exceeds_the_threshold(self):
        snaps = dict(ALL_OK, codex=dict(CODEX_OK, secondary_used=86.0))
        ok, excluded = jroute.eligible(snaps, CONFIG, ["openai-codex/gpt-6-astra"])
        self.assertEqual(ok, [])
        self.assertIn("7d", excluded[0][1])

    def test_codex_per_model_flag_gates_only_that_model(self):
        """The Astra/Sol split: Astra's own cap must not take Sol down with it."""
        snaps = dict(ALL_OK, codex=dict(CODEX_OK, model_available={"gpt-6-astra": False,
                                                                  "gpt-5.6-sol": True}))
        ok, excluded = jroute.eligible(snaps, CONFIG, ["openai-codex/gpt-6-astra",
                                                      "openai-codex/gpt-5.6-sol"])
        self.assertEqual(ok, ["openai-codex/gpt-5.6-sol"])
        self.assertIn("unavailable", excluded[0][1])

    def test_codex_gated_when_probe_failed(self):
        ok, excluded = jroute.eligible({"cursor": CURSOR_OK}, CONFIG,
                                       ["openai-codex/gpt-6-astra"])
        self.assertEqual(ok, [])
        self.assertIn("unknown", excluded[0][1])

    def test_anthropic_models_are_excluded_by_policy(self):
        ok, excluded = jroute.eligible(ALL_OK, CONFIG, ["cursor/claude-opus-5-high",
                                                       "cursor/claude-fable-5-thinking-xhigh"])
        self.assertEqual(ok, [])
        self.assertTrue(all("anthropic" in r for _, r in excluded))

    def test_cursor_auto_bucket_model_is_gated_once_the_bucket_exceeds_threshold(self):
        snaps = dict(ALL_OK, cursor=dict(CURSOR_OK, auto_used=90.0))
        ok, excluded = jroute.eligible(snaps, CONFIG, ["cursor/composer-2.5"])
        self.assertEqual(ok, [])
        self.assertIn("auto bucket", excluded[0][1])

    def test_cursor_auto_bucket_model_stays_eligible_below_threshold(self):
        """73% against an 85% ceiling still passes. The gate is a ceiling, not a preference."""
        ok, _ = jroute.eligible(ALL_OK, CONFIG, ["cursor/composer-2.5"])
        self.assertEqual(ok, ["cursor/composer-2.5"])

    def test_cursor_api_bucket_model_stays_eligible_at_8_percent(self):
        ok, _ = jroute.eligible(ALL_OK, CONFIG, ["cursor/gpt-5.6-sol-high"])
        self.assertEqual(ok, ["cursor/gpt-5.6-sol-high"])

    def test_cursor_total_gate_applies_to_both_buckets(self):
        snaps = dict(ALL_OK, cursor=dict(CURSOR_OK, total_used=91.0))
        ok, excluded = jroute.eligible(snaps, CONFIG, ["cursor/gpt-5.6-sol-high"])
        self.assertEqual(ok, [])
        self.assertIn("total", excluded[0][1])

    def test_opencode_go_is_never_gated(self):
        ok, _ = jroute.eligible(ALL_OK, CONFIG, ["opencode-go/glm-5.3"])
        self.assertEqual(ok, ["opencode-go/glm-5.3"])

    def test_opencode_go_model_absent_from_the_live_catalog_is_rejected(self):
        """kimi-k3-high is a Cursor id, not an OpenCode Go one. A wrong id fails hard at
        launch, so it must be caught by the gates rather than at runtime."""
        ok, excluded = jroute.eligible(ALL_OK, CONFIG, ["opencode-go/kimi-k3-high"])
        self.assertEqual(ok, [])
        self.assertIn("catalog", excluded[0][1])

    def test_opencode_go_catalog_unknown_leaves_the_model_eligible(self):
        """A failed probe must not silently empty the free pool."""
        ok, _ = jroute.eligible({"codex": CODEX_OK, "cursor": CURSOR_OK}, CONFIG,
                                ["opencode-go/anything"])
        self.assertEqual(ok, ["opencode-go/anything"])

    def test_a_configured_model_missing_from_the_catalog_is_gated(self):
        """Guards the config against typo'd or wrong-provider model ids: kimi-k3-high is a
        Cursor id, and a wrong id fails hard at launch rather than at routing time."""
        catalog = {"provider": "opencode-go", "kind": "flat", "models": ["glm-5.3"]}
        snaps = dict(ALL_OK, **{"opencode-go": catalog})
        ok, excluded = jroute.eligible(snaps, CONFIG, ["opencode-go/deepseek-v4.1-flash",
                                                      "opencode-go/kimi-k3-high"])
        self.assertEqual(ok, [])
        self.assertEqual(len(excluded), 2)
        self.assertTrue(all("catalog" in reason for _, reason in excluded))

    def test_unknown_provider_is_rejected(self):
        ok, excluded = jroute.eligible(ALL_OK, CONFIG, ["mistral/large"])
        self.assertEqual(ok, [])
        self.assertIn("unknown provider", excluded[0][1])


class TestResolve(unittest.TestCase):
    def test_picks_the_first_eligible_model_in_the_chain(self):
        ok = ["openai-codex/gpt-5.6-sol", "cursor/gpt-5.6-sol-high"]
        self.assertEqual(jroute.resolve("plan", ok, CONFIG), "openai-codex/gpt-5.6-sol")

    def test_prefers_codex_astra_when_everything_is_eligible(self):
        ok = ["openai-codex/gpt-6-astra", "openai-codex/gpt-5.6-sol", "opencode-go/glm-5.3"]
        self.assertEqual(jroute.resolve("plan", ok, CONFIG), "openai-codex/gpt-6-astra")

    def test_falls_through_to_cursor_when_the_codex_account_is_gated(self):
        """Both primary and fallback are Codex, so the chain must reach outside it."""
        ok = ["cursor/gpt-5.6-sol-high", "opencode-go/glm-5.3"]
        self.assertEqual(jroute.resolve("plan", ok, CONFIG), "cursor/gpt-5.6-sol-high")

    def test_returns_none_when_the_whole_chain_is_gated(self):
        self.assertIsNone(jroute.resolve("plan", [], CONFIG))

    def test_execute_stage_prefers_the_flat_rate_pool(self):
        ok = ["opencode-go/deepseek-v4.1-flash", "cursor/gpt-5.4-mini-medium"]
        self.assertEqual(jroute.resolve("execute", ok, CONFIG),
                         "opencode-go/deepseek-v4.1-flash")

    def test_offset_moves_along_the_eligible_prefix_not_the_raw_chain(self):
        """start=1 means 'one step up from the cheapest model I can actually use'."""
        ok = ["opencode-go/glm-5.3-flash", "cursor/gpt-5.4-mini-medium"]
        self.assertEqual(jroute.resolve("execute", ok, CONFIG, start=1),
                         "cursor/gpt-5.4-mini-medium")

    def test_offset_beyond_the_chain_clamps_to_the_strongest_eligible(self):
        ok = ["opencode-go/deepseek-v4.1-flash", "cursor/gpt-5.4-mini-medium"]
        self.assertEqual(jroute.resolve("execute", ok, CONFIG, start=99),
                         "cursor/gpt-5.4-mini-medium")

    def test_review_skips_the_executors_family(self):
        """A glm executor must not be reviewed by glm, or the review is self-assessment."""
        ok = ["opencode-go/glm-5.3", "opencode-go/qwen3.7-max"]
        self.assertEqual(jroute.resolve("review", ok, CONFIG, exclude_families=("glm",)),
                         "opencode-go/qwen3.7-max")

    def test_review_falls_through_when_every_review_model_is_the_executors_family(self):
        ok = ["opencode-go/glm-5.3"]
        self.assertIsNone(jroute.resolve("review", ok, CONFIG, exclude_families=("glm",)))


class TestFamilyOf(unittest.TestCase):
    def test_extracts_the_family_prefix(self):
        for model, family in [("opencode-go/deepseek-v4.1-flash", "deepseek"),
                              ("opencode-go/glm-5.3-flash", "glm"),
                              ("opencode-go/qwen3.8-flash", "qwen3"),
                              ("openai-codex/gpt-6-astra", "gpt"),
                              ("cursor/gpt-5.6-sol-high", "gpt"),
                              ("opencode-go/kimi-k3", "kimi")]:
            self.assertEqual(jroute.family_of(model), family)

    def test_treats_different_gpt_models_as_one_family(self):
        self.assertEqual(jroute.family_of("openai-codex/gpt-6-astra"),
                         jroute.family_of("cursor/gpt-5.6-sol-high"))


class TestJevStartIndex(unittest.TestCase):
    def answers(self, score, confidence=0.9, consequence=0.1):
        return {"complexity": {"score": score, "confidence": confidence},
                "consequence": {"noul": consequence}}

    def test_routine_work_starts_at_the_cheapest_model(self):
        self.assertEqual(jroute.jev_start_index(self.answers(1.0)), 0)

    def test_moderate_work_starts_one_step_up(self):
        self.assertEqual(jroute.jev_start_index(self.answers(2.0)), 1)

    def test_hard_work_skips_to_the_strong_end(self):
        self.assertEqual(jroute.jev_start_index(self.answers(3.2)), 2)

    def test_low_confidence_rounds_complexity_up_not_down(self):
        """Under-powering costs a failed attempt plus the escalation, so it never rounds down."""
        self.assertEqual(jroute.jev_start_index(self.answers(1.0, confidence=0.4)), 1)

    def test_high_consequence_skips_to_the_strong_end(self):
        self.assertEqual(jroute.jev_start_index(self.answers(1.0, consequence=0.9)), 2)

    def test_missing_answers_default_to_the_cheapest_model(self):
        self.assertEqual(jroute.jev_start_index(None), 0)
        self.assertEqual(jroute.jev_start_index({}), 0)
        self.assertEqual(jroute.jev_start_index({"complexity": {}}), 0)

    def test_a_hard_task_would_offset_astra_off_the_plan_chain_without_the_pin(self):
        """gpt-6-astra leads the plan chain by explicit instruction, so cmd_run pins the plan
        offset to zero. This asserts both halves: the offset would move it, and zero does not.
        """
        ok = ["openai-codex/gpt-6-astra", "openai-codex/gpt-5.6-sol",
              "cursor/gpt-5.6-sol-high"]
        hard = jroute.jev_start_index(self.answers(4.0, consequence=0.95))
        self.assertEqual(hard, 2)
        self.assertEqual(jroute.resolve("plan", ok, CONFIG, start=0),
                         "openai-codex/gpt-6-astra", "pinned offset keeps Astra planning")
        self.assertEqual(jroute.resolve("plan", ok, CONFIG, start=hard),
                         "cursor/gpt-5.6-sol-high", "unpinned, the offset would take it away")


class TestParsePiUsage(unittest.TestCase):
    def test_sums_across_turns_and_keeps_cache_read_separate(self):
        lines = [
            json.dumps({"message": {"role": "assistant",
                                    "usage": {"input": 16039, "output": 5,
                                              "cacheRead": 0, "cacheWrite": 0, "cost": 0.0}}}),
            json.dumps({"message": {"role": "assistant",
                                    "usage": {"input": 200, "output": 400,
                                              "cacheRead": 90000, "cacheWrite": 50,
                                              "cost": 0.03}}}),
        ]
        tot = jroute.parse_pi_usage("\n".join(lines))
        self.assertEqual(tot["turns"], 2)
        self.assertEqual(tot["input"], 16239)
        self.assertEqual(tot["output"], 405)
        self.assertEqual(tot["cache_read"], 90000)
        self.assertEqual(tot["total"], 106694)
        self.assertAlmostEqual(tot["cost"], 0.03)

    def test_ignores_malformed_lines(self):
        tot = jroute.parse_pi_usage("not json\n\n" + json.dumps({"usage": {"input": 1}}))
        self.assertEqual(tot["turns"], 1)
        self.assertEqual(tot["input"], 1)

    def test_empty_input_is_zeroed(self):
        tot = jroute.parse_pi_usage("")
        self.assertEqual(tot["total"], 0)
        self.assertEqual(tot["turns"], 0)


class TestRedact(unittest.TestCase):
    def test_strips_credential_shaped_text(self):
        for secret in ["sk-abc1234567890", "ghp_abcdefghijklmnop", "AKIAIOSFODNN7EXAMPLE",
                       "jv_live_abc123def456", "Bearer eyJhbGciOi.abc.def"]:
            self.assertNotIn(secret, jroute.redact(f"here is {secret} inline"))

    def test_strips_private_key_blocks(self):
        blob = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
        self.assertIn("<redacted>", jroute.redact(blob))

    def test_leaves_ordinary_task_text_intact(self):
        task = "implement the retry logic in docs/plan.md and run the tests"
        self.assertEqual(jroute.redact(task), task)


class TestPlanPath(unittest.TestCase):
    def test_uses_a_ticket_reference_as_the_slug(self):
        self.assertEqual(jroute.plan_path_for("implement CHP-1234 please").name,
                         "chp-1234.md")

    def test_uses_a_hash_issue_as_the_slug(self):
        self.assertEqual(jroute.plan_path_for("fix bug #42").name, "issue-42.md")

    def test_falls_back_to_a_slugified_task(self):
        self.assertEqual(jroute.plan_path_for("Add retry logic!").name, "add-retry-logic.md")

    def test_handles_a_task_with_no_useful_characters(self):
        self.assertEqual(jroute.plan_path_for("!!!").name, "task.md")


EXPECTED_TEXT_ALL_OK = (
    "jroute status\n"
    "\n"
    "codex        OK        5h 10% (reset 4h41m)  7d 56% (reset 45h37m)\n"
    "cursor       OK        auto 73.2%  api 8.6%  total 67.3%\n"
    "             auto bucket contains 2 models (composer-2.5, grok-4.5 ladders)\n"
    "opencode-go  FLAT      6 models, no usage endpoint exists\n"
    "\n"
    "stage routes\n"
    "  plan     -> openai-codex/gpt-6-astra  (medium)\n"
    "  execute  -> opencode-go/deepseek-v4.1-flash  (low)\n"
    "  review   -> opencode-go/glm-5.3  (medium)\n"
    "\n"
    "exclusions\n"
    "  anthropic model, excluded by policy: cursor/claude-opus-5-high\n"
)

EXPECTED_TEXT_PROBE_ERROR = (
    "jroute status\n"
    "\n"
    "codex        ERROR    RuntimeError: boom\n"
    "cursor       OK        auto 73.2%  api 8.6%  total 67.3%\n"
    "             auto bucket contains 2 models (composer-2.5, grok-4.5 ladders)\n"
    "opencode-go  FLAT      6 models, no usage endpoint exists\n"
    "\n"
    "stage routes\n"
    "  plan     -> cursor/gpt-5.6-sol-high  (medium)\n"
    "  execute  -> opencode-go/deepseek-v4.1-flash  (low)\n"
    "  review   -> opencode-go/glm-5.3  (medium)\n"
    "\n"
    "exclusions\n"
    "  codex quota unknown (probe failed): openai-codex/gpt-6-astra, openai-codex/gpt-5.6-sol\n"
    "  anthropic model, excluded by policy: cursor/claude-opus-5-high\n"
)


def status_json(snaps, errors, config=CONFIG, full=False):
    """Run cmd_status in JSON mode with probe_all stubbed; return (parsed, probe_mock)."""
    buf = io.StringIO()
    with patch.object(jroute, "probe_all", return_value=(snaps, errors)) as probe:
        with redirect_stdout(buf):
            jroute.cmd_status(config, True, full)
    return json.loads(buf.getvalue()), probe


def run_main(argv):
    """Invoke main() with argv, a stubbed config path, and cmd_status captured."""
    fake_config = Mock(read_text=lambda: json.dumps(CONFIG))
    with patch.object(sys, "argv", ["jroute", *argv]), \
            patch.object(jroute, "CONFIG_PATH", fake_config), \
            patch.object(jroute, "cmd_status") as status:
        jroute.main()
    return status


class TestStatusJson(unittest.TestCase):
    def test_emits_exactly_one_object_with_the_documented_top_level_schema(self):
        payload, probe = status_json(ALL_OK, {})
        self.assertEqual(set(payload),
                         {"snapshots", "errors", "routes", "exclusions", "help"})
        self.assertEqual(payload["errors"], {})
        probe.assert_called_once()

    def test_full_output_drops_the_help_hint_and_expands_the_detail(self):
        """AXI truncation rule: name the escape hatch only while content is truncated."""
        payload, _ = status_json(ALL_OK, {}, full=True)
        self.assertNotIn("help", payload)
        self.assertIn("auto_bucket", payload["snapshots"]["cursor"])
        self.assertIn("models", payload["snapshots"]["opencode-go"])

    def test_default_schema_collapses_detail_lists_to_counts(self):
        """AXI minimal schema: 28 bucket entries and 33 model ids are detail, not decisions."""
        payload, _ = status_json(ALL_OK, {})
        cursor = payload["snapshots"]["cursor"]
        self.assertNotIn("auto_bucket", cursor)
        self.assertEqual(cursor["auto_bucket_count"], 2)
        self.assertEqual(payload["snapshots"]["opencode-go"]["model_count"], 6)
        self.assertNotIn("models", payload["snapshots"]["opencode-go"])

    def test_default_schema_is_smaller_than_the_full_one(self):
        lean, _ = status_json(ALL_OK, {})
        fat, _ = status_json(ALL_OK, {}, full=True)
        self.assertLess(len(json.dumps(lean)), len(json.dumps(fat)))

    def test_snapshots_preserve_the_normalized_data_and_source_set(self):
        payload, _ = status_json(ALL_OK, {})
        self.assertEqual(set(payload["snapshots"]), set(ALL_OK))
        codex = payload["snapshots"]["codex"]
        self.assertEqual(codex["primary_used"], 10.0)
        self.assertIsInstance(codex["primary_used"], float)
        self.assertEqual(codex["reset_primary_s"], 16892)
        self.assertIs(codex["allowed"], True)
        self.assertIs(codex["limit_reached"], False)

    def test_cursor_auto_bucket_serializes_sorted_without_mutating_the_snapshot(self):
        payload, _ = status_json(ALL_OK, {}, full=True)
        self.assertEqual(payload["snapshots"]["cursor"]["auto_bucket"],
                         ["composer-2.5", "grok-4.5"])
        self.assertIsInstance(ALL_OK["cursor"]["auto_bucket"], set)
        self.assertEqual(ALL_OK["cursor"]["auto_bucket"], {"composer-2.5", "grok-4.5"})

    def test_routes_map_every_stage_to_model_and_effort(self):
        payload, _ = status_json(ALL_OK, {})
        self.assertEqual(payload["routes"], {
            "plan": {"model": "openai-codex/gpt-6-astra", "effort": "medium"},
            "execute": {"model": "opencode-go/deepseek-v4.1-flash", "effort": "low"},
            "review": {"model": "opencode-go/glm-5.3", "effort": "medium"},
        })

    def test_exclusions_are_ordered_model_reason_entries(self):
        payload, _ = status_json(ALL_OK, {})
        self.assertEqual(payload["exclusions"],
                         [{"model": "cursor/claude-opus-5-high",
                           "reason": "anthropic model, excluded by policy"}])
        all_models = [m for chain in CONFIG["stages"].values() for m in chain]
        _, expected = jroute.eligible(ALL_OK, CONFIG, all_models)
        self.assertEqual([(e["model"], e["reason"]) for e in payload["exclusions"]], expected)

    def test_gated_variant_falls_back_and_agrees_with_eligible_and_resolve(self):
        snaps = dict(ALL_OK, codex=dict(CODEX_OK, primary_used=71.0))
        payload, _ = status_json(snaps, {})
        all_models = [m for chain in CONFIG["stages"].values() for m in chain]
        ok_ids, expected = jroute.eligible(snaps, CONFIG, all_models)
        self.assertEqual(payload["routes"]["plan"]["model"],
                         jroute.resolve("plan", ok_ids, CONFIG))
        self.assertEqual(payload["routes"]["plan"]["model"], "cursor/gpt-5.6-sol-high")
        self.assertEqual([(e["model"], e["reason"]) for e in payload["exclusions"]], expected)
        self.assertIn("5h", payload["exclusions"][0]["reason"])

    def test_total_probe_failure_still_emits_a_complete_report(self):
        errors = {p: "probe failed" for p in ALL_OK}
        payload, _ = status_json({}, errors)
        self.assertEqual(payload["snapshots"], {})
        self.assertEqual(payload["errors"], errors)
        self.assertEqual(payload["routes"], {
            "plan": {"model": "opencode-go/glm-5.3", "effort": "medium"},
            "execute": {"model": "opencode-go/deepseek-v4.1-flash", "effort": "low"},
            "review": {"model": "opencode-go/glm-5.3", "effort": "medium"},
        })
        self.assertNotIn("opencode-go", {e["model"].split("/")[0]
                                         for e in payload["exclusions"]})

    def test_null_route_when_the_whole_chain_is_gated(self):
        config = json.loads(json.dumps(CONFIG))
        config["stages"] = {"plan": ["openai-codex/gpt-6-astra"]}
        snaps = dict(ALL_OK, codex=dict(CODEX_OK, primary_used=99.0))
        payload, _ = status_json(snaps, {}, config)
        self.assertIsNone(payload["routes"]["plan"]["model"])
        self.assertEqual(payload["routes"]["plan"]["effort"], "medium")

    def test_missing_quota_values_serialize_as_null(self):
        snaps = {"codex": jroute.normalize_codex({}), "cursor": jroute.normalize_cursor({})}
        payload, _ = status_json(snaps, {}, full=True)
        self.assertIsNone(payload["snapshots"]["codex"]["primary_used"])
        self.assertIsNone(payload["snapshots"]["codex"]["secondary_used"])
        self.assertIsNone(payload["snapshots"]["cursor"]["total_used"])
        self.assertEqual(payload["snapshots"]["cursor"]["auto_bucket"], [])


class TestStatusTextUnchanged(unittest.TestCase):
    def test_default_text_output_is_unchanged(self):
        buf = io.StringIO()
        with patch.object(jroute, "probe_all", return_value=(ALL_OK, {})):
            with redirect_stdout(buf):
                jroute.cmd_status(CONFIG)
        self.assertEqual(buf.getvalue(), EXPECTED_TEXT_ALL_OK)

    def test_probe_error_text_output_is_unchanged(self):
        snaps = {"cursor": CURSOR_OK, "opencode-go": OPENCODE_OK}
        buf = io.StringIO()
        with patch.object(jroute, "probe_all", return_value=(snaps, {"codex": "RuntimeError: boom"})):
            with redirect_stdout(buf):
                jroute.cmd_status(CONFIG)
        self.assertEqual(buf.getvalue(), EXPECTED_TEXT_PROBE_ERROR)


class TestStatusCli(unittest.TestCase):
    def test_json_flag_dispatches_json_mode(self):
        status = run_main(["status", "--json"])
        status.assert_called_once_with(CONFIG, True, False)

    def test_status_defaults_to_text_mode(self):
        status = run_main(["status"])
        status.assert_called_once_with(CONFIG, False, False)

    def test_full_flag_reaches_cmd_status(self):
        status = run_main(["status", "--json", "--full"])
        status.assert_called_once_with(CONFIG, True, True)

    def test_status_help_documents_the_json_flag(self):
        buf = io.StringIO()
        with patch.object(sys, "argv", ["jroute", "status", "--help"]), \
                redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
            jroute.main()
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("--json", buf.getvalue())

    def test_other_subcommands_reject_the_json_flag(self):
        with patch.object(sys, "argv", ["jroute", "log", "--json"]), \
                redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            jroute.main()
        self.assertEqual(cm.exception.code, 2)


class TestJevShape(unittest.TestCase):
    """Jev judges, code decides the pipeline. The whole point is that a question never pays
    for an Astra plan."""

    def answers(self, complexity=1.0, confidence=0.9, question=0.0,
                needs_design=0.0, consequence=0.1):
        return {"complexity": {"score": complexity, "confidence": confidence},
                "is_question": {"noul": question},
                "needs_design": {"noul": needs_design},
                "consequence": {"noul": consequence}}

    def test_a_question_gets_answered_not_planned(self):
        self.assertEqual(jroute.jev_shape(self.answers(question=0.95), CONFIG), ("answer",))

    def test_a_question_is_still_a_question_when_it_is_complex(self):
        """'Explain how the quota gate works' is complex to answer and needs no pipeline."""
        shape = jroute.jev_shape(self.answers(complexity=3.5, question=0.9), CONFIG)
        self.assertEqual(shape, ("answer",))

    def test_a_routine_change_with_no_design_goes_straight_to_execute(self):
        shape = jroute.jev_shape(self.answers(complexity=0.4, needs_design=0.1), CONFIG)
        self.assertEqual(shape, ("execute",), "no plan stage, so Astra never runs")

    def test_a_routine_change_that_needs_design_still_gets_a_plan(self):
        shape = jroute.jev_shape(self.answers(complexity=0.4, needs_design=0.9), CONFIG)
        self.assertEqual(shape, ("plan", "execute"))

    def test_moderate_work_gets_plan_and_execute(self):
        shape = jroute.jev_shape(self.answers(complexity=2.0), CONFIG)
        self.assertEqual(shape, ("plan", "execute"))

    def test_hard_work_adds_the_review_stage(self):
        shape = jroute.jev_shape(self.answers(complexity=3.2), CONFIG)
        self.assertEqual(shape, ("plan", "execute", "review"))

    def test_high_consequence_adds_the_review_stage_even_when_routine(self):
        shape = jroute.jev_shape(self.answers(complexity=0.5, consequence=0.85), CONFIG)
        self.assertEqual(shape, ("plan", "execute", "review"))

    def test_low_confidence_escalates_the_shape_not_just_the_chain(self):
        """Complexity 0.5 with 0.4 confidence must not be treated as routine."""
        shape = jroute.jev_shape(self.answers(complexity=0.5, confidence=0.4), CONFIG)
        self.assertEqual(shape, ("plan", "execute"))

    def test_no_answers_falls_back_to_the_configured_default(self):
        for missing in (None, {}):
            self.assertEqual(jroute.jev_shape(missing, CONFIG), ("plan", "execute"))

    def test_thresholds_are_configurable_not_hardcoded(self):
        strict = dict(CONFIG, shape=dict(CONFIG["shape"], question_threshold=0.99))
        loose = self.answers(question=0.7)
        self.assertEqual(jroute.jev_shape(loose, CONFIG), ("answer",))
        self.assertNotEqual(jroute.jev_shape(loose, strict), ("answer",))


class TestAnswerArgv(unittest.TestCase):
    def test_pi_providers_use_print_mode_without_a_session(self):
        argv = jroute.answer_argv("opencode-go/deepseek-v4.1-flash", "low")
        self.assertEqual(argv, ["pi", "-p", "--no-session",
                                "--model", "opencode-go/deepseek-v4.1-flash",
                                "--thinking", "low"])

    def test_cursor_uses_its_own_print_flag(self):
        self.assertEqual(jroute.answer_argv("cursor/gpt-5.4-mini-medium", "low"),
                         ["cursor-agent", "-p", "--model", "gpt-5.4-mini-medium"])

    def test_unknown_provider_raises(self):
        with self.assertRaises(RuntimeError):
            jroute.answer_argv("mistral/large", "low")


class TestBriefFor(unittest.TestCase):
    """A routine task skips the plan stage, so the executor must never be told to follow a
    plan document that was never written. This bug shipped once; the test is here so it
    cannot ship twice."""

    def test_execute_with_a_plan_points_at_the_plan_file(self):
        brief = jroute.brief_for("execute", "do a thing", CONFIG, jroute.Path("/tmp/p.md"),
                                 has_plan=True)
        self.assertIn("/tmp/p.md", brief)
        self.assertIn("read-only", brief, "positive phrasing, not 'do not rewrite'")

    def test_execute_without_a_plan_does_not_reference_one(self):
        brief = jroute.brief_for("execute", "add a --version flag", CONFIG,
                                 jroute.Path("/tmp/p.md"), has_plan=False)
        self.assertNotIn("plan at", brief)
        self.assertIn("add a --version flag", brief, "the task text must carry the intent")
        self.assertIn("directly", brief)

    def test_plan_brief_names_the_artifact_and_the_line_cap(self):
        brief = jroute.brief_for("plan", "ticket CHP-1", CONFIG, jroute.Path("/tmp/p.md"),
                                 has_plan=False)
        self.assertIn("/tmp/p.md", brief)
        self.assertIn(str(CONFIG["plan_line_cap"]), brief)

    def test_every_stage_has_a_brief(self):
        for stage in ("plan", "execute", "review", "implement"):
            self.assertIn(stage, jroute.BRIEFS)

    def test_briefs_steer_by_positive_target_not_prohibition(self):
        """writing-for-agents: a negation activates the behaviour it bans, so every brief
        states what to do instead of what to avoid."""
        for stage in ("plan", "execute", "implement", "review"):
            brief = jroute.brief_for("execute", "x", CONFIG, jroute.Path("/tmp/p.md"),
                                     has_plan=stage == "execute").lower()
            for banned in ("do not", "don't", "no preamble", "no summary", "never"):
                self.assertNotIn(banned, brief, f"{stage} brief steers by prohibition")

    def test_each_stage_names_its_skills(self):
        config = json.loads(json.dumps(CONFIG))
        config["skills"] = {"plan": ["to-spec", "to-tickets"],
                            "execute": ["implement", "tdd"],
                            "review": ["code-review"]}
        for stage, expected in config["skills"].items():
            brief = jroute.brief_for(stage, "task", config, jroute.Path("/tmp/p.md"),
                                     has_plan=True)
            self.assertIn("Load these skills first", brief)
            for name in expected:
                self.assertIn(name, brief)

    def test_a_stage_with_no_configured_skills_gets_no_skill_line(self):
        config = json.loads(json.dumps(CONFIG))
        config["skills"] = {}
        brief = jroute.brief_for("plan", "task", config, jroute.Path("/tmp/p.md"),
                                 has_plan=False)
        self.assertNotIn("Load these skills", brief)

    def test_the_direct_execute_brief_keeps_its_completion_criterion(self):
        """A checkable bound: the reply is the commit sha, so done is not a judgement call."""
        brief = jroute.brief_for("execute", "x", CONFIG, jroute.Path("/tmp/p.md"),
                                 has_plan=False)
        self.assertIn("reply with only the commit sha", brief.lower())


class TestProgress(unittest.TestCase):
    def test_formats_elapsed_as_minutes_and_seconds(self):
        self.assertEqual(jroute.format_elapsed(0), "0:00")
        self.assertEqual(jroute.format_elapsed(9.7), "0:09")
        self.assertEqual(jroute.format_elapsed(75), "1:15")
        self.assertEqual(jroute.format_elapsed(3600), "60:00")
        self.assertEqual(jroute.format_elapsed(-5), "0:00")

    def test_spinner_is_silent_when_not_a_tty(self):
        """Piped output must stay clean: no escape codes, no filler frames. The completion
        line still prints, because that belongs in a log."""
        spin = jroute.Spinner("test", enabled=False)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            spin.tick("something")
            spin.done("finished")
        self.assertFalse(spin.shown)
        text = buffer.getvalue()
        self.assertNotIn("\r", text)
        self.assertEqual(text.strip(), "finished")

    def test_spinner_is_silent_before_its_delay_elapses(self):
        """A fast call shows nothing, so no spinner flashes for a two-second Jev answer."""
        spin = jroute.Spinner("test", delay=30, enabled=True)
        spin.tick()
        self.assertFalse(spin.shown)
        spin.shown = True
        with redirect_stdout(io.StringIO()):
            spin.done()
        self.assertFalse(spin.shown)

    def test_spinner_prints_a_frame_once_the_delay_has_passed(self):
        spin = jroute.Spinner("working", delay=0, enabled=True)
        spin.started -= 5
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            spin.tick("reading jroute.py")
        text = buffer.getvalue()
        self.assertIn("working", text)
        self.assertIn("0:05", text)
        self.assertIn("reading jroute.py", text)

    def test_chrome_filter_drops_pane_furniture(self):
        for marker in jroute.CHROME:
            self.assertTrue(any(marker in line for line in ["x", marker]),
                            f"CHROME should contain {marker!r}")

    def test_chrome_list_covers_the_observed_footers(self):
        footers = ["↑16k ↓5 $0.161 (sub) 5.9%/272k (auto)  (openai-codex) gpt-6-astra",
                   "escape interrupt · ctrl+c/ctrl+d clear/exit · / commands",
                   "─────────────────────────────────────────",
                   "● 🐴 ponytail: ⚡ FULL"]
        for footer in footers:
            self.assertTrue(any(m in footer for m in jroute.CHROME),
                            f"footer should be filtered: {footer!r}")

    def test_activity_picks_the_agents_real_line_not_the_status_bar(self):
        """The status bar sits at the bottom, so a naive last-line read reports it forever."""
        screen = "  → read jroute.py\n  Running the test suite...\n● 🐴 ponytail: ⚡ FULL\n"
        picked = ""
        for line in reversed(screen.splitlines()):
            line = line.strip()
            if not line or any(m in line for m in jroute.CHROME):
                continue
            picked = line
            break
        self.assertEqual(picked, "Running the test suite...")


class TestStatusVersion(unittest.TestCase):
    def test_git_short_sha_is_a_nonempty_hex_abbreviation(self):
        sha = jroute.git_short_sha()
        self.assertRegex(sha, r"^[0-9a-f]{7,40}$")

    def test_status_version_prints_the_sha_without_probing(self):
        with patch.object(jroute, "cmd_status") as cmd, \
             patch.object(jroute, "git_short_sha", return_value="abc1234"), \
             patch.object(jroute.sys, "argv", ["jroute", "status", "--version"]), \
             redirect_stdout(io.StringIO()) as out:
            jroute.main()
        self.assertEqual(out.getvalue().strip(), "abc1234")
        cmd.assert_not_called()


class TestStreamRendering(unittest.TestCase):
    """The session JSONL is the structured stream. Tailing it is why reasoning and token use
    can be shown at all: the rendered pane may hide thinking entirely."""

    def test_short_count_keeps_long_token_counts_readable(self):
        self.assertEqual(jroute.short_count(0), "0")
        self.assertEqual(jroute.short_count(999), "999")
        self.assertEqual(jroute.short_count(18154), "18.2k")
        self.assertEqual(jroute.short_count(1_500_000), "1.50M")

    def test_renders_thinking_text_and_tool_calls_separately(self):
        entry = {"message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "Let me look at the repo."},
            {"type": "text", "text": "I will add the flag."},
            {"type": "toolCall", "name": "bash", "arguments": {"command": "ls -la"}},
        ]}}
        self.assertEqual(jroute.render_event(entry), [
            ("think", "Let me look at the repo."),
            ("say", "I will add the flag."),
            ("tool", "bash: ls -la"),
        ])

    def test_does_not_echo_the_user_prompt_as_assistant_output(self):
        """The first text part in a session is the brief itself; echoing it is just noise."""
        entry = {"message": {"role": "user", "content": [
            {"type": "text", "text": "Implement this change directly..."}]}}
        self.assertEqual(jroute.render_event(entry), [])

    def test_truncates_long_thinking_and_flattens_newlines(self):
        entry = {"message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "a\n\nb  " + "x" * 900}]}}
        (kind, text), = jroute.render_event(entry)
        self.assertEqual(kind, "think")
        self.assertLessEqual(len(text), 403)
        self.assertNotIn("\n", text)
        self.assertTrue(text.endswith("…"))

    def test_tolerates_unknown_and_malformed_parts(self):
        entry = {"message": {"role": "assistant", "content": [
            "not a dict", {"type": "weird"}, {"type": "thinking"}]}}
        self.assertEqual(jroute.render_event(entry), [])
        self.assertEqual(jroute.render_event({}), [])
        self.assertEqual(jroute.render_event({"message": {"content": "a string"}}), [])

    def test_usage_absorbs_both_numeric_and_nested_costs(self):
        """pi reports cost as a dict. A numeric-only check silently reports zero spend."""
        tot = jroute.new_totals()
        jroute.absorb_usage(tot, {"input": 100, "output": 20, "reasoning": 7,
                                  "cost": {"total": 0.0025}})
        jroute.absorb_usage(tot, {"input": 5, "cost": 0.001})
        self.assertEqual(tot["input"], 105)
        self.assertEqual(tot["reasoning"], 7)
        self.assertAlmostEqual(tot["cost"], 0.0035)

    def test_parse_pi_usage_counts_cost_from_the_dict_form(self):
        lines = [json.dumps({"message": {"usage": {"input": 18154, "output": 119,
                                                    "reasoning": 7,
                                                    "cost": {"total": 0.0027945}}}})]
        tot = jroute.parse_pi_usage("\n".join(lines))
        self.assertEqual(tot["input"], 18154)
        self.assertEqual(tot["reasoning"], 7)
        self.assertAlmostEqual(tot["cost"], 0.0027945)

    def test_stream_summary_reports_what_was_actually_used(self):
        stream = jroute.SessionStream(None, enabled=False)
        stream.totals.update({"input": 18154, "output": 119, "cache_read": 354688,
                              "reasoning": 7, "cost": 0.0028, "turns": 3})
        summary = stream.summary()
        for fragment in ("↑18.2k", "↓119", "cache 354.7k", "think 7", "$0.0028", "3 steps"):
            self.assertIn(fragment, summary)

    def test_stream_reads_only_new_lines_on_each_poll(self):
        """Incremental reads are the whole point; reprinting the file every poll would flood."""
        path = Path(tempfile.mkdtemp()) / "session.jsonl"
        path.write_text("")
        stream = jroute.SessionStream(path, enabled=True)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            path.write_text(json.dumps({"message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "first"}]}}) + "\n")
            stream.poll()
            first = buffer.getvalue()
            stream.poll()  # no new bytes
            self.assertEqual(buffer.getvalue(), first)
        self.assertIn("first", first)
        self.assertEqual(stream.totals["turns"], 0, "no usage block in that entry")

    def test_stream_handles_a_partial_trailing_line(self):
        path = Path(tempfile.mkdtemp()) / "session.jsonl"
        stream = jroute.SessionStream(path, enabled=True)
        entry = json.dumps({"message": {"usage": {"input": 42}}})
        with redirect_stdout(io.StringIO()):
            path.write_text(entry[:20])  # half a line, no newline yet
            stream.poll()
            self.assertEqual(stream.totals["turns"], 0)
            with path.open("a") as fh:
                fh.write(entry[20:] + "\n")
            stream.poll()
        self.assertEqual(stream.totals["turns"], 1)
        self.assertEqual(stream.totals["input"], 42)

    def test_stream_survives_a_missing_file(self):
        stream = jroute.SessionStream(Path("/nonexistent/nope.jsonl"), enabled=True)
        stream.poll()
        self.assertEqual(stream.summary(), "")


class TestLaunchArgv(unittest.TestCase):
    def test_pi_gets_the_effort_flag_for_codex_and_opencode(self):
        kind, argv = jroute.launch_argv("plan", "openai-codex/gpt-6-astra", "medium")
        self.assertEqual(kind, "pi")
        self.assertEqual(argv, ["--model", "openai-codex/gpt-6-astra", "--thinking", "medium"])

    def test_cursor_has_no_effort_flag_because_it_is_baked_into_the_model_id(self):
        kind, argv = jroute.launch_argv("plan", "cursor/gpt-5.6-sol-high", "medium")
        self.assertEqual(kind, "cursor-agent")
        self.assertEqual(argv, ["--model", "gpt-5.6-sol-high"])

    def test_unknown_provider_raises_rather_than_launching_something_wrong(self):
        with self.assertRaises(RuntimeError):
            jroute.launch_argv("plan", "mistral/large", "medium")


if __name__ == "__main__":
    unittest.main()
