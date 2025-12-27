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
- **Real-time Web Interface**: Monitor agent activity via browser
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

2. Create the keys directory:
```bash
mkdir -p keys
touch session.json
```

3. Build and run with Docker:
```bash
docker build -t agent-controller .

docker run -d --name agent-app -p 5000:5000 \
  -v $(pwd)/keys:/app/keys \
  -v $(pwd)/session.json:/app/session.json \
  agent-controller
```

4. Access the web interface at `http://localhost:5000`

## Configuration

All configuration is done through the **web interface** - no need to manually edit files!

### First-Time Setup

1. Open `http://localhost:5000` in your browser
2. Click the **Settings** icon (⚙️) in the top navigation
3. Configure each section:

   **Agent Configuration:**
   - LLM Provider (Ollama, Gemini, or Anthropic)
   - API Key (for Gemini/Anthropic)
   - Model name (e.g., `llama3:latest`, `gemini-pro`, `claude-3-5-sonnet-20241022`)
   - Max steps, timeouts, and summarization threshold

   **System Configuration:**
   - Target system IP address
   - SSH username
   - SSH port (default: 22)
   - SSH key path (auto-generated if not exists)

   **Advanced:**
   - Customize prompts for different scenarios
   - Adjust validator prompts
   - Configure summarization behavior

4. Click **Save** to apply settings
5. Test SSH connection using the "Test Connection" button

### SSH Key Setup

The application handles SSH key generation and deployment automatically through the web UI:

1. Go to Settings → System Config
2. Enter your target system credentials (IP, username, password)
3. Click **"Deploy SSH Key"** button
4. The application will:
   - Generate an SSH key pair if it doesn't exist
   - Automatically deploy the public key to your target system
   - Configure passwordless SSH access

**That's it!** No manual copying or editing of `authorized_keys` files needed.

**Note:** Configuration is automatically saved to `keys/config.ini` for persistence across container restarts.

## Usage

1. **Start a Task**:
   - Open the web interface
   - Enter your objective (e.g., "Install nginx and configure it to serve a static website")
   - Choose execution mode (Independent/Assisted)
   - Click "Execute Task"

2. **Monitor Execution**:
   - View real-time logs in the "Agent Execution Log" panel
   - See command outputs in the "VM Screen" panel
   - Track agent's reasoning and decisions

3. **Interact**:
   - Pause/Resume execution
   - Approve commands in Assisted mode
   - Answer agent questions (if ASK mode enabled)
   - Manually search history or edit agent memory

## Advanced Features

### SRCH (History Search)
The agent can search its full execution history using `SRCH: <query>` to recall information from earlier steps, even after summarization.

### WRITE_FILE
The agent can create files on the remote system with custom content, useful for generating configuration files, scripts, or documents.

### Dynamic Timeout Adjustment
The agent can adjust command timeouts on a per-step basis for long-running operations.

### Manual Memory Editing
Edit the agent's working memory directly via the History tab to guide its behavior.

## Security Considerations

⚠️ **Important**: This tool executes commands on remote systems autonomously. Always:
- Use dedicated test systems for experimentation
- Review generated commands in Assisted mode
- Restrict SSH access appropriately
- Never share your `config.ini` or API keys
- Monitor agent activity closely

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
