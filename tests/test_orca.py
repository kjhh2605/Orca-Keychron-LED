import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orca_led.orca import (OrcaClient, OrcaError, OrcaRPC, LocalUIFileProvider,
                           state_from_agent_status, aggregate_states,
                           IDLE, RUNNING, WAITING, COMPLETE, ERROR, UNKNOWN)

NOW = 1800000000.0

def hook(state, outcome=None, **extra):
    main = {"state": state, "stateStartedAt": NOW * 1000}
    if outcome is not None:
        main["outcome"] = outcome
    return {"state": state, "mainAgent": main, "stateObservedAt": NOW * 1000, **extra}


class FakeRPC:
    def __init__(self, workspaces=None):
        self.calls = []
        self.runtime_id = "runtime-1"
        self.version = "1.4.216"
        self.workspaces = workspaces if workspaces is not None else [
            {"worktree": "repo::/b", "tabs": [{"type": "terminal", "id": "tab-b", "agentStatus": hook("done")}]},
            {"worktree": "repo::/a", "tabs": [{"type": "terminal", "id": "tab-a", "agentStatus": hook("working")}]},
        ]

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "status.get":
            return {"runtimeId": self.runtime_id, "appVersion": self.version, "graphStatus": "ready",
                    "desktopWindowStatus": "available", "authoritativeWindowId": 1}
        if method == "session.tabs.listAll":
            return {"snapshots": self.workspaces}
        if method == "worktree.activate":
            return {"activated": True, "worktreeId": params["worktree"][3:]}
        raise AssertionError(method)


def ui_snapshot(rows=None, **overrides):
    return {"source": "orca-renderer", "captured_at": NOW, "runtime_id": "runtime-1",
            "window_id": 1, "active_view": "terminal", "selected_id": "repo::/a",
            "slots": rows or [{"id": "repo::/a", "label": "A"}, {"id": "repo::/b", "label": "B"}],
            **overrides}


class RawHookJoinTest(unittest.TestCase):
    def setUp(self):
        self.route = {"worktreeId": "repo::/a", "terminalHandle": "terminal1",
                      "tabId": "parent", "paneKey": "parent:leaf"}
        stamp = NOW * 1000 - 25
        self.raw = {**self.route, "state": "done", "agentType": "codex",
                    "mainAgent": {"state": "done", "stateStartedAt": stamp},
                    "evidenceObservedAt": stamp, "receivedAt": stamp,
                    "observation": {"origin": "hook", "authorityId": "main-agent-hooks:one",
                                    "incarnation": 3, "revision": 60, "observedAt": stamp,
                                    "kind": "transition"}}
        self.projected = {**self.route, "state": "done", "agentType": "codex",
                          "updatedAt": NOW * 1000, "stateStartedAt": NOW * 1000}
        self.tab = {"type": "terminal", "id": "parent::leaf", "parentTabId": "parent",
                    "leafId": "leaf", "terminal": "terminal1", "ptyId": "pty-current",
                    "status": "ready", "agentStatus": self.projected}
        self.rpc = FakeRPC([{"worktree": "repo::/a", "tabs": [self.tab]}])
        self.ui = ui_snapshot(hook_statuses=[self.raw])
        self.client = OrcaClient(rpc=self.rpc, ui_provider=lambda: self.ui, clock=lambda: NOW)

    def state(self):
        result = self.client.snapshot()
        self.assertTrue(result["supported"], result)
        return result["slots"][0]["state"]

    def test_real_stop_recovers_projection_without_claiming_success(self):
        self.assertEqual(self.client.inventory()["workspaces"]["repo::/a"]["state"], UNKNOWN)
        result = self.client.snapshot()["slots"][0]
        self.assertEqual(result["state"], COMPLETE)
        self.assertEqual(result["completion_source"], "codex_stop")
        self.assertFalse(result["outcome_verified"])

    def test_current_terminal_and_every_route_component_must_match(self):
        original = deepcopy(self.raw)
        for key in self.route:
            for source in (self.raw, self.projected):
                with self.subTest(key=key, raw=source is self.raw):
                    value = source[key]
                    source[key] = "other"
                    self.assertEqual(self.state(), UNKNOWN)
                    source[key] = value
        self.assertEqual(self.raw, original)
        for key, value in [("status", "restoring"), ("ptyId", None), ("terminal", "new-handle"),
                           ("parentTabId", "new-parent"), ("leafId", "new-leaf")]:
            with self.subTest(key=key):
                previous = self.tab[key]
                self.tab[key] = value
                self.assertEqual(self.state(), UNKNOWN)
                self.tab[key] = previous

    def test_missing_or_ambiguous_hook_and_missing_projection_remain_unknown(self):
        for rows in ([], [self.raw, deepcopy(self.raw)]):
            self.ui["hook_statuses"] = rows
            self.assertEqual(self.state(), UNKNOWN)
        self.ui["hook_statuses"] = [self.raw]
        self.tab.pop("agentStatus")
        self.assertEqual(self.state(), UNKNOWN)

    def test_old_stop_cannot_hide_new_work_waiting_or_different_provider(self):
        for state in ("working", "waiting", "blocked", None, [], {}):
            with self.subTest(state=state):
                self.projected["state"] = state
                self.assertEqual(self.state(), UNKNOWN)
        self.projected["state"] = "done"
        self.projected["agentType"] = "claude"
        self.assertEqual(self.state(), UNKNOWN)

    def test_raw_child_activity_cancel_failure_and_explicit_invalid_outcome_survive(self):
        for aggregate, outcome, expected in [("working", None, RUNNING), ("waiting", None, WAITING),
                                              ("done", "failure", ERROR), ("done", "cancellation", IDLE),
                                              ("done", "success", COMPLETE), ("done", "other", UNKNOWN),
                                              ("done", {}, UNKNOWN)]:
            with self.subTest(aggregate=aggregate, outcome=outcome):
                self.raw["state"] = self.projected["state"] = aggregate
                self.raw["mainAgent"] = {"state": "done"}
                if outcome is not None:
                    self.raw["mainAgent"]["outcome"] = outcome
                self.assertEqual(self.state(), expected)
        self.raw["mainAgent"] = {"state": "done"}
        for source in (self.raw, self.projected):
            for flag in ("interrupted", "sessionBoundary"):
                source[flag] = True
                self.assertEqual(self.state(), IDLE)
                source.pop(flag)

    def test_only_observed_non_restored_hook_rows_are_eligible(self):
        for source in (self.raw, self.projected):
            for flag in ("restoredUnconfirmed", "providerSessionOnly"):
                source[flag] = True
                self.assertEqual(self.state(), UNKNOWN)
                source.pop(flag)
        observation = deepcopy(self.raw["observation"])
        for key, value in [("origin", "title"), ("origin", "osc"), ("authorityId", "other:one"),
                           ("incarnation", -1), ("incarnation", True), ("revision", "60"),
                           ("kind", "identity-only"), ("kind", []), ("observedAt", 0)]:
            with self.subTest(key=key, value=value):
                self.raw["observation"] = {**observation, key: value}
                self.assertEqual(self.state(), UNKNOWN)
        self.raw["observation"] = observation
        self.raw.pop("mainAgent")
        self.assertEqual(self.state(), UNKNOWN)

    def test_projection_time_must_identify_same_turn_not_later_title(self):
        for stamp in (NOW * 1000 + 1, NOW * 1000 - 26, None, True, "bad", float("nan")):
            with self.subTest(stamp=stamp):
                self.projected["updatedAt"] = stamp
                self.assertEqual(self.state(), UNKNOWN)
        self.projected["updatedAt"] = NOW * 1000
        self.raw["evidenceObservedAt"] = self.raw["receivedAt"] = NOW * 1000 - 5001
        self.raw["observation"]["observedAt"] = self.raw["evidenceObservedAt"]
        self.assertEqual(self.state(), UNKNOWN)
        # A previously matched finished turn remains complete without a new
        # transition; age is not mistaken for fresh evidence or a new title.
        self.projected["updatedAt"] -= 7200000
        self.raw["evidenceObservedAt"] = self.raw["receivedAt"] = self.projected["updatedAt"] - 25
        self.raw["observation"]["observedAt"] = self.raw["evidenceObservedAt"]
        self.assertEqual(self.state(), COMPLETE)

    def test_explicit_session_run_and_authority_conflicts_reject_match(self):
        self.raw["providerSession"] = {"key": "session_id", "id": "old-session"}
        self.projected["providerSession"] = {"key": "session_id", "id": "new-session"}
        self.assertEqual(self.state(), UNKNOWN)
        self.projected["providerSession"] = deepcopy(self.raw["providerSession"])
        self.assertEqual(self.state(), COMPLETE)
        for key in ("runId", "executionId"):
            self.projected[key] = "new-run"
            self.raw[key] = "old-run"
            self.assertEqual(self.state(), UNKNOWN)
            self.projected.pop(key)
        self.projected["observation"] = {**self.raw["observation"], "incarnation": 4}
        self.assertEqual(self.state(), UNKNOWN)

    def test_existing_rich_projection_has_priority_over_raw_hook(self):
        self.tab["agentStatus"] = hook("working", agentType="codex")
        self.assertEqual(self.state(), RUNNING)
        self.tab["agentStatus"] = hook("done", "failure", agentType="codex")
        self.assertEqual(self.state(), ERROR)

    def test_raw_rows_are_only_used_after_renderer_and_runtime_validation(self):
        self.ui["source"] = "orca-accessibility"
        self.assertEqual(self.state(), UNKNOWN)
        self.ui["source"] = "orca-renderer"
        self.ui["runtime_id"] = "previous-run"
        self.assertFalse(self.client.snapshot()["supported"])
        self.ui["runtime_id"] = "runtime-1"
        self.ui["hook_statuses"] = {"invalid": self.raw}
        self.assertFalse(self.client.snapshot()["supported"])


class AgentStatesTest(unittest.TestCase):
    def test_codex_stop_is_completed_without_claiming_a_success_verdict(self):
        value = hook("done", agentType="codex")
        self.assertEqual(state_from_agent_status(value, now_ms=NOW * 1000), COMPLETE)
        self.assertNotIn("outcome", value["mainAgent"])
        # Finished turns follow the existing done policy: age alone does not
        # undo completion while the authoritative row still reports that turn.
        value["stateObservedAt"] = (NOW - 7200) * 1000
        self.assertEqual(state_from_agent_status(value, now_ms=NOW * 1000), COMPLETE)

    def test_codex_stop_requires_valid_observation_time(self):
        for stamp in [None, True, "1800000000000", float("nan"), float("inf"), -1, 0, NOW * 1000 + 1]:
            with self.subTest(stamp=stamp):
                value = hook("done", agentType="codex", stateObservedAt=stamp)
                self.assertEqual(state_from_agent_status(value, now_ms=NOW * 1000), UNKNOWN)
        for field in ["stateObservedAt", "evidenceObservedAt", "updatedAt"]:
            with self.subTest(field=field):
                value = hook("done", agentType="codex")
                value.pop("stateObservedAt")
                value[field] = NOW * 1000
                self.assertEqual(state_from_agent_status(value, now_ms=NOW * 1000), COMPLETE)
        value = hook("done", agentType="codex")
        value.pop("stateObservedAt")
        self.assertEqual(state_from_agent_status(value, now_ms=NOW * 1000), UNKNOWN)

    def test_codex_stop_never_overrides_cancel_failure_or_session_reset(self):
        for outcome, extra, expected in [
            ("success", {}, COMPLETE), ("failure", {}, ERROR), ("cancellation", {}, IDLE),
            (None, {"interrupted": True}, IDLE), (None, {"sessionBoundary": True}, IDLE),
            (None, {"restoredUnconfirmed": True}, UNKNOWN),
        ]:
            with self.subTest(outcome=outcome, extra=extra):
                value = hook("done", outcome, agentType="codex", **extra)
                self.assertEqual(state_from_agent_status(value, now_ms=NOW * 1000), expected)

    def test_codex_stop_does_not_hide_child_work_or_a_new_turn(self):
        for state, expected in [("working", RUNNING), ("waiting", WAITING), ("blocked", WAITING)]:
            value = hook("done", agentType="codex")
            value["state"] = state
            self.assertEqual(state_from_agent_status(value, now_ms=NOW * 1000), expected)
        self.assertEqual(state_from_agent_status(hook("working", agentType="codex"), now_ms=NOW * 1000), RUNNING)
        inconsistent = hook("working", agentType="codex")
        inconsistent["state"] = "done"
        self.assertEqual(state_from_agent_status(inconsistent, now_ms=NOW * 1000), UNKNOWN)

    def test_claude_stop_is_completed_like_codex_stop(self):
        value = hook("done", agentType="claude")
        self.assertEqual(state_from_agent_status(value, now_ms=NOW * 1000), COMPLETE)
        for outcome, extra, expected in [
            ("failure", {}, ERROR), ("cancellation", {}, IDLE),
            (None, {"interrupted": True}, IDLE), (None, {"restoredUnconfirmed": True}, UNKNOWN),
        ]:
            with self.subTest(outcome=outcome, extra=extra):
                value = hook("done", outcome, agentType="claude", **extra)
                self.assertEqual(state_from_agent_status(value, now_ms=NOW * 1000), expected)
        value = hook("done", agentType="claude")
        value.pop("stateObservedAt")
        self.assertEqual(state_from_agent_status(value, now_ms=NOW * 1000), UNKNOWN)

    def test_only_explicit_stop_hook_is_completed_without_outcome(self):
        for provider in [None, "unknown", "Codex", "Claude"]:
            self.assertEqual(state_from_agent_status(hook("done", agentType=provider), now_ms=NOW * 1000), UNKNOWN)
        for value in [None, {"state": "done", "agentType": "codex", "updatedAt": NOW * 1000},
                      {"state": "done", "agentType": "codex", "mainAgent": None, "updatedAt": NOW * 1000}]:
            self.assertEqual(state_from_agent_status(value, now_ms=NOW * 1000), UNKNOWN)
        for outcome in [None, "", "unknown", "completed", False, {}, []]:
            with self.subTest(outcome=outcome):
                value = hook("done", agentType="codex")
                value["mainAgent"]["outcome"] = outcome
                self.assertEqual(state_from_agent_status(value, now_ms=NOW * 1000), UNKNOWN)

    def test_explicit_state_sequence_and_outcomes(self):
        for value, expected in [
            (hook("working"), RUNNING), (hook("waiting"), WAITING),
            (hook("blocked"), WAITING), (hook("done"), UNKNOWN),
            (hook("done", "success"), COMPLETE), (hook("done", "failure"), ERROR),
            (hook("done", "cancellation"), IDLE), (hook("done", interrupted=True), IDLE),
            (hook("done", sessionBoundary=True), IDLE),
        ]:
            with self.subTest(value=value):
                self.assertEqual(state_from_agent_status(value, now_ms=NOW * 1000), expected)

    def test_title_heuristics_and_missing_evidence_are_unknown(self):
        for value in [None, {}, {"state": "done"}, {"state": "working", "updatedAt": NOW * 1000},
                      hook("working", restoredUnconfirmed=True),
                      hook("working", stateObservedAt=(NOW - 1801) * 1000),
                      hook("working", stateObservedAt=(NOW + 60) * 1000)]:
            self.assertEqual(state_from_agent_status(value, now_ms=NOW * 1000), UNKNOWN)

    def test_live_child_work_prevents_completed_main_appearing_done(self):
        value = hook("done", "success")
        value["state"] = "working"
        self.assertEqual(state_from_agent_status(value, now_ms=NOW * 1000), RUNNING)
        value["state"] = "waiting"
        self.assertEqual(state_from_agent_status(value, now_ms=NOW * 1000), WAITING)

    def test_workspace_priority_unknown_blocks_partial_completion(self):
        self.assertEqual(aggregate_states([COMPLETE, UNKNOWN]), UNKNOWN)
        self.assertEqual(aggregate_states([UNKNOWN, RUNNING]), RUNNING)
        self.assertEqual(aggregate_states([RUNNING, WAITING]), WAITING)
        self.assertEqual(aggregate_states([WAITING, ERROR]), ERROR)
        self.assertEqual(aggregate_states([]), IDLE)


class ClientTest(unittest.TestCase):
    def test_real_ui_order_wins_over_backend_order(self):
        result = OrcaClient(lambda: ui_snapshot(), FakeRPC(), clock=lambda: NOW).snapshot()
        self.assertTrue(result["supported"])
        self.assertEqual([s["id"] for s in result["slots"][:2]], ["repo::/a", "repo::/b"])
        self.assertEqual([s["state"] for s in result["slots"][:2]], [RUNNING, UNKNOWN])
        self.assertFalse(result["slots"][1]["outcome_verified"])
        self.assertEqual(len(result["slots"]), 12)
        self.assertEqual(result["selected_id"], "repo::/a")

    def test_absent_ui_does_not_guess_slots_from_worktree_list(self):
        result = OrcaClient(rpc=FakeRPC(), clock=lambda: NOW).snapshot()
        self.assertFalse(result["supported"])
        self.assertEqual(result["reason"], "ui_order_unavailable")
        self.assertTrue(all(s["id"] is None for s in result["slots"]))
        self.assertIn("repo::/a", result["diagnostics"]["workspaces"])

    def test_changed_runtime_stale_ui_and_unknown_version_fail_closed(self):
        for changes, reason in [({"captured_at": NOW - 4}, "stale_ui_snapshot"),
                                ({"runtime_id": "old-runtime"}, "runtime_changed"),
                                ({"window_id": 3}, "window_mismatch")]:
            client = OrcaClient(lambda: ui_snapshot(**changes), FakeRPC(), clock=lambda: NOW)
            self.assertEqual(client.snapshot()["reason"], reason)
        rpc = FakeRPC()
        rpc.version = "1.5.0"
        self.assertEqual(OrcaClient(rpc=rpc).snapshot()["reason"], "unsupported_version")

    def test_view_and_modal_do_not_masquerade_as_current_workspace(self):
        for changes in [{"active_view": "settings"}, {"modal_open": True}]:
            result = OrcaClient(lambda: ui_snapshot(**changes), FakeRPC(), clock=lambda: NOW).snapshot()
            self.assertIsNone(result["selected_id"])

    def test_duplicate_and_remote_rows_are_not_silently_reordered(self):
        for rows, reason in [([{"id": "same"}, {"id": "same"}], "ambiguous_ui_snapshot"),
                             ([{"id": "same", "execution_host_id": "remote"}], "remote_workspace_unsupported")]:
            result = OrcaClient(lambda: ui_snapshot(rows), FakeRPC(), clock=lambda: NOW).snapshot()
            self.assertEqual(result["reason"], reason)

    def test_f12_selects_by_actual_id_and_host_only(self):
        rows = [{"id": f"repo::/{i}"} for i in range(1, 14)]
        rpc = FakeRPC([{"worktree": row["id"], "tabs": []} for row in rows])
        client = OrcaClient(lambda: ui_snapshot(rows), rpc, clock=lambda: NOW)
        result = client.select("repo::/12")
        self.assertTrue(result["accepted"])
        self.assertFalse(result["selection_verified"])
        self.assertEqual(rpc.calls[-1], ("worktree.activate", {"worktree": "id:repo::/12", "navigation": "host", "notifyClients": False}))
        with self.assertRaises(OrcaError):
            client.select("repo::/13")

    def test_deleted_target_never_triggers_navigation(self):
        rpc = FakeRPC([])
        client = OrcaClient(lambda: ui_snapshot(), rpc, clock=lambda: NOW)
        with self.assertRaises(OrcaError):
            client.select("repo::/a")
        self.assertFalse(any(name == "worktree.activate" for name, _ in rpc.calls))

    def test_standalone_folder_is_observable_but_default_navigation_is_blocked(self):
        identity = "folder:standalone-id"
        rpc = FakeRPC([{"worktree": identity, "tabs": [{"type": "terminal", "agentStatus": hook("working")}]}])
        client = OrcaClient(lambda: ui_snapshot([{"id": identity}], selected_id=identity), rpc, clock=lambda: NOW)
        snapshot = client.snapshot()
        self.assertEqual(snapshot["selected_id"], identity)
        self.assertEqual(snapshot["slots"][0]["state"], RUNNING)
        self.assertFalse(snapshot["slots"][0]["selectable"])
        with self.assertRaises(OrcaError) as error:
            client.select(identity)
        self.assertEqual(error.exception.code, "folder_activation_unsupported")
        self.assertFalse(any(name == "worktree.activate" for name, _ in rpc.calls))

    def test_standalone_folder_uses_explicit_selection_provider_when_available(self):
        identity = "folder:standalone-id"
        rpc = FakeRPC([{"worktree": identity, "tabs": []}])
        selected = []
        def select(row):
            selected.append(row["id"])
            return {"activated": True}
        client = OrcaClient(lambda: ui_snapshot([{"id": identity}]), rpc, select, clock=lambda: NOW)
        self.assertTrue(client.snapshot()["slots"][0]["selectable"])
        client.select(identity)
        self.assertEqual(selected, [identity])
        self.assertFalse(any(name == "worktree.activate" for name, _ in rpc.calls))

    def test_dispatch_guard_runs_after_reads_and_can_reject_without_navigation(self):
        rpc = FakeRPC()
        client = OrcaClient(lambda: ui_snapshot(), rpc, clock=lambda: NOW)
        def reject(snapshot):
            self.assertEqual(rpc.calls[-1][0], "session.tabs.listAll")
            self.assertEqual(snapshot["slots"][0]["id"], "repo::/a")
            raise OrcaError("event_expired", "Key event expired while reading Orca.")
        with self.assertRaises(OrcaError) as error:
            client.select("repo::/a", before_dispatch=reject)
        self.assertEqual(error.exception.code, "event_expired")
        self.assertFalse(any(name == "worktree.activate" for name, _ in rpc.calls))

    def test_dispatch_guard_receives_latest_snapshot_immediately_before_navigation(self):
        rpc = FakeRPC()
        client = OrcaClient(lambda: ui_snapshot(), rpc, clock=lambda: NOW)
        observed = []
        def allow(snapshot):
            observed.append((snapshot["runtime_id"], snapshot["window_id"]))
            rpc.calls.append(("dispatch_guard", None))
        client.select("repo::/a", before_dispatch=allow)
        self.assertEqual(observed, [("runtime-1", 1)])
        self.assertEqual([name for name, _ in rpc.calls[-2:]], ["dispatch_guard", "worktree.activate"])

    def test_done_without_outcome_does_not_signal_normal_completion(self):
        rpc = FakeRPC([{"worktree": "repo::/a", "tabs": [
            {"type": "terminal", "agentStatus": hook("done")},
        ]}])
        client = OrcaClient(lambda: ui_snapshot(), rpc, clock=lambda: NOW)
        self.assertEqual(client.snapshot()["slots"][0]["state"], UNKNOWN)
        rpc.workspaces[0]["tabs"][0]["agentStatus"] = hook("done", "success")
        self.assertEqual(client.snapshot()["slots"][0]["state"], COMPLETE)
        self.assertTrue(client.snapshot()["slots"][0]["outcome_verified"])

    def test_one_unobserved_terminal_prevents_partial_complete(self):
        rpc = FakeRPC([{"worktree": "repo::/a", "tabs": [
            {"type": "terminal", "agentStatus": hook("done", "success")},
            {"type": "terminal"},
        ]}])
        result = OrcaClient(lambda: ui_snapshot(), rpc, clock=lambda: NOW).snapshot()
        self.assertEqual(result["slots"][0]["state"], UNKNOWN)

    def test_completion_key_changes_with_each_finished_turn(self):
        tab = {"type": "terminal", "id": "t1", "agentStatus": hook("done", agentType="claude")}
        rpc = FakeRPC([{"worktree": "repo::/a", "tabs": [tab]}])
        client = OrcaClient(lambda: ui_snapshot(), rpc, clock=lambda: NOW)
        first = client.snapshot()["slots"][0]
        self.assertEqual(first["completion_key"], f"t1@{NOW * 1000}")
        tab["agentStatus"]["mainAgent"]["stateStartedAt"] = NOW * 1000 - 5
        self.assertNotEqual(client.snapshot()["slots"][0]["completion_key"], first["completion_key"])
        tab["agentStatus"] = hook("working", agentType="claude")
        self.assertIsNone(client.snapshot()["slots"][0]["completion_key"])

    def test_codex_completion_diagnostics_and_next_turn_are_preserved(self):
        tab = {"type": "terminal", "id": "codex-tab", "agentStatus": hook("done", agentType="codex")}
        rpc = FakeRPC([{"worktree": "repo::/a", "tabs": [tab]}])
        client = OrcaClient(lambda: ui_snapshot(), rpc, clock=lambda: NOW)
        complete = client.snapshot()["slots"][0]
        self.assertEqual(complete["state"], COMPLETE)
        self.assertEqual(complete["completion_source"], "codex_stop")
        self.assertFalse(complete["outcome_verified"])
        fact = client.inventory()["workspaces"]["repo::/a"]["agents"][0]
        self.assertTrue(fact["hook_observed"])
        self.assertIsNone(fact["outcome"])
        self.assertEqual(fact["completion_source"], "codex_stop")
        tab["agentStatus"] = hook("working", agentType="codex")
        running = client.snapshot()["slots"][0]
        self.assertEqual(running["state"], RUNNING)
        self.assertIsNone(running["completion_source"])
        tab.pop("agentStatus")
        missing = client.snapshot()["slots"][0]
        self.assertEqual(missing["state"], UNKNOWN)
        self.assertIsNone(missing["completion_source"])

    def test_codex_completion_does_not_hide_an_unobserved_terminal(self):
        rpc = FakeRPC([{"worktree": "repo::/a", "tabs": [
            {"type": "terminal", "agentStatus": hook("done", agentType="codex")},
            {"type": "terminal"},
        ]}])
        slot = OrcaClient(lambda: ui_snapshot(), rpc, clock=lambda: NOW).snapshot()["slots"][0]
        self.assertEqual(slot["state"], UNKNOWN)
        self.assertIsNone(slot["completion_source"])

    def test_malformed_codex_outcome_stays_unknown_without_breaking_polling(self):
        status = hook("done", agentType="codex")
        status["mainAgent"]["outcome"] = {"unrecognized": True}
        rpc = FakeRPC([{"worktree": "repo::/a", "tabs": [{"type": "terminal", "agentStatus": status}]}])
        snapshot = OrcaClient(lambda: ui_snapshot(), rpc, clock=lambda: NOW).snapshot()
        self.assertTrue(snapshot["supported"])
        self.assertEqual(snapshot["slots"][0]["state"], UNKNOWN)
        self.assertFalse(snapshot["slots"][0]["outcome_verified"])


class FileProviderTest(unittest.TestCase):
    def test_partial_file_is_never_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "ui"
            Path(str(prefix) + ".0.json").write_text(json.dumps(ui_snapshot()))
            Path(str(prefix) + ".1.json").write_text('{"source":"orca-renderer",')
            self.assertEqual(LocalUIFileProvider(prefix)()["runtime_id"], "runtime-1")
            single = Path(directory) / "ui.json"
            single.write_text(json.dumps(ui_snapshot()))
            self.assertEqual(LocalUIFileProvider(single)()["runtime_id"], "runtime-1")


class TransportTest(unittest.TestCase):
    def test_method_allowlist_prevents_unrelated_mutations(self):
        with self.assertRaises(OrcaError) as error:
            OrcaRPC().call("terminal.send", {"text": "do something"})
        self.assertEqual(error.exception.code, "unsupported_method")

    def test_split_frames_keepalive_and_matching_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            meta = Path(directory) / "runtime.json"
            meta.write_text(json.dumps({"runtimeId": "r", "authToken": "never-log-this",
                                        "transport": {"kind": "unix", "endpoint": "/tmp/test.sock"}}))
            class Socket:
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def settimeout(self, value): pass
                def connect(self, endpoint): self.endpoint = endpoint
                def sendall(self, body):
                    request = json.loads(body)
                    frame = json.dumps({"id": request["id"], "ok": True, "result": {"state": "ready"},
                                        "_meta": {"runtimeId": "r"}}).encode() + b"\n"
                    self.chunks = [b'{"_keepalive":true}\n' + frame[:10], frame[10:]]
                def recv(self, length): return self.chunks.pop(0) if self.chunks else b""
            with patch("orca_led.orca.socket.socket", return_value=Socket()):
                self.assertEqual(OrcaRPC(meta).call("status.get"), {"state": "ready"})


if __name__ == "__main__":
    unittest.main()
