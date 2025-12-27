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

2. Create the keys directory and config:
```bash
mkdir -p keys
cp keys/config.ini.new keys/config.ini
```

3. Edit `keys/config.ini` with your settings:
   - Set `provider` (ollama, gemini, or anthropic)
   - Add API key if using cloud provider
   - Configure target system details (IP, username, SSH port)

4. Build and run with Docker:
```bash
docker build -t agent-controller .

docker run -d --name agent-app -p 5000:5000 \
  -v $(pwd)/keys:/app/keys \
  -v $(pwd)/session.json:/app/session.json \
  agent-controller
```

5. Access the web interface at `http://localhost:5000`

## Configuration

Edit `keys/config.ini` to configure:

- **General**: LLM provider and API keys
- **Agent**: Model name, max steps, timeouts, summarization threshold
- **System**: Target SSH connection details
- **Prompts**: Customize agent behavior for different scenarios

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

This project is open source and available under the MIT License.

## Contributing

Contributions are welcome! Please feel free to submit issues or pull requests.

## Support

For questions or issues, please open an issue on GitHub.
