"""No network, DB, model, or paid judge calls in these integration checks."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(HERE.parents[1]))
from benchmarks import run_public_eval as runner

spec = importlib.util.spec_from_file_location("public_manager", HERE / "manage_fixture.py")
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


class PublicRunnerTest(unittest.TestCase):
    @staticmethod
    def ali_fixture():
        transcript = "张三：周五前整理会议材料。李四：我来负责初稿。"
        case = {"id": "ali_offline_1", "split": "test", "title": "测试会议",
                "transcript": transcript,
                "segments": [{"start_ms": 0, "end_ms": 5000, "speaker": "张三", "text": transcript}],
                "transcript_sha256": runner.sha256(transcript.encode("utf-8")),
                "agent_review_input_limit_chars": 100000,
                "agent_review_transcript_over_limit": False,
                "gold": {"action_ids": ["SECRET_GOLD_ACTION_SENTINEL"]}}
        manifest = {"schema_version": 1, "dataset_id": runner.ALIMEETING_DATASET_ID,
                    "source": {"revision": "offline-test"},
                    "agent_review_input_limit_chars": 100000, "cases": [case]}
        return manifest, case

    @staticmethod
    def valid_minutes():
        return {"summary": "已确认材料初稿负责人。", "topics": [], "viewpoints": [],
                "decisions": [], "pending_items": [], "risks": []}

    @classmethod
    def setUpClass(cls):
        cls.manifest, cls.cases, cls.manifest_hash = runner.load_inputs(HERE / "manifest.json", [])
        cls.full_manifest, cls.full_cases, cls.full_manifest_hash = runner.load_inputs(HERE / "manifest_all_dev.json", [])
        cls.fixed_manifest, cls.fixed_cases, cls.fixed_manifest_hash = runner.load_inputs(
            HERE / "manifest_test_chunk6000.json", [])

    def test_actual_product_budget_is_single_pass(self):
        plan = runner.make_plan(self.cases, 2, 27)
        # 单遍长上下文：生成每场恰 1 次，预留 = 1 + 2 轮×3 次 + 2 重试余量 = 9/场
        # （分块时代同集为 [9,10,12]/31——预算锁逼出每次调用结构变更的显式声明）
        self.assertEqual([row["reserved_calls"] for row in plan["cases"]], [9, 9, 9])
        self.assertEqual(plan["reserved_llm_calls"], 27)
        self.assertEqual([r["agent_context_truncated_by_product"] for r in plan["cases"]], [False, False, True])

    def test_official_reference_text_never_enters_model_payload(self):
        for case in self.full_cases:
            payload = runner.case_request(self.full_manifest, case, self.full_manifest_hash,
                                          token="offline-test", run_id="offline-test", meeting_id=1,
                                          rounds=2, case_limit=8, run_limit=20,
                                          results_subdir="offline-test", timeout_seconds=1)
            serialized = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn("reference_overall", serialized)
            self.assertNotIn(case["reference_overall"], serialized)
            self.assertEqual(payload["transcript"], case["transcript"])
            self.assertEqual(payload["source"]["transcript_sha256"], case["transcript_sha256"])

    def test_ali_manifest_dispatch_and_payload_omit_gold(self):
        manifest, case = self.ali_fixture()
        with tempfile.TemporaryDirectory(dir=HERE) as temp:
            path = Path(temp) / "ali.json"
            path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            adapter = types.ModuleType("alimeeting_adapter")
            adapter.load_manifest = lambda _: manifest
            with patch.dict(sys.modules, {adapter.__name__: adapter}):
                loaded, cases, manifest_hash = runner.load_inputs(path, [])
        self.assertEqual(cases, [case])
        self.assertEqual(runner.dataset_name(loaded), "AliMeeting4MUG")
        payload = runner.case_request(loaded, case, manifest_hash,
                                      token="offline-test", run_id="offline-test", meeting_id=1,
                                      rounds=2, case_limit=5, run_limit=5,
                                      results_subdir="offline-test", timeout_seconds=1)
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("SECRET_GOLD_ACTION_SENTINEL", serialized)
        self.assertNotIn('"gold"', serialized)
        self.assertNotIn("reference_overall", serialized)
        self.assertEqual(payload["source"]["dataset_name"], "AliMeeting4MUG")
        self.assertEqual(payload["source"]["transcript_sha256"], case["transcript_sha256"])
        self.assertNotIn("reference_sha256", runner.make_plan(cases, 2, 5)["cases"][0])

    def test_ali_scoring_reports_only_completion_and_schema_validity(self):
        manifest, case = self.ali_fixture()
        minutes = self.valid_minutes()
        records = [{"case_id": case["id"], "status": "completed",
                    "baseline": {"output": minutes}, "revised": {"output": minutes}}]
        metrics = runner.score_existing(records, [case], manifest)
        self.assertEqual(metrics["gold_scoring_status"], "gold_not_scored_without_human_alignment")
        self.assertEqual(metrics["processing"]["completion_rate"], 1.0)
        self.assertEqual(metrics["structure"]["baseline"]["schema_valid_rate_among_completed"], 1.0)
        self.assertEqual(metrics["structure"]["revised"]["schema_valid_rate_among_planned"], 1.0)
        self.assertNotIn("rouge_l", json.dumps(metrics))
        self.assertNotIn("action_item_f1", json.dumps(metrics))
        with self.assertRaisesRegex(ValueError, "Unknown or duplicate"):
            runner.score_existing(records + records, [case], manifest)

    def test_ali_live_report_and_fixture_name_are_dataset_specific_without_calls(self):
        manifest, case = self.ali_fixture()
        (HERE / "results").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=HERE) as source_temp, \
             tempfile.TemporaryDirectory(dir=HERE / "results") as result_temp:
            manifest_path = Path(source_temp) / "ali.json"
            raw = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
            manifest_path.write_bytes(raw)
            args = argparse.Namespace(authorized_llm_calls=15, output_dir=Path(result_temp) / "ali-run",
                                      manifest=manifest_path, max_rounds=2, case_timeout=1,
                                      base_url="http://offline.invalid")
            minutes = self.valid_minutes()
            record = {"case_id": case["id"], "status": "completed",
                      "baseline": {"output": minutes}, "revised": {"output": minutes},
                      "telemetry": {"actual_llm_dispatch_count": 0}}
            with patch.object(runner, "authenticate_and_preflight",
                              return_value=({"id": 1, "token": "offline-test"}, {"agent_context_limit_chars": 100000})), \
                 patch.object(runner, "api_request", return_value={"id": 17}) as api, \
                 patch.object(runner, "management_request", return_value=record) as management:
                report_path = runner.run_live(args, manifest, [case], runner.sha256(raw),
                                              runner.make_plan([case], 2, 15))
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(report["run_id"].startswith("alimeeting4mug-"))
            self.assertEqual(report["dataset_name"], "AliMeeting4MUG")
            self.assertEqual(report["quality_metrics"]["gold_scoring_status"],
                             "gold_not_scored_without_human_alignment")
            self.assertNotIn("reference_metrics", report)
            self.assertIn("AliMeeting4MUG", api.call_args.args[2]["location"])
            self.assertNotIn("SECRET_GOLD_ACTION_SENTINEL", json.dumps(management.call_args.args[1], ensure_ascii=False))

    def test_full_official_dev_manifest_budget_single_pass(self):
        plan = runner.make_plan(self.full_cases, 2, 198)
        self.assertEqual(len(plan["cases"]), 22)
        # 单遍：生成 22×1=22，自检 22×2×3=132，重试余量 22×2=44（分块时代生成是 93）
        self.assertEqual(sum(row["generation_max_calls"] for row in plan["cases"]), 22)
        self.assertEqual(sum(row["agent_max_calls"] for row in plan["cases"]), 132)  # 22 场 × 2 轮 × 3 次
        self.assertEqual(plan["reserved_llm_calls"], 198)
        self.assertTrue(plan["within_authorized_budget"])
        self.assertEqual(plan["absolute_runner_cap"], runner.MAX_CALLS)
        # 注意：仓库里这份 dev 清单是**分块 12000 时构建的**，其
        # `estimated_total_model_calls_max_2_agent_rounds` 字段已随分块调整而过期。
        # 老清单是旧运行的证据，保留不动；这里改为断言计划自身的自洽性，
        # 并把"清单早于 6000 分块"这一事实显式固定下来。
        for case, row in zip(self.full_cases, plan["cases"]):
            self.assertEqual(row["reserved_calls"], row["generation_max_calls"] + row["agent_max_calls"] + 2)  # +2 重试余量

    def test_after_fix_test_manifest_single_pass_budget(self):
        # 单遍下 test26 全量 = 26×(1+6+2) = 234 次（分块时代 202 次曾超 200 硬上限，
        # 护栏按实测定为 300——含重试余量后仍在护栏内）。
        plan = runner.make_plan(self.fixed_cases, 2, 234)
        self.assertGreaterEqual(plan["absolute_runner_cap"], plan["reserved_llm_calls"])
        self.assertTrue(plan["within_authorized_budget"])
        plan = runner.make_plan(self.fixed_cases, 2, 254)
        self.assertEqual(len(self.fixed_cases), 26)
        self.assertEqual(self.fixed_manifest["agent_review_input_limit_chars"], 100000)
        self.assertEqual(plan["agent_context_limit_chars"], 100000)
        # 每场需要更多次分块调用。这是配置变更导致的**预期**变化，
        # 因此在这里显式更新，而不是放宽断言——预算锁的作用就是逼出这种声明。
        self.assertEqual(sum(row["generation_max_calls"] for row in plan["cases"]), 26)
        self.assertEqual(sum(row["agent_max_calls"] for row in plan["cases"]), 156)  # 26 场 × 2 轮 × 3 次
        self.assertEqual(plan["reserved_llm_calls"], 234)
        self.assertTrue(all(not row["agent_context_truncated_by_product"] for row in plan["cases"]))
        for case in self.fixed_cases:
            payload = runner.case_request(self.fixed_manifest, case, self.fixed_manifest_hash,
                                          token="offline-test", run_id="offline-test", meeting_id=1,
                                          rounds=2, case_limit=9, run_limit=159,
                                          results_subdir="offline-test", timeout_seconds=1)
            self.assertEqual(payload["agent_context_limit_chars"], 100000)
            self.assertNotIn("reference_overall", json.dumps(payload, ensure_ascii=False))
            self.assertNotIn(case["reference_overall"], json.dumps(payload, ensure_ascii=False))

    def test_manifest_rejects_mixed_review_caps(self):
        with tempfile.TemporaryDirectory(dir=HERE) as temp:
            path = Path(temp) / "mixed_cap.json"
            manifest = json.loads((HERE / "manifest_test_chunk6000.json").read_text(encoding="utf-8"))
            manifest["cases"][0]["agent_review_input_limit_chars"] = 20000
            path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "differs from manifest"):
                runner.load_inputs(path, [])

    def test_preflight_rejects_stale_product_cap_without_model_calls(self):
        args = argparse.Namespace(base_url="http://offline.invalid")
        with patch.object(runner, "credentials", return_value=("user", "password")), \
             patch.object(runner, "api_request", return_value={"token": "offline-test"}), \
             patch.object(runner, "management_request", return_value={"agent_context_limit_chars": 20000}) as management:
            with self.assertRaisesRegex(ValueError, "differs from the locked manifest"):
                runner.authenticate_and_preflight(args, 100000)
        self.assertEqual(management.call_args.args[1]["expected_agent_context_limit_chars"], 100000)
        with self.assertRaisesRegex(ValueError, "differs from the locked manifest"):
            manager.verify_agent_context_limit(100000, 20000)
        manager.verify_agent_context_limit(100000, 100000)

    def test_after_fix_default_is_dry_run_and_live_requires_new_allowance(self):
        manifest = str(HERE / "manifest_test_chunk6000.json")
        with patch.object(runner, "authenticate_and_preflight", side_effect=AssertionError("must not authenticate")):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(runner.main(["--manifest", manifest]), 0)
            self.assertEqual(json.loads(output.getvalue())["mode"], "dry-run")
            with redirect_stderr(io.StringIO()):
                self.assertEqual(runner.main(["--mode", "live", "--manifest", manifest]), 1)

    def test_full_dev_146_is_rejected_before_authentication(self):
        args = argparse.Namespace(authorized_llm_calls=146)
        plan = runner.make_plan(self.full_cases, 2, 146)
        with patch.object(runner, "authenticate_and_preflight", side_effect=AssertionError("must not authenticate")):
            with self.assertRaisesRegex(ValueError, "exceeds the new allowance"):
                runner.run_live(args, self.full_manifest, self.full_cases, self.full_manifest_hash, plan)

    def test_management_constructor_accepts_147_but_keeps_case_limit(self):
        with tempfile.TemporaryDirectory(dir=HERE) as temp:
            directory = Path(temp) / "test-run"
            directory.mkdir()
            report = {"run_id": "test-run", "active_case": "case", "plan": {
                "max_llm_calls": 147, "cases": [{"case_id": "case", "reserved_calls": 1}]}}
            (directory / "report.json").write_text(json.dumps(report))
            real_path = Path
            with patch.object(manager, "Path", side_effect=lambda value: real_path(temp) if value == "/evaluation/benchmarks/results" else real_path(value)):
                budget = manager.CallBudget({"run_id": "test-run", "case_id": "case", "max_llm_calls": 1,
                                             "run_max_llm_calls": 147, "results_subdir": "test-run"})
                budget.reserve("MINUTES_CHUNK")
                with self.assertRaises(manager.BudgetExceeded):
                    budget.reserve("AGENT_REVIEW")

    def test_live_without_new_allowance_is_rejected_before_authentication(self):
        args = argparse.Namespace(authorized_llm_calls=None)
        with patch.object(runner, "authenticate_and_preflight", side_effect=AssertionError("must not authenticate")):
            with self.assertRaisesRegex(ValueError, "previous 30-call allowance is not reused"):
                runner.run_live(args, self.manifest, self.cases, self.manifest_hash, {})

    def test_insufficient_allowance_is_rejected_before_authentication(self):
        args = argparse.Namespace(authorized_llm_calls=19)
        plan = runner.make_plan(self.cases, 2, 19)
        with patch.object(runner, "authenticate_and_preflight", side_effect=AssertionError("must not authenticate")):
            with self.assertRaisesRegex(ValueError, "exceeds the new allowance"):
                runner.run_live(args, self.manifest, self.cases, self.manifest_hash, plan)

    def test_public_hard_budget_is_persistent_and_does_not_refund_failure(self):
        with tempfile.TemporaryDirectory(dir=HERE) as temp:
            def budget():
                value = manager.CallBudget.__new__(manager.CallBudget)
                value.run_id, value.case_id = "offline", "case"
                value.run_limit, value.case_limit = 1, 1
                value.path, value.lock_path = Path(temp) / "ledger.json", Path(temp) / "ledger.lock"
                return value
            first = budget()
            call = first.reserve("MINUTES_CHUNK")
            first.finish(call, {"status": "FAILED_OR_CANCELLED"})
            with self.assertRaises(manager.BudgetExceeded):
                budget().reserve("AGENT_REVIEW")
            self.assertEqual(len(json.loads(first.path.read_text())["dispatches"]), 1)

    def test_scoring_cli_accepts_future_runner_record_shape_without_model_calls(self):
        with tempfile.TemporaryDirectory(dir=HERE) as temp:
            directory = Path(temp)
            case = self.cases[0]
            record = {"case_id": case["id"], "status": "completed", "synthetic": False,
                      "baseline": {"output": {"summary": case["reference_overall"]}},
                      "revised": {"output": {"summary": case["reference_overall"]}},
                      "telemetry": {"actual_llm_dispatch_count": 0},
                      "test_fixture_only": True}
            results = directory / "temporary_test_fixture.json"
            results.write_text(json.dumps({"records": [record]}, ensure_ascii=False))
            score_path = directory / "score.json"
            done = subprocess.run([sys.executable, str(HERE / "scoring.py"), "--manifest", str(HERE / "manifest.json"),
                                   "--results", str(results), "--output", str(score_path)],
                                  text=True, capture_output=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue(score_path.is_file())

    def test_recovery_merges_durable_result_without_touching_report_or_ledger(self):
        (HERE / "results").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=HERE / "results") as temp:
            directory = Path(temp)
            case = self.cases[0]
            source = {"manifest_sha256": self.manifest_hash, "transcript_sha256": case["transcript_sha256"]}
            output = {"summary": case["reference_overall"], "topics": [], "viewpoints": [], "decisions": [], "pending_items": [], "risks": []}
            record = {"case_id": case["id"], "status": "completed", "source": source,
                      "baseline": {"output": output, "raw_model_output": output},
                      "revised": {"output": output, "raw_model_output": output},
                      "model_calls": [{"dispatch_id": "one"}], "telemetry": {"actual_llm_dispatch_count": 1}}
            report = {"run_id": "offline-test", "manifest_sha256": self.manifest_hash,
                      "plan": {"max_llm_calls": 147, "cases": [{"case_id": case["id"]}]}, "records": []}
            ledger = {"run_id": "offline-test", "max_llm_calls": 147,
                      "dispatches": [{"dispatch_id": "one", "case_id": case["id"], "status": "RESPONSE_RECEIVED"}]}
            report_path, ledger_path = directory / "report.json", directory / "dispatch-ledger.json"
            report_path.write_text(json.dumps(report)); ledger_path.write_text(json.dumps(ledger))
            (directory / f"{case['id']}.raw-result.json").write_text(json.dumps(record))
            original_report, original_ledger = report_path.read_bytes(), ledger_path.read_bytes()
            with patch.object(runner, "management_request", side_effect=AssertionError("no model/DB invocation")):
                path = runner.recover_durable_results(report_path, self.cases, self.manifest_hash)
                merged = json.loads(path.read_text())
                runner.recover_durable_results(report_path, self.cases, self.manifest_hash)
            self.assertEqual(merged["status"], "recovered_completed")
            self.assertEqual(len(merged["records"]), 1)
            self.assertEqual(merged["recovery"]["model_calls_during_recovery"], 0)
            self.assertEqual(report_path.read_bytes(), original_report)
            self.assertEqual(ledger_path.read_bytes(), original_ledger)

    def test_recovery_does_not_retry_claimed_case_with_no_final_result(self):
        (HERE / "results").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=HERE / "results") as temp:
            directory = Path(temp); case = self.cases[0]
            report = {"run_id": "offline-test", "manifest_sha256": self.manifest_hash,
                      "plan": {"max_llm_calls": 147, "cases": [{"case_id": case["id"]}]}, "records": []}
            report_path = directory / "report.json"
            report_path.write_text(json.dumps(report))
            (directory / f"{case['id']}.claim").write_text("claimed")
            with patch.object(runner, "management_request", side_effect=AssertionError("no automatic retry")):
                path = runner.recover_durable_results(report_path, self.cases, self.manifest_hash)
            recovered = json.loads(path.read_text())
            self.assertEqual(recovered["status"], "recovered_partial")
            self.assertEqual(recovered["recovery"]["unresolved_started_case_ids"], [case["id"]])
            self.assertEqual(recovered["records"], [])

    def test_ali_recovery_keeps_gold_unscored_and_makes_no_dispatch(self):
        manifest, case = self.ali_fixture()
        (HERE / "results").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=HERE / "results") as temp:
            directory = Path(temp)
            manifest_hash = "a" * 64
            minutes = self.valid_minutes()
            record = {"case_id": case["id"], "status": "completed",
                      "source": {"manifest_sha256": manifest_hash,
                                 "transcript_sha256": case["transcript_sha256"]},
                      "baseline": {"output": minutes}, "revised": {"output": minutes},
                      "model_calls": [], "telemetry": {"actual_llm_dispatch_count": 0}}
            report = {"run_id": "ali-offline-test", "dataset_name": "AliMeeting4MUG",
                      "manifest_sha256": manifest_hash,
                      "plan": {"max_llm_calls": 5, "cases": [{"case_id": case["id"]}]},
                      "records": []}
            report_path = directory / "report.json"
            report_path.write_text(json.dumps(report))
            (directory / f"{case['id']}.raw-result.json").write_text(json.dumps(record))
            with patch.object(runner, "management_request", side_effect=AssertionError("no model calls")):
                recovered_path = runner.recover_durable_results(report_path, [case], manifest_hash, manifest)
            recovered = json.loads(recovered_path.read_text())
            self.assertEqual(recovered["quality_metrics"]["gold_scoring_status"],
                             "gold_not_scored_without_human_alignment")
            self.assertNotIn("reference_metrics", recovered)
            self.assertEqual(recovered["recovery"]["model_calls_during_recovery"], 0)


if __name__ == "__main__":
    unittest.main()


class ArmEnvTest(unittest.TestCase):
    """--arm-env：受控机制臂的环境注入与留痕脱敏。"""

    def test_validate_accepts_key_value_and_empty_value(self):
        runner.validate_arm_env(["SOME_FLAG=1", "ANOTHER=a,b", "EMPTY_OK="])

    def test_validate_rejects_malformed_pairs(self):
        for bad in ["NOT_A_PAIR", "1BAD=x", "=x", "HAS SPACE=1"]:
            with self.assertRaises(ValueError, msg=bad):
                runner.validate_arm_env([bad])

    def test_redaction_keeps_flags_but_hides_credentials(self):
        redacted = runner.redacted_arm_env(
            ["SOME_FLAG=keypoints", "SECRET_API_KEY=sk-secret",
             "AGENT_REVIEW_MODEL=qwen3.8-max"])
        self.assertEqual(redacted["SOME_FLAG"], "keypoints")
        self.assertEqual(redacted["AGENT_REVIEW_MODEL"], "qwen3.8-max")
        self.assertEqual(redacted["SECRET_API_KEY"], "<redacted>")
        self.assertNotIn("sk-secret", json.dumps(redacted))

    def test_management_request_injects_env_into_docker_exec(self):
        captured = {}

        class Completed:
            returncode = 0
            stdout = 'AIMEETING_PUBLIC_EVAL_RESULT={"ok": true}\n'
            stderr = ""

        def fake_run(command, **_kwargs):
            captured["command"] = command
            return Completed()

        args = argparse.Namespace(
            container_app_dir="/app", container="aimeeting-local-backend-1",
            arm_env=["SOME_FLAG=1", "OTHER=2"])
        with patch.object(runner.subprocess, "run", side_effect=fake_run):
            result = runner.management_request(args, {"action": "preflight"})
        command = captured["command"]
        self.assertEqual(result, {"ok": True})
        self.assertEqual(command.count("-e"), 2)
        self.assertIn("SOME_FLAG=1", command)
        self.assertIn("OTHER=2", command)
        # -e 必须在容器名之前，否则 docker 会把它当成命令的一部分
        self.assertLess(command.index("-e"), command.index("aimeeting-local-backend-1"))

    def test_main_rejects_malformed_arm_env_before_any_network(self):
        manifest = str(HERE / "manifest_test_chunk6000.json")
        with patch.object(runner, "authenticate_and_preflight",
                          side_effect=AssertionError("must not authenticate")):
            with redirect_stderr(io.StringIO()):
                rc = runner.main(["--manifest", manifest, "--arm-env", "BROKEN"])
        self.assertEqual(rc, 1)

class SchemaContractDriftTest(unittest.TestCase):
    """六字段条目契约的防漂移把守：产品改条目结构时这里必须先红。

    2026-10-05 教训：契约 v2 给条目加了 origin/evidence，bench_common 的独立
    声明没跟上，导致所有新运行的 structure.schema_valid 系统性全 False——
    看起来是产品质量崩了，实际是评测侧契约漂移。金样用钉住的 v2 run 对里的
    真实产物（冻结在仓库里），产品再改结构时本测试先红，逼出显式同步。
    """

    def _golden_outputs(self):
        from lib import corpora
        outputs = []
        for run_name in corpora.NEW_CONTRACT_TEST26_RUNS:
            for path in sorted((corpora.PUBLIC_RESULTS / run_name).glob("vcsum_*.json"))[:3]:
                if ".responses" in path.name:
                    continue
                record = json.loads(path.read_text(encoding="utf-8"))
                for stage in ("baseline", "revised"):
                    output = (record.get(stage) or {}).get("output")
                    if isinstance(output, dict):
                        outputs.append(output)
        return outputs

    def test_real_product_outputs_pass_the_independent_contract(self):
        from benchmarks.bench_common import schema_errors
        outputs = self._golden_outputs()
        self.assertTrue(outputs, "钉住的 v2 run 对里没有可用金样")
        for output in outputs:
            self.assertEqual(schema_errors(output), [])

    def test_drift_examples_are_caught(self):
        from benchmarks.bench_common import schema_errors
        good = {"summary": "s",
                "topics": [{"title": "t", "summary": "s", "origin": "stated",
                            "evidence": [{"quote": "q", "speaker": "发言人1"}]}],
                "viewpoints": [], "decisions": [], "risks": [],
                "pending_items": [{"content": "c", "origin": "stated", "evidence": [],
                                   "owner": {"text": "张三", "basis": "stated", "normalized": "张三",
                                             "anchor": "张三", "evidence": []},
                                   "deadline": {"text": "周五", "basis": "stated", "normalized": "周五",
                                                "anchor": "周五", "evidence": []}}]}
        self.assertEqual(schema_errors(good), [])
        for mutation in (
            lambda o: o["topics"][0].pop("origin"),
            lambda o: o["topics"][0].update({"origin": "invented"}),
            lambda o: o["pending_items"][0].update({"owner": "张三"}),
            lambda o: o["pending_items"][0]["owner"].pop("anchor"),
            lambda o: o["topics"][0].update({"evidence": [{"quote": "q"}]}),
        ):
            broken = json.loads(json.dumps(good, ensure_ascii=False))
            mutation(broken)
            self.assertTrue(schema_errors(broken), f"未检出漂移: {schema_errors(broken)}")


    def test_legacy_contract_validates_frozen_alimeeting_shape(self):
        """legacy 契约：AliMeeting4MUG 冻结产物形态必须过、新形态必须被分开。"""
        from benchmarks.bench_common import schema_errors
        legacy = {"summary": "s",
                  "topics": [{"title": "t", "summary": "s"}],
                  "viewpoints": [{"speaker": "甲", "viewpoint": "v"}],
                  "decisions": [{"content": "c", "basis": "b", "owner_suggestion": "甲",
                                 "deadline_suggestion": "周五"}],
                  "pending_items": [{"content": "c", "owner_suggestion": "", "deadline_suggestion": ""}],
                  "risks": [{"content": "r", "level": "LOW", "suggestion": "s"}]}
        self.assertEqual(schema_errors(legacy, contract="legacy"), [])
        # 同一产物按 v2 契约必须报错（两个契约不可混用）
        self.assertTrue(schema_errors(legacy, contract="v2"))
        # 未知契约显式报错，不静默放行
        self.assertTrue(schema_errors(legacy, contract="v9")[0].startswith("unknown contract"))


class ChunkBudgetConsistencyTest(unittest.TestCase):
    """分块上限必须只有一处声明。

    实测教训：产品把上限从 12000 调到 6000 后，评测侧有**四处**各自实现了这个量，
    导致预算预留与实际分块不符，运行到一半报
    "Actual product chunking exceeds reserved budget"，连续四轮复跑都失败。
    这个测试把"四处一致"变成一条可执行的断言。
    """

    def test_estimators_are_single_pass(self):
        import bench_common
        import vcsum_adapter  # type: ignore

        # 单遍长上下文：每场生成恰好一次调用，适配器与运行器读同一实现
        manifest = vcsum_adapter.build_test_manifest(review_cap_chars=100000)
        case = manifest["cases"][0]
        self.assertEqual(bench_common.estimated_generation_calls(
            {"segments": case["segments"], "transcript": case["transcript"]}), 1)

    def test_guardrail_matches_between_planner_and_executor(self):
        """护栏值在"算计划"与"守执行"两处必须相等。

        实测模式：同一常量在两处各写一份，改一处就静默失效——计划批准了、执行却拒绝，
        或者更糟：执行放行、计划没算到。这个断言把「两处一致」变成可执行检查。
        """
        self.assertEqual(runner.MAX_CALLS, manager.PUBLIC_RUN_MAX_CALLS)

    def test_guardrail_covers_the_largest_known_plan(self):
        """护栏必须容得下记录在案的最大合法计划。

        历史注：分块时代 test26 全量 26 场 = 202 次预留；单遍后同集缩到
        26×(1+3×2)=182 次，护栏上限 300 不变。"""
        fixed_manifest, fixed_cases, _ = runner.load_inputs(HERE / "manifest_test_chunk6000.json", [])
        plan = runner.make_plan(fixed_cases, 2, 202)
        self.assertEqual(len(fixed_cases), 26)
        self.assertLessEqual(plan["reserved_llm_calls"], runner.MAX_CALLS)
        self.assertLessEqual(plan["reserved_llm_calls"], manager.PUBLIC_RUN_MAX_CALLS)


    def test_failed_run_records_a_message_not_just_the_type(self):
        """失败的运行必须留下可诊断的信息。

        实测证据：两个运行目录留下 0 调用、0 完成的报告，里面只有一句 "RuntimeError"
        ——没有任何可用于判断成因的信息。只记类型等于把一个失败变成不可诊断的失败。
        """
        (HERE / "results").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=HERE / "results") as temp:
            output_dir = Path(temp) / "failure-record-test"
            args = argparse.Namespace(authorized_llm_calls=60, output_dir=output_dir,
                                      manifest=HERE / "manifest.json", max_rounds=2,
                                      case_timeout=1, base_url="http://offline.invalid")
            manifest, cases, manifest_hash = runner.load_inputs(HERE / "manifest.json", [])
            plan = runner.make_plan(cases, 2, 60)
            with patch.object(runner, "authenticate_and_preflight",
                              side_effect=RuntimeError("vcsum_23 did not complete; stopped without retry")):
                with self.assertRaises(RuntimeError):
                    runner.run_live(args, manifest, cases, manifest_hash, plan)
            report = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed_or_interrupted")
            self.assertEqual(report["failure_type"], "RuntimeError")
            self.assertIn("did not complete", report["failure_message"])

    def test_no_hardcoded_chunk_literal_remains(self):
        """守卫此前写成 `!= 12000` 的字面量，改分块就只能改工具本身。"""
        import re
        for name in ("manage_fixture.py",):
            text = (HERE / name).read_text(encoding="utf-8")
            self.assertIsNone(
                re.search(r"MAX_CHUNK_CHARS\s*!=\s*\d+", text),
                msg=f"{name} 仍有硬编码的分块上限比较",
            )
