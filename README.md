# Slack Claude Code Bot

A Slack app that allows you to run Claude Code CLI commands from Slack. Each channel represents a separate session, with persistent PTY-based sessions, multi-agent workflows, usage budgeting, and permission approval via Slack buttons.

## Features

- **Persistent PTY Sessions**: Keep Claude Code running in interactive mode per channel using pexpect
- **Channel-based Sessions**: Each Slack channel maintains its own Claude Code session with working directory and command history
- **Multi-Agent Workflows**: Run complex tasks through Planner → Worker → Evaluator pipeline
- **Usage Budgeting**: Time-aware usage thresholds (day/night) with automatic pausing
- **Permission Approval**: Handle MCP tool permissions via Slack buttons
- **Command History**: Commands are stored in the database and can be rerun via buttons
- **FIFO Queue**: Queue multiple commands for sequential execution
- **Filesystem Navigation**: Navigate directories with `/ls` and `/cd` commands
- **Claude CLI Passthrough**: Access Claude Code CLI commands directly from Slack
- **Streaming Output**: See Claude's responses as they're generated
- **Hook System**: Event-driven architecture for session events

## Prerequisites

- Python 3.10+
- [Claude Code CLI](https://github.com/anthropics/claude-code) installed and authenticated
- A Slack workspace where you can install apps

## Installation

1. **Clone and install dependencies**:

```bash
cd slack-claude-code
poetry install
```

2. **Create your Slack App**:

   Go to https://api.slack.com/apps and click "Create New App"

   - Choose "From scratch"
   - Name it (e.g., "Claude Code Bot")
   - Select your workspace

3. **Enable Socket Mode**:

   - Go to "Socket Mode" in the sidebar
   - Toggle "Enable Socket Mode" ON
   - Create an app-level token with `connections:write` scope
   - Save the token (starts with `xapp-`)

4. **Add Bot Token Scopes**:

   Go to "OAuth & Permissions" and add these Bot Token Scopes:
   - `chat:write` - Send messages
   - `commands` - Handle slash commands
   - `channels:history` - Read channel messages (for context)
   - `app_mentions:read` - Respond to @mentions

5. **Create Slash Commands**:

   Go to "Slash Commands" and create:

   | Command | Description |
   |---------|-------------|
   | `/c` | Run a Claude Code command |
   | `/cwd` | Show or set working directory |
   | `/ls` | List directory contents |
   | `/cd` | Change working directory (supports relative paths) |
   | `/q` | Add command to FIFO queue |
   | `/qv` | View queue status |
   | `/qc` | Clear pending queue items |
   | `/qr` | Remove specific queue item |
   | `/st` | View active jobs |
   | `/cc` | Cancel running jobs |
   | `/task` | Start multi-agent workflow task |
   | `/tasks` | List active multi-agent tasks |
   | `/task-cancel` | Cancel a multi-agent task |
   | `/usage` | Show Claude Pro usage |
   | `/budget` | Configure usage thresholds |
   | `/pty` | Show PTY session status |
   | `/clear` | Reset Claude conversation |
   | `/add-dir` | Add directory to Claude context |
   | `/compact` | Compact conversation |
   | `/cost` | Show session cost |
   | `/claude-help` | Show Claude Code help |
   | `/doctor` | Run Claude Code diagnostics |
   | `/claude-config` | Show Claude Code config |

6. **Install to Workspace**:

   - Go to "Install App" in the sidebar
   - Click "Install to Workspace"
   - Authorize the app

7. **Configure Environment**:

```bash
cp .env.example .env
# Edit .env with your tokens
```

Required environment variables:
- `SLACK_BOT_TOKEN` - Bot User OAuth Token (xoxb-...)
- `SLACK_APP_TOKEN` - App-Level Token (xapp-...)
- `SLACK_SIGNING_SECRET` - From Basic Information > App Credentials

8. **Run the Bot**:

```bash
poetry run python run.py
```

## Usage

### Basic Commands

```
/c Explain this codebase
```

Runs the prompt in Claude Code and sends the response.

```
/cwd /home/dan/projects/my-app
```

Sets the working directory for the current channel's session.

### Multi-Agent Workflows

```
/task Implement a new feature that adds dark mode support
```

Starts a multi-agent workflow:
1. **Planner**: Analyzes the task and creates a structured plan
2. **Worker**: Executes the plan step by step
3. **Evaluator**: Reviews the work and determines if it's complete

The workflow iterates until the evaluator marks it complete or max iterations reached.

```
/tasks
```

Lists all active multi-agent tasks with status and cancel buttons.

```
/task-cancel abc123
```

Cancels a specific task by ID.

### Usage Budgeting

```
/usage
```

Shows current Claude Pro usage with a visual progress bar:
```
Usage: 67.5% ✅
[█████████████░░░░░░░]
Reset: 2 hours | Threshold (day): 85%
```

```
/budget
```

Shows current budget thresholds:
- Day threshold (default 85%)
- Night threshold (default 95%)
- Night hours (22:00 - 06:00)

```
/budget day 80
/budget night 90
```

Updates the threshold for day or night periods.

### PTY Session Management

```
/pty
```

Shows PTY session status for the current channel:
- Session ID
- State (idle, busy, awaiting_approval)
- Process ID
- Working directory

Includes a "Restart Session" button to force-restart the session.

### Filesystem Navigation

```
/ls
```

Lists contents of the current working directory.

```
/ls src/handlers
```

Lists contents of a specific directory (relative or absolute path).

```
/cd ..
```

Changes to the parent directory.

```
/cd src/handlers
```

Changes to a subdirectory (supports relative paths).

### Command Queue

```
/q Explain the main entry point
/q List all API endpoints
/q Write tests for the user service
```

Adds commands to a FIFO queue. Commands execute one at a time in order, maintaining Claude session continuity.

```
/qv
```

Shows the current queue status (pending items and running item).

```
/qc
```

Clears all pending items from the queue.

```
/qr 42
```

Removes a specific queue item by ID.

### Claude CLI Commands

Access Claude Code CLI commands directly from Slack:

```
/clear              # Reset conversation
/add-dir ./lib      # Add directory to context
/compact            # Compact conversation
/cost               # Show session cost
/claude-help        # Show Claude Code help
/doctor             # Run diagnostics
/claude-config      # Show config
```

### Job Management

```
/st
```

Shows all active jobs in the channel.

```
/cc
```

Cancels all active jobs in the channel.

```
/cc 123
```

Cancels a specific job by ID.

## Architecture

```
slack-claude-code/
├── src/
│   ├── app.py              # Main entry point
│   ├── config.py           # Configuration
│   ├── database/           # SQLite persistence
│   │   ├── models.py       # Data models
│   │   ├── migrations.py   # Schema setup
│   │   └── repository.py   # Data access
│   ├── claude/             # Claude CLI integration
│   │   ├── executor.py     # PTY-based execution
│   │   ├── streaming.py    # JSON stream parsing
│   │   └── subprocess_executor.py  # Subprocess-based execution
│   ├── pty/                # PTY session management
│   │   ├── session.py      # PTYSession class (pexpect)
│   │   ├── pool.py         # Session pool registry
│   │   └── parser.py       # ANSI stripping, prompt detection
│   ├── hooks/              # Event hook system
│   │   ├── registry.py     # HookRegistry with decorators
│   │   └── types.py        # Event types and data
│   ├── agents/             # Multi-agent orchestration
│   │   ├── orchestrator.py # Planner→Worker→Evaluator pipeline
│   │   └── roles.py        # Agent prompts and config
│   ├── budget/             # Usage budgeting
│   │   ├── checker.py      # Usage checking (claude usage)
│   │   └── scheduler.py    # Time-aware thresholds
│   ├── approval/           # Permission handling
│   │   ├── handler.py      # PermissionManager
│   │   └── slack_ui.py     # Approval button blocks
│   ├── handlers/           # Slack event handlers
│   │   ├── base.py         # Command decorator and context
│   │   ├── basic.py        # /c, /cwd, /ls, /cd commands
│   │   ├── queue.py        # /q, /qv, /qc, /qr commands
│   │   ├── claude_cli.py   # Claude CLI passthrough commands
│   │   ├── agents.py       # /task, /tasks, /task-cancel
│   │   ├── budget.py       # /usage and /budget commands
│   │   ├── parallel.py     # /st, /cc commands
│   │   ├── pty.py          # /pty command
│   │   └── actions.py      # Button interactions
│   └── utils/              # Helpers
│       ├── formatting.py   # Slack Block Kit
│       └── validators.py   # Input validation
├── data/                   # SQLite database
├── .env                    # Configuration
└── run.py                  # Startup script
```

### System Flow

```
Slack (Socket Mode)
       │
       ▼
┌─────────────────────────────────────────────────────────┐
│                    Command Router                        │
└─────────────────────────────────────────────────────────┘
       │                    │                    │
       ▼                    ▼                    ▼
┌─────────────┐    ┌────────────────┐    ┌─────────────┐
│ Direct Cmd  │    │  Multi-Agent   │    │   Budget    │
│  Executor   │    │  Orchestrator  │    │   Manager   │
└──────┬──────┘    └───────┬────────┘    └─────────────┘
       │                   │
       ▼                   ▼
┌─────────────────────────────────────────────────────────┐
│                   PTY Session Pool                       │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐   │
│  │ #chan-1  │ │ #worker-1│ │ #worker-2│ │ #eval    │   │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘   │
└─────────────────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────┐
│  Hook System: [on_tool_use] [on_approval] [on_result]   │
└─────────────────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────┐
│  MCP Approval Handler → Slack Buttons → PTY stdin       │
└─────────────────────────────────────────────────────────┘
```

## Configuration

Environment variables (set in `.env`):

```bash
# Slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_SIGNING_SECRET=...

# Paths
DATABASE_PATH=./data/slack_claude.db
DEFAULT_WORKING_DIR=/home/dan/projects

# PTY Sessions
SESSION_IDLE_TIMEOUT=1800  # 30 minutes

# Multi-Agent
PLANNER_MAX_TURNS=10
WORKER_MAX_TURNS=30
EVALUATOR_MAX_TURNS=10

# Budget
USAGE_THRESHOLD_DAY=85.0
USAGE_THRESHOLD_NIGHT=95.0
NIGHT_START_HOUR=22
NIGHT_END_HOUR=6

# Permissions
PERMISSION_TIMEOUT=300  # 5 minutes
AUTO_APPROVE_TOOLS=Read,Glob,Grep,LSP  # Comma-separated
```

## Tips

- **Long outputs**: Responses exceeding Slack's limit are truncated. Use "View Output" for full text.
- **Streaming**: Responses update every 2 seconds during generation to avoid rate limits.
- **Sessions**: Each channel maintains a persistent PTY session with Claude Code.
- **Timeouts**: Default 5-minute timeout. Set `COMMAND_TIMEOUT` to adjust.
- **Multi-agent tasks**: Use `/task` for complex work that benefits from planning and evaluation.
- **Night mode**: Higher usage thresholds at night allow more intensive work during off-hours.
- **Command queue**: Use `/q` to queue multiple commands that will execute sequentially.
- **Filesystem**: Use `/ls` and `/cd` to navigate directories without running Claude commands.
- **Session management**: Use `/clear` to reset conversation, `/compact` to reduce context size.

## Troubleshooting

**"Configuration errors" on startup**
- Ensure all required environment variables are set in `.env`

**Commands not appearing in Slack**
- Verify slash commands are created in your app settings
- Check the app is installed to your workspace

**"Working directory does not exist"**
- Use `/cwd` to set a valid directory

**Timeouts**
- Increase `COMMAND_TIMEOUT` for long-running operations
- Consider using parallel execution for complex tasks

**PTY session errors**
- Use `/pty` to check session status
- Click "Restart Session" to force a fresh session

**Permission prompts not appearing**
- Ensure the bot has permission to post in the channel
- Check that button actions are registered in the Slack app

## License

MIT
