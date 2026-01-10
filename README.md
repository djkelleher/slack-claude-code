# Slack Claude Code Bot

A Slack app that allows you to run Claude Code CLI commands from Slack. Each channel and thread represents a separate session, with persistent PTY-based sessions, thread-based contexts, file upload support, smart context management, git integration, multi-agent workflows, usage budgeting, and permission approval via Slack buttons.

## Features

- **Persistent PTY Sessions**: Keep Claude Code running in interactive mode per channel using pexpect
- **Thread-Based Contexts**: Each Slack thread maintains its own independent Claude session with separate working directory and command history
- **Channel-based Sessions**: Each Slack channel maintains its own Claude Code session with working directory and command history
- **File Upload Support**: Drag and drop files into Slack - Claude can read and work with uploaded files (code, images, PDFs, documents)
- **Smart Context Management**: Automatically tracks frequently-used files and includes them in future prompts for better context
- **Git Integration**: Local git operations (status, diff, commit, branch management) directly from Slack
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
   - `commands` - Handle slash commands (optional, for `/c` and other commands)
   - `channels:history` - Read channel messages
   - `app_mentions:read` - Respond to @mentions
   - `files:write` - Upload files (for long responses)

5. **Subscribe to Events**:

   Go to "Event Subscriptions" and enable events:
   - Toggle "Enable Events" ON
   - Under "Subscribe to bot events", add:
     - `message.channels` - Listen to messages in public channels
     - `app_mention` - Respond to @mentions

   **Note**: All messages in channels where the bot is present will be sent to Claude Code. The bot automatically ignores its own messages to prevent loops.

6. **Create Slash Commands** (Optional):

   These are optional - you can just type messages directly in the channel.

   Go to "Slash Commands" and create:

   | Command | Description |
   |---------|-------------|
   | `/c` | Run a Claude Code command |
   | `/plan` | Plan mode - review implementation plan before execution |
   | `/ls` | List directory contents (shows cwd when no argument) |
   | `/cd` | Change working directory (supports relative paths) |
   | `/pwd` | Print current working directory |
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
   | `/clear` | Reset Claude conversation and cancel processes |
   | `/add-dir` | Add directory to Claude context |
   | `/compact` | Compact conversation |
   | `/cost` | Show session cost |
   | `/claude-help` | Show Claude Code help |
   | `/doctor` | Run Claude Code diagnostics |
   | `/claude-config` | Show Claude Code config |
   | `/context` | Visualize context usage |
   | `/model` | Show or change AI model |
   | `/resume` | Resume a previous session |
   | `/init` | Initialize project with CLAUDE.md |
   | `/memory` | Edit CLAUDE.md memory files |
   | `/review` | Request code review |
   | `/permissions` | View or update permissions |
   | `/stats` | Show usage stats and history |
   | `/todos` | List current TODO items |
   | `/diff` | Show git diff of uncommitted changes |
   | `/status` | Show git status |
   | `/commit` | Commit staged changes |
   | `/branch` | Git branch operations (list, create, switch) |
   | `/sessions` | List all sessions for the channel |
   | `/session-cleanup` | Delete inactive sessions (>30 days) |

7. **Install to Workspace**:

   - Go to "Install App" in the sidebar
   - Click "Install to Workspace"
   - Authorize the app

8. **Configure Environment**:

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

### Sending Messages to Claude

Just type your message in any channel where the bot is present:

```
Explain this codebase
```

The bot will automatically send your message to Claude Code and stream the response back to Slack.

**Note**: You can also use the `/c` command if you prefer:

```
/c Explain this codebase
```

### Thread-Based Contexts

Each Slack thread automatically creates an independent Claude session:

```
# In main channel
How does authentication work?

# Start a thread (reply to any message)
└─> Let's refactor the auth module
```

**Key Features**:
- **Independent Sessions**: Each thread has its own Claude conversation context
- **Separate Working Directories**: Threads can work in different directories
- **Isolated Command History**: Thread commands don't affect the main channel
- **Thread Continuity**: All messages in a thread share the same session

**Usage**:
- Messages in the main channel use the channel-level session
- Messages in threads use thread-specific sessions
- `/clear` in a thread only clears that thread's session
- `/clear` in the channel only clears the channel-level session

### File Upload Support

Upload files directly to Slack and Claude can read and work with them:

```
# Upload a file by dragging and dropping into Slack
[Uploads: config.yaml]
"What does this configuration file do?"

# Claude receives the file and can read it
```

**Supported File Types**:
- **Code files**: .py, .js, .java, .go, etc.
- **Documents**: .txt, .md, .pdf, .docx
- **Images**: .png, .jpg, .gif (thumbnails shown in thread)
- **Data files**: .json, .yaml, .csv, .xml

**How it Works**:
1. Files are downloaded to `.slack_uploads/` in the session's working directory
2. File paths are automatically added to the prompt
3. Claude can read, analyze, and work with the uploaded files
4. Images show thumbnails for easy reference

**Configuration**:
- `MAX_FILE_SIZE_MB`: Maximum file size (default: 10MB)
- `MAX_UPLOAD_STORAGE_MB`: Total storage limit (default: 100MB)

### Smart Context Management

The bot automatically tracks files that Claude works with and includes them in future prompts:

**How it Works**:
- When Claude reads, edits, or creates a file, it's tracked
- Files with 2+ uses are automatically included in context
- Recently used files appear in prompts with usage stats
- Upload tracking ensures uploaded files stay in context

**Example**:
```
# First interaction
Edit src/auth.py to add logging

# Later interaction
Add more logging
# Claude automatically knows you're working with src/auth.py
```

**Context Display**:
```
[Recently accessed files in this session:]
- src/auth.py (modified 3x, 2m ago)
- src/database.py (read 5x, 10m ago)
- config.yaml (uploaded 1x, 15m ago)
```

### Git Integration

Perform local git operations directly from Slack:

**Check Status**:
```
/status
```

Shows git status with:
- Current branch
- Commits ahead/behind
- Staged changes
- Unstaged changes
- Untracked files

**View Changes**:
```
/diff                # Show uncommitted changes
/diff --staged       # Show staged changes only
```

Displays diff with syntax highlighting and truncation for large diffs.

**Commit Changes**:
```
/commit Fix authentication bug in login handler
```

Commits all staged changes with the provided message.

**Branch Management**:
```
/branch                    # Show current branch
/branch create feature-x   # Create and switch to new branch
/branch switch main        # Switch to existing branch
```

**Notes**:
- All git operations use your local git configuration
- No GitHub API integration - purely local git commands
- Works in any directory that's a git repository
- Non-git directories show appropriate error messages

### Session Management

Manage Claude sessions across channels and threads:

**List Sessions**:
```
/sessions
```

Shows all sessions for the current channel:
- Channel-level session
- All thread-level sessions
- Working directory for each
- Last active time

**Cleanup Inactive Sessions**:
```
/session-cleanup
```

Removes sessions that haven't been used in 30+ days to free up resources.

### Plan Mode

Use plan mode when you want to review Claude's implementation plan before execution:

```
/plan Add a new API endpoint for user authentication
```

How it works:
1. **Planning Phase**: Claude explores the codebase and creates a detailed implementation plan
2. **Review**: The plan is displayed in Slack with Approve/Reject buttons
3. **Execution Phase**: If approved, Claude executes the plan step by step
4. **Session Continuity**: Both phases share the same session context

Plan mode is ideal for:
- Complex implementations requiring careful planning
- Tasks where you want visibility into the approach before execution
- Learning how Claude approaches problems
- Situations where you want to review and approve before making changes

The plan approval times out after 10 minutes by default (configurable via `PLAN_APPROVAL_TIMEOUT`).

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
/pwd
```

Prints the current working directory.

```
/ls
```

Lists contents of the current working directory and displays the cwd path.

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
/context            # Visualize context usage
/model              # Show or change AI model
/resume             # Resume a previous session
/init               # Initialize project with CLAUDE.md
/memory             # Edit CLAUDE.md memory files
/review             # Request code review
/permissions        # View or update permissions
/stats              # Show usage stats and history
/todos              # List current TODO items
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
│   ├── exceptions.py       # Custom exception classes
│   ├── database/           # SQLite persistence
│   │   ├── models.py       # Data models (Session, UploadedFile, FileContext)
│   │   ├── migrations.py   # Schema setup
│   │   └── repository.py   # Data access
│   ├── claude/             # Claude CLI integration
│   │   ├── executor.py     # PTY-based execution
│   │   ├── streaming.py    # JSON stream parsing
│   │   └── subprocess_executor.py  # Subprocess-based execution with tool tracking
│   ├── git/                # Git integration
│   │   ├── service.py      # Git operations (status, diff, commit, branch)
│   │   └── models.py       # GitStatus data models
│   ├── pty/                # PTY session management
│   │   ├── session.py      # PTYSession class (pexpect)
│   │   ├── pool.py         # Session pool registry
│   │   ├── parser.py       # ANSI stripping, prompt detection
│   │   ├── process.py      # Process management utilities
│   │   └── types.py        # PTY type definitions
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
│   │   ├── plan_manager.py # PlanApprovalManager
│   │   └── slack_ui.py     # Approval button blocks
│   ├── tasks/              # Background task management
│   │   └── manager.py      # Task lifecycle and tracking
│   ├── handlers/           # Slack event handlers
│   │   ├── base.py         # Command decorator and context
│   │   ├── basic.py        # /c, /ls, /cd commands
│   │   ├── queue.py        # /q, /qv, /qc, /qr commands
│   │   ├── claude_cli.py   # Claude CLI passthrough commands
│   │   ├── git.py          # /diff, /status, /commit, /branch commands
│   │   ├── session_management.py  # /sessions, /session-cleanup commands
│   │   ├── agents.py       # /task, /tasks, /task-cancel
│   │   ├── budget.py       # /usage and /budget commands
│   │   ├── parallel.py     # /st, /cc commands
│   │   ├── pty.py          # /pty command
│   │   ├── plan.py         # /plan command
│   │   └── actions.py      # Button interactions
│   └── utils/              # Helpers
│       ├── formatting.py   # Slack Block Kit
│       ├── formatters/     # Specialized formatters
│       │   ├── base.py         # Base formatter classes
│       │   ├── command.py      # Command output formatting
│       │   ├── directory.py    # Directory listing formatting
│       │   ├── job.py          # Job status formatting
│       │   ├── plan.py         # Plan output formatting
│       │   ├── queue.py        # Queue status formatting
│       │   ├── session.py      # Session list formatting
│       │   ├── streaming.py    # Streaming output formatting
│       │   └── tool_blocks.py  # Tool usage block formatting
│       ├── file_downloader.py  # Slack file download service
│       ├── slack_helpers.py    # Slack API utilities
│       ├── streaming.py        # Output streaming utilities
│       └── validators.py       # Input validation
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
DEFAULT_WORKING_DIR=/path/to/projects  # Defaults to directory where server is started

# PTY Sessions
SESSION_IDLE_TIMEOUT=1800  # 30 minutes

# Command Execution
COMMAND_TIMEOUT=300  # 5 minutes max per command

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

# Claude Code Permission Mode
# approve-all: Auto-approve all file operations (recommended for personal use)
# prompt: Prompt for approval via Slack buttons
# deny: Deny all file operations (read-only mode)
CLAUDE_PERMISSION_MODE=approve-all

# Plan Mode
PLAN_APPROVAL_TIMEOUT=600  # 10 minutes

# File Upload
MAX_FILE_SIZE_MB=10  # Maximum file upload size
MAX_UPLOAD_STORAGE_MB=100  # Total storage limit for uploads
```

## Tips

- **Long outputs**: Responses exceeding Slack's limit are truncated. Use "View Output" for full text.
- **Streaming**: Responses update every 2 seconds during generation to avoid rate limits.
- **Sessions**: Each channel maintains a persistent PTY session with Claude Code.
- **Thread isolation**: Use threads to work on multiple separate tasks in the same channel.
- **File uploads**: Drag and drop files directly into Slack - Claude can read and work with them.
- **Smart context**: Files you work with frequently are automatically included in future prompts.
- **Git integration**: Use `/status`, `/diff`, `/commit`, and `/branch` for version control.
- **Timeouts**: Default 5-minute timeout. Set `COMMAND_TIMEOUT` to adjust.
- **Multi-agent tasks**: Use `/task` for complex work that benefits from planning and evaluation.
- **Night mode**: Higher usage thresholds at night allow more intensive work during off-hours.
- **Command queue**: Use `/q` to queue multiple commands that will execute sequentially.
- **Filesystem**: Use `/pwd` to see current directory, `/ls` to list contents, `/cd` to navigate directories.
- **Session management**: Use `/clear` to reset conversation and cancel processes, `/compact` to reduce context size.
- **Session cleanup**: Use `/session-cleanup` to remove inactive sessions and free up resources.

## Troubleshooting

**"Configuration errors" on startup**
- Ensure all required environment variables are set in `.env`

**Commands not appearing in Slack**
- Verify slash commands are created in your app settings
- Check the app is installed to your workspace

**"Working directory does not exist"**
- Use `/cd` to set a valid directory

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
