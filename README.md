<p align="center">
  <img src="assets/repo_logo.png" alt="Slack Claude Code Bot" width="1000">
</p>

<p align="center">
  <a href="https://pypi.org/project/slack-claude-code/"><img src="https://img.shields.io/pypi/v/slack-claude-code" alt="PyPI version"></a>
  <a href="https://pypi.org/project/slack-claude-code/"><img src="https://img.shields.io/pypi/pyversions/slack-claude-code" alt="Python versions"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <a href="https://github.com/djkelleher/slack-claude-code/actions/workflows/tests.yml"><img src="https://github.com/djkelleher/slack-claude-code/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
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

### 2. Create Slack App
Go to https://api.slack.com/apps → "Create New App" → "From scratch"

**Socket Mode**: Enable and create an app-level token with `connections:write` scope (save the `xapp-` token)

**Bot Token Scopes** (OAuth & Permissions):
- `chat:write`, `commands`, `channels:history`, `app_mentions:read`, `files:read`, `files:write`

**Event Subscriptions**: Enable and add `message.channels`, `app_mention`

**App Icon**: In "Basic Information" → "Display Information", upload `assets/claude_logo.png` from this repo as the app icon

**Slash Commands**: Add the commands from the tables below (or the subset that you plan to use)

#### Configuration
Customize Claude's behavior for your workflow.

| Command | Description | Example |
|---------|-------------|---------|
| `/model` | Show or change AI model | `/model sonnet` |
| `/mode` | View or set permission mode | `/mode`, `/mode plan` |
| `/permissions` | View/update permissions | `/permissions` |
| `/notifications` | Configure notifications | `/notifications` |

#### Session Management
Each Slack thread maintains an isolated Claude session with its own context.

| Command | Description | Example |
|---------|-------------|---------|
| `/clear` | Reset conversation | `/clear` |
| `/compact` | Compact context | `/compact` |
| `/cost` | Show session cost | `/cost` |
| `/resume` | Resume previous session | `/resume` |
| `/sessions` | List active sessions | `/sessions` |
| `/session-cleanup` | Clean up inactive sessions | `/session-cleanup` |

#### Navigation
Control the working directory for Claude's file operations.

| Command | Description | Example |
|---------|-------------|---------|
| `/ls` | List directory contents | `/ls`, `/ls src/` |
| `/cd` | Change working directory | `/cd /home/user/project`, `cd subfolder`, `cd ..` |
| `/pwd` | Print working directory | `/pwd` |
| `/add-dir` | Add directory to context | `/add-dir ./lib` |

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
| `/mcp` | Show MCP server configuration | `/mcp` |
| `/claude-help` | Show Claude Code help | `/claude-help` |
| `/claude-config` | Show configuration | `/claude-config` |

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


### 3. Configure

Use the built-in config CLI to securely store your Slack credentials:

```bash
ccslack-config set SLACK_BOT_TOKEN=xoxb-...
ccslack-config set SLACK_APP_TOKEN=xapp-...
ccslack-config set SLACK_SIGNING_SECRET=...
```

**Config CLI Commands:**
| Command | Description |
|---------|-------------|
| `ccslack-config set KEY=VALUE` | Store a configuration value |
| `ccslack-config get KEY` | Retrieve a configuration value |
| `ccslack-config list` | List all stored configuration |
| `ccslack-config delete KEY` | Remove a configuration value |
| `ccslack-config path` | Show config file locations |

Configuration is encrypted and stored in `~/.slack-claude-code/config.enc`. Sensitive values (tokens, secrets) are masked when displayed.

**Alternative:** You can also use environment variables or a `.env` file. Config values take precedence over environment variables.

**Where to find these values:**
- `SLACK_BOT_TOKEN`: Your App → OAuth & Permissions → Bot User OAuth Token
- `SLACK_APP_TOKEN`: Your App → Basic Information → App-Level Tokens → (token you created with `connections:write`)
- `SLACK_SIGNING_SECRET`: Your App → Basic Information → App Credentials → Signing Secret

### 4. Start the Slack bot
You can now run `ccslack` in your terminal. The working directory where you start the executable will be the default working directory for your Claude Code session(s). If you have a .env file in this directory, it will automatically be loaded.   

## Usage

Type messages in any channel where the bot is installed. The main channel is a single Claude Code session. If you click `reply` to any message and start a thread, this will be a new Claude Code session.

## Architecture

```
src/
├── app.py                 # Main entry point
├── config.py              # Configuration
├── database/              # SQLite persistence (models, migrations, repository)
├── claude/                # Claude CLI integration (streaming)
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

## License

MIT

- - - 

Congratulations, you can now use Claude Code from anywhere 🎉💪