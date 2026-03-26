from pathlib import Path

import aiosqlite

SCHEMA = """
-- Sessions table (one per channel or thread)
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    thread_ts TEXT DEFAULT NULL,
    working_directory TEXT DEFAULT '~',
    claude_session_id TEXT,
    permission_mode TEXT DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    model TEXT DEFAULT NULL,
    added_dirs TEXT DEFAULT NULL,  -- JSON array of directories added via /add-dir
    -- Codex-specific fields
    codex_session_id TEXT DEFAULT NULL,
    sandbox_mode TEXT DEFAULT 'danger-full-access',
    approval_mode TEXT DEFAULT 'on-request'
);

-- Command history table
CREATE TABLE IF NOT EXISTS command_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    command TEXT NOT NULL,
    output TEXT,
    detailed_output TEXT,
    git_diff_summary TEXT,
    git_diff_output TEXT,
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

-- Queue items for FIFO command queue
CREATE TABLE IF NOT EXISTS queue_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    channel_id TEXT NOT NULL,
    thread_ts TEXT DEFAULT NULL,
    prompt TEXT NOT NULL,
    working_directory_override TEXT DEFAULT NULL,
    parallel_group_id TEXT DEFAULT NULL,
    parallel_limit INTEGER DEFAULT NULL,
    status TEXT DEFAULT 'pending',
    output TEXT,
    error_message TEXT,
    position INTEGER NOT NULL,
    message_ts TEXT,
    automation_meta TEXT DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

-- Uploaded files tracking
CREATE TABLE IF NOT EXISTS uploaded_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    slack_file_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    mimetype TEXT,
    size INTEGER,
    local_path TEXT NOT NULL,
    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_referenced TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    UNIQUE(session_id, slack_file_id)
);

-- Git checkpoints for version control
CREATE TABLE IF NOT EXISTS git_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    channel_id TEXT NOT NULL,
    name TEXT NOT NULL,
    stash_ref TEXT NOT NULL,
    stash_message TEXT,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_auto BOOLEAN DEFAULT 0,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

-- Notification settings per channel (enabled by default)
CREATE TABLE IF NOT EXISTS notification_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL UNIQUE,
    notify_on_completion INTEGER DEFAULT 1,
    notify_on_permission INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Queue execution controls per channel/thread scope
CREATE TABLE IF NOT EXISTS queue_controls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    thread_ts TEXT DEFAULT NULL,
    state TEXT DEFAULT 'running',
    auto_finish_pending INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Scheduled queue controls per channel/thread scope
CREATE TABLE IF NOT EXISTS queue_scheduled_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    thread_ts TEXT DEFAULT NULL,
    action TEXT NOT NULL,
    execute_at TIMESTAMP NOT NULL,
    status TEXT DEFAULT 'pending',
    error_message TEXT DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    executed_at TIMESTAMP DEFAULT NULL
);

-- Workspace leases for concurrent execution isolation
CREATE TABLE IF NOT EXISTS workspace_leases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    channel_id TEXT NOT NULL,
    thread_ts TEXT DEFAULT NULL,
    session_scope TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    repo_root TEXT DEFAULT NULL,
    target_worktree_path TEXT DEFAULT NULL,
    target_branch TEXT DEFAULT NULL,
    leased_root TEXT NOT NULL,
    leased_cwd TEXT NOT NULL,
    base_cwd TEXT NOT NULL,
    relative_subdir TEXT DEFAULT NULL,
    lease_kind TEXT NOT NULL DEFAULT 'direct',
    worktree_name TEXT DEFAULT NULL,
    worktree_origin TEXT DEFAULT NULL,
    merge_status TEXT DEFAULT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    released_at TIMESTAMP DEFAULT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_sessions_channel ON sessions(channel_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_channel_thread
ON sessions(channel_id, COALESCE(thread_ts, ''));
CREATE INDEX IF NOT EXISTS idx_sessions_thread ON sessions(thread_ts) WHERE thread_ts IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_history_session ON command_history(session_id);
CREATE INDEX IF NOT EXISTS idx_history_created ON command_history(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_session ON parallel_jobs(session_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON parallel_jobs(status);
CREATE INDEX IF NOT EXISTS idx_queue_items_status ON queue_items(status);
CREATE INDEX IF NOT EXISTS idx_queue_items_channel ON queue_items(channel_id);
CREATE INDEX IF NOT EXISTS idx_queue_items_position ON queue_items(channel_id, position);
CREATE INDEX IF NOT EXISTS idx_queue_items_scope_status ON queue_items(channel_id, thread_ts, status);
CREATE INDEX IF NOT EXISTS idx_queue_items_scope_position ON queue_items(channel_id, thread_ts, position);
CREATE INDEX IF NOT EXISTS idx_uploaded_files_session ON uploaded_files(session_id);
CREATE INDEX IF NOT EXISTS idx_git_checkpoints_channel ON git_checkpoints(channel_id);
CREATE INDEX IF NOT EXISTS idx_git_checkpoints_session ON git_checkpoints(session_id);
CREATE INDEX IF NOT EXISTS idx_notification_settings_channel ON notification_settings(channel_id);
CREATE INDEX IF NOT EXISTS idx_queue_controls_scope ON queue_controls(channel_id, thread_ts);
CREATE INDEX IF NOT EXISTS idx_queue_scheduled_events_due ON queue_scheduled_events(status, execute_at);
CREATE INDEX IF NOT EXISTS idx_queue_scheduled_events_scope ON queue_scheduled_events(channel_id, thread_ts, status);
CREATE INDEX IF NOT EXISTS idx_workspace_leases_execution ON workspace_leases(execution_id);
CREATE INDEX IF NOT EXISTS idx_workspace_leases_scope ON workspace_leases(channel_id, thread_ts, status);
CREATE INDEX IF NOT EXISTS idx_workspace_leases_repo ON workspace_leases(repo_root, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_leases_active_root
ON workspace_leases(leased_root)
WHERE status = 'active' AND released_at IS NULL;
"""


async def init_database(db_path: str) -> None:
    """Initialize the database with the schema."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA)
        await db.commit()
        # Run migrations for existing databases
        await _run_migrations(db)


async def _add_column_if_missing(
    db: aiosqlite.Connection, column_names: list[str], column_name: str, ddl: str
) -> None:
    """Add a column and commit when it does not already exist."""
    if column_name in column_names:
        return
    await db.execute(ddl)
    await db.commit()
    column_names.append(column_name)


async def _run_migrations(db: aiosqlite.Connection) -> None:
    """Run any necessary migrations for schema updates."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS queue_controls (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               channel_id TEXT NOT NULL,
               thread_ts TEXT DEFAULT NULL,
               state TEXT DEFAULT 'running',
               auto_finish_pending INTEGER DEFAULT 0,
               created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS queue_scheduled_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               channel_id TEXT NOT NULL,
               thread_ts TEXT DEFAULT NULL,
               action TEXT NOT NULL,
               execute_at TIMESTAMP NOT NULL,
               status TEXT DEFAULT 'pending',
               error_message TEXT DEFAULT NULL,
               created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               executed_at TIMESTAMP DEFAULT NULL
           )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS workspace_leases (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               session_id INTEGER NOT NULL,
               channel_id TEXT NOT NULL,
               thread_ts TEXT DEFAULT NULL,
               session_scope TEXT NOT NULL,
               execution_id TEXT NOT NULL,
               repo_root TEXT DEFAULT NULL,
               target_worktree_path TEXT DEFAULT NULL,
               target_branch TEXT DEFAULT NULL,
               leased_root TEXT NOT NULL,
               leased_cwd TEXT NOT NULL,
               base_cwd TEXT NOT NULL,
               relative_subdir TEXT DEFAULT NULL,
               lease_kind TEXT NOT NULL DEFAULT 'direct',
               worktree_name TEXT DEFAULT NULL,
               worktree_origin TEXT DEFAULT NULL,
               merge_status TEXT DEFAULT NULL,
               status TEXT NOT NULL DEFAULT 'active',
               created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               released_at TIMESTAMP DEFAULT NULL,
               FOREIGN KEY (session_id) REFERENCES sessions(id)
           )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_workspace_leases_execution "
        "ON workspace_leases(execution_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_workspace_leases_scope "
        "ON workspace_leases(channel_id, thread_ts, status)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_workspace_leases_repo "
        "ON workspace_leases(repo_root, status)"
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_leases_active_root "
        "ON workspace_leases(leased_root) "
        "WHERE status = 'active' AND released_at IS NULL"
    )

    # Check if model column exists in sessions table
    cursor = await db.execute("PRAGMA table_info(sessions)")
    columns = await cursor.fetchall()
    column_names = [col[1] for col in columns]

    await _add_column_if_missing(
        db,
        column_names,
        "model",
        "ALTER TABLE sessions ADD COLUMN model TEXT DEFAULT NULL",
    )
    await _add_column_if_missing(
        db,
        column_names,
        "added_dirs",
        "ALTER TABLE sessions ADD COLUMN added_dirs TEXT DEFAULT NULL",
    )
    await _add_column_if_missing(
        db,
        column_names,
        "codex_session_id",
        "ALTER TABLE sessions ADD COLUMN codex_session_id TEXT DEFAULT NULL",
    )
    await _add_column_if_missing(
        db,
        column_names,
        "sandbox_mode",
        "ALTER TABLE sessions ADD COLUMN sandbox_mode TEXT DEFAULT 'danger-full-access'",
    )
    await _add_column_if_missing(
        db,
        column_names,
        "approval_mode",
        "ALTER TABLE sessions ADD COLUMN approval_mode TEXT DEFAULT 'on-request'",
    )

    history_cursor = await db.execute("PRAGMA table_info(command_history)")
    history_columns = await history_cursor.fetchall()
    history_column_names = [col[1] for col in history_columns]
    await _add_column_if_missing(
        db,
        history_column_names,
        "detailed_output",
        "ALTER TABLE command_history ADD COLUMN detailed_output TEXT",
    )
    await _add_column_if_missing(
        db,
        history_column_names,
        "git_diff_summary",
        "ALTER TABLE command_history ADD COLUMN git_diff_summary TEXT",
    )
    await _add_column_if_missing(
        db,
        history_column_names,
        "git_diff_output",
        "ALTER TABLE command_history ADD COLUMN git_diff_output TEXT",
    )

    # Add queue_items.thread_ts for thread-scoped queueing
    queue_cursor = await db.execute("PRAGMA table_info(queue_items)")
    queue_columns = await queue_cursor.fetchall()
    queue_column_names = [col[1] for col in queue_columns]
    await _add_column_if_missing(
        db,
        queue_column_names,
        "thread_ts",
        "ALTER TABLE queue_items ADD COLUMN thread_ts TEXT DEFAULT NULL",
    )
    await _add_column_if_missing(
        db,
        queue_column_names,
        "working_directory_override",
        "ALTER TABLE queue_items ADD COLUMN working_directory_override TEXT DEFAULT NULL",
    )
    await _add_column_if_missing(
        db,
        queue_column_names,
        "parallel_group_id",
        "ALTER TABLE queue_items ADD COLUMN parallel_group_id TEXT DEFAULT NULL",
    )
    await _add_column_if_missing(
        db,
        queue_column_names,
        "parallel_limit",
        "ALTER TABLE queue_items ADD COLUMN parallel_limit INTEGER DEFAULT NULL",
    )
    await _add_column_if_missing(
        db,
        queue_column_names,
        "automation_meta",
        "ALTER TABLE queue_items ADD COLUMN automation_meta TEXT DEFAULT NULL",
    )

    queue_control_cursor = await db.execute("PRAGMA table_info(queue_controls)")
    queue_control_columns = await queue_control_cursor.fetchall()
    queue_control_column_names = [col[1] for col in queue_control_columns]
    await _add_column_if_missing(
        db,
        queue_control_column_names,
        "auto_finish_pending",
        "ALTER TABLE queue_controls ADD COLUMN auto_finish_pending INTEGER DEFAULT 0",
    )

    # Ensure queue scope indexes exist for channel+thread isolation
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_queue_items_scope_status ON queue_items(channel_id, thread_ts, status)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_queue_items_scope_position ON queue_items(channel_id, thread_ts, position)"
    )

    # Normalize historical blank thread scopes to NULL so scope matching is stable.
    await db.execute(
        "UPDATE sessions SET thread_ts = NULL WHERE TRIM(COALESCE(thread_ts, '')) = ''"
    )
    await db.execute(
        """
        DELETE FROM sessions
        WHERE id IN (
            SELECT id
            FROM (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY channel_id, COALESCE(thread_ts, '')
                        ORDER BY
                            (CASE WHEN model IS NOT NULL THEN 1 ELSE 0 END) DESC,
                            (
                                (CASE WHEN model IS NOT NULL THEN 1 ELSE 0 END) +
                                (CASE WHEN codex_session_id IS NOT NULL THEN 1 ELSE 0 END) +
                                (CASE WHEN claude_session_id IS NOT NULL THEN 1 ELSE 0 END) +
                                (CASE WHEN permission_mode IS NOT NULL THEN 1 ELSE 0 END)
                            ) DESC,
                            last_active DESC,
                            id DESC
                    ) AS row_num
                FROM sessions
            ) ranked_sessions
            WHERE row_num > 1
        )
        """
    )
    await db.execute("DROP INDEX IF EXISTS idx_sessions_channel_thread")
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_channel_thread "
        "ON sessions(channel_id, COALESCE(thread_ts, ''))"
    )
    await db.execute(
        "UPDATE queue_items SET thread_ts = NULL WHERE TRIM(COALESCE(thread_ts, '')) = ''"
    )
    await db.execute(
        "UPDATE queue_items SET working_directory_override = NULL "
        "WHERE TRIM(COALESCE(working_directory_override, '')) = ''"
    )
    await db.execute(
        "UPDATE queue_items SET parallel_group_id = NULL "
        "WHERE TRIM(COALESCE(parallel_group_id, '')) = ''"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_queue_controls_scope ON queue_controls(channel_id, thread_ts)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_queue_scheduled_events_due "
        "ON queue_scheduled_events(status, execute_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_queue_scheduled_events_scope "
        "ON queue_scheduled_events(channel_id, thread_ts, status)"
    )
    await db.execute(
        "UPDATE queue_controls SET thread_ts = NULL WHERE TRIM(COALESCE(thread_ts, '')) = ''"
    )
    await db.execute(
        "UPDATE queue_scheduled_events SET thread_ts = NULL "
        "WHERE TRIM(COALESCE(thread_ts, '')) = ''"
    )
    await db.commit()


async def reset_database(db_path: str) -> None:
    """Drop all tables and reinitialize (for development)."""
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(
            """
            DROP TABLE IF EXISTS notification_settings;
            DROP TABLE IF EXISTS queue_scheduled_events;
            DROP TABLE IF EXISTS queue_controls;
            DROP TABLE IF EXISTS git_checkpoints;
            DROP TABLE IF EXISTS uploaded_files;
            DROP TABLE IF EXISTS queue_items;
            DROP TABLE IF EXISTS parallel_jobs;
            DROP TABLE IF EXISTS command_history;
            DROP TABLE IF EXISTS sessions;
        """
        )
        await db.commit()

    await init_database(db_path)
