from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


audit = load_module("model_effort_router_history_audit", ROOT / "scripts" / "history_audit.py")


def line(event_type: str, payload: dict, timestamp: str = "2026-08-01T12:00:00Z") -> str:
    return json.dumps({"timestamp": timestamp, "type": event_type, "payload": payload}, ensure_ascii=False)


class HistoryAuditTest(unittest.TestCase):
    def make_history(self, root: Path) -> Path:
        source = root / "sessions" / "2026" / "08" / "sample.jsonl"
        source.parent.mkdir(parents=True)
        events = [
            line("session_meta", {"id": "private-session", "cwd": "synthetic-project-root"}),
            line("event_msg", {"type": "task_started", "turn_id": "private-turn"}),
            line("turn_context", {"turn_id": "private-turn", "model": "gpt-5.6-sol", "effort": "xhigh", "cwd": "synthetic-project-root"}),
            line("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "全量实现代码并运行测试 secret-prompt"}]}),
            line("response_item", {"type": "custom_tool_call", "name": "apply_patch", "arguments": "private-code"}),
            line("response_item", {"type": "custom_tool_call", "name": "exec_command", "arguments": "pytest"}),
            line("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10, "reasoning_output_tokens": 5, "total_tokens": 110}}}),
            line("event_msg", {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"input_tokens": 200, "cached_input_tokens": 50, "output_tokens": 30, "reasoning_output_tokens": 10, "total_tokens": 230},
                },
                "rate_limits": {"primary": {"used_percent": 75, "window_minutes": 10080}},
            }),
            line("event_msg", {"type": "task_complete"}),
            "{malformed",
        ]
        source.write_text("\n".join(events) + "\n", encoding="utf-8")
        return source

    def test_end_to_end_keeps_only_derived_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "codex"
            output = Path(temporary) / "private-output"
            source = self.make_history(root)
            original = (source.stat().st_size, source.stat().st_mtime_ns)
            result = audit.run_all(root, output, None)
            self.assertEqual(1, result["extract"]["unique_turn_count"])
            self.assertEqual(1, result["extract"]["malformed_line_count"])
            self.assertEqual(0, result["extract"]["source_files_changed_during_read"])
            self.assertEqual(0, result["extract"]["source_files_added_during_read"])
            self.assertEqual(0, result["extract"]["source_files_removed_during_read"])
            record = audit.read_jsonl(output / "turns.classified.jsonl")[0]
            self.assertEqual(200, record["tokens"]["input_tokens"])
            self.assertEqual(50, record["tokens"]["cached_input_tokens"])
            self.assertEqual(75, record["quota"]["used_percent"])
            self.assertEqual("sol", record["actual"]["model"])
            self.assertNotEqual("private-session", record["session_id"])
            self.assertNotEqual("private-turn", record["turn_id"])
            self.assertEqual(original, (source.stat().st_size, source.stat().st_mtime_ns))
            derived = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in output.rglob("*.json*"))
            for secret in ("secret-prompt", "private-code", "synthetic-project-root", "private-session", "private-turn"):
                self.assertNotIn(secret, derived)

    def test_incremental_cache_reuses_unchanged_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "codex"
            output = Path(temporary) / "private-output"
            self.make_history(root)
            audit.extract(root, output)
            second = audit.extract(root, output)
            self.assertEqual(1, second["cache_reused_source_count"])

    def test_mixed_route_is_marked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "codex"
            output = Path(temporary) / "private-output"
            source = self.make_history(root)
            text = source.read_text(encoding="utf-8")
            insertion = line("turn_context", {"turn_id": "private-turn", "model": "gpt-5.6-terra", "effort": "high"})
            source.write_text(text.replace(line("event_msg", {"type": "task_complete"}), insertion + "\n" + line("event_msg", {"type": "task_complete"})), encoding="utf-8")
            audit.extract(root, output)
            record = audit.read_jsonl(output / "turns.extracted.jsonl")[0]
            self.assertTrue(record["mixed_route"])
            self.assertEqual(2, record["route_count"])
            self.assertEqual("terra", record["actual"]["model"])

    def test_session_cumulative_usage_is_converted_to_turn_delta(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "codex"
            output = Path(temporary) / "private-output"
            source = self.make_history(root)
            second = [
                line("event_msg", {"type": "task_started", "turn_id": "second-turn"}),
                line("turn_context", {"turn_id": "second-turn", "model": "gpt-5.6-terra", "effort": "medium"}),
                line("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 260, "cached_input_tokens": 70, "output_tokens": 40, "reasoning_output_tokens": 12, "total_tokens": 300}}}),
                line("event_msg", {"type": "task_complete"}),
            ]
            with source.open("a", encoding="utf-8") as handle:
                handle.write("\n".join(second) + "\n")
            audit.extract(root, output)
            records = audit.read_jsonl(output / "turns.extracted.jsonl")
            self.assertEqual(2, len(records))
            later = next(item for item in records if item["actual"]["model"] == "terra")
            self.assertEqual(60, later["tokens"]["input_tokens"])
            self.assertEqual(20, later["tokens"]["cached_input_tokens"])
            self.assertEqual(10, later["tokens"]["output_tokens"])
            self.assertEqual("session_cumulative_delta", later["token_basis"])

    def test_prospective_quality_failure_requires_revert(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "analysis"
            telemetry = Path(temporary) / "telemetry"
            telemetry.mkdir()
            audit.prospective("init", output, telemetry)
            run = {
                "started_at": "2026-08-01T00:00:00Z",
                "project_id": "project",
                "actual": {"model": "terra", "effort": "medium"},
                "outcome": {"status": "accepted", "severe_defect": True, "scope_violation": False, "regression": False},
            }
            (telemetry / "runs.jsonl").write_text(json.dumps(run) + "\n", encoding="utf-8")
            status = audit.prospective("status", output, telemetry)
            self.assertTrue(status["automatic_revert_required"])
            self.assertFalse(status["review_ready"])


if __name__ == "__main__":
    unittest.main()
