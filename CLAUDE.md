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

**agent_core.py** - Autonomous agent logic
- `agent_task_runner()` - Main execution loop running in eventlet greenthread
- Command generation via LLM with retry logic
- Supports multiple action types: COMMAND, SRCH, WRITE_FILE, ASK, TIMEOUT, REPORT
- Command validation (Independent mode uses LLM validator, Assisted mode requires user approval)
- History summarization when threshold exceeded
- Execution modes: Independent (auto-validate), Assisted (user approval), with optional ASK capability
- LangChain integration for search result and step output summarization (cloud providers)

**ssh_utils.py** - SSH operations
- `execute_ssh_command()` - Executes commands on remote system via paramiko
- ANSI escape sequence stripping for Windows compatibility
- Windows detection (uses `get_pty=False` for Windows, `True` for Unix)
- SSH key generation and deployment
- Sudo capability detection (skips on Windows)

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

**log_manager.py** - Unified logging architecture (NEW)
- `BaseLogManager` - Manages immutable full log in `execution_log.txt` (append-only)
- `ViewGenerator` - Creates different log views (Actions, Commands, VM Screen) from full log
- `AgentMemoryManager` - Manages LLM working memory in `execution_log_llm_context.txt`
- `UnifiedLogManager` - Facade providing unified interface for all logging operations
- `search_past_context()` - Enables agent to search execution history (SRCH capability)
- Performance optimizations: view truncation for large logs (5000 lines max)

### Frontend Architecture

**templates/index.html** - Main execution interface
- Real-time log display with keyword filtering
- Command timeout control with live update button
- Execution countdown display
- SocketIO event handlers for agent communication

**templates/layout.html** - Shared layout and modals
- Settings modals (Agent Config, System Config, Prompt Editor, Validator Prompt)
- Connection management (saved SSH connections)
- Manual summarization trigger

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
2. If COMMAND: Validation (Independent: LLM validator, Assisted: user approval)
3. Pager detection and prevention (adds `SYSTEMD_PAGER=cat PAGER=cat` prefix)
4. SSH execution with timeout (reads from global_state or ephemeral TIMEOUT value)
5. Retry logic (3 attempts) with SSH connectivity check
6. Result logged to full log and LLM context file
7. If SRCH: Search full log and add results to LLM context
8. If WRITE_FILE: Create file via echo/cat, log content to full log for future SRCH

**Timeout Management:**
- Stored in `global_state['command_timeout']` for live updates
- `CommandExecutionTimer` reads from global_state every second
- `execute_ssh_command_with_timeout()` reads current timeout at each retry
- Frontend update button instantly modifies global_state and config.ini

**Prompt System:**
- Multiple prompt templates stored in config.ini sections:
  - OllamaPrompt / CloudPrompt (standard - includes SRCH capability)
  - OllamaPromptWithAsk / CloudPromptWithAsk (with ASK capability)
  - OllamaValidatePrompt / CloudValidatePrompt (command validation)
  - OllamaSummarizePrompt / CloudSummarizePrompt (history compression)
  - OllamaStepSummaryPrompt / CloudStepSummaryPrompt (step output summarization - NEW)
  - OllamaSearchSummaryPrompt / CloudSearchSummaryPrompt (search results summarization - NEW)
- Note: CloudPrompt applies to both Gemini and Anthropic providers
- Variables available: `{objective}`, `{history}`, `{system_info}`, `{command}`, `{sudo_available}`, `{reason}`, `{summarization_threshold}`, `{command_timeout}`, `{output}`, `{results}`
- All prompts include SRCH documentation to enable history search capability

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

## Critical Implementation Details

**Docker Volume Mount Issue:**
- `session.json` must be a file, not directory
- Docker creates it as directory if doesn't exist on host before mount
- Fixed with runtime check in `session_manager.py` that auto-detects and fixes

**Windows SSH Compatibility:**
- ANSI escape sequences stripped via regex in `ssh_utils.py:strip_ansi_sequences()`
- Uses `get_pty=False` for Windows commands, `True` for Unix (detected via command keywords)
- Sudo detection skips Windows entirely

**LLM Response Parsing:**
- Expected format: `REASON: [text]\n[ACTION]`
- Supported actions:
  - `COMMAND: [command]` - Execute SSH command
  - `SRCH: [query]` - Search execution history
  - `WRITE_FILE: [path]\nCONTENT:\n[content]\nEND_CONTENT` - Create file
  - `ASK: [question]` - Request human input (if enabled)
  - `REPORT: [final report]` - Complete task
  - `TIMEOUT: [seconds]` - Adjust timeout (can combine with other actions)
- Validator format: `APPROVE` or `REJECT REASON: [reason]`
- Parser in `agent_core.py` handles whitespace variations and retries on invalid format
- SRCH results are summarized using LangChain when using cloud providers (Gemini/Anthropic)

**History Summarization:**
- Triggered when LLM context (execution_log_llm_context.txt) exceeds threshold (default 15000 chars)
- Preserves last 5 steps + critical entities
- Stores full backup in `full_history_backups` array in session.json
- Uses dedicated LLM call with summarization prompt
- Full Log (execution_log.txt) is never summarized - grows indefinitely

**WRITE_FILE Capability:**
- Agent can create files on remote system using `WRITE_FILE: <path>\nCONTENT:\n<content>\nEND_CONTENT`
- Implementation: Uses `echo` or `cat` with heredoc to write content via SSH
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
- `[Agent]` - model_name, max_steps, summarization_threshold, command_timeout, llm_timeout
- `[System]` - ip_address, username, ssh_port, ssh_key_path
- `[Ollama]` - api_url
- `[OllamaPrompt]`, `[CloudPrompt]`, `[OllamaValidatePrompt]`, `[CloudValidatePrompt]`, etc. - Prompt templates
- `[OllamaStepSummaryPrompt]`, `[CloudStepSummaryPrompt]` - Step output summarization prompts (NEW)
- `[OllamaSearchSummaryPrompt]`, `[CloudSearchSummaryPrompt]` - Search results summarization prompts (NEW)

**Persistent files:**
- `/app/keys/id_rsa` - SSH private key
- `/app/keys/id_rsa.pub` - SSH public key
- `/app/keys/config.ini` - Application configuration (moved from /app for persistence)
- `/app/keys/connections.json` - Saved SSH connection history
- `/app/keys/execution_log.txt` - Full immutable log (managed by BaseLogManager, append-only)
- `/app/keys/execution_log_llm_context.txt` - Agent working memory / LLM context (NEW)
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

**Windows output garbled:**
- ANSI sequences should be stripped automatically
- Check `strip_ansi_sequences()` being called
- Verify Windows detection working (looks for 'ver', 'w32tm', etc. in command)

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
- If special characters in content, check heredoc escaping in SSH command
- Windows vs Linux path differences (use forward slashes where possible)
