# Gemini Agent Controller

## Project Overview

This project is a web-based UI for controlling an AI agent that can execute commands on a remote system via SSH. The agent can be configured to use different LLMs (Ollama or Gemini) and can operate in either an "independent" mode (where it validates its own commands) or an "assisted" mode (where it requires user approval for each command).

**Main Technologies:**

*   **Backend:** Python, Flask, Flask-SocketIO, eventlet
*   **Frontend:** HTML, CSS, JavaScript, Socket.IO
*   **AI/LLM:** LangChain, Ollama, Google Gemini
*   **SSH:** Paramiko
*   **Containerization:** Docker

## Building and Running

The application is designed to be run as a Docker container.

**Dependencies:**

The Python dependencies are listed in the `dockerfile`:
*   `pydantic`
*   `pydantic-settings`
*   `langchain`
*   `langchain-core`
*   `langchain-community`
*   `langchain-google-genai`
*   `google-generativeai`
*   `paramiko`
*   `Flask`
*   `Flask-SocketIO`
*   `requests`
*   `gunicorn`
*   `eventlet`

**Running the Application:**

1.  **Build the Docker image:**
    ```bash
    docker build -t ai-agent-controller .
    ```

2.  **Run the Docker container:**
    ```bash
    docker run -p 5000:5000 -v $(pwd)/keys:/app/keys ai-agent-controller
    ```
    *   The `-v $(pwd)/keys:/app/keys` volume mount is important to persist the SSH keys and other session data.

The application will be accessible at `http://localhost:5000`.

## Development Conventions

*   **Configuration:** The application is configured through the `config.ini` file. This file is created with default values if it doesn't exist.
*   **State Management:** The application's state is managed in a global dictionary (`GLOBAL_STATE`) in `app.py`. This state is passed to the agent's thread and is also saved to `session.json` for persistence.
*   **Modularity:** The code is well-structured into modules with specific responsibilities:
    *   `app.py`: The main Flask application.
    *   `agent_core.py`: The core logic of the AI agent.
    *   `config.py`: Configuration management.
    *   `ssh_utils.py`: SSH-related functionality.
    *   `llm_utils.py`: LLM interaction utilities.
    *   `session_manager.py`: Session state management.
*   **Real-time Communication:** Flask-SocketIO is used for real-time communication between the backend and the frontend.
*   **Error Handling:** The code includes `try...except` blocks for handling errors, especially in areas like SSH connections and LLM interactions.

## Aliases

No aliases are defined in the current environment.