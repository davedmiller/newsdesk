# ABOUTME: Unit tests for the hub side of newsdesk — relay cycle, push, remote pull, web feed.
# ABOUTME: Remote shell commands run for real under sh against tmp_path; only ssh and curl are faked.

import argparse
import fcntl
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import newsdesk as nd


def entry(title, priority=0, id=None, ts=1.0):
    e = {"ts": ts, "title": title, "message": "m", "priority": priority,
         "project": "p", "machine": "box"}
    if id is not None:
        e["id"] = id
    return e


def write_jsonl(path, entries):
    with open(path, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def titles(path):
    return [e["title"] for e in nd.parse_jsonl(str(path))]


@pytest.fixture
def config(tmp_path):
    return {
        "queue_file": str(tmp_path / "queue.jsonl"),
        "history_file": str(tmp_path / "history.jsonl"),
        "remote_machines": [],
        "pushover_min_priority": 1,
    }


@pytest.fixture
def keychain(monkeypatch):
    """Fake Keychain: a dict of service -> value that tests can mutate."""
    store = {"newsdesk-app-token": "app", "newsdesk-user-key": "usr"}
    monkeypatch.setattr(nd, "read_keychain_token",
                        lambda service, account="pushover": store.get(service))
    return store


@pytest.fixture
def forwards(monkeypatch):
    """Fake Pushover: records forwarded titles; `ok` controls the outcome."""
    box = {"calls": [], "ok": True}

    def fake_forward(e, app_token, user_key):
        box["calls"].append(e["title"])
        if box["ok"] == "raise":
            raise RuntimeError("boom")
        return box["ok"]

    monkeypatch.setattr(nd, "forward_to_pushover", fake_forward)
    return box


@pytest.fixture
def pings(monkeypatch):
    """Fake detached spawns: records the argv of every Popen."""
    calls = []

    class FakePopen:
        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))

    monkeypatch.setattr(nd.subprocess, "Popen", FakePopen)
    return calls


# ---------------------------------------------------------------------------
# T1, T12, T14 — the relay cycle
# ---------------------------------------------------------------------------
class TestRelayCycle:
    """T1: one cycle moves the local queue and spool files into history and forwards."""

    def test_consumes_queue_and_spool_into_history(self, config, keychain, forwards):
        write_jsonl(config["queue_file"], [entry("local", id="a")])
        write_jsonl(config["queue_file"] + ".in.b1", [entry("pushed", id="b")])
        rt = nd.new_relay(config, now=1000.0)
        summary = nd.relay_cycle(rt, 1000.0)
        assert sorted(titles(config["history_file"])) == ["local", "pushed"]
        assert summary["consumed"] == 2
        assert not os.path.exists(config["queue_file"] + ".processing")
        assert not os.path.exists(config["queue_file"] + ".in.b1")

    def test_forwards_only_at_or_above_threshold_and_never_silent(self, config, keychain, forwards):
        write_jsonl(config["queue_file"], [
            entry("low", 0, "a"), entry("high", 1, "b"),
            entry("urgent", 2, "c"), entry("silent", -2, "d"),
        ])
        rt = nd.new_relay(config, now=1000.0)
        summary = nd.relay_cycle(rt, 1000.0)
        assert forwards["calls"] == ["high", "urgent"]
        assert summary["forwarded"] == 2

    def test_no_pushover_forwards_nothing(self, config, keychain, forwards):
        write_jsonl(config["queue_file"], [entry("urgent", 2, "a")])
        rt = nd.new_relay(config, no_pushover=True, now=1000.0)
        nd.relay_cycle(rt, 1000.0)
        assert forwards["calls"] == []
        assert titles(config["history_file"]) == ["urgent"]

    def test_claim_is_committed_only_after_history_write(self, config, keychain, forwards, monkeypatch):
        write_jsonl(config["queue_file"], [entry("kept", id="a")])
        write_jsonl(config["queue_file"] + ".in.b1", [entry("kept2", id="b")])
        rt = nd.new_relay(config, now=1000.0)

        def failing_append(path, new_entries):
            raise OSError("disk full")

        monkeypatch.setattr(nd, "append_to_history", failing_append)
        with pytest.raises(OSError):
            nd.relay_cycle(rt, 1000.0)
        # nothing was deleted, and nothing was marked seen
        assert os.path.exists(config["queue_file"] + ".processing")
        assert os.path.exists(config["queue_file"] + ".in.b1")
        monkeypatch.undo()
        monkeypatch.setattr(nd, "forward_to_pushover", lambda *a: True)
        nd.relay_cycle(rt, 1002.0)
        assert sorted(titles(config["history_file"])) == ["kept", "kept2"]


class TestSpoolOrder:
    """Batches pushed within one cycle keep the order they arrived in."""

    def test_spool_files_are_claimed_oldest_first_not_by_name(self, config, keychain, forwards):
        # names sort z, m, a — the reverse of arrival
        for age, name, title in ((30, "z", "first"), (20, "m", "second"), (10, "a", "third")):
            path = config["queue_file"] + ".in." + name
            write_jsonl(path, [entry(title, id=title)])
            stamp = time.time() - age
            os.utime(path, (stamp, stamp))
        rt = nd.new_relay(config, now=1000.0)
        nd.relay_cycle(rt, 1000.0)
        assert titles(config["history_file"]) == ["first", "second", "third"]


class TestRelayDedupe:
    """T12: an id is accepted once, however many times it is delivered."""

    def test_id_already_in_history_is_dropped(self, config, keychain, forwards):
        write_jsonl(config["history_file"], [entry("old", 2, "a")])
        write_jsonl(config["queue_file"], [entry("old", 2, "a"), entry("new", 2, "b")])
        rt = nd.new_relay(config, now=1000.0)
        summary = nd.relay_cycle(rt, 1000.0)
        assert titles(config["history_file"]) == ["old", "new"]
        assert forwards["calls"] == ["new"]
        assert summary["dupes"] == 1

    def test_same_batch_in_two_spool_files_in_one_cycle(self, config, keychain, forwards):
        batch = [entry("one", 2, "a"), entry("two", 2, "b")]
        write_jsonl(config["queue_file"] + ".in.x1", batch)
        write_jsonl(config["queue_file"] + ".in.x2", batch)
        rt = nd.new_relay(config, now=1000.0)
        nd.relay_cycle(rt, 1000.0)
        assert sorted(titles(config["history_file"])) == ["one", "two"]
        assert sorted(forwards["calls"]) == ["one", "two"]

    def test_truncated_spool_then_full_resend(self, config, keychain, forwards):
        full = "".join(json.dumps(e) + "\n" for e in [entry("one", id="a"), entry("two", id="b")])
        with open(config["queue_file"] + ".in.cut", "w") as f:
            f.write(full[:len(full) - 20])  # second line torn
        rt = nd.new_relay(config, now=1000.0)
        nd.relay_cycle(rt, 1000.0)
        assert titles(config["history_file"]) == ["one"]
        with open(config["queue_file"] + ".in.full", "w") as f:
            f.write(full)
        nd.relay_cycle(rt, 1002.0)
        assert titles(config["history_file"]) == ["one", "two"]

    def test_entry_without_id_passes_through(self, config, keychain, forwards):
        write_jsonl(config["queue_file"], [entry("legacy"), entry("legacy")])
        rt = nd.new_relay(config, now=1000.0)
        nd.relay_cycle(rt, 1000.0)
        assert titles(config["history_file"]) == ["legacy", "legacy"]


class TestRelaySurvivesBadEntries:
    """T14: one bad entry or one failing forward does not stop the cycle."""

    def test_forward_that_raises(self, config, keychain, forwards):
        forwards["ok"] = "raise"
        write_jsonl(config["queue_file"], [entry("a", 2, "a"), entry("b", 2, "b")])
        rt = nd.new_relay(config, now=1000.0)
        summary = nd.relay_cycle(rt, 1000.0)
        assert forwards["calls"] == ["a", "b"]
        assert summary["forward_failed"] == 2
        assert titles(config["history_file"]) == ["a", "b"]

    def test_non_integer_priority_and_non_object_line(self, config, keychain, forwards):
        with open(config["queue_file"], "w") as f:
            f.write(json.dumps({"title": "odd", "message": "m", "priority": "high", "id": "x"}) + "\n")
            f.write("[1, 2, 3]\n")
            f.write(json.dumps(entry("good", 2, "g")) + "\n")
        rt = nd.new_relay(config, now=1000.0)
        nd.relay_cycle(rt, 1000.0)
        assert forwards["calls"] == ["good"]
        assert "good" in titles(config["history_file"])


# ---------------------------------------------------------------------------
# T9 — forward retry
# ---------------------------------------------------------------------------
class TestForwardRetry:
    """T9: a failed forward is retried on a schedule, then given up on."""

    def test_retry_after_interval_then_success(self, config, keychain, forwards):
        forwards["ok"] = False
        write_jsonl(config["queue_file"], [entry("alarm", 2, "a")])
        rt = nd.new_relay(config, now=1000.0)
        summary = nd.relay_cycle(rt, 1000.0)
        assert summary["forward_failed"] == 1 and summary["retrying"] == 1
        # too soon: no new attempt
        nd.relay_cycle(rt, 1000.0 + nd.FORWARD_RETRY_INTERVAL_S - 1)
        assert forwards["calls"] == ["alarm"]
        forwards["ok"] = True
        summary = nd.relay_cycle(rt, 1000.0 + nd.FORWARD_RETRY_INTERVAL_S)
        assert forwards["calls"] == ["alarm", "alarm"]
        assert summary["forwarded"] == 1 and summary["retrying"] == 0
        # and it stops
        nd.relay_cycle(rt, 1000.0 + 5 * nd.FORWARD_RETRY_INTERVAL_S)
        assert forwards["calls"] == ["alarm", "alarm"]

    def test_expires(self, config, keychain, forwards):
        forwards["ok"] = False
        write_jsonl(config["queue_file"], [entry("alarm", 2, "a")])
        rt = nd.new_relay(config, now=1000.0)
        nd.relay_cycle(rt, 1000.0)
        summary = nd.relay_cycle(rt, 1000.0 + nd.FORWARD_RETRY_EXPIRE_S + 1)
        assert summary["expired"] == 1 and summary["retrying"] == 0
        assert nd.build_state(rt)["retrying"] == 0


# ---------------------------------------------------------------------------
# T10 — heartbeat
# ---------------------------------------------------------------------------
class TestHeartbeat:
    """T10: state file, Healthchecks ping, token re-read."""

    def test_no_ping_without_url(self, config, keychain, forwards, pings):
        rt = nd.new_relay(config, now=1000.0)
        nd.relay_cycle(rt, 1000.0 + nd.HEARTBEAT_INTERVAL_S)
        assert pings == []
        assert nd.build_state(rt)["healthchecks"] is False

    def test_first_ping_waits_for_uptime_then_is_detached(self, config, keychain, forwards, pings):
        keychain["newsdesk-hc-url"] = "https://hc.example/abc"
        rt = nd.new_relay(config, now=1000.0)
        nd.relay_cycle(rt, 1000.0)
        nd.relay_cycle(rt, 1000.0 + nd.HEARTBEAT_INTERVAL_S - 1)
        assert pings == []
        nd.relay_cycle(rt, 1000.0 + nd.HEARTBEAT_INTERVAL_S)
        assert len(pings) == 1
        argv, kwargs = pings[0]
        assert argv[0] == "curl" and argv[-1] == "https://hc.example/abc"
        assert kwargs.get("start_new_session") is True
        # not again until another interval has passed
        nd.relay_cycle(rt, 1000.0 + nd.HEARTBEAT_INTERVAL_S + 2)
        assert len(pings) == 1

    def test_fail_suffix_when_tokens_missing(self, config, keychain, forwards, pings):
        keychain["newsdesk-hc-url"] = "https://hc.example/abc"
        del keychain["newsdesk-user-key"]
        rt = nd.new_relay(config, now=1000.0)
        nd.relay_cycle(rt, 1000.0 + nd.HEARTBEAT_INTERVAL_S)
        assert pings[0][0][-1] == "https://hc.example/abc/fail"
        assert nd.build_state(rt)["pushover"] == "no keychain tokens"

    def test_never_fail_under_no_pushover(self, config, keychain, forwards, pings):
        keychain["newsdesk-hc-url"] = "https://hc.example/abc"
        del keychain["newsdesk-user-key"]
        rt = nd.new_relay(config, no_pushover=True, now=1000.0)
        nd.relay_cycle(rt, 1000.0 + nd.HEARTBEAT_INTERVAL_S)
        assert pings[0][0][-1] == "https://hc.example/abc"

    def test_missing_tokens_are_reread_on_heartbeat(self, config, keychain, forwards, pings):
        del keychain["newsdesk-user-key"]
        rt = nd.new_relay(config, now=1000.0)
        keychain["newsdesk-user-key"] = "usr"
        nd.relay_cycle(rt, 1000.0 + nd.HEARTBEAT_INTERVAL_S)
        assert nd.build_state(rt)["pushover"] == "≥ 1"

    def test_no_ping_from_a_cycle_that_raised(self, config, keychain, forwards, pings, monkeypatch):
        keychain["newsdesk-hc-url"] = "https://hc.example/abc"
        write_jsonl(config["queue_file"], [entry("x", id="a")])
        rt = nd.new_relay(config, now=1000.0)

        def failing_append(path, new_entries):
            raise OSError("disk full")

        monkeypatch.setattr(nd, "append_to_history", failing_append)
        with pytest.raises(OSError):
            nd.relay_cycle(rt, 1000.0 + nd.HEARTBEAT_INTERVAL_S)
        assert pings == []

    def test_state_file_written_and_old_temp_files_removed(self, config, keychain, forwards, pings):
        old = config["queue_file"] + ".incoming.old"
        fresh = config["queue_file"] + ".incoming.fresh"
        for p in (old, fresh):
            open(p, "w").close()
        os.utime(old, (1.0, 1.0))
        rt = nd.new_relay(config, now=time.time())
        nd.relay_cycle(rt, time.time() + nd.HEARTBEAT_INTERVAL_S)
        state = json.load(open(os.path.join(os.path.dirname(config["queue_file"]), "relay.state")))
        assert state["pushover"] == "≥ 1" and state["remotes"] == {}
        assert not os.path.exists(old) and os.path.exists(fresh)


class TestRelayOnce:
    """T1: --once prints a summary even for an empty cycle, writes state, never pings."""

    def test_once(self, config, keychain, forwards, pings, monkeypatch, capsys):
        keychain["newsdesk-hc-url"] = "https://hc.example/abc"
        monkeypatch.setattr(nd, "load_config", lambda path: config)
        rc = nd.cmd_relay(argparse.Namespace(once=True, no_pushover=True))
        assert rc == 0
        out = capsys.readouterr().out
        assert "consumed=0" in out and "remotes=0/0" in out
        assert os.path.exists(os.path.join(os.path.dirname(config["queue_file"]), "relay.state"))
        assert pings == []


class TestRelaySingleInstance:
    """T15: a second relay exits 1 while the lock is held."""

    def test_second_relay_exits_1(self, config, keychain, forwards, pings, monkeypatch, capsys):
        held = nd.acquire_relay_lock(os.path.dirname(config["queue_file"]))
        assert held is not None
        assert nd.acquire_relay_lock(os.path.dirname(config["queue_file"])) is None
        monkeypatch.setattr(nd, "load_config", lambda path: config)
        assert nd.cmd_relay(argparse.Namespace(once=True, no_pushover=True)) == 1
        held.close()


# ---------------------------------------------------------------------------
# T13, T17 — changed helpers
# ---------------------------------------------------------------------------
class TestHistoryWriteIsAtomic:
    """T13: history is replaced whole, never truncated in place."""

    def test_goes_through_replace(self, tmp_path, monkeypatch):
        history = str(tmp_path / "history.jsonl")
        nd.append_to_history(history, [entry("a")])
        replaced = []
        real_replace = os.replace

        def spy(src, dst):
            # at the moment of the swap the old file is still whole
            replaced.append((dst, len(nd.parse_jsonl(dst))))
            real_replace(src, dst)

        monkeypatch.setattr(nd.os, "replace", spy)
        nd.append_to_history(history, [entry("b")])
        assert replaced == [(history, 1)]
        assert titles(history) == ["a", "b"]
        assert [n for n in os.listdir(tmp_path) if "tmp" in n] == []


class TestReadKeychainTokenAccount:
    """T17: the account argument reaches the security command."""

    def _capture(self, monkeypatch):
        seen = {}

        def fake_run(cmd, *a, **k):
            seen["cmd"] = cmd

            class R:
                returncode = 0
                stdout = "secret\n"

            return R()

        monkeypatch.setattr(nd.subprocess, "run", fake_run)
        return seen

    def test_default_account_is_pushover(self, monkeypatch):
        seen = self._capture(monkeypatch)
        assert nd.read_keychain_token("newsdesk-app-token") == "secret"
        assert seen["cmd"][seen["cmd"].index("-a") + 1] == "pushover"

    def test_account_argument(self, monkeypatch):
        seen = self._capture(monkeypatch)
        nd.read_keychain_token("newsdesk-hc-url", account="dave")
        assert seen["cmd"][seen["cmd"].index("-a") + 1] == "dave"


class TestForwardReportsOutcome:
    """C11: forward_to_pushover says whether Pushover accepted the message."""

    def _run(self, monkeypatch, returncode, stdout):
        def fake_run(cmd, *a, **k):
            class R:
                pass

            r = R()
            r.returncode, r.stdout = returncode, stdout
            return r

        monkeypatch.setattr(nd.subprocess, "run", fake_run)
        return nd.forward_to_pushover({"title": "T", "message": "M", "priority": 1}, "a", "u")

    def test_accepted(self, monkeypatch):
        assert self._run(monkeypatch, 0, b'{"status":1,"request":"x"}') is True

    def test_rejected_by_pushover(self, monkeypatch):
        assert self._run(monkeypatch, 0, b'{"status":0,"errors":["bad token"]}') is False

    def test_curl_failure(self, monkeypatch):
        assert self._run(monkeypatch, 6, b"") is False

    def test_exception(self, monkeypatch):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired("curl", 10)

        monkeypatch.setattr(nd.subprocess, "run", boom)
        assert nd.forward_to_pushover({"title": "T", "message": "M", "priority": 1}, "a", "u") is False


# ---------------------------------------------------------------------------
# T6 — send
# ---------------------------------------------------------------------------
class TestSendWithHub:
    """T6: send stamps an id, and spawns a detached push only when a hub is configured."""

    def _args(self):
        return argparse.Namespace(title="T", message="M", priority=0, project="t",
                                  url=None, url_title=None)

    def test_every_entry_has_a_distinct_id(self, tmp_path):
        queue = tmp_path / "queue.jsonl"
        nd.send_notification(str(queue), "A", "m", 0, "p")
        nd.send_notification(str(queue), "B", "m", 0, "p")
        ids = [e["id"] for e in nd.parse_jsonl(str(queue))]
        assert len(set(ids)) == 2 and all(ids)

    def test_no_hub_no_spawn(self, tmp_path, monkeypatch, pings):
        queue = tmp_path / "queue.jsonl"
        monkeypatch.setattr(nd, "load_config", lambda path: {"queue_file": str(queue)})
        assert nd.cmd_send(self._args()) == 0
        assert pings == []

    def test_hub_spawns_detached_push(self, tmp_path, monkeypatch, pings):
        queue = tmp_path / "queue.jsonl"
        monkeypatch.setattr(nd, "load_config", lambda path: {
            "queue_file": str(queue), "hub": {"host": "mini", "queue_file": "~/q.jsonl"}})
        assert nd.cmd_send(self._args()) == 0
        argv, kwargs = pings[0]
        assert argv == [sys.executable, os.path.abspath(nd.__file__), "push"]
        assert kwargs.get("start_new_session") is True
        assert len(nd.parse_jsonl(str(queue))) == 1

    def test_spawn_failure_still_queues_and_returns_0(self, tmp_path, monkeypatch):
        queue = tmp_path / "queue.jsonl"
        monkeypatch.setattr(nd, "load_config", lambda path: {
            "queue_file": str(queue), "hub": {"host": "mini", "queue_file": "~/q.jsonl"}})

        def boom(*a, **k):
            raise OSError("no fork")

        monkeypatch.setattr(nd.subprocess, "Popen", boom)
        assert nd.cmd_send(self._args()) == 0
        assert len(nd.parse_jsonl(str(queue))) == 1

    def test_rotation_skipped_only_with_hub(self, tmp_path, monkeypatch, pings):
        for name, cfg_extra, expected in (
            ("plain", {}, nd.QUEUE_ROTATE_TO),
            ("hub", {"hub": {"host": "mini", "queue_file": "~/q.jsonl"}}, nd.QUEUE_MAX_LINES + 1),
        ):
            queue = tmp_path / f"{name}.jsonl"
            write_jsonl(str(queue), [entry(f"e{i}") for i in range(nd.QUEUE_MAX_LINES)])
            cfg = {"queue_file": str(queue)}
            cfg.update(cfg_extra)
            monkeypatch.setattr(nd, "load_config", lambda path, cfg=cfg: cfg)
            nd.cmd_send(self._args())
            assert len(nd.parse_jsonl(str(queue))) == expected


# ---------------------------------------------------------------------------
# T3, T4, T5 — push
# ---------------------------------------------------------------------------
@pytest.fixture
def hub_config(tmp_path):
    return {"queue_file": str(tmp_path / "queue.jsonl"),
            "hub": {"host": "mini", "queue_file": "~/hub/queue.jsonl"}}


@pytest.fixture
def ships(monkeypatch):
    """Fake ssh transport: records shipped batches; `ok` controls the outcome."""
    box = {"batches": [], "ok": True, "hook": None}

    def fake_ship(hub, data):
        if box["hook"]:
            box["hook"]()
        if box["ok"]:
            box["batches"].append([json.loads(l)["title"] for l in data.decode().splitlines() if l])
        return box["ok"]

    monkeypatch.setattr(nd, "ship_batch", fake_ship)
    return box


class TestPushRecovery:
    """T3: push never deletes what it has not shipped."""

    def test_no_hub_is_a_no_op(self, tmp_path, ships):
        queue = tmp_path / "queue.jsonl"
        write_jsonl(str(queue), [entry("a")])
        assert nd.push_queue({"queue_file": str(queue)}) == 0
        assert ships["batches"] == [] and titles(queue) == ["a"]

    def test_failure_keeps_batch_in_processing(self, hub_config, ships):
        ships["ok"] = False
        write_jsonl(hub_config["queue_file"], [entry("a")])
        assert nd.push_queue(hub_config) == 1
        assert titles(hub_config["queue_file"] + ".processing") == ["a"]

    def test_second_failure_loses_nothing(self, hub_config, ships):
        ships["ok"] = False
        write_jsonl(hub_config["queue_file"], [entry("a")])
        nd.push_queue(hub_config)
        write_jsonl(hub_config["queue_file"], [entry("b")])
        nd.push_queue(hub_config)
        on_disk = titles(hub_config["queue_file"] + ".processing") + titles(hub_config["queue_file"])
        assert sorted(on_disk) == ["a", "b"]

    def test_old_processing_is_still_shipped(self, hub_config, ships):
        processing = hub_config["queue_file"] + ".processing"
        write_jsonl(processing, [entry("ancient")])
        old = time.time() - nd.STALE_PROCESSING_AGE - 100
        os.utime(processing, (old, old))
        assert nd.push_queue(hub_config) == 0
        assert ships["batches"] == [["ancient"]]

    def test_success_after_two_failures_ships_everything_and_leaves_no_file(self, hub_config, ships):
        ships["ok"] = False
        write_jsonl(hub_config["queue_file"], [entry("a")])
        nd.push_queue(hub_config)
        write_jsonl(hub_config["queue_file"], [entry("b")])
        nd.push_queue(hub_config)
        ships["ok"] = True
        assert nd.push_queue(hub_config) == 0
        assert ships["batches"] == [["a"], ["b"]]
        assert not os.path.exists(hub_config["queue_file"])
        assert not os.path.exists(hub_config["queue_file"] + ".processing")

    def test_ssh_timeout_counts_as_failure(self, hub_config, monkeypatch):
        def slow(*a, **k):
            raise subprocess.TimeoutExpired("ssh", nd.PUSH_SSH_TIMEOUT_S)

        monkeypatch.setattr(nd.subprocess, "run", slow)
        assert nd.ship_batch(hub_config["hub"], b"x\n") is False

    def test_ship_batch_uses_bounded_ssh(self, hub_config, monkeypatch):
        seen = {}

        def fake_run(cmd, *a, **k):
            seen["cmd"], seen["kw"] = cmd, k

            class R:
                returncode = 0

            return R()

        monkeypatch.setattr(nd.subprocess, "run", fake_run)
        assert nd.ship_batch(hub_config["hub"], b"x\n") is True
        assert seen["cmd"][0] == "ssh" and "mini" in seen["cmd"]
        assert "BatchMode=yes" in " ".join(seen["cmd"])
        assert seen["kw"]["timeout"] == nd.PUSH_SSH_TIMEOUT_S
        assert seen["kw"]["input"] == b"x\n"
        assert "$HOME/hub/queue.jsonl" in seen["cmd"][-1]


class TestPushLock:
    """T5: one push at a time; the holder drains what later sends wrote."""

    def test_entry_written_mid_ship_goes_out_in_the_same_push(self, hub_config, ships):
        write_jsonl(hub_config["queue_file"], [entry("first")])
        state = {"done": False}

        def late_send():
            if not state["done"]:
                state["done"] = True
                write_jsonl(hub_config["queue_file"], [entry("late")])

        ships["hook"] = late_send
        assert nd.push_queue(hub_config) == 0
        assert ships["batches"] == [["first"], ["late"]]

    def test_push_exits_0_when_lock_is_held(self, hub_config, ships):
        write_jsonl(hub_config["queue_file"], [entry("a")])
        with open(hub_config["queue_file"] + ".push.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            assert nd.push_queue(hub_config) == 0
        assert ships["batches"] == [] and titles(hub_config["queue_file"]) == ["a"]


class TestPushRemoteCommand:
    """T4: the command push runs on the hub, executed for real under sh."""

    def _run(self, queue, batch_id, data):
        cmd = nd.build_push_command(str(queue), batch_id)
        return subprocess.run(["sh", "-c", cmd], input=data, capture_output=True)

    def test_lands_as_one_spool_file_with_exact_bytes(self, tmp_path):
        queue = tmp_path / "hub" / "queue.jsonl"
        data = (json.dumps(entry("a", id="1")) + "\n").encode()
        assert self._run(queue, "b1", data).returncode == 0
        assert sorted(os.listdir(queue.parent)) == ["queue.jsonl.in.b1"]
        assert (queue.parent / "queue.jsonl.in.b1").read_bytes() == data

    def test_queue_file_itself_is_untouched(self, tmp_path):
        queue = tmp_path / "queue.jsonl"
        write_jsonl(str(queue), [entry("local")])
        self._run(queue, "b1", b'{"title": "x"}\n')
        assert titles(queue) == ["local"]

    def test_no_line_lost_against_a_concurrent_consumer(self, tmp_path):
        queue = tmp_path / "queue.jsonl"
        total = 150
        got, stop = [], threading.Event()

        def consumer():
            while True:
                finished = stop.is_set()
                entries, paths = nd.claim_spool(str(queue))
                got.extend(e["id"] for e in entries)
                for p in paths:
                    os.unlink(p)
                if finished:
                    return

        t = threading.Thread(target=consumer)
        t.start()
        for i in range(total):
            data = (json.dumps(entry("x", id=str(i))) + "\n").encode()
            assert self._run(queue, f"b{i}", data).returncode == 0
        stop.set()
        t.join()
        assert sorted(got, key=int) == [str(i) for i in range(total)]


# ---------------------------------------------------------------------------
# T2, T16 — remote pull
# ---------------------------------------------------------------------------
class TestRemotePullAndAck:
    """T16: pull deletes nothing; ack removes exactly what was pulled."""

    def _pull(self, queue, batch_id):
        cmd = nd.build_pull_command(str(queue), batch_id)
        r = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True)
        assert r.returncode == 0
        return nd.parse_pull_output(r.stdout)

    def test_pull_renames_and_returns_entries_and_names(self, tmp_path):
        queue = tmp_path / "queue.jsonl"
        write_jsonl(str(queue), [entry("a", id="1"), entry("b", id="2")])
        entries, files = self._pull(queue, "p1")
        assert [e["title"] for e in entries] == ["a", "b"]
        assert files == [str(queue) + ".out.p1"]
        assert not queue.exists() and os.path.exists(files[0])

    def test_second_pull_without_ack_returns_the_same_entries_plus_new(self, tmp_path):
        queue = tmp_path / "queue.jsonl"
        write_jsonl(str(queue), [entry("a", id="1")])
        self._pull(queue, "p1")
        write_jsonl(str(queue), [entry("b", id="2")])
        entries, files = self._pull(queue, "p2")
        assert sorted(e["title"] for e in entries) == ["a", "b"]
        assert len(files) == 2

    def test_ack_removes_exactly_the_named_files(self, tmp_path):
        queue = tmp_path / "queue.jsonl"
        write_jsonl(str(queue), [entry("a", id="1")])
        _, first = self._pull(queue, "p1")
        write_jsonl(str(queue), [entry("b", id="2")])
        _, both = self._pull(queue, "p2")
        r = subprocess.run(["sh", "-c", nd.build_ack_command(first)], capture_output=True)
        assert r.returncode == 0
        remaining = [f for f in both if os.path.exists(f)]
        assert remaining == [str(queue) + ".out.p2"]

    def test_no_queue_file_is_reached_with_no_entries(self, tmp_path):
        entries, files = self._pull(tmp_path / "queue.jsonl", "p1")
        assert entries == [] and files == []

    def test_file_without_trailing_newline_does_not_swallow_the_next_name(self, tmp_path):
        queue = tmp_path / "queue.jsonl"
        (tmp_path / "queue.jsonl.out.a").write_text(json.dumps(entry("a", id="1")))
        (tmp_path / "queue.jsonl.out.b").write_text(json.dumps(entry("b", id="2")) + "\n")
        entries, files = self._pull(queue, "p1")
        assert sorted(e["title"] for e in entries) == ["a", "b"] and len(files) == 2

    def test_ssh_failure_and_timeout_are_not_reached(self, monkeypatch):
        class R:
            returncode, stdout = 255, ""

        monkeypatch.setattr(nd.subprocess, "run", lambda *a, **k: R())
        assert nd.pull_remote_queue("box", "~/q.jsonl") == ([], False, [])

        def slow(*a, **k):
            raise subprocess.TimeoutExpired("ssh", nd.REMOTE_PULL_TIMEOUT_S)

        monkeypatch.setattr(nd.subprocess, "run", slow)
        assert nd.pull_remote_queue("box", "~/q.jsonl") == ([], False, [])

    def test_ack_refuses_names_that_are_not_out_files(self):
        with pytest.raises(ValueError):
            nd.build_ack_command(["/etc/passwd"])


class TestRelayRemoteCadence:
    """T2: remotes are pulled on a 30 s cadence and acked after the history write."""

    @pytest.fixture
    def remote(self, config, monkeypatch):
        config["remote_machines"] = [{"name": "box", "host": "box", "queue_file": "~/q.jsonl"}]
        box = {"pulls": 0, "acks": [], "reached": True, "entries": [entry("from-box", 2, "r1")],
               "history_at_ack": None}

        def fake_pull(host, queue_file):
            box["pulls"] += 1
            if not box["reached"]:
                return [], False, []
            return list(box["entries"]), True, ["/h/q.jsonl.out.1"] if box["entries"] else []

        def fake_ack(host, files):
            box["acks"].append((host, files))
            box["history_at_ack"] = titles(config["history_file"])

        monkeypatch.setattr(nd, "pull_remote_queue", fake_pull)
        monkeypatch.setattr(nd, "ack_remote_queue", fake_ack)
        return box

    def test_no_remotes_pulls_nothing(self, config, keychain, forwards, monkeypatch):
        def never(*a):
            raise AssertionError("pulled")

        monkeypatch.setattr(nd, "pull_remote_queue", never)
        rt = nd.new_relay(config, now=1000.0)
        assert nd.relay_cycle(rt, 1000.0)["remotes_total"] == 0

    def test_first_cycle_then_only_after_interval(self, config, keychain, forwards, remote):
        rt = nd.new_relay(config, now=1000.0)
        summary = nd.relay_cycle(rt, 1000.0)
        assert remote["pulls"] == 1 and summary["remotes_reached"] == 1
        nd.relay_cycle(rt, 1000.0 + nd.REMOTE_POLL_INTERVAL_S - 1)
        assert remote["pulls"] == 1
        nd.relay_cycle(rt, 1000.0 + nd.REMOTE_POLL_INTERVAL_S)
        assert remote["pulls"] == 2

    def test_ack_after_history_and_only_for_returned_files(self, config, keychain, forwards, remote):
        rt = nd.new_relay(config, now=1000.0)
        nd.relay_cycle(rt, 1000.0)
        assert remote["acks"] == [("box", ["/h/q.jsonl.out.1"])]
        assert remote["history_at_ack"] == ["from-box"]
        assert forwards["calls"] == ["from-box"]
        remote["entries"] = []
        nd.relay_cycle(rt, 1000.0 + nd.REMOTE_POLL_INTERVAL_S)
        assert len(remote["acks"]) == 1

    def test_unreached_remote_keeps_its_last_reached_time(self, config, keychain, forwards, remote):
        rt = nd.new_relay(config, now=1000.0)
        nd.relay_cycle(rt, 1000.0)
        remote["reached"] = False
        summary = nd.relay_cycle(rt, 1000.0 + nd.REMOTE_POLL_INTERVAL_S)
        assert summary["remotes_reached"] == 0
        assert nd.build_state(rt)["remotes"] == {"box": 1000.0}


# ---------------------------------------------------------------------------
# T7, T8 — web
# ---------------------------------------------------------------------------
@pytest.fixture
def web(config, keychain, forwards):
    rt = nd.new_relay(config, now=1000.0)
    server = nd.make_web_server(rt, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, rt
    server.shutdown()
    server.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, dict(r.headers), r.read()


class TestFeed:
    """T7: the feed returns the newest entries in the order the hub accepted them."""

    def test_limit_and_order_with_a_late_old_entry(self, web, config):
        base, rt = web
        nd.append_to_history(config["history_file"], [
            entry("one", ts=100.0), entry("two", ts=200.0), entry("late-but-old", ts=5.0)])
        status, headers, body = get(base + "/api/feed?limit=2")
        feed = json.loads(body)
        assert [e["title"] for e in feed["entries"]] == ["two", "late-but-old"]
        assert feed["state"]["pushover"] == "≥ 1"
        assert feed["stale_after_s"] == nd.RELAY_STALE_AFTER_S

    def test_default_limit(self, web, config):
        base, rt = web
        nd.append_to_history(config["history_file"],
                             [entry(f"e{i}") for i in range(nd.WEB_FEED_ENTRIES + 5)])
        feed = json.loads(get(base + "/api/feed")[2])
        assert len(feed["entries"]) == nd.WEB_FEED_ENTRIES
        assert feed["entries"][-1]["title"] == f"e{nd.WEB_FEED_ENTRIES + 4}"

    def test_bad_limit_falls_back_to_default(self, web, config):
        base, rt = web
        nd.append_to_history(config["history_file"], [entry("a")])
        assert len(json.loads(get(base + "/api/feed?limit=abc")[2])["entries"]) == 1


class TestWebRoutes:
    """T8: the page, the feed, and nothing else."""

    PAGE_IDS = ["status", "banner", "list", "filter", "bell", "bell-threshold",
                "show-silent", "mark-read", "snapshot"]

    def test_page_is_served_uncached_with_the_ids_the_script_needs(self, web):
        base, rt = web
        status, headers, body = get(base + "/")
        assert status == 200 and headers["Cache-Control"] == "no-store"
        html = body.decode()
        for element_id in self.PAGE_IDS:
            assert f'id="{element_id}"' in html
        assert "/api/feed" in html

    def test_page_never_builds_markup_from_entry_text(self):
        html = open(os.path.join(os.path.dirname(nd.__file__), "web", "index.html")).read()
        assert "innerHTML" not in html and "textContent" in html

    def test_unknown_path_is_404(self, web):
        base, rt = web
        with pytest.raises(urllib.error.HTTPError) as e:
            get(base + "/etc/passwd")
        assert e.value.code == 404

    def test_post_is_refused(self, web):
        base, rt = web
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base + "/api/feed", data=b"x"), timeout=5)
        assert e.value.code in (405, 501)

    def test_bind_on_a_taken_port_retries_until_it_is_free(self, config, keychain, forwards):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        rt = nd.new_relay(config, now=1000.0)
        stop = threading.Event()
        t = threading.Thread(target=nd.web_server_loop, args=(rt, "127.0.0.1", port, stop),
                             kwargs={"retry_s": 0.05}, daemon=True)
        t.start()
        time.sleep(0.2)
        assert t.is_alive() and rt.get("web_server") is None
        blocker.close()
        deadline = time.time() + 5
        while rt.get("web_server") is None and time.time() < deadline:
            time.sleep(0.05)
        assert get(f"http://127.0.0.1:{port}/api/feed")[0] == 200
        stop.set()
        rt["web_server"].shutdown()
        t.join(timeout=5)
        assert not t.is_alive()
