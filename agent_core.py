import re
import json
import traceback
import time
import threading
import concurrent.futures
from datetime import datetime
from time import sleep # Folosim sleep direct
from langchain_community.llms import Ollama
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import PromptTemplate

# Importam functiile necesare din modulele separate
from config import get_config, VALIDATOR_WHITELIST_PATH
from ssh_utils import execute_ssh_command, set_detected_os, execute_ssh_block_command, is_block_command, parse_block_command
from log_manager import UnifiedLogManager

# --- API Task Registry ---
# Stores state for asynchronous API tasks (task_id -> task_info dict)
# Each entry contains: status, start_time, result, current_step, latest_activity, last_updated
API_TASK_REGISTRY = {}

# --- Chat Processing Lock ---
# Prevents concurrent process_chat_message calls from racing on the same LLM
_chat_processing_lock = threading.Lock()

# --- Chat Cancel Flag ---
# Set by cancel_chat_processing() to interrupt the active chat turn loop
_chat_cancel_event = threading.Event()

def cancel_chat_processing():
    """Signal the active chat processing loop to stop at the next safe checkpoint."""
    _chat_cancel_event.set()

# ---
# --- Functii Helper pentru Logare ---
# ---

def broadcast_emit(socketio, event, data, global_state=None):
    """
    Helper function for socket emissions that handles API vs UI context.

    When a task is triggered via API (headless), there's no specific session ID,
    so we broadcast to ALL connected clients for live view updates.
    When triggered via UI, we also broadcast so other tabs/users can see.

    This ensures the Web UI updates automatically without refresh.
    """
    # Check if this is an API-triggered task (no UI session)
    is_api_task = global_state.get('is_api_task', False) if global_state else False

    # Always broadcast - this ensures all connected browsers see updates
    # Whether triggered by API or UI, we want live view for everyone
    socketio.emit(event, data)


def log_and_emit(socketio, global_state, message, clear=False):
    """Functie helper pentru a loga, emite prin socket si salva in stare."""
    print(message, flush=True) # Logam in consola serverului
    global_state['last_session']['log'] += message + '\n' # Adaugam la log-ul complet
    # Persist to disk so Agent Execution Log survives Docker rebuilds
    try:
        from config import AGENT_LIVE_LOG_PATH
        with open(AGENT_LIVE_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(message + '\n')
    except Exception:
        pass  # Don't break execution if disk write fails
    # Use broadcast_emit for consistent live view behavior
    broadcast_emit(socketio, 'agent_log', {'data': message, 'clear': clear}, global_state)


def get_configured_system_names():
    """
    Returns a comma-separated list of configured system names from connections.json.
    Used for injecting available systems into chat prompts.
    """
    try:
        import session_manager
        connections = session_manager.load_connections()
        if connections:
            system_names = []
            for conn in connections:
                # Use friendly name if available, otherwise user@ip
                name = conn.get('name', f"{conn.get('username', 'unknown')}@{conn.get('ip', 'unknown')}")
                system_names.append(name)
            return ', '.join(system_names)
    except Exception as e:
        print(f"[get_configured_system_names] Error: {e}", flush=True)
    return "None defined"


def get_timestamped_step_header(step_counter: int) -> str:
    """
    Generate a timestamped step header for LLM context entries.
    Format: --- STEP X [YYYY-MM-DD HH:MM:SS] ---
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"--- STEP {step_counter} [{timestamp}] ---"


class ThinkingIndicator:
    """Clasa pentru gestionarea indicatorului 'Thinking...' cu timer in-place."""
    def __init__(self, socketio, timeout_seconds=120):
        self.socketio = socketio
        self.timeout_seconds = timeout_seconds
        self.start_time = None
        self.stop_flag = False
        self.greenlet = None

    def start(self):
        """Porneste indicatorul de thinking cu countdown timer."""
        try:
            self.start_time = time.time()
            self.stop_flag = False
            # Emit mesajul initial
            self.socketio.emit('thinking_start', {'timeout': self.timeout_seconds})
            # Pornim un thread daemon pentru actualizarea timer-ului
            self._thread = threading.Thread(target=self._update_timer, daemon=True)
            self._thread.start()
        except Exception as e:
            print(f"ThinkingIndicator start error: {e}")

    def _update_timer(self):
        """Actualizeaza timer-ul la fiecare secunda."""
        try:
            while not self.stop_flag:
                elapsed = int(time.time() - self.start_time)
                remaining = max(0, self.timeout_seconds - elapsed)
                self.socketio.emit('thinking_update', {'remaining': remaining})
                if remaining <= 0:
                    break
                time.sleep(1)
        except Exception as e:
            print(f"ThinkingIndicator update error: {e}")

    def stop(self):
        """Opreste indicatorul de thinking."""
        try:
            self.stop_flag = True
            self.socketio.emit('thinking_end', {})
        except Exception as e:
            print(f"ThinkingIndicator stop error: {e}")

class CommandExecutionTimer:
    """Clasa pentru gestionarea timer-ului de executie comenzi SSH cu countdown in-place."""
    def __init__(self, socketio, global_state, command="", specific_timeout=None):
        self.socketio = socketio
        self.global_state = global_state
        self.command = command
        self.specific_timeout = specific_timeout  # Store the specific timeout
        self.start_time = None
        self.stop_flag = False
        self._thread = None

    def start(self):
        """Porneste timer-ul de executie."""
        try:
            self.start_time = time.time()
            self.stop_flag = False
            # Use specific timeout if provided, else global
            current_timeout = self.specific_timeout if self.specific_timeout else self.global_state.get('command_timeout', 300)

            self.socketio.emit('command_exec_start', {'timeout': current_timeout, 'command': self.command})
            # Pornim un thread daemon pentru actualizarea timer-ului
            self._thread = threading.Thread(target=self._update_timer, daemon=True)
            self._thread.start()
        except Exception as e:
            print(f"CommandExecutionTimer start error: {e}")

    def _update_timer(self):
        """Actualizeaza timer-ul la fiecare secunda."""
        try:
            while not self.stop_flag:
                elapsed = int(time.time() - self.start_time)
                # Use specific timeout if provided, else global
                current_timeout = self.specific_timeout if self.specific_timeout else self.global_state.get('command_timeout', 300)

                remaining = max(0, current_timeout - elapsed)
                self.socketio.emit('command_exec_update', {'remaining': remaining})
                if remaining <= 0:
                    break
                time.sleep(1)
        except Exception as e:
            print(f"CommandExecutionTimer update error: {e}")

    def stop(self):
        """Opreste timer-ul de executie."""
        try:
            self.stop_flag = True
            self.socketio.emit('command_exec_end', {})
        except Exception as e:
            print(f"CommandExecutionTimer stop error: {e}")

# ---
# --- Functii Core Agent ---
# ---

def parse_command_log(full_log):
    """Extrage doar liniile relevante (comenzi, pasi, etc.) pentru afisajul live."""
    if not isinstance(full_log, str):
        return "Invalid log data."
        
    filtered_lines = []
    # Cuvinte cheie care indica o linie de afisat in log-ul filtrat
    log_filter_keywords = [
        '===', '---', 'STEP', 'COMMAND:', 'Executing Command:', 'REASON:', 
        'REPORT:', 'ASK:', 'Validating command', 'Auto-Rejected', 
        'Auto-Validated', 'Pager disabled', 'Intervention:', 'Human Response:',
        'ERROR:', 'CRITICAL:', 'FATAL:', 'Exception:', 'Timeout:', 'Objective updated'
    ]

    for line in full_log.splitlines():
        # Verificam daca linia incepe (dupa spatii) cu unul din cuvintele cheie
        if any(line.strip().startswith(keyword) for keyword in log_filter_keywords):
            filtered_lines.append(line)
            
    return "\n".join(filtered_lines)

def clean_command_string(raw_command):
    """
    Cleans formatting artifacts from the LLM response.
    Removes Markdown code blocks, inline backticks, and surrounding quotes.
    """
    if not raw_command:
        return ""

    cmd = raw_command.strip()

    # 1. Remove Markdown Code Blocks (```bash ... ```)
    # Regex looks for starting ``` (optional lang) and ending ```
    # We handle multiline just in case, though commands should be single line per our prompt
    if cmd.startswith("```"):
        # Remove the first line (``` or ```bash)
        cmd = re.sub(r"^```[a-zA-Z0-9]*\s*", "", cmd)
        # Remove the last line (```) if present
        if cmd.endswith("```"):
            cmd = cmd[:-3]
        cmd = cmd.strip()

    # 2. Remove Inline Code (`...`)
    # Only if the WHOLE string is wrapped in backticks
    if cmd.startswith("`") and cmd.endswith("`") and len(cmd) > 2:
        cmd = cmd[1:-1].strip()

    # 3. Remove Surrounding Quotes ("..." or '...')
    # Only if the WHOLE string is wrapped.
    # Example: "ls -la" -> ls -la (Fixes LLM artifact)
    # Example: grep "foo" bar -> grep "foo" bar (Touched nothing, which is correct)
    if (cmd.startswith('"') and cmd.endswith('"')) and len(cmd) > 2:
        cmd = cmd[1:-1].strip()
    elif (cmd.startswith("'") and cmd.endswith("'")) and len(cmd) > 2:
        cmd = cmd[1:-1].strip()

    # 4. Remove leading markdown/decorative symbols that LLMs sometimes add
    # These are NOT valid bash command prefixes and break execution
    # Examples: **, ***, ##, ###, -, >, etc.
    max_iterations = 10  # Safety limit to prevent infinite loops
    iteration_count = 0

    while cmd and iteration_count < max_iterations:
        original_cmd = cmd
        iteration_count += 1

        # Pattern 1: Asterisks (markdown bold/italic: *, **, ***)
        if cmd.startswith('***'):
            cmd = cmd[3:].lstrip()
        elif cmd.startswith('**'):
            cmd = cmd[2:].lstrip()
        elif cmd.startswith('*') and (len(cmd) == 1 or cmd[1] in (' ', '\t', '*')):
            cmd = cmd[1:].lstrip()

        # Pattern 2: Hash symbols (markdown headers: #, ##, ###, ####)
        elif cmd.startswith('####'):
            cmd = cmd[4:].lstrip()
        elif cmd.startswith('###'):
            cmd = cmd[3:].lstrip()
        elif cmd.startswith('##'):
            cmd = cmd[2:].lstrip()
        elif cmd.startswith('#') and (len(cmd) == 1 or cmd[1] in (' ', '\t', '#')):
            cmd = cmd[1:].lstrip()

        # Pattern 3: Leading dash (markdown list: -, --)
        # CAREFUL: Don't remove single - followed by letter (valid flag like -l)
        elif cmd.startswith('--') and (len(cmd) == 2 or cmd[2] in (' ', '\t', '-')):
            cmd = cmd[2:].lstrip()
        elif cmd.startswith('-') and (len(cmd) == 1 or cmd[1] in (' ', '\t')):
            cmd = cmd[1:].lstrip()

        # Pattern 4: Leading greater-than (markdown quote: >, >>)
        # CAREFUL: Don't remove redirection (e.g., > file.txt should be preserved if it's a full command)
        elif cmd.startswith('>>') and (len(cmd) == 2 or cmd[2] in (' ', '\t', '>')):
            cmd = cmd[2:].lstrip()
        elif cmd.startswith('>') and (len(cmd) == 1 or cmd[1] in (' ', '\t', '>')):
            cmd = cmd[1:].lstrip()

        # Pattern 5: Underscore repeating (markdown separator: ___, __, _)
        elif cmd.startswith('___'):
            cmd = cmd[3:].lstrip()
        elif cmd.startswith('__') and (len(cmd) == 2 or cmd[2] in (' ', '\t', '_')):
            cmd = cmd[2:].lstrip()
        elif cmd.startswith('_') and (len(cmd) == 1 or cmd[1] in (' ', '\t', '_')):
            cmd = cmd[1:].lstrip()

        # No more patterns matched - command is clean
        if cmd == original_cmd:
            break

    return cmd.strip()

# ---
# --- Pure Accumulation Strategy: No Helper Functions Needed ---
# ---
# (All summarization is now handled directly by log_manager.py)

def summarize_history(socketio, global_state, force_summary=False):
    """
    PURE SUMMARIZATION:
    Takes the ENTIRE current LLM context from disk.
    Sends it to LLM to create a NEW Summary.
    Overwrites the context file with the new summary.
    """
    log_manager = global_state.get('log_manager')
    if not log_manager:
        return

    # 1. Get the full text we want to summarize (Source of Truth)
    full_context_to_compress = log_manager.get_llm_context()
    current_objective = global_state.get('current_objective', "No objective set.")

    log_prefix = "--- Summarization ---"

    # BUG FIX: Only skip if context is truly empty or is JUST the default message.
    is_default_msg = "No commands have been executed yet" in full_context_to_compress
    is_short = len(full_context_to_compress) < 500

    if not full_context_to_compress or (is_default_msg and is_short):
        log_and_emit(socketio, global_state, f"{log_prefix} No history to summarize. Skipping.")
        return

    log_and_emit(socketio, global_state, f"\n{log_prefix}\nCompressing history ({len(full_context_to_compress)} chars)...")
    socketio.emit('summarization_status', {'active': True, 'message': 'Summarizing history...'})

    try:
        cfg = get_config()
        provider = cfg.get('General', 'provider', fallback='')
        model_name = cfg.get('Agent', 'model_name', fallback='')

        # Determine Prompt (Cloud prompts for Gemini/Anthropic, Ollama prompts for Ollama)
        prompt_key = 'OllamaSummarizePrompt' if provider == 'ollama' else 'CloudSummarizePrompt'
        prompt_template_str = cfg.get(prompt_key, 'template', fallback="Summarize: {history}")

        # Init LLM
        temperature = cfg.getfloat('Agent', 'temperature', fallback=0.5)
        llm = None
        if provider == 'ollama':
            api_url = cfg.get('Ollama', 'api_url', fallback='')
            ollama_num_ctx = cfg.getint('Ollama', 'num_ctx', fallback=32768)
            keep_alive = cfg.get('Ollama', 'keep_alive', fallback='-1')
            sum_timeout = cfg.getint('Agent', 'llm_timeout', fallback=120)
            llm = Ollama(model=model_name, base_url=api_url, timeout=sum_timeout, num_ctx=ollama_num_ctx, temperature=temperature, keep_alive=keep_alive)
        elif provider == 'gemini':
            api_key = cfg.get('General', 'gemini_api_key', fallback='')
            llm = ChatGoogleGenerativeAI(model=model_name, google_api_key=api_key, generation_config={"temperature": temperature})
        elif provider == 'anthropic':
            api_key = cfg.get('General', 'anthropic_api_key', fallback='')
            llm = ChatAnthropic(model=model_name, api_key=api_key, temperature=temperature)

        if not llm:
             log_and_emit(socketio, global_state, f"{log_prefix} ERROR: LLM not configured.")
             return

        # Prepare Prompt
        prompt = PromptTemplate.from_template(prompt_template_str).format(
            history=full_context_to_compress,
            objective=current_objective
        )

        # Execute
        summary = ""
        for i in range(3):
            try:
                log_and_emit(socketio, global_state, f"{log_prefix} Generating summary (Attempt {i+1})...")
                socketio.emit('summarization_status', {'active': True, 'message': f'Generating summary (attempt {i+1}/3)...'})
                raw_summary = llm.invoke(prompt)
                if provider == 'gemini' and hasattr(raw_summary, 'content'):
                    summary = raw_summary.content
                else:
                    summary = str(raw_summary)

                if summary.strip(): break
                sleep(2)
            except Exception as e:
                print(f"Summarization attempt failed: {e}")
                sleep(2)

        if not summary.strip():
            log_and_emit(socketio, global_state, f"{log_prefix} FAILED to generate summary. Continuing with raw history.")
            socketio.emit('summarization_status', {'active': False})
            return

        # --- COMMIT THE CHANGE ---
        # We append a specific header to the summary so the agent knows it's a summary
        final_summary_text = f"--- History has been summarized ---\n{summary.strip()}"

        # Overwrite the Context File on Disk
        log_manager.set_summarized_history(final_summary_text)

        # Update UI State
        new_context = log_manager.get_llm_context()
        global_state['agent_history'] = new_context
        socketio.emit('update_history', {'data': new_context})

        log_and_emit(socketio, global_state, f"{log_prefix} COMPLETED. New context size: {len(new_context)} chars.")
        socketio.emit('summarization_status', {'active': False})

    except Exception as e:
        log_and_emit(socketio, global_state, f"{log_prefix} CRITICAL ERROR: {e}")
        traceback.print_exc()
        socketio.emit('summarization_status', {'active': False})


def test_ssh_connectivity(socketio, global_state):
    """
    Testa conectivitatea SSH cu un simplu 'echo' command.
    Returneaza True daca conexiunea este activa, False altfel.
    """
    try:
        result = execute_ssh_command("echo 'connectivity_test'")
        if "connectivity_test" in result and "Error" not in result:
            log_and_emit(socketio, global_state, "--- Connectivity Test: PASSED ---")
            return True
        else:
            log_and_emit(socketio, global_state, "--- Connectivity Test: FAILED (unexpected response) ---")
            return False
    except Exception as e:
        log_and_emit(socketio, global_state, f"--- Connectivity Test: FAILED ({str(e)}) ---")
        return False

def _try_convert_heredoc_to_base64(command: str):
    """
    If the command is a heredoc write (cat/tee > file << 'EOF' ... EOF),
    convert it to a reliable base64 write command.
    Returns (base64_command, file_path) on match, or (None, None) if not a heredoc write.
    Handles: append (>>), sudo, any delimiter word, indented content (<<-).
    """
    import base64 as _b64
    m = re.match(
        r'^\s*(sudo\s+)?(?:cat|tee)\s*(>>|>)\s*(\S+)\s*<<-?\s*[\'"]?(\w+)[\'"]?\s*\n(.*?)\n[^\S\r\n]*\4[^\S\r\n]*$',
        command.strip(),
        re.DOTALL
    )
    if not m:
        return None, None
    use_sudo = bool(m.group(1))
    append = m.group(2) == '>>'
    path = m.group(3)
    content = m.group(5)
    # Strip leading tab/space added by <<- (indented heredoc)
    content_lines = [l.lstrip('\t') for l in content.split('\n')]
    content = '\n'.join(content_lines)
    b64 = _b64.b64encode(content.encode('utf-8')).decode('ascii')
    redirect = '>>' if append else '>'
    if use_sudo:
        cmd = f"echo '{b64}' | base64 -d | sudo tee {redirect} {path} > /dev/null"
    else:
        cmd = f"echo '{b64}' | base64 -d {redirect} {path}"
    return cmd, path


def execute_ssh_command_with_timeout(socketio, global_state, command, timeout_seconds, max_retries=3):
    """
    Executa o comanda SSH cu timeout si retry logic.
    timeout_seconds is the step-specific timeout (already clamped to user limit).
    User can still lower it via UI during execution (emergency abort).
    Returneaza (success, result, attempt_number).
    """
    def _effective_timeout():
        """Step timeout is primary. User can lower it via UI but not raise above step value."""
        ui_timeout = global_state.get('command_timeout', timeout_seconds)
        return min(timeout_seconds, ui_timeout)

    for attempt in range(1, max_retries + 1):
        try:
            current_timeout = _effective_timeout()
            log_and_emit(socketio, global_state, f"Executing command (attempt {attempt}/{max_retries}, timeout: {current_timeout}s)...")

            # Create an event to signal completion
            execution_complete = threading.Event()
            execution_result = {'success': False, 'data': None, 'timed_out': False, 'streamed': False}
            start_time = time.time()

            def execute_command_thread():
                """Thread that executes the SSH command."""
                try:
                    # Check if this is a block command (multi-line or Cisco config)
                    if is_block_command(command):
                        # Parse into list of commands
                        commands_list = parse_block_command(command)
                        log_and_emit(socketio, global_state, f"[BLOCK MODE] Executing {len(commands_list)} commands in interactive session...")

                        # Get enable password from config
                        cfg = get_config()
                        enable_pwd = cfg.get('System', 'enable_password', fallback='')

                        effective = _effective_timeout()
                        result = execute_ssh_block_command(
                            commands=commands_list,
                            enable_password=enable_pwd if enable_pwd else None,
                            timeout_per_command=effective // max(len(commands_list), 1)
                        )
                        execution_result['streamed'] = False
                    else:
                        # Strip block markers if the LLM generated them on a non-network device
                        clean_command = command
                        for _marker in ['BLOCK:', 'CONFIG:', 'INTERACTIVE:']:
                            if clean_command.strip().upper().startswith(_marker):
                                clean_command = clean_command.strip()[len(_marker):].strip()
                                break

                        # Intercept heredoc writes and convert to safe base64 commands
                        heredoc_cmd, heredoc_path = _try_convert_heredoc_to_base64(clean_command)
                        if heredoc_cmd:
                            print(f"[AGENT] Heredoc write intercepted → converting to base64 write for: {heredoc_path}", flush=True)
                            clean_command = heredoc_cmd

                        # Streaming single command: throttled emit to avoid flooding the browser.
                        # Accumulate chunks and flush every 400ms or every 32KB — whichever comes first.
                        _stream_buf = []
                        _stream_last_flush = [time.time()]

                        def _flush_stream_buf():
                            if _stream_buf:
                                broadcast_emit(socketio, 'vm_screen', {'data': ''.join(_stream_buf)}, global_state)
                                _stream_buf.clear()
                            _stream_last_flush[0] = time.time()

                        def on_chunk(chunk_text):
                            _stream_buf.append(chunk_text)
                            buf_size = sum(len(s) for s in _stream_buf)
                            if buf_size >= 32768 or (time.time() - _stream_last_flush[0]) >= 0.4:
                                _flush_stream_buf()

                        result = execute_ssh_command(clean_command, on_chunk=on_chunk)
                        _flush_stream_buf()  # flush any remaining buffered output
                        execution_result['streamed'] = True

                    execution_result['success'] = True
                    execution_result['data'] = result
                except Exception as e:
                    execution_result['streamed'] = False
                    execution_result['success'] = False
                    execution_result['data'] = str(e)
                finally:
                    execution_complete.set()

            def timeout_monitor_thread():
                """Thread that monitors timeout. Uses step timeout, respects UI lowering."""
                try:
                    while not execution_complete.is_set():
                        elapsed = time.time() - start_time
                        current_limit = _effective_timeout()

                        if elapsed >= current_limit:
                            execution_result['timed_out'] = True
                            execution_complete.set()
                            break

                        time.sleep(0.5)  # Check every 0.5 seconds
                except:
                    pass

            # Start both threads
            exec_thread = threading.Thread(target=execute_command_thread, daemon=True)
            monitor_thread = threading.Thread(target=timeout_monitor_thread, daemon=True)
            exec_thread.start()
            monitor_thread.start()

            # Wait for completion or timeout
            execution_complete.wait()

            # Check if timed out
            if execution_result['timed_out']:
                actual_timeout = _effective_timeout()
                log_and_emit(socketio, global_state, f"--- TIMEOUT after {actual_timeout}s (attempt {attempt}/{max_retries}) ---")
                raise TimeoutError(f"Command timed out after {actual_timeout}s")

            # Return result if successful
            if execution_result['success']:
                return True, execution_result['data'], attempt, execution_result['streamed']
            else:
                raise Exception(execution_result['data'])

        except TimeoutError:
            actual_timeout = _effective_timeout()
            log_and_emit(socketio, global_state, f"--- TIMEOUT after {actual_timeout}s (attempt {attempt}/{max_retries}) ---")

            # Test connectivity immediately to see if host is down or just busy
            log_and_emit(socketio, global_state, "--- Testing SSH connectivity... ---")
            is_connected = test_ssh_connectivity(socketio, global_state)

            if is_connected:
                # LOGIC CHANGE: Connection is ALIVE, so the command is just slow/stuck.
                # Do NOT retry (Fail Fast). Retrying identical slow commands just piles up load.
                log_and_emit(socketio, global_state, "--- Connection is ALIVE. Command execution took too long. Aborting retries. ---")

                error_msg_context = (
                    f"Error: Command timed out after {actual_timeout} seconds.\n"
                    "WARNING: The process may still be running in the background on the remote system.\n"
                    "SUGGESTION: Check for running processes (ps/jobs), terminate if necessary, and optimize your command or increase timeout."
                )
                # Return False immediately, skip remaining retries
                return False, error_msg_context, attempt, False
            else:
                # Connection died. This is a network failure.
                log_and_emit(socketio, global_state, "--- Connection lost. Aborting retries. ---")
                return False, "Error: Command timed out and connection was lost.", attempt, False

        except Exception as e:
            # Check if this error is due to User Stop
            if not global_state.get('task_running', False):
                log_and_emit(socketio, global_state, "--- Execution interrupted by User Stop. ---")
                return False, "Task stopped by user during execution.", attempt, False

            log_and_emit(socketio, global_state, f"--- Command execution error: {str(e)} ---")
            if attempt < max_retries:
                log_and_emit(socketio, global_state, f"--- Retrying... ---")
                sleep(2)
            else:
                return False, f"Error: {str(e)}", attempt, False

    # Should not reach here, but just in case
    return False, "Error: Unexpected execution flow.", max_retries, False

def detect_sudo_capability(socketio, global_state):
    """
    Detecteaza daca utilizatorul curent poate folosi sudo fara parola.
    Actualizeaza global_state['sudo_available'] cu rezultatul.
    Returneaza True/False.
    """
    log_prefix = "--- Sudo Detection ---"

    try:
        # Pe Windows, sudo nu exista - setam direct False
        os_result = execute_ssh_command("uname -s 2>/dev/null || echo 'Windows'")
        if "Windows" in os_result or "not recognized" in os_result or "Error" in os_result:
            global_state['sudo_available'] = False
            log_and_emit(socketio, global_state, f"{log_prefix} Windows detected - sudo not applicable")
            return False

        # Verificam daca putem rula sudo fara parola (doar pe Unix/Linux)
        test_command = "sudo -n true"
        result = execute_ssh_command(test_command)

        # Daca comanda a reusit fara eroare, sudo este disponibil
        if "Error" not in result and "password" not in result.lower():
            global_state['sudo_available'] = True
            log_and_emit(socketio, global_state, f"{log_prefix} Passwordless sudo detected: AVAILABLE")
            return True
        else:
            global_state['sudo_available'] = False
            log_and_emit(socketio, global_state, f"{log_prefix} Passwordless sudo: NOT AVAILABLE")
            return False

    except Exception as e:
        log_and_emit(socketio, global_state, f"{log_prefix} Detection failed: {e}")
        global_state['sudo_available'] = False
        return False

def detect_system_info(global_state):
    """
    Detect OS, username, sudo capability and update global_state['system_os_info'].
    Called after system switch or at task start. Does NOT emit to UI log.
    """
    try:
        os_result = execute_ssh_command("uname -s 2>/dev/null || ver")
        user_result = execute_ssh_command("whoami")

        detected_os = "Unknown"
        if "Linux" in os_result:
            detected_os = "Linux"
        elif "Darwin" in os_result:
            detected_os = "macOS"
        elif "Windows" in os_result or "Microsoft" in os_result or "Version" in os_result:
            detected_os = "Windows (or non-Unix)"
        elif "Error" in os_result or not os_result.strip():
            win_test = execute_ssh_command("echo %OS%")
            if "Windows" in win_test:
                detected_os = "Windows (or non-Unix)"
            else:
                detected_os = "Windows (or non-Unix)"

        # Clean user result
        detected_user = "unknown"
        if "Error" not in user_result:
            user_lines = [line.strip() for line in user_result.split('\n') if line.strip()]
            raw_user = user_lines[-1] if user_lines else "unknown"
            detected_user = raw_user.split('\\')[-1] if '\\' in raw_user else raw_user

        # Sudo detection
        if detected_os == "Windows (or non-Unix)":
            global_state['sudo_available'] = False
            sudo_status = "not applicable (Windows)"
        else:
            try:
                sudo_result = execute_ssh_command("sudo -n true")
                if "Error" not in sudo_result and "password" not in sudo_result.lower():
                    global_state['sudo_available'] = True
                    sudo_status = "available (passwordless)"
                else:
                    global_state['sudo_available'] = False
                    sudo_status = "not available or requires password"
            except Exception:
                global_state['sudo_available'] = False
                sudo_status = "not available or requires password"

        # Get device type from config
        cfg_device = get_config()
        device_type = cfg_device.get('System', 'device_type', fallback='linux').strip().lower()

        if device_type in ['cisco', 'nxos', 'iosxr', 'iosxe', 'brocade', 'juniper', 'arista', 'other']:
            system_context = f"Device: {device_type.upper()}, User: {detected_user}, BLOCK_MODE: ENABLED"
            global_state['device_type'] = device_type
            global_state['block_mode'] = True
        else:
            system_context = f"OS: {detected_os}, User: {detected_user}, Sudo: {sudo_status}"
            global_state['device_type'] = device_type
            global_state['block_mode'] = False

        global_state['system_os_info'] = system_context
        set_detected_os(detected_os)
        print(f"[SYSTEM INFO] Detected: {system_context}", flush=True)
        return system_context

    except Exception as e:
        print(f"[SYSTEM INFO] Detection failed: {e}", flush=True)
        global_state['system_os_info'] = "Unknown OS (detection failed)"
        return "Unknown OS (detection failed)"

# ---------------------------------------------------------------------------
# --- Per-system Validator Whitelist ---
# Commands approved by the LLM validator are stored per system_name so future
# identical commands skip the LLM call entirely.
# Commands containing shell metacharacters are NEVER whitelisted — they always
# go through the LLM validator regardless of prior approvals.
# ---------------------------------------------------------------------------

# Detects shell metacharacters that make a command unsafe to whitelist.
_SHELL_METACHAR_RE = re.compile(r'[;|&<>`]|\$\(|\n')

def _has_shell_metacharacters(cmd: str) -> bool:
    return bool(_SHELL_METACHAR_RE.search(cmd))

def _load_validator_whitelist() -> dict:
    """Load the per-system whitelist from disk. Returns {} on missing/corrupt file."""
    try:
        with open(VALIDATOR_WHITELIST_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        print(f"[VALIDATOR] Error loading whitelist: {e}", flush=True)
        return {}

def _save_validator_whitelist(whitelist: dict):
    """Persist the per-system whitelist to disk."""
    try:
        with open(VALIDATOR_WHITELIST_PATH, 'w', encoding='utf-8') as f:
            json.dump(whitelist, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[VALIDATOR] Error saving whitelist: {e}", flush=True)

def _whitelist_check(system_name: str, command: str) -> bool:
    """Return True if command is in the per-system whitelist (no LLM needed)."""
    if _has_shell_metacharacters(command):
        return False  # Safety gate: complex commands always go to LLM
    return command.strip() in _load_validator_whitelist().get(system_name, [])

def _whitelist_add(system_name: str, command: str):
    """Add an LLM-approved command to the per-system whitelist (if safe to whitelist)."""
    if _has_shell_metacharacters(command):
        return  # Never whitelist commands with pipes/redirects/etc.
    cmd = command.strip()
    if not cmd:
        return
    whitelist = _load_validator_whitelist()
    if system_name not in whitelist:
        whitelist[system_name] = []
    if cmd not in whitelist[system_name]:
        whitelist[system_name].append(cmd)
        _save_validator_whitelist(whitelist)
        print(f"[VALIDATOR] Added to whitelist for '{system_name}': {cmd}", flush=True)


def validate_command_with_llm(socketio, global_state, command_to_validate, reason=""):
    """
    Verifica o comanda folosind un LLM (validator) pentru a preveni output-ul excesiv
    sau comenzile care blocheaza.
    Include sistem de retry (10 incercari) si context despre sudo si motivatia agentului.
    Returneaza (True, "OK") sau (False, "Motiv respingere").
    """
    log_prefix = "--- Command Validator ---"
    log_and_emit(socketio, global_state, f"{log_prefix} Validating command: {command_to_validate}")

    try:
        cfg = get_config()

        # Priority: ValidatorLLM > ChatLLM > ExecLLM
        val_enabled = cfg.getboolean('ValidatorLLM', 'enabled', fallback=False)
        val_model = cfg.get('ValidatorLLM', 'model_name', fallback='').strip()
        chat_enabled = cfg.getboolean('ChatLLM', 'enabled', fallback=False)
        chat_model = cfg.get('ChatLLM', 'model_name', fallback='').strip()

        if val_enabled and val_model:
            provider = cfg.get('ValidatorLLM', 'provider', fallback='ollama')
            model_name = val_model
            source_label = "ValidatorLLM"
        elif chat_enabled and chat_model:
            provider = cfg.get('ChatLLM', 'provider', fallback='ollama')
            model_name = chat_model
            source_label = "ChatLLM (validator fallback)"
        else:
            provider = cfg.get('General', 'provider', fallback='')
            model_name = cfg.get('Agent', 'model_name', fallback='')
            source_label = "ExecLLM (validator fallback)"

        if not provider or not model_name:
            log_and_emit(socketio, global_state, f"{log_prefix} ERROR: Validator LLM not configured.")
            return False, "Validator LLM not configured."

        log_and_emit(socketio, global_state, f"{log_prefix} Using {source_label}: {provider}/{model_name}")

        # Determinam ce prompt sa folosim (Cloud prompts for Gemini/Anthropic, Ollama prompts for Ollama)
        prompt_key = 'OllamaValidatePrompt' if provider == 'ollama' else 'CloudValidatePrompt'
        prompt_template_str = cfg.get(prompt_key, 'template', fallback="Analyze: {command}\nRespond APPROVE or REJECT")

        val_timeout = cfg.getint('Agent', 'llm_timeout', fallback=120)

        # Initializam clientul LLM
        def _build_validator_llm(prov, mod, section='ValidatorLLM'):
            """Build a validator LLM from the given provider/model/section config."""
            if prov == 'ollama':
                sec_url = cfg.get(section, 'ollama_url', fallback='').strip()
                api_url = sec_url or cfg.get('Ollama', 'api_url', fallback='')
                if not api_url:
                    return None, "Ollama URL not configured."
                ollama_num_ctx = cfg.getint('Ollama', 'num_ctx', fallback=32768)
                keep_alive = cfg.get('Ollama', 'keep_alive', fallback='-1')
                return Ollama(model=mod, base_url=api_url, timeout=val_timeout, num_ctx=ollama_num_ctx, keep_alive=keep_alive), None
            elif prov == 'gemini':
                sec_key = cfg.get(section, 'api_key', fallback='').strip()
                api_key = sec_key or cfg.get('General', 'gemini_api_key', fallback='')
                if not api_key:
                    return None, "Gemini API Key not configured."
                return ChatGoogleGenerativeAI(model=mod, google_api_key=api_key, generation_config={"temperature": 0.0}), None
            elif prov == 'anthropic':
                sec_key = cfg.get(section, 'api_key', fallback='').strip()
                api_key = sec_key or cfg.get('General', 'anthropic_api_key', fallback='')
                if not api_key:
                    return None, "Anthropic API Key not configured."
                return ChatAnthropic(model=mod, api_key=api_key, temperature=0.0), None
            else:
                return None, f"Unknown validator LLM provider '{prov}'."

        # Determine config section to read Ollama URL / API key from
        if val_enabled and val_model:
            cfg_section = 'ValidatorLLM'
        elif chat_enabled and chat_model:
            cfg_section = 'ChatLLM'
        else:
            cfg_section = 'General'

        llm, err = _build_validator_llm(provider, model_name, cfg_section)
        if err:
            log_and_emit(socketio, global_state, f"{log_prefix} ERROR: {err}")
            return False, err

        # Formatare prompt cu toate variabilele necesare
        current_threshold = cfg.getint('Agent', 'summarization_threshold', fallback=15000)
        command_timeout = cfg.getint('Agent', 'command_timeout', fallback=120)
        sudo_available = global_state.get('sudo_available', False)
        sudo_status = "YES - passwordless sudo is configured" if sudo_available else "NO - sudo requires password or is unavailable"

        # Obtinem informatii despre sistem pentru validare OS-specifica
        system_info = global_state.get('system_os_info', 'Unknown OS')

        # Asiguram ca reason este intotdeauna un string
        reason_str = str(reason) if reason is not None else ""

        prompt = PromptTemplate.from_template(prompt_template_str).format(
            command=command_to_validate,
            summarization_threshold=current_threshold,
            command_timeout=command_timeout,
            sudo_available=sudo_status,
            reason=reason_str,
            system_info=system_info
        )

        # IMPROVEMENT: Apelam LLM-ul cu retry (10 incercari)
        max_retries = 10
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    log_and_emit(socketio, global_state, f"{log_prefix} Retry {attempt}/{max_retries}...")
                    sleep(1)  # Pauza scurta intre incercari

                raw_response = llm.invoke(prompt)

                # Extragem textul in functie de provider
                response_text = ""
                # Both Gemini and Anthropic use .content attribute
                if provider in ['gemini', 'anthropic'] and hasattr(raw_response, 'content'):
                    response_text = raw_response.content
                else:
                    response_text = str(raw_response)

                response_text = response_text.strip()

                # Cautam decizia
                if response_text.upper().startswith("APPROVE"):
                    log_and_emit(socketio, global_state, f"{log_prefix} Result: APPROVE")
                    return True, "Command approved by validator."

                elif response_text.upper().startswith("REJECT"):
                    # Extragem motivul
                    reason = response_text[len("REJECT"):].strip()
                    if reason.upper().startswith("REASON:"):
                        reason = reason[len("REASON:"):].strip()

                    log_and_emit(socketio, global_state, f"{log_prefix} Result: REJECT (Reason: {reason})")
                    return False, reason

                else:
                    # LLM-ul nu a raspuns corect - reincercam
                    if attempt < max_retries - 1:
                        log_and_emit(socketio, global_state, f"{log_prefix} Attempt {attempt+1}: Unclear response: '{response_text[:100]}...' Retrying...")
                        continue
                    else:
                        # Ultima incercare esuata - default la aprobare cu warning
                        log_and_emit(socketio, global_state, f"{log_prefix} WARNING: All retries failed. Defaulting to APPROVE (unsafe fallback).")
                        return True, f"Validator failed after {max_retries} attempts - command approved by default."

            except Exception as invoke_err:
                if attempt < max_retries - 1:
                    log_and_emit(socketio, global_state, f"{log_prefix} Attempt {attempt+1} error: {invoke_err}. Retrying...")
                    sleep(2)
                    continue
                else:
                    log_and_emit(socketio, global_state, f"{log_prefix} ERROR: All retries failed with exceptions.")
                    traceback.print_exc()
                    # Try ValidatorLLM fallback before giving up
                    fb_ok = cfg.getboolean('ValidatorLLMFallback', 'enabled', fallback=False)
                    fb_model = cfg.get('ValidatorLLMFallback', 'model_name', fallback='').strip()
                    fb_provider = cfg.get('ValidatorLLMFallback', 'provider', fallback='ollama')
                    if fb_ok and fb_model:
                        log_and_emit(socketio, global_state, f"{log_prefix} Trying ValidatorLLMFallback: {fb_provider}/{fb_model}")
                        fb_llm, fb_err = _build_validator_llm(fb_provider, fb_model, 'ValidatorLLMFallback')
                        if fb_llm and not fb_err:
                            try:
                                fb_raw = fb_llm.invoke(prompt)
                                fb_text = (fb_raw.content if hasattr(fb_raw, 'content') else str(fb_raw)).strip()
                                if fb_text.upper().startswith("APPROVE"):
                                    log_and_emit(socketio, global_state, f"{log_prefix} Fallback Result: APPROVE")
                                    return True, "Command approved by validator fallback."
                                elif fb_text.upper().startswith("REJECT"):
                                    fb_reason = fb_text[len("REJECT"):].strip()
                                    if fb_reason.upper().startswith("REASON:"):
                                        fb_reason = fb_reason[len("REASON:"):].strip()
                                    log_and_emit(socketio, global_state, f"{log_prefix} Fallback Result: REJECT ({fb_reason})")
                                    return False, fb_reason
                            except Exception as fb_invoke_err:
                                log_and_emit(socketio, global_state, f"{log_prefix} Fallback also failed: {fb_invoke_err}")
                    return True, f"Validator exception after {max_retries} attempts - command approved by default."

    except Exception as e:
        error_msg = f"--- UNEXPECTED ERROR during command validation setup: {e} ---"
        log_and_emit(socketio, global_state, error_msg)
        traceback.print_exc()
        return True, "Unexpected error in validator setup - command approved by default."


def validate_report_with_llm(socketio, global_state, report_text, objective, execution_history):
    """
    Specialized validator for final REPORT content.
    Compares the report's claims against the actual execution history.
    Uses ValidatorLLM > ChatLLM > ExecLLM priority (same as command validator).
    Returns (True, "OK") or (False, "reason: what's unverified").
    """
    log_prefix = "--- Report Validator ---"
    log_and_emit(socketio, global_state, f"{log_prefix} Validating final report...")

    try:
        cfg = get_config()

        # Same LLM priority chain as validate_command_with_llm
        val_enabled = cfg.getboolean('ValidatorLLM', 'enabled', fallback=False)
        val_model = cfg.get('ValidatorLLM', 'model_name', fallback='').strip()
        chat_enabled = cfg.getboolean('ChatLLM', 'enabled', fallback=False)
        chat_model = cfg.get('ChatLLM', 'model_name', fallback='').strip()

        if val_enabled and val_model:
            provider = cfg.get('ValidatorLLM', 'provider', fallback='ollama')
            model_name = val_model
            cfg_section = 'ValidatorLLM'
            source_label = "ValidatorLLM"
        elif chat_enabled and chat_model:
            provider = cfg.get('ChatLLM', 'provider', fallback='ollama')
            model_name = chat_model
            cfg_section = 'ChatLLM'
            source_label = "ChatLLM"
        else:
            provider = cfg.get('General', 'provider', fallback='')
            model_name = cfg.get('Agent', 'model_name', fallback='')
            cfg_section = 'General'
            source_label = "ExecLLM"

        if not provider or not model_name:
            log_and_emit(socketio, global_state, f"{log_prefix} No LLM configured — skipping report validation.")
            return True, "No LLM available for report validation."

        log_and_emit(socketio, global_state, f"{log_prefix} Using {source_label}: {provider}/{model_name}")

        prompt_key = 'OllamaValidateReportPrompt' if provider == 'ollama' else 'CloudValidateReportPrompt'
        prompt_template_str = cfg.get(prompt_key, 'template', fallback='')
        if not prompt_template_str:
            log_and_emit(socketio, global_state, f"{log_prefix} No report validator prompt configured — skipping.")
            return True, "No report validator prompt configured."

        val_timeout = cfg.getint('Agent', 'llm_timeout', fallback=120)

        # Build LLM instance
        llm = None
        if provider == 'ollama':
            sec_url = cfg.get(cfg_section, 'ollama_url', fallback='').strip() if cfg_section not in ('General',) else ''
            api_url = sec_url or cfg.get('Ollama', 'api_url', fallback='')
            ollama_num_ctx = cfg.getint('Ollama', 'num_ctx', fallback=32768)
            keep_alive = cfg.get('Ollama', 'keep_alive', fallback='-1')
            llm = Ollama(model=model_name, base_url=api_url, timeout=val_timeout, num_ctx=ollama_num_ctx, keep_alive=keep_alive)
        elif provider == 'gemini':
            sec_key = cfg.get(cfg_section, 'api_key', fallback='').strip() if cfg_section not in ('General',) else ''
            api_key = sec_key or cfg.get('General', 'gemini_api_key', fallback='')
            llm = ChatGoogleGenerativeAI(model=model_name, google_api_key=api_key, generation_config={"temperature": 0.0})
        elif provider == 'anthropic':
            sec_key = cfg.get(cfg_section, 'api_key', fallback='').strip() if cfg_section not in ('General',) else ''
            api_key = sec_key or cfg.get('General', 'anthropic_api_key', fallback='')
            llm = ChatAnthropic(model=model_name, api_key=api_key, temperature=0.0)

        if not llm:
            return True, "Could not build report validator LLM."

        # No trim needed — execution_history is the LLM context file, which is already bounded
        # by summarization_threshold (agent summarizes automatically when that limit is hit).
        history_trimmed = execution_history

        prompt = PromptTemplate.from_template(prompt_template_str).format(
            objective=objective or "(not set)",
            history=history_trimmed,
            report=report_text
        )

        max_retries = 3
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    log_and_emit(socketio, global_state, f"{log_prefix} Retry {attempt}/{max_retries}...")
                    sleep(1)

                raw_response = llm.invoke(prompt)
                response_text = (raw_response.content if hasattr(raw_response, 'content') else str(raw_response)).strip()

                upper = response_text.upper()
                if upper.startswith("APPROVE"):
                    log_and_emit(socketio, global_state, f"{log_prefix} Result: APPROVE")
                    return True, "Report approved by validator."

                elif upper.startswith("REJECT"):
                    reason = response_text[len("REJECT"):].strip()
                    if reason.upper().startswith("REASON:"):
                        reason = reason[len("REASON:"):].strip()
                    log_and_emit(socketio, global_state, f"{log_prefix} Result: REJECT — {reason[:200]}")
                    return False, reason

                else:
                    if attempt < max_retries - 1:
                        log_and_emit(socketio, global_state, f"{log_prefix} Unclear response (attempt {attempt+1}): '{response_text[:100]}'. Retrying...")
                        continue
                    else:
                        log_and_emit(socketio, global_state, f"{log_prefix} All retries unclear — accepting report by default.")
                        return True, "Validator gave unclear response — report accepted by default."

            except Exception as invoke_err:
                if attempt < max_retries - 1:
                    log_and_emit(socketio, global_state, f"{log_prefix} Attempt {attempt+1} error: {invoke_err}. Retrying...")
                    sleep(2)
                else:
                    log_and_emit(socketio, global_state, f"{log_prefix} All retries failed — accepting report by default.")
                    return True, f"Validator error after {max_retries} attempts — report accepted by default."

    except Exception as e:
        log_and_emit(socketio, global_state, f"--- UNEXPECTED ERROR in report validator: {e} ---")
        traceback.print_exc()
        return True, "Unexpected error in report validator — report accepted by default."


def summarize_single_output(output_text, llm, provider, socketio, global_state, reason="", flood_limit=None):
    """
    Compresses a single large command output using the LLM.
    Uses configurable prompts from config.ini.

    Args:
        output_text: The command output to summarize
        llm: The LLM instance to use for summarization
        provider: The LLM provider (ollama, gemini, anthropic)
        socketio: SocketIO instance for real-time updates
        global_state: Global application state
        reason: The agent's reasoning for executing the command (helps contextualize the summary)
    """
    log_prefix = "--- Output Flood Protection ---"
    log_and_emit(socketio, global_state, f"{log_prefix} Output size {len(output_text)} exceeds limit. Summarizing...")
    socketio.emit('summarization_status', {'active': True, 'message': 'Summarizing large output...'})

    try:
        # 1. Get Prompt from Config
        cfg = get_config()
        prompt_key = 'OllamaStepSummaryPrompt' if provider == 'ollama' else 'CloudStepSummaryPrompt'
        prompt_template_str = cfg.get(prompt_key, 'template', fallback="Summarize output: {output}")

        # 2. Format Prompt
        # Cap input to protect LLM window
        # Include reason if available and the template supports it
        format_args = {'output': output_text[:20000]}
        if '{reason}' in prompt_template_str and reason:
            format_args['reason'] = reason
        elif '{reason}' in prompt_template_str:
            format_args['reason'] = "Not provided"

        prompt = PromptTemplate.from_template(prompt_template_str).format(**format_args)

        # 3. Call LLM
        summary = ""
        try:
            raw_summary = llm.invoke(prompt)
            # Both Gemini and Anthropic use .content attribute
            if provider in ['gemini', 'anthropic'] and hasattr(raw_summary, 'content'):
                summary = raw_summary.content
            else:
                summary = str(raw_summary)
        except Exception as e:
            print(f"Single output summarization failed: {e}")
            socketio.emit('summarization_status', {'active': False})
            # Fallback truncation
            return output_text[:1000] + "\n... [Output Truncated due to size] ...\n" + output_text[-1000:]

        socketio.emit('summarization_status', {'active': False})
        if summary:
            limit_display = f"{flood_limit:,}" if flood_limit else "N/A"
            header = f"[SYSTEM CONTEXT FLOOD PROTECTION]\n(Outputs over {limit_display} chars are summarized according to the provided REASON)\n"
            return f"{header}{summary.strip()}"
        else:
            return output_text[:2000] + "\n... [Output Truncated] ..."

    except Exception as e:
        traceback.print_exc()
        socketio.emit('summarization_status', {'active': False})
        return output_text[:1000] + "\n... [Output Truncated] ..."


def agent_task_runner(socketio, global_state, control_flags, event_objects, log_manager=None, task_id=None):
    """
    Thread-ul principal care ruleaza task-ul agentului.
    Primeste si modifica 'global_state' direct.
    Uses UnifiedLogManager for multi-log architecture.

    Args:
        task_id: Optional UUID for API task tracking. When provided, updates API_TASK_REGISTRY
                 with status, progress, and results for async polling.
    """
    global API_TASK_REGISTRY

    # Initialize log manager if not provided
    if log_manager is None:
        log_manager = UnifiedLogManager()

    # Initialize API task registry entry if this is an API task
    if task_id and task_id in API_TASK_REGISTRY:
        API_TASK_REGISTRY[task_id].update({
            'status': 'running',
            'current_step': 0,
            'latest_activity': 'Initializing agent...',
            'last_updated': time.time()
        })

    # --- Extragem datele din starea globala ---
    # Acestea sunt copii (pentru valori simple) sau referinte (pentru dict/list)
    current_objective = global_state['current_objective']
    # NOTA: current_execution_mode se citeste dinamic din global_state pentru a permite schimbarea in timpul pauzei
    current_summarization_mode = global_state['current_summarization_mode']
    current_allow_ask_mode = global_state['current_allow_ask_mode']
    
    # --- Extragem obiectele de control ---
    user_approval_event = event_objects['user_approval_event']
    user_response = event_objects['user_response']
    summarization_event = event_objects['summarization_event']
    user_answer_event = event_objects['user_answer_event']
    user_answer = event_objects['user_answer']
    
    # --- Functie helper locala pentru logare ---
    def log_agent(message, clear=False):
        """Helper pentru a loga in contextul acestui task."""
        log_and_emit(socketio, global_state, message, clear)

    try:
        # --- 1. Initializare LLM & Configurare ---
        log_agent(f"--- Agent task starting ---")
        log_agent(f"\n=== OBJECTIVE ===\n{current_objective}\n")

        # IMPROVEMENT: Detectam sudo capability si initializam system info
        detect_sudo_capability(socketio, global_state)

        # Detectam OS si user info
        try:
            # Try uname first (works on Unix/Linux/macOS)
            os_result = execute_ssh_command("uname -s 2>/dev/null || ver")
            user_result = execute_ssh_command("whoami")

            # DEBUG: Log what we actually received
            log_agent(f"[OS DETECTION DEBUG] os_result raw: '{os_result}'")
            log_agent(f"[OS DETECTION DEBUG] user_result raw: '{user_result}'")

            detected_os = "Unknown"
            if "Linux" in os_result:
                detected_os = "Linux"
            elif "Darwin" in os_result:
                detected_os = "macOS"
            elif "Windows" in os_result or "Microsoft" in os_result or "Version" in os_result:
                # Windows 'ver' command returns something like "Microsoft Windows [Version 10.0.xxxxx]"
                detected_os = "Windows (or non-Unix)"
            elif "Error" in os_result or not os_result.strip():
                # If uname fails and ver also fails, try another Windows detection
                log_agent("[OS DETECTION] First attempt failed, trying Windows-specific detection...")
                win_test = execute_ssh_command("echo %OS%")
                log_agent(f"[OS DETECTION DEBUG] win_test result: '{win_test}'")
                if "Windows" in win_test:
                    detected_os = "Windows (or non-Unix)"
                else:
                    detected_os = "Windows (or non-Unix)"  # Default to Windows if all else fails

            # Curatam user_result - luam ultima linie non-empty (pentru Windows care poate avea caractere ciudate)
            if "Error" not in user_result:
                user_lines = [line.strip() for line in user_result.split('\n') if line.strip()]
                raw_user = user_lines[-1] if user_lines else "unknown"

                # Windows whoami returns "HOSTNAME\username" - extract only username
                if '\\' in raw_user:
                    detected_user = raw_user.split('\\')[-1]  # Take part after backslash
                else:
                    detected_user = raw_user
            else:
                detected_user = "unknown"

            sudo_status = "not applicable (Windows)" if detected_os == "Windows (or non-Unix)" else \
                         ("available (passwordless)" if global_state.get('sudo_available', False) else "not available or requires password")

            # Get device type from config
            cfg_device = get_config()
            device_type = cfg_device.get('System', 'device_type', fallback='linux').strip().lower()

            # Actualizam system_os_info cu informatii complete
            if device_type in ['cisco', 'nxos', 'iosxr', 'iosxe', 'brocade', 'juniper', 'arista', 'other']:
                # Network device - include device type and block mode instructions
                system_context = f"Device: {device_type.upper()}, User: {detected_user}, BLOCK_MODE: ENABLED"
                global_state['device_type'] = device_type
                global_state['block_mode'] = True
            else:
                system_context = f"OS: {detected_os}, User: {detected_user}, Sudo: {sudo_status}"
                global_state['device_type'] = device_type
                global_state['block_mode'] = False

            global_state['system_os_info'] = system_context

            # IMPORTANT: Communicate detected OS to ssh_utils for proper PTY handling
            set_detected_os(detected_os)

            # Update log manager with system info string prep
            system_info_detailed = f"{detected_os}, user: {detected_user}, Sudo: {sudo_status}, IP: {global_state.get('system_ip', 'unknown')}, Device: {device_type}"

            # Log SSH connection at task start ONLY if system actually changed
            # This prevents ghost "SYSTEM CONTEXT CHANGED" entries when starting tasks on the same system
            current_username = global_state.get('system_username', 'unknown')
            current_ip = global_state.get('system_ip', 'unknown')
            current_system_name = global_state.get('system_name', '')

            # Get the last logged system to check if we need to log a change
            last_logged = global_state.get('last_logged_system', {})
            last_logged_username = last_logged.get('username', '')
            last_logged_ip = last_logged.get('ip', '')

            # Only log system context if it's different from last logged entry
            is_system_changed = (last_logged_ip != current_ip) or (last_logged_username != current_username)

            if is_system_changed:
                # Log the change with proper previous values
                log_manager.log_ssh_connection_change(current_username, current_ip, last_logged_username, last_logged_ip, current_system_name)
                # Update the tracking so we don't log the same system again
                global_state['last_logged_system'] = {
                    'username': current_username,
                    'ip': current_ip,
                    'name': current_system_name
                }
                if last_logged_ip and last_logged_username:
                    log_agent(f"System changed: {last_logged_username}@{last_logged_ip} -> {current_system_name or current_username}@{current_ip}")
                elif current_system_name:
                    log_agent(f"Task started on: {current_system_name} ({current_username}@{current_ip})")
                else:
                    log_agent(f"Task started on: {current_username}@{current_ip}")
            else:
                # System unchanged, just log task start without SYSTEM CONTEXT CHANGED entry
                if current_system_name:
                    log_agent(f"Task started on: {current_system_name} ({current_username}@{current_ip})")
                else:
                    log_agent(f"Task started on: {current_username}@{current_ip}")

            # --- NEW: Always Auto-Summarize Previous History on Task Start ---
            existing_context = log_manager.get_llm_context()

            # Read summarization threshold from config (needed for auto-summarization check)
            cfg_threshold = get_config()
            SUMMARIZATION_THRESHOLD = cfg_threshold.getint('Agent', 'summarization_threshold', fallback=15000)

            # Check if history exists and is larger than 70% of threshold (User Preference)
            # We ignore the default "No commands" message
            summarization_threshold_70 = int(SUMMARIZATION_THRESHOLD * 0.7)
            if existing_context and len(existing_context) > summarization_threshold_70 and "No commands have been executed yet" not in existing_context:
                log_agent(f"--- Starting New Task: History size {len(existing_context)} chars > {summarization_threshold_70} (70% of {SUMMARIZATION_THRESHOLD}). Forcing summarization... ---")

                # Sync global state so summarizer sees the text
                global_state['agent_history'] = existing_context

                # Run summarization with FORCE flag enabled
                # This ensures we switch to a clean summary format even if it slightly increases size
                summarize_history(socketio, global_state, force_summary=True)

                log_agent("--- Previous history summarized. Initializing new task context... ---")

            # Initialize the new task in log manager (Appends NEW TASK header)
            # Build current system string for context (e.g., "root@debian_ai (root@192.168.0.140)")
            if current_system_name:
                current_system_display = f"{current_system_name} ({current_username}@{current_ip})"
            else:
                current_system_display = f"{current_username}@{current_ip}"
            log_manager.log_new_task(current_objective, system_info_detailed, current_system_display)

            # --- MODIFICATION: Reconstruct memory from log manager ---
            # Instead of manually building the string, we ask the log manager
            # This ensures we are in sync with the disk log (SSOT)
            # It also picks up any previous history if we are continuing
            
            # Force a reload of the context from the file we just wrote to
            global_state['agent_history'] = log_manager.get_llm_context()
            
            log_agent(f"System detected: {detected_os}, User: {detected_user}, Sudo: {sudo_status}")

        except Exception as detect_err:
            log_agent(f"Warning: System detection failed: {detect_err}")
            global_state['system_os_info'] = "Unknown OS (detection failed)"

        cfg = get_config()
        PROVIDER = cfg.get('General', 'provider', fallback='')
        MODEL_NAME = cfg.get('Agent', 'model_name', fallback='')
        MAX_STEPS = cfg.getint('Agent', 'max_steps', fallback=50)
        #SUMMARIZATION_THRESHOLD = cfg.getint('Agent', 'summarization_threshold', fallback=15000)    >>>>> Corectie treshhold live

        # Initializam command_timeout in global_state pentru update live
        global_state['command_timeout'] = cfg.getint('Agent', 'command_timeout', fallback=120)

        if not PROVIDER or not MODEL_NAME:
            log_agent("--- ERROR: LLM Provider/Model missing in config.ini. Stopping. ---")
            return # Iesim din thread

        log_agent(f"Agent: {PROVIDER.capitalize()} ({MODEL_NAME})")
        log_agent(f"Max Steps: {MAX_STEPS}")
        log_agent(f"Command Timeout: {global_state['command_timeout']} seconds")
        #log_agent(f"Summarize Threshold: {SUMMARIZATION_THRESHOLD} chars")   >>> Se citeste in interiorul buclei dupa corectie

        # Initializam clientul LLM
        temperature = cfg.getfloat('Agent', 'temperature', fallback=0.5)
        llm = None
        if PROVIDER == 'ollama':
            api_url = cfg.get('Ollama', 'api_url', fallback='')
            if not api_url:
                raise ValueError("Ollama API URL missing in config.ini.")
            # Read configurable context window (default 32768 tokens)
            ollama_num_ctx = cfg.getint('Ollama', 'num_ctx', fallback=32768)
            keep_alive = cfg.get('Ollama', 'keep_alive', fallback='-1')
            llm_timeout = cfg.getint('Agent', 'llm_timeout', fallback=120)
            llm = Ollama(model=MODEL_NAME, base_url=api_url, timeout=llm_timeout, num_ctx=ollama_num_ctx, temperature=temperature, keep_alive=keep_alive)
            print(f"[LLM] Ollama execution agent: model={MODEL_NAME}, num_ctx={ollama_num_ctx}, temp={temperature}, timeout={llm_timeout}s, keep_alive={keep_alive}", flush=True)
        elif PROVIDER == 'gemini':
            api_key = cfg.get('General', 'gemini_api_key', fallback='')
            if not api_key:
                raise ValueError("Gemini API Key missing in config.ini.")
            llm = ChatGoogleGenerativeAI(model=MODEL_NAME, google_api_key=api_key,
                                         generation_config={"temperature": temperature})
        elif PROVIDER == 'anthropic':
            api_key = cfg.get('General', 'anthropic_api_key', fallback='')
            if not api_key:
                raise ValueError("Anthropic API Key missing in config.ini.")
            llm = ChatAnthropic(model=MODEL_NAME, api_key=api_key, temperature=temperature)
        else:
            raise ValueError(f"Unsupported LLM: {PROVIDER}")

        # --- Exec LLM Failback initialization ---
        fallback_exec_llm = None
        fallback_exec_provider = None
        if cfg.getboolean('ExecLLMFallback', 'enabled', fallback=False):
            fb_model = cfg.get('ExecLLMFallback', 'model_name', fallback='').strip()
            fb_provider = cfg.get('ExecLLMFallback', 'provider', fallback='ollama')
            if fb_model:
                try:
                    fb_temp = cfg.getfloat('Agent', 'temperature', fallback=0.5)
                    fb_timeout = cfg.getint('Agent', 'llm_timeout', fallback=120)
                    fb_num_ctx = cfg.getint('Ollama', 'num_ctx', fallback=32768)
                    fb_keep_alive = cfg.get('Ollama', 'keep_alive', fallback='-1')
                    if fb_provider == 'ollama':
                        fb_url = cfg.get('ExecLLMFallback', 'ollama_url', fallback='').strip() or cfg.get('Ollama', 'api_url', fallback='')
                        fallback_exec_llm = Ollama(model=fb_model, base_url=fb_url, timeout=fb_timeout, num_ctx=fb_num_ctx, temperature=fb_temp, keep_alive=fb_keep_alive)
                    elif fb_provider == 'gemini':
                        fb_key = cfg.get('ExecLLMFallback', 'api_key', fallback='').strip() or cfg.get('General', 'gemini_api_key', fallback='')
                        fallback_exec_llm = ChatGoogleGenerativeAI(model=fb_model, google_api_key=fb_key, generation_config={"temperature": fb_temp})
                    elif fb_provider == 'anthropic':
                        fb_key = cfg.get('ExecLLMFallback', 'api_key', fallback='').strip() or cfg.get('General', 'anthropic_api_key', fallback='')
                        fallback_exec_llm = ChatAnthropic(model=fb_model, api_key=fb_key, temperature=fb_temp)
                    fallback_exec_provider = fb_provider
                    log_agent(f"[FAILBACK] Exec LLM failback ready: {fb_provider}/{fb_model}")
                except Exception as fb_init_err:
                    log_agent(f"[FAILBACK] Warning: Could not init exec failback LLM: {fb_init_err}")

        global_state['_validator_consecutive_rejections'] = 0
        global_state['_using_exec_fallback'] = False
        global_state['_report_rejection_count'] = 0

        # --- 2. Bucla Principala a Agentului ---
        step_counter = 1
        while step_counter <= MAX_STEPS:
            # Reset per-step variables
            command_to_validate = None

            # --- A. Verificari de Control ---
            if not control_flags['is_running']():
                log_agent("\n--- Task stopped by user (loop check). ---")
                break # Iesim din bucla while

            # Check for human search pending (pause execution while human searches)
            while global_state.get('human_search_pending', False):
                if not control_flags['is_running']():
                    log_agent("\n--- Task stopped by user (human search pending check). ---")
                    break
                sleep(0.5) # Wait for human search to complete

            while control_flags['is_paused']():
                if not control_flags['is_running']():
                    log_agent("\n--- Task stopped by user (pause check). ---")
                    break # Iesim din bucla de pauza
                sleep(1) # Asteptam in pauza

            if not control_flags['is_running']():
                break # Iesim din bucla while daca s-a oprit in pauza

            # --- B. Pure Accumulation: Simple Threshold Check ---
            # Re-read threshold from config at each loop iteration
            cfg_loop = get_config()
            SUMMARIZATION_THRESHOLD = cfg_loop.getint('Agent', 'summarization_threshold', fallback=15000)

            # Use log_manager to get current context size
            current_context_size = log_manager.get_context_size()

            # If threshold exceeded, trigger summarization
            if SUMMARIZATION_THRESHOLD > 0 and current_context_size > SUMMARIZATION_THRESHOLD:
                if current_summarization_mode == 'automatic':
                    log_agent(f"\n--- Memory limit ({current_context_size}/{SUMMARIZATION_THRESHOLD} chars). Auto-summarizing... ---")
                    summarize_history(socketio, global_state)
                else:
                    # Assisted mode - request user approval
                    log_agent(f"\n--- Memory limit ({current_context_size}/{SUMMARIZATION_THRESHOLD} chars). Pausing for summarization approval. ---")
                    control_flags['set_paused'](True)
                    socketio.emit('task_paused')

                    # Reset event if already triggered
                    if summarization_event.is_set():
                        summarization_event.clear()

                    socketio.emit('request_history_summarization', {
                        'current_length': current_context_size,
                        'current_threshold': SUMMARIZATION_THRESHOLD
                    })

                    try:
                        summarization_event.wait()  # Wait for user response (summarize/continue)
                    except Exception as e:
                        log_agent(f"Summarization wait interrupted: {e}")

                    if not control_flags['is_running']():
                        break  # Stopped during summarization pause

                    # Resume (regardless of choice, state has been updated)
                    control_flags['set_paused'](False)
                    socketio.emit('task_resumed')
                    log_agent("--- Resuming after summarization choice. ---")

            # --- C. Pregatirea Prompt-ului ---
            try:
                # --- DYNAMIC PROMPT LOADING ---
                # Re-read config to pick up live edits from Prompt Editor
                cfg_prompt = get_config()

                # Select prompt section based on provider (Ollama vs Cloud providers)
                if PROVIDER == 'ollama':
                    section_base = 'OllamaPrompt'
                else:
                    # Both Gemini and Anthropic use 'CloudPrompt'
                    section_base = 'CloudPrompt'

                prompt_section_key = f"{section_base}WithAsk" if current_allow_ask_mode else section_base

                default_prompt_text = "Objective: {objective}\nHistory: {history}\nSystem: {system_info}\nProvide COMMAND:"
                prompt_template_str = cfg_prompt.get(prompt_section_key, 'template', fallback=default_prompt_text)

                prompt_template_obj = PromptTemplate.from_template(prompt_template_str)
                # ------------------------------

                # Extragem mereu cele mai noi date din starea globala
                # Inject command_timeout so the agent knows the limit
                # Get current system name/alias
                current_system_name = global_state.get('system_name', '')
                if not current_system_name:
                    # Fallback: try to get from config
                    try:
                        cfg = get_config()
                        current_system_name = cfg.get('System', 'system_name', fallback='')
                        if not current_system_name:
                            # Use user@ip as fallback
                            ip = cfg.get('System', 'ip_address', fallback='')
                            user = cfg.get('System', 'username', fallback='')
                            current_system_name = f"{user}@{ip}" if ip and user else "Unknown"
                    except:
                        current_system_name = "Unknown"

                # Get available systems list
                available_systems = get_configured_system_names()

                # Build system info with device-specific instructions
                base_system_info = global_state['system_os_info']
                device_type = global_state.get('device_type', 'linux')

                # Add Cisco/Network device specific instructions
                if device_type in ['cisco', 'nxos', 'iosxr', 'iosxe']:
                    cisco_instructions = """
*** CISCO CONFIGURATION RULE - CRITICAL ***
When you need to apply configuration (e.g., 'conf t', 'vlan', 'interface'), you MUST send ALL commands in a single multi-line block.
The system will execute them in an interactive session to maintain context.

CORRECT Example for creating a VLAN:
COMMAND:
configure terminal
vlan 10
name VLAN_TEST
exit
end
write memory

WRONG Example (DO NOT DO THIS):
COMMAND: configure terminal
[next step]
COMMAND: vlan 10
This will FAIL because 'configure terminal' context is lost between steps!

Always group related config commands together in one COMMAND block.
"""
                    base_system_info = f"{base_system_info}\n{cisco_instructions}"
                elif device_type in ['brocade']:
                    brocade_instructions = """
*** BROCADE FABRIC OS RULE ***
This is a Brocade Fibre Channel switch. Use Fabric OS commands.
For configuration changes, group related commands together.
Common commands: switchshow, cfgshow, zoneshow, portshow, etc.
"""
                    base_system_info = f"{base_system_info}\n{brocade_instructions}"
                elif device_type in ['juniper']:
                    juniper_instructions = """
*** JUNIPER JUNOS RULE ***
When configuring, enter configuration mode with 'configure' and commit changes with 'commit'.
Group related configuration commands in a single block.
"""
                    base_system_info = f"{base_system_info}\n{juniper_instructions}"
                elif device_type in ['arista']:
                    arista_instructions = """
*** ARISTA EOS RULE ***
Similar to Cisco IOS. Use 'configure terminal' to enter config mode.
Group related commands in a single block to maintain context.
"""
                    base_system_info = f"{base_system_info}\n{arista_instructions}"

                format_args = {
                    'objective': global_state['current_objective'], # Folosim obiectivul actualizat
                    'history': global_state['agent_history'],
                    'system_info': base_system_info,
                    'command_timeout': global_state.get('command_timeout', 300),
                    'current_system': current_system_name,
                    'system_list': available_systems,
                    'current_datetime': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }

                # Verificam daca prompt-ul are variabile necunoscute
                required_keys = re.findall(r'\{(\w+)\}', prompt_template_str)
                missing_keys = [k for k in required_keys if k not in format_args]
                if missing_keys:
                    raise KeyError(f"Prompt template missing keys: {missing_keys}")

                full_prompt = prompt_template_obj.format(**format_args)

                # --- PERIODIC FORMAT REMINDER ---
                # Inject a short format reminder every 5 steps starting at step 10
                # to prevent format drift (markdown, code blocks, invented labels) in long tasks.
                # Cost: ~20 tokens. Reactivates format attention in long contexts.
                if step_counter >= 10 and step_counter % 5 == 0:
                    full_prompt += "\n\n[SYSTEM REMINDER: Respond using the exact format. Start with REASON: on line 1. No markdown. No code blocks.]"
                    print(f"[FORMAT] Injected periodic format reminder at step {step_counter}", flush=True)

            except KeyError as fmt_err:
                log_agent(f"\n--- ERROR: Prompt format error (Missing Key: {fmt_err}). Check template. Stopping. ---")
                break
            except Exception as fmt_err:
                log_agent(f"\n--- ERROR: Prompt format error ({fmt_err}). Stopping. ---")
                traceback.print_exc()
                break

            # --- D. Apelarea LLM-ului (cu reincercari) ---
            retries = 0
            llm_response = ""
            action_found = False
            skipped_commands = []  # Commands beyond the first COMMAND: in LLM response

            # Progressive temperature bump per retry: +0.1 each attempt to unstick local LLMs
            base_temperature = getattr(llm, 'temperature', 0.5)
            if base_temperature is None:
                base_temperature = 0.5

            while retries < 5:
                if not control_flags['is_running']():
                    break # Iesim din bucla de reincercari

                # Bump temperature on retries to help unstick local LLMs
                if retries > 0:
                    bumped_temp = min(base_temperature + (retries * 0.1), 1.0)
                    try:
                        llm.temperature = bumped_temp
                        if hasattr(llm, 'generation_config') and isinstance(llm.generation_config, dict):
                            llm.generation_config['temperature'] = bumped_temp
                    except Exception:
                        pass
                    print(f"[EXEC] Retry {retries}/4: temperature bumped to {bumped_temp:.1f}", flush=True)

                # Doar logam incercarile esuate (nu prima incercare de succes)
                if retries == 0:
                    log_agent(f"\n--- STEP {step_counter}/{MAX_STEPS} ---")
                    # Step will be logged after we extract reason and command
                    # Update API task registry with current step
                    if task_id and task_id in API_TASK_REGISTRY:
                        API_TASK_REGISTRY[task_id].update({
                            'current_step': step_counter,
                            'latest_activity': 'Thinking...',
                            'last_updated': time.time()
                        })
                else:
                    log_agent(f"\n--- STEP {step_counter}/{MAX_STEPS} --- (Retry {retries}/4)")

                # Pornim indicatorul de thinking cu timer (citim timeout-ul din config)
                cfg_timeout = get_config()
                llm_timeout = cfg_timeout.getint('Agent', 'llm_timeout', fallback=120)
                thinking = ThinkingIndicator(socketio, timeout_seconds=llm_timeout)
                thinking.start()

                try:
                    # DEFINE STOP SEQUENCES (prevent output hallucination)
                    # Gemini limit: max 5 stop sequences
                    # NOTE: "Result:" removed — it could prematurely cut <<REQUEST_TASK>> tags
                    stop_sequences = ["Output:", "Observation:", "\nOutput", "\nObservation:"]

                    # Invoke LLM with stop sequences (with exec failback on error)
                    def _invoke_exec(l, p, s):
                        try:
                            return l.invoke(p, stop=s)
                        except TypeError:
                            return l.invoke(p)

                    try:
                        llm_response_obj = _invoke_exec(llm, full_prompt, stop_sequences)
                    except Exception as _exec_invoke_err:
                        if fallback_exec_llm and not global_state.get('_using_exec_fallback'):
                            log_agent(f"[FAILBACK] Exec LLM error: {_exec_invoke_err}. Switching to fallback...")
                            llm = fallback_exec_llm
                            PROVIDER = fallback_exec_provider
                            global_state['_using_exec_fallback'] = True
                            global_state['_validator_consecutive_rejections'] = 0
                            broadcast_emit(socketio, 'exec_failback_active', {'active': True, 'reason': 'llm_error'}, global_state)
                            llm_response_obj = _invoke_exec(llm, full_prompt, stop_sequences)
                        else:
                            raise

                    # Oprim indicatorul de thinking
                    thinking.stop()

                    # Extragem textul
                    if PROVIDER in ('gemini', 'anthropic') and hasattr(llm_response_obj, 'content'):
                        llm_response = llm_response_obj.content
                    else:
                        llm_response = str(llm_response_obj)

                    llm_response = llm_response.strip()

                    if not llm_response:
                        raise ValueError("Empty response from LLM.")

                    # --- THINKING EXTRACTION ---
                    # Strip thinking/reasoning markers from models that support it
                    # Pattern 1: "Thinking...\n..content..\n...done thinking.\n"
                    # Pattern 2: "<think>..content..</think>"
                    thinking_content = ""
                    think_match = re.search(r'(?:Thinking\.\.\.)\s*(.*?)\s*(?:\.\.\.done thinking\.?)', llm_response, re.DOTALL | re.IGNORECASE)
                    if not think_match:
                        think_match = re.search(r'<think>(.*?)</think>', llm_response, re.DOTALL | re.IGNORECASE)
                    if think_match:
                        thinking_content = think_match.group(1).strip()
                        # Remove the thinking block from the response
                        llm_response = llm_response[:think_match.start()] + llm_response[think_match.end():]
                        llm_response = llm_response.strip()
                        print(f"[THINKING] Extracted thinking ({len(thinking_content)} chars): {thinking_content[:200]}{'...' if len(thinking_content) > 200 else ''}", flush=True)
                        # Emit thinking to UI for debugging visibility
                        socketio.emit('llm_thinking', {'step': step_counter, 'content': thinking_content})

                    if not llm_response:
                        raise ValueError("Empty response from LLM (only thinking content, no action).")

                    # --- POST-PROCESSING: Strip multi-step hallucination ---
                    # Some LLMs generate multiple actions separated by "--- next step ---"
                    # We only process the first action per response
                    next_step_match = re.search(r'\n\s*---\s*next\s*step\s*---', llm_response, re.IGNORECASE)
                    if next_step_match:
                        print(f"[PARSER] Stripped '--- next step ---' multi-action hallucination ({len(llm_response) - next_step_match.start()} chars removed)", flush=True)
                        llm_response = llm_response[:next_step_match.start()].strip()

                    # Salvam raspunsul brut (without thinking markers)
                    global_state['last_session']['raw_llm_responses'].append(llm_response)
                    raw_responses_formatted = "\n\n".join([f"--- Response {i+1} ---\n{r}" for i, r in enumerate(global_state['last_session']['raw_llm_responses'])])
                    socketio.emit('update_raw_llm_responses', {'data': raw_responses_formatted})

                    # --- PREPROCESSING: Normalize block command format ---
                    # Convert COMMAND (BLOCK): with --- BLOCK START/END --- to simple COMMAND: format
                    # This ensures the standard regex can parse it
                    normalized_response = llm_response

                    # Check if LLM used block format
                    if 'COMMAND (BLOCK):' in normalized_response or 'COMMAND(BLOCK):' in normalized_response:
                        print("[PARSER] Detected COMMAND (BLOCK) format, normalizing...", flush=True)
                        # Replace COMMAND (BLOCK): with COMMAND:
                        normalized_response = re.sub(r'COMMAND\s*\(BLOCK\)\s*:', 'COMMAND:', normalized_response, flags=re.IGNORECASE)
                        # Remove --- BLOCK START --- and --- BLOCK END --- markers
                        normalized_response = re.sub(r'---\s*BLOCK\s*START\s*---\s*\n?', '', normalized_response, flags=re.IGNORECASE)
                        normalized_response = re.sub(r'\n?\s*---\s*BLOCK\s*END\s*---', '', normalized_response, flags=re.IGNORECASE)
                        print(f"[PARSER] Normalized response preview: {normalized_response[:200]}...", flush=True)

                    # Cautam actiuni (use normalized response)
                    report_match = re.search(r"REPORT:\s*(.*)", normalized_response, re.DOTALL | re.IGNORECASE)
                    ask_match = None
                    if current_allow_ask_mode:
                        ask_match = re.search(r"ASK:\s*(.*)", normalized_response, re.DOTALL | re.IGNORECASE)
                    srch_match = re.search(r"SRCH:\s*(.*)", normalized_response, re.DOTALL | re.IGNORECASE)

                    # Standard format: COMMAND: followed by content.
                    # Stop at: blank line (\n\n), separator line (---), or a known keyword.
                    # This prevents capturing a second REASON:/COMMAND: block when an LLM
                    # generates multiple commands in one response — only the first is used.
                    command_match = re.search(
                        r'COMMAND:\s*(.*?)(?=\n\n|\n-{3,}|\n(?:REASON|TIMEOUT|REPORT|ASK|SRCH|WRITE_FILE|END_CONTENT)|$)',
                        normalized_response,
                        re.DOTALL | re.IGNORECASE
                    )
                    # IMPROVED: WRITE_FILE regex - try strict (with END_CONTENT) first, then tolerant (to end)
                    # Captures: (1) file path, (2) content strictly up to END_CONTENT
                    write_match = re.search(
                        r'WRITE_FILE:\s*(.*?)\n.*?CONTENT:\s*\n(.*?)END_CONTENT',
                        normalized_response,
                        re.DOTALL | re.IGNORECASE
                    )
                    if not write_match:
                        # Tolerant fallback: no END_CONTENT, capture to end of response
                        write_match = re.search(
                            r'WRITE_FILE:\s*(.*?)\n.*?CONTENT:\s*\n(.*)',
                            normalized_response,
                            re.DOTALL | re.IGNORECASE
                        )
                        if write_match:
                            print("[PARSER] WRITE_FILE matched without END_CONTENT (tolerant fallback)", flush=True)

                    # Detect additional COMMAND: lines beyond the first (multi-command hallucination).
                    # We extract all COMMAND: blocks and store the extras so the LLM can be
                    # notified after the first command executes (Option 3 — system note injection).
                    if command_match:
                        all_cmd_iter = re.finditer(
                            r'COMMAND:\s*(.*?)(?=\nCOMMAND:|\n\n|\n-{3,}|\n(?:REASON|TIMEOUT|REPORT|ASK|SRCH|WRITE_FILE|END_CONTENT)|$)',
                            normalized_response,
                            re.DOTALL | re.IGNORECASE
                        )
                        all_cmds = [m.group(1).strip() for m in all_cmd_iter if m.group(1).strip()]
                        # First entry is the one already captured by command_match; rest are skipped
                        skipped_commands = [c for c in all_cmds[1:] if c and not c.upper().startswith('WRITE_FILE')]
                        if skipped_commands:
                            print(f"[PARSER] Multiple COMMAND: detected — {len(skipped_commands)} extra command(s) will be deferred to next steps.", flush=True)

                    # PRIORITY FIX: If command_match captured WRITE_FILE content, invalidate it
                    # so write_match takes precedence
                    if command_match and write_match:
                        cmd_text = command_match.group(1).strip()
                        if cmd_text.upper().startswith('WRITE_FILE'):
                            print("[PARSER] command_match contained WRITE_FILE content, deferring to write_match", flush=True)
                            command_match = None
                    timeout_match = re.search(r"TIMEOUT:\s*(\d+)", normalized_response, re.IGNORECASE)

                    # Updated check (timeout alone is not an action, but we parse it here)
                    if report_match or ask_match or srch_match or command_match or write_match:
                        action_found = True

                        # --- PROCESS TIMEOUT ADJUSTMENT (Ephemeral) ---

                        # 1. Get the User-Defined Limit from Config (This is the Ceiling & Default)
                        cfg_t = get_config()
                        user_limit = cfg_t.getint('Agent', 'command_timeout', fallback=120)

                        # Initialize step_timeout with the user default
                        step_timeout = user_limit

                        if timeout_match:
                            try:
                                requested_timeout = int(timeout_match.group(1))

                                # 2. Compare Requested vs User Limit
                                if requested_timeout > user_limit:
                                    # Case A: Request exceeds limit -> Clamp to User Limit
                                    step_timeout = user_limit
                                    log_agent(f"--- Step Timeout: Agent requested {requested_timeout}s, clamped to User Limit ({user_limit}s). ---")
                                else:
                                    # Case B: Request is within limit -> Use Agent's preference for this step only
                                    step_timeout = max(10, requested_timeout)
                                    log_agent(f"--- Step Timeout: Set to {step_timeout}s by Agent (valid for this step only). ---")

                            except ValueError:
                                log_agent("--- Warning: Invalid TIMEOUT format from Agent. Using default. ---")

                        # NOTE: We do NOT update global_state['command_timeout'] or emit to UI.
                        # This ensures the UI setting remains the persistent master value.
                        # ----------------------------------

                        break # Am gasit o actiune valida, iesim din reincercari

                    raise ValueError("Invalid format (No COMMAND, REPORT, ASK, SRCH, or WRITE_FILE).")
                    
                except Exception as e:
                    # Oprim indicatorul de thinking in caz de eroare
                    thinking.stop()
                    retries += 1
                    error_log = f"Attempt {retries}/5 failed: {type(e).__name__} - {e}"
                    log_agent(error_log)

                    # --- CUDA OOM / Resource Error Detection ---
                    # These errors won't self-resolve with retries, so break immediately
                    error_str = str(e).lower()
                    if any(keyword in error_str for keyword in ['cuda', 'out of memory', 'oom', 'gpu memory', 'vram', 'alloc']):
                        oom_msg = f"--- GPU MEMORY ERROR: The LLM ran out of CUDA/GPU memory. Lower the 'Ollama Context Window (num_ctx)' in Agent & LLM settings and try again. Current error: {e} ---"
                        log_agent(oom_msg)
                        global_state['last_session']['final_report'] = oom_msg
                        socketio.emit('final_report', {'data': oom_msg})
                        break  # Don't retry OOM errors

                    # --- SMART RETRY: Handle Empty Responses ---
                    # If the LLM returned empty, we apply progressive pressure.
                    if "Empty response" in str(e):
                        if retries < 3:
                            # Attempts 1-2: Soft Nudge (Warning)
                            log_agent("--- Injecting system nudge to force response... ---")
                            full_prompt += "\n\nSYSTEM ERROR: You returned an empty response. You MUST provide a valid COMMAND or REPORT now."
                        else:
                            # Attempts 3-5: Hard Nudge (Force Feed)
                            # We write the first line of the response FOR the agent.
                            # This forces the LLM to complete the pattern instead of starting from scratch.
                            log_agent("--- Nudge escalation: Forcing 'Format 1: Action' preamble... ---")
                            full_prompt += "\n\nSYSTEM: Action required immediately.\nFormat 1: Action"

                    traceback.print_exc()
                    sleep(retries * 2) # Asteptare exponentiala

            # Restore original temperature after retries
            try:
                llm.temperature = base_temperature
                if hasattr(llm, 'generation_config') and isinstance(llm.generation_config, dict):
                    llm.generation_config['temperature'] = base_temperature
            except Exception:
                pass

            if not control_flags['is_running']():
                break # Oprit in timpul apelului LLM

            if not action_found:
                final_error_msg = f"--- ERROR: Failed LLM action after {retries} attempts. Stopping. ---"
                log_agent(final_error_msg)
                global_state['last_session']['final_report'] = final_error_msg
                socketio.emit('final_report', {'data': final_error_msg})
                break # Oprim task-ul

            # --- E. Procesarea Actiunii LLM ---
            
            command_to_execute = None
            original_command_from_llm = None  # Explicit init — avoids UnboundLocalError if no branch sets it
            command_to_validate = None        # Explicit init — avoids locals() check fragility
            vm_screen_display = None  # Reset per-step: human-readable display for WRITE_FILE
            reason_text = ""
            
            # Extragem motivul (comun pentru toate actiunile)
            reason_match = re.search(r"REASON:\s*(.*?)(?:COMMAND:|REPORT:|ASK:|SRCH:|WRITE_FILE:|$)", llm_response, re.DOTALL | re.IGNORECASE)
            if reason_match:
                reason_text = reason_match.group(1).strip()

            # Update API task registry with reasoning/activity
            if task_id and task_id in API_TASK_REGISTRY:
                # Determine action type for activity description
                action_type = "Processing"
                if report_match:
                    action_type = "Completing task"
                elif command_match:
                    action_type = "Executing command"
                elif srch_match:
                    action_type = "Searching history"
                elif write_match:
                    action_type = "Writing file"
                elif ask_match:
                    action_type = "Asking user"

                # Truncate reason for activity display (max 200 chars)
                activity_text = reason_text[:200] + "..." if len(reason_text) > 200 else reason_text
                API_TASK_REGISTRY[task_id].update({
                    'latest_activity': f"[{action_type}] {activity_text}" if activity_text else action_type,
                    'last_updated': time.time()
                })

            # --- CAZUL 1: REPORT (Task finalizat) ---
            if report_match:
                final_report_text = report_match.group(1).strip()

                # --- Report Validator ---
                max_report_rejections = 3
                report_rejection_count = global_state.get('_report_rejection_count', 0)
                if report_rejection_count < max_report_rejections:
                    exec_history = log_manager.get_llm_context() if hasattr(log_manager, 'get_llm_context') else global_state.get('agent_history', '')
                    report_approved, report_reason = validate_report_with_llm(
                        socketio, global_state,
                        final_report_text,
                        global_state.get('current_objective', ''),
                        exec_history
                    )
                    if not report_approved:
                        global_state['_report_rejection_count'] = report_rejection_count + 1
                        rejection_msg = (
                            f"\n\n[SYSTEM EVENT] REPORT REJECTED BY VALIDATOR\n"
                            f"Reason: {report_reason}\n"
                            f"Instruction: Your report contains claims about actions or results that do not appear "
                            f"in the execution history. Do NOT resubmit the same report. First identify what is "
                            f"missing, execute the necessary commands to verify the actual state, then write a new "
                            f"REPORT: based only on what you have actually observed in this session.\n"
                        )
                        log_agent(f"--- Report Validator: REJECTED (attempt {global_state['_report_rejection_count']}/{max_report_rejections}) ---\nReason: {report_reason}")
                        log_manager.append_to_llm_context(rejection_msg)
                        global_state['agent_history'] = log_manager.get_llm_context()
                        step_counter += 1
                        continue
                else:
                    log_agent(f"--- Report Validator: Max rejections ({max_report_rejections}) reached — accepting report. ---")

                report_log_message = f"--- REPORT ---\nREASON: {reason_text}\nREPORT: {final_report_text}"
                log_agent(report_log_message)

                # Log task completion to log_manager
                log_manager.log_task_completed(final_report_text)

                global_state['last_session']['final_report'] = final_report_text
                socketio.emit('final_report', {'data': final_report_text})

                # Update API task registry with completion
                if task_id and task_id in API_TASK_REGISTRY:
                    API_TASK_REGISTRY[task_id].update({
                        'status': 'completed',
                        'result': final_report_text,
                        'latest_activity': 'Task completed successfully',
                        'last_updated': time.time()
                    })

                # CRITICAL: Update LLM context file
                history_entry = f"\n\n{get_timestamped_step_header(step_counter)}\n\n{report_log_message}\n"
                log_manager.append_to_llm_context(history_entry)

                # Sync global state from file to ensure consistency
                global_state['agent_history'] = log_manager.get_llm_context()
                socketio.emit('update_history', {'data': global_state['agent_history']})

                log_agent("\n--- Task completed (REPORT received). ---")
                break # Task terminat

            # --- CAZUL 2: ASK (Agentul intreaba) ---
            elif ask_match and current_allow_ask_mode:
                question = ask_match.group(1).strip()
                ask_log_message = f"--- AGENT ASKING ---\nREASON: {reason_text}\nASK: {question}"
                log_agent(ask_log_message)

                # Log question to log_manager
                log_manager.log_ask_question(question, reason_text)

                user_answer.clear()

                # Resetam event-ul daca a fost deja triggered
                if user_answer_event.is_set():
                    user_answer_event.clear()

                # Mark pending so reconnecting clients can see the ASK modal
                global_state['_pending_ask_data'] = {
                    'question': question,
                    'reason': reason_text,
                    'objective': global_state['current_objective']
                }

                # Trimitem intrebarea si obiectivul curent catre UI
                socketio.emit('awaiting_user_answer', global_state['_pending_ask_data'])

                # Small sleep to ensure emit is flushed before blocking on wait()
                socketio.sleep(0.1)

                try:
                    answered = user_answer_event.wait(timeout=3600) # Asteptam 1 ora
                    if not answered:
                        log_agent("\n--- USER ANSWER TIMEOUT (1h). Stopping. ---")
                        global_state['_pending_ask_data'] = None
                        break # Oprim task-ul
                except Exception as e:
                    log_agent(f"User answer event interrupted: {e}")
                    global_state['_pending_ask_data'] = None
                    # Verificam daca a fost oprit
                    if not control_flags['is_running']():
                        break

                # Clear pending flag
                global_state['_pending_ask_data'] = None

                # Event-ul se reseteaza automat dupa wait()

                if not control_flags['is_running']():
                    break # Oprit in timpul asteptarii raspunsului

                user_answer_text = user_answer.get('answer', 'No answer provided.')
                new_objective_from_user = user_answer.get('objective', global_state['current_objective'])
                objective_updated = False

                # Log answer to log_manager
                log_manager.log_ask_answer(user_answer_text)

                # Verificam daca utilizatorul a modificat obiectivul
                if new_objective_from_user and new_objective_from_user.strip() != global_state['current_objective']:
                    old_obj = global_state['current_objective']
                    global_state['current_objective'] = new_objective_from_user.strip()
                    objective_updated = True
                    log_msg = f"\n--- Objective updated by user (during ASK) ---\nNew: {global_state['current_objective']}\n"
                    log_agent(log_msg)
                
                # Salvam interactiunea in istoric
                history_entry = f"\n\n{get_timestamped_step_header(step_counter)}\n\n{ask_log_message}\n\nOutput:\nHuman Response: {user_answer_text}\n"
                if objective_updated:
                    history_entry += f"\nIntervention: Objective updated.\nOld: {old_obj}\nNew: {global_state['current_objective']}\n"

                # CRITICAL: Update LLM context file
                log_manager.append_to_llm_context(history_entry)

                # Sync global state from file to ensure consistency
                global_state['agent_history'] = log_manager.get_llm_context()
                socketio.emit('update_history', {'data': global_state['agent_history']})
                step_counter += 1
                continue # Trecem la pasul urmator

            # --- CAZUL 3: SRCH (Agent searches base log) ---
            elif srch_match:
                search_query = srch_match.group(1).strip().split('\n')[0].strip()
                srch_log_message = f"--- AGENT SEARCHING ---\nREASON: {reason_text}\nSRCH: {search_query}"
                log_agent(srch_log_message)

                # Use unified search function from app.py
                # Import here to avoid circular dependency
                import app

                # Start thinking indicator for search/summarization
                thinking = ThinkingIndicator(socketio, timeout_seconds=60)
                thinking.start()

                try:
                    # Pass the agent's reasoning to the search context
                    search_result = app.perform_unified_search(search_query, reason=reason_text, summarize=True)
                    search_context = search_result['results_summarized']
                    was_summarized = search_result['was_summarized']
                    size = search_result['size']

                    thinking.stop()

                    if was_summarized:
                        log_agent(f"Search results ({size} chars) summarized to {len(search_context)} chars.")
                    else:
                        log_agent(f"Search results: {size} chars (no summarization needed).")

                except Exception as e:
                    thinking.stop()
                    log_agent(f"Search failed: {e}. Using empty results.")
                    search_context = f"Search error: {str(e)}"

                # Add search results to agent history
                history_entry = f"\n\n{get_timestamped_step_header(step_counter)}\n\n{srch_log_message}\n\nSearch Results:\n{search_context}\n"

                # CRITICAL: Update LLM context file
                log_manager.append_to_llm_context(history_entry)

                # Sync global state from file to ensure consistency
                global_state['agent_history'] = log_manager.get_llm_context()
                socketio.emit('update_history', {'data': global_state['agent_history']})

                log_agent(f"Search results added to context. Continuing...")
                step_counter += 1
                continue # Continue to next step with enriched context

            # --- CAZUL 4: WRITE_FILE (Scriere sigura prin Base64) ---
            elif write_match:
                target_path = write_match.group(1).strip()
                raw_content = write_match.group(2)

                # --- Enhanced Cleaning Logic ---
                # Remove Markdown code blocks (start and end)
                # Handles ```bash, ```sh, ```python, or just ```
                clean_content = re.sub(r"^```[a-zA-Z0-9]*\n", "", raw_content.strip())
                if clean_content.endswith("```"):
                    clean_content = clean_content[:-3]

                # Strip any remaining leading/trailing whitespace
                file_content = clean_content.strip()

                # Safety check: Ensure we don't have empty content
                if not file_content:
                    log_agent("--- WARNING: WRITE_FILE content is empty after cleaning. Skipping write. ---")
                    continue

                # --- CRITICAL: Log the clear content to Full Log ---
                # This enables the agent to SRCH for code/configs it wrote earlier.
                log_manager.log_file_content(target_path, file_content)
                # ---------------------------------------------------

                log_agent(f"--- Preparing WRITE_FILE operation for {target_path} ---")

                # 1. Encode content to Base64
                import base64
                b64_content = base64.b64encode(file_content.encode('utf-8')).decode('utf-8')

                # 2. Construct the safe command based on OS
                is_windows = "Windows" in global_state.get('system_os_info', '') or ":\\" in target_path or target_path.lower().startswith("c:")

                if is_windows:
                    # Windows Logic: Use PowerShell to decode and write bytes
                    # Note: We use single quotes for PowerShell strings and double quotes for the cmd wrapper if needed.
                    # The command writes binary directly to avoid encoding issues (CRLF vs LF).

                    # Ensure path uses backslashes for Windows compatibility if needed,
                    # though PowerShell handles forward slashes usually.
                    win_path = target_path.replace('/', '\\')

                    command_to_execute = f"powershell -NoProfile -NonInteractive -Command \"[System.IO.File]::WriteAllBytes('{win_path}', [System.Convert]::FromBase64String('{b64_content}'))\""

                else:
                    # Linux/Unix Logic
                    needs_sudo = target_path.startswith(('/etc', '/var', '/usr', '/root', '/boot', '/opt'))

                    if needs_sudo and global_state.get('sudo_available', False):
                        # Use sudo tee for system paths
                        command_to_execute = f"echo '{b64_content}' | base64 -d | sudo tee {target_path} > /dev/null"
                    else:
                        # Standard user write
                        command_to_execute = f"echo '{b64_content}' | base64 -d > {target_path}"

                # 3. Set display metadata for logs
                original_command_from_llm = f"[WRITE_FILE Action] Writing {len(file_content)} bytes to {target_path}"

                # CRITICAL FIX: Append to existing reason instead of overwriting it.
                # We want to keep the LLM's original explanation (e.g., "Fixing bug in script").
                if reason_text:
                    reason_text += f"\n(Technical: Writing to {target_path} via safe Base64 injection)"
                else:
                    reason_text = f"Writing file {target_path} via safe Base64 injection."

                # 4. PREPARE FOR VALIDATION
                # Instead of sending the Base64 blob, we send the readable content so the LLM can judge safety.
                # We truncate extremely large files for validation to save tokens.
                preview_content = file_content[:2000] + ("\n... [content truncated for validation] ..." if len(file_content) > 2000 else "")
                command_to_validate = f"WRITE_FILE operation.\nTarget: {target_path}\nContent Preview:\n{preview_content}"

                # 5. PREPARE HUMAN-READABLE DISPLAY for VM Screen (Live)
                # Instead of showing the raw base64 command, show the actual file content
                vm_screen_display = f"cat << 'WRITE_FILE_EOF' > {target_path}\n{file_content}\nWRITE_FILE_EOF"

                # Log step start with reason and readable command description
                log_manager.log_step_start(step_counter, reason_text, original_command_from_llm)

                # IMPORTANT: We do NOT continue/break here.
                # We let the flow fall through to Step F (Validation) and Step G (Execution).
                # This ensures standard logging of Success/Exit Code 1.

            # --- CAZUL 5: COMMAND (Executie SSH) ---
            elif command_match:
                raw_cmd = command_match.group(1).strip()

                # --- HALLUCINATION SANITIZATION FILTER ---
                # If LLM generated "Output:" or similar markers, cut everything after
                # Note: Using case-insensitive matching via lower() comparison
                hallucination_markers = ["Output:", "Result:", "Observation:"]
                for marker in hallucination_markers:
                    # Case-insensitive check
                    if marker.lower() in raw_cmd.lower():
                        # Find the position and split
                        marker_pos = raw_cmd.lower().find(marker.lower())
                        raw_cmd = raw_cmd[:marker_pos].strip()
                        print(f"[SANITIZATION] Filtered hallucinated output marker: {marker}")

                # If command has multiple lines and contains output marker on subsequent lines
                # Take only the first valid line
                command_lines = raw_cmd.split('\n')
                if len(command_lines) > 1:
                    # Check if any line after the first contains output markers
                    for i, line in enumerate(command_lines):
                        for marker in hallucination_markers:
                            # Case-insensitive check
                            if marker.lower() in line.lower() and i > 0:  # Not the first line
                                # Truncate to lines before the marker
                                command_lines = command_lines[:i]
                                raw_cmd = '\n'.join(command_lines).strip()
                                print(f"[SANITIZATION] Filtered hallucinated output on line {i+1}")
                                break

                # Edge case: If command is empty after filtering (ex: COMMAND: Output:...)
                if not raw_cmd:
                    print("[SANITIZATION] Empty command after filtering hallucination. Injecting error message.")
                    raw_cmd = "echo 'Error: LLM hallucinated output without command'"
                # --- END HALLUCINATION SANITIZATION ---

                # DEFENSIVE SANITIZATION: Line-by-line check to catch edge cases
                # Stop at first line that starts with a reserved keyword
                sanitized_lines = []
                for line in raw_cmd.split('\n'):
                    if re.match(r'^(REASON|REPORT|ASK|SRCH|TIMEOUT|WRITE_FILE):', line.strip(), re.IGNORECASE):
                        break  # Stop processing at keyword
                    sanitized_lines.append(line)
                raw_cmd = '\n'.join(sanitized_lines).strip()

                # Apply cleaning to remove Markdown/Quotes artifacts
                command_to_execute = clean_command_string(raw_cmd)

                if not command_to_execute:
                    log_agent(f"--- WARNING: LLM provided an empty COMMAND. Retrying step. ---")

                    # CRITICAL: Update LLM context file
                    history_entry = f"\n\n{get_timestamped_step_header(step_counter)}\n\nREASON: {reason_text}\n\nCOMMAND: (empty)\n\nOutput:\nInvalid empty command. Retrying."
                    log_manager.append_to_llm_context(history_entry)

                    continue # Trecem la urmatoarea iteratie a buclei 'while' fara a incrementa step_counter

                # Log if cleaning happened (for debugging transparency)
                if raw_cmd != command_to_execute:
                    print(f"Cleaned command: '{raw_cmd}' -> '{command_to_execute}'")

                # Log step start with reason and proposed command to log_manager
                log_manager.log_step_start(step_counter, reason_text, command_to_execute)

                # Standard command: original is same as executed
                original_command_from_llm = command_to_execute
            
            else:
                # Nu ar trebui sa ajungem aici, dar ca masura de siguranta
                log_agent(f"--- ERROR: No valid action found despite action_found=True. Response: {llm_response}")
                continue # Incercam pasul din nou

            # --- F. Validare & Executie Comanda ---

            approved_for_execution = False
            human_intervention_log_entry = None
            # Use command_to_execute as fallback if no branch set original_command_from_llm
            if original_command_from_llm is None:
                original_command_from_llm = command_to_execute

            # Citim modul de executie dinamic (poate fi schimbat in timpul pauzei)
            current_mode = global_state['current_execution_mode']
            log_agent(f"\n--- Validation Mode: {current_mode.upper()} ---")

            if current_mode == 'assisted':
                # Show readable command for approval
                display_cmd = original_command_from_llm if "WRITE_FILE" in original_command_from_llm else command_to_execute
                log_agent(f"\n--- Waiting for command approval ---\nREASON: {reason_text}\nCOMMAND: {display_cmd}")
                log_agent("--- EXECUTION PAUSED ---")

                # Curatam raspunsul anterior
                user_response.clear()

                # Resetam event-ul daca a fost deja triggered (pentru a evita skip-ul wait-ului)
                if user_approval_event.is_set():
                    user_approval_event.clear()

                # Mark pending so reconnecting clients can see the approval modal
                global_state['_pending_approval_data'] = {'command': display_cmd, 'reason': reason_text}

                # Emitem cererea de aprobare (show readable command in UI)
                socketio.emit('awaiting_command_approval', global_state['_pending_approval_data'])

                # Small sleep to ensure emit is flushed before blocking on wait()
                socketio.sleep(0.1)

                # Asteptam aprobarea utilizatorului
                try:
                    approved = user_approval_event.wait(timeout=3600) # Asteptam 1 ora
                    if not approved:
                        log_agent("\n--- APPROVAL TIMEOUT (1h). Stopping. ---")
                        global_state['_pending_approval_data'] = None
                        break
                except Exception as e:
                    log_agent(f"Approval event interrupted: {e}")
                    global_state['_pending_approval_data'] = None
                    if not control_flags['is_running']():
                        break

                # Clear pending flag
                global_state['_pending_approval_data'] = None

                # Verificam din nou daca task-ul mai ruleaza
                if not control_flags['is_running']():
                    break # Oprit in timpul aprobarii

                # Procesam raspunsul utilizatorului
                if user_response.get('approved'):
                    approved_for_execution = True

                    # For WRITE_FILE: the UI shows a human-readable description, not the actual base64 command.
                    # We must NOT overwrite command_to_execute with the display text.
                    if "[WRITE_FILE Action]" not in original_command_from_llm:
                        command_to_execute = user_response.get('command', command_to_execute).strip()

                    if original_command_from_llm != command_to_execute:
                        modification_reason = user_response.get('modification_reason', 'no reason provided')
                        log_msg = f"--- Command modified & approved by user ---\nReason: {modification_reason}"
                        log_agent(log_msg)
                        human_intervention_log_entry = f"Intervention: Command modified by user. Reason: {modification_reason}.\nOriginal: {original_command_from_llm}\nNew: {command_to_execute}"
                        log_manager.log_intervention("Command Modified", f"Original: {original_command_from_llm} -> New: {command_to_execute} (Reason: {modification_reason})")
                    else:
                        log_agent("--- Command approved by user ---")
                        human_intervention_log_entry = "Intervention: Command approved by user."

                    # Log validation result - approved by user in assisted mode
                    log_manager.log_validator_result(True, 'assisted')
                else:
                    rejection_reason = user_response.get('reason', 'No reason provided')
                    log_agent(f"--- Command rejected by user ---\nReason: {rejection_reason}")
                    log_manager.log_validator_result(False, 'assisted', rejection_reason)
                    log_manager.log_step_end()

                    # CRITICAL: Update LLM context file
                    history_entry = f"\n\n{get_timestamped_step_header(step_counter)}\n\nREASON: {reason_text}\n\nCOMMAND: {original_command_from_llm}\n\nOutput:\nIntervention: Rejected by user. Reason: {rejection_reason}\n"
                    log_manager.append_to_llm_context(history_entry)

                    # Sync global state from file to ensure consistency
                    global_state['agent_history'] = log_manager.get_llm_context()
                    socketio.emit('update_history', {'data': global_state['agent_history']})
                    step_counter += 1
                    continue # Trecem la pasul urmator

            elif current_mode == 'independent':
                # --- Validation Logic ---

                # WRITE_FILE operations are pre-validated by the parser — they are not shell commands
                # and must not be sent to the shell command validator (which would always reject them).
                if "[WRITE_FILE Action]" in original_command_from_llm:
                    log_agent("--- WRITE_FILE operation: bypassing shell command validator ---")
                    approved_for_execution = True

                # Check if Validator is Enabled globally
                elif global_state.get('validator_enabled', True):
                    validation_input = command_to_validate if command_to_validate else command_to_execute
                    system_name = global_state.get('system_name', '')

                    # --- Whitelist check: skip LLM if command was previously approved ---
                    if _whitelist_check(system_name, validation_input):
                        log_agent("--- Command whitelisted: Auto-approved (no LLM call) ---")
                        log_manager.log_validator_result(True, 'independent', 'Whitelisted command — auto-approved')
                        approved_for_execution = True

                    else:
                        # --- Validare Automata cu LLM ---
                        is_valid, validation_reason = validate_command_with_llm(socketio, global_state, validation_input, reason_text)

                        # Log validation result to log_manager
                        log_manager.log_validator_result(is_valid, 'independent', validation_reason)

                        if is_valid:
                            log_agent("--- Command Auto-Validated. Proceeding... ---")
                            # Auto-add to per-system whitelist for future fast approval
                            _whitelist_add(system_name, validation_input)
                            approved_for_execution = True
                            # Reset consecutive rejection counter on successful validation
                            global_state['_validator_consecutive_rejections'] = 0
                        else:
                            log_agent(f"--- Command Auto-Rejected by Validator ---")
                            log_agent(f"Reason: {validation_reason}")
                            log_manager.log_step_end()

                            # Increment consecutive rejection counter + check failback threshold
                            global_state['_validator_consecutive_rejections'] = global_state.get('_validator_consecutive_rejections', 0) + 1
                            consec = global_state['_validator_consecutive_rejections']
                            if consec >= 3 and fallback_exec_llm and not global_state.get('_using_exec_fallback'):
                                log_agent(f"[FAILBACK] Exec LLM switched to fallback after {consec} consecutive validator rejections")
                                llm = fallback_exec_llm
                                PROVIDER = fallback_exec_provider
                                global_state['_using_exec_fallback'] = True
                                global_state['_validator_consecutive_rejections'] = 0
                                broadcast_emit(socketio, 'exec_failback_active', {'active': True, 'reason': 'validator_rejections', 'count': consec}, global_state)

                            # Update LLM context file
                            history_entry = f"\n\n{get_timestamped_step_header(step_counter)}\n\nREASON: {reason_text}\n\nCOMMAND: {original_command_from_llm}\n\nOutput:\nIntervention: Command auto-rejected by validator. Reason: {validation_reason}\n"
                            log_manager.append_to_llm_context(history_entry)

                            # Sync global state
                            global_state['agent_history'] = log_manager.get_llm_context()
                            socketio.emit('update_history', {'data': global_state['agent_history']})
                            step_counter += 1
                            continue
                else:
                    # --- Validator DISABLED: Auto-Approve ---
                    log_agent("--- Validator DISABLED by user. Auto-approving command... ---")
                    approved_for_execution = True
            
            # --- G. Executie SSH (daca a fost aprobat) ---
            if approved_for_execution and control_flags['is_running']():
                log_agent("--- EXECUTION RESUMED - Command Approved ---")

                # --- FIX: PAGER BLOCKING (systemctl, service, journalctl, man, psql) ---
                pager_commands = ['systemctl status', 'systemctl', 'service', 'journalctl', 'man', 'psql', 'sudo -u postgres psql']
                command_to_check = command_to_execute.strip()

                needs_pager_fix = False
                for cmd in pager_commands:
                    # Check if command starts with the pager command (with or without sudo)
                    if command_to_check.startswith(cmd) or command_to_check.startswith(f"sudo {cmd}"):
                        needs_pager_fix = True
                        break

                if needs_pager_fix:
                    # Add both SYSTEMD_PAGER and PAGER for maximum compatibility
                    command_to_execute = f"SYSTEMD_PAGER=cat PAGER=cat {command_to_execute}"
                    log_agent(f"Note: Pager disabled for command to prevent blocking.")
                # --- END PAGER FIX ---

                # Show readable command if available, else full command
                display_cmd = original_command_from_llm if "WRITE_FILE" in original_command_from_llm else command_to_execute

                # Format multi-line commands clearly in the live log
                if '\n' in display_cmd:
                    # Multi-line block command - format with clear markers
                    log_agent(f"\nExecuting Block Command:")
                    log_agent("--- COMMAND BLOCK ---")
                    for line in display_cmd.split('\n'):
                        if line.strip():
                            log_agent(f"  {line}")
                    log_agent("--- END BLOCK ---")
                else:
                    log_agent(f"\nExecuting Command: {display_cmd}")

                # Emitem catre ecranul VM (broadcast for live view)
                # For WRITE_FILE: show human-readable content instead of base64 command
                vm_display_cmd = vm_screen_display if vm_screen_display else command_to_execute
                vm_prompt = f"\n{global_state['system_username']}@{global_state['system_ip']}~# "
                if '\n' in vm_display_cmd:
                    # Multi-line block - show each command on its own line with continuation prompt
                    vm_output = vm_prompt
                    cmd_lines = vm_display_cmd.split('\n')
                    for i, line in enumerate(cmd_lines):
                        if line.strip():
                            if i == 0:
                                vm_output += line + '\n'
                            else:
                                vm_output += f"  > {line}\n"  # Continuation prompt for block commands
                    broadcast_emit(socketio, 'vm_screen', {'data': vm_output}, global_state)
                    global_state['persistent_vm_output'] += vm_output
                else:
                    broadcast_emit(socketio, 'vm_screen', {'data': vm_prompt + vm_display_cmd + '\n'}, global_state)
                    global_state['persistent_vm_output'] += vm_prompt + vm_display_cmd + '\n'

                # IMPROVEMENT: Executam comanda cu timeout si retry
                # Use the step-specific timeout calculated in parsing phase
                # If step_timeout wasn't set (e.g. direct command match without parsing block), fallback to config
                final_timeout = step_timeout if 'step_timeout' in locals() else global_state.get('command_timeout', 300)

                # Pornim timer-ul de executie
                # Note: We pass the specific timeout to the timer so the UI countdown is correct for this step
                exec_timer = CommandExecutionTimer(socketio, global_state, command=command_to_execute, specific_timeout=final_timeout)
                exec_timer.start()

                success, result, attempt_num, streamed = execute_ssh_command_with_timeout(
                    socketio, global_state, command_to_execute, final_timeout, max_retries=3
                )

                # Oprim timer-ul de executie
                exec_timer.stop()
                socketio.emit('command_exec_done')

                if not success:
                    log_agent(f"     --- Command failed after {attempt_num} attempt(s). ---")
                else:
                    log_agent(" .         --- Command Completed ---")

                # 1. Log result to Full Log (Disk)
                # For WRITE_FILE: log human-readable heredoc instead of base64 command
                log_command = vm_screen_display if vm_screen_display else command_to_execute
                log_manager.log_command_execution(log_command, result, success)

                # 2. Process result for LLM Context (RAM)
                context_result = result

                # --- WRITE_FILE context: summarize file content for LLM memory ---
                # Note: base64 redirect produces no stdout on success. ssh_utils returns the
                # placeholder "Success: Command executed with no output." for empty output —
                # that string must NOT be treated as an error.
                _SSH_NO_OUTPUT = "Success: Command executed with no output."
                if "[WRITE_FILE Action]" in original_command_from_llm and success:
                    # Treat result as error only if it's non-empty AND not the no-output placeholder
                    _real_error = result.strip() and result.strip() != _SSH_NO_OUTPUT
                    if _real_error:
                        context_result = f"WRITE_FILE failed for {target_path}. Error: {result.strip()}"
                    else:
                        # Empty result = success (base64 redirect produces no stdout)
                        wf_content = vm_screen_display.split('\n', 1)[1].rsplit('\n', 1)[0] if vm_screen_display else ""
                        if len(wf_content) <= 500:
                            context_result = f"Success: File written to {target_path} ({len(file_content)} bytes).\nContent:\n{wf_content}"
                        else:
                            try:
                                summary_prompt = f"Summarize in 1-2 sentences what this script/file does:\n{wf_content[:2000]}"
                                wf_summary = llm.invoke(summary_prompt).content.strip()
                                context_result = f"Success: File written to {target_path} ({len(file_content)} bytes).\nSummary: {wf_summary}"
                            except Exception:
                                context_result = f"Success: File written to {target_path} ({len(file_content)} bytes). Content available in full history."

                # --- Flood Protection ---
                else:
                    # Only check for flood if we didn't already swap the message
                    cfg_flood = get_config()
                    SUMMARIZATION_THRESHOLD = cfg_flood.getint('Agent', 'summarization_threshold', fallback=15000)

                    # Threshold check: If output > 30% of total budget, summarize it
                    # (e.g. 4500 chars for a 15000 limit)
                    flood_limit = int(SUMMARIZATION_THRESHOLD * 0.3) if SUMMARIZATION_THRESHOLD > 0 else 4000

                    if len(result) > flood_limit:
                        context_result = summarize_single_output(result, llm, PROVIDER, socketio, global_state, reason=reason_text, flood_limit=flood_limit)

                # Log step end
                log_manager.log_step_end()

                if not control_flags['is_running']():
                    log_agent("--- Stop signal received during SSH execution. ---")
                    break  # Oprit in timpul executiei SSH

                # Emit result to VM Screen.
                # If streaming was active, chunks were already shown live — don't duplicate.
                # For failed commands, always emit the error status line so the user sees it.
                if not streamed:
                    broadcast_emit(socketio, 'vm_screen', {'data': result + '\n'}, global_state)
                elif not success:
                    # Show the error summary line (e.g. "Error (Exit Code 1):") even after streaming
                    status_line = result.split('\n', 1)[0]
                    broadcast_emit(socketio, 'vm_screen', {'data': '\n' + status_line + '\n'}, global_state)
                global_state['persistent_vm_output'] += result + '\n'

                # Actualizam istoricul agentului cu rezultatul PROCESAT
                # Keep format simple so LLM doesn't get confused
                # For multi-line commands, just use COMMAND: followed by the lines
                history_entry = f"\n\n{get_timestamped_step_header(step_counter)}\n\nREASON: {reason_text}\n\nCOMMAND:\n{original_command_from_llm}\n\n"
                if human_intervention_log_entry:
                    history_entry += f"Output:\n{human_intervention_log_entry}\n\n"

                # Use the potentially summarized result for the Agent's memory
                history_entry += f"Output:\n{context_result}\n"

                # CRITICAL: Update LLM context file
                log_manager.append_to_llm_context(history_entry)

                # If the LLM sent multiple COMMAND: lines, inject a system note so it knows
                # the extra commands were NOT executed and must be issued one per step.
                if skipped_commands:
                    skipped_list = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(skipped_commands))
                    skipped_note = (
                        f"\n[SYSTEM NOTE] IMPORTANT: Your previous response contained {len(skipped_commands) + 1} COMMAND: lines "
                        f"but only ONE command can be executed per step. The first command was executed above. "
                        f"You MUST issue each remaining command separately in the following steps — do not skip them or assume their outcome:\n"
                        f"{skipped_list}\n"
                    )
                    log_manager.append_to_llm_context(skipped_note)
                    log_agent(f"--- NOTE: {len(skipped_commands)} skipped COMMAND(s) injected into LLM context for next steps. ---")
                    skipped_commands = []  # Reset after injection

                # Sync global state from file to ensure consistency
                global_state['agent_history'] = log_manager.get_llm_context()
                socketio.emit('update_history', {'data': global_state['agent_history']})

                # Verificam daca sistemul de operare a fost identificat
                if "Unknown. The first step" in global_state['system_os_info']:
                    if "Linux" in result or "Ubuntu" in result or "CentOS" in result or "Debian" in result:
                         global_state['system_os_info'] = result
                         log_agent(f"--- System OS info updated ---")
                    elif "Windows" in result:
                         global_state['system_os_info'] = result
                         log_agent(f"--- System OS info updated ---")

                step_counter += 1
                log_agent("\n" + ("=" * 60) + "\n")  # Separator vizual intre pasi cu spatii
                sleep(1) # Mica pauza intre pasi

            elif not control_flags['is_running']():
                break # Oprit inainte de executie
            
            else:
                # Cazul in care nu a fost aprobat (nu ar trebui sa ajungem aici
                # decat daca logica de aprobare esueaza)
                log_agent(f"--- ERROR: Command '{command_to_execute}' was not approved for execution. Skipping. ---")
                step_counter += 1

        # --- 3. Finalizarea Buclei ---
        if not control_flags['is_running']():
            log_agent("\n--- Task stopped by user. ---")
            global_state['last_session']['final_report'] = "Task stopped by user."
            socketio.emit('final_report', {'data': "Task stopped by user."})
            # Update API task registry
            if task_id and task_id in API_TASK_REGISTRY:
                API_TASK_REGISTRY[task_id].update({
                    'status': 'stopped',
                    'result': 'Task stopped by user.',
                    'latest_activity': 'Stopped by user',
                    'last_updated': time.time()
                })

        elif step_counter > MAX_STEPS:
            final_msg = f"\n--- Max steps ({MAX_STEPS}) reached. Stopping. ---"
            log_agent(final_msg)
            global_state['last_session']['final_report'] = f"Stopped after {MAX_STEPS} steps."
            socketio.emit('final_report', {'data': f"Stopped after {MAX_STEPS} steps."})
            # Update API task registry
            if task_id and task_id in API_TASK_REGISTRY:
                API_TASK_REGISTRY[task_id].update({
                    'status': 'completed',
                    'result': f"Stopped after {MAX_STEPS} steps.",
                    'latest_activity': 'Max steps reached',
                    'last_updated': time.time()
                })

    except Exception as e:
        # Check if this is a forced stop (socket closed, etc)
        if not global_state.get('task_running', False):
            log_agent("\n--- Task stopped immediately by user. ---")
            global_state['last_session']['final_report'] = "Task stopped by user."
            socketio.emit('final_report', {'data': "Task stopped by user."})
            # Update API task registry
            if task_id and task_id in API_TASK_REGISTRY:
                API_TASK_REGISTRY[task_id].update({
                    'status': 'stopped',
                    'result': 'Task stopped by user.',
                    'latest_activity': 'Stopped by user',
                    'last_updated': time.time()
                })
        else:
            # Genuine error
            error_message = f"\n--- AGENT RUNNER FATAL ERROR ---\n{type(e).__name__}: {e}\n{traceback.format_exc()}\n--- TASK STOPPED ---"
            log_agent(error_message)
            global_state['last_session']['final_report'] = f"Task failed with error: {e}"
            # Update API task registry
            if task_id and task_id in API_TASK_REGISTRY:
                API_TASK_REGISTRY[task_id].update({
                    'status': 'failed',
                    'result': f"Task failed with error: {e}",
                    'latest_activity': f'Error: {type(e).__name__}',
                    'last_updated': time.time()
                })
            socketio.emit('final_report', {'data': f"Task failed with error: {e}"})
        
    finally:
        # Asiguram ca starea este setata pe oprit, indiferent cum s-a iesit
        log_agent("--- Agent task thread finishing. ---")

        # Store log_manager reference in global_state for app.py to access
        global_state['log_manager'] = log_manager

        control_flags['set_running'](False)
        control_flags['set_paused'](False)
        # Emitem un semnal final catre UI (wrapper-ul va emite inca unul ca garantie)
        socketio.emit('task_finished')


# --- NEW: Chat Message Processing ---
def process_chat_message(socketio, global_state, user_message, is_system=False):
    """
    Processes a chat message. Handles:
    1. Normal responses.
    2. Task proposals (<<REQUEST_TASK>>).
    3. Search requests (SRCH:) with auto-recursion (thought loop).

    Args:
        is_system: If True, the message is an internal system trigger (e.g. task completed)
                   and will not be logged to chat history or shown in the UI.
    """
    msg_type = "system" if is_system else "user"

    # Acquire lock to prevent concurrent chat LLM calls from racing
    lock_acquired = _chat_processing_lock.acquire(timeout=120)
    if not lock_acquired:
        print(f"[CHAT] ✗ Could not acquire chat lock after 120s ({msg_type} message). Skipping.", flush=True)
        if not is_system:
            socketio.emit('chat_response', {'role': 'assistant', 'content': 'Chat is busy processing another request. Please try again in a moment.'})
            socketio.emit('chat_status', {'status': 'idle'})
        return

    try:
        cfg = get_config()
        provider = cfg.get('General', 'provider', fallback='')
        model_name = cfg.get('Agent', 'model_name', fallback='')

        # 0. Save User Message (only once at the start) — skip for system triggers.
        # API messages are already saved with role='api' by app.py before the background
        # task starts — skip here to avoid overwriting with role='user'.
        log_manager = global_state.get('log_manager')
        if log_manager and not is_system and not global_state.get('_api_message_logged'):
            log_manager.log_chat_message('user', user_message)
        global_state.pop('_api_message_logged', None)  # consume the flag

        # 1. Select LLM (Use separate chat LLM if configured, otherwise use execution LLM)
        llm = global_state.get('chat_llm')

        if llm is None:
            # Fallback to execution LLM if separate chat LLM not configured
            temperature = cfg.getfloat('Agent', 'temperature', fallback=0.5)
            fallback_timeout = cfg.getint('Agent', 'llm_timeout', fallback=120)
            print(f"[CHAT] Chat LLM not configured separately. Using execution LLM for chat (timeout={fallback_timeout}s).", flush=True)
            if provider == 'ollama':
                api_url = cfg.get('Ollama', 'api_url', fallback='')
                print(f"[CHAT] Creating Ollama execution LLM: model={model_name}, url={api_url}", flush=True)
                ollama_num_ctx = cfg.getint('Ollama', 'num_ctx', fallback=32768)
                keep_alive = cfg.get('Ollama', 'keep_alive', fallback='-1')
                llm = Ollama(model=model_name, base_url=api_url, timeout=fallback_timeout, num_ctx=ollama_num_ctx, temperature=temperature, keep_alive=keep_alive)
            elif provider == 'gemini':
                api_key = cfg.get('General', 'gemini_api_key', fallback='')
                print(f"[CHAT] Creating Gemini execution LLM: model={model_name}", flush=True)
                llm = ChatGoogleGenerativeAI(model=model_name, google_api_key=api_key, generation_config={"temperature": temperature}, timeout=fallback_timeout, request_timeout=fallback_timeout)
            elif provider == 'anthropic':
                api_key = cfg.get('General', 'anthropic_api_key', fallback='')
                print(f"[CHAT] Creating Anthropic execution LLM: model={model_name}", flush=True)
                llm = ChatAnthropic(model=model_name, api_key=api_key, temperature=temperature, timeout=fallback_timeout, default_request_timeout=fallback_timeout)
        else:
            # Get the model name from the Chat LLM config
            chat_cfg = get_config()
            chat_provider = chat_cfg.get('ChatLLM', 'provider', fallback='ollama')
            chat_model = chat_cfg.get('ChatLLM', 'model_name', fallback='unknown')
            print(f"[CHAT] ✓ Using separate Chat LLM: provider={chat_provider}, model={chat_model}", flush=True)

        if not llm:
            print("[CHAT] ✗ Error: LLM not configured.", flush=True)
            socketio.emit('chat_response', {'role': 'assistant', 'content': 'Error: LLM not configured.'})
            return

        # --- RECURSION LOOP (Max 15 turns for Search/WebSearch) ---
        # We loop to allow the agent to: User -> Agent(SRCH/WEB_SEARCH/KNOWLEDGE/SWITCH) -> System(Results) -> Agent(Answer)
        max_turns = 15
        current_turn = 0
        original_user_message = user_message  # Preserve for re-prompts
        completed_switch_target = None  # Prevent re-switch loop on same target

        while current_turn < max_turns:
            current_turn += 1

            # Cancel check — user recalled the message
            if _chat_cancel_event.is_set():
                print(f"[CHAT] Cancelled by user before turn {current_turn}", flush=True)
                socketio.emit('chat_cancelled', {'message': original_user_message})
                if log_manager and not is_system:
                    log_manager.remove_last_chat_message()
                socketio.emit('chat_status', {'status': 'idle'})
                return

            # 2. Prepare Prompt
            # We re-read config here in case prompts changed, but usually static per request
            prompt_template_str = cfg.get('ChatPrompt', 'template', fallback="Context: {history}\nUser: {user_message}")
            prompt = PromptTemplate.from_template(prompt_template_str)

            # Get context (it updates every loop iteration if we added search results)
            if log_manager:
                history_context = log_manager.get_llm_context()
            else:
                history_context = global_state.get('agent_history', '')

            # Get Action Plan Status (separate variable)
            action_plan_status_text = "No active plan."
            if log_manager:
                plan_status = log_manager.get_action_plan_status()
                if plan_status:
                    action_plan_status_text = plan_status

            # Safety truncation
            if len(history_context) > 40000:
                 history_context = "...[Truncated old history]...\n" + history_context[-40000:]

            # Load recent chat history (configurable count, excluding current user message)
            chat_history_text = ""
            if log_manager:
                # Read message count from config (default 20)
                chat_msg_count = int(cfg.get('Agent', 'chat_history_message_count', fallback='20'))

                chat_messages = log_manager.get_chat_history()
                # Get last N messages (excluding the current user message which was just added)
                recent_messages = chat_messages[-(chat_msg_count+1):-1] if len(chat_messages) > 1 else []

                if recent_messages:
                    formatted_messages = []
                    for msg in recent_messages:
                        role_label = "You" if msg.get('role') == 'user' else "Agent"
                        content = msg.get('content', '')
                        # Include timestamp in DD.MM.YYYY HH:MM:SS format
                        timestamp_str = ""
                        if msg.get('timestamp'):
                            try:
                                ts = datetime.fromisoformat(msg['timestamp'])
                                timestamp_str = f"[{ts.strftime('%d.%m.%Y %H:%M:%S')}] "
                            except:
                                timestamp_str = ""
                        formatted_messages.append(f"{timestamp_str}{role_label}: {content}")

                    chat_history_text = "\n".join(formatted_messages)
                else:
                    chat_history_text = "(No previous chat messages)"
            else:
                chat_history_text = "(Chat history not available)"

            # Get available system names for injection into prompt
            available_systems_list = get_configured_system_names()

            # Get current system name/alias
            current_system_name = global_state.get('system_name', '')
            if not current_system_name:
                # Fallback: try to get from config
                try:
                    cfg = get_config()
                    current_system_name = cfg.get('System', 'system_name', fallback='')
                    if not current_system_name:
                        # Use user@ip as fallback
                        ip = cfg.get('System', 'ip_address', fallback='')
                        user = cfg.get('System', 'username', fallback='')
                        current_system_name = f"{user}@{ip}" if ip and user else "Unknown"
                except:
                    current_system_name = "Unknown"

            # Format Prompt (Include action_plan_status, system_list, and current_system as variables)
            # Tag system messages so LLM can distinguish them from user messages
            effective_message = user_message
            if is_system or current_turn > 1:
                # Re-prompt (turn > 1) or system trigger — mark clearly for LLM
                if not user_message.startswith('[SYSTEM'):
                    effective_message = f"[SYSTEM EVENT] {user_message}"

            # Current date/time for LLM awareness
            current_datetime = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Use safe formatting to handle missing placeholders and avoid KeyError on special chars (like LaTeX)
            def safe_format(template_obj, **kwargs):
                import re
                # Extract string if it's a LangChain PromptTemplate
                template_str = getattr(template_obj, 'template', str(template_obj))
                # Find all {var} patterns
                matches = re.findall(r'\{([a-zA-Z0-9_]+)\}', template_str)
                for match in matches:
                    if match in kwargs:
                        # Replace only known variables
                        template_str = template_str.replace('{' + match + '}', str(kwargs[match]))
                return template_str

            full_prompt = safe_format(
                prompt,
                objective=global_state.get('current_objective', 'None'),
                action_plan_status=action_plan_status_text,
                system_info=global_state.get('system_os_info', 'Unknown'),
                history=history_context,
                chat_history=chat_history_text,
                user_message=effective_message,
                system_list=available_systems_list,
                current_system=current_system_name,
                current_datetime=current_datetime
            )

            # Auto-append knowledge injection if enabled and documents exist
            km = global_state.get('knowledge_manager')
            if km and km.has_documents():
                documents_text = km.get_direct_inject_context()
                if documents_text:
                    try:
                        knowledge_template = cfg.get('KnowledgePrompt', 'template', fallback='')
                        if knowledge_template and '{documents}' in knowledge_template:
                            knowledge_injection = knowledge_template.format(documents=documents_text)
                            full_prompt = full_prompt + "\n\n" + knowledge_injection
                            print(f"[KNOWLEDGE] Injected {len(documents_text)} chars of knowledge into chat prompt ({km.get_document_count()} documents)", flush=True)
                    except Exception as k_err:
                        print(f"[KNOWLEDGE] Chat injection error: {k_err}", flush=True)

            # Auto-append web search injection if enabled
            ws_enabled = cfg.getboolean('WebSearch', 'enabled', fallback=False)
            if ws_enabled:
                try:
                    ws_injection_template = cfg.get('WebSearchInjection', 'template', fallback='')
                    if ws_injection_template:
                        full_prompt = full_prompt + "\n\n" + ws_injection_template
                        print(f"[WEB_SEARCH] Injected web search instructions into chat prompt ({len(ws_injection_template)} chars)", flush=True)
                except Exception as ws_inj_err:
                    print(f"[WEB_SEARCH] Chat injection error: {ws_inj_err}", flush=True)

            # 3. Invoke LLM (with empty response retry)
            socketio.emit('chat_status', {'status': 'thinking'})

            # DEFINE STOP SEQUENCES
            # Tell the LLM to stop if it starts writing Output or Observation
            # Gemini limit: max 5 stop sequences
            # NOTE: "Result:" removed — it could prematurely cut <<REQUEST_TASK>> tags
            stop_sequences = ["Output:", "Observation:", "\nOutput", "\nObservation:"]

            # Read LLM timeout from config (shared with execution agent)
            llm_timeout = int(cfg.get('Agent', 'llm_timeout', fallback='120'))
            print(f"[CHAT] Invoking LLM ({msg_type} message, prompt={len(full_prompt)} chars, timeout={llm_timeout}s)...", flush=True)

            response_text = ""
            max_retries = 5
            # Progressive temperature bump per retry: +0.1 each attempt to unstick local LLMs
            base_temperature = getattr(llm, 'temperature', 0.5)
            if base_temperature is None:
                base_temperature = 0.5

            for retry_attempt in range(max_retries):
                try:
                    # Bump temperature on retries to help unstick local LLMs
                    if retry_attempt > 0:
                        bumped_temp = min(base_temperature + (retry_attempt * 0.1), 1.0)
                        try:
                            llm.temperature = bumped_temp
                            # For Gemini, temperature is inside generation_config
                            if hasattr(llm, 'generation_config') and isinstance(llm.generation_config, dict):
                                llm.generation_config['temperature'] = bumped_temp
                        except Exception:
                            pass  # Some LLM wrappers may not allow attribute setting
                        print(f"[CHAT] Retry {retry_attempt + 1}/{max_retries}: temperature bumped to {bumped_temp:.1f}", flush=True)

                    # Use ThreadPoolExecutor to enforce timeout on LLM invoke
                    def _invoke_with_stop(l, p, s):
                        try:
                            return l.invoke(p, stop=s)
                        except TypeError:
                            return l.invoke(p)

                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(_invoke_with_stop, llm, full_prompt, stop_sequences)

                        try:
                            response_obj = future.result(timeout=llm_timeout)
                        except concurrent.futures.TimeoutError:
                            print(f"[CHAT] ✗ LLM invoke timed out after {llm_timeout}s (attempt {retry_attempt + 1}/{max_retries})", flush=True)
                            raise TimeoutError(f"LLM did not respond within {llm_timeout} seconds")

                    if hasattr(response_obj, 'content'):
                        response_text = response_obj.content
                    else:
                        response_text = str(response_obj)

                    print(f"[CHAT] ✓ LLM responded ({len(response_text)} chars)", flush=True)
                    # Log raw response for debugging (truncated to 3000 chars)
                    print(f"[CHAT-RAW] {response_text[:3000]}", flush=True)
                    if len(response_text) > 3000:
                        print(f"[CHAT-RAW] ... (truncated, total {len(response_text)} chars)", flush=True)

                    # Check for empty response
                    if response_text and response_text.strip():
                        break  # Got valid response
                    else:
                        print(f"[CHAT] Empty response from LLM (attempt {retry_attempt + 1}/{max_retries})", flush=True)
                        if retry_attempt < max_retries - 1:
                            full_prompt += "\n\nSYSTEM: You returned an empty response. Please provide a helpful answer to the user's message."
                            socketio.emit('chat_status', {'status': f'retrying ({retry_attempt + 2}/{max_retries})...'})
                        else:
                            response_text = "I'm sorry, I was unable to generate a response. Please try rephrasing your message."
                            print(f"[CHAT] All {max_retries} attempts returned empty. Sending fallback message.", flush=True)

                except TimeoutError as te:
                    # Try chat failback on last attempt timeout
                    chat_fallback = global_state.get('_chat_llm_fallback')
                    if retry_attempt == max_retries - 1 and chat_fallback and not global_state.get('_using_chat_fallback'):
                        print(f"[CHAT] Timeout — switching to Chat LLM failback...", flush=True)
                        llm = chat_fallback
                        global_state['_using_chat_fallback'] = True
                        base_temperature = getattr(llm, 'temperature', 0.5) or 0.5
                        broadcast_emit(socketio, 'chat_failback_active', {'active': True, 'reason': 'timeout'}, global_state)
                        socketio.emit('chat_status', {'status': 'retrying with failback...'})
                    elif retry_attempt < max_retries - 1:
                        socketio.emit('chat_status', {'status': f'retrying ({retry_attempt + 2}/{max_retries})...'})
                    else:
                        response_text = "The AI model took too long to respond. Please try again."
                        print(f"[CHAT] All {max_retries} attempts timed out.", flush=True)

                except Exception as invoke_err:
                    print(f"[CHAT] LLM invoke error (attempt {retry_attempt + 1}/{max_retries}): {invoke_err}", flush=True)
                    # Check for CUDA OOM - don't retry, inform user immediately
                    err_str = str(invoke_err).lower()
                    if any(kw in err_str for kw in ['cuda', 'out of memory', 'oom', 'gpu memory', 'vram', 'alloc']):
                        response_text = "GPU Memory Error: The LLM ran out of CUDA/GPU memory. Please lower the 'Ollama Context Window (num_ctx)' value in Agent & LLM settings and try again."
                        print(f"[CHAT] CUDA OOM detected. Stopping retries.", flush=True)
                        break
                    # Try chat failback on last attempt error
                    chat_fallback = global_state.get('_chat_llm_fallback')
                    if retry_attempt == max_retries - 1 and chat_fallback and not global_state.get('_using_chat_fallback'):
                        print(f"[CHAT] Error on last attempt — switching to Chat LLM failback...", flush=True)
                        llm = chat_fallback
                        global_state['_using_chat_fallback'] = True
                        base_temperature = getattr(llm, 'temperature', 0.5) or 0.5
                        broadcast_emit(socketio, 'chat_failback_active', {'active': True, 'reason': 'llm_error'}, global_state)
                        socketio.emit('chat_status', {'status': 'retrying with failback...'})
                    elif retry_attempt < max_retries - 1:
                        socketio.emit('chat_status', {'status': f'retrying ({retry_attempt + 2}/{max_retries})...'})
                    else:
                        response_text = f"I encountered an error while processing your message. Please try again."
                        print(f"[CHAT] All {max_retries} attempts failed. Sending error message.", flush=True)

            # Cancel check — user recalled the message while LLM was responding
            if _chat_cancel_event.is_set():
                print(f"[CHAT] Cancelled by user after LLM responded (turn {current_turn})", flush=True)
                socketio.emit('chat_cancelled', {'message': original_user_message})
                if log_manager and not is_system:
                    log_manager.remove_last_chat_message()
                socketio.emit('chat_status', {'status': 'idle'})
                return

            # Restore original temperature after retries
            try:
                llm.temperature = base_temperature
                if hasattr(llm, 'generation_config') and isinstance(llm.generation_config, dict):
                    llm.generation_config['temperature'] = base_temperature
            except Exception:
                pass

            # --- THINKING EXTRACTION (Chat) ---
            # Strip thinking/reasoning markers before processing response
            think_match = re.search(r'(?:Thinking\.\.\.)\s*(.*?)\s*(?:\.\.\.done thinking\.?)', response_text, re.DOTALL | re.IGNORECASE)
            if not think_match:
                think_match = re.search(r'<think>(.*?)</think>', response_text, re.DOTALL | re.IGNORECASE)
            if think_match:
                chat_thinking = think_match.group(1).strip()
                response_text = response_text[:think_match.start()] + response_text[think_match.end():]
                response_text = response_text.strip()
                print(f"[CHAT THINKING] Extracted ({len(chat_thinking)} chars): {chat_thinking[:200]}{'...' if len(chat_thinking) > 200 else ''}", flush=True)

            # Strip fenced code blocks before checking for action tags.
            # The LLM often includes SRCH:/KNOWLEDGE:/WEB_SEARCH: etc. as *examples*
            # inside ``` blocks. Without stripping, the regex would fire on those examples
            # and trigger real actions (search loop, infinite re-invocations).
            # We keep the original response_text for display; tag detection uses the stripped copy.
            _stripped_for_tags = re.sub(r'```.*?```', '', response_text, flags=re.DOTALL)
            # Also strip inline code spans (single backtick), e.g. `SRCH: foo`
            _stripped_for_tags = re.sub(r'`[^`\n]+`', '', _stripped_for_tags)

            # 4. Check for SRCH:
            srch_match = re.search(r'SRCH:\s*(.*)', _stripped_for_tags, re.IGNORECASE)

            if srch_match:
                search_query = srch_match.group(1).strip()

                # --- Inline answer detection ---
                # Some models (e.g. kimi) output SRCH: as chain-of-thought "thinking"
                # and then immediately provide the answer in the same response.
                # If substantial content exists after the SRCH line, use it directly
                # instead of triggering a real search loop.
                srch_line_end = response_text.lower().find('srch:')
                if srch_line_end != -1:
                    srch_line_end = response_text.find('\n', srch_line_end)
                post_srch_text = response_text[srch_line_end:].strip() if srch_line_end != -1 else ''
                answer_lines = [
                    l for l in post_srch_text.split('\n')
                    if l.strip() and not re.match(r'^\s*(REASON|SRCH|KNOWLEDGE|WEB_SEARCH|REQUEST_TASK):', l, re.I)
                ]
                inline_answer = '\n'.join(answer_lines).strip()

                if len(inline_answer) > 150:
                    # Model already answered inline after SRCH — display that, skip search loop
                    print(f"[CHAT] SRCH detected but model provided inline answer ({len(inline_answer)} chars). Using inline answer, skipping search.", flush=True)
                    response_text = inline_answer
                    # Fall through to normal response display (no continue)

                else:
                    # No inline answer — execute the search and loop
                    # Extract REASON (optional) for better summarization
                    reason_match = re.search(r'REASON:\s*(.*)', response_text, re.IGNORECASE)
                    reason = reason_match.group(1).strip() if reason_match else "Chat Agent Search"

                    socketio.emit('chat_status', {'status': f'searching: {search_query[:20]}...'})

                    # Execute Search with extracted REASON
                    import app
                    search_result = app.perform_unified_search(search_query, reason=reason, summarize=True)
                    search_data = search_result['results_summarized']

                    # Update Context with Results
                    # We append to the persistent context so the agent "remembers" the search in this turn
                    history_injection = f"\n\n--- CHAT SEARCH ---\nQuery: {search_query}\nResults:\n{search_data}\n"

                    if log_manager:
                        # Log internally as system event
                        log_manager.append_to_llm_context(history_injection)
                        # Sync global state
                        global_state['agent_history'] = log_manager.get_llm_context()

                    # Modify user_message to include directive for LLM to analyze results
                    search_system_msg = cfg.get('SystemMessages', 'search_completed', fallback="Search completed. The results have been added to the execution history. Please analyze the search results and provide relevant findings to the user.")
                    user_message = f"{search_system_msg}\n\nOriginal user question: {original_user_message}"

                    # Continue loop -> Re-prompt LLM with updated context containing search results
                    continue

            # 4b. Check for KNOWLEDGE:
            knowledge_match = re.search(r'KNOWLEDGE:\s*(.*)', _stripped_for_tags, re.IGNORECASE)

            if knowledge_match:
                knowledge_query = knowledge_match.group(1).strip().split('\n')[0].strip()
                print(f"[KNOWLEDGE] Chat agent requesting knowledge search: '{knowledge_query}'", flush=True)

                socketio.emit('chat_status', {'status': f'searching knowledge: {knowledge_query[:20]}...'})

                km = global_state.get('knowledge_manager')
                if km:
                    try:
                        knowledge_results = km.query(knowledge_query, top_k=5)
                        print(f"[KNOWLEDGE] Chat knowledge search returned {len(knowledge_results)} chars", flush=True)
                    except Exception as e:
                        knowledge_results = f"Knowledge search error: {str(e)}"
                        print(f"[KNOWLEDGE] Chat knowledge search error: {e}", flush=True)
                else:
                    knowledge_results = "Knowledge system is not configured or has no documents."
                    print(f"[KNOWLEDGE] Chat knowledge search failed: no knowledge manager", flush=True)

                # Update Context with Results
                history_injection = f"\n\n--- CHAT KNOWLEDGE SEARCH ---\nQuery: {knowledge_query}\nResults:\n{knowledge_results}\n"

                if log_manager:
                    log_manager.append_to_llm_context(history_injection)
                    global_state['agent_history'] = log_manager.get_llm_context()

                print(f"[KNOWLEDGE] Chat knowledge results injected into context. Re-prompting LLM.", flush=True)

                # Re-prompt LLM with knowledge results
                knowledge_system_msg = cfg.get('SystemMessages', 'knowledge_completed', fallback="Knowledge search completed. The results have been added to the context. Please analyze the knowledge results and provide relevant findings to the user.")
                user_message = f"{knowledge_system_msg}\n\nOriginal user question: {original_user_message}"

                continue

            # 4c. Check for WEB_SEARCH:
            websearch_match = re.search(r'WEB_SEARCH:\s*(.*)', _stripped_for_tags, re.IGNORECASE)

            if websearch_match:
                ws_query = websearch_match.group(1).strip().split('\n')[0].strip()

                # Extract Reason (optional)
                ws_reason_match = re.search(r'(?:REASON|Reason):\s*(.*?)(?=WEB_SEARCH:|$)', response_text, re.DOTALL | re.IGNORECASE)
                ws_reason = ws_reason_match.group(1).strip() if ws_reason_match else "Chat agent web search"

                print(f"[WEB_SEARCH] Chat agent requesting web search: '{ws_query}' (reason: {ws_reason[:80]})", flush=True)

                # Check if web search is enabled
                ws_cfg = get_config()
                ws_enabled = ws_cfg.getboolean('WebSearch', 'enabled', fallback=False)

                if not ws_enabled:
                    print("[WEB_SEARCH] Web search module is disabled.", flush=True)
                    # Inject a message telling the LLM web search is not available
                    ws_injection = "\n\n--- WEB SEARCH ---\nWeb search is not enabled. Please answer based on your existing knowledge or suggest the user enable Web Search in settings.\n"
                    if log_manager:
                        log_manager.append_to_llm_context(ws_injection)
                        global_state['agent_history'] = log_manager.get_llm_context()
                    user_message = "Web search is not enabled. Please provide the best answer you can without web search."
                    continue

                socketio.emit('chat_status', {'status': f'web searching: {ws_query[:30]}...'})

                # Perform web search synchronously
                try:
                    import web_search_module as wsm
                    from config import get_config as gc

                    ws_config = wsm.get_web_search_config(gc())

                    # Create status tracker and store in global state
                    ws_status = wsm.WebSearchStatus(socketio)
                    global_state['web_search_status'] = ws_status

                    ws_result = wsm.perform_web_search(
                        reason=ws_reason,
                        query=ws_query,
                        config=ws_config,
                        socketio=socketio,
                        global_state=global_state
                    )

                    if ws_result['success'] and not ws_result.get('partial'):
                        # Full success — relevant content found and processed
                        ws_summary = ws_result['summary']
                        ws_injection = f"\n\n--- WEB SEARCH RESULT ---\nQuery: {ws_query}\n"
                        if ws_result.get('document_attached'):
                            ws_injection += f"Document attached to knowledge: {ws_result['document_name']}\n"
                            ws_injection += f"Source URL: {ws_result.get('source_url', 'N/A')}\n"
                            # Skip summary when document is attached as direct inject (full content already in prompt via Knowledge injection)
                            # ws_injection += f"Summary:\n{ws_summary}\n--- END WEB SEARCH ---\n"
                            ws_injection += f"The full document content is available in the Knowledge context above.\n--- END WEB SEARCH ---\n"
                        else:
                            ws_injection += f"Summary:\n{ws_summary}\n--- END WEB SEARCH ---\n"
                        if ws_result.get('document_attached'):
                            ws_context_msg = ws_cfg.get('SystemMessages', 'web_search_completed_attached',
                                fallback="Web search completed. The full content has been attached to the Knowledge store as a document. Use KNOWLEDGE: <query> to retrieve specific information from it, then answer the user's question.")
                        else:
                            ws_context_msg = ws_cfg.get('SystemMessages', 'web_search_completed_injected',
                                fallback="Web search completed. The findings are included directly above in the context. Analyze them and answer the user's question with specific details from the source.")
                        print(f"[WEB_SEARCH] Results injected into context ({len(ws_summary)} chars, doc_attached={ws_result.get('document_attached', False)}). Re-prompting LLM.", flush=True)

                    elif ws_result['success'] and ws_result.get('partial'):
                        # Partial — found results but no relevant content could be fetched
                        ws_summary = ws_result['summary']
                        ws_injection = f"\n\n--- WEB SEARCH PARTIAL RESULT ---\nQuery: {ws_query}\nNote: Search found results but none of the pages could be fetched or contained relevant content. Only search snippets are available.\n\n{ws_summary}\n--- END WEB SEARCH ---\n"
                        ws_context_msg = "Web search completed with partial results. The pages found could not be fetched or did not contain relevant content — only brief search snippets are available. Analyze what is available and decide: either try a different search query, or answer the user with the limited information you have. Be transparent about the limitation."
                        print(f"[WEB_SEARCH] Partial results injected into context ({len(ws_summary)} chars). Re-prompting LLM.", flush=True)

                    else:
                        # Full failure — no results at all
                        ws_injection = f"\n\n--- WEB SEARCH FAILED ---\nQuery: {ws_query}\nError: {ws_result['summary']}\n"
                        ws_context_msg = "Web search failed — no results were found for the query. You may try a different, simpler search query, or inform the user that the information could not be found online."
                        print(f"[WEB_SEARCH] Search failed: {ws_result['summary']}", flush=True)

                    if log_manager:
                        log_manager.append_to_llm_context(ws_injection)
                        global_state['agent_history'] = log_manager.get_llm_context()

                except Exception as ws_err:
                    print(f"[WEB_SEARCH] Error performing web search: {ws_err}", flush=True)
                    traceback.print_exc()
                    ws_injection = f"\n\n--- WEB SEARCH ERROR ---\nQuery: {ws_query}\nError: {str(ws_err)}\n"
                    if log_manager:
                        log_manager.append_to_llm_context(ws_injection)
                        global_state['agent_history'] = log_manager.get_llm_context()
                    ws_context_msg = f"Web search encountered an error: {str(ws_err)}. You may try again with a different query or inform the user."

                # Re-prompt LLM with appropriate message based on search outcome
                user_message = f"{ws_context_msg}\n\nOriginal user question: {original_user_message}"
                continue

            # 5. Check for Action Plan (<<ACTION_PLAN_START>> ... <<ACTION_PLAN_STOP>>)
            plan_match = re.search(
                r'<<ACTION_PLAN_START>>(.*?)<<ACTION_PLAN_STOP>>',
                response_text,
                re.DOTALL | re.IGNORECASE
            )

            if plan_match:
                plan_content = plan_match.group(1).strip()

                # Extract title (first line after marker)
                lines = [line.strip() for line in plan_content.split('\n') if line.strip()]
                title = "Multi-Step Plan"
                steps = []

                for line in lines:
                    # Check if line starts with "Title:" or "title:"
                    if line.lower().startswith('title:'):
                        title = line.split(':', 1)[1].strip()
                    # Check if line starts with "Step" followed by number
                    elif re.match(r'step\s+\d+[\.\):]?\s*', line, re.IGNORECASE):
                        # Extract step objective (everything after "Step X. " or "Step X: " etc)
                        step_text = re.sub(r'^step\s+\d+[\.\):]?\s*', '', line, flags=re.IGNORECASE)
                        if step_text:
                            steps.append(step_text)

                if steps and log_manager:
                    log_manager.set_action_plan(title, steps)
                    socketio.emit('chat_status', {'status': f'plan created: {len(steps)} steps'})

                    # Emit action plan data to update UI button
                    plan_data = log_manager.action_plan.get_active_plan()
                    if plan_data:
                        socketio.emit('action_plan_data', {
                            'exists': True,
                            'title': plan_data.get('title', 'Action Plan'),
                            'steps': plan_data['steps'],
                            'total_steps': len(plan_data['steps']),
                            'completed_steps': sum(1 for s in plan_data['steps'] if s.get('completed', False)),
                            'next_step_index': next((i+1 for i, s in enumerate(plan_data['steps']) if not s.get('completed', False)), None),
                            'created_at': plan_data.get('created_at', '')
                        })

                # Remove the plan markup from response
                response_text = response_text.replace(plan_match.group(0), "").strip()

                # Add confirmation message if response is now empty
                if not response_text:
                    response_text = f"I've created an action plan with {len(steps)} steps. Let's start with Step 1."

            # 6. Check for Explicit Step Completion Tag (<<MARK_STEP_COMPLETED: X>>)
            # Handles LLM variations: "3", "Step 3", "step3", "Step: 3", "#3"
            step_mark_match = re.search(r'<<MARK_STEP_COMPLETED:\s*(?:step\s*:?\s*)?#?(\d+)\s*>>', _stripped_for_tags, re.IGNORECASE)

            if step_mark_match:
                try:
                    step_number = int(step_mark_match.group(1))
                    if log_manager:
                        updated = log_manager.action_plan.mark_step_by_index(step_number)
                        if updated:
                            # Emit update to UI immediately
                            plan_data = log_manager.action_plan.get_active_plan()
                            if plan_data:
                                socketio.emit('action_plan_data', {
                                    'exists': True,
                                    'title': plan_data.get('title', 'Action Plan'),
                                    'steps': plan_data['steps'],
                                    'total_steps': len(plan_data['steps']),
                                    'completed_steps': sum(1 for s in plan_data['steps'] if s.get('completed', False)),
                                    'next_step_index': next((i+1 for i, s in enumerate(plan_data['steps']) if not s.get('completed', False)), None),
                                    'created_at': plan_data.get('created_at', '')
                                })
                except Exception as e:
                    print(f"Error marking step completed: {e}")

                # Remove the tag from the message shown to the user
                response_text = response_text.replace(step_mark_match.group(0), "").strip()

            # 6b. Check for Plan Abort (<<ABORT_PLAN: reason>>)
            abort_match = re.search(r'<<ABORT_PLAN(?::\s*(.*?))?>>', response_text, re.IGNORECASE | re.DOTALL)
            if not abort_match:
                # Fallback: no closing bracket
                abort_match = re.search(r'<<ABORT_PLAN(?::\s*(.*))?$', response_text, re.IGNORECASE | re.DOTALL)

            if abort_match:
                abort_reason = (abort_match.group(1) or "").strip().rstrip('>')
                print(f"[CHAT] ABORT_PLAN detected. Reason: {abort_reason or 'none'}", flush=True)

                log_manager = global_state.get('log_manager')
                if log_manager:
                    aborted_title, new_active = log_manager.abort_action_plan(abort_reason)

                    if aborted_title:
                        # Emit updated plan data (new active plan or cleared)
                        if new_active:
                            socketio.emit('action_plan_data', {
                                'exists': True,
                                'title': new_active.get('title', 'Action Plan'),
                                'steps': new_active['steps'],
                                'total_steps': len(new_active['steps']),
                                'completed_steps': sum(1 for s in new_active['steps'] if s.get('completed', False)),
                                'next_step_index': next((i+1 for i, s in enumerate(new_active['steps']) if not s.get('completed', False)), None),
                                'created_at': new_active.get('created_at', '')
                            })
                        else:
                            socketio.emit('action_plan_cleared')

                # Remove the tag from response
                response_text = response_text.replace(abort_match.group(0), "").strip()

            # 7. Check for System Switch Request (<<SWITCH_SYSTEM: system_name>>)
            switch_match = re.search(r'<<SWITCH_SYSTEM:\s*(.*?)>>', _stripped_for_tags, re.IGNORECASE)

            if switch_match:
                target_system = switch_match.group(1).strip()

                # Extract the reason: last non-empty line before <<SWITCH_SYSTEM:>>.
                # With the prompt format "REASON:[text]\n<<SWITCH_SYSTEM:...>>", this captures
                # the REASON: line directly. Strip the "REASON:" prefix if present.
                text_before_tag = response_text[:switch_match.start()].strip()
                switch_reason = ''
                if text_before_tag:
                    lines = [l.strip() for l in text_before_tag.split('\n') if l.strip()]
                    if lines:
                        raw = lines[-1][:300]
                        switch_reason = re.sub(r'^REASON:\s*', '', raw, flags=re.IGNORECASE)

                # Prevent re-switch loop: if LLM regenerates the same switch tag, just strip it
                if completed_switch_target and target_system.lower() == completed_switch_target.lower():
                    print(f"[CHAT] Ignoring duplicate SWITCH_SYSTEM for already-completed target: {target_system}", flush=True)
                    response_text = response_text.replace(switch_match.group(0), "").strip()
                    # Fall through to emit whatever text remains
                else:
                    # Validate system exists in saved connections
                    import session_manager
                    connections = session_manager.load_connections()

                    system_valid = False
                    for conn in connections:
                        conn_name = conn.get('name', f"{conn.get('username')}@{conn.get('ip')}")
                        if conn_name.lower() == target_system.lower() or \
                           f"{conn.get('username')}@{conn.get('ip')}".lower() == target_system.lower():
                            system_valid = True
                            target_system = conn_name  # Use the actual stored name
                            break

                    if system_valid:
                        # Get shared event objects from global_state (same instances as app.py)
                        switch_event = global_state['_switch_event']
                        switch_response = global_state['_switch_response']

                        # Reset the event before emitting proposal
                        switch_event.clear()
                        switch_response.clear()

                        # Mark pending switch so reconnecting clients can see it
                        global_state['_pending_switch_target'] = {'target_system': target_system, 'reason': switch_reason}

                        # Check server-side auto-switch flag first (set by browser checkbox sync)
                        if global_state.get('auto_switch'):
                            approve_fn = global_state.get('_fn_approve_switch')
                            if approve_fn:
                                approve_fn(target_system)   # sets switch_event immediately
                                print(f"[CHAT] Auto-switch enabled — auto-approving switch to {target_system}", flush=True)
                            else:
                                # Fallback: no callable, just set event as approved manually
                                switch_response['approved'] = True
                                switch_response['target_system'] = target_system
                                switch_event.set()
                        # Use api_ui_locked (persistent mode flag) as the gate — not just
                        # api_chat_active — so that reconnecting browsers never get the modal
                        # while the app is in API mode, regardless of session state.
                        elif global_state.get('api_ui_locked') or global_state.get('api_chat_active'):
                            if global_state.get('_api_chat_unattended'):
                                # Unattended API mode (e.g. auto-analyze after task): no client is
                                # polling, so auto-deny the switch immediately to avoid blocking.
                                switch_response['approved'] = False
                                switch_response['error'] = 'No user available to approve switch in unattended API mode'
                                switch_event.set()
                                print(f"[CHAT] Unattended API mode — auto-denying switch to {target_system}", flush=True)
                            else:
                                # Attended API mode: store pending state for HTTP polling, skip socket emit
                                global_state['_api_chat_pending_type'] = 'switch_proposal'
                                global_state['_api_chat_pending_data'] = {'target_system': target_system, 'reason': switch_reason}
                                global_state['_api_chat_status'] = 'awaiting_approval'
                                print(f"[CHAT] API mode — switch proposal stored for polling: {target_system}", flush=True)
                        else:
                            # WebUI mode: emit socket events as normal
                            socketio.emit('chat_switch_proposal', {'target_system': target_system, 'reason': switch_reason})
                            socketio.emit('chat_status', {'status': f'awaiting approval for switch to {target_system}'})
                            print(f"[CHAT] System switch proposal sent, waiting for user: {target_system}", flush=True)
                            # Small sleep to ensure emit is flushed before blocking on wait()
                            socketio.sleep(0.1)

                        # Wait for user/API response (timeout after 5 minutes)
                        # In unattended API mode the event is already set above, so this returns immediately.
                        approved = switch_event.wait(timeout=300)
                        print(f"[CHAT SWITCH] Wait returned: approved={approved}", flush=True)

                        # Clear pending flags
                        global_state['_pending_switch_target'] = None
                        if global_state.get('api_chat_active'):
                            global_state['_api_chat_pending_type'] = None
                            global_state['_api_chat_pending_data'] = None
                            global_state['_api_chat_status'] = 'processing'

                        if not approved:
                            # Dismiss the modal on the frontend
                            socketio.emit('chat_switch_dismissed', {})
                            # Timeout - no response, inject message and let agent respond
                            switch_timeout_template = cfg.get('SystemMessages', 'switch_timeout', fallback="[SYSTEM EVENT] Your request to switch to '{target_system}' timed out - no user response was received within the time limit. Please acknowledge this to the user.")
                            switch_sys_msg = switch_timeout_template.replace('{target_system}', target_system)
                        elif switch_response.get('approved'):
                            # Switch was successful - mark target to prevent re-switch loop
                            completed_switch_target = target_system
                            new_system = switch_response.get('target_system', target_system)
                            switch_approved_template = cfg.get('SystemMessages', 'switch_approved', fallback="[SYSTEM EVENT] SUCCESS: The user APPROVED your request. System context has been switched to '{new_system}'. You are now connected to this system. Please confirm the switch to the user and continue assisting them.")
                            switch_sys_msg = switch_approved_template.replace('{new_system}', new_system)
                            print(f"[CHAT] Switch approved, injecting success context for agent", flush=True)
                        else:
                            # Switch was denied or failed - inject denial message for agent
                            error_msg = switch_response.get('error', 'Request denied by user')
                            switch_denied_template = cfg.get('SystemMessages', 'switch_denied', fallback="[SYSTEM EVENT] DENIED: The user REJECTED your request to switch to '{target_system}'. Reason: {error_msg}. Please acknowledge this to the user and continue assisting them with the current system.")
                            switch_sys_msg = switch_denied_template.replace('{target_system}', target_system).replace('{error_msg}', error_msg)
                            print(f"[CHAT] Switch denied, injecting denial context for agent", flush=True)

                        # Persist the switch result to LLM context so it survives across turns
                        if log_manager:
                            log_manager.append_to_llm_context(f"\n[SYSTEM EVENT] {switch_sys_msg}")
                            global_state['agent_history'] = log_manager.get_llm_context()

                        # In unattended API mode:
                        # - If switch was DENIED: emit summary and break (no second LLM call needed).
                        # - If switch was APPROVED (via auto_switch): continue so the LLM can
                        #   propose REQUEST_TASK on the new system without needing manual polling.
                        if global_state.get('_api_chat_unattended') and not switch_response.get('approved'):
                            summary = response_text.replace(switch_match.group(0), "").strip()
                            if not summary:
                                summary = switch_sys_msg
                            socketio.emit('chat_response', {'role': 'assistant', 'content': summary})
                            if log_manager:
                                log_manager.log_chat_message('assistant', summary)
                            if global_state.get('api_chat_active'):
                                global_state['_api_chat_response'] = summary
                            break

                        # Re-prompt with switch result so agent can respond naturally
                        user_message = f"{switch_sys_msg}\n\nOriginal user question: {original_user_message}"
                        response_text = response_text.replace(switch_match.group(0), "").strip()
                        continue
                    else:
                        # System not found - inform user
                        socketio.emit('chat_response', {
                            'role': 'assistant',
                            'content': f"System '{target_system}' not found in saved connections. Please check the system name and try again."
                        })
                        socketio.emit('chat_status', {'status': 'idle'})
                        return

                # Remove the tag from the response
                response_text = response_text.replace(switch_match.group(0), "").strip()

            # 8. Check for Task Requests (<<REQUEST_TASK>>)
            # Flexible regex to handle LLM variations:
            # <<REQUEST_TASK: Objective>>       (Standard)
            # <<REQUEST_TASK: Objective>        (Single closing bracket)
            # <<REQUEST_TASK: Objective          (No closing bracket)
            # <<REQUEST_TASK>>: Objective       (Common LLM hallucination)
            # <<REQUEST_TASK>> Objective        (No colon after tag)

            task_match = None
            new_task_objective = ""

            # Pattern 1: <<REQUEST_TASK: ...>> (double closing bracket - standard)
            # Uses >> instead of >{1,2} to avoid matching single > in shell redirects (2>/dev/null)
            # NOTE: searched in response_text (not _stripped_for_tags) so that commands inside
            # backticks within the objective are preserved (e.g. `echo "test" && uname -a`).
            match_standard = re.search(r'<<REQUEST_TASK:\s*(.*?)>>', response_text, re.IGNORECASE | re.DOTALL)

            # Pattern 2: <<REQUEST_TASK: ... (no closing bracket — capture to end)
            match_no_close = re.search(r'<<REQUEST_TASK:\s*(.*)', response_text, re.IGNORECASE | re.DOTALL)

            # Pattern 3: Loose <<REQUEST_TASK>>: ...
            match_loose = re.search(r'<<REQUEST_TASK>>:?\s*(.*)', response_text, re.IGNORECASE)

            if match_standard:
                task_match = match_standard
                new_task_objective = match_standard.group(1).strip().rstrip('>')
                print(f"[CHAT] REQUEST_TASK matched (Pattern 1 - standard >>): objective='{new_task_objective[:200]}'", flush=True)
            elif match_no_close:
                task_match = match_no_close
                new_task_objective = match_no_close.group(1).strip().rstrip('>')
                print(f"[CHAT] REQUEST_TASK matched (Pattern 2 - no close): objective='{new_task_objective[:200]}'", flush=True)
            elif match_loose:
                task_match = match_loose
                new_task_objective = match_loose.group(1).strip().rstrip('>')
                print(f"[CHAT] REQUEST_TASK matched (Pattern 3 - loose): objective='{new_task_objective[:200]}'", flush=True)
            else:
                print(f"[CHAT] No REQUEST_TASK tag detected in response", flush=True)

            final_content = response_text
            if task_match:
                # Clean the tag from the response shown to user
                clean_content = response_text.replace(task_match.group(0), "").strip()
                if not clean_content:
                    clean_content = f"I suggest we perform a new task: {new_task_objective}"
                final_content = clean_content

            # 8. Final Response — include raw LLM text so UI debug panel can show it for any message
            socketio.emit('chat_response', {'role': 'assistant', 'content': final_content, 'raw': response_text})

            # Store response for API polling
            if global_state.get('api_chat_active'):
                global_state['_api_chat_response'] = final_content

            if task_match:
                if global_state.get('_api_chat_unattended') and not global_state.get('auto_accept_tasks') \
                        and not global_state.get('api_chat_active'):
                    # Truly unattended (internal trigger, no API caller polling): skip to avoid orphaned state.
                    # If api_chat_active=True there IS a caller polling, so we fall through to store pending_data.
                    print(f"[CHAT] Unattended API mode (no auto-accept, no active caller) — skipping REQUEST_TASK: {new_task_objective[:80]}", flush=True)
                elif global_state.get('auto_accept_tasks'):
                    # Server-side auto-accept: start the task immediately without waiting for approval.
                    start_fn = global_state.get('_fn_start_task')
                    if start_fn:
                        task_id = start_fn(new_task_objective)
                        print(f"[CHAT] Auto-accept enabled — task auto-started: task_id={task_id}", flush=True)
                    else:
                        print(f"[CHAT] Auto-accept enabled but _fn_start_task not available — falling through to normal flow", flush=True)
                        global_state['_pending_task_proposal'] = new_task_objective
                        if global_state.get('api_chat_active'):
                            global_state['_api_chat_pending_type'] = 'task_proposal'
                            global_state['_api_chat_pending_data'] = {'objective': new_task_objective}
                        else:
                            socketio.emit('chat_task_proposal', {'objective': new_task_objective})
                else:
                    # Mark pending so reconnecting clients can see the task proposal
                    global_state['_pending_task_proposal'] = new_task_objective
                    if global_state.get('api_chat_active'):
                        # API mode: store pending for HTTP polling, skip socket emit
                        global_state['_api_chat_pending_type'] = 'task_proposal'
                        global_state['_api_chat_pending_data'] = {'objective': new_task_objective}
                    else:
                        socketio.emit('chat_task_proposal', {'objective': new_task_objective})

            if log_manager:
                log_manager.log_chat_message('assistant', final_content)

            break  # Exit loop

        else:
            # Loop exhausted max_turns without a clean break — emit last LLM response to avoid silent failure
            print(f"[CHAT] max_turns ({max_turns}) exhausted without final answer — emitting last response ({len(response_text)} chars)", flush=True)
            if response_text:
                socketio.emit('chat_response', {'role': 'assistant', 'content': response_text, 'raw': response_text})
                if log_manager:
                    log_manager.log_chat_message('assistant', response_text)
            else:
                socketio.emit('chat_response', {'role': 'assistant', 'content': "I'm sorry, I wasn't able to complete my response after several attempts. Please try rephrasing your question."})

        socketio.emit('chat_status', {'status': 'idle'})

    except Exception as e:
        traceback.print_exc()
        socketio.emit('chat_response', {'role': 'assistant', 'content': f"Error: {str(e)}"})
        socketio.emit('chat_status', {'status': 'error'})
    finally:
        _chat_cancel_event.clear()  # Clear any stale cancel flag for the next request
        _chat_processing_lock.release()
        # Clean up API session — mark completed if still processing
        # Keep api_chat_active=True if a task_proposal is pending (API client still needs to act)
        # api_ui_locked is intentionally NOT cleared here — it stays until "Take Control" or /api/chat/release
        if global_state.get('api_chat_active'):
            if global_state.get('_api_chat_status') in ('processing', None):
                global_state['_api_chat_status'] = 'completed'
            if global_state.get('_api_chat_pending_type') == 'task_proposal':
                # Stay active — API client must call /api/chat/approve to act on proposal
                print(f"[CHAT] API session staying active (task_proposal pending), request_id={global_state.get('api_chat_request_id')}", flush=True)
            else:
                global_state['api_chat_active'] = False
                # UI lock (api_ui_locked) persists — browser stays read-only until explicit release
                print(f"[CHAT] API session processing done, request_id={global_state.get('api_chat_request_id')} (UI still locked)", flush=True)
        print(f"[CHAT] Lock released ({msg_type} message)", flush=True)
