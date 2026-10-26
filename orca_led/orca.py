"""Read-only Orca state adapter and explicitly targeted workspace navigation.

Orca 1.4.223's runtime API is an internal, version-specific interface. Its
terminal/worktree list order is NOT the Cmd+number order. A live UI provider
must supply that order; without one the adapter returns supported=False.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import socket
import time
import uuid
from typing import Any, Callable

EMPTY, IDLE, RUNNING, WAITING, COMPLETE, ERROR, UNKNOWN = range(7)
STATE_NAMES = ("empty", "idle", "running", "waiting", "complete", "error", "unknown")
STOP_HOOK_AGENTS = frozenset({"codex", "claude"})
TESTED_ORCA_VERSION = "1.4.223"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
HOOK_STALE_AFTER_MS = 30 * 60 * 1000
UI_MAX_AGE_SECONDS = 3.0


class OrcaError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class OrcaRPC:
    """Bounded local NDJSON transport, matching Orca's shipped CLI transport.

    Reads the existing user-owned bootstrap token without logging it, changing
    permissions, launching Orca, retrying mutations, or opening a network port.
    """
    METHODS = frozenset({"status.get", "session.tabs.listAll", "terminal.list",
                         "worktree.ps", "worktree.activate"})

    def __init__(self, metadata_path: str | Path | None = None, timeout: float = 3.0):
        if not 0 < timeout <= 30:
            raise ValueError("timeout must be in (0, 30]")
        default = Path(os.environ.get("ORCA_USER_DATA_PATH", str(
            Path.home() / "Library/Application Support/orca"))) / "orca-runtime.json"
        self.metadata_path = Path(metadata_path) if metadata_path else default
        self.timeout = timeout
        self.last_runtime_id = None

    def call(self, method: str, params: dict | None = None) -> dict:
        if method not in self.METHODS:
            raise OrcaError("unsupported_method", "This adapter does not expose that Orca operation.")
        try:
            meta = json.loads(self.metadata_path.read_text())
            transports = meta.get("transports", [meta.get("transport")])
            transport = next(t for t in transports if isinstance(t, dict) and t.get("kind") == "unix")
            endpoint, token = transport["endpoint"], meta["authToken"]
            if not isinstance(endpoint, str) or not isinstance(token, str) or not token:
                raise ValueError()
        except (OSError, ValueError, KeyError, StopIteration, TypeError):
            raise OrcaError("runtime_unavailable", "Orca runtime metadata is missing or invalid.") from None
        request_id = str(uuid.uuid4())
        body = json.dumps({"id": request_id, "authToken": token,
                           "method": method, "params": params or {}}, ensure_ascii=False).encode() + b"\n"
        deadline = time.monotonic() + self.timeout
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(endpoint)
                connection.sendall(body)
                buffer = bytearray()
                total = 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError()
                    connection.settimeout(remaining)
                    chunk = connection.recv(65536)
                    if not chunk:
                        raise OrcaError("runtime_closed", "Orca closed the connection before responding.")
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        raise OrcaError("response_too_large", "Orca response exceeded the adapter size limit.")
                    buffer.extend(chunk)
                    while b"\n" in buffer:
                        line, _, rest = buffer.partition(b"\n")
                        buffer = bytearray(rest)
                        if not line.strip():
                            continue
                        try:
                            frame = json.loads(line)
                        except (ValueError, UnicodeError):
                            raise OrcaError("invalid_response", "Orca returned invalid JSON.") from None
                        if not isinstance(frame, dict):
                            raise OrcaError("invalid_response", "Orca returned an invalid response envelope.")
                        if frame.get("_keepalive") is True:
                            continue  # Absolute deadline remains bounded.
                        if frame.get("id") != request_id:
                            raise OrcaError("invalid_response", "Orca response ID did not match the request.")
                        runtime_id = frame.get("_meta", {}).get("runtimeId")
                        if runtime_id and runtime_id != meta.get("runtimeId"):
                            raise OrcaError("runtime_changed", "Orca restarted during the request.")
                        if frame.get("ok") is not True:
                            # Do not echo unbounded server text or arbitrary payloads.
                            code = frame.get("error", {}).get("code", "runtime_error")
                            raise OrcaError(str(code)[:100], "Orca rejected the requested operation.")
                        if not isinstance(frame.get("result"), dict):
                            raise OrcaError("invalid_response", "Orca returned an invalid result object.")
                        self.last_runtime_id = runtime_id or meta.get("runtimeId")
                        return frame["result"]
        except PermissionError:
            raise OrcaError("runtime_access_denied", "The caller cannot access Orca's local runtime socket.") from None
        except (TimeoutError, socket.timeout):
            raise OrcaError("runtime_timeout", "Timed out waiting for Orca.") from None
        except OSError:
            raise OrcaError("runtime_unavailable", "Could not connect to the running Orca runtime.") from None


class LocalUIFileProvider:
    """Read two alternating complete JSON frames from a trusted renderer bridge.

    The reader only accepts a fully parsed frame. If one file is being replaced,
    the other retains the previous complete frame; consumers reject it after 3s.
    No partially written JSON or per-field merge can become a UI snapshot.
    """
    def __init__(self, prefix: str | Path):
        self.prefix = Path(prefix)

    def __call__(self) -> dict:
        frames = []
        paths = (self.prefix,) if self.prefix.suffix == ".json" else tuple(
            Path(str(self.prefix) + f".{index}.json") for index in (0, 1))
        for path in paths:
            try:
                stat = path.stat()
                if stat.st_uid != os.getuid() or stat.st_size > 1024 * 1024:
                    continue
                value = json.loads(path.read_text())
                if isinstance(value, dict) and _number(value.get("captured_at")):
                    frames.append(value)
            except (OSError, ValueError, UnicodeError):
                continue
        if not frames:
            raise OrcaError("ui_snapshot_unavailable", "No complete renderer UI snapshot is available.")
        return max(frames, key=lambda frame: (frame["captured_at"], frame.get("sequence", 0)))


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# Orca's pi-family hook normalizer publishes these without mainAgent. pi and
# prime-agent share it but also synthesize "done" on session_start, which is
# not a finished turn, so only omp's agent_end is accepted as completion.
HOOK_ONLY_AGENTS = frozenset({"omp"})


def _hook_only_observed_at(status: dict, now: float) -> float | None:
    """Return the hook observation time only if the row proves it came from a hook.

    Without mainAgent, the tab row's own observation is the sole difference
    between a real hook transition and a title-derived synthetic row.
    """
    if status.get("agentType") not in HOOK_ONLY_AGENTS or status.get("mainAgent") is not None:
        return None
    if any(status.get(flag) not in (None, False) for flag in ("restoredUnconfirmed", "providerSessionOnly")):
        return None
    observation = status.get("observation")
    if (not isinstance(observation, dict) or observation.get("origin") != "hook"
            or observation.get("kind") not in ("transition", "snapshot")
            or not isinstance(observation.get("authorityId"), str)
            or not observation["authorityId"].startswith("main-agent-hooks:")
            or any(type(observation.get(key)) is not int or observation[key] < 0
                   for key in ("incarnation", "revision"))):
        return None
    observed = observation.get("observedAt")
    # evidenceObservedAt is Orca's own freshness clock for this row; a
    # mismatch means the observation does not describe the published state.
    if (not _number(observed) or not _number(now) or not 0 < observed <= now
            or status.get("evidenceObservedAt") != observed):
        return None
    return observed


def _hook_only_state(status: dict, now: float) -> int:
    observed = _hook_only_observed_at(status, now)
    state = status.get("state")
    if observed is None or state not in ("working", "waiting", "blocked", "done"):
        return UNKNOWN
    if state == "done":
        # omp agent_end is an observed response end, not a success verdict.
        # Without a turn timestamp a new finish could not be told apart.
        if not _number(status.get("stateStartedAt")):
            return UNKNOWN
        if status.get("interrupted") is True or status.get("sessionBoundary") is True:
            return IDLE
        return COMPLETE  # Like Stop, a finished turn does not expire by age.
    # stateObservedAt stays at the turn start while tool/message hooks keep
    # advancing the evidence clock; Orca itself decays by evidence age.
    if now - observed > HOOK_STALE_AFTER_MS:
        return UNKNOWN
    return RUNNING if state == "working" else WAITING


def state_from_agent_status(status: Any, *, now_ms: float | None = None) -> int:
    """Use explicit hooks only; Orca also publishes title-derived synthetic rows."""
    if not isinstance(status, dict) or status.get("restoredUnconfirmed") is True:
        return UNKNOWN
    if status.get("agentType") in HOOK_ONLY_AGENTS and status.get("mainAgent") is None:
        return _hook_only_state(status, time.time() * 1000 if now_ms is None else now_ms)
    main = status.get("mainAgent")
    if not isinstance(main, dict) or main.get("state") not in ("working", "waiting", "blocked", "done"):
        return UNKNOWN
    state = status.get("state")
    if state not in ("working", "waiting", "blocked", "done"):
        return UNKNOWN
    if state != "done":
        # stateObservedAt is serialized by session.tabs.listAll. A local stamp
        # may be compared only to the local clock; unsupported remote UI rows
        # are rejected before this function is used by OrcaClient.
        observed = status.get("stateObservedAt", status.get("evidenceObservedAt", status.get("updatedAt")))
        if status.get("structuredHostOwned") is not True:
            if not _number(observed):
                return UNKNOWN
            now = time.time() * 1000 if now_ms is None else now_ms
            if observed > now + 5000 or now - observed > HOOK_STALE_AFTER_MS:
                return UNKNOWN
    outcome = main.get("outcome") if main.get("state") == "done" else None
    if outcome == "failure":
        return ERROR
    if state in {"blocked", "waiting"}:
        return WAITING
    if state == "working":
        return RUNNING  # Combined state also includes live child work.
    if outcome == "cancellation" or status.get("interrupted") is True or status.get("sessionBoundary") is True:
        return IDLE
    if outcome == "success":
        return COMPLETE
    if (status.get("agentType") in STOP_HOOK_AGENTS and main.get("state") == "done"
            and "outcome" not in main):
        # Orca's Codex and Claude Code Stop hooks publish a finished lead but do
        # not generate a success verdict. This is an observed response end,
        # not proof that the work was correct. Synthetic title rows lack mainAgent.
        observed = status.get("stateObservedAt", status.get("evidenceObservedAt", status.get("updatedAt")))
        now = time.time() * 1000 if now_ms is None else now_ms
        if _number(observed) and _number(now) and 0 < observed <= now:
            # Like explicit done outcomes, a finished turn does not expire by
            # age alone. A later turn/status or a missing row replaces it.
            return COMPLETE
    return UNKNOWN


def aggregate_states(states: list[int]) -> int:
    """Unknown prevents a partial set of completed agents from appearing done."""
    if not states:
        return IDLE
    for state in (ERROR, WAITING, RUNNING, UNKNOWN, COMPLETE, IDLE):
        if state in states:
            return state
    return UNKNOWN


def _joined_hook_status(workspace: str, tab: dict, rows: list, now_ms: float) -> dict | None:
    """Recover a rich hook only when the current terminal projection agrees.

    Orca may replace a hook with an OSC-title projection, losing mainAgent.
    The hook API attaches today's terminal handle to its pane, so that handle
    alone is insufficient: require the complete live route and contemporaneous
    projected state as well. Never use a title, tab position or workspace path
    to guess a match, and never let an old Stop hide newer activity.
    Hook-only agents (omp) have no mainAgent; their raw row qualifies by its
    hook observation alone. Orca 1.4.241 drops that observation from the done
    projection, so without this join every finished omp turn reads unknown.
    """
    projected = tab.get("agentStatus")
    if (tab.get("type") != "terminal" or tab.get("status") != "ready"
            or not isinstance(projected, dict) or isinstance(projected.get("mainAgent"), dict)):
        return None
    parent, leaf, terminal = tab.get("parentTabId"), tab.get("leafId"), tab.get("terminal")
    if not all(isinstance(value, str) and value for value in (parent, leaf, terminal, tab.get("ptyId"))):
        return None
    pane = parent + ":" + leaf
    route = {"worktreeId": workspace, "terminalHandle": terminal, "tabId": parent, "paneKey": pane}
    if any(projected.get(key) != value for key, value in route.items()):
        return None
    candidates = [row for row in rows if isinstance(row, dict)
                  and all(row.get(key) == value for key, value in route.items())]
    if len(candidates) != 1:
        return None
    raw = candidates[0]
    if any(row.get(flag) not in (None, False) for row in (raw, projected)
           for flag in ("restoredUnconfirmed", "providerSessionOnly")):
        return None
    if raw.get("structuredHost") is not None:
        return None
    main, observation = raw.get("mainAgent"), raw.get("observation")
    hook_only = raw.get("agentType") in HOOK_ONLY_AGENTS and main is None
    if (not (isinstance(main, dict) or hook_only) or not isinstance(observation, dict)
            or observation.get("origin") != "hook"
            or observation.get("kind") not in ("transition", "snapshot")
            or not isinstance(observation.get("authorityId"), str)
            or not observation["authorityId"].startswith("main-agent-hooks:")
            or any(type(observation.get(key)) is not int or observation[key] < 0
                   for key in ("incarnation", "revision"))):
        return None
    if (raw.get("state") not in ("working", "waiting", "blocked", "done")
            or raw.get("state") != projected.get("state")
            or raw.get("agentType") != projected.get("agentType")):
        return None
    observed, received, projected_at = raw.get("evidenceObservedAt"), raw.get("receivedAt"), projected.get("updatedAt")
    if (not all(_number(value) and 0 < value <= now_ms
                for value in (observed, received, projected_at, observation.get("observedAt")))
            or observation["observedAt"] != observed or received < observed
            # A title update is expected just after the matching Stop (25 ms
            # in the verified build). A much later title is not turn evidence.
            or not 0 <= projected_at - observed <= 5000):
        return None
    for key in ("runId", "executionId"):
        if key in projected and projected[key] != raw.get(key):
            return None
    if isinstance(projected.get("observation"), dict):
        other = projected["observation"]
        if any(other.get(key) != observation[key] for key in ("authorityId", "incarnation", "revision")):
            return None
    left, right = raw.get("providerSession"), projected.get("providerSession")
    if isinstance(left, dict) and isinstance(right, dict):
        if any(left.get(key) != right.get(key) for key in ("key", "id")):
            return None
    # Cancellation/reset flags on either lane must survive the merge.
    return {**raw, **{flag: True for flag in ("interrupted", "sessionBoundary")
                     if projected.get(flag) is True}}


def _slot(index: int, identity: str | None = None, label: str = "", state: int = EMPTY, **extra: Any) -> dict:
    return {"index": index, "id": identity, "label": label, "state": state,
            "state_name": STATE_NAMES[state], **extra}


class OrcaClient:
    """Combine actual UI order with explicit runtime agent facts.

    ui_provider returns the live UI snapshot (see cdp_ui.py).
    It is intentionally separate from the runtime inventory: neither the
    backend's worktree sort nor per-worktree activeTabId identifies the current
    application workspace. A select_provider may perform an exact AX/renderer
    selection instead of the local worktree.activate RPC.
    """
    def __init__(self, ui_provider: Callable[[], dict] | None = None,
                 rpc: OrcaRPC | None = None,
                 select_provider: Callable[[dict], dict] | None = None,
                 clock: Callable[[], float] = time.time):
        self.rpc = rpc or OrcaRPC()
        self.ui_provider = ui_provider
        self.select_provider = select_provider
        self.clock = clock

    def inventory(self) -> dict:
        """Diagnostic inventory. Its order MUST NOT be assigned to F keys."""
        status, snapshots = self._read_inventory()
        return self._build_inventory(status, snapshots)

    def _read_inventory(self) -> tuple[dict, list]:
        status = self.rpc.call("status.get")
        if status.get("appVersion") != TESTED_ORCA_VERSION:
            raise OrcaError("unsupported_version", "Orca's internal API needs validation for this app version.")
        if status.get("graphStatus") != "ready" or status.get("desktopWindowStatus") != "available":
            raise OrcaError("renderer_unavailable", "Orca's desktop workspace graph is not ready.")
        data = self.rpc.call("session.tabs.listAll")
        if getattr(self.rpc, "last_runtime_id", status.get("runtimeId")) != status.get("runtimeId"):
            raise OrcaError("runtime_changed", "Orca restarted while the inventory was being read.")
        snapshots = data.get("snapshots")
        if not isinstance(snapshots, list):
            raise OrcaError("invalid_response", "Orca did not return workspace tab snapshots.")
        return status, snapshots

    def _build_inventory(self, status: dict, snapshots: list, hook_rows: list | None = None) -> dict:
        workspaces = {}
        for item in snapshots:
            if not isinstance(item, dict) or not isinstance(item.get("worktree"), str) or not isinstance(item.get("tabs"), list):
                raise OrcaError("invalid_response", "Orca returned an invalid workspace snapshot.")
            facts = []
            for tab in item["tabs"]:
                if not isinstance(tab, dict):
                    raise OrcaError("invalid_response", "Orca returned an invalid tab entry.")
                if tab.get("type") not in {"terminal", "agent", "agent-session"}:
                    continue
                agent = tab.get("agentStatus")
                joined = _joined_hook_status(item["worktree"], tab, hook_rows or [], self.clock() * 1000)
                if joined is not None:
                    agent = joined
                now_ms = self.clock() * 1000
                state = state_from_agent_status(agent, now_ms=now_ms)
                main = agent.get("mainAgent") if isinstance(agent, dict) else None
                outcome = main.get("outcome") if isinstance(main, dict) else None
                hook_only = isinstance(agent, dict) and _hook_only_observed_at(agent, now_ms) is not None
                completion_source = completed_at = None
                if state == COMPLETE and isinstance(main, dict):
                    completion_source = "provider_success" if outcome == "success" else f"{agent['agentType']}_stop"
                    completed_at = main.get("stateStartedAt", agent.get("stateStartedAt"))
                elif state == COMPLETE and hook_only:
                    # Each agent_end enters done anew, so its start keys the turn.
                    completion_source = f"{agent['agentType']}_agent_end"
                    turn_end = agent.get("turnCompletedAt")
                    completed_at = turn_end if _number(turn_end) and turn_end > 0 else agent["stateStartedAt"]
                facts.append({"tab_id": tab.get("id"), "terminal": tab.get("terminal"),
                              "state": state, "state_name": STATE_NAMES[state],
                              "hook_observed": isinstance(main, dict) or hook_only, "outcome": outcome,
                              "status_source": "raw_hook" if joined is not None else "tab_projection",
                              "completion_source": completion_source, "completed_at": completed_at,
                              "outcome_verified": isinstance(outcome, str) and outcome in {"success", "failure", "cancellation"}})
            state = aggregate_states([fact["state"] for fact in facts])
            sources = {fact["completion_source"] for fact in facts if fact["completion_source"] is not None}
            completion_source = (next(iter(sources)) if len(sources) == 1 else "mixed") if state == COMPLETE else None
            # Changes whenever any agent here finishes a new turn.
            completion_key = ("|".join(sorted(f"{f['tab_id']}@{f['completed_at']}" for f in facts if f["state"] == COMPLETE))
                              if state == COMPLETE else None)
            workspaces[item["worktree"]] = {"id": item["worktree"], "state": state,
                                           "agents": facts, "publication_epoch": item.get("publicationEpoch"),
                                           "completion_source": completion_source,
                                           "completion_key": completion_key,
                                           "outcome_verified": bool(facts) and all(f["outcome_verified"] for f in facts)}
        return {"runtime_id": status.get("runtimeId"), "app_version": status.get("appVersion"),
                "window_id": status.get("authoritativeWindowId"), "workspaces": workspaces}

    def _read_ui(self, inventory: dict) -> dict:
        if self.ui_provider is None:
            raise OrcaError("ui_order_unavailable", "A live renderer or verified accessibility bridge is required for Cmd+number order.")
        ui = self.ui_provider()
        if not isinstance(ui, dict) or ui.get("source") not in {"orca-renderer", "orca-accessibility"}:
            raise OrcaError("invalid_ui_snapshot", "UI order must come from Orca's actual current interface.")
        captured = ui.get("captured_at")
        if not _number(captured) or not 0 <= self.clock() - captured <= UI_MAX_AGE_SECONDS:
            raise OrcaError("stale_ui_snapshot", "Orca UI snapshot is missing or stale.")
        if ui.get("runtime_id") != inventory["runtime_id"]:
            raise OrcaError("runtime_changed", "UI and runtime snapshots belong to different Orca runs.")
        if ui.get("window_id") is None or ui.get("window_id") != inventory["window_id"]:
            raise OrcaError("window_mismatch", "The snapshot does not identify Orca's authoritative window.")
        rows = ui.get("slots")
        if not isinstance(rows, list) or len(rows) > 10000:
            raise OrcaError("invalid_ui_snapshot", "UI snapshot has no valid ordered slot list.")
        identities = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                raise OrcaError("invalid_ui_snapshot", "A UI row has no stable workspace ID.")
            if row.get("execution_host_id", "local") not in {None, "local"}:
                raise OrcaError("remote_workspace_unsupported", "This adapter supports the local Orca host only.")
            if row["id"] in identities:
                raise OrcaError("ambiguous_ui_snapshot", "A workspace appears more than once in the numbered UI order.")
            identities.add(row["id"])
        if ui.get("selected_id") is not None and not isinstance(ui.get("selected_id"), str):
            raise OrcaError("invalid_ui_snapshot", "Current workspace ID is invalid.")
        return ui

    def snapshot(self) -> dict:
        empty = [_slot(i) for i in range(1, 13)]
        try:
            status, snapshots = self._read_inventory()
            inventory = self._build_inventory(status, snapshots)
        except OrcaError as error:
            return {"supported": False, "app_running": None, "slots": empty,
                    "selected_id": None, "window_id": None, "reason": error.code, "message": str(error)}
        try:
            ui = self._read_ui(inventory)
            hook_rows = ui.get("hook_statuses", []) if ui.get("source") == "orca-renderer" else []
            if not isinstance(hook_rows, list) or len(hook_rows) > 10000:
                raise OrcaError("invalid_ui_snapshot", "Orca returned an invalid hook status snapshot.")
            if hook_rows:
                inventory = self._build_inventory(status, snapshots, hook_rows)
        except OrcaError as error:
            return {"supported": False, "app_running": True, "slots": empty, "selected_id": None,
                    "window_id": inventory["window_id"], "runtime_id": inventory["runtime_id"],
                    "reason": error.code, "message": str(error), "diagnostics": inventory}
        slots = []
        for index, row in enumerate(ui["slots"][:12], 1):
            fact = inventory["workspaces"].get(row["id"])
            # A real sidebar workspace can have no tab snapshot yet; that is
            # unknown, and navigation is not allowed to create a new terminal.
            state = fact["state"] if fact else UNKNOWN
            navigation_reason = (
                "target_unavailable" if fact is None else
                "folder_activation_unsupported" if row["id"].startswith("folder:") and self.select_provider is None else
                None)
            slots.append(_slot(index, row["id"], str(row.get("label", row["id"]))[:512], state,
                               execution_host_id=row.get("execution_host_id") or "local",
                               selectable=navigation_reason is None,
                               navigation_reason=navigation_reason,
                               completion_source=fact.get("completion_source") if fact else None,
                               completion_key=fact.get("completion_key") if fact else None,
                               outcome_verified=fact.get("outcome_verified", False) if fact else False))
        slots.extend(empty[len(slots):])
        return {"supported": True, "app_running": True, "slots": slots,
                "selected_id": ui.get("selected_id") if ui.get("active_view") == "terminal" and not ui.get("modal_open") and ui.get("selected_host_id", "local") in {None, "local"} else None,
                "window_id": ui["window_id"], "runtime_id": inventory["runtime_id"],
                "active_view": ui.get("active_view"), "captured_at": ui["captured_at"],
                "source": ui["source"], "total_slots": len(ui["slots"])}

    def select(self, target_id: str,
               before_dispatch: Callable[[dict], None] | None = None) -> dict:
        """Navigate one existing local workspace; never type into a terminal."""
        current = self.snapshot()
        if not current.get("supported"):
            raise OrcaError(current["reason"], current.get("message", "Orca UI is unavailable."))
        target = next((row for row in current["slots"] if row["id"] == target_id), None)
        if target is None:
            raise OrcaError("target_unavailable", "The requested workspace is no longer in the assigned slots.")
        if not target.get("selectable"):
            raise OrcaError(target.get("navigation_reason") or "target_unavailable",
                            "The requested workspace has no verified navigation provider.")
        if before_dispatch is not None:
            # A caller may reject a stale key event or changed foreground
            # session after the potentially blocking snapshot reads finish.
            before_dispatch(current)
        if self.select_provider:
            result = self.select_provider({**target, "window_id": current["window_id"]})
        else:
            # Internal RPC is version-checked above; host avoids steering paired
            # clients. No shell startup, prompt, terminal creation or retry.
            result = self.rpc.call("worktree.activate", {"worktree": "id:" + target_id,
                                                         "navigation": "host", "notifyClients": False})
        if not self.select_provider and (result.get("activated") is not True or result.get("worktreeId") != target_id):
            raise OrcaError("activation_unconfirmed", "Orca did not accept the requested workspace activation.")
        return {"accepted": True, "target_id": target_id, "window_id": current["window_id"],
                "selection_verified": False, "result": result}
