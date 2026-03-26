<p align="center">
  <img src="assets/repo_logo.png" alt="Slack Claude Code Bot" width="1000">
</p>

<p align="center">
  <a href="https://pypi.org/project/slack-claude-code/"><img src="https://img.shields.io/pypi/v/slack-claude-code" alt="PyPI version"></a>
  <a href="https://pypi.org/project/slack-claude-code/"><img src="https://img.shields.io/pypi/pyversions/slack-claude-code" alt="Python versions"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <a href="https://github.com/djkelleher/slack-claude-code/actions/workflows/tests.yml"><img src="https://github.com/djkelleher/slack-claude-code/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
</p>

**Claude Code and Codex, in Slack.** Access both backends remotely from any device, with Slack-native approvals, threads, uploads, queues, and status views.

## Why Slack?

| Feature | Terminal | Slack |
|---------|----------|-------|
| **Code blocks** | Plain text | Syntax-highlighted with copy button |
| **Long output** | Scrolls off screen | "View Details" modal |
| **Permissions** | Y/n prompts | Approve/Deny buttons |
| **Parallel work** | Multiple terminals | Threads = isolated sessions |
| **File sharing** | `cat` or copy-paste | Drag & drop with preview |
| **Notifications** | Watch the terminal | Alerts when tasks complete |
| **Streaming** | Live terminal output | Watch responses as they generate |
| **Smart context** | Manual file inclusion | Frequently-used files auto-included |

## Installation

### Prerequisites
- Python 3.10+
- [Claude Code CLI](https://github.com/anthropics/claude-code) installed and authenticated
- `codex` CLI installed if you plan to use Codex models (the bot uses `codex app-server` for Codex sessions)

### 1. Install the CLI
```bash
pipx install slack-claude-code
```

This installs:
- `aislack` - start the Slack bot
- `aislack-config` - manage encrypted local config

`src.app:run()` still accepts legacy `ccslack`-style invocation if your environment already exposes it, but the packaged entrypoints are `aislack` and `aislack-config`.

### 2. Create Slack App
Go to https://api.slack.com/apps → "Create New App" → "From scratch"

**Socket Mode**: Enable and create an app-level token with `connections:write` scope (save the `xapp-` token)

**Bot Token Scopes** (OAuth & Permissions):
- `chat:write`, `commands`, `channels:history`, `app_mentions:read`, `files:read`, `files:write`

**Event Subscriptions**: Enable and add `message.channels`, `app_mention`

**App Icon**: In "Basic Information" → "Display Information", upload `assets/claude_logo.png` from this repo as the app icon

**Slash Commands**: Add the commands from the tables below (or the subset that you plan to use)

If you want worktree workflows, register `/worktree` (and optionally `/wt` as an alias) in Slack.

#### Configuration
Customize behavior for your workflow.

| Command | Description | Example |
|---------|-------------|---------|
| `/model` | Show or change AI model and effort | `/model claude-opus-4-6 high` |
| `/mode` | View or set session mode (Claude and Codex) | `/mode`, `/mode plan`, `/mode bypass`, `/mode approval never`, `/mode sandbox workspace-write` |
| `/permissions` | Show current approval/sandbox settings and how to change them | `/permissions` |
| `/notifications` | View or configure notifications | `/notifications`, `/notifications on`, `/notifications completion off` |

#### Codex Controls
Use these when your session model is a Codex model.

| Command | Description | Example |
|---------|-------------|---------|
| `/usage` | Show backend-native usage details (Codex status/rate limits or Claude `/usage`) | `/usage` |
| `/review` | Start a Codex review for uncommitted changes (or custom target text) | `/review`, `/review API auth flow` |
| `/review status [thread_id\|current]` | Inspect latest review/thread lifecycle status (`read` is accepted as an alias) | `/review status`, `/review status current` |
| `/mcp` | Show Codex MCP server status | `/mcp` |

Claude sessions use the Claude CLI directly. Codex sessions use `codex app-server` JSON-RPC.
See [Codex app-server JSON-RPC integration notes](CODEX_APP_SERVER_JSON_RPC.md) for
the exact request/notification methods this app handles.

`/mode` in Codex sessions:
- `/mode bypass` -> `approval=never`
- `/mode ask`, `/mode default`, and `/mode plan` -> `approval=on-request`
- `/mode plan` sets native app-server `turn/start.collaborationMode` to `plan`
- `/mode accept` and `/mode delegate` remain unsupported for Codex
- `/mode approval <untrusted|on-failure|on-request|never>` sets explicit approval policy
- `/mode sandbox <read-only|workspace-write|danger-full-access>` sets sandbox policy

Current Codex defaults:
- `CODEX_SANDBOX_MODE=danger-full-access`
- `CODEX_APPROVAL_MODE=on-request`
- Set `CODEX_APPROVAL_MODE=never` if you want Codex command/file approvals to auto-accept instead of posting Slack approve/deny buttons.
- Set `/mode sandbox ...` or `CODEX_SANDBOX_MODE=...` to override the sandbox policy per session or globally.
- Existing saved sessions keep their stored sandbox/approval settings until you change them with `/mode`.

Supported Codex models:
- `gpt-5.3-codex`
- `gpt-5.4`
- `gpt-5.3-codex-spark`
- `gpt-5.2-codex`
- `gpt-5.1-codex-max`
- `gpt-5.2`
- `gpt-5.1-codex-mini`

Supported Claude models:
- `opus` / `opus-4.6` / `claude-opus-4-6`
- `sonnet` / `sonnet-4.6` / `claude-sonnet-4-6`
- `haiku` / `haiku-4.5` / `claude-haiku-4-5`
- `opus-4.5` / `claude-opus-4-5` (legacy)
- `sonnet-4.5` / `claude-sonnet-4-5` (legacy)

Optional Claude effort argument (space-separated):
- `/model <claude-model> low`
- `/model <claude-model> medium`
- `/model <claude-model> high`
- `/model <claude-model> max`
- `/model <claude-model> auto`

Note: `/fast` is not currently exposed as a Slack slash command in this wrapper.

Optional Codex effort argument (space-separated):
- `/model <codex-model> low`
- `/model <codex-model> medium`
- `/model <codex-model> high`
- `/model <codex-model> xhigh` (alias: `extra-high`)

Breaking change:
- Legacy Codex transport flags `CODEX_NATIVE_PLAN_MODE_ENABLED` and `CODEX_USE_DANGEROUS_BYPASS` were removed.

#### Session Management
Each Slack thread maintains an isolated backend session with its own context.

| Command | Description | Example |
|---------|-------------|---------|
| `/clear` | Reset conversation | `/clear` |
| `/compact` | Compact context | `/compact` |
| `/cost` | Show session cost | `/cost` |
| `/usage` | Show backend-native usage details (Codex status/rate limits or Claude `/usage`) | `/usage` |

#### Claude CLI Utilities
These map to terminal Claude Code slash commands. In Codex sessions, unsupported ones return a hint.

| Command | Description | Example |
|---------|-------------|---------|
| `/context` | Show Claude context usage | `/context` |
| `/init` | Initialize project memory files | `/init` |

#### Shell & Navigation
Control the working directory and run lightweight host commands.

| Command | Description | Example |
|---------|-------------|---------|
| `/!` | Run a bash command directly on the host in the session working directory | `/! pytest -q` |
| `/ls` | List directory contents | `/ls`, `/ls src/` |
| `/cd` | Change working directory | `/cd /home/user/project`, `/cd subfolder`, `/cd ..` |
| `/pwd` | Print working directory | `/pwd` |

#### Directory Context
Manage extra directories available to the assistant in addition to the working directory.

| Command | Description | Example |
|---------|-------------|---------|
| `/add-dir` | Add directory to context | `/add-dir /home/user/other-project` |
| `/remove-dir` | Remove directory from context | `/remove-dir /home/user/other-project` |
| `/list-dirs` | List all directories in context | `/list-dirs` |

#### Agents
Configurable subagents for specialized tasks. Matches terminal Claude Code's agent system.

| Command | Description | Example |
|---------|-------------|---------|
| `/agents` | List all available agents | `/agents` |
| `/agents run` | Run a specific agent | `/agents run explore find all API endpoints` |
| `/agents info` | Show agent configuration | `/agents info plan` |
| `/agents create` | Show how to create custom agents | `/agents create` |

**Built-in agents:**
- `explore` - Read-only codebase exploration (fast, uses Haiku)
- `plan` - Create detailed implementation plans
- `bash` - Execute shell commands, git, npm, etc.
- `general` - Full capabilities for implementation

**Custom agents:** Agent definitions are loaded in this precedence order, with higher-priority entries overriding lower ones when names collide:
- Built-in agents
- User agents from `~/.claude/agents/*.md`
- Project agents from `.claude/agents/*.md`

Create `.claude/agents/<name>.md` files with YAML frontmatter to define project-specific agents. Supported frontmatter keys:
- `name`
- `description`
- `tools`
- `disallowedTools`
- `model`
- `permissionMode`
- `maxTurns`

Example:

```md
---
description: Investigate queue and scheduler behavior
tools: Read, Grep, Bash
model: haiku
permissionMode: bypassPermissions
maxTurns: 20
---

Trace queue execution paths, summarize relevant files, and do not modify code.
```

#### Command Queue
Queue commands for sequential execution while preserving Claude's session context across items.

| Command | Description | Example |
|---------|-------------|---------|
| `/q` | Add command to queue | `/q analyze the API endpoints` |
| `/qc view` | View queue status (legacy control command) | `/qc view` |
| `/qc clear` | Clear pending queue (legacy control command) | `/qc clear` |
| `/qc delete` | Delete the entire queue scope, including running/history items (legacy control command) | `/qc delete` |
| `/qc remove [id]` | Remove next or specific pending item (legacy control command) | `/qc remove` |
| `/qc pause` | Pause queue after current running item(s) finish (legacy control command) | `/qc pause` |
| `/qc stop` | Stop queue immediately and cancel the active queue processor (legacy control command) | `/qc stop` |
| `/qc resume` | Resume a paused/stopped queue | `/qc resume` |
| `/qc append <prompt>` | Append a plain prompt to the current queue scope | `/qc append summarize the failures` |
| `/qc prepend <prompt>` | Prepend a plain prompt to the current queue scope | `/qc prepend run smoke tests first` |
| `/qc insert <index> <prompt>` | Insert a plain prompt at a 1-based queue position | `/qc insert 2 rerun the flaky test` |
| `/qc timer add <action> <time> [channel\|thread_ts]` | Add a scheduled queue control timer (`pause`, `resume`, `stop`, `start`) | `/qc timer add pause 2026-03-30T15:00:00-04:00` |
| `/qc timer cancel <event_id\|all> [channel\|thread_ts]` | Cancel one pending timer by id or cancel all pending timers in scope | `/qc timer cancel 501` |
| `/qv` | View queue status | `/qv` |
| `/qclear` | Clear pending queue | `/qclear` |
| `/qdelete` | Delete the entire queue scope, including running/history items | `/qdelete` |
| `/qr` | Remove the next pending item | `/qr` |
| `/qr <id>` | Remove a specific pending item | `/qr 5` |

Queue scope follows session scope:
- Channel messages use a channel-level queue
- Thread messages use an isolated queue per thread

Queue control behavior:
- `/qc pause` lets any currently running queue item finish, then leaves the remaining items pending.
- `/qc stop` cancels the active queue processor immediately and marks the queue as stopped.
- `/qc resume` flips the scope back to running and restarts processing if pending items remain.
- `/qclear` and `/qc clear` remove only pending items.
- `/qdelete` and `/qc delete` remove the entire queue scope, including running/completed/cancelled records, clear pending scheduled controls, then reset the scope to `running`.
- Adding new items with `/q` does not auto-start processing while a scope is paused or stopped; resume it explicitly with `/qc resume`.
- `/qv` and `/qc view` show queue state and include a notice when the scope is paused or stopped.
- Use `/qc timer add ...` and `/qc timer cancel ...` to manage scheduled queue control timers dynamically; `/qv` and `/qc view` show pending timer IDs.
- Set `QUEUE_AUTO_ANSWER_QUESTIONS=true` to auto-answer assistant questions during queue execution by choosing `(Recommended)` options (fallback: first option).
- Set `QUEUE_AUTO_APPROVE_PERMISSIONS=true` to auto-approve permission prompts during queue execution. This defaults to `true`.
- Set `QUEUE_PAUSE_ON_QUESTIONS=true` to pause the queue and return the current item to pending whenever the assistant requests user input.
- Set `QUEUE_AUTO_MAX_CONTINUE_ROUNDS` to cap auto-continue rounds per auto-follow chain (default `20`).
- Set `QUEUE_AUTO_MAX_CHECK_ROUNDS` to cap auto-check rounds per auto-follow chain (default `10`).
- Set `QUEUE_AUTO_JUDGE_TIMEOUT_SECONDS` to cap per-item LLM judge runtime (default `30` seconds).

Inline runtime mode directives are also supported in prompt text:
- `(mode: plan)` (or `((mode: plan))`) applies a one-prompt runtime mode override without changing stored session mode.
- Optional wrapper form `(mode: ...) ... (end)` is supported for regular prompts when `(end)` is the final non-empty line.
- Supported values mirror `/mode` compatibility aliases and Codex policy forms: `plan`, `default`, `ask`, `bypass`, `accept`, `delegate`, `approval <mode>`, `sandbox <mode>`.

#### Structured Queue DSL (Queues + Worktree + Loops)

`/q` also supports a structured DSL so one command can enqueue many prompts.

Queue submission directives supported before the first content block:
- `((append))` append to pending items
- `((prepend))` prepends the expanded plan
- `((insert<n>))` inserts the expanded plan at 1-based position `n`
- `((auto))` enables auto-follow checks/continuation after each completed queue prompt
- `((auto-finish))` enables one consolidated auto-follow pass when the queue drains

| Marker | Meaning |
|--------|---------|
| `***` | Prompt separator (split into multiple queue items) |
| `((branch <name>))` ... `((end))` | Run enclosed prompts in worktree for branch `<name>` (`((end))` optional at EOF) |
| `((loop<n>))` ... `((end))` | Repeat enclosed prompts `n` times (`n >= 1`, `((end))` optional at EOF) |
| `FOR <name> IN ((a, b))` ... `((end))` | Repeat enclosed prompts for each value, substituting `((<name>))` or `((((<name>))))` |
| `((parallel))` ... `((end))` | Run all enclosed prompts concurrently as one barriered queue group |
| `((parallel<n>))` ... `((end))` | Keep up to `<n>` enclosed prompts running concurrently until the block is drained |
| `((mode: <value>))` ... `((end))` | Apply runtime mode override to enclosed prompts (nestable; inner mode overrides outer mode) |
| `((at <time> [action]))` | Schedule queue control action (`start`, `pause`, `resume`, `stop`) for this queue scope; default action is `resume` |
| `((save <name>))` | Save a queue item's final output into variable `<name>` for later prompts |
| `((p<n>output))` | Inject the final output from authored queue position `n` |
| `((<name>))` | Inject a previously saved named output variable |

Rules:
- Markers normally appear on their own line.
- Start markers also support a single-line shorthand: `((loop3)) do this` or `((branch feature/auth)) do this`.
- Substitution loops also support a single-line shorthand: `FOR name IN ((joe, tod)) greet ((name))`.
- Blocks can be nested (`loop` inside `branch`, `branch` inside `loop`, etc.).
- `parallel` can be nested with `loop` and `branch`, but nested `parallel` blocks are invalid.
- Substitution placeholders only replace loop variables that are currently in scope; unrelated
  `((name))` references still resolve later as saved queue outputs.
- If a block reaches end-of-input, its closing marker can be omitted.
- Timer directives are top-level queue submission directives (before first non-directive content line).
- Timer `<time>` supports ISO datetime with timezone (for example `2026-03-13T18:30:00-04:00`) or server-local `HH:MM` for today.
- Timer directives must be in the future when submitted.
- Scheduled controls append to any existing pending scheduled controls in the same scope.
- `start` uses the same runtime behavior as `resume`.
- Queue clearing is intentionally not part of the DSL; use `/qc clear`.
- Branch blocks require your current session directory to be a git repo.
- Missing branch worktrees are auto-created for that branch when needed.
- Parallel blocks are barriers: later queue items do not start until the parallel block fully finishes.
- Mode blocks are fully nestable and can wrap branch/loop/parallel blocks; runtime mode changes are per-queue-item and do not update stored session mode.

Structured queue plans can be submitted as normal message text or uploaded as a text file/snippet. If an uploaded file contains queue-plan markers, the bot parses it directly.

Example:

```text
/q
((loop2))
Run test suite and summarize failures
***
((branch feature/auth))
Implement auth middleware updates
***
Add/update auth tests and run them
((end))
((end))
((parallel2))
Summarize open PRs touching auth
***
Review auth-related production logs
***
((end))
((save release_notes))
Write release notes summary
***
Post the saved summary into changelog using:
((release_notes))
```

This expands to 10 queued items:
1. `Run test suite and summarize failures`
2. `Implement auth middleware updates` (in `feature/auth` worktree)
3. `Add/update auth tests and run them` (in `feature/auth` worktree)
4. `Run test suite and summarize failures`
5. `Implement auth middleware updates` (in `feature/auth` worktree)
6. `Add/update auth tests and run them` (in `feature/auth` worktree)
7. `Summarize open PRs touching auth` (runs in parallel block)
8. `Review auth-related production logs` (runs in parallel block)
9. `Write release notes summary`
10. `Post the saved summary into changelog using: ((release_notes))`

#### Jobs & Control
Monitor and control long-running operations with real-time progress updates.

| Command | Description | Example |
|---------|-------------|---------|
| `/st` | Show active job status | `/st` |
| `/cc` | Cancel tracked background jobs | `/cc` or `/cc 42` |
| `/cancel` | Cancel active Claude/Codex executions in current channel/thread | `/cancel` |
| `/c` | Alias for `/cancel` | `/c` |
| `/esc` | Interrupt current operation (Escape/Ctrl+C style) | `/esc` |

#### Git Worktrees
Worktree workflows are exposed directly via Slack commands.

| Command | Description | Example |
|---------|-------------|---------|
| `/worktree` | Manage worktrees (`add`, `list`, `switch`, `merge`, `remove`, `prune`) | `/worktree add feature/auth` |
| `/wt` | Alias for `/worktree` | `/wt list` |

Worktree command examples:
- `/worktree add feature/auth --from main`
- `/worktree add hotfix/login --stay`
- `/worktree list --verbose`
- `/worktree merge feature/auth` (merges into your current session worktree branch)
- `/worktree merge feature/auth --into main`
- `/worktree remove feature/auth --delete-branch`
- `/worktree prune --dry-run`

Behavior notes:
- In Claude sessions, `/worktree add <branch>` with no `--from` and no `--stay` uses Claude's native `--worktree` flow when available.
- Branch-scoped queue plans auto-create missing worktrees and route those queue items to the branch worktree path.
- `/worktree list --verbose` includes cleanliness and ahead/behind metadata.

#### Worktree Workflow Tutorial

Use this when you want clean isolation for multiple tasks without stashing or branch juggling.

**How worktree + Slack sessions interact**
- A worktree is a separate directory checked out at a branch.
- This app stores a working directory per Slack session scope (channel or thread).
- `/worktree add ...` creates a worktree and switches the current session to it (unless `--stay`).
- `/worktree switch ...` changes only your Slack session's working directory.

**Workflow 1: Feature branch from `main` to merge and cleanup**
1. Create and switch to a new worktree:
   - `/worktree add feature/auth --from main`
2. Confirm context:
   - `/pwd`
   - `/! git status --short --branch`
3. Implement and commit as usual:
   - Ask the assistant to make changes, or run your own commands with `/!`
   - `/! git diff --staged`
   - `/! git commit -m "feat: add auth middleware"`
4. Merge into your current target branch:
   - `/worktree switch main`
   - `/worktree merge feature/auth`
5. Remove the finished worktree and local branch:
   - `/worktree remove feature/auth --delete-branch`

**Workflow 2: Parallel tasks in Slack threads**
1. In channel root, keep your default worktree for ongoing work.
2. In thread A:
   - `/wt add feature/api-cleanup --from main`
3. In thread B:
   - `/wt add hotfix/login-timeout --from main`
4. Work independently in each thread. Each thread has isolated session state and queue scope.
5. Merge each branch when ready:
   - `/wt merge <branch>`
6. Cleanup each finished worktree:
   - `/wt remove <branch> --delete-branch`

**Workflow 3: Safe cleanup of stale worktrees**
1. Inspect everything:
   - `/worktree list --verbose`
2. Preview prune candidates:
   - `/worktree prune --dry-run`
3. Apply prune:
   - `/worktree prune`
4. Remove explicit stale worktrees:
   - `/worktree remove <branch-or-path>`
   - Add `--force` only when you intentionally want to discard uncommitted changes.

**Common mistakes to avoid**
- Trying to remove the current or main worktree (blocked by design).
- Merging into a dirty target worktree (commit or stash first).
- Assuming `/worktree merge` always keeps source worktree: by default it may auto-remove a clean source worktree unless you pass `--keep-worktree`.


### 3. Configure

Use the built-in config CLI to securely store your Slack credentials:

```bash
aislack-config set SLACK_BOT_TOKEN=xoxb-...
aislack-config set SLACK_APP_TOKEN=xapp-...
aislack-config set SLACK_SIGNING_SECRET=...
```

Equivalent form:

```bash
aislack config set SLACK_BOT_TOKEN=xoxb-...
```

**Config CLI Commands:**
| Command | Description |
|---------|-------------|
| `aislack-config set KEY=VALUE` | Store a configuration value |
| `aislack-config get KEY` | Retrieve a configuration value |
| `aislack-config list` | List all stored configuration |
| `aislack-config delete KEY` | Remove a configuration value |
| `aislack-config path` | Show config file locations |

Configuration is encrypted and stored in `~/.slack-claude-code/config.enc`. Sensitive values (tokens, secrets) are masked when displayed.

**Alternative:** You can also use environment variables or a `.env` file. Config values take precedence over environment variables.

**Where to find these values:**
- `SLACK_BOT_TOKEN`: Your App → OAuth & Permissions → Bot User OAuth Token
- `SLACK_APP_TOKEN`: Your App → Basic Information → App-Level Tokens → (token you created with `connections:write`)
- `SLACK_SIGNING_SECRET`: Your App → Basic Information → App Credentials → Signing Secret
- `DEFAULT_WORKING_DIR`: Optional default starting directory for new sessions
- `DEFAULT_MODEL`: Optional default Claude or Codex model
- `SLACK_QUESTION_MENTION`: Optional mention text for interactive approval/question posts (if unset, detected question text falls back to `@channel`)
- `GITHUB_REPO`: Optional `owner/repo` used for GitHub file links in Slack output
- `QUEUE_AUTO_ANSWER_QUESTIONS`: Optional queue auto-answer toggle (`false` by default)
- `QUEUE_AUTO_APPROVE_PERMISSIONS`: Optional queue auto-approve toggle (`true` by default)
- `QUEUE_PAUSE_ON_QUESTIONS`: Optional queue pause-on-question toggle (`false` by default)
- `QUEUE_AUTO_MAX_CONTINUE_ROUNDS`: Optional per-chain auto-continue cap (`20` by default)
- `QUEUE_AUTO_MAX_CHECK_ROUNDS`: Optional per-chain auto-check cap (`10` by default)
- `QUEUE_AUTO_JUDGE_TIMEOUT_SECONDS`: Optional queue auto-judge timeout in seconds (`30` by default)

### 4. Start the Slack bot
Run `aislack` in your terminal. The working directory where you start it becomes the default working directory for new session scopes unless `DEFAULT_WORKING_DIR` is set. If a `.env` file exists in that directory, it is loaded automatically.

```bash
aislack
```

## Usage

Type messages in any channel where the bot is installed. The channel root is one session scope; each Slack thread is a separate isolated session scope with its own backend conversation, model, queue, working directory, and added directories.

File uploads are downloaded into the app data directory and added to the session context automatically. If a message contains uploaded files but no text, the bot asks the selected backend to analyze those files. Images also get a Slack preview block in-thread.


## Troubleshooting

| Problem | Solution |
|---------|----------|
| Configuration errors on startup | Check `aislack-config list`, `.env`, or environment variables for the required Slack tokens |
| Commands not appearing | Verify you registered the slash commands in your Slack app settings |
| Uploaded files fail | Check `files:read` and `files:write` scopes and confirm the file size is within `MAX_FILE_SIZE_MB` |
| Queue branch blocks fail | Make sure the current session directory is inside a git repo |

## License

MIT
