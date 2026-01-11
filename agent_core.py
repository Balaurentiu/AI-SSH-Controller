import re
import traceback
import time
import eventlet
from time import sleep # Folosim sleep direct
from langchain_community.llms import Ollama
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import PromptTemplate
from eventlet.timeout import Timeout

# Importam functiile necesare din modulele separate
from config import get_config
from ssh_utils import execute_ssh_command, set_detected_os
from log_manager import UnifiedLogManager

# ---
# --- Functii Helper pentru Logare ---
# ---

def log_and_emit(socketio, global_state, message, clear=False):
    """Functie helper pentru a loga, emite prin socket si salva in stare."""
    print(message, flush=True) # Logam in consola serverului
    global_state['last_session']['log'] += message + '\n' # Adaugam la log-ul complet
    socketio.emit('agent_log', {'data': message, 'clear': clear})

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
            # Pornim un greenlet pentru actualizarea timer-ului
            self.greenlet = eventlet.spawn(self._update_timer)
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
                eventlet.sleep(1)
        except Exception as e:
            print(f"ThinkingIndicator update error: {e}")

    def stop(self):
        """Opreste indicatorul de thinking."""
        try:
            self.stop_flag = True
            if self.greenlet:
                try:
                    self.greenlet.kill()
                except:
                    pass
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
        self.greenlet = None

    def start(self):
        """Porneste timer-ul de executie."""
        try:
            self.start_time = time.time()
            self.stop_flag = False
            # Use specific timeout if provided, else global
            current_timeout = self.specific_timeout if self.specific_timeout else self.global_state.get('command_timeout', 120)

            self.socketio.emit('command_exec_start', {'timeout': current_timeout, 'command': self.command})
            # Pornim un greenlet pentru actualizarea timer-ului
            self.greenlet = eventlet.spawn(self._update_timer)
        except Exception as e:
            print(f"CommandExecutionTimer start error: {e}")

    def _update_timer(self):
        """Actualizeaza timer-ul la fiecare secunda."""
        try:
            while not self.stop_flag:
                elapsed = int(time.time() - self.start_time)
                # Use specific timeout if provided, else global
                current_timeout = self.specific_timeout if self.specific_timeout else self.global_state.get('command_timeout', 120)

                remaining = max(0, current_timeout - elapsed)
                self.socketio.emit('command_exec_update', {'remaining': remaining})
                if remaining <= 0:
                    break
                eventlet.sleep(1)
        except Exception as e:
            print(f"CommandExecutionTimer update error: {e}")

    def stop(self):
        """Opreste timer-ul de executie."""
        try:
            self.stop_flag = True
            if self.greenlet:
                try:
                    self.greenlet.kill()
                except:
                    pass
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

    return cmd

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

    try:
        cfg = get_config()
        provider = cfg.get('General', 'provider', fallback='')
        model_name = cfg.get('Agent', 'model_name', fallback='')

        # Determine Prompt (Cloud prompts for Gemini/Anthropic, Ollama prompts for Ollama)
        prompt_key = 'OllamaSummarizePrompt' if provider == 'ollama' else 'CloudSummarizePrompt'
        prompt_template_str = cfg.get(prompt_key, 'template', fallback="Summarize: {history}")

        # Init LLM
        llm = None
        if provider == 'ollama':
            api_url = cfg.get('Ollama', 'api_url', fallback='')
            llm = Ollama(model=model_name, base_url=api_url, timeout=300)
        elif provider == 'gemini':
            api_key = cfg.get('General', 'gemini_api_key', fallback='')
            llm = ChatGoogleGenerativeAI(model=model_name, google_api_key=api_key, generation_config={"temperature": 0.5})
        elif provider == 'anthropic':
            api_key = cfg.get('General', 'anthropic_api_key', fallback='')
            llm = ChatAnthropic(model=model_name, api_key=api_key, temperature=0.5)

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

    except Exception as e:
        log_and_emit(socketio, global_state, f"{log_prefix} CRITICAL ERROR: {e}")
        traceback.print_exc()


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

def execute_ssh_command_with_timeout(socketio, global_state, command, timeout_seconds, max_retries=3):
    """
    Executa o comanda SSH cu timeout si retry logic.
    Citeste timeout-ul dinamic din global_state pentru a permite actualizari live.
    Returneaza (success, result, attempt_number).
    """
    import eventlet
    from eventlet.timeout import Timeout as EventletTimeout
    from eventlet.event import Event

    for attempt in range(1, max_retries + 1):
        try:
            # Read current timeout from global_state (allows live updates during execution)
            current_timeout = global_state.get('command_timeout', timeout_seconds)
            log_and_emit(socketio, global_state, f"Executing command (attempt {attempt}/{max_retries}, timeout: {current_timeout}s)...")

            # Create an event to signal completion
            execution_complete = Event()
            execution_result = {'success': False, 'data': None, 'timed_out': False}
            start_time = time.time()

            def execute_command_greenlet():
                """Greenlet that executes the SSH command."""
                try:
                    result = execute_ssh_command(command)
                    execution_result['success'] = True
                    execution_result['data'] = result
                except Exception as e:
                    execution_result['success'] = False
                    execution_result['data'] = str(e)
                finally:
                    execution_complete.send()

            def timeout_monitor_greenlet():
                """Greenlet that monitors timeout from global_state."""
                try:
                    while not execution_complete.ready():
                        elapsed = time.time() - start_time
                        # Read current timeout from global_state (allows live updates)
                        current_limit = global_state.get('command_timeout', timeout_seconds)

                        if elapsed >= current_limit:
                            execution_result['timed_out'] = True
                            execution_complete.send()
                            break

                        eventlet.sleep(0.5)  # Check every 0.5 seconds
                except:
                    pass

            # Start both greenlets
            exec_greenlet = eventlet.spawn(execute_command_greenlet)
            monitor_greenlet = eventlet.spawn(timeout_monitor_greenlet)

            # Wait for completion or timeout
            execution_complete.wait()

            # Kill both greenlets
            try:
                exec_greenlet.kill()
                monitor_greenlet.kill()
            except:
                pass

            # Check if timed out
            if execution_result['timed_out']:
                actual_timeout = global_state.get('command_timeout', timeout_seconds)
                log_and_emit(socketio, global_state, f"--- TIMEOUT after {actual_timeout}s (attempt {attempt}/{max_retries}) ---")
                raise EventletTimeout()

            # Return result if successful
            if execution_result['success']:
                return True, execution_result['data'], attempt
            else:
                raise Exception(execution_result['data'])

        except EventletTimeout:
            # Read actual timeout that was used
            actual_timeout = global_state.get('command_timeout', timeout_seconds)
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
                return False, error_msg_context, attempt
            else:
                # Connection died. This is a network failure.
                log_and_emit(socketio, global_state, "--- Connection lost. Aborting retries. ---")
                return False, f"Error: Command timed out and connection was lost.", attempt

        except Exception as e:
            # Check if this error is due to User Stop
            if not global_state.get('task_running', False):
                log_and_emit(socketio, global_state, "--- Execution interrupted by User Stop. ---")
                return False, "Task stopped by user during execution.", attempt

            log_and_emit(socketio, global_state, f"--- Command execution error: {str(e)} ---")
            if attempt < max_retries:
                log_and_emit(socketio, global_state, f"--- Retrying... ---")
                sleep(2)
            else:
                return False, f"Error: {str(e)}", attempt

    # Should not reach here, but just in case
    return False, "Error: Unexpected execution flow.", max_retries

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
        provider = cfg.get('General', 'provider', fallback='')
        model_name = cfg.get('Agent', 'model_name', fallback='')

        if not provider or not model_name:
            log_and_emit(socketio, global_state, f"{log_prefix} ERROR: Validator LLM not configured.")
            return False, "Validator LLM not configured."

        # Determinam ce prompt sa folosim (Cloud prompts for Gemini/Anthropic, Ollama prompts for Ollama)
        prompt_key = 'OllamaValidatePrompt' if provider == 'ollama' else 'CloudValidatePrompt'
        prompt_template_str = cfg.get(prompt_key, 'template', fallback="Analyze: {command}\nRespond APPROVE or REJECT")

        # Initializam clientul LLM
        llm = None
        if provider == 'ollama':
            api_url = cfg.get('Ollama', 'api_url', fallback='')
            if not api_url:
                log_and_emit(socketio, global_state, f"{log_prefix} ERROR: Ollama URL not configured.")
                return False, "Validator Ollama URL not configured."
            llm = Ollama(model=model_name, base_url=api_url, timeout=60)
        elif provider == 'gemini':
            api_key = cfg.get('General', 'gemini_api_key', fallback='')
            if not api_key:
                log_and_emit(socketio, global_state, f"{log_prefix} ERROR: Gemini API Key not configured.")
                return False, "Validator Gemini API Key not configured."
            llm = ChatGoogleGenerativeAI(model=model_name, google_api_key=api_key,
                                         generation_config={"temperature": 0.0})
        elif provider == 'anthropic':
            api_key = cfg.get('General', 'anthropic_api_key', fallback='')
            if not api_key:
                log_and_emit(socketio, global_state, f"{log_prefix} ERROR: Anthropic API Key not configured.")
                return False, "Validator Anthropic API Key not configured."
            llm = ChatAnthropic(model=model_name, api_key=api_key, temperature=0.0)
        else:
            log_and_emit(socketio, global_state, f"{log_prefix} ERROR: Unknown LLM provider '{provider}'.")
            return False, "Unknown validator LLM provider."

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
                    return True, f"Validator exception after {max_retries} attempts - command approved by default."

    except Exception as e:
        error_msg = f"--- UNEXPECTED ERROR during command validation setup: {e} ---"
        log_and_emit(socketio, global_state, error_msg)
        traceback.print_exc()
        return True, "Unexpected error in validator setup - command approved by default."


def summarize_single_output(output_text, llm, provider, socketio, global_state):
    """
    Compresses a single large command output using the LLM.
    Uses configurable prompts from config.ini.
    """
    log_prefix = "--- Output Flood Protection ---"
    log_and_emit(socketio, global_state, f"{log_prefix} Output size {len(output_text)} exceeds limit. Summarizing...")

    try:
        # 1. Get Prompt from Config
        cfg = get_config()
        prompt_key = 'OllamaStepSummaryPrompt' if provider == 'ollama' else 'CloudStepSummaryPrompt'
        prompt_template_str = cfg.get(prompt_key, 'template', fallback="Summarize output: {output}")

        # 2. Format Prompt
        # Cap input to protect LLM window
        prompt = PromptTemplate.from_template(prompt_template_str).format(output=output_text[:20000])

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
            # Fallback truncation
            return output_text[:1000] + "\n... [Output Truncated due to size] ...\n" + output_text[-1000:]

        if summary:
            return f"[ Output too big, here is a summary of it : ]\n{summary.strip()}"
        else:
            return output_text[:2000] + "\n... [Output Truncated] ..."

    except Exception as e:
        traceback.print_exc()
        return output_text[:1000] + "\n... [Output Truncated] ..."


def agent_task_runner(socketio, global_state, control_flags, event_objects, log_manager=None):
    """
    Thread-ul principal care ruleaza task-ul agentului.
    Primeste si modifica 'global_state' direct.
    Uses UnifiedLogManager for multi-log architecture.
    """

    # Initialize log manager if not provided
    if log_manager is None:
        log_manager = UnifiedLogManager()

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

            # Actualizam system_os_info cu informatii complete
            system_context = f"OS: {detected_os}, User: {detected_user}, Sudo: {sudo_status}"
            global_state['system_os_info'] = system_context

            # IMPORTANT: Communicate detected OS to ssh_utils for proper PTY handling
            set_detected_os(detected_os)

            # Update log manager with system info string prep
            system_info_detailed = f"{detected_os}, user: {detected_user}, Sudo: {sudo_status}, IP: {global_state.get('system_ip', 'unknown')}"

            # --- NEW: Always Auto-Summarize Previous History on Task Start ---
            existing_context = log_manager.get_llm_context()

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
            log_manager.log_new_task(current_objective, system_info_detailed)

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
        llm = None
        if PROVIDER == 'ollama':
            api_url = cfg.get('Ollama', 'api_url', fallback='')
            if not api_url:
                raise ValueError("Ollama API URL missing in config.ini.")
            llm = Ollama(model=MODEL_NAME, base_url=api_url, timeout=300) # Timeout 5 min
        elif PROVIDER == 'gemini':
            api_key = cfg.get('General', 'gemini_api_key', fallback='')
            if not api_key:
                raise ValueError("Gemini API Key missing in config.ini.")
            llm = ChatGoogleGenerativeAI(model=MODEL_NAME, google_api_key=api_key,
                                         generation_config={"temperature": 0.5}) # Adaugam temperatura
        elif PROVIDER == 'anthropic':
            api_key = cfg.get('General', 'anthropic_api_key', fallback='')
            if not api_key:
                raise ValueError("Anthropic API Key missing in config.ini.")
            llm = ChatAnthropic(model=MODEL_NAME, api_key=api_key, temperature=0.5)
        else:
            raise ValueError(f"Unsupported LLM: {PROVIDER}")

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
                    if summarization_event.ready():
                        summarization_event.reset()

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
                format_args = {
                    'objective': global_state['current_objective'], # Folosim obiectivul actualizat
                    'history': global_state['agent_history'],
                    'system_info': global_state['system_os_info'],
                    'command_timeout': global_state.get('command_timeout', 120)
                }
                
                # Verificam daca prompt-ul are variabile necunoscute
                required_keys = re.findall(r'\{(\w+)\}', prompt_template_str)
                missing_keys = [k for k in required_keys if k not in format_args]
                if missing_keys:
                    raise KeyError(f"Prompt template missing keys: {missing_keys}")

                full_prompt = prompt_template_obj.format(**format_args)
                
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
            
            while retries < 5:
                if not control_flags['is_running']():
                    break # Iesim din bucla de reincercari

                # Doar logam incercarile esuate (nu prima incercare de succes)
                if retries == 0:
                    log_agent(f"\n--- STEP {step_counter}/{MAX_STEPS} ---")
                    # Step will be logged after we extract reason and command
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
                    stop_sequences = ["Output:", "Observation:", "Result:", "\nOutput", "\nResult"]

                    # Invoke LLM with stop sequences
                    try:
                        llm_response_obj = llm.invoke(full_prompt, stop=stop_sequences)
                    except TypeError:
                        # Fallback for models that don't accept 'stop' parameter
                        llm_response_obj = llm.invoke(full_prompt)

                    # Oprim indicatorul de thinking
                    thinking.stop()

                    # Extragem textul
                    if PROVIDER == 'gemini' and hasattr(llm_response_obj, 'content'):
                        llm_response = llm_response_obj.content
                    else:
                        llm_response = str(llm_response_obj)

                    llm_response = llm_response.strip()

                    if not llm_response:
                        raise ValueError("Empty response from LLM.")
                        
                    # Salvam raspunsul brut
                    global_state['last_session']['raw_llm_responses'].append(llm_response)
                    raw_responses_formatted = "\n\n".join([f"--- Response {i+1} ---\n{r}" for i, r in enumerate(global_state['last_session']['raw_llm_responses'])])
                    socketio.emit('update_raw_llm_responses', {'data': raw_responses_formatted})

                    # Cautam actiuni
                    report_match = re.search(r"REPORT:\s*(.*)", llm_response, re.DOTALL | re.IGNORECASE)
                    ask_match = None
                    if current_allow_ask_mode:
                        ask_match = re.search(r"ASK:\s*(.*)", llm_response, re.DOTALL | re.IGNORECASE)
                    srch_match = re.search(r"SRCH:\s*(.*)", llm_response, re.DOTALL | re.IGNORECASE)
                    # IMPROVED: Non-greedy regex that stops at reserved keywords to prevent TIMEOUT/REASON from being included in the command
                    command_match = re.search(
                        r'COMMAND:\s*(.*?)(?=\n(?:REASON|TIMEOUT|REPORT|ASK|SRCH|WRITE_FILE|END_CONTENT)|$)',
                        llm_response,
                        re.DOTALL | re.IGNORECASE
                    )
                    # IMPROVED: WRITE_FILE regex that skips any REASON/other text between path and CONTENT marker
                    # Captures: (1) file path, (2) content strictly up to END_CONTENT
                    write_match = re.search(
                        r'WRITE_FILE:\s*(.*?)\n.*?CONTENT:\s*\n(.*?)END_CONTENT',
                        llm_response,
                        re.DOTALL | re.IGNORECASE
                    )
                    timeout_match = re.search(r"TIMEOUT:\s*(\d+)", llm_response, re.IGNORECASE)

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
            reason_text = ""
            
            # Extragem motivul (comun pentru toate actiunile)
            reason_match = re.search(r"REASON:\s*(.*?)(?:COMMAND:|REPORT:|ASK:|SRCH:|WRITE_FILE:|$)", llm_response, re.DOTALL | re.IGNORECASE)
            if reason_match:
                reason_text = reason_match.group(1).strip()
            
            # --- CAZUL 1: REPORT (Task finalizat) ---
            if report_match:
                final_report_text = report_match.group(1).strip()
                report_log_message = f"--- REPORT ---\nREASON: {reason_text}\nREPORT: {final_report_text}"
                log_agent(report_log_message)

                # Log task completion to log_manager
                log_manager.log_task_completed(final_report_text)

                global_state['last_session']['final_report'] = final_report_text
                socketio.emit('final_report', {'data': final_report_text})

                # CRITICAL: Update LLM context file
                history_entry = f"\n\n--- STEP {step_counter} ---\n\n{report_log_message}\n"
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
                if user_answer_event.ready():
                    user_answer_event.reset()

                # Trimitem intrebarea si obiectivul curent catre UI
                socketio.emit('awaiting_user_answer', {
                    'question': question,
                    'reason': reason_text,
                    'objective': global_state['current_objective']
                })

                try:
                    user_answer_event.wait(timeout=3600) # Asteptam 1 ora
                except Timeout:
                    log_agent("\n--- USER ANSWER TIMEOUT (1h). Stopping. ---")
                    break # Oprim task-ul
                except Exception as e:
                    log_agent(f"User answer event interrupted: {e}")
                    # Verificam daca a fost oprit
                    if not control_flags['is_running']():
                        break

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
                history_entry = f"\n\n--- STEP {step_counter} ---\n\n{ask_log_message}\n\nOutput:\nHuman Response: {user_answer_text}\n"
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
                history_entry = f"\n\n--- STEP {step_counter} ---\n\n{srch_log_message}\n\nSearch Results:\n{search_context}\n"

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
                    history_entry = f"\n\n--- STEP {step_counter} ---\n\nREASON: {reason_text}\n\nCOMMAND: (empty)\n\nOutput:\nInvalid empty command. Retrying."
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
            # Don't overwrite if already set by WRITE_FILE handler
            if 'original_command_from_llm' not in locals():
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
                if user_approval_event.ready():
                    user_approval_event.reset()

                # Emitem cererea de aprobare (show readable command in UI)
                socketio.emit('awaiting_command_approval', {'command': display_cmd, 'reason': reason_text})

                # Asteptam aprobarea utilizatorului
                try:
                    user_approval_event.wait(timeout=3600) # Asteptam 1 ora
                except Timeout:
                    log_agent("\n--- APPROVAL TIMEOUT (1h). Stopping. ---")
                    break
                except Exception as e:
                    log_agent(f"Approval event interrupted: {e}")
                    if not control_flags['is_running']():
                        break

                # Verificam din nou daca task-ul mai ruleaza
                if not control_flags['is_running']():
                    break # Oprit in timpul aprobarii

                # Procesam raspunsul utilizatorului
                if user_response.get('approved'):
                    approved_for_execution = True
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
                    history_entry = f"\n\n--- STEP {step_counter} ---\n\nREASON: {reason_text}\n\nCOMMAND: {original_command_from_llm}\n\nOutput:\nIntervention: Rejected by user. Reason: {rejection_reason}\n"
                    log_manager.append_to_llm_context(history_entry)

                    # Sync global state from file to ensure consistency
                    global_state['agent_history'] = log_manager.get_llm_context()
                    socketio.emit('update_history', {'data': global_state['agent_history']})
                    step_counter += 1
                    continue # Trecem la pasul urmator

            elif current_mode == 'independent':
                # --- Validation Logic ---

                # Check if Validator is Enabled globally
                if global_state.get('validator_enabled', True):
                    # --- Validare Automata cu LLM ---
                    validation_input = command_to_validate if 'command_to_validate' in locals() and command_to_validate else command_to_execute

                    is_valid, validation_reason = validate_command_with_llm(socketio, global_state, validation_input, reason_text)

                    # Log validation result to log_manager
                    log_manager.log_validator_result(is_valid, 'independent', validation_reason)

                    if is_valid:
                        log_agent("--- Command Auto-Validated. Proceeding... ---")
                        approved_for_execution = True
                    else:
                        log_agent(f"--- Command Auto-Rejected by Validator ---")
                        log_agent(f"Reason: {validation_reason}")
                        log_manager.log_step_end()

                        # Update LLM context file
                        history_entry = f"\n\n--- STEP {step_counter} ---\n\nREASON: {reason_text}\n\nCOMMAND: {original_command_from_llm}\n\nOutput:\nIntervention: Command auto-rejected by validator. Reason: {validation_reason}\n"
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
                log_agent(f"\nExecuting Command: {display_cmd}")

                # Emitem catre ecranul VM
                vm_prompt = f"\n{global_state['system_username']}@{global_state['system_ip']}~# "
                socketio.emit('vm_screen', {'data': vm_prompt + command_to_execute + '\n'})
                global_state['persistent_vm_output'] += vm_prompt + command_to_execute + '\n'

                # IMPROVEMENT: Executam comanda cu timeout si retry
                # Use the step-specific timeout calculated in parsing phase
                # If step_timeout wasn't set (e.g. direct command match without parsing block), fallback to config
                final_timeout = step_timeout if 'step_timeout' in locals() else global_state.get('command_timeout', 120)

                # Pornim timer-ul de executie
                # Note: We pass the specific timeout to the timer so the UI countdown is correct for this step
                exec_timer = CommandExecutionTimer(socketio, global_state, command=command_to_execute, specific_timeout=final_timeout)
                exec_timer.start()

                success, result, attempt_num = execute_ssh_command_with_timeout(
                    socketio, global_state, command_to_execute, final_timeout, max_retries=3
                )

                # Oprim timer-ul de executie
                exec_timer.stop()
                socketio.emit('command_exec_done')

                if not success:
                    log_agent(f"     --- Command failed after {attempt_num} attempt(s). ---")
                else:
                    log_agent(" .         --- Command Completed ---")

                # 1. Log RAW result to Full Log (Disk) - Audit trail must be complete
                log_manager.log_command_execution(command_to_execute, result, success)

                # 2. Process result for LLM Context (RAM)
                context_result = result

                # --- FEEDBACK FIX: Custom message for WRITE_FILE success ---
                # Prevents the agent from looping/rewriting because it thinks "no output" means failure.
                if "[WRITE_FILE Action]" in original_command_from_llm and success and "Success:" in result:
                    context_result = "Success: File written successfully. Content not shown here but available in full history."

                # --- Flood Protection ---
                else:
                    # Only check for flood if we didn't already swap the message
                    cfg_flood = get_config()
                    SUMMARIZATION_THRESHOLD = cfg_flood.getint('Agent', 'summarization_threshold', fallback=15000)

                    # Threshold check: If output > 30% of total budget, summarize it
                    # (e.g. 4500 chars for a 15000 limit)
                    flood_limit = int(SUMMARIZATION_THRESHOLD * 0.3) if SUMMARIZATION_THRESHOLD > 0 else 4000

                    if len(result) > flood_limit:
                        context_result = summarize_single_output(result, llm, PROVIDER, socketio, global_state)

                # Log step end
                log_manager.log_step_end()

                if not control_flags['is_running']():
                    log_agent("--- Stop signal received during SSH execution. ---")
                    break  # Oprit in timpul executiei SSH

                # Emitem rezultatul (Raw to VM Screen for visibility, Summarized to History)
                socketio.emit('vm_screen', {'data': result + '\n'})  # User sees full output
                global_state['persistent_vm_output'] += result + '\n'

                # Actualizam istoricul agentului cu rezultatul PROCESAT
                history_entry = f"\n\n--- STEP {step_counter} ---\n\nREASON: {reason_text}\n\nCOMMAND: {original_command_from_llm}\n\n"
                if human_intervention_log_entry:
                    history_entry += f"Output:\n{human_intervention_log_entry}\n\n"

                # Use the potentially summarized result for the Agent's memory
                history_entry += f"Output:\n{context_result}\n"

                # CRITICAL: Update LLM context file
                log_manager.append_to_llm_context(history_entry)

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
            
        elif step_counter > MAX_STEPS:
            final_msg = f"\n--- Max steps ({MAX_STEPS}) reached. Stopping. ---"
            log_agent(final_msg)
            global_state['last_session']['final_report'] = f"Stopped after {MAX_STEPS} steps."
            socketio.emit('final_report', {'data': f"Stopped after {MAX_STEPS} steps."})
            
    except Exception as e:
        # Check if this is a forced stop (socket closed, etc)
        if not global_state.get('task_running', False):
            log_agent("\n--- Task stopped immediately by user. ---")
            global_state['last_session']['final_report'] = "Task stopped by user."
            socketio.emit('final_report', {'data': "Task stopped by user."})
        else:
            # Genuine error
            error_message = f"\n--- AGENT RUNNER FATAL ERROR ---\n{type(e).__name__}: {e}\n{traceback.format_exc()}\n--- TASK STOPPED ---"
            log_agent(error_message)
            global_state['last_session']['final_report'] = f"Task failed with error: {e}"
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
def process_chat_message(socketio, global_state, user_message):
    """
    Processes a chat message. Handles:
    1. Normal responses.
    2. Task proposals (<<REQUEST_TASK>>).
    3. Search requests (SRCH:) with auto-recursion (thought loop).
    """
    import app  # Import here to avoid circular dependency

    try:
        cfg = get_config()
        provider = cfg.get('General', 'provider', fallback='')
        model_name = cfg.get('Agent', 'model_name', fallback='')

        # 0. Save User Message (only once at the start)
        log_manager = global_state.get('log_manager')
        if log_manager:
            log_manager.log_chat_message('user', user_message)

        # 1. Select LLM (Use separate chat LLM if configured, otherwise use execution LLM)
        llm = global_state.get('chat_llm')

        if llm is None:
            # Fallback to execution LLM if separate chat LLM not configured
            print("[CHAT] Chat LLM not configured separately. Using execution LLM for chat.", flush=True)
            if provider == 'ollama':
                api_url = cfg.get('Ollama', 'api_url', fallback='')
                print(f"[CHAT] Creating Ollama execution LLM: model={model_name}, url={api_url}", flush=True)
                llm = Ollama(model=model_name, base_url=api_url, timeout=120)
            elif provider == 'gemini':
                api_key = cfg.get('General', 'gemini_api_key', fallback='')
                print(f"[CHAT] Creating Gemini execution LLM: model={model_name}", flush=True)
                llm = ChatGoogleGenerativeAI(model=model_name, google_api_key=api_key, generation_config={"temperature": 0.6})
            elif provider == 'anthropic':
                api_key = cfg.get('General', 'anthropic_api_key', fallback='')
                print(f"[CHAT] Creating Anthropic execution LLM: model={model_name}", flush=True)
                llm = ChatAnthropic(model=model_name, api_key=api_key, temperature=0.6)
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

        # --- RECURSION LOOP (Max 3 turns for Search) ---
        # We loop to allow the agent to: User -> Agent(SRCH) -> System(Results) -> Agent(Answer)
        max_turns = 3
        current_turn = 0

        while current_turn < max_turns:
            current_turn += 1

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
                        formatted_messages.append(f"{role_label}: {content}")

                    chat_history_text = "\n".join(formatted_messages)
                else:
                    chat_history_text = "(No previous chat messages)"
            else:
                chat_history_text = "(Chat history not available)"

            # Format Prompt (Include action_plan_status as separate variable)
            full_prompt = prompt.format(
                objective=global_state.get('current_objective', 'None'),
                action_plan_status=action_plan_status_text,
                system_info=global_state.get('system_os_info', 'Unknown'),
                history=history_context,
                chat_history=chat_history_text,
                user_message=user_message
            )

            # 3. Invoke LLM
            socketio.emit('chat_status', {'status': 'thinking'})

            # DEFINE STOP SEQUENCES
            # Tell the LLM to stop if it starts writing Output or Observation
            # Gemini limit: max 5 stop sequences
            stop_sequences = ["Output:", "Observation:", "Result:", "\nOutput", "\nResult"]

            # Check if model supports 'stop' in invoke method (most LangChain models do)
            try:
                response_obj = llm.invoke(full_prompt, stop=stop_sequences)
            except TypeError:
                # Fallback for models that don't accept 'stop' directly in invoke
                response_obj = llm.invoke(full_prompt)

            response_text = ""
            if hasattr(response_obj, 'content'):
                response_text = response_obj.content
            else:
                response_text = str(response_obj)

            # 4. Check for SRCH:
            srch_match = re.search(r'SRCH:\s*(.*)', response_text, re.IGNORECASE)

            if srch_match:
                search_query = srch_match.group(1).strip()

                # Extract REASON (optional) for better summarization
                reason_match = re.search(r'REASON:\s*(.*)', response_text, re.IGNORECASE)
                reason = reason_match.group(1).strip() if reason_match else "Chat Agent Search"

                socketio.emit('chat_status', {'status': f'searching: {search_query[:20]}...'})

                # Execute Search with extracted REASON
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
                user_message = "Search completed. The results have been added to the execution history. Please analyze the search results and provide relevant findings to the user."

                # Continue loop -> Re-prompt LLM with updated context containing search results
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
                        import app
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
            step_mark_match = re.search(r'<<MARK_STEP_COMPLETED:\s*(\d+)>>', response_text, re.IGNORECASE)

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

            # 7. Check for Task Requests (<<REQUEST_TASK>>)
            # Improved regex to handle variants like:
            # <<REQUEST_TASK: Objective>>  (Standard)
            # <<REQUEST_TASK>>: Objective  (Common LLM hallucination)

            task_match = None
            new_task_objective = ""

            # Pattern 1: Standard <<REQUEST_TASK: ... >>
            match_standard = re.search(r'<<REQUEST_TASK:\s*(.*?)>>', response_text, re.IGNORECASE)

            # Pattern 2: Loose <<REQUEST_TASK>>: ...
            match_loose = re.search(r'<<REQUEST_TASK>>:?\s*(.*)', response_text, re.IGNORECASE)

            if match_standard:
                task_match = match_standard
                new_task_objective = match_standard.group(1).strip()
            elif match_loose:
                task_match = match_loose
                new_task_objective = match_loose.group(1).strip()

            final_content = response_text
            if task_match:
                # Clean the tag from the response shown to user
                clean_content = response_text.replace(task_match.group(0), "").strip()
                if not clean_content:
                    clean_content = f"I suggest we perform a new task: {new_task_objective}"
                final_content = clean_content

                socketio.emit('chat_task_proposal', {'objective': new_task_objective})

            # 8. Final Response (No SRCH found, loop ends)
            socketio.emit('chat_response', {'role': 'assistant', 'content': final_content})

            if log_manager:
                log_manager.log_chat_message('assistant', final_content)

            break  # Exit loop

        socketio.emit('chat_status', {'status': 'idle'})

    except Exception as e:
        traceback.print_exc()
        socketio.emit('chat_response', {'role': 'assistant', 'content': f"Error: {str(e)}"})
        socketio.emit('chat_status', {'status': 'error'})
