# Release v0.2.0

**Release Date:** 2026-02-02

## Summary

Major release with significant improvements to plan mode reliability, full-width message display using Slack rich_text blocks, a new configurable subagent system, and numerous bug fixes for slash commands and Slack integration.

## New Features

### Full-Width Message Display
- **Rich text blocks for Slack** - Messages now render at full width instead of ~50% width on desktop clients by using Slack's `rich_text` block type instead of `section` blocks with `mrkdwn`

### Configurable Subagent System
- **New `/agents` command** - View and manage available Claude Code subagents
- **Agent registry** - YAML-based configuration for custom agent definitions
- Removed legacy multi-agent orchestrator in favor of the new system

### Enhanced Plan Mode
- **Auto-execute plans** - Plans are automatically executed after approval
- **Improved plan file detection** - Better detection of plan files written by subagents
- **Session-specific plan files** - Plans are now stored with session-specific naming to prevent race conditions

### Slack Integration Improvements
- **Native table support** - Markdown tables are converted to native Slack table blocks
- **Snippet support** - Read and process pasted code snippets from Slack
- **Mobile message support** - Messages from mobile clients are now properly handled
- **Custom answers for questions** - Users can now provide custom text input for AskUserQuestion prompts

### Testing
- **Live integration tests** - New test suite with `--live` flag for testing against real Slack workspace

## Bug Fixes

### Plan Mode (15 fixes)
- Fix plan mode terminating before Task/Plan subagent results are captured
- Fix race conditions in plan mode and concurrent execution handling
- Fix plan file detection timing with retry mechanism (10 second timeout)
- Fix plan file displayed as binary instead of text snippet
- Fix dynamic plan mode switching not working from Slack
- Fix plan content capture to only use Plan subagent results
- Prioritize Write tool activity when finding plan files
- Look for plan files in `~/.claude/plans/` directory
- Extract plan file path from Task subagent results
- Remove redundant plain text plan display
- Don't use conversation output as plan content fallback
- Preserve markdown structure in flatten_text for plan display
- Use Claude's designated plan file and improve robustness
- Session-specific naming to prevent race conditions
- Detect Plan subagent completion to trigger approval

### Slash Commands (4 fixes)
- Fix `/permissions` command returning 'Unknown skill' error
- Fix `/stats` command returning 'Unknown skill' error
- Fix `/clear` command returning 'Unknown skill' error
- Fix `/doctor` and other slash commands returning no output

### Slack API (8 fixes)
- Remove deprecated `filetype` parameter from `files_upload_v2` calls
- Add retry logic for Slack API calls with network error handling
- Fix sessions table column order mismatch
- Don't HTML-escape content inside Slack code blocks
- Truncate long text in AskUserQuestion Slack blocks
- Escape Python dunder methods in Slack markdown conversion
- Only attach `.md` files with "plan" in name for plan approval
- Improve error handling and remove approval/question timeouts

### Other Fixes
- Increase readline timeout to 30 minutes for long-running operations
- Fix 21 code review issues: exceptions, timeouts, error handling
- Fix 6 code review issues: race conditions, error handling, validation
- Various import fixes and resource cleanup improvements

## Breaking Changes

- **Removed legacy multi-agent orchestrator** - The old orchestration system has been replaced with the new configurable subagent system via `/agents`

## Code Quality

- Removed unused `upload_text_file` function
- Consolidated `split_text_into_blocks` function with config constant
- Simplified plan approval UI to use file attachment only
- Simplified snippet handling by moving detection into `download_slack_file`
- Added `PLANS_DIR` constant for consistent plan file paths
- Fixed stale tests after multi-agent refactoring
- Added streaming module tests

## Installation

```bash
pip install slack-claude-code==0.2.0
```

Or with Poetry:
```bash
poetry add slack-claude-code@0.2.0
```

## Full Changelog

https://github.com/your-org/slack-claude-code/compare/v0.1.6...v0.2.0
