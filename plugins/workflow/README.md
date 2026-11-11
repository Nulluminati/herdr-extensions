# Workflow

Open [herdr-plus](https://github.com/cloudmanic/herdr-plus) workflow workspaces for a Git repository, and show each workflow's project and profile in the Herdr sidebar.

A workflow is a herdr-plus project template, such as `implementation` or `pr-review`. The launcher opens the template for one repository and labels the workspace `<profile> · <path>`, for example `implementation · ~/Projects/herdr-extensions`. If a workspace with that label is open, the launcher focuses it.

The plugin writes two workspace tokens:

| Token | Example | Value |
|---|---|---|
| `$workflow_project` | `herdr-extensions` | The repository's directory name |
| `$workflow_profile` | `implementation` | The workflow profile |

## Install

Requirements: Herdr 0.9 or later, Python 3.11 or later, herdr-plus, Git and zoxide. The review workflows also need `hunk`, and `pr-review` needs `gh`.

```bash
herdr plugin install Nulluminati/herdr-extensions/plugins/workflow
```

To work on the plugin from a clone, link it instead, so edits take effect at once:

```bash
herdr plugin link ~/Projects/herdr-extensions/plugins/workflow
```

## Configure

The templates and prompts are herdr-plus config. The launcher reads them from the directory that `herdr plugin config-dir cloudmanic.herdr-plus` gives:

- `projects/<profile>.toml`: one herdr-plus project template for each profile. The launcher replaces its `name` and `working_dir`.
- `prompts/review.md`, `prompts/local-review.md` and `prompts/pr-review.md`: the system prompt of the review agents.

The profiles are `implementation`, `top-level`, `local-review` and `pr-review`. Only these get sidebar tokens.

herdr-plus quick actions and template panes run the launcher by its path in the plugin. For a linked clone, that is `~/Projects/herdr-extensions/plugins/workflow/bin/herdr_workflow.py`. Run `herdr plugin list --plugin nulluminati.workflow --json` to find the plugin root of an installed copy. A quick action that opens a workflow for a recent repository:

```toml
name = "Implementation in a repository"
type = "select"
options_command = 'uv run --no-project --script "$HOME/Projects/herdr-extensions/plugins/workflow/bin/herdr_workflow.py" list-repositories'
command = 'uv run --no-project --script "$HOME/Projects/herdr-extensions/plugins/workflow/bin/herdr_workflow.py" open implementation'
```

The commands of the launcher:

| Command | What it does |
|---|---|
| `open <profile> [directory]` | Open or focus the workflow for the repository that holds the directory. Without a directory, it uses the directory the quick action ran from. |
| `list-repositories` | List the current repository and the repositories in zoxide's history, for a select quick action. |
| `create-project` | Ask for a new directory, run `git init` in it, and open the `implementation` workflow. |
| `refresh-sidebar` | Report the sidebar tokens of every open workflow workspace again. |
| `review-agent <profile> <agent>` | Start `pi` or `claude` with the review prompts. For a template pane. |
| `review-hunk <profile> [reference]` | Start `hunk` on the working tree, or on a GitHub pull request. For a template pane. |
| `layout-claude <profile>` | Stack the Claude pane below the first pane, then start Claude. For a template pane. |

To show the tokens, add them to a sidebar row in `~/.config/herdr/config.toml`. Then run `herdr server reload-config`.

## When the tokens change

- When the launcher opens or focuses a workflow, for that workspace.
- At Herdr startup, for every workflow workspace. Herdr keeps tokens in memory only, so restored workspaces lose them.
- When you run the action "Workflow: refresh the sidebar", for every workflow workspace.

## Limits

- The launcher finds workflow workspaces by their label. If you rename a workspace, it loses its tokens at the next refresh.
- Pull request review supports github.com only, not GitHub Enterprise.
