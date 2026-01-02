# Debug Test Failure

Analyze the most recent E2E test failure and help resolve it.

## Instructions

1. First, find the most recent error report:
```bash
ls -lt /home/dan/dev-repos/trading/danklab-ui/test_output/error_reports/*.json 2>/dev/null | head -1
```

2. Read the error report JSON to understand the failure. The report contains:
   - `test_name`: Which test failed
   - `error.type`: The exception type (AssertionError, TimeoutError, etc.)
   - `error.message`: The error message
   - `error.traceback`: Full stack trace
   - `browser_errors.console_errors`: JavaScript console errors
   - `browser_errors.js_exceptions`: Uncaught JS exceptions
   - `browser_errors.network_failures`: HTTP 4xx/5xx responses
   - `browser_errors.failed_requests`: Requests that failed entirely
   - `screenshot`: Path to failure screenshot
   - `page_url`: URL at time of failure

3. Based on the error type, investigate:

   **For TimeoutError (element not found):**
   - Check if the selector is correct
   - Check if the element is rendered (look at screenshot)
   - Check for JS errors that might prevent rendering

   **For AssertionError (expectation failed):**
   - Compare expected vs actual values
   - Check if the test expectations are correct
   - Check if the component behavior changed

   **For Network Failures:**
   - Check if the server is running
   - Check API endpoint paths
   - Check for CORS issues in console errors

   **For JS Exceptions:**
   - Look at the exception message
   - Find the source file in the component code
   - Check for null/undefined access

4. View the screenshot to understand visual state:
```bash
# The screenshot path is in the error report
```

5. After analysis, either:
   - Fix the test if the test is wrong
   - Fix the application code if there's a bug
   - Update selectors if the DOM structure changed

$ARGUMENTS
