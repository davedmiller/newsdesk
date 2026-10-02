# ABOUTME: Unit tests for newsdesk CLI — send, config, JSONL parsing, Pushover forwarding.
# ABOUTME: TDD: tests are written before implementation.

import argparse
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
        assert config["pushover_min_priority"] == -1


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
        }
        cfg_path.write_text(json.dumps(cfg_data))
        config = nd.load_config(str(cfg_path))
        assert config["queue_file"] == "/tmp/test/queue.jsonl"
        assert len(config["remote_machines"]) == 1
        assert config["remote_machines"][0]["name"] == "box"

    def test_tilde_expansion(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_data = {
            "queue_file": "~/newsdesk/queue.jsonl",
            "history_file": "~/newsdesk/history.jsonl",
            "remote_machines": [],
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
        }
        cfg_path.write_text(json.dumps(cfg_data))
        config = nd.load_config(str(cfg_path))
        assert len(config["remote_machines"]) == 2
        assert config["remote_machines"][0]["name"] == "mini"
        assert config["remote_machines"][1]["host"] == "server.local"
        # Remote paths keep ~ intact — expanded on the remote machine, not locally
        assert config["remote_machines"][0]["queue_file"] == "~/.local/share/newsdesk/queue.jsonl"


# ---------------------------------------------------------------------------
# Phase 2: forwarding tests
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


class TestSilentSkipsPushover:
    """U21: priority -2 entries are not forwarded to Pushover."""

    def test_silent_not_forwarded(self):
        assert nd.should_forward_pushover({"priority": -2}) is False

    def test_others_forwarded(self):
        for p in (-1, 0, 1, 2):
            assert nd.should_forward_pushover({"priority": p}) is True


class TestPushoverMinPriority:
    """U: pushover_min_priority gates forwarding; priority -2 is always silent."""

    def test_default_forwards_all_nonsilent(self):
        # default threshold (-1) preserves prior behaviour
        for p in (-1, 0, 1, 2):
            assert nd.should_forward_pushover({"priority": p}, -1) is True

    def test_threshold_blocks_below(self):
        assert nd.should_forward_pushover({"priority": 0}, 1) is False
        assert nd.should_forward_pushover({"priority": -1}, 1) is False
        assert nd.should_forward_pushover({"priority": 1}, 1) is True
        assert nd.should_forward_pushover({"priority": 2}, 1) is True

    def test_silent_always_blocked_even_below_threshold(self):
        # -2 never forwards regardless of how low the threshold is set
        assert nd.should_forward_pushover({"priority": -2}, -2) is False

    def test_config_default_threshold(self):
        assert nd.DEFAULT_CONFIG["pushover_min_priority"] == -1


class TestPushoverStatusLabel:
    """U: pushover_status_label summarizes forwarding state for the relay state and page header."""

    def test_suppressed(self):
        assert nd.pushover_status_label(True, "a", "b", 1) == "off (--no-pushover)"

    def test_no_tokens(self):
        assert nd.pushover_status_label(False, None, None, 1) == "no keychain tokens"
        assert nd.pushover_status_label(False, "a", None, 1) == "no keychain tokens"

    def test_threshold_shown(self):
        assert nd.pushover_status_label(False, "a", "b", 1) == "≥ 1"
        assert nd.pushover_status_label(False, "a", "b", -1) == "≥ -1"


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


# ---------------------------------------------------------------------------
# Supplementary URL passthrough (Pushover url / url_title)
# ---------------------------------------------------------------------------


class TestSendUrlPassthrough:
    """U: send_notification persists url/url_title only when provided."""

    def test_url_fields_included_when_provided(self, tmp_path):
        queue = tmp_path / "queue.jsonl"
        nd.send_notification(
            str(queue), "T", "M", 1, "p",
            url="http://100.70.51.21:5555/", url_title="Open status page",
        )
        entry = json.loads(queue.read_text().strip())
        assert entry["url"] == "http://100.70.51.21:5555/"
        assert entry["url_title"] == "Open status page"

    def test_url_fields_omitted_when_not_provided(self, tmp_path):
        queue = tmp_path / "queue.jsonl"
        nd.send_notification(str(queue), "T", "M", 0, "p")
        entry = json.loads(queue.read_text().strip())
        assert "url" not in entry
        assert "url_title" not in entry

    def test_cmd_send_passes_url_through_to_queue(self, tmp_path, monkeypatch):
        # Covers the argparse-args -> cmd_send -> send_notification seam.
        queue = tmp_path / "queue.jsonl"
        monkeypatch.setattr(nd, "load_config", lambda path: {"queue_file": str(queue)})
        args = argparse.Namespace(
            title="T", message="M", priority=1, project="t",
            url="http://100.70.51.21:5555/", url_title="Open status page",
        )
        assert nd.cmd_send(args) == 0
        entry = json.loads(queue.read_text().strip())
        assert entry["url"] == "http://100.70.51.21:5555/"
        assert entry["url_title"] == "Open status page"


class TestForwardToPushoverUrl:
    """U: forward_to_pushover adds url/url_title form-strings only when present."""

    @staticmethod
    def _capture_curl(monkeypatch):
        captured = {}

        def fake_run(cmd, *a, **k):
            captured["cmd"] = cmd

            class _Result:
                returncode = 0

            return _Result()

        monkeypatch.setattr(nd.subprocess, "run", fake_run)
        return captured

    def test_url_included_when_present(self, monkeypatch):
        captured = self._capture_curl(monkeypatch)
        entry = {
            "title": "T", "message": "M", "priority": 1,
            "url": "http://100.70.51.21:5555/", "url_title": "Open status page",
        }
        nd.forward_to_pushover(entry, "apptok", "userkey")
        cmd = captured["cmd"]
        assert "url=http://100.70.51.21:5555/" in cmd
        assert "url_title=Open status page" in cmd
        # the API endpoint stays last
        assert cmd[-1] == "https://api.pushover.net/1/messages.json"

    def test_url_omitted_when_absent(self, monkeypatch):
        captured = self._capture_curl(monkeypatch)
        entry = {"title": "T", "message": "M", "priority": 0}
        nd.forward_to_pushover(entry, "apptok", "userkey")
        cmd = captured["cmd"]
        assert not any(str(x).startswith("url=") for x in cmd)
        assert not any(str(x).startswith("url_title=") for x in cmd)

    def test_url_title_omitted_without_url(self, monkeypatch):
        captured = self._capture_curl(monkeypatch)
        entry = {"title": "T", "message": "M", "priority": 0, "url_title": "Orphan"}
        nd.forward_to_pushover(entry, "apptok", "userkey")
        cmd = captured["cmd"]
        assert not any(str(x).startswith("url_title=") for x in cmd)


class TestForwardToPushoverEmergency:
    """U: priority-2 (Pushover emergency) sends retry/expire; lower priorities don't.

    Pushover REQUIRES retry+expire for priority 2 or rejects the whole message.
    """

    def test_emergency_params_for_priority_2(self, monkeypatch):
        captured = TestForwardToPushoverUrl._capture_curl(monkeypatch)
        nd.forward_to_pushover({"title": "T", "message": "M", "priority": 2}, "app", "usr")
        cmd = captured["cmd"]
        assert "priority=2" in cmd
        assert f"retry={nd.PUSHOVER_EMERGENCY_RETRY_S}" in cmd
        assert f"expire={nd.PUSHOVER_EMERGENCY_EXPIRE_S}" in cmd

    def test_no_emergency_params_below_priority_2(self, monkeypatch):
        captured = TestForwardToPushoverUrl._capture_curl(monkeypatch)
        nd.forward_to_pushover({"title": "T", "message": "M", "priority": 1}, "app", "usr")
        cmd = captured["cmd"]
        assert not any(str(x).startswith("retry=") for x in cmd)
        assert not any(str(x).startswith("expire=") for x in cmd)

    def test_pushover_emergency_constants_valid(self):
        # Pushover constraints: retry >= 30s, expire <= 10800s
        assert nd.PUSHOVER_EMERGENCY_RETRY_S >= 30
        assert nd.PUSHOVER_EMERGENCY_EXPIRE_S <= 10800
        assert nd.PUSHOVER_EMERGENCY_EXPIRE_S >= nd.PUSHOVER_EMERGENCY_RETRY_S
