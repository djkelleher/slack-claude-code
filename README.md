<p align="center">
  <img src="assets/repo_logo.png" alt="Slack Claude Code Bot" width="1000">
</p>


**Claude Code, but in Slack.** Access Claude Code remotely from any device, or use it full-time for a better UI experience.

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

### 1. Install the `ccslack` executable
```bash
pipx install slack-claude-code
```
You can now run `ccslack` in your termainl. The working directory where you start the executable will be the defualt working direcotry for your Claude Code session(s)

### 2. Create Slack App
Go to https://api.slack.com/apps → "Create New App" → "From scratch"

**Socket Mode**: Enable and create an app-level token with `connections:write` scope (save the `xapp-` token)

**Bot Token Scopes** (OAuth & Permissions):
- `chat:write`, `commands`, `channels:history`, `app_mentions:read`, `files:write`

**Event Subscriptions**: Enable and add `message.channels`, `app_mention`

**App Icon**: In "Basic Information" → "Display Information", upload `assets/claude_logo.png` from this repo as the app icon

**Slash Commands**: Add the commands from this table (or the subset that you plan to use)

| Category | Command | Description |
|----------|---------|-------------|
| CLI | `/init` | Initialize Claude project configuration |
| CLI | `/memory` | View/edit Claude's memory and context |
| CLI | `/review` | Review code changes with Claude |
| CLI | `/doctor` | Diagnose Claude Code installation issues |
| CLI | `/stats` | Show session statistics |
| CLI | `/context` | Display current context information |
| CLI | `/todos` | List and manage todos |
| CLI | `/claude-help` | Show Claude Code help |
| CLI | `/claude-config` | Show Claude Code configuration |
| Session | `/clear` | Clear session and reset conversation |
| Session | `/compact` | Compact conversation context |
| Session | `/cost` | Show session cost |
| Session | `/resume` | Resume a previous Claude session |
| Session | `/pty` | PTY session management |
| Session | `/sessions` | List active sessions |
| Session | `/session-cleanup` | Clean up inactive sessions |
| Navigation | `/ls` | List directory contents |
| Navigation | `/cd` | Change working directory |
| Navigation | `/pwd` | Print working directory |
| Navigation | `/add-dir` | Add directory to context |
| Git | `/status` | Show git status |
| Git | `/diff` | Show git diff |
| Git | `/commit` | Commit staged changes |
| Git | `/branch` | Show/create/switch branches |
| Config | `/model` | Show or change AI model |
| Config | `/mode` | Set permission mode (plan/approve/bypass) |
| Config | `/permissions` | View or update permissions |
| Config | `/notifications` | Configure notification settings |
| Queue | `/q <cmd>` | Queue a command for execution |
| Queue | `/qv` | View queued commands |
| Queue | `/qc` | Clear the command queue |
| Queue | `/qr <id>` | Remove a specific queued command |
| Jobs | `/st` | Show status of running jobs |
| Jobs | `/cc` | Cancel current job |
| Jobs | `/esc` | Interrupt current operation |
| Multi-Agent | `/task` | Create a new agent task |
| Multi-Agent | `/tasks` | List running agent tasks |
| Multi-Agent | `/task-cancel` | Cancel an agent task |

### 3. Configure and run
```bash
cp .env.example .env
# Add your tokens: SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_SIGNING_SECRET
poetry run python run.py
```

## Usage

Type messages in any channel where the bot is present. Each Slack thread maintains an independent Claude session with its own working directory and context.

### Plan Mode

```
/mode plan
```

Claude creates a detailed plan before execution, shown with Approve/Reject buttons. Ideal for complex implementations where you want to review the approach first.

### Multi-Agent Tasks

The multi-agent system uses a **Planner → Worker → Evaluator** pipeline for complex tasks:

```
/task refactor the authentication module to use JWT tokens
```

**How it works:**
1. **Planner** analyzes the task and creates an actionable plan
2. **Worker** executes the plan step by step
3. **Evaluator** reviews the output and determines if more work is needed

The system iterates up to 3 times until the evaluator marks the task as complete.

**Commands:**
| Command | Description |
|---------|-------------|
| `/task <description>` | Start a new multi-agent task |
| `/tasks` | List all active tasks with status |
| `/task-cancel <id>` | Cancel a running task |

**Example workflow:**
```
User: /task add unit tests for the user service

🔄 Planning... (analyzing task and creating plan)
🔨 Working... (executing the plan)
✅ Evaluating... (reviewing results)

✅ Task Complete
Verdict: COMPLETE
Created 12 unit tests covering UserService methods
```

### Command Queue

Queue multiple commands for sequential execution while maintaining session context:

```
/q analyze the database schema
/q suggest performance improvements
/q implement the top 3 suggestions
```

Each command runs after the previous completes, preserving Claude's memory across the queue.

**Commands:**
| Command | Description |
|---------|-------------|
| `/q <prompt>` | Add command to queue (shows position) |
| `/qv` | View queue status and pending items |
| `/qc` | Clear all pending items |
| `/qr <id>` | Remove specific item from queue |

**Example:**
```
User: /q review the API endpoints
Added to queue at position #1

User: /q fix any security issues found
Added to queue at position #2

User: /qv
📋 Queue Status
Running: review the API endpoints
Pending:
  #2: fix any security issues found
```

### Jobs & Parallel Execution

Monitor and control long-running operations:

```
/st          # Show status of all active jobs
/cc          # Cancel all jobs in channel
/cc abc123   # Cancel specific job by ID
/esc         # Send interrupt signal (like Ctrl+C)
```

Jobs track parallel analysis tasks and sequential loops. Each job shows progress and provides cancel buttons in the Slack UI.

**Example:**
```
User: /st
📊 Active Jobs

Job: abc123
  Type: parallel_analysis
  Status: running (3/5 complete)
  [Cancel]

Job: def456
  Type: sequential_loop
  Status: running (iteration 2/10)
  [Cancel]
```

### Git Integration

Full git workflow support without leaving Slack:

```
/status              # Show branch, staged/unstaged changes
/diff                # Show uncommitted changes
/diff --staged       # Show only staged changes
/commit fix: resolve login race condition
/branch              # Show current branch
/branch create feature/auth
/branch switch main
```

**Example workflow:**
```
User: /status
📌 Branch: feature/auth
↑2 ahead of origin

📝 Staged:
  src/auth.py
  tests/test_auth.py

📄 Modified:
  README.md

User: /diff --staged
[Shows diff of staged files]

User: /commit add JWT token refresh logic
✅ Committed: a1b2c3d
```

Git commands include safety validations for branch names and commit messages, with automatic truncation for large diffs in Slack.


## Configuration

Key environment variables (see `.env.example` for full list):

```bash
# Required
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_SIGNING_SECRET=...

# Optional
DEFAULT_WORKING_DIR=/path/to/projects
COMMAND_TIMEOUT=300              # 5 min default
CLAUDE_PERMISSION_MODE=approve-all  # or: prompt, deny
AUTO_APPROVE_TOOLS=Read,Glob,Grep,LSP
```

## Architecture

```
src/
├── app.py                 # Main entry point
├── config.py              # Configuration
├── database/              # SQLite persistence (models, migrations, repository)
├── claude/                # Claude CLI integration (executor, streaming)
├── pty/                   # PTY session management (session, pool, parser)
├── handlers/              # Slack command handlers
├── agents/                # Multi-agent orchestration (planner→worker→evaluator)
├── approval/              # Permission & plan approval handling
├── git/                   # Git operations (status, diff, commit, branch)
├── hooks/                 # Event hook system
├── question/              # AskUserQuestion tool support
├── tasks/                 # Background task management
└── utils/                 # Formatters, helpers, validators
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Configuration errors on startup | Check `.env` has all required tokens |
| Commands not appearing | Verify slash commands in Slack app settings |
| Timeouts | Increase `COMMAND_TIMEOUT` |
| PTY session errors | Use `/pty` → "Restart Session" |

## License

MIT
