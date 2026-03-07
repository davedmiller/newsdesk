# ABOUTME: Unit tests for newsdesk CLI — send, config, JSONL parsing, watch helpers.
# ABOUTME: TDD: tests are written before implementation.

import json
import os
import sys
import time

import pytest

# Add repo root to path so we can import newsdesk
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import newsdesk as nd


# ---------------------------------------------------------------------------
# Phase 1: send + config tests
# ---------------------------------------------------------------------------


class TestSendAppendsJsonl:
    """U1: send writes valid JSONL with all fields."""

    def test_appends_single_entry(self, tmp_path):
        queue = tmp_path / "queue.jsonl"
        nd.send_notification(
            str(queue), title="Hello", message="World", priority=0, project="test"
        )
        lines = queue.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["title"] == "Hello"
        assert entry["message"] == "World"
        assert entry["priority"] == 0
        assert entry["project"] == "test"
        assert "ts" in entry
        assert isinstance(entry["ts"], float)

    def test_appends_multiple_entries(self, tmp_path):
        queue = tmp_path / "queue.jsonl"
        nd.send_notification(str(queue), "A", "a", 0, "p1")
        nd.send_notification(str(queue), "B", "b", 1, "p2")
        lines = queue.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["title"] == "A"
        assert json.loads(lines[1])["title"] == "B"


class TestSendDefaultProject:
    """U2: omitted --project results in DEFAULT_PROJECT."""

    def test_default_project(self, tmp_path):
        queue = tmp_path / "queue.jsonl"
        nd.send_notification(str(queue), "T", "M", 0)
        entry = json.loads(queue.read_text().strip())
        assert entry["project"] == nd.DEFAULT_PROJECT


class TestSendRotation:
    """U3: queue > QUEUE_MAX_LINES gets rotated to QUEUE_ROTATE_TO."""

    def test_rotation_triggers(self, tmp_path):
        queue = tmp_path / "queue.jsonl"
        # Write QUEUE_MAX_LINES + 1 entries
        for i in range(nd.QUEUE_MAX_LINES + 1):
            nd.send_notification(str(queue), f"T{i}", "m", 0, "p")
        lines = queue.read_text().strip().splitlines()
        assert len(lines) == nd.QUEUE_ROTATE_TO
        # Should keep the most recent entries
        last = json.loads(lines[-1])
        assert last["title"] == f"T{nd.QUEUE_MAX_LINES}"

    def test_no_rotation_at_limit(self, tmp_path):
        queue = tmp_path / "queue.jsonl"
        for i in range(nd.QUEUE_MAX_LINES):
            nd.send_notification(str(queue), f"T{i}", "m", 0, "p")
        lines = queue.read_text().strip().splitlines()
        assert len(lines) == nd.QUEUE_MAX_LINES


class TestSendCreatesParentDirs:
    """U4: send creates missing directories for queue file."""

    def test_creates_dirs(self, tmp_path):
        queue = tmp_path / "deep" / "nested" / "queue.jsonl"
        nd.send_notification(str(queue), "T", "M", 0, "p")
        assert queue.exists()
        entry = json.loads(queue.read_text().strip())
        assert entry["title"] == "T"


class TestParseJsonlValid:
    """U5: valid JSONL lines are parsed correctly."""

    def test_parses_valid_lines(self, tmp_path):
        queue = tmp_path / "q.jsonl"
        records = [
            {"ts": 1.0, "title": "A", "message": "a", "priority": 0, "project": "p"},
            {"ts": 2.0, "title": "B", "message": "b", "priority": 1, "project": "q"},
        ]
        queue.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        parsed = nd.parse_jsonl(str(queue))
        assert len(parsed) == 2
        assert parsed[0]["title"] == "A"
        assert parsed[1]["title"] == "B"


class TestParseJsonlMalformed:
    """U6: malformed lines are skipped without error."""

    def test_skips_bad_lines(self, tmp_path):
        queue = tmp_path / "q.jsonl"
        good = json.dumps(
            {"ts": 1.0, "title": "A", "message": "a", "priority": 0, "project": "p"}
        )
        queue.write_text(f"{good}\nNOT JSON\n{good}\n")
        parsed = nd.parse_jsonl(str(queue))
        assert len(parsed) == 2


class TestParseJsonlEmptyFile:
    """U7: empty/missing file returns empty list."""

    def test_missing_file(self, tmp_path):
        parsed = nd.parse_jsonl(str(tmp_path / "nonexistent.jsonl"))
        assert parsed == []

    def test_empty_file(self, tmp_path):
        queue = tmp_path / "q.jsonl"
        queue.write_text("")
        parsed = nd.parse_jsonl(str(queue))
        assert parsed == []


class TestConfigDefaults:
    """U12: missing config file produces correct defaults."""

    def test_defaults(self, tmp_path):
        config = nd.load_config(str(tmp_path / "nonexistent.json"))
        assert "queue_file" in config
        assert "history_file" in config
        assert config["remote_machines"] == []
        assert config["pushover_enabled"] is True
        assert config["pushover_projects_default"] is True


class TestConfigLoads:
    """U13: valid config file is parsed correctly."""

    def test_loads_config(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_data = {
            "queue_file": "/tmp/test/queue.jsonl",
            "history_file": "/tmp/test/history.jsonl",
            "remote_machines": [
                {"name": "box", "host": "box.local", "queue_file": "/tmp/q.jsonl"}
            ],
            "pushover_enabled": False,
            "pushover_projects_default": False,
        }
        cfg_path.write_text(json.dumps(cfg_data))
        config = nd.load_config(str(cfg_path))
        assert config["queue_file"] == "/tmp/test/queue.jsonl"
        assert config["pushover_enabled"] is False
        assert len(config["remote_machines"]) == 1
        assert config["remote_machines"][0]["name"] == "box"

    def test_tilde_expansion(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_data = {
            "queue_file": "~/newsdesk/queue.jsonl",
            "history_file": "~/newsdesk/history.jsonl",
            "remote_machines": [],
            "pushover_enabled": True,
            "pushover_projects_default": True,
        }
        cfg_path.write_text(json.dumps(cfg_data))
        config = nd.load_config(str(cfg_path))
        assert not config["queue_file"].startswith("~")
        assert config["queue_file"].startswith("/")


class TestMultiMachineConfig:
    """U17: remote_machines list parsed correctly."""

    def test_multiple_machines(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        machines = [
            {"name": "mini", "host": "mini", "queue_file": "~/.local/share/newsdesk/queue.jsonl"},
            {"name": "server", "host": "server.local", "queue_file": "/data/newsdesk/queue.jsonl"},
        ]
        cfg_data = {
            "queue_file": "/tmp/q.jsonl",
            "history_file": "/tmp/h.jsonl",
            "remote_machines": machines,
            "pushover_enabled": True,
            "pushover_projects_default": True,
        }
        cfg_path.write_text(json.dumps(cfg_data))
        config = nd.load_config(str(cfg_path))
        assert len(config["remote_machines"]) == 2
        assert config["remote_machines"][0]["name"] == "mini"
        assert config["remote_machines"][1]["host"] == "server.local"
        # queue_file in remote machines should also be expanded
        assert not config["remote_machines"][0]["queue_file"].startswith("~")
