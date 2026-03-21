"""Unit tests for plan mode formatter helpers."""

from src.utils.formatters import plan as plan_fmt


def test_plan_processing_message_escapes_and_truncates_prompt() -> None:
    """Prompt preview should be escaped and limited to 200 characters."""
    prompt = "<build & deploy> " + ("x" * 220)

    blocks = plan_fmt.plan_processing_message(prompt)

    assert blocks[0]["type"] == "context"
    prompt_text = blocks[0]["elements"][0]["text"]
    assert prompt_text.startswith("> &lt;build &amp; deploy&gt;")
    assert prompt_text.endswith("...")


def test_plan_ready_message_includes_approval_id() -> None:
    """Plan-ready message should surface the approval id."""
    blocks = plan_fmt.plan_ready_message(
        prompt="Review and apply changes",
        plan_preview="unused preview",
        approval_id="approval-123",
    )

    assert "approval-123" in blocks[2]["text"]["text"]
    assert "Plan created successfully" in blocks[2]["text"]["text"]


def test_plan_execution_update_uses_placeholder_without_output() -> None:
    """Execution update should show a placeholder before output exists."""
    blocks = plan_fmt.plan_execution_update(prompt="Run migration", current_output="")

    assert blocks[2]["text"]["text"] == ":gear: *Executing plan...*"
    assert blocks[3]["text"]["text"] == "_Starting execution..._"


def test_plan_execution_update_splits_output_and_adds_duration_footer() -> None:
    """Execution update should split long output and append duration metadata."""
    blocks = plan_fmt.plan_execution_update(
        prompt="Run test suite",
        current_output=("A" * 3100) + "\n" + ("B" * 100),
        duration_ms=1250,
    )

    output_sections = [block for block in blocks if block["type"] == "section"][1:]
    assert len(output_sections) >= 2
    assert blocks[-2]["type"] == "divider"
    assert blocks[-1]["elements"][0]["text"] == ":stopwatch: 1.2s"


def test_plan_execution_complete_handles_no_output_and_optional_footer_fields() -> None:
    """Completion message should render placeholders and only include provided metadata."""
    blocks = plan_fmt.plan_execution_complete(
        prompt="Ship release",
        output="",
        duration_ms=2500,
        cost_usd=0.5,
        command_id=7,
    )

    assert blocks[2]["text"]["text"] == "_No output_"
    footer = blocks[-1]["elements"][0]["text"]
    assert ":heavy_check_mark: Plan execution complete" in footer
    assert ":stopwatch: 2.5s" in footer
    assert ":moneybag: $0.5000" in footer
    assert ":memo: History #7" in footer


def test_plan_execution_complete_omits_falsey_optional_metadata() -> None:
    """Falsey optional values should not be rendered in the footer."""
    blocks = plan_fmt.plan_execution_complete(
        prompt="Do work",
        output="Done",
        duration_ms=0,
        cost_usd=0.0,
        command_id=0,
    )

    footer = blocks[-1]["elements"][0]["text"]
    assert footer == ":heavy_check_mark: Plan execution complete"
