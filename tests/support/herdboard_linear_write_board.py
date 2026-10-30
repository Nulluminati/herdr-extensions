from support import linear_data
from herdboard.board import StateScreen
from support.herdboard_linear_board import LinearBoardCase


class LinearWriteBoardCase(LinearBoardCase):
    def setUp(self):
        super().setUp()
        linear_data.with_workflow(self.fixtures)

    def issue(self, identifier):
        return next(i for i in self.fixtures["issues"] if i["identifier"] == identifier)

    def column_of(self, app, ref):
        return next(d.name for d in app.columns for c in d.cards if c.ref == ref)

    async def wait_for_dialog(self, pilot):
        for _ in range(100):
            await pilot.pause(0.05)
            if isinstance(pilot.app.screen, StateScreen):
                return pilot.app.screen
        self.fail("The state dialog did not open")
