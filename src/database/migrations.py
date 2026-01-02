import aiosqlite
from pathlib import Path

SCHEMA = """
-- Sessions table (one per channel)
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT UNIQUE NOT NULL,
    working_directory TEXT DEFAULT '~',
    claude_session_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Command history table
CREATE TABLE IF NOT EXISTS command_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    command TEXT NOT NULL,
    output TEXT,
    status TEXT DEFAULT 'pending',
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

-- Parallel jobs table
CREATE TABLE IF NOT EXISTS parallel_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    channel_id TEXT NOT NULL,
    job_type TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    config JSON,
    results JSON,
    aggregation_output TEXT,
    message_ts TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

-- PTY sessions tracking
CREATE TABLE IF NOT EXISTS pty_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT UNIQUE NOT NULL,
    channel_id TEXT NOT NULL,
    working_directory TEXT,
    state TEXT DEFAULT 'idle',
    pid INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_activity TIMESTAMP
);

-- Agent tasks for multi-agent workflow
CREATE TABLE IF NOT EXISTS agent_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    channel_id TEXT NOT NULL,
    description TEXT NOT NULL,
    priority TEXT DEFAULT 'thought',
    status TEXT DEFAULT 'pending',
    plan_output TEXT,
    work_output TEXT,
    eval_output TEXT,
    eval_status TEXT,
    slack_thread_ts TEXT,
    message_ts TEXT,
    turn_count INTEGER DEFAULT 0,
    max_turns INTEGER DEFAULT 50,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    error_message TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

-- Agent turn tracking
CREATE TABLE IF NOT EXISTS agent_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    agent_role TEXT NOT NULL,
    turn_number INTEGER NOT NULL,
    input_prompt TEXT,
    output_text TEXT,
    tool_calls TEXT,
    cost_usd REAL,
    duration_ms INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES agent_tasks(id)
);

-- Permission requests
CREATE TABLE IF NOT EXISTS permission_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_id TEXT UNIQUE NOT NULL,
    session_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    thread_ts TEXT,
    user_id TEXT,
    tool_name TEXT NOT NULL,
    tool_input TEXT,
    status TEXT DEFAULT 'pending',
    message_ts TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP,
    resolved_by TEXT
);

-- Usage snapshots
CREATE TABLE IF NOT EXISTS usage_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usage_percent REAL NOT NULL,
    reset_time TEXT,
    threshold_used REAL,
    is_paused INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Budget configuration
CREATE TABLE IF NOT EXISTS budget_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    threshold_day REAL DEFAULT 85.0,
    threshold_night REAL DEFAULT 95.0,
    night_start_hour INTEGER DEFAULT 22,
    night_end_hour INTEGER DEFAULT 6,
    pause_on_threshold INTEGER DEFAULT 1,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Hook events log
CREATE TABLE IF NOT EXISTS hook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    session_id TEXT,
    channel_id TEXT,
    event_data TEXT,
    handler_results TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Queue items for FIFO command queue
CREATE TABLE IF NOT EXISTS queue_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    channel_id TEXT NOT NULL,
    prompt TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    output TEXT,
    error_message TEXT,
    position INTEGER NOT NULL,
    message_ts TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_sessions_channel ON sessions(channel_id);
CREATE INDEX IF NOT EXISTS idx_history_session ON command_history(session_id);
CREATE INDEX IF NOT EXISTS idx_history_created ON command_history(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_session ON parallel_jobs(session_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON parallel_jobs(status);
CREATE INDEX IF NOT EXISTS idx_pty_sessions_channel ON pty_sessions(channel_id);
CREATE INDEX IF NOT EXISTS idx_pty_sessions_state ON pty_sessions(state);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_status ON agent_tasks(status);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_channel ON agent_tasks(channel_id);
CREATE INDEX IF NOT EXISTS idx_permission_requests_status ON permission_requests(status);
CREATE INDEX IF NOT EXISTS idx_hook_events_type ON hook_events(event_type);
CREATE INDEX IF NOT EXISTS idx_queue_items_status ON queue_items(status);
CREATE INDEX IF NOT EXISTS idx_queue_items_channel ON queue_items(channel_id);
CREATE INDEX IF NOT EXISTS idx_queue_items_position ON queue_items(channel_id, position);
"""


async def init_database(db_path: str) -> None:
    """Initialize the database with the schema."""
    # Ensure directory exists
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def reset_database(db_path: str) -> None:
    """Drop all tables and reinitialize (for development)."""
    async with aiosqlite.connect(db_path) as db:
        await db.executescript("""
            DROP TABLE IF EXISTS parallel_jobs;
            DROP TABLE IF EXISTS command_history;
            DROP TABLE IF EXISTS sessions;
        """)
        await db.commit()

    await init_database(db_path)
