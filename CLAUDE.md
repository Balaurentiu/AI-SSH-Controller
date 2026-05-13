# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Important Rules for Claude

**DO NOT build or run Docker containers.** The user handles all building and testing. Claude should only:
- Make code changes
- Run syntax checks: `python3 -m py_compile <file>.py`
- Explain what was changed

## Project Overview

AI Agent Controller is a web-based system for executing commands on remote systems via SSH using LLM-powered autonomous agents (Ollama/Gemini/Anthropic). Features include:
- Multiple execution modes (Independent/Assisted)
- History search (SRCH), file creation (WRITE_FILE), dynamic timeout
- REST API for programmatic control (6 async endpoints)
- Multi-system orchestration with guarded system switching
- Network device support (Cisco, Brocade, Juniper, Arista) with Block Execution mode
- Multiple authentication methods (key, password, key_or_password fallback)
- Legacy SSH algorithm support for older devices
- Dual LLM configuration (separate LLMs for execution vs chat)
- Connections import/export
- Knowledge management (document upload, embedding, vector search via KNOWLEDGE: action)
- Web search module (autonomous DuckDuckGo search with dedicated LLM)
- Command abort (kill running command + pause execution without stopping task)
- Chat & knowledge export (PDF/DOCX with markdown rendering, dark/light themes)

## Architecture (Summary)

### Core Files
| File | Purpose |
|------|---------|
| `app.py` | Flask + SocketIO, routes, global state, REST API endpoints, system switch handling |
| `agent_core.py` | Main agent logic, LLM interaction, action parsing, API task registry |
| `ssh_utils.py` | SSH operations via paramiko, OS-aware PTY, legacy algorithms, block command execution |
| `log_manager.py` | Dual logging (full log + LLM context), action plans, block command formatting |
| `config.py` | Configuration paths, config.ini management, section backfill |
| `session_manager.py` | State persistence (session.json), ZIP backup/restore |
| `llm_utils.py` | LLM API calls (Ollama/Gemini/Anthropic), dynamic model listing |
| `knowledge_manager.py` | Document upload, embedding (Ollama/Gemini), vector search (ChromaDB/FAISS), OCR |
| `web_search_module.py` | Autonomous web research: DuckDuckGo search, page fetch, LLM evaluation, knowledge attachment |
| `chat_export.py` | Chat message export: PDF (xhtml2pdf) and DOCX (python-docx) with markdown rendering, dark/light themes |

### Templates
| File | Purpose |
|------|---------|
| `templates/layout.html` | Base template, styling, modals (system config, prompts, web search config, switch confirmation, knowledge viewer, chat export) |
| `templates/index.html` | Main UI: execution control, chat interface, live view, API task indicator, web search status, chat export selection |
| `templates/history.html` | History page with search, memory editing |

### Key Patterns
- **Global State**: `GLOBAL_STATE` dict in app.py shared across threads
- **Dual Memory**: Full log (immutable) + LLM context (summarized)
- **Event Objects**: `threading.Event()` for synchronization (use `is_set()`, `clear()`, `set()`, `wait()`)
- **SocketIO**: Real-time bidirectional communication with broadcast for API/UI sync
- **API Task Registry**: `API_TASK_REGISTRY` dict in agent_core.py tracks async task state
- **Legacy SSH Fallback**: Standard connection first, then retry with legacy ciphers/KEX/host keys
- **Block Execution Mode**: Interactive shell sessions for network devices (maintains config context)
- **Smart System Logging**: Only logs `SYSTEM CONTEXT CHANGED` when IP/username actually changes (prevents ghost entries)
- **Chat Concurrency Lock**: `_chat_processing_lock` in agent_core.py serializes concurrent `process_chat_message` calls
- **LLM Timeout Enforcement**: `ThreadPoolExecutor` wraps chat LLM invoke with configurable timeout (`llm_timeout`)
- **System Message Tagging**: Re-prompt and system messages prefixed with `[SYSTEM EVENT]` so LLM distinguishes them from user messages
- **Eventlet Emit Flush**: `socketio.sleep(0.1)` after all modal emits (ASK, approval, switch) to force eventlet hub to flush before blocking `wait()`
- **Pending Modal Re-emit**: `PENDING_ASK_DATA`, `PENDING_APPROVAL_DATA`, `PENDING_SWITCH_TARGET` globals in app.py — re-emitted in `handle_connect()` when client reconnects during pending modal

### Agent Actions
- `COMMAND:` - Execute SSH command (single line)
- `BLOCK:` / `CONFIG:` / multi-line `COMMAND:` - Execute block command (interactive session)
- `SRCH:` - Search execution history
- `KNOWLEDGE:` - Search embedded knowledge documents (vector similarity)
- `WEB_SEARCH:` - Trigger autonomous web search (chat LLM only, requires `REASON:` + `WEB_SEARCH:` format)
- `WRITE_FILE:` - Create file on remote system
- `ASK:` - Request human input (if enabled)
- `TIMEOUT:` - Adjust command timeout (step-specific, clamped to UI max)
- `REPORT:` - Final task report

### Chat Tags
- `<<MARK_STEP_COMPLETED: X>>` - Mark action plan step as done (tolerant regex: accepts `Step 3`, `step: 3`, `#3`, etc.)
- `<<SWITCH_SYSTEM: name>>` - Request system switch (requires approval unless Auto-Switch)
- `<<REQUEST_TASK: objective>>` - Propose new task (flexible regex: accepts `>>`, `>`, or no closing bracket; multi-line via `re.DOTALL`)
- `<<ACTION_PLAN_START>>..<<ACTION_PLAN_STOP>>` - Create multi-step plan

## REST API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/execute_ssh` | POST | Start async task (with optional system targeting) |
| `/api/task_status/<id>` | GET | Poll task status |
| `/api/status` | GET | Health check |
| `/api/stop` | POST | Stop running task |
| `/api/list_systems` | GET | List saved systems + current system |
| `/api/switch_system` | POST | Switch target system (with SSH validation) |

### Additional Web Endpoints
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/export_connections` | GET | Download connections.json |
| `/import_connections` | POST | Upload/replace connections from JSON |
| `/get_web_search_config` | GET | Get web search module configuration |
| `/save_web_search_config` | POST | Save web search module configuration |
| `/clear_web_knowledge` | POST | Remove all WEB-sourced knowledge documents |
| `/export_chat` | POST | Export chat messages as PDF/DOCX (receives messages, format, theme) |
| `/get_knowledge_doc/<doc_id>` | GET | Get knowledge document content + metadata as JSON |

## Configuration

**config.ini sections:**
- `[General]` - provider, gemini_api_key, anthropic_api_key
- `[Agent]` - model_name, max_steps, summarization_threshold, command_timeout, llm_timeout, chat_history_message_count, temperature
- `[System]` - ip_address, username, ssh_port, ssh_key_path, system_name, auth_method, auth_password, device_type, enable_password
- `[Ollama]` - api_url, num_ctx
- `[ChatLLM]` - enabled, provider, model_name (optional separate LLM for chat)
- `[WebSearch]` - enabled, provider, model_name, temperature, context_size, timeout, max_retries, max_results, max_fetch_pages, max_page_size, brief_threshold, search_engine, region, safe_search, ollama_url, api_key
- `[WebSearchPrompt]` - Autonomous web search LLM prompt (variables: `{reason}`, `{query}`)
- `[WebSearchInjection]` - Chat prompt injection template (teaches chat LLM how to use WEB_SEARCH:)
- `[OllamaPrompt]`, `[CloudPrompt]` - Main agent instruction templates
- `[OllamaPromptWithAsk]`, `[CloudPromptWithAsk]` - Variants with ASK capability
- `[OllamaSummarizePrompt]`, `[CloudSummarizePrompt]` - History summarization
- `[OllamaStepSummaryPrompt]`, `[CloudStepSummaryPrompt]` - Step output summarization
- `[OllamaSearchSummaryPrompt]`, `[CloudSearchSummaryPrompt]` - Search result summarization
- `[ChatPrompt]` - Chat instruction template
- `[OllamaValidatePrompt]`, `[CloudValidatePrompt]` - Command validation templates
- `[KnowledgePrompt]` - Knowledge injection template (uses `{documents}` placeholder)
- `[SystemMessages]` - Hidden auto-injected chat messages (task_completed, search_completed, knowledge_completed, switch_timeout, switch_approved, switch_denied, web_search_completed)

**System config fields:**
- `auth_method`: `key` | `password` | `key_or_password` (fallback)
- `device_type`: `linux` | `windows` | `cisco` | `nxos` | `iosxr` | `iosxe` | `brocade` | `juniper` | `arista` | `other`
- `system_name`: Friendly alias for the connection
- `enable_password`: Cisco enable/secret password for privileged mode

**Persistent files (in /app/keys/):**
- `config.ini`, `connections.json`, `execution_log.txt`, `execution_log_llm_context.txt`
- `chat_history.json`, `action_plan.json`, `id_rsa`, `id_rsa.pub`
- `knowledge/` - Knowledge documents, embeddings, vector store data

## Recent Changes (2025-2026)

### Chat Message Export (PDF/DOCX)
- `chat_export.py` — New module for exporting chat messages and knowledge documents
- **PDF generation**: `markdown` lib → HTML → `xhtml2pdf` (pisa), dark/light CSS themes
- **DOCX generation**: Custom markdown→DOCX parser with `python-docx`, headings, code blocks, lists, inline formatting
- **DejaVu font registration**: `@font-face` for DejaVu Sans/Mono (fixes Romanian diacritics ă/î/ș/ț/â in PDF)
- **Selection UI**: Right-click context menu on chat messages (Select/Deselect/Select All/Export Selected)
- **Export modal**: Format (PDF/DOCX) + Theme (Dark/Light) chooser, z-index 26010/26011 (above other modals)
- **Badge counter**: Shows count of selected messages in chat header
- **Dependencies**: `markdown`, `xhtml2pdf` added to dockerfile pip install
- **Reused by knowledge export**: `/export_chat` accepts any messages array, knowledge viewer passes doc as single message

### Knowledge Document Viewer & Export
- **Fullscreen viewer modal**: Click document name in knowledge list → modal with markdown-rendered content (via `marked.parse()`)
- **`get_document_text(doc_id)`**: New method in `knowledge_manager.py` — reads raw `.txt` file by document ID
- **`GET /get_knowledge_doc/<doc_id>`**: New route in `app.py` — returns `{filename, type, content, mode, size}`
- **Right-click context menu**: View Document, Export as PDF/DOCX (Dark/Light) — 5 options
- **Export from viewer**: Export button in viewer modal opens format/theme chooser, reuses `/export_chat` endpoint
- **Clickable filenames**: `refreshKnowledgeList()` wraps filenames with `onclick` and `oncontextmenu` handlers

### Eventlet Emit Flush & Pending Modal Re-emit
- **Problem**: SocketIO emits from background tasks (ASK, command approval, system switch) not reaching client — either not flushed before `wait()` blocks, or client disconnected at emit time
- **Fix 1 — Flush**: `socketio.sleep(0.1)` after every modal emit forces eventlet hub to process the queue before blocking `wait()`
- **Fix 2 — Reconnect re-emit**: Three global variables in `app.py`: `PENDING_ASK_DATA`, `PENDING_APPROVAL_DATA`, `PENDING_SWITCH_TARGET`
  - Set before emit, cleared after `wait()` completes
  - `handle_connect()` checks these and re-emits to `request.sid` of reconnecting client
- **Affected modals**: `awaiting_user_answer` (ASK), `awaiting_command_approval`, `chat_switch_proposal`

### System Switch Context Persistence
- **Problem**: Chat agent lost continuity after system switch — repeated itself, forgot context
- **Root cause**: Switch result messages (approved/denied/timeout) were not persisted to LLM context via `append_to_llm_context()`, and the original user message was discarded
- **Fix**: Added `log_manager.append_to_llm_context()` for switch result messages + preserved `original_user_message` in re-prompt (same pattern as SRCH/KNOWLEDGE/WEB_SEARCH)

### Web Search Module
- `web_search_module.py` - Autonomous web research with dedicated LLM (separate from execution/chat)
- **5-step pipeline**: Query optimization → DuckDuckGo search → LLM result ranking → Page fetch & relevance check → Summary generation
- **Dual content mode**: Brief content (< threshold) injected directly into chat context; large content attached to knowledge store with `WEB` source label
- **DuckDuckGo via `ddgs` package** (renamed from `duckduckgo-search`); page extraction via `trafilatura` (primary) + `beautifulsoup4` (fallback); PDF via PyPDF2
- **Query optimization**: LLM reformulates queries; `site:`/`inurl:` operators stripped (DuckDuckGo incompatible); automatic fallback to original query on 0 results
- **3-tier result classification**: Full success (`partial=False`) / Partial (snippets only, `partial=True`) / Failed — each triggers different re-prompt message to chat LLM
- **Auto-init KnowledgeManager**: `_try_init_knowledge_manager()` creates KM on the fly when Knowledge module isn't enabled but web search needs to store documents
- **Configurable prompt template**: `[WebSearchPrompt]` with `{reason}` and `{query}` variables, prepended as system preamble to all LLM calls
- **Dynamic chat injection**: `[WebSearchInjection]` template appended to chat prompt when web search is enabled (like Knowledge injection)
- **UI**: Web Search Configuration modal (LLM settings, search settings, content settings, prompt with fullscreen toggle), Web Search Status modal (live progress, step indicators, log), nav link, Prompt Editor "Web Search" tab for injection template
- **Config fallback**: WebSearch-specific `ollama_url`/`api_key` fields; empty = use main config values
- **SocketIO**: `web_search_status` events for live UI updates

### Chat Retry with Progressive Temperature
- **5 retries** (up from 3) for empty LLM responses in chat, matching execution agent
- **Progressive temperature bump**: +0.1 per retry (capped at 1.0) to unstick local LLMs
- Temperature restored to original value after retry loop completes
- Each retry also appends escalating nudge message to prompt

### System Message Tagging
- Chat re-prompt messages (from SRCH, KNOWLEDGE, WEB_SEARCH results) and system triggers (`is_system=True`) are prefixed with `[SYSTEM EVENT]`
- LLM can now distinguish system-injected messages from actual user messages in `{user_message}`
- Prevents LLM from treating internal re-prompts as user conversation

### Persistent UI Settings
- Advanced Settings (temperature, max_steps, summarization_threshold, llm_timeout, chat_history_message_count, num_ctx) pre-populated from config on page load via `DOMContentLoaded` fetch
- `command_timeout` in index.html loaded from persistent config when no `sessionStorage` value exists
- `command_timeout` added to `/get_agent_config` response
- All settings survive Docker rebuilds (stored in `/app/keys/config.ini`)

### REQUEST_TASK Regex Flexibility
- 3-pattern matching: `<<REQUEST_TASK: ...>>` (standard), `<<REQUEST_TASK: ...>` (single bracket), `<<REQUEST_TASK: ...` (no close)
- Loose pattern `<<REQUEST_TASK>>: ...` still supported as fallback
- `rstrip('>')` on extracted objective cleans residual brackets
- All patterns support multi-line objectives via `re.DOTALL`

### Network Device Support (Block Execution Mode)
- New `device_type` config field for Cisco, Brocade, Juniper, Arista, etc.
- `execute_ssh_block_command()` - Interactive shell sessions maintaining config context (conf t, interface, etc.)
- `is_block_command()` / `parse_block_command()` - Detect and parse multi-line block commands
- `BLOCK:` / `CONFIG:` / `INTERACTIVE:` command prefixes supported
- Auto-disable paging on Cisco devices (`terminal length 0`)
- Enable mode handling with `enable_password`
- Error detection for network device error patterns (`% Invalid`, `% Ambiguous`, etc.)

### Legacy SSH Algorithm Support
- `LEGACY_CIPHERS`, `LEGACY_KEY_EXCHANGE`, `LEGACY_HOST_KEYS` constants for older devices
- `load_private_key()` - Auto-detects key type (RSA, DSA, ECDSA, Ed25519)
- `connect_with_legacy_support()` - Tries standard connection first, falls back to legacy algorithms
- `create_ssh_client_with_legacy_support()` - Pre-configured client for legacy devices
- Two-phase connection: standard algorithms → legacy algorithms (Transport-level)

### Multiple Authentication Methods
- `auth_method` config: `key`, `password`, `key_or_password` (tries key first, falls back to password)
- `auth_password` stored in config.ini for SSH connections
- All SSH functions (`check_ssh_connection`, `execute_ssh_command`, `deploy_ssh_key`) updated
- UI: Auth method dropdown with dynamic field visibility

### System Naming & Smart Logging
- `system_name` (alias) field for connections (e.g., "Production Server")
- Smart system change logging: only creates `SYSTEM CONTEXT CHANGED` entry when IP/username actually changes
- `last_logged_system` tracking in GLOBAL_STATE prevents duplicate log entries after restart
- Timestamps in NEW TASK headers and SYSTEM CONTEXT CHANGED entries in LLM context

### REST API (Async)
- 6 endpoints for headless operation
- `API_TASK_REGISTRY` in agent_core.py tracks: status, start_time, result, current_step, latest_activity
- `/api/execute_ssh` supports `system_name` param for targeted execution
- Live View broadcasts to connected UIs (API tasks show indicator in UI)
- `broadcast_emit()` helper ensures all connected browsers see updates

### Guarded System Switch
- Chat agent uses `<<SWITCH_SYSTEM: name>>` tag
- `SYSTEM_SWITCH_EVENT` + `SYSTEM_SWITCH_RESPONSE` for async approval
- User approval required unless Auto-Switch checkbox enabled
- Modal confirmation with approve/deny buttons
- System events injected to LLM context after approval/denial

### Connections Import/Export
- **Save List**: Downloads `connections.json` with all saved connections
- **Load List**: Imports connections from JSON file (with validation)
- Validation: array format, required fields (ip, username)
- "Load" button renamed to "Activate" for clarity
- Backend: `/export_connections`, `/import_connections`

### Dual LLM Configuration
- `[ChatLLM]` config section: enabled, provider, model_name
- Separate LLM instance for chat vs execution agent
- Falls back to main LLM if ChatLLM not configured

### Chat Enhancements
- Timestamps on chat messages (DD.MM.YYYY HH:MM:SS format)
- Auto-Switch checkbox for automatic system switching
- Block command display in Commands view (multi-line formatting)

### Action Plan Improvements
- Explicit step completion via `<<MARK_STEP_COMPLETED: X>>` tags
- Edit mode with view/edit state management
- Fullscreen toggle, locked completed steps
- Stack-based: Main Plan → Sub-task Plans (nested)

### LLM Enhancements
- Stop sequences prevent hallucinated output
- Post-processing sanitization filters output markers
- Progressive nudging (5 retries with escalating pressure)
- Timestamped step headers in LLM context
- `summarize_single_output()` now accepts `reason` for contextual summaries

### Docker
- Timezone set to Europe/Bucharest (TZ environment variable)

### ASK Mode Event Fix
**Problem:** `AttributeError: 'Event' object has no attribute 'ready'` / `'send'`
**Fix:** `threading.Event` methods: `is_set()`, `clear()`, `set()`, `wait()`

### Windows SSH Compatibility
- Enhanced ANSI stripping (CSI + OSC patterns)
- OS detection at task start via `uname -s 2>/dev/null || ver`
- PTY handling: Windows=False, Unix=True
- Username parsing handles `HOSTNAME\username` format

### Knowledge Management System
- `knowledge_manager.py` - Document upload, text extraction, embedding, vector search
- Dual mode: small docs → direct injection into prompt; large docs → vector search via `KNOWLEDGE:` action
- Supports: txt, md, json, csv, pdf, docx + image OCR (png, jpg, etc.) via Tesseract
- Embedding providers: Ollama (`nomic-embed-text`) or Gemini (`models/embedding-001`)
- Vector stores: ChromaDB (persistent) or FAISS (manual persistence)
- `[KnowledgePrompt]` template with `{documents}` placeholder for auto-injection
- Max 10 documents, configurable small doc threshold (10% of summarization_threshold)
- `source_label` field: `USER` (manual upload) or `WEB` (from web search module)
- `clear_by_source()` / `get_documents_by_source()` methods for source-based filtering

### Chat Reliability Improvements
- **Concurrency lock**: `_chat_processing_lock` prevents race conditions when task-completion trigger and user message hit chat simultaneously
- **LLM timeout**: All chat LLM instances (Ollama/Gemini/Anthropic) now use `llm_timeout` from config (not hardcoded)
- **ThreadPoolExecutor timeout**: `llm.invoke()` wrapped with configurable timeout; falls back gracefully on timeout
- **Debug logging**: Prompt size, message type, LLM response status, lock acquire/release logged for diagnosis

### Chat Tag Parsing Robustness
- `<<REQUEST_TASK: ...>>` flexible regex: 3 patterns handle `>>`, `>`, no close bracket, and multi-line objectives
- `<<MARK_STEP_COMPLETED: X>>` regex tolerates LLM variations: `Step 3`, `step: 3`, `#3`, `Step3`
- Loose `<<REQUEST_TASK>>: ...` pattern still supported as fallback

### Timeout Handling Fix & Abort Command
- **Bug fix**: `execute_ssh_command_with_timeout()` now uses step-specific timeout (from agent's `TIMEOUT:` action) as primary value, not `global_state['command_timeout']`
- **`_effective_timeout()`**: `min(timeout_seconds, global_state)` — step timeout is respected, user can lower it via UI during execution but cannot silently raise above agent's value
- **Abort Command button**: Red "Abort Cmd" button appears during command execution (between `command_exec_start`/`command_exec_end`), kills SSH connection and pauses task (not stop)
- **ASK modal timeout field**: Timeout input in ASK modal, pre-populated with current value, auto-syncs with main UI on submit
- **Timeout rules**: Agent TIMEOUT → use it; No TIMEOUT → use UI max; Agent > UI max → clamp to UI max

### UI Enhancements
- Objective textarea fullscreen toggle (same pattern as Log/Screen fullscreen)
- Prompt Editor modal: 6 tabs (Standard, Ask, Chat, Knowledge Injection, System Messages, Web Search)
- Prompts import/export as ZIP

## Common Debugging

**Syntax check:** `python3 -m py_compile app.py agent_core.py ssh_utils.py config.py session_manager.py llm_utils.py log_manager.py knowledge_manager.py web_search_module.py chat_export.py`

**Check logs:** `docker logs ai-agent --tail 50`

**Key issues:**
- **ASK/Approval hanging**: Check Event methods use `is_set()`/`clear()`/`set()` not `ready()`/`reset()`/`send()`
- **Windows output garbled**: Check OS detection logs, verify `get_pty=False` for Windows
- **SRCH not finding**: Search is in Full Log only, case-insensitive substring match
- **Action Plan not updating**: Use `<<MARK_STEP_COMPLETED: X>>` tag (primary method)
- **System switch failing**: Check target exists in connections.json, SSH key deployed or password configured
- **PTY allocation error**: Server-side issue (check sshd_config `PermitTTY`)
- **Legacy SSH connection failed**: Check `LEGACY_CIPHERS`/`LEGACY_KEY_EXCHANGE` in ssh_utils.py
- **Block command not executing**: Verify `device_type` is set to a network device type in config
- **Ghost "SYSTEM CONTEXT CHANGED"**: Check `last_logged_system` tracking in GLOBAL_STATE
- **API task not updating UI**: Verify `broadcast_emit()` is used, check `is_api_task` flag
- **Chat stuck in "thinking"**: Check `[CHAT]` logs for lock/timeout issues; two concurrent calls may race — `_chat_processing_lock` serializes them
- **Timeout not respecting agent TIMEOUT: value**: Verify `_effective_timeout()` in `execute_ssh_command_with_timeout()` uses `min(timeout_seconds, global_state)`
- **REQUEST_TASK not detected**: 3-pattern regex handles `>>`, `>`, no close bracket; check `re.DOTALL` for multi-line
- **MARK_STEP_COMPLETED not detected**: LLMs often write `Step 3` instead of just `3`; regex handles this
- **Web search returns 0 results**: LLM may add `site:` operators; code strips them but check optimized query in logs
- **Web search "Knowledge manager not available"**: KM auto-initializes for web search; check embedding provider/model availability
- **Chat empty responses**: 5 retries with progressive temperature bump (+0.1 per retry, cap 1.0)
- **Settings reset after rebuild**: All settings stored in `/app/keys/config.ini` (persistent volume); UI pre-populates on page load
- **Modal not appearing (ASK/approval/switch)**: Two causes: (1) emit not flushed — check `socketio.sleep(0.1)` after emit; (2) client disconnected — check `PENDING_*_DATA` variables and `handle_connect()` re-emit
- **PDF diacritics as black squares**: Check DejaVu Sans `@font-face` registration in `_get_pdf_font_face()` and `font-family: 'DejaVu'` in PDF CSS
- **Chat export modal behind viewer**: z-index must be 26010/26011 (higher than knowledge viewer at 26000/26001)
- **Chat agent repeats after system switch**: Check `append_to_llm_context()` call in switch result handling + `original_user_message` preservation

## Threading Event Reference

`threading.Event()` methods (used for user_answer_event, summarization_event, user_approval_event, system_switch_event):
- `is_set()` - Check if flag is True
- `set()` - Set flag to True, wake waiters
- `clear()` - Reset flag to False
- `wait(timeout)` - Block until flag is True

`eventlet.event.Event()` methods (used for execution_complete):
- `ready()` - Check if event was sent
- `send()` - Signal the event
- `wait()` - Block until sent

**Do not mix these up!**
