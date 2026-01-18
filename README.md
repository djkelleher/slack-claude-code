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
| **Parallel work** | Multiple terminals | Threads with isolated sessions |
| **File sharing** | `cat` or copy-paste | Drag & drop with preview |
| **Notifications** | Watch the terminal | Alerts when tasks complete |

All Claude Code commands work the same way: `/clear`, `/compact`, `/model`, `/mode`, `/add-dir`, `/review`, plus filesystem and git commands.

## Commands

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

## Installation

### Prerequisites
- Python 3.10+
- [Claude Code CLI](https://github.com/anthropics/claude-code) installed and authenticated

### 1. Install dependencies
```bash
cd slack-claude-code
poetry install
```

### 2. Create Slack App
Go to https://api.slack.com/apps → "Create New App" → "From scratch"

**Socket Mode**: Enable and create an app-level token with `connections:write` scope (save the `xapp-` token)

**Bot Token Scopes** (OAuth & Permissions):
- `chat:write`, `commands`, `channels:history`, `app_mentions:read`, `files:write`

**Event Subscriptions**: Enable and add `message.channels`, `app_mention`

**App Icon**: In "Basic Information" → "Display Information", upload `assets/claude_logo.png` from this repo as the app icon

**Slash Commands** (optional): Create commands like `/clear`, `/model`, `/ls`, `/cd`, `/status`, `/diff`, etc.

### 3. Configure and run
```bash
cp .env.example .env
# Add your tokens: SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_SIGNING_SECRET
poetry run python run.py
```

## Usage

Type messages in any channel where the bot is present. Each Slack thread maintains an independent Claude session with its own working directory and context.

### Key Features

- **Threads = Sessions**: Each thread has isolated context; `/clear` only affects that thread
- **File Uploads**: Drag & drop files—Claude sees them instantly (code, images, PDFs)
- **Smart Context**: Frequently-used files are automatically included in prompts
- **Streaming**: Watch Claude's responses as they're generated

### Plan Mode

```
/mode plan
```

Claude creates a detailed plan before execution, shown with Approve/Reject buttons. Ideal for complex implementations where you want to review the approach first.


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
