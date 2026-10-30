


def content_offset(widget, x=0, y=0):
    """An offset inside the widget's content, past its border and padding."""
    return (widget.content_region.x - widget.region.x + x, widget.content_region.y - widget.region.y + y)
