from support.herdboard_merge import GitHerdr


class ReviewerHerdr(GitHerdr):
    """GitHerdr with pane splits, a reviewer agent whose status follows a script, and notifications."""

    def __init__(self, root):
        super().__init__(root)
        self.reviewer_states = ["working", "idle"]
        self.findings = "## Confirmed problems\n- [high] a.py:1: bug. Fix it.\n- [low] a.py:2: style.\n"
        self.review_folder = None
        self.reviewer_pane = None
        # The file that the agent in the split writes when it turns idle.
        self.output = "findings.md"

    def __call__(self, argv, timeout=120):
        args = argv[1:]
        operation = " ".join(args[:2])
        if operation == "pane split":
            self.calls.append(args)
            workspace = args[2].split(":")[0]
            pane = f"{workspace}:p{self.next_pane}"
            self.next_pane += 1
            self.panes.setdefault(workspace, [workspace + ":p1"]).append(pane)
            self.reviewer_pane = pane
            return self.reply(argv, {"pane": {"pane_id": pane}})
        if operation == "pane close":
            self.calls.append(args)
            for panes in self.panes.values():
                if args[2] in panes:
                    panes.remove(args[2])
            return self.reply(argv, {"type": "ok"})
        if operation in ("notification show", "agent wait"):
            self.calls.append(args)
            return self.reply(argv, {"type": "ok"})
        if operation == "agent start" and args[2].endswith("-review"):
            self.calls.append(args)
            return self.reply(argv, {"type": "ok"})
        if operation == "agent get" and (args[2] == self.reviewer_pane or args[2].endswith("-review")):
            self.calls.append(args)
            state = self.reviewer_states.pop(0) if self.reviewer_states else "idle"
            if state is None:
                return self.reply(argv, error="agent_not_found")
            if state == "idle" and self.findings is not None and self.review_folder:
                (self.review_folder / self.output).write_text(self.findings)
            return self.reply(argv, {"agent": {"agent_status": state, "pane_id": args[2]}})
        return super().__call__(argv, timeout)
