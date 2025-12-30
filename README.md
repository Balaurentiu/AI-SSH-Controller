# AI SSH Controller

AI-powered autonomous agent for executing commands on remote systems via SSH. The agent uses LLM models (Ollama, Google Gemini, or Anthropic Claude) to analyze objectives, generate commands, and complete tasks autonomously.

## Features

- **Multiple LLM Support**: Works with Ollama (local), Google Gemini, or Anthropic Claude
- **Autonomous Execution**: Agent independently generates and executes commands
- **Execution Modes**:
  - **Independent**: Agent validates commands automatically via LLM
  - **Assisted**: User approval required for each command
- **Advanced Capabilities**:
  - **SRCH**: Search past execution history
  - **WRITE_FILE**: Create files on remote systems
  - **ASK**: Request human input when needed
  - **Dynamic Timeout**: Adjust command timeouts per step
- **Interactive Chat Interface**:
  - Chat with the agent about executed tasks
  - Request new tasks via natural language
  - Auto-accept tasks or manual approval
  - Action plans with multi-step workflows
  - Search execution history from chat
- **Modern Web Interface**:
  - Real-time execution monitoring
  - Fullscreen modes for Log, Screen, and Chat
  - Dedicated status bar for command execution
  - Modal configuration dialogs
  - Tabbed navigation (Execution / Chat)
- **Persistent Memory**: Agent maintains context across sessions
- **History Summarization**: Automatic context compression when threshold exceeded

## Architecture

### Core Components

- **app.py**: Flask web application with SocketIO for real-time communication
- **agent_core.py**: Main agent execution loop and LLM interaction
- **log_manager.py**: Unified logging architecture with dual-memory system
- **ssh_utils.py**: SSH command execution with Windows/Linux compatibility
- **config.py**: Configuration management and persistent paths
- **session_manager.py**: State persistence and session handling
- **llm_utils.py**: LLM API integration (Ollama/Gemini/Anthropic)

### Dual-Memory System

1. **Full Log** (`execution_log.txt`): Immutable append-only record of all activity
2. **LLM Context** (`execution_log_llm_context.txt`): Agent's working memory (subject to summarization)

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

2. Create the keys directory and prepare configuration:
```bash
mkdir -p keys
touch session.json

# Copy the template configuration file
cp keys/config.ini.new keys/config.ini
```

**Important:** The `keys/config.ini.new` is a template file. After cloning the repository, copy it to `keys/config.ini` before running the application. This file will be automatically populated with your settings through the web interface.

3. Build and run with Docker:
```bash
docker build -t agent-controller .

docker run -d --name agent-app -p 5000:5000 \
  -v $(pwd)/keys:/app/keys \
  -v $(pwd)/session.json:/app/session.json \
  agent-controller
```

4. Access the web interface at `http://localhost:5000`

### Pre-Built Prompts (Optional)

The repository includes a **GOOD_PROMPTS/** directory containing production-tested, optimized prompt templates:

- ✅ **Without ASK.txt** - Standard task execution prompts
- ✅ **With ASK.txt** - Execution prompts with human interaction capability
- ✅ **Chat.txt** - Conversational chat interface prompt
- ✅ **Validator.txt** - Command validation and safety checks
- ✅ **Summarisation.txt** - History compression prompts
- ✅ **Step Summarisation.txt** - Large output summarization
- ✅ **Search Summarisation.txt** - Search results analysis

These prompts have been thoroughly tested and embody best practices for:
- Methodical system administration
- Safe command execution
- Efficient debugging
- Clear failure reporting
- Natural conversational flow

**To use these prompts:**
1. Open the web interface at `http://localhost:5000`
2. Click the **Prompt Editor** card in the settings bar
3. Copy the content from the desired `.txt` file in `GOOD_PROMPTS/`
4. Paste into the appropriate prompt field (Ollama or Cloud)
5. Click **Save Templates**

**Note:** These are optional. The application will work with default prompts, but these pre-built templates provide enhanced performance and better agent behavior.

## Configuration

All configuration is done through the **web interface** - no need to manually edit files!

### First-Time Setup

1. Open `http://localhost:5000` in your browser
2. You'll see a **settings bar** below the navigation with configuration cards
3. Configure each section by clicking on the cards:

   **Agent & LLM Configuration** (click the card):
   - Select LLM Provider (Ollama, Gemini, or Anthropic)
   - Enter API Key (for Gemini/Anthropic)
   - Click **Fetch Models** to load available models from your provider
   - Choose model from dropdown (e.g., `llama3:latest`, `gemini-pro`, `claude-3-5-sonnet-20241022`, `The best for local Ollama is gpt-oss:20b`)
   - Set max steps, timeouts, and summarization threshold
   - Click **Save Agent Config** to apply

   **Remote System Connection** (click the card):
   - Enter target system IP address
   - Enter SSH username
   - Set SSH port (default: 22)
   - Configure SSH key path ... enter the password for automatic deploy and press Deploy
   - Click **Save & Test Connection** to apply and verify connectivity

   **Optional - Advanced Settings:**
   - **Prompt Editor**: Customize agent instruction templates
   - **Validator Prompt**: Modify command validation rules
   - **Summarization Prompt**: Adjust history compression behavior

### SSH Key Setup

The application handles SSH key generation and deployment automatically:

1. Click on the **"Remote System Connection"** card in the settings bar
2. Enter your target system credentials (IP, username, password)
3. Click the **"Deploy SSH Key"** button
4. The application will:
   - Generate an SSH key pair if it doesn't exist
   - Automatically deploy the public key to your target system
   - Configure passwordless SSH access

**That's it!** No manual copying or editing of `authorized_keys` files needed.

**Note:** Configuration is automatically saved to `keys/config.ini` for persistence across container restarts.

## Usage

### Execution Tab (Direct Task Execution)

1. **Start a Task**:
   - Open the web interface (Execution tab)
   - Enter your objective (e.g., "Install nginx and configure it to serve a static website")
   - Choose execution mode (Independent/Assisted)
   - Enable "Allow Agent to ASK" if you want the agent to request clarification
   - Click "Execute Task"

2. **Monitor Execution**:
   - View real-time logs in the "Agent Execution Log (Live)" panel
   - See command outputs in the "Remote System Screen (Live)" panel
   - Track agent's reasoning and decisions
   - Watch the dedicated status bar below the log for current activity

3. **Interact**:
   - Pause/Resume execution
   - Approve commands in Assisted mode
   - Answer agent questions (if ASK mode enabled)
   - Adjust command timeout on the fly
   - Use fullscreen mode (⛶ button) for focused views

### Chat Tab (Conversational Interface)

1. **Chat with the Agent**:
   - Switch to the "Assistant Chat" tab
   - Ask questions about past executions
   - Request information from execution history
   - Discuss system status and configurations

2. **Request Tasks via Chat**:
   - Describe what you want to accomplish in natural language
   - Agent proposes a task with `<<REQUEST_TASK: objective>>`
   - Approve or reject the proposed task
   - Or enable "Auto-Accept Tasks" for automatic execution

3. **Action Plans**:
   - Agent can create multi-step action plans: `<<ACTION_PLAN_START>>...<<ACTION_PLAN_STOP>>`
   - View plan status in the chat toolbar (e.g., "AP: Step 2/5 in progress...")
   - Click the status button to see full plan details
   - Plans automatically track completed and pending steps
   - Supports nested sub-plans (stack-based)

4. **Search from Chat**:
   - Agent uses `SRCH: <query>` to search execution history
   - Retrieves full file contents logged in previous executions
   - Summarizes search results before responding

### History & Reports Page

- View and edit agent's working memory (LLM context)
- Browse full execution log (immutable record)
- Download complete session data as ZIP
- Manually trigger history summarization
- Search past executions

## Advanced Features

### SRCH (History Search)
The agent can search its full execution history using `SRCH: <query>` to recall information from earlier steps, even after summarization.

**Enhanced Search Capabilities:**
- Retrieves complete file content blocks (marked with `--- FILE CONTENT WRITTEN TO ---`)
- Returns up to 50 matches with context
- Works from both task execution and chat interface
- Automatically summarized by LLM when using cloud providers

### WRITE_FILE
The agent can create files on the remote system with custom content, useful for generating configuration files, scripts, or documents.

**Format:**
```
WRITE_FILE: /path/to/file
CONTENT:
[file content here]
END_CONTENT
```

File content is automatically logged to execution history for future SRCH operations.

### Action Plans (Multi-Step Workflows)
The agent can break down complex objectives into sequential steps:

**Creating a Plan (from chat):**
```
<<ACTION_PLAN_START>>
Title: Deploy Web Application
Step 1. Install dependencies
Step 2. Configure database
Step 3. Deploy application
Step 4. Start services
<<ACTION_PLAN_STOP>>
```

**Features:**
- Visual progress tracking in chat toolbar
- Automatic step completion detection (fuzzy keyword matching)
- Stack-based architecture supports nested sub-plans
- Catch-up logic for out-of-order completions
- Persistent across sessions

### Dynamic Timeout Adjustment
The agent can adjust command timeouts on a per-step basis for long-running operations using `TIMEOUT: <seconds>`.

### Manual Memory Editing
Edit the agent's working memory directly via the "History & Reports" page to guide its behavior. Click "Edit Agent Memory" to modify the LLM context.

### Chat Interface Features

**Auto-Accept Tasks:**
Enable the "Auto-Accept Tasks" checkbox in the chat toolbar to automatically execute tasks proposed by the agent without manual confirmation.

**Fullscreen Modes:**
Each panel (Chat, Log, Screen) has a fullscreen toggle button (⛶) for focused viewing:
- **Chat Fullscreen:** Immersive conversational interface
- **Log Fullscreen:** Maximize execution log for detailed analysis
- **Screen Fullscreen:** Full-viewport terminal output view

**Status Bar:**
Dedicated status bar below the execution log shows transient messages:
- "Thinking... (30s remaining)"
- "Executing command... (120s timeout)"
- Doesn't clutter the main log output

## Prompt System

The agent's behavior is guided by a comprehensive set of prompts that define how it analyzes tasks, generates commands, and handles various situations. All prompts are customizable through the web interface.

### Available Prompt Types

#### 1. **Standard Execution Prompt** (`CloudPrompt` / `OllamaPrompt`)
The main prompt that guides the agent's decision-making process. It includes:

**Core Strategy: Verify → Learn → Act**
- Verify system information before acting
- Learn command syntax with `--help` or `man` if uncertain
- Execute only after verifying prerequisites
- Handle timeouts and exit codes appropriately

**Response Formats Available:**
- `COMMAND:` - Execute a shell command
- `WRITE_FILE:` - Create files on the remote system
- `SRCH:` - Search execution history
- `REPORT:` - Complete the task (Success/Failure)
- `TIMEOUT:` - Adjust command timeout for current step

**Key Features:**
- Simplicity mandate (keep commands focused)
- Loop detection (prevent infinite retries)
- Summary awareness (respect summarized context)
- Non-interactive terminal handling

#### 2. **ASK-Enabled Prompt** (`CloudPromptWithAsk` / `OllamaPromptWithAsk`)
Extended version that adds the `ASK:` capability for human interaction.

**When Agent Can Ask:**
- Critical actions (destructive operations, security implications)
- Ambiguity/multiple choices requiring user guidance
- Clarification needed for safe decision-making

**Format:**
```
REASON: [Explanation]
ASK: [Question for user]
```

#### 3. **History Summarization Prompt** (`CloudSummarizePrompt` / `OllamaSummarizePrompt`)
Used when execution history exceeds the summarization threshold.

**Preserves:**
- Initial system discoveries (OS, hardware)
- Major actions (software installed, files created)
- Key errors and resolutions
- HUMAN SEARCH/INTERVENTION findings
- Last 2-3 command outputs

**Variables:** `{objective}`, `{history}`

#### 4. **Step Output Summarization Prompt** (`CloudStepSummaryPrompt` / `OllamaStepSummaryPrompt`)
Triggered when a single command output is too large (flood protection).

**Rules:**
1. Preserve all error messages, warnings, exit codes
2. Preserve last 5-10 lines exactly
3. Keep key data points (IPs, paths, IDs)
4. State clearly this is a summary

**Variable:** `{output}`

#### 5. **Search Results Summarization Prompt** (`CloudSearchSummaryPrompt` / `OllamaSearchSummaryPrompt`)
Used when SRCH returns results from execution history.

**Task:**
- Synthesize findings relevant to the search reason
- List specific data explicitly (IPs, paths, errors)
- Ignore irrelevant logs
- State clearly if nothing relevant found

**Variables:** `{objective}`, `{reason}`, `{results}`

#### 6. **Command Validator Prompt** (`CloudValidatePrompt` / `OllamaValidatePrompt`)
Safety checker in Independent mode. Validates commands before execution.

**Checks For:**
- **Interactive blocking** - Commands requiring user input (nano, vi, apt without -y)
- **Invalid format** - Syntax errors, OS incompatibility
- **Timeout issues** - Commands likely exceeding configured timeout
- **Destructive operations** - Potentially dangerous commands

**Special Handling:**
- Passwordless sudo support (when configured)
- Piped password injection detection
- OS-specific command validation

**Response Format:** `APPROVE` or `REJECT\nREASON: [explanation]`

**Variables:** `{system_info}`, `{sudo_available}`, `{command}`, `{reason}`, `{summarization_threshold}`, `{command_timeout}`

### Customizing Prompts

You can customize any prompt through the web interface:

1. Click the **Prompt Editor** card in the settings bar
2. Select the prompt type (Standard/Ask Mode)
3. Edit prompts for Ollama or Cloud providers separately
4. Click **Save Templates**

For validator and summarization prompts, use their dedicated cards in the settings bar.

### Prompt Variables

Common variables available across prompts:
- `{objective}` - Current task objective
- `{history}` - Execution history or LLM context
- `{system_info}` - Remote system information (OS, user, IP)
- `{command}` - Command to validate
- `{reason}` - Agent's reasoning
- `{sudo_available}` - Whether passwordless sudo is configured
- `{command_timeout}` - Maximum command execution time
- `{summarization_threshold}` - Context size threshold
- `{output}` - Command output to summarize
- `{results}` - Search results to analyze
- `{action_plan_status}` - Current action plan status (for chat prompts)
- `{chat_history}` - Recent chat messages (configurable count)
- `{user_message}` - Current user message (chat only)

### Default Configuration

The `keys/config.ini.new` template file includes comprehensive, production-ready prompts for all scenarios. These prompts embody best practices for:
- Methodical system administration
- Safe command execution
- Efficient debugging
- Clear failure reporting

You can use these as-is or customize them to match your specific use cases.

## User Interface Overview

### Main Components

**Navigation Bar:**
- Execution / Chat / History & Reports tabs
- Settings cards (Agent Config, System Config, Prompts)
- Save/Load session buttons

**Execution Tab:**
- **Objective Input:** Enter task description
- **Control Panel:** Execution mode, ASK toggle, validator toggle, command timeout
- **Execution Buttons:** Execute, Stop, Pause
- **Agent Execution Log:** Real-time agent activity with view modes (Actions/Commands)
- **Execution Status Bar:** Shows "Thinking..." and "Executing command..." states
- **Remote System Screen:** Live command output display
- **Fullscreen Toggles:** ⛶ buttons for Log and Screen panels

**Chat Tab:**
- **Chat History:** Conversational interface with the agent
- **Chat Input:** Natural language input field
- **Action Plan Status:** Visual indicator showing current step progress
- **Auto-Accept Tasks:** Toggle for automatic task approval
- **Chat Controls:** Clear chat, fullscreen mode

**History & Reports Tab:**
- **Agent Memory Editor:** Edit LLM working context
- **Full Log Viewer:** Immutable execution record
- **Session Management:** Download/reset functionality

### Visual Indicators

- **Green:** Success states, active elements
- **Orange:** In-progress states, warnings
- **Red:** Errors, destructive actions
- **Blue:** Information, navigation links

### Keyboard Shortcuts

- **Tab Navigation:** Click tab names to switch views
- **Fullscreen:** Click ⛶ buttons for focused views
- **Modals:** Click outside or "X" to close

## Security Considerations

⚠️ **Important**: This tool executes commands on remote systems autonomously. Always:
- Use dedicated test systems for experimentation
- Review generated commands in Assisted mode
- Restrict SSH access appropriately
- Never share your `config.ini` or API keys
- Monitor agent activity closely
- Be cautious with auto-accept tasks in production environments
- Review action plans before execution starts

## Documentation

See [CLAUDE.md](CLAUDE.md) for comprehensive technical documentation including:
- Detailed architecture diagrams
- Development guidelines
- Troubleshooting guide
- Docker deployment details

## License

This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](LICENSE) file for details.

GPL v3 ensures that any modifications or derivative works remain open source and freely available to the community.

## Contributing

Contributions are welcome! Please feel free to submit issues or pull requests.

## Support

For questions or issues, please open an issue on GitHub.
