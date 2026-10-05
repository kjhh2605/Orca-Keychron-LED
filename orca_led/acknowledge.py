"""Remember which completed workspaces the user has already looked at."""
import json
from pathlib import Path

from .orca import COMPLETE

COMPLETE_SEEN = 7
STATE_NAME = "complete_seen"


class CompletionTracker:
    """Turn COMPLETE into COMPLETE_SEEN once its workspace was shown in Orca.

    A completion is identified by its workspace and `completion_key`, which
    changes whenever an agent in that workspace finishes a new turn. Viewing
    means the workspace is Orca's selected terminal view while Orca is the
    frontmost, unminimized application.
    """
    def __init__(self, path):
        self.path = Path(path)
        try:
            value = json.loads(self.path.read_text())
            self.seen = {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}
        except (OSError, ValueError, AttributeError):
            self.seen = {}

    def apply(self, snapshot, orca_focused):
        if not snapshot.get("supported"):
            return snapshot
        selected = snapshot.get("selected_id") if orca_focused else None
        before = dict(self.seen)
        slots = []
        for slot in snapshot.get("slots", []):
            identity, key = slot.get("id"), slot.get("completion_key")
            if slot.get("state") == COMPLETE and identity and key:
                if identity == selected:
                    self.seen[identity] = key
                if self.seen.get(identity) == key:
                    slot = {**slot, "state": COMPLETE_SEEN, "state_name": STATE_NAME}
            slots.append(slot)
        # Forget workspaces that left the F-key row; others keep their last key
        # so a brief non-complete reading does not make an old result blink.
        assigned = {slot.get("id") for slot in slots}
        self.seen = {k: v for k, v in self.seen.items() if k in assigned}
        if self.seen != before:
            self._save()
        return {**snapshot, "slots": slots}

    def _save(self):
        from .controller import atomic_json
        try:
            atomic_json(self.path, self.seen)
        except OSError:
            pass  # Acknowledgement is a convenience; losing it only re-blinks.
