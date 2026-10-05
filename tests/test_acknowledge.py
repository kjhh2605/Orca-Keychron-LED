import tempfile
import unittest
from pathlib import Path

from orca_led.acknowledge import COMPLETE_SEEN, CompletionTracker
from orca_led.orca import COMPLETE, RUNNING


def snapshot(*slots, selected=None):
    rows = [{"index": i, "id": identity, "state": state, "completion_key": key}
            for i, (identity, state, key) in enumerate(slots, 1)]
    return {"supported": True, "selected_id": selected, "slots": rows}


class CompletionTrackerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "completion-seen.json"
        self.tracker = CompletionTracker(self.path)

    def states(self, value):
        return [row["state"] for row in value["slots"]]

    def test_completion_stays_unseen_until_viewed_in_focused_orca(self):
        value = snapshot(("a", COMPLETE, "t@1"), ("b", COMPLETE, "t@2"), selected="a")
        self.assertEqual(self.states(self.tracker.apply(value, orca_focused=False)), [COMPLETE, COMPLETE])
        self.assertEqual(self.states(self.tracker.apply(value, orca_focused=True)), [COMPLETE_SEEN, COMPLETE])
        # Moving away keeps the acknowledgement for that completion.
        moved = snapshot(("a", COMPLETE, "t@1"), ("b", COMPLETE, "t@2"), selected=None)
        self.assertEqual(self.states(self.tracker.apply(moved, orca_focused=True)), [COMPLETE_SEEN, COMPLETE])

    def test_new_turn_blinks_again_and_running_is_untouched(self):
        self.tracker.apply(snapshot(("a", COMPLETE, "t@1"), selected="a"), orca_focused=True)
        self.assertEqual(self.states(self.tracker.apply(snapshot(("a", RUNNING, None)), True)), [RUNNING])
        self.assertEqual(self.states(self.tracker.apply(snapshot(("a", COMPLETE, "t@1")), True)), [COMPLETE_SEEN])
        self.assertEqual(self.states(self.tracker.apply(snapshot(("a", COMPLETE, "t@9")), True)), [COMPLETE])

    def test_acknowledgements_persist_and_forget_unassigned_workspaces(self):
        self.tracker.apply(snapshot(("a", COMPLETE, "t@1"), selected="a"), orca_focused=True)
        restarted = CompletionTracker(self.path)
        self.assertEqual(self.states(restarted.apply(snapshot(("a", COMPLETE, "t@1")), False)), [COMPLETE_SEEN])
        restarted.apply(snapshot(("b", COMPLETE, "t@2")), False)
        self.assertEqual(CompletionTracker(self.path).seen, {})

    def test_unsupported_snapshot_and_corrupt_file_are_harmless(self):
        self.path.write_text("not json")
        tracker = CompletionTracker(self.path)
        value = {"supported": False, "slots": []}
        self.assertIs(tracker.apply(value, True), value)


if __name__ == "__main__":
    unittest.main()
