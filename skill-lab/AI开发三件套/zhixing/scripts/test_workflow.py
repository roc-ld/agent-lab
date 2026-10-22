"""Behavioral integration tests. Temporary repositories only; no network or live DB."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import yaml
import workflow_contract as c

SCRIPTS = Path(__file__).resolve().parent
ENTRY = SCRIPTS / "task_state.py"   # 资源包是拉平结构，没有版本分发入口


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ws = self.root / "doc"
        for p in ("src", "doc/verify", "doc/authority", "doc/tickets/T-001", "build"):
            (self.root / p).mkdir(parents=True)
        (self.root / "src/app.py").write_text("answer = 42\n")
        (self.root / "build/app.bin").write_text("build of answer=42\n")
        (self.ws / "verify/probe.py").write_text("import json\nprint(json.dumps({'code':200,'answer':42}))\n")
        cfg = {"schema_version": 2, "release": c.RELEASE, "project_root": "..",
               "source_paths": ["src"], "harness_paths": ["doc/verify"], "runtime": {"capacity": 1},
               "integration_after": [], "preflight": {stage: [
                   {"id": "positive", "run": f"{sys.executable} doc/verify/probe.py", "expect_exit": 0},
                   {"id": "negative", "run": "exit 2", "expect_exit": 2}] for stage in ("tools", "integration")}}
        c.write(self.ws / "workflow.json", cfg)
        self.yaml("requirements.yaml", {"requirements": [{"id": "REQ-001", "source": "request", "text": "Return answer", "domain": "SYS", "priority": "P0", "status": "pending"}]})
        self.yaml("authority/flows.yaml", {"kind": "flow", "records": [{"id": "FLOW:F-001", "title": "Answer", "status": "active", "body": "Return answer"}]})
        self.yaml("phase-plan.yaml", {"phases": [{"id": "D1", "order": 1, "goal": "Answer", "qa_scope": {lv: ["Check answer"] for lv in c.LEVELS}}], "final_rehearsal": {"scenarios": ["Read answer"]}})
        self.fm = {"ticket": "T-001", "track": "SYS", "phase": "D1", "req_refs": ["REQ-001"], "depends_on": [],
                   "touches": ["src/app.py"], "blueprint_refs": ["FLOW:F-001"], "accept": [{"name": "answer", "form": "API",
                   "run": f"{sys.executable} doc/verify/probe.py", "counterfeit": "Incorrect answer fails"}]}
        self.ticket()
        self.state_path = self.ws / "waves/state.json"
        self.manifest = self.ws / "waves/deployment.json"
        self.log = self.ws / "qa-result.log"
        self.log.write_text("actual fixture result\n")
        c.write(self.ws / "authority/_snapshot.json", {"hashes": {"FLOW:F-001": "test"}})
        self.run_tool("workflow.py", "preflight", "--stage", "tools")
        self.run_tool("workflow.py", "preflight", "--stage", "integration")
        self.run_tool("task_state.py", "init")
        self.stamp()

    def tearDown(self):
        self.temporary.cleanup()

    def yaml(self, path, value):
        p = self.ws / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False))

    def ticket(self):
        (self.ws / "tickets/T-001/prompt.md").write_text("---\n" + yaml.safe_dump(self.fm, sort_keys=False) + "---\nImplement answer.\n")

    def run_tool(self, name, *args, ok=True, entry=None):
        p = subprocess.run([sys.executable, str(entry or SCRIPTS / name), "--ws", str(self.ws), *args],
                           cwd=self.root, text=True, capture_output=True)
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        else:
            self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def state(self):
        return c.read(self.state_path)

    def stamp(self):
        self.run_tool("workflow.py", "stamp", "--environment", "fixture", "--artifact", str(self.root / "build/app.bin"),
                      "--probe", f"{sys.executable} doc/verify/probe.py", "--out", str(self.manifest))

    def done(self):
        p = self.run_tool("workflow.py", "accept", "--ticket", "T-001", "--environment", "fixture", "--artifact-manifest", str(self.manifest))
        path = json.loads(p.stdout)["accept_json"]
        self.run_tool("task_state.py", "set", "--ticket", "T-001", "--status", "done", "--note", "fixture verified", "--accept-json", path)
        return path

    def start(self):
        p = self.run_tool("task_state.py", "qa", "start", "--phase", "D1", "--environment", "fixture", "--artifact-manifest", str(self.manifest))
        return json.loads(p.stdout)["binding"]

    def audit(self, binding, issues=None, retests=None):
        d = {"binding": binding, "auditor": "independent", "levels": {
             lv: {"execution": "completed", "evidence": [c.asset(self.log)],
                  "checks": [{"id": f"{lv}-001", "status": "pass", "assertions": 1}]} for lv in c.LEVELS},
             "issues": issues or [], "retests": retests or []}
        path = self.ws / f"waves/qa/D1-r{binding['round']}-ALL.json"
        c.write(path, d)
        return path, d

    def finding(self, root):
        return {"root_cause": root, "severity": "S1", "level": "L2", "type": "arch", "ticket": "T-001", "title": root}

    def finish(self, verdict="PASS"):
        p = self.run_tool("task_state.py", "qa", "merge", "--phase", "D1")
        self.assertEqual(json.loads(p.stdout)["verdict"], verdict)
        if verdict in ("PASS", "FAIL"):
            self.run_tool("task_state.py", "set", "--phase", "D1", "--status", "qa_passed" if verdict == "PASS" else "qa_failed")

    def repair(self, identifier):
        (self.root / "src/app.py").write_text((self.root / "src/app.py").read_text() + f"# repaired {identifier}\n")
        self.run_tool("task_state.py", "issue", "repair", "--id", str(identifier), "--note", "fixed root and local check passed", "--evidence", str(self.log))
        self.stamp()

    def test_full_path_accept_and_qa(self):
        self.done(); binding = self.start(); self.audit(binding); self.finish()
        self.assertEqual(self.state()["phases"][0]["status"], "qa_passed")
        self.assertEqual(self.state()["phases"][0]["qa_rounds"], 1)

    def test_done_requires_current_complete_accept(self):
        self.run_tool("task_state.py", "set", "--ticket", "T-001", "--status", "done", "--note", "no evidence", ok=False)
        path = self.done()
        (self.root / "src/app.py").write_text("answer = 0\n")
        self.run_tool("task_state.py", "set", "--ticket", "T-001", "--status", "done", "--note", "stale", "--accept-json", path, ok=False)

    def test_tool_error_never_passes_or_counts_product_round(self):
        self.done(); binding = self.start(); path, data = self.audit(binding)
        data["levels"]["L2"]["execution"] = "error"
        data["levels"]["L2"]["checks"][0].update(status="error", assertions=0)
        data["issues"] = [{**self.finding("browser"), "type": "harness"}]
        c.write(path, data); self.finish("ERROR")
        self.run_tool("task_state.py", "set", "--phase", "D1", "--status", "qa_passed", ok=False)
        self.run_tool("task_state.py", "set", "--phase", "D1", "--status", "qa_failed", ok=False)
        self.assertEqual(self.state()["phases"][0]["qa_rounds"], 0)

    def test_missing_level_and_missing_scope_rejected(self):
        self.done(); binding = self.start(); path, data = self.audit(binding)
        data["levels"].pop("L2"); c.write(path, data)
        self.run_tool("task_state.py", "qa", "merge", "--phase", "D1", ok=False)
        path, data = self.audit(binding)
        data["levels"]["L2"]["checks"][0]["id"] = "different-scope"; c.write(path, data)
        self.run_tool("task_state.py", "qa", "merge", "--phase", "D1", ok=False)

    def test_wrong_binding_and_changed_evidence_rejected(self):
        self.done(); binding = self.start(); path, data = self.audit(binding)
        data["binding"] = dict(binding, phase="D2"); c.write(path, data)
        self.run_tool("task_state.py", "qa", "merge", "--phase", "D1", ok=False)
        self.audit(binding)
        self.run_tool("task_state.py", "qa", "merge", "--phase", "D1")
        self.log.write_text("altered after merge")
        self.run_tool("task_state.py", "set", "--phase", "D1", "--status", "qa_passed", ok=False)

    def test_unresolved_issue_cannot_disappear(self):
        self.done()
        self.run_tool("task_state.py", "issue", "add", "--phase", "D1", "--severity", "S0", "--level", "L2", "--root-cause", "lost", "--title", "critical")
        self.audit(self.start())
        self.run_tool("task_state.py", "qa", "merge", "--phase", "D1", ok=False)

    def test_different_new_roots_continue_and_retests_close_ledger(self):
        self.done(); self.audit(self.start(), [self.finding("root-a")]); self.finish("FAIL")
        self.repair(1)
        self.audit(self.start(), [self.finding("root-b")], [{"root_cause": "root-a", "status": "pass", "evidence": [c.asset(self.log)]}]); self.finish("FAIL")
        p = self.run_tool("task_state.py", "next")
        self.assertEqual(json.loads(p.stdout)["action"], "qa_rework")
        self.assertEqual(self.state()["open_issues"][0]["status"], "resolved")
        self.repair(2)
        self.audit(self.start(), retests=[{"root_cause": "root-b", "status": "pass", "evidence": [c.asset(self.log)]}]); self.finish()

    def test_same_root_two_failed_repairs_stop(self):
        self.done(); self.audit(self.start(), [self.finding("persistent")]); self.finish("FAIL")
        for _ in range(2):
            self.repair(1); self.audit(self.start(), [self.finding("persistent")]); self.finish("FAIL")
        p = self.run_tool("task_state.py", "next", ok=False)
        self.assertEqual(json.loads(p.stdout)["kind"], "same_root_repair_failed")

    def test_completed_round_cannot_be_aborted(self):
        self.done(); self.audit(self.start(), [self.finding("failure")])
        self.run_tool("task_state.py", "qa", "merge", "--phase", "D1")
        self.run_tool("task_state.py", "qa", "abort", "--phase", "D1", "--reason", "attempt to erase product failure", ok=False)

    def test_four_round_total_budget_and_rearm_cannot_reset_it(self):
        self.done()
        previous = None
        for index in range(4):
            retests = []
            if previous:
                self.repair(index)
                retests = [{"root_cause": previous, "status": "pass", "evidence": [c.asset(self.log)]}]
            root = f"new-root-{index}"
            self.audit(self.start(), [self.finding(root)], retests); self.finish("FAIL")
            previous = root
        p = self.run_tool("task_state.py", "next", ok=False)
        self.assertEqual(json.loads(p.stdout)["kind"], "qa_budget_exhausted")
        self.run_tool("task_state.py", "rearm", "--phase", "D1", "--by", "fixture", "--reason", "authorized retry cannot bypass cap", ok=False)

    def test_deduplicates_same_root_across_levels(self):
        self.done(); binding = self.start()
        findings = [{**self.finding("same-query-root"), "level": lv, "checks": [f"{lv}-001"]} for lv in ("L1", "L2")]
        path, data = self.audit(binding, findings)
        for lv in ("L1", "L2"):
            data["levels"][lv]["checks"][0]["status"] = "fail"
        c.write(path, data); self.finish("FAIL")
        self.assertEqual(len(self.state()["open_issues"]), 1)

    def test_no_silent_failed_check_or_zero_assertions(self):
        self.done(); binding = self.start(); path, data = self.audit(binding)
        data["levels"]["L2"]["checks"][0]["status"] = "fail"; c.write(path, data)
        self.run_tool("task_state.py", "qa", "merge", "--phase", "D1", ok=False)
        data["levels"]["L2"]["checks"][0].update(status="pass", assertions=0); c.write(path, data)
        self.run_tool("task_state.py", "qa", "merge", "--phase", "D1", ok=False)

    def test_post_merge_issue_and_changed_artifact_block_set(self):
        self.done(); self.audit(self.start()); self.run_tool("task_state.py", "qa", "merge", "--phase", "D1")
        self.run_tool("task_state.py", "issue", "add", "--phase", "D1", "--severity", "S0", "--level", "L2", "--root-cause", "late", "--title", "late issue")
        self.run_tool("task_state.py", "set", "--phase", "D1", "--status", "qa_passed", ok=False)
        (self.root / "build/app.bin").write_text("different binary")
        self.run_tool("task_state.py", "qa", "merge", "--phase", "D1", ok=False)

    def test_runtime_error_can_resume_same_round(self):
        self.done(); binding = self.start(); path, data = self.audit(binding)
        data["levels"]["L2"]["execution"] = "blocked"
        data["levels"]["L2"]["checks"][0].update(status="blocked", assertions=0)
        c.write(path, data); self.finish("ERROR")
        self.audit(binding); self.finish()
        self.assertEqual(self.state()["phases"][0]["qa_rounds"], 1)

    def test_human_risk_acceptance_stays_distinct_from_qa_pass(self):
        self.done(); self.audit(self.start(), [self.finding("known-risk")]); self.finish("FAIL")
        self.run_tool("task_state.py", "set", "--phase", "D1", "--status", "risk_accepted", "--by", "test-user",
                      "--note", "Explicit fixture authorization accepts this known residual; no claim that QA passed.")
        ph = self.state()["phases"][0]
        self.assertEqual(ph["status"], "risk_accepted")
        self.assertEqual(ph["human_override"]["qa_verdict"], "FAIL")

    def test_mutated_harness_invalidates_preflight(self):
        (self.ws / "verify/probe.py").write_text("print('different harness')")
        p = self.run_tool("task_state.py", "next", ok=False)
        self.assertEqual(json.loads(p.stdout)["action"], "preflight_required")

    def test_preflight_requires_both_positive_and_negative_cases(self):
        cfg = c.read(self.ws / "workflow.json")
        for cases in ([{"id": "only-negative", "run": "exit 1", "expect_exit": 1}],
                      [{"id": "only-positive", "run": "exit 0", "expect_exit": 0}]):
            cfg["preflight"]["tools"] = cases
            c.write(self.ws / "workflow.json", cfg)
            self.run_tool("workflow.py", "preflight", "--stage", "tools", ok=False)

    def test_blocking_issue_cannot_be_individually_hidden(self):
        self.done(); self.audit(self.start(), [self.finding("blocking-risk")]); self.finish("FAIL")
        self.run_tool("task_state.py", "issue", "accept-risk", "--id", "1", "--by", "test-user",
                      "--note", "Accept a residual without changing the original verdict", ok=False)
        self.assertEqual(self.state()["open_issues"][0]["status"], "open")

    def test_first_qa_can_record_an_existing_issue_without_claiming_a_repair(self):
        self.done()
        self.run_tool("task_state.py", "issue", "add", "--phase", "D1", "--severity", "S1", "--level", "L2",
                      "--root-cause", "known-before-qa", "--title", "Known before independent QA")
        self.audit(self.start(), [self.finding("known-before-qa")]); self.finish("FAIL")
        self.assertEqual(self.state()["phases"][0].get("repair_failures", {}), {})

    def test_sequential_repairs_of_multiple_roots_preserve_attempt_count(self):
        self.done(); self.audit(self.start(), [self.finding("one"), self.finding("two")]); self.finish("FAIL")
        self.repair(1); self.repair(2)
        self.audit(self.start(), [self.finding("one"), self.finding("two")]); self.finish("FAIL")
        self.assertEqual(self.state()["phases"][0]["repair_failures"], {"one": 1, "two": 1})

    def test_changed_spec_needs_explicit_sync(self):
        self.fm["accept"][0]["counterfeit"] = "changed contract"
        self.ticket()
        p = self.run_tool("task_state.py", "next", ok=False)
        self.assertEqual(json.loads(p.stdout)["action"], "blueprint_changed")

    def test_ui_contract_is_required(self):
        self.fm["touches"] = ["src/components/Button.vue"]
        errors = c.ticket_errors(self.fm)
        self.assertTrue(any("E2E" in x for x in errors))
        self.fm["ui"] = {"required": True}
        self.fm["accept"][0].update(form="E2E", entry="home", action="save", outcome="persisted", result="result.json")
        self.assertEqual(c.ticket_errors(self.fm), [])

    def test_root_level_ui_paths_require_e2e_in_real_lint(self):
        for directory in ("pages", "views", "components", "router", "routes"):
            with self.subTest(directory=directory):
                self.fm["touches"] = [directory + "/Invoice.vue"]
                self.assertTrue(any("E2E" in x for x in c.ticket_errors(self.fm)))
        self.ticket()
        p = subprocess.run([sys.executable, str(SCRIPTS / "dag_lint.py"), str(self.ws / "tickets")],
                           text=True, capture_output=True)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("E2E", p.stdout)

    def e2e_ticket(self, assertions=2, outcome="saved"):
        self.fm["touches"] = ["src/components/Button.vue"]
        self.fm["ui"] = {"required": True}
        result = self.ws / "waves/ui-result.json"
        payload = {"status": "pass", "assertions": assertions, "entry": "home", "action": "save",
                   "outcome": outcome, "evidence": [c.asset(self.log)]}
        adapter = self.ws / "verify/ui_adapter.py"
        adapter.write_text("from pathlib import Path\nPath('doc/waves/ui-result.json').write_text(" + repr(json.dumps(payload)) + ")\n")
        self.fm["accept"][0].update(form="E2E", entry="home", action="save", outcome="saved", result="doc/waves/ui-result.json",
                                    run=f"{sys.executable} doc/verify/ui_adapter.py")
        self.ticket()
        self.run_tool("task_state.py", "sync", "--reason", "fixture plans E2E contract before implementation")
        self.run_tool("workflow.py", "preflight", "--stage", "tools")
        self.stamp()
        return result

    def test_e2e_adapter_protocol_complete_result_is_accepted(self):
        self.e2e_ticket()
        path = self.done()
        self.assertEqual(c.read(path)["checks"][0]["form"], "E2E")

    def test_e2e_rejects_zero_assertions_and_wrong_scenario(self):
        result = self.e2e_ticket(assertions=0)
        args = ("accept", "--ticket", "T-001", "--environment", "fixture", "--artifact-manifest", str(self.manifest))
        self.run_tool("workflow.py", *args, ok=False)
        result.unlink()
        self.e2e_ticket(outcome="different outcome")
        self.run_tool("workflow.py", *args, ok=False)

    def test_e2e_rejects_stale_result(self):
        result = self.e2e_ticket()
        c.write(result, {"status": "pass"})
        self.run_tool("workflow.py", "accept", "--ticket", "T-001", "--environment", "fixture", "--artifact-manifest", str(self.manifest), ok=False)

    def test_e2e_rerun_preserves_previous_archived_evidence(self):
        result = self.e2e_ticket()
        first = c.read(self.done())
        result.unlink()
        self.done()
        for check in first["checks"]:
            c.validate_assets(check["evidence"])

    def test_runner_exposes_log_and_releases_lock_after_timeout(self):
        p = self.run_tool("workflow.py", "run", "--label", "log-check", "--", sys.executable, "-c", "print('observed')")
        log = json.loads(p.stdout)["evidence"]
        self.assertIn("observed", Path(log["path"]).read_text())
        self.run_tool("workflow.py", "run", "--label", "timeout-check", "--timeout", "1", "--", sys.executable, "-c", "import time;time.sleep(20)", ok=False)
        self.run_tool("workflow.py", "run", "--label", "after-timeout", "--", sys.executable, "-c", "print('released')")

    def test_wrapper_uses_v2_for_opted_in_state(self):
        p = self.run_tool("task_state.py", "next", entry=ENTRY)
        self.assertEqual(json.loads(p.stdout)["action"], "impl")

    def test_resource_lock_times_out_before_command(self):
        code = "import fcntl,time,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); time.sleep(2)"
        path = self.ws / "resource.lock"
        child = subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True)
        self.assertEqual(child.stdout.readline().strip(), "locked")
        try:
            with self.assertRaises(c.ContractError):
                with c.lock(path, timeout=0.1):
                    self.fail("concurrent resource entry")
        finally:
            child.terminate(); child.wait(); child.stdout.close()
        with c.lock(path, timeout=0.1):
            pass


if __name__ == "__main__":
    unittest.main()
