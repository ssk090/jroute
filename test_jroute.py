#!/usr/bin/env python3
"""Tests for jroute's pure logic: the quota normalizers, the gates, chain resolution,
token accounting, and redaction. These are the seams where routing bugs would hide.

    python3 -m unittest test_jroute -v
"""

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
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


def status_json(snaps, errors, config=CONFIG):
    """Run cmd_status in JSON mode with probe_all stubbed; return (parsed, probe_mock)."""
    buf = io.StringIO()
    with patch.object(jroute, "probe_all", return_value=(snaps, errors)) as probe:
        with redirect_stdout(buf):
            jroute.cmd_status(config, True)
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
        self.assertEqual(set(payload), {"snapshots", "errors", "routes", "exclusions"})
        self.assertEqual(payload["errors"], {})
        probe.assert_called_once()

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
        payload, _ = status_json(ALL_OK, {})
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
        payload, _ = status_json(snaps, {})
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
        status.assert_called_once_with(CONFIG, True)

    def test_status_defaults_to_text_mode(self):
        status = run_main(["status"])
        status.assert_called_once_with(CONFIG, False)

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
