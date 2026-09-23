#!/usr/bin/env python3
"""Tests for jroute's pure logic: the quota normalizers, the gates, chain resolution,
token accounting, and redaction. These are the seams where routing bugs would hide.

    python3 -m unittest test_jroute -v
"""

import json
import unittest

import jroute


CONFIG = {
    "stages": {
        "plan": ["openai-codex/gpt-6-astra", "openai-codex/gpt-5.6-sol",
                 "cursor/gpt-5.6-sol-high", "opencode-go/glm-5.3"],
        "execute": ["opencode-go/deepseek-v4.1-flash", "cursor/gpt-5.4-mini-medium"],
        "review": ["opencode-go/glm-5.3", "cursor/claude-opus-5-high"],
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

OPENCODE_OK = {"provider": "opencode-go", "kind": "flat", "models": ["glm-5.3"]}

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
