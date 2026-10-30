from support import linear_data
from herdboard.config import Config
from herdboard.linear import LinearClient
from herdboard.linear_writes import LinearWriter
from support.herdboard_linear_source import LinearEnvTestCase


class WriterCase(LinearEnvTestCase):
    def setUp(self):
        super().setUp()
        linear_data.with_workflow(self.fixtures)

    timeout = 2.0

    def build_client(self, key):
        return LinearClient(key, endpoint=self.endpoint, timeout=self.timeout, sleep=lambda _: None)

    def slow_mutation(self, failure="timeout_after"):
        """Make the next write answer after the client gave up. A first request builds the
        schema, so it runs before the client timeout gets short."""
        self.writer().client().viewer()
        self.timeout = 0.3
        self.fake.mutation_failure, self.fake.mutation_delay = failure, 0.8

    def writer(self, **kwargs):
        self.source_in_use = self.source(**kwargs)
        writer = LinearWriter(Config(todos_dir=self.root / "todos"), self.source_in_use)
        writer.wait = lambda _: None
        return writer

    def issue(self, identifier):
        return next(i for i in self.fixtures["issues"] if i["identifier"] == identifier)

    def inputs(self, operation):
        return [r["variables"]["input"] for r in self.requests(operation)]

    def choices_file(self):
        return self.state / "herdboard" / "o1" / "u1" / "state-choices.json"
