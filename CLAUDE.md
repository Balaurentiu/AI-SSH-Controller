# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI Agent Controller is a web-based system for executing commands on remote systems via SSH using LLM-powered autonomous agents. The agent can use Ollama (local), Google Gemini, or Anthropic Claude models and operates in multiple execution modes (Independent/Assisted). The agent has advanced capabilities including history search (SRCH), file creation (WRITE_FILE), and dynamic timeout adjustment.

## Build and Deploy Commands

**Build Docker image:**
```bash
docker build -t agent-controller .
```

**Run container (standard deployment):**
```bash
docker run -d --name agent-app -p 5000:5000 \
  -v /mnt/docker-data/PROJECTS/AI-Agent/keys:/app/keys \
  -v /mnt/docker-data/PROJECTS/AI-Agent/session.json:/app/session.json \
  agent-controller
```
Note: config.ini is now stored in the keys directory for persistence.

**Common rebuild workflow:**
```bash
docker build -t agent-controller . && \
docker stop agent-app && docker rm agent-app && \
docker run -d --name agent-app -p 5000:5000 \
  -v /mnt/docker-data/PROJECTS/AI-Agent/keys:/app/keys \
  -v /mnt/docker-data/PROJECTS/AI-Agent/session.json:/app/session.json \
  agent-controller
```

**Check logs:**
```bash
docker logs agent-app --tail 50
docker logs agent-app -f  # Follow logs
```

**Syntax check before building:**
```bash
python3 -m py_compile app.py agent_core.py ssh_utils.py config.py session_manager.py llm_utils.py log_manager.py
```

**Key Dependencies:**
- LangChain (v0.2.5) with langchain-core, langchain-community
- langchain-google-genai (v1.0.5) for Gemini support
- langchain-anthropic (>=0.1.0) for Claude/Anthropic support
- Flask + Flask-SocketIO + eventlet for web interface
- paramiko for SSH operations
- Pydantic v2 for data validation

## Architecture Overview

### Core Components

**app.py** - Flask application with SocketIO
- Global state management (`GLOBAL_STATE` dict)
- SocketIO event handlers for real-time communication
- Routes for configuration management (agent config, system config, prompts)
- Session persistence using `session_manager.py`
- **Dual LLM Configuration**:
  - Separate Chat LLM and Execution LLM support
  - Chat LLM reads API keys from `[General]` section (gemini_api_key, anthropic_api_key)
  - Ensures API key persistence across container rebuilds
- **Action Plan Management**:
  - `/update_action_plan` endpoint updates existing plans in-place (not creating new ones)
  - Proper edit/save functionality for plan steps

**agent_core.py** - Autonomous agent logic
- `agent_task_runner()` - Main execution loop running in eventlet greenthread
- `process_chat_message()` - Handles chat interactions with recursive search support
- Command generation via LLM with retry logic
- Supports multiple action types: COMMAND, SRCH, WRITE_FILE, ASK, TIMEOUT, REPORT
- Command validation (Independent mode uses LLM validator, Assisted mode requires user approval)
- History summarization when LLM context exceeds threshold (default: 15000 chars)
- Step output summarization when command output exceeds 30% of summarization threshold (cloud providers only)
- Execution modes: Independent (auto-validate), Assisted (user approval), with optional ASK capability
- LangChain integration for search result and step output summarization (cloud providers)
- **Enhanced OS Detection** (runs at task start):
  - Executes `uname -s 2>/dev/null || ver` for cross-platform detection
  - Detects Linux, macOS, Windows via output parsing (`Microsoft`, `Version` keywords)
  - Fallback to `echo %OS%` if primary detection fails
  - Parses Windows `whoami` format (`HOSTNAME\username`) to extract just username
  - Calls `set_detected_os()` to communicate OS to ssh_utils.py for proper PTY handling
  - Debug logging shows raw detection results
- **LLM Hallucination Prevention**:
  - Stop sequences in LLM invocations (max 5 for Gemini compatibility): `["Output:", "Observation:", "Result:", "\nOutput", "\nResult"]`
  - Post-processing sanitization filter removes hallucinated output markers
  - Case-insensitive matching for robustness
- **NEW**: Explicit action plan step completion via `<<MARK_STEP_COMPLETED: X>>` tags in chat
- **NEW**: Automatic completion prompt injection for chat-initiated searches

**ssh_utils.py** - SSH operations
- `execute_ssh_command()` - Executes commands on remote system via paramiko
- **Enhanced ANSI escape sequence stripping** for Windows compatibility:
  - Removes CSI sequences (colors, cursor movements)
  - Removes OSC Window Title sequences (fixes `0;C:\WINDOWS\system32\conhost.exe` artifacts)
  - Line-by-line fallback filtering for orphaned artifacts
- **OS-aware PTY handling**:
  - Global `DETECTED_OS` variable stores OS type ('windows', 'linux', 'macos')
  - `set_detected_os(os_type)` - Called by agent_core.py after OS detection
  - `get_detected_os()` - Returns current OS type
  - Uses `get_pty=False` for Windows, `True` for Unix (based on actual detected OS, not keyword guessing)
  - Fallback to keyword heuristic if OS not yet detected
- SSH key generation and deployment
- Sudo capability detection (skips on Windows)
- **Debug logging** for command execution (stdout/stderr lengths, PTY mode, exit status)

**config.py** - Configuration management
- Creates `config.ini` with defaults if missing
- Defines persistent paths:
  - KEYS_DIR, SESSION_FILE_PATH, CONFIG_FILE_PATH
  - EXECUTION_LOG_FILE_PATH (NEW)
  - EXECUTION_LOG_LLM_CONTEXT_PATH (NEW)
- Disables ConfigParser interpolation to handle special characters in prompts
- Default prompts now include SRCH capability documentation

**session_manager.py** - State persistence
- Saves/loads `session.json` (agent history, system info, VM output)
- No longer manages `execution_log.txt` - now handled by `UnifiedLogManager` (append-only)
- **Critical fix**: Auto-detects if `session.json` is a directory (Docker mount issue) and fixes it
- Imports and uses `UnifiedLogManager` from log_manager.py

**llm_utils.py** - LLM interaction utilities
- Handles Ollama, Gemini, and Anthropic Claude API calls
- Timeout handling and response parsing
- LangChain integration for advanced prompting
- Progressive LLM nudging: 5 retry attempts with escalating pressure on empty responses
- Smart retry system: Injects "SYSTEM ERROR" warnings to force LLM response

**log_manager.py** - Unified logging architecture
- `BaseLogManager` - Manages immutable full log in `execution_log.txt` (append-only)
- `ViewGenerator` - Creates different log views (Actions, Commands, VM Screen) from full log
  - **VM Screen View**: Parses username from System Info with Windows format support
  - Regex handles both `user: username` and `user: HOSTNAME\username` formats
  - Extracts just username for display (e.g., `aiadmin@192.168.0.192~#`)
- `AgentMemoryManager` - Manages LLM working memory in `execution_log_llm_context.txt`
- `UnifiedLogManager` - Facade providing unified interface for all logging operations
- `ActionPlanManager` - Manages multi-step action plans with stack structure
  - **NEW**: `mark_step_by_index(step_index)` - Explicit step completion by 1-based index (reliable)
  - `mark_step_completed(step_objective)` - Legacy fuzzy matching method (less reliable)
  - `get_plan_status()` - Returns formatted plan status for LLM prompt injection
  - `set_plan(title, steps)` - Creates new plan and pushes to stack
- `search_past_context()` - Enables agent to search execution history (SRCH capability)
- Performance optimizations: view truncation for large logs (5000 lines max)

### Frontend Architecture

**templates/index.html** - Main execution interface
- Real-time log display with keyword filtering
- Command timeout control with live update button
- Execution countdown display
- SocketIO event handlers for agent communication
- **Action Plan UI** (NEW):
  - Edit mode with view/edit state management (`isPlanEditMode` flag)
  - `renderPlanViewMode()` - Display read-only plan with progress
  - `renderPlanEditMode()` - Editable plan with add/delete/save functionality
  - Fullscreen toggle for better editing experience
  - Create-from-zero functionality (plan accessible even when none exists)
  - Locked completed steps (read-only, cannot delete)
  - Auto-refresh protection during editing (prevents interference)

**templates/layout.html** - Shared layout and modals
- Settings modals (Agent Config, System Config, Prompt Editor, Validator Prompt)
- Connection management (saved SSH connections)
- Manual summarization trigger
- **Action Plan Modal** (NEW):
  - Fullscreen button with proper positioning (⛶ icon)
  - Edit mode buttons (Edit Plan, Add Step, Save Changes, Clear Plan)
  - Responsive sizing (fixed delete buttons at 70px, flexible text inputs)
  - Overflow control to prevent horizontal scroll bars

**templates/history.html** - Agent persistent memory viewer

### Key Architectural Patterns

**State Management:**
- `GLOBAL_STATE` in app.py is shared across threads
- Critical values (command_timeout, summarization_threshold) stored in global_state for live updates
- New flags in GLOBAL_STATE:
  - `validator_enabled` - Master switch for LLM validator (default: True)
  - `human_search_pending` - Pauses agent execution during manual search operations
  - `sudo_available` - Auto-detected sudo capability on remote system
- Session data persisted to disk on task completion

**Real-time Communication:**
- SocketIO events for bidirectional communication
- Frontend → Backend: execute_task, stop_task, pause_task, update_timeout, approve_command
- Backend → Frontend: agent_log, vm_screen, command_exec_update, timeout_updated

**Agent Capabilities & Execution Flow:**

The agent can perform multiple actions each step:
- **COMMAND** - Execute SSH command on remote system
- **SRCH** - Search past execution history for specific information
- **WRITE_FILE** - Create files on remote system with specified content
- **ASK** - Request human input (if ASK mode enabled)
- **TIMEOUT** - Adjust command timeout for current step (ephemeral)
- **REPORT** - Provide final task completion report

**Command Execution Flow:**
1. LLM generates action with REASON (can include TIMEOUT adjustment)
2. If COMMAND: Validation (Independent: LLM validator with 10 retries, Assisted: user approval)
3. Pager detection and prevention (adds `SYSTEMD_PAGER=cat PAGER=cat` prefix)
4. SSH execution with timeout (reads from global_state or ephemeral TIMEOUT value)
5. Retry logic (3 attempts) with SSH connectivity check; **fail-fast on timeout** (if connection alive but command slow, abort retries immediately)
6. Result logged to full log and LLM context file
7. If SRCH: Search full log and add results to LLM context
8. If WRITE_FILE: Create file via Base64 encoding (OS-aware, sudo-aware), log content to full log for future SRCH

**Timeout Management:**
- Stored in `global_state['command_timeout']` for live updates
- `CommandExecutionTimer` reads from global_state every second
- `execute_ssh_command_with_timeout()` reads current timeout at each retry
- Frontend update button instantly modifies global_state and config.ini

**Prompt System:**
- Multiple prompt templates stored in config.ini sections:
  - **ChatPrompt** - Chat interface interactions (NEW - includes action plan completion instructions)
  - OllamaPrompt / CloudPrompt (standard - includes SRCH capability)
  - OllamaPromptWithAsk / CloudPromptWithAsk (with ASK capability)
  - OllamaValidatePrompt / CloudValidatePrompt (command validation)
  - OllamaSummarizePrompt / CloudSummarizePrompt (history compression)
  - OllamaStepSummaryPrompt / CloudStepSummaryPrompt (step output summarization)
  - OllamaSearchSummaryPrompt / CloudSearchSummaryPrompt (search results summarization)
- Note: CloudPrompt applies to both Gemini and Anthropic providers
- Legacy note: config.ini.new template contains GeminiPrompt sections for backwards compatibility, but the code uses CloudPrompt for both Gemini and Anthropic
- Variables available: `{objective}`, `{history}`, `{system_info}`, `{command}`, `{sudo_available}`, `{reason}`, `{summarization_threshold}`, `{command_timeout}`, `{output}`, `{results}`, `{action_plan_status}`, `{chat_history}`, `{user_message}`
- All prompts include SRCH documentation to enable history search capability
- **ChatPrompt includes explicit action plan completion instructions** (section 6)

**Logging Architecture & Dual-Memory System:**

The system maintains two separate memory stores:

1. **Full Log (execution_log.txt)** - Immutable append-only record:
   - Managed by `BaseLogManager` in log_manager.py
   - Contains complete execution history with all details
   - Format: Human-readable with structured sections (NEW TASK, STEP, COMMAND, OUTPUT, etc.)
   - Never modified or truncated - grows indefinitely
   - Used for audit trail and SRCH capability
   - File content written via WRITE_FILE is logged here for future searchability

2. **LLM Context (execution_log_llm_context.txt)** - Working memory:
   - Managed by `AgentMemoryManager` in log_manager.py
   - Contains only what the agent "sees" in its context window
   - Subject to summarization when threshold exceeded
   - Updated after each step with new commands/results
   - Can be manually edited by user for direct memory manipulation

**View Generation:**
- `ViewGenerator` creates real-time views from Full Log:
  - **Actions View** - High-level activity timeline (e.g., "Thinking...", "Executing command...")
  - **Commands View** - List of executed commands by step
  - **VM Screen View** - Terminal-like output showing commands and their outputs
- All views are generated on-demand via parsing, not stored separately
- Performance: Views truncated to 5000 lines max for browser rendering

**SRCH Capability:**
- Agent can search Full Log using `SRCH: <query>` action
- Search strips quotes/backticks from query for better matching
- Returns up to 50 matches with 2 lines context before/after
- Results are summarized via LangChain when using cloud providers
- Search results added to LLM context for current step
- Enables agent to recall information from earlier in session (even if summarized out)
- **NEW**: Chat-initiated searches automatically inject completion prompt
  - After search completes, `user_message` is modified to: "Search completed. The results have been added to the execution history. Please analyze the search results and provide relevant findings to the user."
  - Ensures LLM responds with analysis instead of just having results in context

**Action Plan Management:**
- **Explicit Tag-Based Completion** (NEW - Recommended):
  - LLM generates `<<MARK_STEP_COMPLETED: X>>` tag in chat responses
  - Detected by `agent_core.py:process_chat_message()` (line ~1684)
  - Calls `ActionPlanManager.mark_step_by_index(step_number)` for reliable completion
  - Tag removed from displayed message automatically
  - UI updates immediately via SocketIO event
  - **This is the PRIMARY method for marking steps** - more reliable than fuzzy matching

- **Legacy Fuzzy Matching** (Fallback):
  - `ActionPlanManager.mark_step_completed(step_objective)` matches by text similarity
  - Uses token matching with 50% threshold
  - Less reliable, can mark wrong steps
  - Still present for backwards compatibility

- **Chat Prompt Instructions**:
  - ChatPrompt section 6 teaches LLM when/how to use `<<MARK_STEP_COMPLETED: X>>`
  - Rules: Only mark ONE step at a time, only when task confirmed complete
  - LLM should immediately propose next step after marking one complete

- **Plan Stack Structure**:
  - Plans stored as JSON array/stack in `action_plan.json`
  - Supports nested sub-plans (push/pop operations)
  - Active plan is always top of stack (`stack[-1]`)
  - Each step has: `objective` (string), `completed` (boolean)

## Critical Implementation Details

**Docker Volume Mount Issue:**
- `session.json` must be a file, not directory
- Docker creates it as directory if doesn't exist on host before mount
- Fixed with runtime check in `session_manager.py` that auto-detects and fixes

**Windows SSH Compatibility:**
- **Enhanced ANSI stripping** in `ssh_utils.py:strip_ansi_sequences()`:
  - CSI pattern: `\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])` removes colors/cursor control
  - OSC pattern: `\x1B\]0;.*?(?:\x07|\x1B\\)` removes Window Title sequences
  - Line-by-line fallback removes orphaned artifacts like `0;C:\WINDOWS\system32\conhost.exe`
- **OS Detection & PTY Handling**:
  - agent_core.py detects OS at task start via `uname -s 2>/dev/null || ver`
  - Communicates detected OS to ssh_utils via `set_detected_os(detected_os)`
  - ssh_utils stores in global `DETECTED_OS` variable ('windows', 'linux', 'macos')
  - `execute_ssh_command()` uses actual detected OS (not keyword guessing) for PTY mode
  - Windows: `get_pty=False` (prevents ANSI artifacts)
  - Unix/Linux/macOS: `get_pty=True` (prevents pager blocking)
  - Fallback to keyword heuristic if OS not yet detected
- **Windows Username Parsing**:
  - Windows `whoami` returns `HOSTNAME\username` format
  - agent_core.py extracts just username part: `raw_user.split('\\')[-1]`
  - log_manager.py VM Screen parser handles both formats with regex
- Sudo detection skips Windows entirely

**LLM Response Parsing:**
- Expected format: `REASON: [text]\n[ACTION]`
- Supported actions in task execution:
  - `COMMAND: [command]` - Execute SSH command
  - `SRCH: [query]` - Search execution history
  - `WRITE_FILE: [path]\nCONTENT:\n[content]\nEND_CONTENT` - Create file
  - `ASK: [question]` - Request human input (if enabled)
  - `REPORT: [final report]` - Complete task
  - `TIMEOUT: [seconds]` - Adjust timeout (can combine with other actions)
- Supported tags in chat responses:
  - `<<MARK_STEP_COMPLETED: X>>` - Mark action plan step X as completed (NEW)
  - `<<REQUEST_TASK: [objective]>>` - Propose a new task to execute
  - `<<ACTION_PLAN_START>>...<plan>...<<ACTION_PLAN_STOP>>` - Create multi-step plan
  - `SRCH: [query]` - Search history (triggers recursive loop with completion prompt)
- Validator format: `APPROVE` or `REJECT REASON: [reason]`
- Parser in `agent_core.py` handles whitespace variations and retries on invalid format
- SRCH results are summarized using LangChain when using cloud providers (Gemini/Anthropic)
- All special tags are automatically removed from displayed messages to keep UI clean

**LLM Hallucination Prevention:**
- **Stop Sequences** (Gemini max: 5, applied to both Chat and Execution LLMs):
  - `["Output:", "Observation:", "Result:", "\nOutput", "\nResult"]`
  - Tells LLM to stop generation when it starts writing output
  - Applied via `llm.invoke(prompt, stop=stop_sequences)`
  - Fallback to no-stop invocation if model doesn't support stop parameter
- **Post-Processing Sanitization** (agent_core.py command parsing):
  - Case-insensitive detection: `marker.lower() in raw_cmd.lower()`
  - Markers: `["Output:", "Result:", "Observation:"]`
  - Truncates command at first output marker
  - Multi-line protection: checks each line for markers
  - Edge case: injects error message if command empty after filtering
- **Why**: LLMs sometimes hallucinate command outputs (e.g., `COMMAND: ls\nOutput: file1.txt file2.txt`)
- **Result**: Prevents execution of hallucinated output as part of command

**History Summarization:**
- Triggered when LLM context (execution_log_llm_context.txt) exceeds threshold (default 15000 chars)
- Preserves last 5 steps + critical entities
- Stores full backup in `full_history_backups` array in session.json
- Uses dedicated LLM call with summarization prompt
- Full Log (execution_log.txt) is never summarized - grows indefinitely

**WRITE_FILE Capability:**
- Agent can create files on remote system using `WRITE_FILE: <path>\nCONTENT:\n<content>\nEND_CONTENT`
- Implementation: Uses Base64 encoding for safe injection (prevents heredoc/escape issues)
- OS-aware: Uses PowerShell (`[Convert]::FromBase64String`) for Windows, bash/tee for Linux
- Sudo-aware: Automatically uses sudo for system paths when available
- File content is logged to Full Log for audit trail and future SRCH operations
- Enables agent to create config files, scripts, or documents
- Combined with SRCH, agent can recall exact content of files it created

**Log Filtering (Frontend):**
- `logFilterKeywords` in agent_core.py defines what appears in "Agent Execution Log (Live)"
- Keywords: `===`, `---`, `STEP`, `COMMAND:`, `Executing Command:`, `REASON:`, `REPORT:`, `ASK:`, `SRCH:`, etc.
- Full unfiltered log accessible via Full Log view or execution_log.txt file

**Performance Optimizations:**
- View truncation: All views limited to prevent browser performance issues
  - VM Screen: 5000 lines max
  - Actions View: 2000 lines max
  - Commands View: 2000 lines max
  - Full Log (UI display): 500KB max (full version available via download)
- Large command outputs can be summarized using step summary prompts
- Search results summarized when using cloud LLMs to reduce context size

## Configuration Structure

**config.ini sections:**
- `[General]` - provider (ollama/gemini/anthropic), gemini_api_key, anthropic_api_key
- `[Agent]` - model_name, max_steps, summarization_threshold, command_timeout, llm_timeout, chat_history_message_count
- `[System]` - ip_address, username, ssh_port, ssh_key_path
- `[Ollama]` - api_url
- `[ChatPrompt]` - Chat interface prompt (includes action plan completion instructions - section 6)
- `[OllamaPrompt]`, `[CloudPrompt]` - Task execution prompts
- `[OllamaPromptWithAsk]`, `[CloudPromptWithAsk]` - With ASK capability enabled
- `[OllamaValidatePrompt]`, `[CloudValidatePrompt]` - Command validation prompts
- `[OllamaSummarizePrompt]`, `[CloudSummarizePrompt]` - History compression prompts
- `[OllamaStepSummaryPrompt]`, `[CloudStepSummaryPrompt]` - Step output summarization prompts
- `[OllamaSearchSummaryPrompt]`, `[CloudSearchSummaryPrompt]` - Search results summarization prompts
- Note: Legacy `[GeminiPrompt]` sections exist in config.ini.new template for backwards compatibility, but code uses `[CloudPrompt]`

**Persistent files:**
- `/app/keys/id_rsa` - SSH private key
- `/app/keys/id_rsa.pub` - SSH public key
- `/app/keys/config.ini` - Application configuration (moved from /app for persistence)
- `/app/keys/connections.json` - Saved SSH connection history
- `/app/keys/execution_log.txt` - Full immutable log (managed by BaseLogManager, append-only)
- `/app/keys/execution_log_llm_context.txt` - Agent working memory / LLM context
- `/app/keys/chat_history.json` - Persistent chat conversation history
- `/app/keys/action_plan.json` - Active action plan stack (array of plans)
- `/app/session.json` - Agent persistent memory (history, system info)
- `/app/logs/base_log.jsonl` - Structured log in JSONL format (optional)
- `/app/logs/agent_memory.json` - Agent memory snapshot (optional)
- `/app/logs/execution_catalog.json` - Execution catalog (optional)

## Testing Approach

No automated tests present. Manual testing workflow:
1. Rebuild container
2. Check logs: `docker logs agent-app --tail 20`
3. Verify web interface accessible at http://localhost:5000
4. Test execution flow with simple objective
5. Monitor both "Agent Execution Log" and "VM Screen" output
6. Check persistent memory in History tab

## Common Debugging Scenarios

**Container won't start:**
- Check syntax: `python3 -m py_compile <file>.py`
- Check logs: `docker logs agent-app`
- Verify volumes mounted correctly

**Session.json errors:**
- Check if it's a file: `ls -la /path/to/session.json`
- If directory: `rm -rf session.json && echo '{}' > session.json`
- Runtime auto-fix handles this automatically now

**Commands timing out at old value:**
- Verify timeout stored in global_state during initialization
- Check timer reads from global_state dynamically
- Verify backend handler updates global_state immediately

**Windows output garbled or blank:**
- **Debug logging enabled**: Check docker logs for `[SSH_UTILS DEBUG]` messages
- **Raw output check**: Look for "Raw stdout length" - if 0, command produced no output
- **Strip check**: Compare "Raw stdout length" vs "After strip stdout length"
  - If raw has content but stripped is empty, ANSI stripping is too aggressive
  - Check for Window Title sequences being properly removed
- **PTY mode check**: Windows should use `get_pty=False`
  - Look for `[SSH_UTILS] OS type set to: windows` at task start
  - Verify `get_pty=False` in debug output
  - If PTY wrong, check OS detection succeeded
- **ANSI artifacts**: Lines starting with `0;` and containing `conhost.exe` or `:\` are filtered
- **OS detection**: Run new task to trigger detection, check logs for:
  - `[OS DETECTION DEBUG] os_result raw: '...'`
  - `[SSH_UTILS] OS type set to: windows`

**Agent gets stuck/blocked:**
- Likely pager issue - check if `PAGER=cat` prefix added
- Check command execution timeout not exceeded
- Verify SSH connectivity still working

**SRCH not finding results:**
- Check Full Log contains the information (view via Full Log tab)
- Search is case-insensitive but looks for substring matches
- Query is auto-stripped of quotes/backticks - no need to sanitize
- Verify search results appear in agent log with context lines

**Agent memory issues:**
- LLM context grows until summarization threshold hit (default 15000 chars)
- Full Log never truncated - available for SRCH indefinitely
- Manual memory editing: Edit execution_log_llm_context.txt directly or via History tab
- Check if summarization is working (look for "Summarization" in logs)

**Performance degradation (slow UI):**
- Full Log file too large for browser display (>500KB truncated automatically)
- VM Screen view truncated at 5000 lines for performance
- Download full session as ZIP to view complete logs
- Consider resetting logs if no longer needed (backs up automatically)

**WRITE_FILE not working:**
- Check file was created on remote system
- Verify file content logged to Full Log (search for "FILE CONTENT WRITTEN")
- If special characters in content, check Base64 encoding is working
- Windows vs Linux path differences (use forward slashes where possible)

**Action Plan steps not marking as completed:**
- **Primary method** (Recommended): LLM should use `<<MARK_STEP_COMPLETED: X>>` tag in chat
  - Check if tag is present in LLM response (look for the tag in logs)
  - Verify `agent_core.py:process_chat_message()` detects the tag (~line 1684)
  - Check `ActionPlanManager.mark_step_by_index()` is being called
  - Ensure UI receives SocketIO event `action_plan_data` after marking
- **Legacy method** (Fallback): Fuzzy text matching
  - Task objective must match step text with >50% token overlap
  - This method is unreliable - use explicit tags instead
- **ChatPrompt section 6** teaches LLM when to use the tag - verify it's present in config.ini
- **Check the plan exists**: View action_plan.json to verify plan structure

**OS Detection showing "Unknown":**
- **Check task start**: OS detection runs automatically at beginning of each task
- **Not during connection test**: "Test Connection" doesn't trigger OS detection
- **Debug logs**: Look for `[OS DETECTION DEBUG]` messages in docker logs
  - Should show: `os_result raw: 'Microsoft Windows [Version ...]'` for Windows
  - Should show: `os_result raw: 'Linux'` for Linux
- **Detection command**: `uname -s 2>/dev/null || ver` runs at task start
- **Fallback logic**: If first detection fails, tries `echo %OS%` on Windows
- **Communication check**: Verify `[SSH_UTILS] OS type set to: windows` appears
- **If still Unknown**: OS detection command may be failing - check SSH connectivity

**VM Screen showing wrong username (hostname instead):**
- **Windows format**: `whoami` returns `HOSTNAME\username` on Windows
- **Fix applied**: agent_core.py extracts username part after backslash
- **Backward compatibility**: log_manager.py regex handles both formats
- **Check logs**: System detection should show `User: aiadmin` not `User: win11\aiadmin`
- **Old logs**: Historical logs with old format still parsed correctly

**Chat LLM API key not persisting:**
- **Root cause**: Chat LLM was reading from `[ChatLLM].api_key` instead of `[General].gemini_api_key`
- **Fix applied**: Chat LLM now reads from same location as Execution LLM
- **Gemini**: Reads from `[General].gemini_api_key`
- **Anthropic**: Reads from `[General].anthropic_api_key`
- **Verification**: Check startup logs for `[CHAT LLM INIT] ✓ Chat LLM initialized`

**Action Plan edit/save not working:**
- **Root cause**: `/update_action_plan` was creating new plans instead of updating existing
- **Fix applied**: Endpoint now updates existing plan in-place
- **How to test**: Edit a step text, click Save, refresh - text should persist
- **Stack handling**: Uses `stack[-1]` to update active plan directly

**Action Plan UI issues:**
- **Edit mode getting reset**: Auto-refresh protection should prevent this
  - Check `isPlanEditMode` flag is being respected in `socket.on('action_plan_data')`
  - Check setInterval also respects the flag (~line 1211-1218 in index.html)
- **Modal positioning off-screen in fullscreen**:
  - Verify `modal.style.transform = 'none'` is set when entering fullscreen
  - Check fullscreen toggle function (`togglePlanFullscreen()`)
- **Horizontal scroll bars appearing**:
  - Ensure `overflow-x: hidden` on modal and content containers
  - Verify `box-sizing: border-box` on all input fields
  - Check delete buttons have fixed width (70px max-width)
- **Save fails with AttributeError**:
  - Modern version uses `_save_stack()` method, not `_save_plan()`
  - Check app.py `/update_action_plan` endpoint uses correct method
