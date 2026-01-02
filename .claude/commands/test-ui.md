# Run UI E2E Tests

Run the E2E tests for the danklab-ui package and analyze any failures.

## Instructions

1. Run the E2E tests using the test runner:
```bash
cd /home/dan/dev-repos/trading/danklab-ui
/home/dan/miniforge3/envs/trading/bin/python tests/e2e/run_tests.py $ARGUMENTS
```

2. If tests fail, check the error reports in `test_output/error_reports/` for detailed failure information including:
   - Browser console errors
   - Network failures
   - JS exceptions
   - Screenshots

3. Read the most recent error report JSON file to understand the failure:
```bash
ls -lt test_output/error_reports/ | head -5
```

4. For each failure, analyze:
   - The error type and message
   - Browser console errors that may indicate JS issues
   - Network failures that may indicate API problems
   - The screenshot to understand the visual state

5. If the error is in the test code, fix the test.
   If the error is in the application code, fix the application.

## Common Arguments

- `--headed`: Run with visible browser (useful for debugging)
- `-k <pattern>`: Run only tests matching pattern
- `--debug`: Enable tracing and video capture
- `--browser firefox`: Use Firefox instead of Chromium
