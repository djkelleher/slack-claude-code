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

### Configure 
```bash
cp .env.example .env
# Add your tokens: SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_SIGNING_SECRET
```
Key environment variables (see `.env.example` for full list):
```bash
# Required
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_SIGNING_SECRET=...

# Optional
DEFAULT_WORKING_DIR=/path/to/projects
CLAUDE_PERMISSION_MODE=approve-all  # or: prompt, deny
AUTO_APPROVE_TOOLS=Read,Glob,Grep,LSP
```

### 1. Install the `ccslack` executable
```bash
pipx install slack-claude-code
```
You can now run `ccslack` in your terminal. The working directory where you start the executable will be the defualt working direcotry for your Claude Code session(s)

### 2. Create Slack App
Go to https://api.slack.com/apps → "Create New App" → "From scratch"

**Socket Mode**: Enable and create an app-level token with `connections:write` scope (save the `xapp-` token)

**Bot Token Scopes** (OAuth & Permissions):
- `chat:write`, `commands`, `channels:history`, `app_mentions:read`, `files:write`

**Event Subscriptions**: Enable and add `message.channels`, `app_mention`

**App Icon**: In "Basic Information" → "Display Information", upload `assets/claude_logo.png` from this repo as the app icon

**Slash Commands**: Add the commands from the tables below (or the subset that you plan to use)

#### Multi-Agent Tasks
Autonomous Planner → Worker → Evaluator pipeline for complex tasks. Iterates up to 3 times until complete.

| Command | Description | Example |
|---------|-------------|---------|
| `/task` | Start a multi-agent task | `/task add unit tests for UserService` |
| `/tasks` | List active tasks with status | `/tasks` |
| `/task-cancel` | Cancel a running task | `/task-cancel abc123` |

#### Command Queue
Queue commands for sequential execution while preserving Claude's session context across items.

| Command | Description | Example |
|---------|-------------|---------|
| `/q` | Add command to queue | `/q analyze the API endpoints` |
| `/qv` | View queue status | `/qv` |
| `/qc` | Clear pending queue | `/qc` |
| `/qr` | Remove specific item | `/qr 5` |

#### Jobs & Control
Monitor and control long-running operations with real-time progress updates.

| Command | Description | Example |
|---------|-------------|---------|
| `/st` | Show active job status | `/st` |
| `/cc` | Cancel jobs | `/cc` or `/cc abc123` |
| `/esc` | Send interrupt (Ctrl+C) | `/esc` |

#### Git
Full git workflow without leaving Slack. Includes branch name and commit message validation.

| Command | Description | Example |
|---------|-------------|---------|
| `/status` | Show branch and changes | `/status` |
| `/diff` | Show uncommitted changes | `/diff --staged` |
| `/commit` | Commit staged changes | `/commit fix: resolve race condition` |
| `/branch` | Manage branches | `/branch create feature/auth` |

#### Session Management
Each Slack thread maintains an isolated Claude session with its own context.

| Command | Description | Example |
|---------|-------------|---------|
| `/clear` | Reset conversation | `/clear` |
| `/compact` | Compact context | `/compact` |
| `/cost` | Show session cost | `/cost` |
| `/resume` | Resume previous session | `/resume` |
| `/pty` | PTY session management | `/pty` |
| `/sessions` | List active sessions | `/sessions` |
| `/session-cleanup` | Clean up inactive sessions | `/session-cleanup` |

#### Navigation
Control the working directory for Claude's file operations.

| Command | Description | Example |
|---------|-------------|---------|
| `/ls` | List directory contents | `/ls src/` |
| `/cd` | Change working directory | `/cd /home/user/project` |
| `/pwd` | Print working directory | `/pwd` |
| `/add-dir` | Add directory to context | `/add-dir ./lib` |

#### Configuration
Customize Claude's behavior for your workflow.

| Command | Description | Example |
|---------|-------------|---------|
| `/model` | Show or change AI model | `/model sonnet` |
| `/mode` | Set permission mode | `/mode plan` |
| `/permissions` | View/update permissions | `/permissions` |
| `/notifications` | Configure notifications | `/notifications` |

#### CLI Tools
Direct access to Claude Code CLI functionality.

| Command | Description | Example |
|---------|-------------|---------|
| `/init` | Initialize project config | `/init` |
| `/memory` | View/edit Claude's memory | `/memory` |
| `/review` | Review code changes | `/review` |
| `/doctor` | Diagnose installation | `/doctor` |
| `/stats` | Show session statistics | `/stats` |
| `/context` | Display context info | `/context` |
| `/todos` | List and manage todos | `/todos` |
| `/claude-help` | Show Claude Code help | `/claude-help` |
| `/claude-config` | Show configuration | `/claude-config` |



## Usage

Type messages in any channel where the bot is present. Each Slack thread maintains an independent Claude session with its own working directory and context.


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
| PTY session errors | Use `/pty` → "Restart Session" |

## License

MIT
