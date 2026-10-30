from support import linear_data
from support.herdboard_linear_cli import LinearCliCase


class WriteCliCase(LinearCliCase):
    def setUp(self):
        super().setUp()
        linear_data.with_workflow(self.fixtures)

    def issue(self, identifier):
        return next(i for i in self.fixtures["issues"] if i["identifier"] == identifier)

    def inputs(self, operation):
        return [r["variables"]["input"] for r in self.requests(operation)]

    def error(self, *args, expect=1, directory=None):
        return self.run_json(*args, directory=directory or self.repo, expect=expect)["error"]
