# Slack Claude Code Bot - Implementation Plan

> **Status: COMPLETED** - This plan has been fully implemented. See README.md for current documentation.

## Overview
A Slack app that allows running Claude Code CLI commands from Slack, with each channel representing a separate session. The app runs locally on your laptop and uses Socket Mode for WebSocket-based communication (no public URL required).

## Implemented Features (January 2025)

The following advanced features have been implemented on top of the original plan:

- **PTY-based persistent sessions** (`src/pty/`) - Keep Claude Code running in interactive mode using pexpect
- **Multi-agent workflows** (`src/agents/`) - Planner → Worker → Evaluator pipeline for complex tasks
- **Usage budgeting** (`src/budget/`) - Time-aware thresholds (day/night) with `claude usage` integration
- **Permission approval** (`src/approval/`) - Handle MCP tool permissions via Slack buttons
- **Hook system** (`src/hooks/`) - Event-driven architecture for session events

New slash commands: `/task`, `/tasks`, `/task-cancel`, `/usage`, `/budget`, `/pty`

---

## Original Plan (November 2024)

## Architecture

### Core Components

```
┌─────────────────────────────────────────────────────────────────┐
│                         Slack App                                │
│  ┌─────────────┐    ┌──────────────┐    ┌────────────────────┐  │
│  │ Socket Mode │───▶│ Bolt Python  │───▶│ Command Dispatcher │  │
│  │  WebSocket  │    │  Framework   │    │                    │  │
│  └─────────────┘    └──────────────┘    └─────────┬──────────┘  │
│                                                    │             │
│  ┌─────────────────────────────────────────────────┼───────────┐ │
│  │                  Session Manager                │           │ │
│  │  ┌───────────────┐  ┌───────────────┐  ┌───────▼─────────┐ │ │
│  │  │ Channel #dev  │  │ Channel #prod │  │ Terminal Pool   │ │ │
│  │  │  Session A    │  │  Session B    │  │ (PTY instances) │ │ │
│  │  └───────────────┘  └───────────────┘  └─────────────────┘ │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │                    SQLite Database                          │ │
│  │  • Command History  • Session State  • Parallel Job Results │ │
│  └─────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

### Technology Stack
- **Language**: Python 3.10+
- **Slack SDK**: slack-bolt (Socket Mode)
- **Database**: SQLite with aiosqlite for async operations
- **Process Management**: asyncio subprocess for Claude CLI execution
- **CLI Interface**: Claude Code with `--print` and `--output-format stream-json`

## Database Schema

```sql
-- Sessions table (one per channel)
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY,
    channel_id TEXT UNIQUE NOT NULL,
    working_directory TEXT DEFAULT '~',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Command history table
CREATE TABLE command_history (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    command TEXT NOT NULL,
    output TEXT,
    status TEXT DEFAULT 'pending', -- pending, running, completed, failed
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

-- Parallel jobs table
CREATE TABLE parallel_jobs (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    job_type TEXT NOT NULL, -- 'parallel_analysis', 'sequential_loop'
    status TEXT DEFAULT 'pending',
    config JSON, -- stores n_instances, commands array, loop_count, etc.
    results JSON, -- stores outputs from each terminal
    aggregation_output TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
```

## Slack Commands & Interactions

### Slash Commands
| Command | Description |
|---------|-------------|
| `/claude <prompt>` | Run a Claude Code command in the current channel's session |
| `/claude-history` | Show paginated command history with "rerun" buttons |
| `/claude-parallel <n> <prompt>` | Run prompt in n terminals, then aggregate results |
| `/claude-sequence <json>` | Run array of commands sequentially |
| `/claude-loop <n> <json>` | Run command array n times |
| `/claude-status` | Show active jobs and their status |
| `/claude-cancel [job_id]` | Cancel running job(s) |
| `/claude-cwd <path>` | Set working directory for the session |

### Message Format Examples

**Command Output:**
```
🤖 Claude Code Response
━━━━━━━━━━━━━━━━━━━━━━
> Your prompt here

[Claude's response in formatted blocks]

⏱️ Completed in 12.3s | 📝 History #42
```

**Parallel Job Status:**
```
🔄 Parallel Analysis (3 terminals)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Terminal 1: ✅ Completed
Terminal 2: 🔄 Running...
Terminal 3: ⏳ Pending

[View Results] [Cancel]
```

**History View:**
```
📜 Command History (Page 1/5)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

#42 | 2 hours ago
> Explain this codebase
[Rerun] [View Output]

#41 | 3 hours ago
> Fix the login bug
[Rerun] [View Output]

[◀ Prev] [Next ▶]
```

## Key Features Implementation

### 1. Channel-Session Mapping
- Each Slack channel = one Claude Code session
- Session maintains its own working directory
- Command history is channel-specific
- Uses Claude's `--resume` flag to maintain conversation context

### 2. Command Execution
```python
async def execute_command(session_id: str, prompt: str) -> AsyncGenerator[str, None]:
    """Execute Claude CLI and stream output."""
    process = await asyncio.create_subprocess_exec(
        'claude',
        '--print',
        '--output-format', 'stream-json',
        prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=session.working_directory
    )
    # Stream and parse JSON chunks
    async for line in process.stdout:
        yield parse_stream_json(line)
```

### 3. Parallel Execution with Aggregation
```python
async def parallel_analysis(n: int, prompt: str, channel_id: str):
    """Run prompt in n terminals, then aggregate."""
    # 1. Create n subprocess tasks
    tasks = [execute_command(f"terminal_{i}", prompt) for i in range(n)]

    # 2. Wait for all to complete
    results = await asyncio.gather(*tasks)

    # 3. Create aggregation prompt
    aggregation_prompt = f"""Aggregate these analyses and create a plan:

{chr(10).join(f'--- Terminal {i+1} ---{chr(10)}{result}' for i, result in enumerate(results))}
"""

    # 4. Run aggregation in new terminal
    final_output = await execute_command("aggregation", aggregation_prompt)
    return final_output
```

### 4. Sequential Command Loop
```python
async def sequential_loop(commands: list[str], loop_count: int, channel_id: str):
    """Run commands sequentially, optionally looping."""
    all_outputs = []
    for loop_num in range(loop_count):
        for i, cmd in enumerate(commands):
            output = await execute_command(channel_id, cmd)
            all_outputs.append({
                'loop': loop_num + 1,
                'command_index': i + 1,
                'command': cmd,
                'output': output
            })
            # Send progress update to Slack
            await update_slack_progress(channel_id, loop_num, i, output)
    return all_outputs
```

### 5. Command History with Pagination
- Store all commands and outputs in SQLite
- Interactive buttons for pagination
- "Rerun" button re-executes the exact command
- "View Output" shows full output in thread/modal

## File Structure

```
slack-claude-code/
├── pyproject.toml           # Project dependencies (Poetry)
├── .env.example             # Environment variables template
├── README.md                # Setup instructions
├── src/
│   ├── __init__.py
│   ├── app.py               # Main Bolt app entry point
│   ├── config.py            # Configuration management
│   ├── database/
│   │   ├── __init__.py
│   │   ├── models.py        # SQLAlchemy/dataclass models
│   │   ├── migrations.py    # DB initialization
│   │   └── repository.py    # Data access layer
│   ├── claude/
│   │   ├── __init__.py
│   │   ├── executor.py      # Claude CLI subprocess management
│   │   ├── session.py       # Session state management
│   │   └── streaming.py     # Stream JSON parsing
│   ├── handlers/
│   │   ├── __init__.py
│   │   ├── commands.py      # Slash command handlers
│   │   ├── actions.py       # Button/interaction handlers
│   │   └── events.py        # Slack event handlers
│   └── utils/
│       ├── __init__.py
│       ├── formatting.py    # Slack message formatting
│       └── validators.py    # Input validation
└── tests/
    ├── __init__.py
    ├── test_executor.py
    ├── test_handlers.py
    └── test_database.py
```

## Setup Requirements

### Slack App Configuration
1. Create app at https://api.slack.com/apps
2. Enable Socket Mode (Settings > Socket Mode)
3. Add Bot Token Scopes:
   - `chat:write` - Send messages
   - `commands` - Handle slash commands
   - `channels:history` - Read channel messages
   - `app_mentions:read` - Respond to @mentions
4. Create slash commands (listed above)
5. Install to workspace

### Environment Variables
```bash
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...  # For Socket Mode
SLACK_SIGNING_SECRET=...
DATABASE_PATH=./data/slack_claude.db
DEFAULT_WORKING_DIR=/home/dan/projects
```

## Implementation Steps

1. **Project Setup**
   - Initialize Python project with Poetry
   - Configure dependencies (slack-bolt, aiosqlite, python-dotenv)
   - Set up project structure

2. **Database Layer**
   - Implement SQLite schema and migrations
   - Create data access repository

3. **Claude CLI Executor**
   - Implement async subprocess management
   - Parse stream-json output format
   - Handle errors and timeouts

4. **Core Slack Handlers**
   - `/claude` command - basic prompt execution
   - Message formatting and threading
   - Error handling and user feedback

5. **Session Management**
   - Channel-to-session mapping
   - Working directory management
   - Session persistence

6. **Command History**
   - History storage and retrieval
   - Pagination UI with Block Kit
   - Rerun functionality

7. **Advanced Features**
   - Parallel execution with aggregation
   - Sequential command loops
   - Job status tracking and cancellation

8. **Testing & Documentation**
   - Unit tests for core components
   - Integration tests with mock Slack
   - Setup documentation

## Notes

- **Streaming**: The app will send initial "Processing..." message, then update it as output streams in (to avoid rate limits, updates are batched every 2-3 seconds)
- **Long outputs**: Outputs exceeding Slack's 3000 char limit will be split into threaded replies or uploaded as snippets
- **Timeouts**: Default 5-minute timeout per command, configurable per session
- **Rate Limiting**: Respects Slack's rate limits with exponential backoff
