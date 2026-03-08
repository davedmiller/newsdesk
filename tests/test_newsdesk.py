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
        for i in range(nd.QUEUE_MAX_LINES + 1):
            nd.send_notification(str(queue), f"T{i}", "m", 0, "p")
        lines = queue.read_text().strip().splitlines()
        assert len(lines) == nd.QUEUE_ROTATE_TO
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
        # Remote paths keep ~ intact — expanded on the remote machine, not locally
        assert config["remote_machines"][0]["queue_file"] == "~/.local/share/newsdesk/queue.jsonl"


# ---------------------------------------------------------------------------
# Phase 2: watch tests
# ---------------------------------------------------------------------------


class TestHistoryCap:
    """U8: history.jsonl capped at HISTORY_MAX_ENTRIES."""

    def test_caps_at_limit(self, tmp_path):
        history_path = str(tmp_path / "history.jsonl")
        entries = [
            {"ts": float(i), "title": f"T{i}", "message": "m", "priority": 0, "project": "p"}
            for i in range(nd.HISTORY_MAX_ENTRIES + 50)
        ]
        nd.append_to_history(history_path, entries)
        result = nd.parse_jsonl(history_path)
        assert len(result) == nd.HISTORY_MAX_ENTRIES
        assert result[-1]["title"] == f"T{nd.HISTORY_MAX_ENTRIES + 49}"

    def test_appends_within_limit(self, tmp_path):
        history_path = str(tmp_path / "history.jsonl")
        entries = [
            {"ts": 1.0, "title": "A", "message": "m", "priority": 0, "project": "p"},
            {"ts": 2.0, "title": "B", "message": "m", "priority": 0, "project": "p"},
        ]
        nd.append_to_history(history_path, entries)
        result = nd.parse_jsonl(history_path)
        assert len(result) == 2


class TestPriorityIconMapping:
    """U9: priority values map to correct icons."""

    def test_all_priorities(self):
        assert nd.priority_icon(-2) is None
        assert nd.priority_icon(-1) == ""
        assert nd.priority_icon(0) == "\u2705"
        assert nd.priority_icon(1) == "\U0001f514"
        assert nd.priority_icon(2) == "\U0001f514"

    def test_unknown_priority_defaults(self):
        assert nd.priority_icon(99) == "\u2705"


class TestPriorityBell:
    """U10: priority >= 1 triggers bell."""

    def test_bell_for_high(self):
        assert nd.should_bell(1) is True
        assert nd.should_bell(2) is True

    def test_no_bell_for_low(self):
        assert nd.should_bell(0) is False
        assert nd.should_bell(-1) is False
        assert nd.should_bell(-2) is False


class TestPrioritySilentSkipped:
    """U11: priority -2 entries are not displayed by default."""

    def test_silent_skipped(self):
        assert nd.should_display({"priority": -2}) is False

    def test_others_displayed(self):
        for p in (-1, 0, 1, 2):
            assert nd.should_display({"priority": p}) is True


class TestSilentSkipsPushover:
    """U21: priority -2 entries are not forwarded to Pushover."""

    def test_silent_not_forwarded(self):
        assert nd.should_forward_pushover({"priority": -2}) is False

    def test_others_forwarded(self):
        for p in (-1, 0, 1, 2):
            assert nd.should_forward_pushover({"priority": p}) is True


class TestSilentVisibleInHistory:
    """U22: show_silent flag controls whether -2 entries appear in display."""

    def test_hidden_by_default(self):
        assert nd.should_display({"priority": -2}, show_silent=False) is False

    def test_visible_when_toggled(self):
        assert nd.should_display({"priority": -2}, show_silent=True) is True

    def test_normal_always_visible(self):
        for p in (-1, 0, 1, 2):
            assert nd.should_display({"priority": p}, show_silent=False) is True
            assert nd.should_display({"priority": p}, show_silent=True) is True


class TestPushoverProjectToggle:
    """U14: ALL OFF disables all; ALL ON respects per-project."""

    def test_all_off_blocks_everything(self):
        state = nd.PushoverState(all_enabled=False, projects_default=True)
        state.ensure_project("pcm")
        assert state.should_forward("pcm") is False

    def test_all_on_respects_project(self):
        state = nd.PushoverState(all_enabled=True, projects_default=True)
        state.ensure_project("pcm")
        state.toggle_project("pcm")
        assert state.should_forward("pcm") is False
        assert state.should_forward("other") is True

    def test_all_on_project_on(self):
        state = nd.PushoverState(all_enabled=True, projects_default=True)
        state.ensure_project("pcm")
        assert state.should_forward("pcm") is True


class TestPushoverNewProjectDefault:
    """U15: new project inherits pushover_projects_default."""

    def test_default_on(self):
        state = nd.PushoverState(all_enabled=True, projects_default=True)
        assert state.should_forward("brand_new") is True

    def test_default_off(self):
        state = nd.PushoverState(all_enabled=True, projects_default=False)
        assert state.should_forward("brand_new") is False


class TestPushoverStatusLine:
    """U27: status_line is compact — no per-project details when all enabled."""

    def test_all_on_no_muted(self):
        state = nd.PushoverState(all_enabled=True, projects_default=True)
        state.ensure_project("pcm")
        state.ensure_project("newsdesk")
        assert state.status_line() == "✓"

    def test_all_off(self):
        state = nd.PushoverState(all_enabled=False, projects_default=True)
        assert state.status_line() == "✗"

    def test_all_on_some_muted(self):
        state = nd.PushoverState(all_enabled=True, projects_default=True)
        state.ensure_project("pcm")
        state.ensure_project("newsdesk")
        state.toggle_project("pcm")
        assert state.status_line() == "✓ (1 muted)"

    def test_all_on_multiple_muted(self):
        state = nd.PushoverState(all_enabled=True, projects_default=True)
        state.ensure_project("a")
        state.ensure_project("b")
        state.ensure_project("c")
        state.toggle_project("a")
        state.toggle_project("c")
        assert state.status_line() == "✓ (2 muted)"


class TestMachineNameInSend:
    """U23: send includes machine name in JSONL entry."""

    def test_machine_field_present(self, tmp_path):
        queue = tmp_path / "queue.jsonl"
        nd.send_notification(str(queue), "T", "M", 0, "p")
        entry = json.loads(queue.read_text().strip())
        assert "machine" in entry
        assert isinstance(entry["machine"], str)
        assert len(entry["machine"]) > 0

    def test_machine_field_matches_hostname(self, tmp_path):
        import socket
        queue = tmp_path / "queue.jsonl"
        nd.send_notification(str(queue), "T", "M", 0, "p")
        entry = json.loads(queue.read_text().strip())
        expected = socket.gethostname().split(".")[0].lower()
        assert entry["machine"] == expected


class TestDetectProjectFromGit:
    """U24: auto-detect project name from git repo."""

    def test_detects_repo_name(self, tmp_path):
        # Create a fake git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        result = nd.detect_project(str(tmp_path))
        assert result == tmp_path.name.lower()

    def test_fallback_when_no_git(self, tmp_path):
        result = nd.detect_project(str(tmp_path))
        assert result == nd.DEFAULT_PROJECT

    def test_nested_directory_finds_repo_root(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        nested = tmp_path / "src" / "deep"
        nested.mkdir(parents=True)
        result = nd.detect_project(str(nested))
        assert result == tmp_path.name.lower()


class TestProjectFieldInDisplay:
    """U16: display output includes [project] prefix."""

    def test_format_includes_project(self):
        entry = {"ts": 1709750535.0, "title": "Hello", "message": "World", "priority": 0, "project": "pcm"}
        line = nd.format_entry(entry)
        assert "pcm" in line
        assert "Hello" in line
        assert "World" in line


class TestMachineNameInDisplay:
    """U25: display output includes machine name."""

    def test_format_includes_machine(self):
        entry = {"ts": 1709750535.0, "title": "Hello", "message": "World",
                 "priority": 0, "project": "pcm", "machine": "mini"}
        line = nd.format_entry(entry)
        assert "mini" in line

    def test_format_without_machine_field(self):
        entry = {"ts": 1709750535.0, "title": "Hello", "message": "World",
                 "priority": 0, "project": "pcm"}
        line = nd.format_entry(entry)
        assert "Hello" in line


class TestFieldPadding:
    """U26: project and machine fields are padded/truncated to fixed width."""

    def test_short_project_padded(self):
        entry = {"ts": 1709750535.0, "title": "T", "message": "M",
                 "priority": 0, "project": "pcm", "machine": "mini"}
        line = nd.format_entry(entry)
        # project field should be padded to DISPLAY_FIELD_WIDTH
        assert f"{'pcm':<{nd.DISPLAY_FIELD_WIDTH}}" in line

    def test_long_project_truncated(self):
        long_name = "a" * (nd.DISPLAY_FIELD_WIDTH + 10)
        entry = {"ts": 1709750535.0, "title": "T", "message": "M",
                 "priority": 0, "project": long_name, "machine": "mini"}
        line = nd.format_entry(entry)
        expected = long_name[:nd.DISPLAY_FIELD_WIDTH - 1] + "\u2026"
        assert expected in line

    def test_short_machine_padded(self):
        entry = {"ts": 1709750535.0, "title": "T", "message": "M",
                 "priority": 0, "project": "pcm", "machine": "m4"}
        line = nd.format_entry(entry)
        assert f"{'m4':<{nd.DISPLAY_FIELD_WIDTH}}" in line

    def test_long_machine_truncated(self):
        long_name = "b" * (nd.DISPLAY_FIELD_WIDTH + 10)
        entry = {"ts": 1709750535.0, "title": "T", "message": "M",
                 "priority": 0, "project": "pcm", "machine": long_name}
        line = nd.format_entry(entry)
        expected = long_name[:nd.DISPLAY_FIELD_WIDTH - 1] + "\u2026"
        assert expected in line

    def test_exact_width_not_truncated(self):
        name = "a" * nd.DISPLAY_FIELD_WIDTH
        entry = {"ts": 1709750535.0, "title": "T", "message": "M",
                 "priority": 0, "project": name, "machine": "mini"}
        line = nd.format_entry(entry)
        assert name in line


class TestQueueRenameReadDelete:
    """U18: rename-read-delete pattern works."""

    def test_consumes_queue(self, tmp_path):
        queue = tmp_path / "queue.jsonl"
        entry = {"ts": 1.0, "title": "A", "message": "m", "priority": 0, "project": "p"}
        queue.write_text(json.dumps(entry) + "\n")
        entries = nd.consume_local_queue(str(queue))
        assert len(entries) == 1
        assert entries[0]["title"] == "A"
        assert not queue.exists()
        assert not (tmp_path / "queue.jsonl.processing").exists()

    def test_empty_queue(self, tmp_path):
        entries = nd.consume_local_queue(str(tmp_path / "queue.jsonl"))
        assert entries == []

    def test_new_sends_during_consume(self, tmp_path):
        queue = tmp_path / "queue.jsonl"
        entry = {"ts": 1.0, "title": "A", "message": "m", "priority": 0, "project": "p"}
        queue.write_text(json.dumps(entry) + "\n")
        entries = nd.consume_local_queue(str(queue))
        assert len(entries) == 1
        entry2 = {"ts": 2.0, "title": "B", "message": "m", "priority": 0, "project": "p"}
        queue.write_text(json.dumps(entry2) + "\n")
        entries2 = nd.consume_local_queue(str(queue))
        assert len(entries2) == 1
        assert entries2[0]["title"] == "B"


class TestStaleProcessingDeleted:
    """U19: .processing file older than STALE_PROCESSING_AGE deleted without reading."""

    def test_stale_deleted(self, tmp_path):
        queue = tmp_path / "queue.jsonl"
        processing = tmp_path / "queue.jsonl.processing"
        entry = {"ts": 1.0, "title": "Stale", "message": "m", "priority": 0, "project": "p"}
        processing.write_text(json.dumps(entry) + "\n")
        old_time = time.time() - (2 * 86400)
        os.utime(str(processing), (old_time, old_time))
        entries = nd.consume_local_queue(str(queue))
        assert entries == []
        assert not processing.exists()


class TestFreshProcessingRecovered:
    """U20: .processing file younger than STALE_PROCESSING_AGE read then deleted."""

    def test_fresh_recovered(self, tmp_path):
        queue = tmp_path / "queue.jsonl"
        processing = tmp_path / "queue.jsonl.processing"
        entry = {"ts": 1.0, "title": "Recovered", "message": "m", "priority": 0, "project": "p"}
        processing.write_text(json.dumps(entry) + "\n")
        entries = nd.consume_local_queue(str(queue))
        assert len(entries) == 1
        assert entries[0]["title"] == "Recovered"
        assert not processing.exists()

    def test_fresh_processing_plus_new_queue(self, tmp_path):
        queue = tmp_path / "queue.jsonl"
        processing = tmp_path / "queue.jsonl.processing"
        old_entry = {"ts": 1.0, "title": "Old", "message": "m", "priority": 0, "project": "p"}
        processing.write_text(json.dumps(old_entry) + "\n")
        new_entry = {"ts": 2.0, "title": "New", "message": "m", "priority": 0, "project": "p"}
        queue.write_text(json.dumps(new_entry) + "\n")
        entries = nd.consume_local_queue(str(queue))
        assert len(entries) == 2
        titles = [e["title"] for e in entries]
        assert "Old" in titles
        assert "New" in titles
        assert not processing.exists()
        assert not queue.exists()
