"""The card details pane: its data and its lines, without a terminal."""
import unittest

from herdboard import activity
from support.herdboard_details import StoreCase


class DetailsAppTests(StoreCase, unittest.IsolatedAsyncioTestCase):
    async def test_draws_the_card_and_redraws_after_a_log_entry(self):
        from textual.widgets import Static

        from herdboard.details_view import DetailsApp
        ref = self.card.ref
        app = DetailsApp(self.store, self.config, ref, check_seconds=0.05)
        async with app.run_test(size=(60, 8)) as pilot:
            await pilot.pause()
            box = app.query_one("#details", Static)
            # Herdr draws the pane border and its `details - <ref>` label, so the view draws none.
            self.assertFalse(box.border_title)
            self.assertEqual(box.styles.border_top[0], "")
            self.assertEqual(box.content_size.width, 58, "one column of padding at each side")
            shown = str(box.render())
            self.assertIn("Fix login timeout", shown)
            self.assertIn(f"no start record for {ref}", shown)
            self.assertIn("no log entries", shown)
            activity.append(self.store, ref, "Moved to Review.", "claude")
            await pilot.pause(0.2)
            self.assertIn("Moved to Review.", str(box.render()))


if __name__ == "__main__":
    unittest.main()
