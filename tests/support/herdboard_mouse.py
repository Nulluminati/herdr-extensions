from herdboard.board import CardWidget
from support.herdboard_board import BoardTestCase


def content_offset(widget, x=0, y=0):
    """An offset inside the widget's content, past its border and padding."""
    return (widget.content_region.x - widget.region.x + x, widget.content_region.y - widget.region.y + y)


class MouseTestCase(BoardTestCase):
    def setUp(self):
        super().setUp()
        self.first = self.store.add(self.repo_path, "first todo", "Todo")
        self.second = self.store.add(self.repo_path, "second todo", "Todo")
        self.waiting = self.store.add(self.repo_path, "waiting card", "Review")

    def widget(self, app, ref):
        return next(w for w in app.query(CardWidget) if w.card.ref == ref)

    def column(self, app, name):
        return next(w for w in app.query(".column") if w.column_name == name)

    def column_of(self, ref):
        return {card.ref: card.column for card in self.store.list_cards([self.repo_path])}[ref]

    def titles(self, column):
        return [card.title for card in self.store.list_cards([self.repo_path]) if card.column == column]
