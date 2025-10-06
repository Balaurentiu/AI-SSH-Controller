# --- CORECTIE: Monkey patching pentru compatibilitate Gunicorn/Eventlet ---
import eventlet
eventlet.monkey_patch()

# --- NOU: Import pentru mecanismul de asteptare ---
from eventlet.event import Event
from eventlet.timeout import Timeout

import os
import re
import time
import paramiko
import configparser
import requests
import ipaddress
import subprocess
import json
from io import BytesIO
from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO
from langchain_community.llms import Ollama
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions

# --- Initializam aplicatia si WebSocket-ul ---
app = Flask(__name__)
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")

# --- CAI PERSISTENTE ---
APP_DIR = '/app'
KEYS_DIR = os.path.join(APP_DIR, 'keys')
CONFIG_FILE_PATH = os.path.join(APP_DIR, 'config.ini')
CONNECTIONS_FILE_PATH = os.path.join(KEYS_DIR, 'connections.json')
SESSION_FILE_PATH = os.path.join(APP_DIR, 'session.json')

# Asiguram ca directorul pentru chei exista
os.makedirs(KEYS_DIR, exist_ok=True)


# --- MEMORIA APLICATIEI & STAREA TASK-ULUI ---
AGENT_HISTORY = "No commands have been executed yet."
SYSTEM_OS_INFO = "Unknown. The first step should be to determine the OS."
LAST_SESSION = { "log": "", "vm_output": "", "final_report": "", "raw_llm_responses": [], "reasoning_log": "" }
PERSISTENT_VM_OUTPUT = ""
TASK_RUNNING = False
TASK_PAUSED = False
CURRENT_OBJECTIVE = ""
SSH_CONNECTION_STATUS = {"status": "unconfigured", "message": "Not configured"}
LLM_CONNECTION_STATUS = {"status": "unconfigured", "message": "Not configured"}

# --- Variabile pentru modul asistat si control dinamic ---
USER_APPROVAL_EVENT = None
USER_RESPONSE = {}
CURRENT_EXECUTION_MODE = "independent"


# --- Functii pentru salvarea si incarcarea sesiunii ---
def save_current_session_to_disk():
    global AGENT_HISTORY, SYSTEM_OS_INFO, PERSISTENT_VM_OUTPUT, LAST_SESSION
    session_data = {
        'agent_history': AGENT_HISTORY,
        'system_os_info': SYSTEM_OS_INFO,
        'persistent_vm_output': PERSISTENT_VM_OUTPUT,
        'last_session': LAST_SESSION
    }
    with open(SESSION_FILE_PATH, 'w') as f:
        json.dump(session_data, f, indent=4)

def load_session_from_disk():
    global AGENT_HISTORY, SYSTEM_OS_INFO, PERSISTENT_VM_OUTPUT, LAST_SESSION
    if os.path.exists(SESSION_FILE_PATH):
        try:
            with open(SESSION_FILE_PATH, 'r') as f:
                data = json.load(f)
                AGENT_HISTORY = data.get('agent_history', "No commands have been executed yet.")
                SYSTEM_OS_INFO = data.get('system_os_info', "Unknown. The first step should be to determine the OS.")
                PERSISTENT_VM_OUTPUT = data.get('persistent_vm_output', "")
                LAST_SESSION = data.get('last_session', { "log": "", "vm_output": "", "final_report": "", "raw_llm_responses": [], "reasoning_log": "" })
        except (json.JSONDecodeError, FileNotFoundError):
            print("Error loading session file, starting with a fresh session.")
            
def reset_all_memory():
    global AGENT_HISTORY, SYSTEM_OS_INFO, LAST_SESSION, PERSISTENT_VM_OUTPUT
    AGENT_HISTORY = "No commands have been executed yet."
    SYSTEM_OS_INFO = "Unknown. The first step should be to determine the OS."
    PERSISTENT_VM_OUTPUT = ""
    LAST_SESSION = { "log": "", "vm_output": "", "final_report": "", "raw_llm_responses": [], "reasoning_log": "" }
    if os.path.exists(SESSION_FILE_PATH):
        os.remove(SESSION_FILE_PATH)
        print("Session file deleted.")


def get_config():
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE_PATH)
    return config

def load_connections():
    if not os.path.exists(CONNECTIONS_FILE_PATH):
        return []
    try:
        with open(CONNECTIONS_FILE_PATH, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return []

def save_connections(connections):
    with open(CONNECTIONS_FILE_PATH, 'w') as f:
        json.dump(connections, f, indent=4)

# --- FUNCTII DE VALIDARE SPECIFICE PROVIDER-ULUI ---
def check_ollama_connection(api_url):
    if not api_url:
        return False, "Ollama URL cannot be empty.", []
    try:
        response = requests.get(f"{api_url}/api/tags", timeout=5)
        response.raise_for_status()
        models_data = response.json()
        model_names = [model['name'] for model in models_data.get('models', [])]
        return True, "Ollama connection successful.", sorted(model_names)
    except requests.exceptions.ConnectionError:
        return False, "Connection refused. Check Ollama server and host settings.", []
    except requests.exceptions.RequestException:
        return False, "Connection error. Is the URL correct?", []
    except Exception as e:
        return False, f"An unexpected error occurred: {e}", []

def check_gemini_connection(api_key):
    if not api_key:
        return False, "Gemini API Key cannot be empty.", []
    try:
        genai.configure(api_key=api_key)
        gemini_models = [model.name for model in genai.list_models() if 'generateContent' in model.supported_generation_methods]
        if not gemini_models:
            return False, "API Key is valid, but no compatible models found.", []
        return True, "Gemini API Key is valid.", sorted(gemini_models)
    except Exception as e:
        return False, f"Gemini validation failed. Check API Key. Error: {str(e)}", []

# --- FUNCTII SSH & HOST ---
def check_host_availability(ip_str: str):
    if not ip_str: return False, "System IP cannot be empty."
    try: ipaddress.ip_address(ip_str)
    except ValueError: return False, f"Invalid IP address format: {ip_str}"
    try:
        result = subprocess.run(["ping", "-c", "1", "-W", "2", ip_str], capture_output=True, text=True, check=False)
        return (True, f"Host {ip_str} is reachable.") if result.returncode == 0 else (False, f"Host {ip_str} is unreachable or timed out.")
    except Exception as e: return False, f"An error occurred during ping: {str(e)}"

def check_ssh_connection():
    config = get_config()
    ip = config.get('System', 'ip_address', fallback='').strip()
    user = config.get('System', 'username', fallback='').strip()
    key_path = config.get('System', 'ssh_key_path', fallback='').strip()
    if not ip: return False, "SSH Connection Failed: System IP is not configured."
    if not user: return False, "SSH Connection Failed: Username is not configured."
    if not key_path or not os.path.exists(key_path): return False, f"SSH Connection Failed: SSH key not found at {key_path}."
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        pkey = paramiko.RSAKey.from_private_key_file(key_path)
        client.connect(hostname=ip, username=user, pkey=pkey, timeout=5)
        client.close()
        return True, "SSH connection successful."
    except Exception as e: return False, f"SSH Connection Failed: {type(e).__name__} - {str(e)}"


def generate_new_ssh_key():
    """Generates a new RSA 4096 bit SSH key pair using Paramiko."""
    private_key_path = os.path.join(KEYS_DIR, 'id_rsa')
    public_key_path = os.path.join(KEYS_DIR, 'id_rsa.pub')
    
    try:
        key = paramiko.RSAKey.generate(4096)
        key.write_private_key_file(private_key_path)
        os.chmod(private_key_path, 0o600)
        
        pub_key_string = f"ssh-rsa {key.get_base64()} generated-by-ai-agent"
        with open(public_key_path, "w") as f: f.write(pub_key_string)
        
        print("Successfully generated new SSH key pair.")
        return True, "Key generated successfully."
    except Exception as e:
        print(f"Failed to generate SSH key: {e}")
        return False, f"Failed to generate SSH key: {e}"


def execute_ssh_command(command: str) -> str:
    config = get_config()
    ip = config.get('System', 'ip_address', fallback='').strip()
    user = config.get('System', 'username', fallback='').strip()
    key_path = config.get('System', 'ssh_key_path', fallback='').strip()
    if not all([ip, user, key_path]): return "Error: System IP, Username, or SSH Key Path is missing."
    
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        pkey = paramiko.RSAKey.from_private_key_file(key_path)
        client.connect(hostname=ip, username=user, pkey=pkey, timeout=30)
        
        stdin, stdout, stderr = client.exec_command(command, timeout=600)
        exit_status = stdout.channel.recv_exit_status()
        
        output = stdout.read().decode('utf-8', 'ignore').strip()
        error_output = stderr.read().decode('utf-8', 'ignore').strip()
        client.close()

        full_output = ""
        if output:
            full_output += output
        if error_output:
            if full_output:
                full_output += "\n"
            full_output += error_output
        
        if exit_status != 0:
            return f"Error (Exit Code {exit_status}): {full_output if full_output else 'Command failed with no output.'}"
        else:
            return full_output if full_output else "Success: Command executed with no output."

    except Exception as e: 
        return f"An SSH function exception occurred: {type(e).__name__} - {str(e)}"


def agent_task_runner(objective: str):
    global AGENT_HISTORY, LAST_SESSION, PERSISTENT_VM_OUTPUT, TASK_RUNNING, SYSTEM_OS_INFO, TASK_PAUSED, CURRENT_OBJECTIVE, USER_APPROVAL_EVENT, USER_RESPONSE, CURRENT_EXECUTION_MODE
    
    CURRENT_OBJECTIVE = objective

    try:
        config = get_config()
        PROVIDER, MODEL_NAME, MAX_STEPS = config['General']['provider'], config['Agent']['model_name'], int(config['Agent']['max_steps'])
        
        socketio.emit('agent_log', {'data': f"--- Starting Agent with {PROVIDER.capitalize()} ({MODEL_NAME}) ---\nObjective: {CURRENT_OBJECTIVE}"})
            
        if PROVIDER == 'ollama':
            llm = Ollama(model=MODEL_NAME, base_url=config['Ollama']['api_url'], timeout=120)
            prompt_template = config.get('OllamaPrompt', 'template', fallback='Prompt not found')
        elif PROVIDER == 'gemini':
            llm = ChatGoogleGenerativeAI(model=MODEL_NAME, google_api_key=config['General']['gemini_api_key'])
            prompt_template_str = config.get('GeminiPrompt', 'template', fallback='Prompt not found')
            prompt_template_obj = PromptTemplate.from_template(prompt_template_str)
        else:
            socketio.emit('agent_log', {'data': "LLM provider not configured."}); return

        for step_counter in range(1, MAX_STEPS + 1):
            if not TASK_RUNNING: break
            
            while TASK_PAUSED:
                if not TASK_RUNNING: return
                socketio.sleep(1)

            if PROVIDER == 'gemini':
                full_prompt = prompt_template_obj.format(objective=CURRENT_OBJECTIVE, history=AGENT_HISTORY, system_info=SYSTEM_OS_INFO)
            else:
                full_prompt = prompt_template.format(objective=CURRENT_OBJECTIVE, history=AGENT_HISTORY, system_info=SYSTEM_OS_INFO)

            retries = 0
            command_executed = False
            command, reason = "", ""
            while retries < 10:
                if not TASK_RUNNING: break
                step_log_message = f"\n--- STEP {step_counter}/{MAX_STEPS} --- (Attempt {retries + 1}/10)\nAgent is thinking..."
                socketio.emit('agent_log', {'data': step_log_message})
                if retries > 0: LAST_SESSION["log"] += step_log_message + '\n'

                try:
                    llm_response_obj = llm.invoke(full_prompt)
                    
                    if PROVIDER == 'gemini':
                        response_content = llm_response_obj.content
                        if isinstance(response_content, list):
                            llm_response = "\n".join([part['text'] for part in response_content if isinstance(part, dict) and 'text' in part])
                        else:
                            llm_response = str(response_content)
                    else:
                        llm_response = str(llm_response_obj)

                    LAST_SESSION["raw_llm_responses"].append(llm_response)
                    socketio.emit('update_raw_llm_responses', {'data': "\n\n".join([f"--- Response {i+1} ---\n{r}" for i, r in enumerate(LAST_SESSION["raw_llm_responses"])])})

                    report_match = re.search(r"REPORT:\s*(.*)", llm_response, re.DOTALL | re.IGNORECASE)
                    command_match = re.search(r"COMMAND:\s*(.*)", llm_response, re.DOTALL | re.IGNORECASE)
                    
                    if report_match:
                        final_report_text = report_match.group(1).strip()
                        report_log_message = f"REPORT: {final_report_text}"
                        socketio.emit('agent_log', {'data': report_log_message})
                        LAST_SESSION["log"] += report_log_message + '\n'
                        LAST_SESSION["final_report"] = final_report_text
                        socketio.emit('final_report', {'data': final_report_text})
                        AGENT_HISTORY += f"\n\nREPORT:\n{final_report_text}\n"
                        socketio.emit('update_history', {'data': AGENT_HISTORY})
                        save_current_session_to_disk()
                        return

                    if command_match:
                        command = command_match.group(1).strip().split('\n')[0]
                        reason_match = re.search(r"REASON:\s*(.*?)(?:COMMAND:|REPORT:|$)", llm_response, re.DOTALL | re.IGNORECASE)
                        reason = reason_match.group(1).strip() if reason_match else "(Reasoning not provided by LLM)"
                        command_executed = True
                        break
                    
                    raise ValueError("Invalid format received from model (No COMMAND or REPORT found).")

                except Exception as e:
                    retries += 1
                    error_log = f"Attempt {retries}/10 failed. Reason: {e}"
                    socketio.emit('agent_log', {'data': error_log})
                    time.sleep(2)
            
            if not TASK_RUNNING: break

            if not command_executed:
                final_error_msg = "Failed to get a valid command from the model after 10 attempts. Stopping execution."
                socketio.emit('agent_log', {'data': final_error_msg})
                LAST_SESSION["log"] += final_error_msg + '\n'
                LAST_SESSION["final_report"] = final_error_msg
                socketio.emit('final_report', {'data': final_error_msg})
                save_current_session_to_disk()
                return

            original_command_from_llm = command
            human_intervention_log_entry = None

            if CURRENT_EXECUTION_MODE == 'assisted':
                socketio.emit('agent_log', {'data': "\n--- Waiting for user approval... ---"})
                
                USER_APPROVAL_EVENT = Event()
                USER_RESPONSE.clear()
                
                socketio.emit('awaiting_command_approval', {'command': command, 'reason': reason})

                approval_timeout = 600 # 10 minutes
                try:
                    USER_APPROVAL_EVENT.wait(timeout=approval_timeout)
                except Timeout:
                    socketio.emit('agent_log', {'data': f"\n--- APPROVAL TIMEOUT ({approval_timeout}s) ---\nTask stopped due to inactivity."})
                    handle_stop_task()
                    return

                if not TASK_RUNNING: break

                if USER_RESPONSE.get('approved'):
                    command = USER_RESPONSE['command']
                    was_modified = (original_command_from_llm != command)

                    if was_modified:
                        modification_reason = USER_RESPONSE.get('modification_reason', 'no reason provided')
                        log_msg = f"--- Command modified and approved by user ---\nReason: {modification_reason}"
                        socketio.emit('agent_log', {'data': log_msg})
                        human_intervention_log_entry = (
                            f"Human Intervention: Command was modified by the administrator. Reason: {modification_reason}.\n"
                            f"New command executed: {command}"
                        )
                    else:
                        socketio.emit('agent_log', {'data': f"--- Command approved by user ---"})
                else: # Rejected
                    rejection_reason = USER_RESPONSE['reason']
                    socketio.emit('agent_log', {'data': f"--- Command rejected by user ---\nReason: {rejection_reason}"})
                    
                    AGENT_HISTORY += (
                        f"\n\n--- STEP {step_counter} ---\n\n"
                        f"REASON:\n{reason}\n\n"
                        f"COMMAND:\n{original_command_from_llm}\n\n"
                        f"Output:\nHuman Intervention: Command was rejected by the administrator. Reason: {rejection_reason}\n"
                    )
                    socketio.emit('update_history', {'data': AGENT_HISTORY})
                    save_current_session_to_disk()
                    continue 

            socketio.emit('agent_log', {'data': f"COMMAND: {command}"})
            LAST_SESSION["log"] += f"COMMAND: {command}\n"
            
            vm_msg = f"\n\n{config['System']['username']}@{config['System']['ip_address']}~# {command}"
            socketio.emit('vm_screen', {'data': vm_msg + '\n'}); PERSISTENT_VM_OUTPUT += vm_msg + '\n'
            
            result = execute_ssh_command(command)
            
            if not TASK_RUNNING: break

            socketio.emit('vm_screen', {'data': result + '\n'}); PERSISTENT_VM_OUTPUT += result + '\n'
            
            history_entry = f"\n\n--- STEP {step_counter} ---\n\nREASON:\n{reason}\n\n"
            if human_intervention_log_entry:
                history_entry += f"COMMAND (Original):\n{original_command_from_llm}\n\nOutput:\n{human_intervention_log_entry}\n\nOutput:\n{result}\n"
            else:
                history_entry += f"COMMAND:\n{command}\n\nOutput:\n{result}\n"

            AGENT_HISTORY += history_entry
            socketio.emit('update_history', {'data': AGENT_HISTORY})
            
            save_current_session_to_disk()
            time.sleep(1)

    except Exception as e:
        error_message = f"\n--- AGENT ERROR ---\nAn unexpected error occurred: {type(e).__name__} - {str(e)}\n"
        socketio.emit('agent_log', {'data': error_message})
        LAST_SESSION["log"] += error_message
        print(f"Agent task error: {e}")

    finally:
        TASK_RUNNING = False
        TASK_PAUSED = False
        socketio.emit('task_finished')

def is_agent_configured(config):
    provider = config.get('General', 'provider', fallback='')
    if provider == 'ollama': return config.get('Ollama', 'api_url', fallback='').strip() != ''
    elif provider == 'gemini': return config.get('General', 'gemini_api_key', fallback='').strip() != ''
    return False

@app.route('/get_connections')
def get_connections(): return jsonify(load_connections())

@app.route('/delete_connection', methods=['POST'])
def delete_connection():
    data, conns = request.get_json(), load_connections()
    if data.get('delete_all'): conns = []
    else: conns = [c for c in conns if not (c['ip'] == data.get('ip') and c['username'] == data.get('username'))]
    save_connections(conns); return jsonify({'status': 'success'})

@app.route('/')
def index():
    config = get_config()
    return render_template('index.html', config=config, agent_configured=is_agent_configured(config), ssh_status=SSH_CONNECTION_STATUS, llm_status=LLM_CONNECTION_STATUS)

@app.route('/history')
def history():
    config = get_config()
    raw_responses = "\n\n".join([f"--- Response {i+1} ---\n{r}" for i, r in enumerate(LAST_SESSION["raw_llm_responses"])])
    return render_template('history.html', config=config, agent_configured=is_agent_configured(config), ssh_status=SSH_CONNECTION_STATUS, llm_status=LLM_CONNECTION_STATUS, initial_history=AGENT_HISTORY, last_report=LAST_SESSION["final_report"], raw_llm_responses=raw_responses)

@app.route('/get_llm_models', methods=['POST'])
def handle_get_llm_models():
    try:
        provider = request.form.get('provider')
        is_ok, message, models = False, "Invalid provider.", []
        if provider == 'ollama':
            is_ok, message, models = check_ollama_connection(request.form.get('api_url', '').strip())
        elif provider == 'gemini':
            is_ok, message, models = check_gemini_connection(request.form.get('gemini_api_key', '').strip())
        if is_ok:
            return jsonify({'status': 'success', 'message': f'{len(models)} models found.', 'models': models})
        else:
            return jsonify({'status': 'error', 'message': message}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'An unexpected error occurred: {e}'}), 500

@app.route('/save_agent_settings', methods=['POST'])
def handle_save_agent_settings():
    try:
        config = get_config()
        config['General']['provider'] = request.form.get('provider')
        config['General']['gemini_api_key'] = request.form.get('gemini_api_key', '')
        config['Ollama']['api_url'] = request.form.get('api_url', '')
        config['Agent']['model_name'] = request.form.get('model_name', '').strip()
        config['Agent']['max_steps'] = request.form.get('max_steps', '30').strip()
        with open(CONFIG_FILE_PATH, 'w') as f: config.write(f)
        initialize_llm_status()
        return jsonify({'status': 'success', 'message': 'Agent settings saved!'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'An error occurred: {e}'}), 500

@app.route('/save_system_settings', methods=['POST'])
def save_system_settings():
    form_data = request.form.to_dict()
    socketio.start_background_task(system_save_task, form_data)
    return jsonify({'status': 'started', 'message': 'Deployment started.'})


def system_save_task(form_data):
    global AGENT_HISTORY, SYSTEM_OS_INFO, SSH_CONNECTION_STATUS
    
    private_key_path = os.path.join(KEYS_DIR, 'id_rsa')
    public_key_path = os.path.join(KEYS_DIR, 'id_rsa.pub')
    
    ip, user, pwd = form_data.get('ip_address'), form_data.get('username'), form_data.get('password')

    try:
        is_reachable, msg = check_host_availability(ip)
        socketio.emit('deploy_log', {'data': f"Pinging {ip}... {msg}\n"})
        if not is_reachable:
            SSH_CONNECTION_STATUS = {"status": "failure", "message": msg}
            socketio.emit('deploy_finished', {'status': 'failure', 'message': msg}); return
        
        config = get_config()
        config['System']['ssh_key_path'] = private_key_path
        config['System']['ip_address'], config['System']['username'] = ip, user
        with open(CONFIG_FILE_PATH, 'w') as f: config.write(f)

        if pwd:
            socketio.emit('deploy_log', {'data': "Password provided. Attempting to deploy public key...\n"})
            
            try:
                with open(public_key_path, 'r') as f: pub_key_content = f.read().strip()
            except FileNotFoundError:
                msg = "FATAL: Public key file not found. Please generate one first."
                socketio.emit('deploy_log', {'data': f"{msg}\n"})
                SSH_CONNECTION_STATUS = {"status": "failure", "message": "Public key missing."}
                socketio.emit('deploy_finished', {'status': 'failure', 'message': msg}); return

            def deploy_key(h, u, p, k):
                try:
                    client = paramiko.SSHClient()
                    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    client.connect(h, username=u, password=p, timeout=10)

                    is_windows = False
                    try:
                        _, stdout, _ = client.exec_command('uname', timeout=5)
                        if stdout.channel.recv_exit_status() == 0 and stdout.read():
                            is_windows = False
                        else:
                            is_windows = True
                    except Exception:
                        is_windows = True

                    socketio.emit('deploy_log', {'data': f"Target OS detected as {'Windows' if is_windows else 'UNIX-like (Linux/Solaris/macOS)'}.\n"})
                    
                    if is_windows:
                        socketio.emit('deploy_log', {'data': "Attempting deployment for both standard user and administrator paths...\n"})
                        
                        cmd_user = (
                            f"powershell -Command \""
                            f"$sshDir = Join-Path $env:USERPROFILE '.ssh'; "
                            f"if (-not (Test-Path $sshDir)) {{ New-Item -Path $sshDir -ItemType Directory }}; "
                            f"$authKeysFile = Join-Path $sshDir 'authorized_keys'; "
                            f"if (-not (Test-Path $authKeysFile) -or -not ((Get-Content $authKeysFile -ErrorAction SilentlyContinue) | Where-Object {{ $_ -eq '{k}' }})) {{ "
                            f"Add-Content -Path $authKeysFile -Value '{k}' "
                            f"}}"
                            f"\""
                        )
                        _, stdout_user, stderr_user = client.exec_command(cmd_user)
                        exit_status_user = stdout_user.channel.recv_exit_status()
                        error_user = stderr_user.read().decode()
                        socketio.emit('deploy_log', {'data': f"User path deployment exit code: {exit_status_user}. Errors: {'None' if not error_user else error_user}\n"})

                        cmd_admin = (
                            f"powershell -Command \""
                            f"$adminAuthFile = Join-Path $env:ProgramData 'ssh\\administrators_authorized_keys'; "
                            f"if (-not (Test-Path $adminAuthFile) -or -not ((Get-Content $adminAuthFile -ErrorAction SilentlyContinue) | Where-Object {{ $_ -eq '{k}' }})) {{ "
                            f"Add-Content -Path $adminAuthFile -Value '{k}' "
                            f"}}"
                            f"\""
                        )
                        _, stdout_admin, stderr_admin = client.exec_command(cmd_admin)
                        exit_status_admin = stdout_admin.channel.recv_exit_status()
                        error_admin = stderr_admin.read().decode()
                        socketio.emit('deploy_log', {'data': f"Admin path deployment exit code: {exit_status_admin}. Errors: {'None' if not error_admin else error_admin}\n"})

                        client.close()

                        if exit_status_user == 0 or exit_status_admin == 0:
                            return True, "Key deployed successfully to at least one standard location."
                        else:
                            return False, f"Key deployment failed on all paths. User path error: {error_user}. Admin path error: {error_admin}."

                    else:
                        cmd = (
                            f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
                            f"touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
                            f"grep -qF '{k}' ~/.ssh/authorized_keys || echo '{k}' >> ~/.ssh/authorized_keys"
                        )
                        _, stdout, stderr = client.exec_command(cmd)
                        exit_status = stdout.channel.recv_exit_status()
                        error_output = stderr.read().decode()
                        client.close()
                        return exit_status == 0, "Key deployed successfully." if exit_status == 0 else f"Key deployment failed: {error_output}"
                
                except Exception as e:
                    return False, f"Key deployment failed with exception: {e}"

            success, msg = deploy_key(ip, user, pwd, pub_key_content)
            socketio.emit('deploy_log', {'data': f"{msg}\n"})
            if not success:
                SSH_CONNECTION_STATUS = {"status": "failure", "message": msg}
                socketio.emit('deploy_finished', {'status': 'failure', 'message': msg}); return

        socketio.emit('deploy_log', {'data': "Verifying final SSH connection with key...\n"})
        is_ok, ssh_msg = check_ssh_connection()
        if not is_ok:
            SSH_CONNECTION_STATUS = {"status": "failure", "message": ssh_msg.split(':', 1)[-1].strip()}
            socketio.emit('deploy_finished', {'status': 'failure', 'message': ssh_msg}); return

        socketio.emit('deploy_log', {'data': "Connection successful. Retrieving OS info...\n"})
        os_info = execute_ssh_command("cat /etc/os-release || uname -a")
        SYSTEM_OS_INFO = "Could not retrieve OS info." if "Error" in os_info else os_info
        AGENT_HISTORY = f"--- System Discovery ---\nOutput:\n{SYSTEM_OS_INFO}"
        socketio.emit('update_history', {'data': AGENT_HISTORY})
        
        conns = load_connections(); new_conn = {'ip': ip, 'username': user}
        if new_conn not in conns: conns.append(new_conn); save_connections(conns)
        
        SSH_CONNECTION_STATUS = {"status": "success", "message": f"Connected to {user}@{ip}"}
        socketio.emit('deploy_finished', {'status': 'success', 'message': 'System configured successfully!'})
        save_current_session_to_disk()
    except Exception as e:
        error_message = f"An unexpected error occurred: {e}"
        SSH_CONNECTION_STATUS = {"status": "failure", "message": "An unexpected error occurred."}
        socketio.emit('deploy_finished', {'status': 'failure', 'message': error_message})

@app.route('/save_session')
def save_session():
    try:
        config = get_config()
        session_data = {
            'config': {s: dict(config.items(s)) for s in config.sections()},
            'history': AGENT_HISTORY,
            'os_info': SYSTEM_OS_INFO,
            'reasoning_log': LAST_SESSION.get('reasoning_log', '')
        }
        str_io = BytesIO(json.dumps(session_data, indent=4).encode('UTF-8'))
        return send_file(str_io, mimetype='application/json', as_attachment=True, download_name='agent_session.json')
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/load_session', methods=['POST'])
def load_session():
    global AGENT_HISTORY, SYSTEM_OS_INFO, LAST_SESSION, PERSISTENT_VM_OUTPUT, SSH_CONNECTION_STATUS
    try:
        file = request.files.get('session_file')
        if not file or file.filename == '': return jsonify({'status': 'error', 'message': 'No file selected'}), 400
        if file and file.filename.endswith('.json'):
            data = json.load(file)
            if not all(k in data for k in ['config', 'history', 'os_info']): raise ValueError("Invalid session file.")
            AGENT_HISTORY, SYSTEM_OS_INFO = data['history'], data['os_info']
            config = configparser.ConfigParser(); config.read_dict(data['config'])
            
            config['System']['ssh_key_path'] = os.path.join(KEYS_DIR, 'id_rsa')
            with open(CONFIG_FILE_PATH, 'w') as f: config.write(f)
            
            LAST_SESSION, PERSISTENT_VM_OUTPUT = {k: "" for k in LAST_SESSION}, ""
            LAST_SESSION['reasoning_log'] = data.get('reasoning_log', '')
            initialize_ssh_status()
            initialize_llm_status()
            return jsonify({'status': 'success', 'message': 'Session loaded.'})
        return jsonify({'status': 'error', 'message': 'Invalid file type.'}), 400
    except Exception as e: return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/generate_ssh_key', methods=['POST'])
def generate_ssh_key_route():
    private_key_path = os.path.join(KEYS_DIR, 'id_rsa')
    public_key_path = os.path.join(KEYS_DIR, 'id_rsa.pub')
    
    try:
        if os.path.exists(private_key_path): os.remove(private_key_path)
        if os.path.exists(public_key_path): os.remove(public_key_path)
    except OSError as e:
        return jsonify({'status': 'error', 'message': f"Error removing existing keys: {e}"}), 500

    success, message = generate_new_ssh_key()
    
    if success:
        try:
            with open(public_key_path, 'r') as f:
                public_key = f.read()
            return jsonify({'status': 'success', 'message': 'New SSH key pair generated.', 'public_key': public_key})
        except Exception as e:
             return jsonify({'status': 'error', 'message': str(e)}), 500
    else:
        return jsonify({'status': 'error', 'message': message}), 500


@app.route('/get_public_key')
def get_public_key():
    public_key_path = os.path.join(KEYS_DIR, 'id_rsa.pub')
    try:
        with open(public_key_path, 'r') as f: key_content = f.read()
        return jsonify({'status': 'success', 'public_key': key_content})
    except FileNotFoundError:
        return jsonify({'status': 'error', 'message': 'Public key not found. Please generate a new key pair.'}), 404
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/get_prompts')
def get_prompts():
    config = get_config()
    ollama_prompt = config.get('OllamaPrompt', 'template', fallback='')
    gemini_prompt = config.get('GeminiPrompt', 'template', fallback='')
    return jsonify({'ollama_prompt': ollama_prompt, 'gemini_prompt': gemini_prompt})

@app.route('/save_prompts', methods=['POST'])
def save_prompts():
    def validate_prompt(prompt_text):
        errors = []
        required_vars = {'objective', 'history', 'system_info'}
        matches = re.findall(r'\{(\w+)\}', prompt_text)
        found_vars = set(matches)
        missing_vars = required_vars - found_vars
        if missing_vars:
            errors.append(f"Missing variables: {', '.join(sorted(list(missing_vars)))}")

        required_keywords = ['COMMAND:', 'REPORT:']
        missing_keywords = [f"`{k}`" for k in required_keywords if k.lower() not in prompt_text.lower()]
        if missing_keywords: errors.append(f"Missing keywords: {', '.join(sorted(missing_keywords))}")

        return (False, ". ".join(errors)) if errors else (True, "Prompt is valid.")

    try:
        config = get_config()
        ollama_prompt = request.form.get('ollama_prompt')
        gemini_prompt = request.form.get('gemini_prompt')

        is_ollama_valid, ollama_msg = validate_prompt(ollama_prompt)
        if not is_ollama_valid: return jsonify({'status': 'error', 'message': f'Ollama Prompt Error: {ollama_msg}'}), 400

        is_gemini_valid, gemini_msg = validate_prompt(gemini_prompt)
        if not is_gemini_valid: return jsonify({'status': 'error', 'message': f'Gemini Prompt Error: {gemini_msg}'}), 400
            
        if not config.has_section('OllamaPrompt'): config.add_section('OllamaPrompt')
        config.set('OllamaPrompt', 'template', ollama_prompt)

        if not config.has_section('GeminiPrompt'): config.add_section('GeminiPrompt')
        config.set('GeminiPrompt', 'template', gemini_prompt)

        with open(CONFIG_FILE_PATH, 'w') as f: config.write(f)

        return jsonify({'status': 'success', 'message': 'Prompts saved successfully!'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'An unexpected error occurred: {e}'}), 500

@socketio.on('connect')
def handle_connect():
    socketio.emit('initial_state', {
        'agent_history': AGENT_HISTORY, 
        'vm_output': PERSISTENT_VM_OUTPUT, 
        'last_log': LAST_SESSION.get('log', ''), 
        'last_report': LAST_SESSION.get('final_report', ''), 
        'raw_llm_responses': "\n\n".join([f"--- Response {i+1} ---\n{r}" for i, r in enumerate(LAST_SESSION.get("raw_llm_responses", []))]), 
        'task_running': TASK_RUNNING,
        'task_paused': TASK_PAUSED,
        'reasoning_log': LAST_SESSION.get('reasoning_log', '')
    })

@socketio.on('execute_task')
def handle_execute_task(json_data):
    global LAST_SESSION, TASK_RUNNING, PERSISTENT_VM_OUTPUT, AGENT_HISTORY, TASK_PAUSED, CURRENT_OBJECTIVE, CURRENT_EXECUTION_MODE
    if TASK_RUNNING: return
    is_connected, msg = check_ssh_connection()
    if not is_connected: 
        socketio.emit('agent_log', {'data': f"--- PRE-FLIGHT CHECK FAILED ---\n{msg}"})
        socketio.emit('task_finished'); return
    
    objective = json_data.get('data')
    mode = json_data.get('mode', 'independent')
    CURRENT_EXECUTION_MODE = mode

    if not objective: 
        socketio.emit('agent_log', {'data': "Objective cannot be empty."}); return
    
    TASK_RUNNING = True
    TASK_PAUSED = False
    CURRENT_OBJECTIVE = objective
    LAST_SESSION["final_report"] = ""
    LAST_SESSION["raw_llm_responses"] = []
    line_separator = "=" * 50
    task_separator_msg = f"\n\n{line_separator}\n--- STARTING NEW TASK ---\nObjective: {objective}\nMode: {CURRENT_EXECUTION_MODE.capitalize()}\n{line_separator}\n\n"
    PERSISTENT_VM_OUTPUT += task_separator_msg
    LAST_SESSION["log"] += task_separator_msg
    socketio.emit('vm_screen', {'data': task_separator_msg})
    socketio.emit('agent_log', {'data': task_separator_msg})
    history_separator = f"\n{line_separator}\nOBJECTIVE:\n{objective}\n{line_separator}\n"
    AGENT_HISTORY += history_separator
    socketio.emit('update_history', {'data': AGENT_HISTORY})
    socketio.emit('task_started')
    socketio.emit('final_report', {'data': ""})
    socketio.start_background_task(agent_task_runner, objective)

@socketio.on('stop_task')
def handle_stop_task():
    global TASK_RUNNING, TASK_PAUSED, USER_APPROVAL_EVENT
    if TASK_RUNNING: 
        TASK_RUNNING = False
        TASK_PAUSED = False
        socketio.emit('agent_log', {'data': "\n--- STOP signal received. ---"})
        if USER_APPROVAL_EVENT and not USER_APPROVAL_EVENT.ready():
            USER_APPROVAL_EVENT.send(None)

@socketio.on('disconnect')
def handle_disconnect():
    print(f"A client disconnected. Task continues in background.")
    pass

@socketio.on('pause_task')
def handle_pause_task():
    global TASK_PAUSED
    if TASK_RUNNING and not TASK_PAUSED:
        TASK_PAUSED = True
        socketio.emit('agent_log', {'data': "\n--- TASK PAUSED ---\n"})
        socketio.emit('task_paused')

@socketio.on('resume_task')
def handle_resume_task(json_data):
    global TASK_PAUSED, CURRENT_OBJECTIVE, AGENT_HISTORY
    if TASK_RUNNING and TASK_PAUSED:
        new_objective = json_data.get('data')
        if new_objective and new_objective.strip() != CURRENT_OBJECTIVE:
            log_msg = f"\n--- OBJECTIVE UPDATED ---\nNew Objective: {new_objective}\n"
            AGENT_HISTORY += f"\n{'-'*20}\nHuman intervention: The objective has been updated.\nPrevious Objective: {CURRENT_OBJECTIVE}\nNew Objective: {new_objective}\n{'-'*20}\n"
            CURRENT_OBJECTIVE = new_objective.strip()
            socketio.emit('update_history', {'data': AGENT_HISTORY})
            socketio.emit('agent_log', {'data': log_msg})
            
        TASK_PAUSED = False
        socketio.emit('agent_log', {'data': "\n--- TASK RESUMED ---\n"})
        socketio.emit('task_resumed')

@socketio.on('reset_agent')
def handle_reset_agent(json_data):
    reset_all_memory()
    socketio.emit('update_history', {'data': AGENT_HISTORY}); socketio.emit('agent_log', {'data': "--- AGENT MEMORY RESET ---", 'clear': True})
    socketio.emit('vm_screen', {'data': "", 'clear': True}); socketio.emit('final_report', {'data': ""})

@socketio.on('submit_command_approval')
def handle_submit_command_approval(data):
    global USER_APPROVAL_EVENT, USER_RESPONSE
    if USER_APPROVAL_EVENT and not USER_APPROVAL_EVENT.ready():
        USER_RESPONSE = data
        USER_APPROVAL_EVENT.send(None)

@socketio.on('update_execution_mode')
def handle_update_execution_mode(data):
    global CURRENT_EXECUTION_MODE
    new_mode = data.get('mode')
    if new_mode in ['independent', 'assisted']:
        CURRENT_EXECUTION_MODE = new_mode
        socketio.emit('agent_log', {'data': f"\n--- Execution mode changed to: {new_mode.capitalize()} ---"})

def initialize_ssh_key_if_needed():
    private_key_path = os.path.join(KEYS_DIR, 'id_rsa')
    if not os.path.exists(private_key_path):
        print("SSH key not found. Generating a new one...")
        generate_new_ssh_key()
    else:
        print("Existing SSH key found.")

def initialize_ssh_status():
    global SSH_CONNECTION_STATUS
    config = get_config()
    ip = config.get('System', 'ip_address', fallback='').strip()
    user = config.get('System', 'username', fallback='').strip()
    if not ip or not user:
        SSH_CONNECTION_STATUS = {"status": "unconfigured", "message": "Not configured"}
        return
    is_ok, msg = check_ssh_connection()
    if is_ok:
        SSH_CONNECTION_STATUS = {"status": "success", "message": f"Connected to {user}@{ip}"}
    else:
        clean_msg = msg.split(':')[-1].strip()
        SSH_CONNECTION_STATUS = {"status": "failure", "message": f"Connection Failed: {clean_msg}"}

def initialize_llm_status():
    global LLM_CONNECTION_STATUS
    config = get_config()
    provider = config.get('General', 'provider', fallback='')
    model = config.get('Agent', 'model_name', fallback='')
    if not provider or not model:
        LLM_CONNECTION_STATUS = {"status": "unconfigured", "message": "Not configured"}
        return
    is_ok = False
    if provider == 'ollama':
        api_url = config.get('Ollama', 'api_url', fallback='')
        is_ok, _, _ = check_ollama_connection(api_url)
    elif provider == 'gemini':
        api_key = config.get('General', 'gemini_api_key', fallback='')
        is_ok, _, _ = check_gemini_connection(api_key)
    if is_ok:
        LLM_CONNECTION_STATUS = {"status": "success", "message": f"Provider: {provider.capitalize()}, Model: {model}"}
    else:
        LLM_CONNECTION_STATUS = {"status": "failure", "message": f"Provider: {provider.capitalize()} - Connection Failed"}

# --- APPLICATION STARTUP ---
initialize_ssh_key_if_needed()
load_session_from_disk()
initialize_ssh_status()
initialize_llm_status()

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000)

