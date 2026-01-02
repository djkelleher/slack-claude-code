from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import json


@dataclass
class Session:
    id: Optional[int] = None
    channel_id: str = ""
    working_directory: str = "~"
    claude_session_id: Optional[str] = None  # For --resume flag
    created_at: datetime = field(default_factory=datetime.now)
    last_active: datetime = field(default_factory=datetime.now)

    @classmethod
    def from_row(cls, row: tuple) -> "Session":
        return cls(
            id=row[0],
            channel_id=row[1],
            working_directory=row[2],
            claude_session_id=row[3],
            created_at=datetime.fromisoformat(row[4]) if row[4] else datetime.now(),
            last_active=datetime.fromisoformat(row[5]) if row[5] else datetime.now(),
        )


@dataclass
class CommandHistory:
    id: Optional[int] = None
    session_id: int = 0
    command: str = ""
    output: Optional[str] = None
    status: str = "pending"  # pending, running, completed, failed, cancelled
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: tuple) -> "CommandHistory":
        return cls(
            id=row[0],
            session_id=row[1],
            command=row[2],
            output=row[3],
            status=row[4],
            error_message=row[5],
            created_at=datetime.fromisoformat(row[6]) if row[6] else datetime.now(),
            completed_at=datetime.fromisoformat(row[7]) if row[7] else None,
        )


@dataclass
class ParallelJob:
    id: Optional[int] = None
    session_id: int = 0
    channel_id: str = ""
    job_type: str = ""  # parallel_analysis, sequential_loop
    status: str = "pending"  # pending, running, completed, failed, cancelled
    config: dict = field(default_factory=dict)  # n_instances, commands, loop_count
    results: list = field(default_factory=list)  # outputs from each terminal
    aggregation_output: Optional[str] = None
    message_ts: Optional[str] = None  # Slack message timestamp for updates
    created_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: tuple) -> "ParallelJob":
        return cls(
            id=row[0],
            session_id=row[1],
            channel_id=row[2],
            job_type=row[3],
            status=row[4],
            config=json.loads(row[5]) if row[5] else {},
            results=json.loads(row[6]) if row[6] else [],
            aggregation_output=row[7],
            message_ts=row[8],
            created_at=datetime.fromisoformat(row[9]) if row[9] else datetime.now(),
            completed_at=datetime.fromisoformat(row[10]) if row[10] else None,
        )
