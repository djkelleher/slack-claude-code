# Codex app-server JSON-RPC Integration

This project runs Codex through:

```bash
codex app-server --listen stdio://
```

Implementation: `src/codex/subprocess_executor.py`

## Client Requests Sent by This App

1. `initialize`
2. `thread/start` or `thread/resume`
3. `turn/start`

`thread/start|resume` parameters sent:
- `cwd`
- `approvalPolicy` (`untrusted`, `on-request`, `never`)
- `sandbox` (`read-only`, `workspace-write`, `danger-full-access`)
- `model` (optional)

`turn/start` parameters sent:
- `threadId`
- `input` (text payload)
- `effort` (optional, parsed from model suffix like `-high`)

## Server Notifications Handled

- `thread/started`
- `item/agentMessage/delta`
- `item/plan/delta`
- `item/started`
- `item/completed`
- `turn/completed`
- `error`

These notifications are normalized into parser events consumed by
`src/codex/streaming.py`.

## Server Requests Handled

- `item/tool/requestUserInput`
- `item/commandExecution/requestApproval`
- `item/fileChange/requestApproval`
- `skill/requestApproval`
- `execCommandApproval`
- `applyPatchApproval`

Unsupported request methods receive JSON-RPC `-32601`.

## Approval Decision Mapping

Bridge helpers: `src/codex/approval_bridge.py`

- `skill/requestApproval` -> `approve|decline`
- `execCommandApproval` / `applyPatchApproval` -> `approved|denied`
- command/file approval methods -> `accept|decline`

If no interactive decision is available, defaults are based on approval mode:
- `never` -> accept/approve
- `on-request` or `untrusted` -> decline

## User Input Mapping

For `item/tool/requestUserInput`, answers are returned as:

```json
{
  "answers": {
    "<question_id>": { "answers": ["..."] }
  }
}
```

Formatting helpers live in `src/question/manager.py`.

## Session Resume Behavior

If `thread/resume` fails with a "thread/session not found" style error, the
executor automatically retries with a fresh `thread/start`.
