import contextlib
import io
import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from prompt_telemetry import claude, cli, db, report
from test_prompt_telemetry import claude_entry, run_hook, wjsonl, Patcher


def run_cli(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cli.main(argv)
    return rc, out.getvalue()


class CliReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.mp = Patcher()
        self.addCleanup(self.mp.stop)
        self.mp.setenv("PROMPT_TELEMETRY_HOME", str(self.tmp_path / "home"))
        self.mp.setenv("HOME", str(self.tmp_path / "userhome"))
        os.environ.pop("COPILOT_OTEL_FILE_EXPORTER_PATH", None)

    def seed_claude_prompt(self):
        tp = self.tmp_path / "t.jsonl"
        base = {"session_id": "s1", "transcript_path": str(tp), "cwd": str(self.tmp_path)}
        run_hook(self.mp, ["claude"], {**base, "hook_event_name": "UserPromptSubmit",
                                       "prompt_id": "p1", "prompt": "hello"})
        wjsonl(tp, [claude_entry("m1", "r1", db.now_iso(), 10, ["t1"])])
        run_hook(self.mp, ["claude"], {**base, "hook_event_name": "Stop", "prompt_id": "p1"})
        return tp

    def test_init_prints_all_snippets_and_creates_db(self):
        rc, out = run_cli(["init"])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(db.db_path()))
        self.assertEqual(os.stat(db.db_path()).st_mode & 0o077, 0)
        for text in ("UserPromptSubmit", "COPILOT_OTEL_FILE_EXPORTER_PATH", "github.copilot.chat.otel.enabled",
                     "userPromptSubmitted", "prompt_telemetry"):
            self.assertIn(text, out)

    def test_init_flags_select_snippets(self):
        _, out = run_cli(["init", "--claude"])
        self.assertIn("UserPromptSubmit", out)
        self.assertNotIn("COPILOT_OTEL_FILE_EXPORTER_PATH", out)
        _, out = run_cli(["init", "--copilot"])
        self.assertNotIn("UserPromptSubmit", out)
        self.assertIn("COPILOT_OTEL_FILE_EXPORTER_PATH", out)

    def test_hook_dispatch_never_fails(self):
        with mock.patch.object(sys, "stdin", io.StringIO("not json")):
            self.assertEqual(cli.main(["hook", "claude"]), 0)

    def test_stats_for_every_grouping(self):
        run_cli(["init"])
        self.seed_claude_prompt()
        for by in ("day", "repo", "model", "client"):
            rc, out = run_cli(["stats", "--days", "1", "--by", by])
            self.assertEqual(rc, 0)
            self.assertIn(by, out.splitlines()[0])
            self.assertIn("USD is API-equivalent", out)
        self.assertIn("claude-code", run_cli(["stats", "--by", "client"])[1])

    def test_tail_lists_prompt(self):
        run_cli(["init"])
        self.seed_claude_prompt()
        _, out = run_cli(["tail", "-n", "5"])
        self.assertIn("claude-code", out)
        self.assertIn("hello", out)

    def test_tail_on_empty_db(self):
        run_cli(["init"])
        _, out = run_cli(["tail"])
        self.assertIn("started_utc", out)

    def test_import_copilot_missing_file_reports_zero(self):
        run_cli(["init"])
        _, out = run_cli(["import", "copilot", str(self.tmp_path / "none.jsonl")])
        self.assertIn("imported 0 new lines", out)

    def test_rescan_reparses_transcript_and_is_idempotent(self):
        run_cli(["init"])
        self.seed_claude_prompt()
        conn = db.connect()
        before = conn.execute("SELECT input_tokens, output_tokens, llm_call_count FROM prompts").fetchone()
        conn.close()
        _, out = run_cli(["rescan", "--all"])
        self.assertIn("rescan complete", out)
        conn = db.connect()
        after = conn.execute("SELECT input_tokens, output_tokens, llm_call_count FROM prompts").fetchone()
        conn.close()
        self.assertEqual(before, after)

    def test_rescan_skips_missing_source_files(self):
        run_cli(["init"])
        conn = db.connect()
        conn.execute(
            "INSERT INTO file_offsets(path, kind) VALUES (?, 'claude_transcript')",
            (str(self.tmp_path / "missing.jsonl"),),
        )
        report.rescan(conn)
        conn.close()

    def test_report_sync_logs_import_errors(self):
        conn = db.connect()
        with mock.patch.object(report.copilot, "import_all", side_effect=OSError("unreadable")), \
                mock.patch.object(report.db, "log_error") as log_error:
            report.sync(conn)
        conn.close()
        log_error.assert_called_once()
        self.assertIn("import before report", log_error.call_args.args[0])

    def test_doctor_without_db(self):
        _, out = run_cli(["doctor"])
        self.assertIn("WARN db missing", out)

    def test_doctor_reports_setup_state(self):
        run_cli(["init"])
        otel = db.otel_dir()
        os.makedirs(otel, exist_ok=True)
        Path(otel, "copilot-cli.jsonl").write_text("{}\n")
        claude_dir = self.tmp_path / "userhome" / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.json").write_text('{"command": "prompt_telemetry"}')
        db.log_error("boom")
        conn = db.connect()
        conn.execute("INSERT INTO sessions(session_id, client, started_at) VALUES ('s','copilot-cli',?)",
                     (db.now_iso(),))
        conn.execute("INSERT INTO prompts(prompt_id, session_id, client, started_at, status, cost_basis) "
                     "VALUES ('cph:1','s','copilot-cli',?, 'aborted', 'none')", (db.now_iso(),))
        conn.commit()
        conn.close()
        self.mp.setenv("COPILOT_OTEL_FILE_EXPORTER_PATH", f"{otel}/copilot-cli.jsonl")
        _, out = run_cli(["doctor"])
        self.assertIn("ok   Claude hooks", out)
        self.assertIn("copilot-cli.jsonl", out)
        self.assertIn("never matched an OTel trace", out)
        self.assertIn("last error:", out)

    def test_doctor_flags_unpriced_models_and_missing_otel(self):
        run_cli(["init"])
        conn = db.connect()
        conn.execute("INSERT INTO sessions(session_id, client, started_at) VALUES ('s','claude-code',?)",
                     (db.now_iso(),))
        conn.execute("INSERT INTO prompts(prompt_id, session_id, client, started_at, status, cost_basis) "
                     "VALUES ('p9','s','claude-code',?, 'done', 'none')", (db.now_iso(),))
        conn.execute("INSERT INTO llm_calls(call_key, prompt_id, client, model, ts) "
                     "VALUES ('c9','p9','claude-code','mystery-model',?)", (db.now_iso(),))
        conn.commit()
        conn.close()
        _, out = run_cli(["doctor"])
        self.assertIn("unpriced model 'mystery-model'", out)
        self.assertIn("no Copilot OTel files found", out)
        self.assertIn("WARN Claude hooks", out)

    def test_table_formats_empty_and_none_values(self):
        self.assertEqual(report._table(["a", "bb"], []), "a  bb")
        self.assertEqual(report._table(["a"], [[None], ["xyz"]]).splitlines()[1].strip(), "")

    def test_module_entrypoint_runs_main(self):
        with mock.patch.object(sys, "argv", ["prompt_telemetry", "init", "--claude"]), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
            runpy.run_module("prompt_telemetry", run_name="__main__")
        self.assertEqual(cm.exception.code, 0)

    def test_git_info_without_directory(self):
        self.assertEqual(claude.git_info(None), (None, None))


if __name__ == "__main__":
    unittest.main()
