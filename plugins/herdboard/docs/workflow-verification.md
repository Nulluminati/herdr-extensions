# Workflow verification

A factory run approves work only when current evidence shows each acceptance criterion of the plan. This page defines the plan's verification contract, the builder's evidence report and the approver's structured approval. It also explains the four workflows, the limits of the check, and how runs from before this policy behave.

## Where the practices come from

herdboard adapts a few practices from two public skill collections. It does not install them, depend on them or update from them. The prompts carry the practices as herdboard text.

Sources read on 2026-11-06:

- [mattpocock/skills](https://github.com/mattpocock/skills) at commit `4ae2317ddb22b046e26455e4f9c9b3aaaf62212d` (2026-10-31): `skills/productivity/grilling`, `skills/engineering/tdd` and `skills/engineering/code-review`.
- [cursor/plugins pstack](https://github.com/cursor/plugins/tree/main/pstack) at commit `c742dd61706fbacc1b95f1a20b7b988cf096ce6c` (2026-10-27, the last change under `pstack/`): `skills/create-verification-skill` and `skills/poteto-mode/playbooks/bug-fix.md`.

| Practice | Source | Where herdboard uses it |
|---|---|---|
| Find facts yourself; ask the user only for decisions | grilling | Planner brief, How to plan step 1 |
| Ask the questions whose prerequisites have answers, in numbered rounds with a recommendation; ask dependent questions later | grilling | Planner brief, step 3 |
| Agree on the test boundaries (public interfaces) before the tests; one small change per step | tdd | Planner brief, step 4 |
| Keep requirements and code standards apart; do not merge their findings | code-review | Lens reviewers check code; the approver checks requirements and evidence |
| Name the launch, isolation, actions, observations, evidence and cleanup of a check from the real repository | create-verification-skill | The contract's check fields |
| Reproduce a defect, then check the fix on the same surface; a unit test alone does not prove that the defect is gone | bug-fix playbook | The defect workflow |
| Keep the proof after cleanup; clean up only what you started | create-verification-skill | Builder evidence rules |

herdboard does not adopt these upstream practices: automatic merging, overnight runs that land work without you, removal of comments, rewrites that nobody asked for, required models, sub-agent fan-out, and any rule that lets an agent skip your decisions. You still merge each card yourself.

## The contract

The plan holds one fenced block labeled `herdboard-verification`, usually under Testing, below the acceptance criteria:

````markdown
## Testing

- AC1: A saved draft opens again after a restart.
- AC2: A draft larger than 1 MB shows an error and keeps the old draft.

```herdboard-verification
{
  "version": 1,
  "workflow": "feature",
  "criteria": [
    {"id": "AC1", "outcome": "A saved draft opens again after a restart."},
    {"id": "AC2", "outcome": "A draft larger than 1 MB shows an error and keeps the old draft."}
  ],
  "checks": [
    {
      "id": "unit",
      "criteria": ["AC1", "AC2"],
      "action": "uv run pytest tests/test_drafts.py",
      "expected": "Exit 0, and test_draft_survives_restart and test_large_draft_is_refused pass.",
      "surface": "unit tests",
      "evidence_kind": "command_output",
      "isolation": "Test temporary directories.",
      "cleanup": "Fixture teardown."
    },
    {
      "id": "live",
      "criteria": ["AC1"],
      "action": "Start the app with a scratch profile, save a draft, quit, start again.",
      "expected": "The draft text is in the editor after the restart.",
      "surface": "the real app in a terminal pane",
      "evidence_kind": "transcript",
      "isolation": "A scratch profile folder and a new pane.",
      "cleanup": "Close the pane that the check opened. Keep the transcript."
    }
  ]
}
```
````

The fence may be indented, for example in a list item. A plan has exactly one such block.

| Field | Meaning |
|---|---|
| `version` | `1`. |
| `workflow` | `feature`, `defect`, `refactor` or `performance`. See [Workflows](#workflows). |
| `criteria[].id` | A short identifier, such as `AC1`. Unique. |
| `criteria[].outcome` | A result that a person or a test can see. Not a task. |
| `checks[].id` | A short identifier, such as `unit`. Unique. |
| `checks[].criteria` | The criteria that the check proves. Each criterion needs at least one check. |
| `checks[].action` | The command or the steps. herdboard never runs it: it is text for the builder. |
| `checks[].expected` | What the result must show. For a command, more than the exit code. |
| `checks[].surface` | Where the check observes the behavior: unit tests, the real CLI, the board, a page. |
| `checks[].evidence_kind` | `command_output`, `transcript`, `screenshot`, `file` or `measurement`. |
| `checks[].isolation` | How the check keeps away from your real data: temporary folders, scratch config, new panes. |
| `checks[].cleanup` | What the check removes afterwards. The evidence stays. |

Unknown fields, duplicate identifiers, a criterion without a check, an unknown workflow or version, two blocks and malformed JSON are errors. The error names the field.

## Workflows

The planner writes the plan for one workflow. herdboard does not guess it.

- **feature**: the observable behavior, acceptance examples, and the real user path to verify. Example check: drive the board in a scratch todos folder and save a transcript.
- **defect**: the reproduction, the failure it shows now, how to find the root cause, the regression test, and a check of the fix on the same surface as the reproduction. Keep the failing output from before the fix as an extra evidence file.
- **refactor**: the behavior to keep, the boundaries that the change touches, and checks that show the same behavior before and after. Example: the same command output before and after, saved as two files.
- **performance**: the metric, a repeatable measurement recipe, the baseline, the target, repeated measurements, and the correctness checks. Use `evidence_kind: "measurement"`. herdboard does not optimize or benchmark anything itself.

## Starting a run

Every new factory run needs a valid contract: Default, a named preset, Custom, and automation runs. A preset or the Custom form chooses the agents, the lenses and the rounds. No preset can turn the contract off.

- The CLI fails with `verification_plan_required` (no plan, or no block) or `verification_plan_invalid` (the field that is wrong). Nothing starts: no worktree, no agent, no worker.
- On the board, Enter in the `F` popup, or a start from the Custom form, shows the error in the dialog. The dialog stays open. Press escape, then `P` to update the plan. The Custom form does not save a preset while the plan is invalid.
- A factory automation logs `Auto start failed: ...` on the spawned card and records `start_failed: ...` as its last result.

A plain `start` without `--factory` needs no contract.

The run keeps the contract, a digest of the plan, and a copy of the plan (`plan.md` in the run folder). The run settings in `run` keep their format: the contract is not part of a preset.

## The builder's evidence

The builder's brief has a Verification evidence section with the run's evidence folder, its run identifier and the contract digest. Before each move to Review, the builder:

1. Commits all its work. The worktree must be clean, with no untracked files.
2. Runs every check of the contract on the final commit.
3. Saves the real output of each check in the evidence folder, with secrets removed.
4. Writes `verification.json` in the evidence folder.

```json
{
  "version": 1,
  "run": "t3m9-20261106-101500",
  "contract_digest": "sha256:…",
  "commit": "<full output of git rev-parse HEAD>",
  "results": [
    {
      "check": "unit",
      "status": "passed",
      "action": "uv run pytest tests/test_drafts.py",
      "observed": "14 passed; test_draft_survives_restart and test_large_draft_is_refused pass.",
      "started": "2026-11-06T10:20:00+00:00",
      "finished": "2026-11-06T10:20:41+00:00",
      "exit_code": 0,
      "evidence": ["unit.txt"]
    },
    {
      "check": "live",
      "status": "blocked",
      "action": "Start the app with a scratch profile.",
      "observed": "No display is available in this session.",
      "started": "2026-11-06T10:21:00+00:00",
      "finished": "2026-11-06T10:21:05+00:00",
      "evidence": []
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `run`, `contract_digest` | From the brief. A report for another run or contract does not count. |
| `commit` | The full commit identifier of the checked tree. It must be the current HEAD. |
| `status` | `passed`, `failed`, `blocked` or `not_applicable`. |
| `action`, `observed` | What the builder did and what it saw. Both must be text. |
| `started`, `finished` | ISO 8601 times with a time zone. |
| `exit_code` | Required for a `command_output` check that passed or failed. A pass needs `0`. |
| `evidence` | Files in the evidence folder. A pass needs at least one non-empty file. |

herdboard reads only bounded files inside the evidence folder. It refuses a report that links outside the folder. As evidence, it refuses absolute paths, `..`, links that leave the folder, names that are not valid file names, missing or empty files, and the report itself. It reads at most 256 KB of report, 4 MB per evidence file and 32 MB in all.

Each round's prompt asks the builder to run the checks again after its fixes. Earlier output, such as a failing test before the fix, can stay as an extra file. It does not replace the final result.

The builder cannot change the contract or exempt a check. For a check that cannot run, it writes `blocked` and explains why.

## The approval

Before the approver starts, herdboard checks the report against the contract, the current HEAD, a clean tree (tracked and untracked files) and the plan digest. It copies the report and its files to `approver/evidence/` and writes its findings to `approver/validation.md`. The approver reads both.

The lens reviewers check the code. The approver checks the requirements and the evidence. It writes `verdict.md` (with an Acceptance table: criterion, check, evidence, outcome) and `approval.json`:

```json
{
  "version": 1,
  "verdict": "approve",
  "criteria": [
    {"id": "AC1", "outcome": "satisfied", "checks": ["unit"], "evidence": "unit.txt"},
    {"id": "AC2", "outcome": "satisfied", "checks": ["unit"], "evidence": "unit.txt"}
  ],
  "exceptions": [
    {"check": "live", "reason": "No display in this session.", "risk": "The restart is not checked in the real app."}
  ]
}
```

After the approver ends, herdboard checks again: the same HEAD, a clean tree, unchanged evidence files (by digest) in both the evidence folder and the approver's copy, and an unchanged plan. Then it decides the effective verdict. Any of these makes it `changes needed`:

- No report, a malformed report, or a report for another run, contract or commit.
- Uncommitted or untracked changes.
- A missing result, an unknown check, a pass without proof, or a pass with a non-zero exit code.
- A failed required check. An exception cannot excuse it.
- A blocked or inapplicable check without a valid exception.
- No `approval.json`, a malformed one, or one that disagrees with `verdict.md`.
- A criterion without an outcome, or one that the approver found unsatisfied. An exception does not satisfy a criterion.
- A commit, a tree change or an evidence change during the approval.
- A plan that changed after the run started. Restart the factory with the new plan.

An exception names one blocked or inapplicable check, a reason and the remaining risk. herdboard refuses an exception for an unknown check, a passed or failed check, a second exception for the same check, and a blanket exception such as `"*"`.

herdboard never raises a verdict. When the approver writes `changes needed`, the result is `changes needed`.

The card gets `factory-verdict.md`. Its first line is the effective verdict. An Evidence gate section lists the reasons when herdboard changed the verdict, and the exceptions that stand. The approver's own verdict follows. The log says `Factory done: approve.`, `Factory done: changes needed.`, or `Factory done: changes needed. The evidence gate found N problems: see factory-verdict.md.`

Every completed assessment moves the card to Review, also `changes needed`. Review means that the work is ready for you to inspect, not that it is approved. herdboard never merges: you merge with `m` as before.

A run whose branch has no change still gets the evidence check and an approver verdict. It starts no lens reviewer.

A run without the code lens (a preset or Custom run with `lenses_on = ["security"]`, for example) still gets the evidence gate. The approver does not replace a code review.

## Reviewers

Each review request now says:

- A confirmed problem cites `path:line` and the observable failure or the concrete code path that shows it. Static evidence is enough.
- A concern that the reviewer cannot prove goes under Risks and questions.
- What the reviewer could not check goes under a Verification limits section. A limit is not a finding, so it does not start another round, and it is not a successful check.

The finding headings, severities and counts stay the same. Reviewers still do not repeat a finding that the builder disagreed with, unless they have new evidence. The builder's answers name the evidence for each fix and the reason for each disagreement.

## Runs from before this policy

A factory run that was active before this change keeps its old approval: no contract, no evidence gate, and the old no-change shortcut. A start record without `verification` in its factory state is such a run. Every new run uses the policy, also on a card or worktree that had an old run.

To move an old plan to the policy, plan the card again with `P`. The planner adds acceptance criteria and the contract. Then start the factory again.

## Claude, pi and Codex

The builder, reviewer and approver prompts are the same text for the three agents. The evidence folder is inside the factory run folder, which each builder can already write (Claude and Codex with `--add-dir`; pi has no sandbox in a builder session). The approver's sandbox writes only its own folder, where `verdict.md` and `approval.json` go. Reviewers and the approver still may not run the project's tests: the builder runs the checks, and the approver judges the evidence.

## Limits

This checks that the evidence is complete, current and consistent. It does not prove that an agent ran a command. An agent with your permissions can write any file, including a false report. Read the evidence of a change that matters.

herdboard does not run checks, infer the workflow, or measure performance. The contract's commands are data.
