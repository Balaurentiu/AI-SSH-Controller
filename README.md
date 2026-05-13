# AI SSH Controller

AI-powered autonomous agent for executing commands on remote systems via SSH. The agent uses LLM models (Ollama, Google Gemini, or Anthropic Claude) to analyze objectives, generate commands, and complete tasks autonomously.

## Features

- **Multi-LLM Architecture**: Separate configurable models for Execution, Chat, Validation, and Web Search
  - Independent provider selection per role (Ollama, Gemini, Anthropic)
  - Fallback LLM chains — automatic switch on timeout or connection error
  - Live model fetching per provider
- **Multiple LLM Providers**: Ollama (local), Google Gemini, Anthropic Claude
- **Autonomous Execution**: Agent independently generates, validates, and executes commands
- **Execution Modes**: Independent (auto-validate) or Assisted (manual approval per command)
- **Report Validator**: Separate LLM audits the agent's final REPORT against actual execution history to catch hallucinations
- **Knowledge Management**: Upload documents (PDF, DOCX, TXT, images via OCR) — agent retrieves relevant context via vector search
- **Autonomous Web Search**: Agent triggers DuckDuckGo research pipeline with dedicated LLM, attaches findings to knowledge store
- **Chat Export**: Export chat conversations and knowledge documents as PDF or DOCX (dark/light themes)
- **REST API**: 6 async endpoints for headless/programmatic control
- **Network Device Support**: Cisco, NX-OS, IOS-XR, IOS-XE, Brocade, Juniper, Arista via interactive Block Execution mode
- **Legacy SSH Support**: Automatic fallback to legacy ciphers/KEX/host keys for older devices
- **Multiple Auth Methods**: SSH key, password, or key-with-password-fallback
- **Multi-System Orchestration**: Switch between saved connections with guarded approval flow
- **Connections Import/Export**: Save and load connection lists as JSON
- **Abort Command**: Kill a running command mid-execution and pause the task without stopping it
- **Telegram Bot**: Optional Telegram interface for remote control
- **Advanced Capabilities**:
  - `SRCH:` — Search full execution history
  - `WRITE_FILE:` — Create files on remote system
  - `KNOWLEDGE:` — Vector search over uploaded documents
  - `WEB_SEARCH:` — Trigger autonomous web research
  - `ASK:` — Request human input
  - `TIMEOUT:` — Adjust per-step command timeout
  - `BLOCK:` / `CONFIG:` — Interactive shell sessions for network devices
- **Interactive Chat Interface**: Chat with the agent, request tasks, create action plans, switch systems
- **Persistent Memory**: Full log + summarized LLM context, survives container restarts
- **History Summarization**: Automatic context compression when threshold exceeded

## Architecture

### Core Components

| File | Purpose |
|------|---------|
| `app.py` | Flask + SocketIO, routes, REST API, global state |
| `agent_core.py` | Agent loop, LLM interaction, action parsing |
| `ssh_utils.py` | SSH via paramiko, PTY, legacy algorithms, block execution |
| `log_manager.py` | Dual logging (full log + LLM context), summarization |
| `config.py` | Configuration management and section backfill |
| `session_manager.py` | State persistence and ZIP backup/restore |
| `llm_utils.py` | LLM API calls (Ollama/Gemini/Anthropic) |
| `knowledge_manager.py` | Document upload, embedding, vector search (ChromaDB/FAISS) |
| `web_search_module.py` | Autonomous web research pipeline with dedicated LLM |
| `chat_export.py` | Chat/knowledge export to PDF and DOCX |
| `telegram_bot.py` | Optional Telegram bot interface |

### Dual-Memory System

1. **Full Log** (`execution_log.txt`): Immutable append-only record of all activity
2. **LLM Context** (`execution_log_llm_context.txt`): Agent's working memory (summarized when threshold exceeded)

## Quick Start

### Prerequisites

- Docker
- SSH access to target system
- API key for Gemini or Anthropic (if not using Ollama)

### Installation

1. Clone the repository:
```bash
git clone https://github.com/Balaurentiu/AI-SSH-Controller.git
cd AI-SSH-Controller
```

2. Prepare the keys directory and configuration:
```bash
mkdir -p keys
cp config.ini keys/config.ini
```

3. Build the Docker image:
```bash
docker build -t agent-controller .
```

4. Start the container:
```bash
docker run -d --name agent-app -p 5000:5000 \
  -v $(pwd)/keys:/app/keys \
  agent-controller
```

5. Access the web interface at `http://localhost:5000`

---

## Docker Reference

### Build

```bash
docker build -t agent-controller .
```

Force a clean rebuild (no cache):
```bash
docker build --no-cache -t agent-controller .
```

### Start

```bash
docker run -d --name agent-app -p 5000:5000 \
  -v $(pwd)/keys:/app/keys \
  agent-controller
```

- `-d` — run in background
- `--name agent-app` — container name for easy reference
- `-p 5000:5000` — expose web interface on port 5000
- `-v $(pwd)/keys:/app/keys` — persist all settings, logs, keys, and knowledge data

To expose on a specific host interface only (e.g. localhost):
```bash
docker run -d --name agent-app -p 127.0.0.1:5000:5000 \
  -v $(pwd)/keys:/app/keys \
  agent-controller
```

### Stop

```bash
docker stop agent-app
```

### Restart

```bash
docker restart agent-app
```

### Remove container (keeps image and keys/)

```bash
docker stop agent-app
docker rm agent-app
```

### Rebuild and redeploy

```bash
docker stop agent-app
docker rm agent-app
docker build -t agent-controller .
docker run -d --name agent-app -p 5000:5000 \
  -v $(pwd)/keys:/app/keys \
  agent-controller
```

All settings, prompts, logs, and knowledge documents are stored in `keys/` and survive rebuilds.

### Logs

View live container logs:
```bash
docker logs agent-app -f
```

Last 100 lines:
```bash
docker logs agent-app --tail 100
```

### Container shell (for debugging)

```bash
docker exec -it agent-app bash
```

### Status

```bash
docker ps -a --filter name=agent-app
```

---

### Pre-Built Prompts (Optional)

The `GOOD_PROMPTS/` directory contains production-tested prompt templates. Import them via the web interface:

1. Click **Prompts I/E** in the settings bar
2. Select the `prompts_export_*.zip` file from `GOOD_PROMPTS/`
3. Click **Import Prompts from ZIP**

## Configuration

All configuration is done through the **web interface**.

### Agent & LLM Configuration

- **Execution LLM**: Provider, model, API key/URL for task execution
- **Chat LLM**: Separate model for conversational chat (optional)
- **Validator LLM**: Stronger model for report auditing (optional, recommended)
- **Fallback LLMs**: Automatic fallback chain per role on failure
- Advanced: max steps, timeouts, summarization threshold, num_ctx, temperature

### Remote System Connection

- IP, username, SSH port, auth method (key / password / key_or_password)
- SSH key auto-generation and deployment
- Device type (linux, windows, cisco, nxos, iosxr, iosxe, brocade, juniper, arista)
- Enable password for Cisco privileged mode

### Knowledge Management

Enable from the **Knowledge & Embeddings** section:
- Embedding provider (Ollama `nomic-embed-text` or Gemini)
- Vector store (ChromaDB persistent or FAISS)
- Upload documents via the Knowledge panel (PDF, DOCX, TXT, MD, CSV, images)
- Agent uses `KNOWLEDGE: <query>` to retrieve relevant content via similarity search

### Web Search

Configure from the **Web Search** nav link:
- Dedicated LLM for search pipeline (provider, model, temperature, context size)
- Fallback models for web search LLM
- Search engine (DuckDuckGo), max results, max pages to fetch, region, safe search
- Content settings: max page size, brief threshold (content below threshold injected directly into chat; larger content attached to knowledge store)
- Customizable prompt template with `{reason}` and `{query}` variables

Agent triggers web search from chat using:
```
REASON: [why the search is needed]
WEB_SEARCH: [search query]
```

### REST API

6 async endpoints for headless operation:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/execute_ssh` | POST | Start async task (with optional `system_name`) |
| `/api/task_status/<id>` | GET | Poll task status |
| `/api/status` | GET | Health check |
| `/api/stop` | POST | Stop running task |
| `/api/list_systems` | GET | List saved systems |
| `/api/switch_system` | POST | Switch target system |

Example:
```bash
curl -X POST http://localhost:5000/api/execute_ssh \
  -H "Content-Type: application/json" \
  -d '{"objective": "Check disk usage and report findings", "max_steps": 10}'
```

## Usage

### Execution Tab

1. Enter your objective
2. Choose mode (Independent / Assisted) and optional settings (ASK, validator, timeout)
3. Click **Execute Task**
4. Monitor live log and remote screen output
5. Use **Abort Cmd** to kill a running command without stopping the task

### Chat Tab

- Chat with the agent about past executions or request new tasks
- Agent proposes tasks via `<<REQUEST_TASK: ...>>` — approve or auto-accept
- Create multi-step action plans: `<<ACTION_PLAN_START>>..<<ACTION_PLAN_STOP>>`
- Switch target systems via `<<SWITCH_SYSTEM: name>>` with optional approval flow
- Export chat history as PDF or DOCX (right-click messages for selection)

### Network Devices

For Cisco, Juniper, Arista etc., set `device_type` in system config. The agent uses `BLOCK:` or `CONFIG:` prefixed commands to maintain interactive shell sessions — preserving `conf t` context across steps.

### Multi-System Orchestration

Save multiple connections via the system config. The agent or user can switch between them:
- **Guarded switch**: User approval required (modal confirmation)
- **Auto-Switch**: Enable checkbox to skip confirmation

### Connections Import/Export

- **Save List**: Download all saved connections as `connections.json`
- **Load List**: Import connections from a JSON file

### History & Reports

- View and edit agent's working memory (LLM context)
- Browse full immutable execution log
- Download complete session as ZIP
- Manually trigger summarization

## Prompt System

All prompts are customizable via the **Prompt Editor** in the web interface. Available prompt types:

| Prompt | Purpose | Variables |
|--------|---------|-----------|
| OllamaPrompt / CloudPrompt | Main execution loop | `{objective}`, `{history}`, `{system_info}`, `{command_timeout}` |
| OllamaPromptWithAsk / CloudPromptWithAsk | Execution with ASK capability | same + ASK format |
| ChatPrompt | Chat interface persona | `{user_message}`, `{chat_history}`, `{action_plan_status}` |
| OllamaSummarizePrompt / CloudSummarizePrompt | History compression | `{objective}`, `{history}` |
| OllamaStepSummaryPrompt / CloudStepSummaryPrompt | Large output summarization | `{output}` |
| OllamaSearchSummaryPrompt / CloudSearchSummaryPrompt | Search result synthesis | `{objective}`, `{reason}`, `{results}` |
| OllamaValidatePrompt / CloudValidatePrompt | Command safety check | `{command}`, `{system_info}`, `{sudo_available}`, `{command_timeout}` |
| OllamaValidateReportPrompt / CloudValidateReportPrompt | Report auditing | `{objective}`, `{history}`, `{report}` |
| WebSearchPrompt | Web research LLM guidance | `{reason}`, `{query}` |
| WebSearchInjection | Teaches chat LLM to use WEB_SEARCH | — |
| KnowledgePrompt | Knowledge document injection | `{documents}` |

## Security Considerations

⚠️ **This tool executes commands on remote systems autonomously.** Always:
- Use dedicated test systems for experimentation
- Review generated commands in Assisted mode
- Monitor agent activity closely
- Never share your `keys/config.ini` or API keys
- Be cautious with Auto-Accept and Auto-Switch in production environments

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE). Any modifications or derivative works must remain open source.

## Contributing

Issues and pull requests are welcome.
