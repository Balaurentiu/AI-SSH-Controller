# --- CORECTIE: Monkey patching pentru compatibilitate Gunicorn/Eventlet ---
import eventlet
eventlet.monkey_patch()

# --- Importuri Python Standard & Pachete ---
import os
import re
import zipfile
import json
import traceback
from io import BytesIO, StringIO
from datetime import datetime
from functools import partial

# --- Importuri Flask & SocketIO ---
from flask import Flask, render_template, request, jsonify, send_file, Response
from flask_socketio import SocketIO
from eventlet.event import Event

# --- Importurile Noilor Module Refactorizate ---
from config import (
    get_config, KEYS_DIR, CONFIG_FILE_PATH,
    SESSION_FILE_PATH, CONNECTIONS_FILE_PATH, EXECUTION_LOG_FILE_PATH,
    APP_DIR
)
import ssh_utils
import llm_utils
import session_manager
import agent_core

# --- Importuri LangChain (Added for Search Summarization) ---
from langchain_community.llms import Ollama
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import PromptTemplate

# --- Initializam Aplicatia si WebSocket-ul ---
app = Flask(__name__, template_folder='templates')
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")

# ---
# --- STAREA GLOBALA A APLICATIEI ---
# ---
# Folosim un singur dictionar partajat pentru a mentine starea.
# Acesta va fi trimis prin referinta catre thread-ul agentului
# pentru a asigura sincronizarea datelor in timp real.
GLOBAL_STATE = {
    "agent_history": "No commands have been executed yet.",
    "system_os_info": "Unknown. The first step should be to determine the OS.",
    "persistent_vm_output": "",
    "full_history_backups": [],
    "last_session": {
        "log": "Application started. Ready for task.",
        "vm_output": "", # Acesta nu mai este folosit, 'persistent_vm_output' a preluat rolul
        "final_report": "",
        "raw_llm_responses": []
    },
    # Informatii executie curenta (folosite de thread-ul agentului)
    "current_objective": "", # Obiectivul curent al task-ului
    "current_execution_mode": "independent", # "independent" sau "assisted"
    "current_summarization_mode": "automatic", # "automatic" sau "assisted"
    "current_allow_ask_mode": False, # True sau False
    "validator_enabled": True, # NEW: Master switch for the LLM Validator
    "system_username": "", # Numele utilizatorului sistemului tinta
    "system_ip": "", # IP-ul sistemului tinta
    "sudo_available": False, # IMPROVEMENT: Detectat automat - True daca sudo fara parola este disponibil
    # Status-uri
    "task_running": False,
    "task_paused": False,
    "human_search_pending": False,  # Flag to pause agent execution during human-initiated search
    "ssh_connection_status": {"status": "unknown", "message": "Not tested yet."},
    "llm_connection_status": {"status": "unknown", "message": "Not tested yet."}
}

# --- Flag-uri si Evenimente de Control pentru Thread-ul Agentului ---
CONTROL_FLAGS = {
    "is_running": lambda: GLOBAL_STATE['task_running'],
    "is_paused": lambda: GLOBAL_STATE['task_paused'],
    "set_running": lambda val: setattr_safe('task_running', val),
    "set_paused": lambda val: setattr_safe('task_paused', val)
}

# --- Evenimente pentru Comunicare Thread-UI ---
USER_APPROVAL_EVENT = Event()
USER_RESPONSE = {}
SUMMARIZATION_EVENT = Event()
USER_ANSWER_EVENT = Event()
USER_ANSWER = {}

EVENT_OBJECTS = {
    "user_approval_event": USER_APPROVAL_EVENT,
    "user_response": USER_RESPONSE,
    "summarization_event": SUMMARIZATION_EVENT,
    "user_answer_event": USER_ANSWER_EVENT,
    "user_answer": USER_ANSWER
}

# --- Functie helper pentru setari sigure ---
def setattr_safe(key, val):
    """Seteaza o valoare in GLOBAL_STATE in mod thread-safe."""
    GLOBAL_STATE[key] = val

# ---
# --- Functii Helper ---
# ---

def save_app_state():
    """Salveaza starea aplicatiei pe disc."""
    session_manager.save_current_session_to_disk(GLOBAL_STATE, SESSION_FILE_PATH, EXECUTION_LOG_FILE_PATH)

def load_app_state():
    """Incarca starea aplicatiei de pe disc."""
    global GLOBAL_STATE
    loaded_state = session_manager.load_session_from_disk(SESSION_FILE_PATH, EXECUTION_LOG_FILE_PATH)
    GLOBAL_STATE.update(loaded_state)

def perform_unified_search(query: str, reason: str = "General inquiry", summarize: bool = True) -> dict:
    """
    Unified search function used by both LLM (SRCH:) and human (web interface).
    Now accepts 'reason' for better context summarization.
    """
    log_manager = GLOBAL_STATE.get('log_manager')
    if not log_manager:
        return {
            'query': query,
            'results_raw': 'Log manager not initialized',
            'results_summarized': 'Log manager not initialized',
            'was_summarized': False,
            'size': 0
        }

    # Perform search
    search_results = log_manager.search_past_context(query, limit=50)
    size = len(search_results)

    # Check if summarization needed (>10% of threshold)
    cfg = get_config()
    summarization_threshold = cfg.getint('Agent', 'summarization_threshold', fallback=15000)
    threshold_10_percent = int(summarization_threshold * 0.1)

    results_summarized = search_results
    was_summarized = False

    if summarize and size > threshold_10_percent:
        try:
            provider = cfg.get('General', 'provider', fallback='')
            model_name = cfg.get('Agent', 'model_name', fallback='')

            if not provider or not model_name:
                 results_summarized = search_results[:threshold_10_percent] + "...[truncated]"
            else:
                # Initialize LLM for summarization
                if provider == 'ollama':
                    api_url = cfg.get('Ollama', 'api_url', fallback='')
                    llm = Ollama(model=model_name, base_url=api_url, timeout=60)
                elif provider == 'gemini':
                    api_key = cfg.get('General', 'gemini_api_key', fallback='')
                    llm = ChatGoogleGenerativeAI(model=model_name, google_api_key=api_key, generation_config={"temperature": 0.5})
                elif provider == 'anthropic':
                    api_key = cfg.get('General', 'anthropic_api_key', fallback='')
                    llm = ChatAnthropic(model=model_name, api_key=api_key, temperature=0.5)

                # --- UPDATED PROMPT LOGIC ---
                # Try to get the specific Search prompt first, fallback to generic Summarize
                prompt_key = 'OllamaSearchSummaryPrompt' if provider == 'ollama' else 'CloudSearchSummaryPrompt'
                fallback_key = 'OllamaSummarizePrompt' if provider == 'ollama' else 'CloudSummarizePrompt'

                # We fallback to the generic one if the search one isn't defined/customized yet
                prompt_template_str = cfg.get(prompt_key, 'template', fallback=cfg.get(fallback_key, 'template', fallback="Analyze: {results}"))

                current_objective = GLOBAL_STATE.get('current_objective', 'No objective set.')

                # Dynamic Input Capping (Safety)
                input_safety_limit = int(summarization_threshold * 1.5)
                safe_search_results = search_results
                if len(search_results) > input_safety_limit:
                    print(f"Search results too large ({len(search_results)} chars). Truncating to {input_safety_limit} chars before summarization.")
                    safe_search_results = search_results[:input_safety_limit] + "\n... [Remaining results omitted for summarization context safety] ..."

                # Inject variables safely (check what the template expects)
                format_args = {}

                # Handle variable name mismatches between templates (results vs history)
                if '{results}' in prompt_template_str:
                    format_args['results'] = safe_search_results
                elif '{history}' in prompt_template_str:
                    format_args['history'] = safe_search_results # Fallback for legacy templates
                else:
                    # If template has neither, append it manually (worst case)
                    prompt_template_str += "\nResults:\n{results}"
                    format_args['results'] = safe_search_results

                if '{objective}' in prompt_template_str:
                    format_args['objective'] = current_objective
                if '{reason}' in prompt_template_str:
                    format_args['reason'] = reason # Inject the reason if the template supports it

                prompt = PromptTemplate.from_template(prompt_template_str).format(**format_args)

                # Call LLM
                summarized_result = llm.invoke(prompt)
                # Both Gemini and Anthropic use .content attribute
                if provider in ['gemini', 'anthropic'] and hasattr(summarized_result, 'content'):
                    results_summarized = summarized_result.content
                else:
                    results_summarized = str(summarized_result)

                was_summarized = True

        except Exception as e:
            print(f"Error summarizing search results ({type(e).__name__}): {e}")
            traceback.print_exc()
            fallback_size = min(len(search_results), 5000)
            results_summarized = search_results[:fallback_size] + f"\n\n[System: Search results were too complex to summarize (Error: {type(e).__name__} - {e}). Showing first {fallback_size} characters.]"
            was_summarized = False

    return {
        'query': query,
        'results_raw': search_results,
        'results_summarized': results_summarized,
        'was_summarized': was_summarized,
        'size': size
    }

# --- CORECTIE: Definim functia locala ce foloseste ssh_utils ---
def get_public_key_content(force_generate=False):
    """Wrapper local care apeleaza functia din ssh_utils."""
    return ssh_utils.get_public_key_content(force_generate=force_generate)

def initialize_log_system():
    """Initialize the unified log manager system."""
    global GLOBAL_STATE
    try:
        print("Initializing log system...")
        from log_manager import UnifiedLogManager
        log_manager = UnifiedLogManager()
        GLOBAL_STATE['log_manager'] = log_manager
        print("Log system initialized successfully.")
    except Exception as e:
        print(f"Error initializing log system: {e}")
        traceback.print_exc()

def initialize_ssh_status():
    """Actualizeaza statusul conexiunii SSH in GLOBAL_STATE."""
    global GLOBAL_STATE
    
    # Citim configuratia
    cfg = get_config()
    ip = cfg.get('System', 'ip_address', fallback='').strip()
    username = cfg.get('System', 'username', fallback='').strip()
    
    if not ip or not username:
        GLOBAL_STATE['ssh_connection_status'] = {
            "status": "failure",
            "message": "System IP or Username not configured."
        }
        return
    
    # Verificam conectivitatea
    is_reachable, ping_msg = ssh_utils.check_host_availability(ip)
    if not is_reachable:
        GLOBAL_STATE['ssh_connection_status'] = {
            "status": "failure",
            "message": f"Cannot reach {ip}."
        }
        return
    
    # Verificam conexiunea SSH
    is_connected, ssh_msg = ssh_utils.check_ssh_connection()
    if is_connected:
        GLOBAL_STATE['ssh_connection_status'] = {
            "status": "success",
            "message": f"Connected to {username}@{ip}."
        }
    else:
        GLOBAL_STATE['ssh_connection_status'] = {
            "status": "failure",
            "message": f"SSH failed: {ssh_msg}"
        }

def initialize_llm_status():
    """Actualizeaza statusul conexiunii LLM in GLOBAL_STATE."""
    global GLOBAL_STATE
    cfg = get_config()
    provider = cfg.get('General', 'provider', fallback='').strip()
    model_name = cfg.get('Agent', 'model_name', fallback='').strip()
    
    if not provider:
        GLOBAL_STATE['llm_connection_status'] = {
            "status": "failure",
            "message": "Provider not configured."
        }
        return
    
    if not model_name:
        GLOBAL_STATE['llm_connection_status'] = {
            "status": "failure",
            "message": "Model not selected."
        }
        return
    
    # Verificam conexiunea in functie de provider
    if provider == 'ollama':
        api_url = cfg.get('Ollama', 'api_url', fallback='').strip()
        if not api_url:
            GLOBAL_STATE['llm_connection_status'] = {
                "status": "failure",
                "message": "Ollama URL not configured."
            }
            return
            
        is_connected, msg, models = llm_utils.check_ollama_connection(api_url)
        if is_connected:
            if model_name in models:
                GLOBAL_STATE['llm_connection_status'] = {
                    "status": "success",
                    "message": f"Ollama: {model_name} ready."
                }
            else:
                GLOBAL_STATE['llm_connection_status'] = {
                    "status": "failure",
                    "message": f"Model {model_name} not found in Ollama."
                }
        else:
            GLOBAL_STATE['llm_connection_status'] = {
                "status": "failure",
                "message": msg
            }
    
    elif provider == 'gemini':
        api_key = cfg.get('General', 'gemini_api_key', fallback='').strip()
        if not api_key:
            GLOBAL_STATE['llm_connection_status'] = {
                "status": "failure",
                "message": "Gemini API Key not configured."
            }
            return

        is_connected, msg, models = llm_utils.check_gemini_connection(api_key)
        if is_connected:
            # Pentru Gemini, verificam daca modelul exista
            if any(model_name in m for m in models):
                GLOBAL_STATE['llm_connection_status'] = {
                    "status": "success",
                    "message": f"Gemini: {model_name} ready."
                }
            else:
                GLOBAL_STATE['llm_connection_status'] = {
                    "status": "failure",
                    "message": f"Model {model_name} not available."
                }
        else:
            GLOBAL_STATE['llm_connection_status'] = {
                "status": "failure",
                "message": msg
            }

    elif provider == 'anthropic':
        api_key = cfg.get('General', 'anthropic_api_key', fallback='').strip()
        if not api_key:
            GLOBAL_STATE['llm_connection_status'] = {
                "status": "failure",
                "message": "Anthropic API Key not configured."
            }
            return

        is_connected, msg, models = llm_utils.check_anthropic_connection(api_key)
        if is_connected:
            # Pentru Anthropic, verificam daca modelul exista
            if model_name in models:
                GLOBAL_STATE['llm_connection_status'] = {
                    "status": "success",
                    "message": f"Anthropic: {model_name} ready."
                }
            else:
                GLOBAL_STATE['llm_connection_status'] = {
                    "status": "failure",
                    "message": f"Model {model_name} not available."
                }
        else:
            GLOBAL_STATE['llm_connection_status'] = {
                "status": "failure",
                "message": msg
            }

    else:
        GLOBAL_STATE['llm_connection_status'] = {
            "status": "failure",
            "message": f"Unknown provider: {provider}"
        }

    # Initialize Chat LLM (separate from execution LLM)
    use_separate_chat_llm = cfg.getboolean('ChatLLM', 'enabled', fallback=False)
    print(f"[CHAT LLM INIT] ChatLLM.enabled = {use_separate_chat_llm}", flush=True)

    if use_separate_chat_llm:
        print("[CHAT LLM INIT] Initializing separate Chat LLM...", flush=True)
        chat_provider = cfg.get('ChatLLM', 'provider', fallback='ollama')
        chat_model = cfg.get('ChatLLM', 'model_name', fallback='')
        chat_api_key = cfg.get('ChatLLM', 'api_key', fallback='')

        print(f"[CHAT LLM INIT] Provider: {chat_provider}, Model: {chat_model}", flush=True)

        # Always read Ollama URL from main config to avoid Docker localhost issues
        ollama_url = cfg.get('Ollama', 'api_url', fallback='http://localhost:11434')

        try:
            # Initialize chat LLM
            if chat_provider == 'ollama':
                from langchain_community.llms import Ollama
                print(f"[CHAT LLM INIT] Creating Ollama instance: model={chat_model}, url={ollama_url}", flush=True)
                GLOBAL_STATE['chat_llm'] = Ollama(model=chat_model, base_url=ollama_url, timeout=120)
                print(f"[CHAT LLM INIT] ✓ Chat LLM initialized: Ollama ({chat_model}) on {ollama_url}", flush=True)
            elif chat_provider == 'gemini':
                from langchain_google_genai import ChatGoogleGenerativeAI
                print(f"[CHAT LLM INIT] Creating Gemini instance: model={chat_model}", flush=True)
                GLOBAL_STATE['chat_llm'] = ChatGoogleGenerativeAI(
                    model=chat_model,
                    google_api_key=chat_api_key,
                    generation_config={"temperature": 0.6},
                    convert_system_message_to_human=True
                )
                print(f"[CHAT LLM INIT] ✓ Chat LLM initialized: Gemini ({chat_model})", flush=True)
            elif chat_provider == 'anthropic':
                from langchain_anthropic import ChatAnthropic
                print(f"[CHAT LLM INIT] Creating Anthropic instance: model={chat_model}", flush=True)
                GLOBAL_STATE['chat_llm'] = ChatAnthropic(
                    model=chat_model,
                    api_key=chat_api_key,
                    temperature=0.6
                )
                print(f"[CHAT LLM INIT] ✓ Chat LLM initialized: Anthropic ({chat_model})", flush=True)
            else:
                print(f"[CHAT LLM INIT] ✗ Unknown chat provider: {chat_provider}. Using shared LLM for chat.", flush=True)
                GLOBAL_STATE['chat_llm'] = None  # Will fallback to main LLM
        except Exception as e:
            print(f"[CHAT LLM INIT] ✗ Error initializing separate Chat LLM: {e}. Using shared LLM for chat.", flush=True)
            traceback.print_exc()
            GLOBAL_STATE['chat_llm'] = None
    else:
        print("[CHAT LLM INIT] Using shared LLM for both execution and chat.", flush=True)
        GLOBAL_STATE['chat_llm'] = None  # Will fallback to main LLM in process_chat_message

# ---
# --- Rute Flask (Pagini Principale) ---
# ---

@app.route('/')
def index():
    """Pagina principala - Live Control."""
    return render_template('index.html',
                         ssh_status=GLOBAL_STATE['ssh_connection_status'],
                         llm_status=GLOBAL_STATE['llm_connection_status'])

@app.route('/history')
def history():
    """Pagina History & Reports."""
    return render_template('history.html',
                         ssh_status=GLOBAL_STATE['ssh_connection_status'],
                         llm_status=GLOBAL_STATE['llm_connection_status'])

# ---
# --- Rute Flask (API Endpoints) ---
# ---

@app.route('/get_agent_config')
def get_agent_config():
    """Returneaza configuratia agentului."""
    cfg = get_config()

    # Load Chat LLM configuration if exists
    chat_llm_config = {
        'enabled': cfg.getboolean('ChatLLM', 'enabled', fallback=False),
        'provider': cfg.get('ChatLLM', 'provider', fallback='ollama'),
        'model_name': cfg.get('ChatLLM', 'model_name', fallback=''),
        'api_key': cfg.get('ChatLLM', 'api_key', fallback='')
    }

    return jsonify({
        'provider': cfg.get('General', 'provider', fallback='ollama'),
        'model_name': cfg.get('Agent', 'model_name', fallback=''),
        'gemini_api_key': cfg.get('General', 'gemini_api_key', fallback=''),
        'anthropic_api_key': cfg.get('General', 'anthropic_api_key', fallback=''),
        'ollama_api_url': cfg.get('Ollama', 'api_url', fallback='http://localhost:11434'),
        'max_steps': cfg.getint('Agent', 'max_steps', fallback=50),
        'summarization_threshold': cfg.getint('Agent', 'summarization_threshold', fallback=15000),
        'llm_timeout': cfg.getint('Agent', 'llm_timeout', fallback=120),
        'chat_history_message_count': cfg.getint('Agent', 'chat_history_message_count', fallback=20),
        'chat_llm': chat_llm_config
    })

@app.route('/save_agent_config', methods=['POST'])
def save_agent_config():
    """Salveaza configuratia agentului."""
    try:
        data = request.json
        cfg = get_config()

        cfg.set('General', 'provider', data['provider'])
        cfg.set('General', 'gemini_api_key', data.get('gemini_api_key', ''))
        cfg.set('General', 'anthropic_api_key', data.get('anthropic_api_key', ''))
        cfg.set('Agent', 'model_name', data['model_name'])
        cfg.set('Agent', 'max_steps', str(data['max_steps']))
        cfg.set('Agent', 'summarization_threshold', str(data['summarization_threshold']))
        cfg.set('Agent', 'llm_timeout', str(data.get('llm_timeout', 120)))
        cfg.set('Agent', 'chat_history_message_count', str(data.get('chat_history_message_count', 20)))
        cfg.set('Ollama', 'api_url', data.get('ollama_api_url', ''))

        # Save Chat LLM Configuration
        if 'chat_llm' in data:
            if not cfg.has_section('ChatLLM'):
                cfg.add_section('ChatLLM')
            chat_llm = data['chat_llm']
            cfg.set('ChatLLM', 'enabled', str(chat_llm.get('enabled', False)))
            cfg.set('ChatLLM', 'provider', chat_llm.get('provider', 'ollama'))
            cfg.set('ChatLLM', 'model_name', chat_llm.get('model_name', ''))
            cfg.set('ChatLLM', 'api_key', chat_llm.get('api_key', ''))

        with open(CONFIG_FILE_PATH, 'w') as f:
            cfg.write(f)

        # Re-testam conexiunea
        initialize_llm_status()
        socketio.emit('llm_status_update', GLOBAL_STATE['llm_connection_status'])

        return jsonify({'status': 'success', 'message': 'Configuration saved!'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/test_ollama', methods=['POST'])
def test_ollama():
    """Testeaza conexiunea la Ollama."""
    try:
        api_url = request.json['api_url']
        is_connected, msg, models = llm_utils.check_ollama_connection(api_url)
        if is_connected:
            return jsonify({'status': 'success', 'message': msg, 'models': models})
        else:
            return jsonify({'status': 'error', 'message': msg, 'models': []})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e), 'models': []}), 500

@app.route('/test_gemini', methods=['POST'])
def test_gemini():
    """Testeaza API Key-ul Gemini."""
    try:
        api_key = request.json['api_key']
        is_connected, msg, models = llm_utils.check_gemini_connection(api_key)
        if is_connected:
            return jsonify({'status': 'success', 'message': msg, 'models': models})
        else:
            return jsonify({'status': 'error', 'message': msg, 'models': []})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e), 'models': []}), 500

@app.route('/test_anthropic', methods=['POST'])
def test_anthropic():
    """Testeaza API Key-ul Anthropic."""
    try:
        api_key = request.json['api_key']
        is_connected, msg, models = llm_utils.check_anthropic_connection(api_key)
        if is_connected:
            return jsonify({'status': 'success', 'message': msg, 'models': models})
        else:
            return jsonify({'status': 'error', 'message': msg, 'models': []})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e), 'models': []}), 500

@app.route('/get_models', methods=['POST'])
def get_models_route():
    """Fetches available models for the specified provider using API key from UI."""
    try:
        data = request.json
        provider = data.get('provider')
        api_key_ui = data.get('api_key')  # Key from UI input

        cfg = get_config()
        models = []

        if provider == 'ollama':
            # Use URL from main config
            url = cfg.get('Ollama', 'api_url', fallback='http://localhost:11434')
            success, msg, models = llm_utils.check_ollama_connection(url)

        elif provider == 'gemini':
            # Use UI key if present, else saved key
            key = api_key_ui if api_key_ui else cfg.get('General', 'gemini_api_key', fallback='')
            if not key:
                return jsonify({'status': 'error', 'message': 'Missing API Key'})
            success, msg, models = llm_utils.check_gemini_connection(key)

        elif provider == 'anthropic':
            # Use UI key if present, else saved key
            key = api_key_ui if api_key_ui else cfg.get('General', 'anthropic_api_key', fallback='')
            if not key:
                return jsonify({'status': 'error', 'message': 'Missing API Key'})
            success, msg, models = llm_utils.check_anthropic_connection(key)

        else:
            return jsonify({'status': 'error', 'message': 'Unknown provider'})

        return jsonify({
            'status': 'success' if success else 'error',
            'models': models,
            'message': msg
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/get_system_config')
def get_system_config():
    """Returneaza configuratia sistemului tinta si conexiunile salvate."""
    cfg = get_config()
    connections = session_manager.load_connections()
    return jsonify({
        'ip_address': cfg.get('System', 'ip_address', fallback=''),
        'username': cfg.get('System', 'username', fallback=''),
        'ssh_port': cfg.getint('System', 'ssh_port', fallback=22),
        'ssh_key_path': cfg.get('System', 'ssh_key_path', fallback='/app/keys/id_rsa'),
        'saved_connections': connections
    })

@app.route('/save_system_config', methods=['POST'])
def save_system_config():
    """Salveaza configuratia sistemului si testeaza conexiunea."""
    try:
        data = request.json
        cfg = get_config()
        
        ip = data['ip_address'].strip()
        username = data['username'].strip()
        ssh_port = data.get('ssh_port', 22)
        ssh_key_path = data.get('ssh_key_path', '/app/keys/id_rsa').strip()

        if not ip or not username:
            return jsonify({'status': 'error', 'message': 'IP and Username are required.'}), 400

        # Salvam in config.ini
        cfg.set('System', 'ip_address', ip)
        cfg.set('System', 'username', username)
        cfg.set('System', 'ssh_port', str(ssh_port))
        cfg.set('System', 'ssh_key_path', ssh_key_path)
        
        with open(CONFIG_FILE_PATH, 'w') as f:
            cfg.write(f)
        
        # Actualizam GLOBAL_STATE
        GLOBAL_STATE['system_ip'] = ip
        GLOBAL_STATE['system_username'] = username
        
        # Salvam in connections.json (istoric)
        connections = session_manager.load_connections()
        
        # Verificam daca conexiunea exista deja
        existing = next((c for c in connections if c['ip'] == ip and c['username'] == username and c.get('port', 22) == ssh_port), None)
        if not existing:
            connections.append({
                'ip': ip,
                'username': username,
                'port': ssh_port,
                'ssh_key_path': ssh_key_path,
                'added_at': datetime.now().isoformat()
            })
            session_manager.save_connections(connections)
        
        # Testam conexiunea
        initialize_ssh_status()
        socketio.emit('ssh_status_update', GLOBAL_STATE['ssh_connection_status'])
        
        return jsonify({'status': 'success', 'message': 'Configuration saved and tested!'})
        
    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/delete_connection', methods=['POST'])
def delete_connection():
    """Sterge o conexiune salvata."""
    try:
        data = request.json
        connections = session_manager.load_connections()
        
        # Filtram conexiunea de sters
        connections = [c for c in connections if not (c['ip'] == data['ip'] and c['username'] == data['username'])]
        
        session_manager.save_connections(connections)
        return jsonify({'status': 'success'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/deploy_ssh_key', methods=['POST'])
def deploy_ssh_key():
    """Deploiaza cheia SSH pe sistemul tinta folosind parola."""
    try:
        data = request.json
        ip = data['ip']
        username = data['username']
        password = data['password']
        
        # Generam cheia daca nu exista
        ssh_utils.initialize_ssh_key_if_needed()
        
        # Citim continutul cheii publice
        pub_key = ssh_utils.get_public_key_content()
        if not pub_key:
            return jsonify({'status': 'error', 'message': 'Failed to read public key.'}), 500
        
        # Deploiem cheia
        success, msg = ssh_utils.deploy_ssh_key(ip, username, password, pub_key, socketio)
        
        if success:
            return jsonify({'status': 'success', 'message': msg})
        else:
            return jsonify({'status': 'error', 'message': msg}), 400
            
    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/get_llm_status')
def get_llm_status():
    """Returneaza statusul conexiunii LLM."""
    return jsonify(GLOBAL_STATE['llm_connection_status'])

@app.route('/get_ssh_status')
def get_ssh_status():
    """Returneaza statusul conexiunii SSH."""
    return jsonify(GLOBAL_STATE['ssh_connection_status'])

@app.route('/get_history_stats')
def get_history_stats():
    """Returneaza statistici despre istoricul agentului."""
    return jsonify({'char_count': len(GLOBAL_STATE['agent_history'])})

@app.route('/get_agent_execution_log')
def get_agent_execution_log():
    """Returns Full Log (immutable base log from execution_log.txt)."""
    try:
        log_manager = GLOBAL_STATE.get('log_manager')
        if log_manager:
            view_data = log_manager.get_full_log()
            return jsonify({'status': 'success', 'data': view_data})
        else:
            # Fallback to execution_log.txt if log_manager not available
            try:
                with open(EXECUTION_LOG_FILE_PATH, 'r', encoding='utf-8') as f:
                    return jsonify({'status': 'success', 'data': f.read()})
            except:
                return jsonify({'status': 'success', 'data': 'No execution log available yet. Start a task first.'})
    except Exception as e:
        print(f"Error getting agent execution log: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/get_execution_log_actions')
def get_execution_log_actions():
    """Returns Actions View (extracted from Full Log)."""
    try:
        log_manager = GLOBAL_STATE.get('log_manager')
        if log_manager:
            view_data = log_manager.get_actions_view()
            return jsonify({'status': 'success', 'data': view_data})
        else:
            return jsonify({'status': 'success', 'data': 'No log manager available. Start a task first.'})
    except Exception as e:
        print(f"Error getting actions view: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/get_execution_log_commands')
def get_execution_log_commands():
    """Returns Commands View (extracted from Full Log)."""
    try:
        log_manager = GLOBAL_STATE.get('log_manager')
        if log_manager:
            view_data = log_manager.get_commands_view()
            return jsonify({'status': 'success', 'data': view_data})
        else:
            return jsonify({'status': 'success', 'data': 'No log manager available. Start a task first.'})
    except Exception as e:
        print(f"Error getting commands view: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/get_vm_screen_log')
def get_vm_screen_log():
    """Returns VM Screen Log view (commands + output, terminal view)."""
    try:
        log_manager = GLOBAL_STATE.get('log_manager')
        if log_manager:
            view_data = log_manager.get_vm_screen_view()
            return jsonify({'status': 'success', 'data': view_data})
        else:
            # Fallback to old persistent_vm_output if log_manager not available
            return jsonify({'status': 'success', 'data': GLOBAL_STATE.get('persistent_vm_output', '')})
    except Exception as e:
        print(f"Error getting VM screen log: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/get_agent_memory_log')
def get_agent_memory_log():
    """Returns Agent Persistent Memory view (actual LLM context)."""
    try:
        log_manager = GLOBAL_STATE.get('log_manager')
        if log_manager:
            # Return the ACTUAL context that the LLM uses
            view_data = log_manager.get_llm_context()
            return jsonify({'status': 'success', 'data': view_data})
        else:
            # Fallback to old agent_history if log_manager not available
            return jsonify({'status': 'success', 'data': GLOBAL_STATE.get('agent_history', 'No data available.')})
            return jsonify({'status': 'success', 'data': GLOBAL_STATE.get('agent_history', fallback_msg)})
    except Exception as e:
        print(f"Error getting agent memory log: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/search_base_log')
def search_base_log():
    """Search the base log with context awareness."""
    try:
        query = request.args.get('q', '')
        reason = request.args.get('reason', 'User Manual Search') # Get reason from UI

        if not query:
            return jsonify({'status': 'error', 'message': 'Query parameter required'}), 400

        if GLOBAL_STATE.get('task_running', False):
            GLOBAL_STATE['human_search_pending'] = True
            # Notify UI that search is happening
            socketio.emit('agent_log', {'data': f"\n--- Manual Search: '{query}' (Reason: {reason}) ---"})

        # Pass reason to unified search
        search_result = perform_unified_search(query, reason=reason, summarize=True)

        return jsonify({
            'status': 'success',
            'query': search_result['query'],
            'data': search_result['results_summarized'],
            'raw_data': search_result['results_raw'],
            'was_summarized': search_result['was_summarized'],
            'size': search_result['size']
        })
    except Exception as e:
        print(f"Error searching base log: {e}")
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/add_search_to_context', methods=['POST'])
def add_search_to_context():
    """Add human-initiated search results to agent context and resume execution."""
    try:
        data = request.json
        search_query = data.get('query', '')
        search_reason = data.get('reason', 'User Manual Search') # Get reason
        search_results = data.get('results', '')
        was_summarized = data.get('was_summarized', False)

        if not search_query or not search_results:
            return jsonify({'status': 'error', 'message': 'Query and results required'}), 400

        # Add search results to agent_history with Reason FIRST
        history_entry = f"\n\n--- HUMAN SEARCH ---\nReason: {search_reason}\nQuery: {search_query}\n"

        if was_summarized:
            history_entry += "Results (summarized):\n"
        else:
            history_entry += "Results:\n"
        history_entry += f"{search_results}\n"

        # CRITICAL FIX: Write to persistent context file via log_manager
        log_manager = GLOBAL_STATE.get('log_manager')
        if log_manager:
            log_manager.append_to_llm_context(history_entry)
            # Sync global state from file
            GLOBAL_STATE['agent_history'] = log_manager.get_llm_context()
        else:
            # Fallback if log_manager missing (should not happen)
            GLOBAL_STATE['agent_history'] += history_entry

        # Emit update to UI
        socketio.emit('update_history', {'data': GLOBAL_STATE['agent_history']})

        # Clear human_search_pending flag
        GLOBAL_STATE['human_search_pending'] = False
        socketio.emit('search_completed', {'message': 'Search results added to context. Agent execution resuming.'})

        return jsonify({
            'status': 'success',
            'message': 'Search results added to agent context'
        })

    except Exception as e:
        print(f"Error adding search to context: {e}")
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/save_session')
def save_session():
    """
    Save current session as a ZIP containing ALL persistence files.
    """
    try:
        # Use the updated session_manager function which saves GLOBAL_STATE + all files
        zip_path = session_manager.save_session_state(GLOBAL_STATE)

        if not zip_path or not os.path.exists(zip_path):
            return jsonify({'status': 'error', 'message': 'Failed to create session ZIP'}), 500

        # Read the ZIP file into memory for download
        with open(zip_path, 'rb') as f:
            zip_data = f.read()

        # Cleanup the temporary file
        os.remove(zip_path)

        # Create BytesIO buffer for send_file
        zip_buffer = BytesIO(zip_data)
        zip_buffer.seek(0)

        filename = os.path.basename(zip_path)

        return send_file(
            zip_buffer,
            mimetype='application/zip',
            as_attachment=True,
            download_name=filename
        )

    except Exception as e:
        print(f"Error saving session: {e}")
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/load_session', methods=['POST'])
def load_session():
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file uploaded'})

    file = request.files['file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'No file selected'})

    if file:
        try:
            # Save uploaded zip temporarily
            temp_path = os.path.join(APP_DIR, 'temp_restore.zip')
            file.save(temp_path)

            # 1. Restore Files to Disk
            loaded_state_data = session_manager.load_session_state(temp_path)

            # 2. Update RAM (GLOBAL_STATE)
            if loaded_state_data:
                global GLOBAL_STATE

                # --- FIX: Protect log_manager from being overwritten by a string ---
                # The JSON save converts objects to strings. We must NOT overwrite
                # the live LogManager object with that string.
                if 'log_manager' in loaded_state_data:
                    del loaded_state_data['log_manager']
                # -----------------------------------------------------------------

                GLOBAL_STATE.update(loaded_state_data)
                print("Global State memory updated from session file.")

                # 3. Update Log Manager RAM
                # Ensure the object exists and is valid
                if 'log_manager' not in GLOBAL_STATE or not hasattr(GLOBAL_STATE['log_manager'], 'reload_state'):
                    print("Log Manager instance missing or invalid. Re-initializing...")
                    initialize_log_system()

                log_manager = GLOBAL_STATE.get('log_manager')
                if log_manager:
                    log_manager.reload_state()

                # Cleanup
                if os.path.exists(temp_path):
                    os.remove(temp_path)

                return jsonify({'status': 'success'})
            else:
                return jsonify({'status': 'error', 'message': 'Failed to parse session data'})

        except Exception as e:
            traceback.print_exc()
            return jsonify({'status': 'error', 'message': str(e)})

    return jsonify({'status': 'error', 'message': 'Unknown error'})

@app.route('/get_public_key')
def get_public_key():
    """Returneaza cheia publica, o genereaza daca este necesar."""
    # CORECTIE: Apelam functia locala cu force_generate=True
    key_content = get_public_key_content(force_generate=True)
    if "Error" in key_content:
        return jsonify({'status': 'error', 'message': key_content}), 500
    return jsonify({'status': 'success', 'public_key': key_content})

@app.route('/get_prompts')
def get_prompts():
    """Returneaza prompt-urile curente (Standard, Ask, or Chat)."""
    cfg = get_config()
    mode = request.args.get('mode', 'standard')

    data = {}

    if mode == 'chat':
        # Chat mode only has one prompt
        chat_prompt = cfg.get('ChatPrompt', 'template', fallback='')
        data['chat_prompt'] = chat_prompt
    else:
        # Standard or Ask logic
        ollama_section = 'OllamaPromptWithAsk' if mode == 'ask' else 'OllamaPrompt'
        cloud_section = 'CloudPromptWithAsk' if mode == 'ask' else 'CloudPrompt'
        data['ollama_prompt'] = cfg.get(ollama_section, 'template', fallback='')
        data['cloud_prompt'] = cfg.get(cloud_section, 'template', fallback='')

    return jsonify(data)

@app.route('/save_prompts', methods=['POST'])
def save_prompts():
    """Salveaza prompt-urile (Standard, Ask, or Chat)."""
    # Validation logic specific to mode
    def validate_prompt(prompt_text, mode):
        errors = []
        if mode == 'chat':
            # Relaxed validation for chat
            if '{user_message}' not in prompt_text:
                errors.append("Missing required variable: {user_message}")
        else:
            # Strict validation for execution modes
            required_vars = {'objective', 'history', 'system_info'}
            matches = re.findall(r'\{(\w+)\}', prompt_text)
            found_vars = set(matches)
            missing_vars = required_vars - found_vars
            if missing_vars:
                errors.append(f"Missing variables: {', '.join(sorted(list(missing_vars)))}")

            is_ask = (mode == 'ask')
            if is_ask and 'ask:' not in prompt_text.lower():
                errors.append("Missing keyword: ASK instructions.")

            if 'COMMAND:' not in prompt_text:
                errors.append("Missing keyword: COMMAND:")

        return (False, ". ".join(errors)) if errors else (True, "Valid.")

    try:
        cfg = get_config()
        mode = request.form.get('mode', 'standard')

        if mode == 'chat':
            chat_prompt = request.form.get('chat_prompt')
            is_valid, msg = validate_prompt(chat_prompt, 'chat')
            if not is_valid:
                return jsonify({'status': 'error', 'message': f'Chat Prompt Error: {msg}'}), 400

            if not cfg.has_section('ChatPrompt'): cfg.add_section('ChatPrompt')
            cfg.set('ChatPrompt', 'template', chat_prompt)

        else:
            # Standard/Ask Logic
            ollama_prompt = request.form.get('ollama_prompt')
            cloud_prompt = request.form.get('cloud_prompt')

            is_valid_o, msg_o = validate_prompt(ollama_prompt, mode)
            is_valid_c, msg_c = validate_prompt(cloud_prompt, mode)

            if not is_valid_o: return jsonify({'status': 'error', 'message': f'Ollama Error: {msg_o}'}), 400
            if not is_valid_c: return jsonify({'status': 'error', 'message': f'Cloud Error: {msg_c}'}), 400

            ollama_section = 'OllamaPromptWithAsk' if mode == 'ask' else 'OllamaPrompt'
            cloud_section = 'CloudPromptWithAsk' if mode == 'ask' else 'CloudPrompt'

            if not cfg.has_section(ollama_section): cfg.add_section(ollama_section)
            cfg.set(ollama_section, 'template', ollama_prompt)
            if not cfg.has_section(cloud_section): cfg.add_section(cloud_section)
            cfg.set(cloud_section, 'template', cloud_prompt)

        with open(CONFIG_FILE_PATH, 'w') as f:
            cfg.write(f)

        return jsonify({'status': 'success', 'message': 'Prompts saved!'})

    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': f'Error saving prompts: {e}'}), 500

@app.route('/get_summarization_prompt')
def get_summarization_prompt():
    """Returneaza prompt-urile de sumarizare (General si Step)."""
    cfg = get_config()

    # General History Summary
    def_hist = "Objective: {objective}\nSummarize: {history}"
    ollama_hist = cfg.get('OllamaSummarizePrompt', 'template', fallback=def_hist)
    cloud_hist = cfg.get('CloudSummarizePrompt', 'template', fallback=def_hist)

    # Step Output Summary
    def_step = "Summarize output: {output}"
    ollama_step = cfg.get('OllamaStepSummaryPrompt', 'template', fallback=def_step)
    cloud_step = cfg.get('CloudStepSummaryPrompt', 'template', fallback=def_step)

    # Search Results Summary
    def_search = "Analyze: {results}"
    ollama_search = cfg.get('OllamaSearchSummaryPrompt', 'template', fallback=def_search)
    cloud_search = cfg.get('CloudSearchSummaryPrompt', 'template', fallback=def_search)

    return jsonify({
        'ollama_summarize_prompt': ollama_hist,
        'cloud_summarize_prompt': cloud_hist,
        'ollama_step_prompt': ollama_step,
        'cloud_step_prompt': cloud_step,
        'ollama_search_prompt': ollama_search,
        'cloud_search_prompt': cloud_search
    })

@app.route('/save_summarization_prompt', methods=['POST'])
def save_summarization_prompt():
    """Salveaza prompt-urile de sumarizare (General si Step)."""

    def validate_template(text, required_var):
        if required_var not in text:
            return False, f"Missing required variable: {{{required_var}}}"
        return True, "Valid"

    try:
        cfg = get_config()

        # General History Prompts
        ollama_hist = request.form.get('ollama_summarize_prompt')
        cloud_hist = request.form.get('cloud_summarize_prompt')

        # Step Output Prompts
        ollama_step = request.form.get('ollama_step_prompt')
        cloud_step = request.form.get('cloud_step_prompt')

        # Search Results Prompts
        ollama_search = request.form.get('ollama_search_prompt')
        cloud_search = request.form.get('cloud_search_prompt')

        # Validation
        for p in [ollama_hist, cloud_hist]:
            ok, msg = validate_template(p, 'history')
            if not ok: return jsonify({'status': 'error', 'message': f'History Prompt Error: {msg}'}), 400

        for p in [ollama_step, cloud_step]:
            ok, msg = validate_template(p, 'output')
            if not ok: return jsonify({'status': 'error', 'message': f'Step Prompt Error: {msg}'}), 400

        for p in [ollama_search, cloud_search]:
            ok, msg = validate_template(p, 'results')
            if not ok: return jsonify({'status': 'error', 'message': f'Search Prompt Error: {msg}'}), 400

        # Save Sections
        if not cfg.has_section('OllamaSummarizePrompt'): cfg.add_section('OllamaSummarizePrompt')
        cfg.set('OllamaSummarizePrompt', 'template', ollama_hist)

        if not cfg.has_section('CloudSummarizePrompt'): cfg.add_section('CloudSummarizePrompt')
        cfg.set('CloudSummarizePrompt', 'template', cloud_hist)

        if not cfg.has_section('OllamaStepSummaryPrompt'): cfg.add_section('OllamaStepSummaryPrompt')
        cfg.set('OllamaStepSummaryPrompt', 'template', ollama_step)

        if not cfg.has_section('CloudStepSummaryPrompt'): cfg.add_section('CloudStepSummaryPrompt')
        cfg.set('CloudStepSummaryPrompt', 'template', cloud_step)

        if not cfg.has_section('OllamaSearchSummaryPrompt'): cfg.add_section('OllamaSearchSummaryPrompt')
        cfg.set('OllamaSearchSummaryPrompt', 'template', ollama_search)

        if not cfg.has_section('CloudSearchSummaryPrompt'): cfg.add_section('CloudSearchSummaryPrompt')
        cfg.set('CloudSearchSummaryPrompt', 'template', cloud_search)

        with open(CONFIG_FILE_PATH, 'w') as f:
            cfg.write(f)

        return jsonify({'status': 'success', 'message': 'All summarization templates saved!'})

    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': f'Error saving prompts: {e}'}), 500

# NEW: Validator prompt endpoints
@app.route('/get_validator_prompt')
def get_validator_prompt():
    """Returneaza prompt-urile de validare."""
    cfg = get_config()
    default_prompt = """You are a security validator for Linux/Windows commands.
Your task is to check if the following command is safe to execute on a remote system.

System: {system_info}
Sudo Available: {sudo_available}
Command: {command}
Reason: {reason}
Summarization Threshold: {summarization_threshold} chars
Command Timeout: {command_timeout} seconds

Rules:
- Check OS compatibility (Windows vs Linux commands)
- REJECT if the command could damage the system or data (e.g., rm -rf /, format c:, del /f /s /q)
- REJECT if the command downloads and executes unknown scripts
- REJECT if the command modifies system-critical files without clear reason
- REJECT if output likely exceeds the summarization threshold
- REJECT if command execution likely exceeds the timeout (e.g., long database operations, large file transfers)
- APPROVE if the command is for system information gathering (uname, systeminfo, etc.)
- APPROVE if the command is for reading files or listing directories
- APPROVE if the command is for non-destructive network operations

Respond with EXACTLY one of the following:
- APPROVE (if safe)
- REJECT REASON: <your reason> (if unsafe)

Your response:"""
    
    ollama_prompt = cfg.get('OllamaValidatePrompt', 'template', fallback=default_prompt)
    cloud_prompt = cfg.get('CloudValidatePrompt', 'template', fallback=default_prompt)
    return jsonify({'ollama_validator_prompt': ollama_prompt, 'cloud_validator_prompt': cloud_prompt})

@app.route('/save_validator_prompt', methods=['POST'])
def save_validator_prompt():
    """Salveaza prompt-urile de validare in config.ini."""
    def validate_validator_prompt(prompt_text):
        errors = []
        # Check for required variable
        if '{command}' not in prompt_text:
            errors.append("Missing required variable: {command}")
        # Check for required keywords
        if 'APPROVE' not in prompt_text.upper():
            errors.append("Missing keyword: APPROVE")
        if 'REJECT' not in prompt_text.upper():
            errors.append("Missing keyword: REJECT")
        return (False, ". ".join(errors)) if errors else (True, "Valid.")

    try:
        cfg = get_config()
        ollama_prompt = request.form.get('ollama_validator_prompt')
        cloud_prompt = request.form.get('cloud_validator_prompt')

        is_valid_ollama, msg_ollama = validate_validator_prompt(ollama_prompt)
        is_valid_cloud, msg_cloud = validate_validator_prompt(cloud_prompt)

        if not is_valid_ollama:
            return jsonify({'status': 'error', 'message': f'Ollama Error: {msg_ollama}'}), 400
        if not is_valid_cloud:
            return jsonify({'status': 'error', 'message': f'Cloud Error: {msg_cloud}'}), 400

        if not cfg.has_section('OllamaValidatePrompt'):
            cfg.add_section('OllamaValidatePrompt')
        cfg.set('OllamaValidatePrompt', 'template', ollama_prompt)

        if not cfg.has_section('CloudValidatePrompt'):
            cfg.add_section('CloudValidatePrompt')
        cfg.set('CloudValidatePrompt', 'template', cloud_prompt)
        
        with open(CONFIG_FILE_PATH, 'w') as f:
            cfg.write(f)
            
        return jsonify({'status': 'success', 'message': 'Validator prompts saved!'})
        
    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': f'Error saving validator prompts: {e}'}), 500

@app.route('/export_prompts')
def export_prompts():
    """Export all prompts as a ZIP file containing text files for each prompt category."""
    try:
        cfg = get_config()

        # Define all prompt sections to export
        prompt_sections = [
            'ChatPrompt',
            'OllamaPrompt',
            'CloudPrompt',
            'OllamaPromptWithAsk',
            'CloudPromptWithAsk',
            'OllamaValidatePrompt',
            'CloudValidatePrompt',
            'OllamaSummarizePrompt',
            'CloudSummarizePrompt',
            'OllamaStepSummaryPrompt',
            'CloudStepSummaryPrompt',
            'OllamaSearchSummaryPrompt',
            'CloudSearchSummaryPrompt'
        ]

        # Create ZIP in memory
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for section in prompt_sections:
                if cfg.has_section(section):
                    prompt_text = cfg.get(section, 'template', fallback='')
                    if prompt_text:
                        # Create text file for each prompt
                        filename = f"{section}.txt"
                        zip_file.writestr(filename, prompt_text)

        zip_buffer.seek(0)

        # Generate filename with timestamp
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'prompts_export_{timestamp}.zip'

        return send_file(
            zip_buffer,
            mimetype='application/zip',
            as_attachment=True,
            download_name=filename
        )

    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': f'Error exporting prompts: {e}'}), 500

@app.route('/import_prompts', methods=['POST'])
def import_prompts():
    """Import prompts from a ZIP file containing text files for each prompt category."""
    try:
        if 'file' not in request.files:
            return jsonify({'status': 'error', 'message': 'No file uploaded'}), 400

        file = request.files['file']

        if file.filename == '':
            return jsonify({'status': 'error', 'message': 'No file selected'}), 400

        if not file.filename.endswith('.zip'):
            return jsonify({'status': 'error', 'message': 'File must be a ZIP archive'}), 400

        cfg = get_config()
        imported_count = 0
        errors = []

        # Read ZIP file
        zip_buffer = BytesIO(file.read())

        with zipfile.ZipFile(zip_buffer, 'r') as zip_file:
            # Get list of files in ZIP
            file_list = zip_file.namelist()

            # Process each text file
            for filename in file_list:
                if filename.endswith('.txt'):
                    # Extract section name from filename (e.g., "ChatPrompt.txt" -> "ChatPrompt")
                    section_name = filename.replace('.txt', '')

                    # Read prompt content
                    try:
                        prompt_content = zip_file.read(filename).decode('utf-8')

                        # Create section if it doesn't exist
                        if not cfg.has_section(section_name):
                            cfg.add_section(section_name)

                        # Update prompt template
                        cfg.set(section_name, 'template', prompt_content)
                        imported_count += 1

                    except Exception as e:
                        errors.append(f"{filename}: {str(e)}")

        if imported_count > 0:
            # Save updated config
            with open(CONFIG_FILE_PATH, 'w') as f:
                cfg.write(f)

            message = f'Successfully imported {imported_count} prompt(s)'
            if errors:
                message += f'. Errors: {"; ".join(errors)}'

            return jsonify({'status': 'success', 'message': message, 'count': imported_count})
        else:
            return jsonify({'status': 'error', 'message': 'No valid prompt files found in ZIP'}), 400

    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': f'Error importing prompts: {e}'}), 500

# ---
# --- Handler-e SocketIO (Logica in Timp Real) ---
# ---

@socketio.on('connect')
def handle_connect():
    """Gestioneaza o noua conexiune client (ex: deschiderea paginii, refresh)."""
    global GLOBAL_STATE
    print(f"Client connected: {request.sid}")
    
    # Trimite starea *curenta* (inclusiv din task-ul care ruleaza)
    try:
        # CORECTIE LOG: Trimitem log-ul parsat pentru vizualizarea 'Live'
        filtered_log = agent_core.parse_command_log(GLOBAL_STATE['last_session']['log'])

        raw_responses = "\n\n".join([f"-- Resp {i+1} --\n{r}" for i, r in enumerate(GLOBAL_STATE['last_session'].get("raw_llm_responses", []))])

        # Load Chat History
        log_manager = GLOBAL_STATE.get('log_manager')
        chat_history = log_manager.get_chat_history() if log_manager else []

        initial_data = {
            'agent_history': GLOBAL_STATE['agent_history'],
            'vm_output': GLOBAL_STATE['persistent_vm_output'],
            'last_log': filtered_log, # Trimitem log-ul filtrat
            'last_report': GLOBAL_STATE['last_session'].get('final_report', ''),
            'raw_llm_responses': raw_responses,
            'task_running': GLOBAL_STATE['task_running'],
            'task_paused': GLOBAL_STATE['task_paused'],
            'validator_enabled': GLOBAL_STATE.get('validator_enabled', True),
            'chat_history': chat_history  # NEW FIELD
        }
        socketio.emit('initial_state', initial_data, to=request.sid)
        
        # Trimitem si statusurile curente
        socketio.emit('ssh_status_update', GLOBAL_STATE['ssh_connection_status'], to=request.sid)
        socketio.emit('llm_status_update', GLOBAL_STATE['llm_connection_status'], to=request.sid)
        
    except Exception as e:
        print(f"Error sending initial state: {e}")
        traceback.print_exc()

@socketio.on('disconnect')
def handle_disconnect():
    """Gestioneaza deconectarea clientului."""
    print(f"Client disconnected: {request.sid}. Task continues if running.")

# --- Wrapper pentru Task-ul Agentului ---

def run_agent_and_update_state(socketio, global_state, control_flags, event_objects):
    """
    Wrapper care ruleaza agent_task_runner si gestioneaza curatarea
    si salvarea starii la final.
    """
    try:
        # Apelam functia principala din agent_core
        agent_core.agent_task_runner(socketio, global_state, control_flags, event_objects)
    except Exception as e:
        print(f"Agent task runner exception: {e}")
        traceback.print_exc()
        global_state['last_session']['final_report'] = f"Task failed with exception: {e}"
    finally:
        # Asiguram resetarea flag-urilor
        global_state['task_running'] = False
        global_state['task_paused'] = False
        socketio.emit('task_finished')
        
        # Salvam starea
        save_app_state()

# --- Handler-e SocketIO pentru Controlul Task-ului ---

@socketio.on('execute_task')
def handle_execute_task(data):
    """Porneste executia unui task nou."""
    global GLOBAL_STATE, USER_RESPONSE, USER_ANSWER
    
    if GLOBAL_STATE['task_running']:
        socketio.emit('agent_log', {'data': "A task is already running. Please stop it first."})
        return
    
    objective = data.get('data', '').strip()
    if not objective:
        socketio.emit('agent_log', {'data': "No objective provided."})
        return
    
    # Actualizam starea
    GLOBAL_STATE['current_objective'] = objective
    GLOBAL_STATE['current_execution_mode'] = data.get('mode', 'independent')
    GLOBAL_STATE['current_summarization_mode'] = data.get('summarization_mode', 'automatic')
    GLOBAL_STATE['current_allow_ask_mode'] = data.get('allow_ask', False)
    GLOBAL_STATE['task_running'] = True
    GLOBAL_STATE['task_paused'] = False

    # IMPROVEMENT: Salvam command_timeout in config daca este furnizat
    command_timeout_value = data.get('command_timeout', None)
    if command_timeout_value is not None:
        try:
            cfg = get_config()
            cfg.set('Agent', 'command_timeout', str(command_timeout_value))
            with open(CONFIG_FILE_PATH, 'w') as configfile:
                cfg.write(configfile)
            print(f"Command timeout updated to {command_timeout_value}s")
        except Exception as e:
            print(f"Error saving command timeout: {e}")

    # Resetam variabilele de comunicare
    USER_RESPONSE.clear()
    USER_ANSWER.clear()

    # NOTE: We no longer clear logs here to maintain context across multiple tasks
    # Logs are only cleared when user explicitly resets via the reset button

    # Citim configuratia pentru a actualiza username si IP
    cfg = get_config()
    GLOBAL_STATE['system_username'] = cfg.get('System', 'username', fallback='unknown')
    GLOBAL_STATE['system_ip'] = cfg.get('System', 'ip_address', fallback='unknown')
    
    # Notificam UI-ul
    socketio.emit('task_started')
    
    # Pornim thread-ul agentului
    socketio.start_background_task(
        run_agent_and_update_state,
        socketio,
        GLOBAL_STATE,
        CONTROL_FLAGS,
        EVENT_OBJECTS
    )

@socketio.on('stop_task')
def handle_stop_task():
    """Opreste task-ul curent."""
    global GLOBAL_STATE
    if GLOBAL_STATE['task_running']:
        GLOBAL_STATE['task_running'] = False
        GLOBAL_STATE['task_paused'] = False

        # 1. Force close SSH to unblock read() calls immediately
        ssh_utils.abort_active_connection()

        # 2. Deblocam eventurile care ar putea bloca thread-ul
        # Folosim send_exception pentru a debloca fara erori de re-send
        try:
            USER_APPROVAL_EVENT.send_exception(Exception("Task stopped by user"))
        except:
            pass
        try:
            SUMMARIZATION_EVENT.send_exception(Exception("Task stopped by user"))
        except:
            pass
        try:
            USER_ANSWER_EVENT.send_exception(Exception("Task stopped by user"))
        except:
            pass
        socketio.emit('agent_log', {'data': "\n--- Task stopped by user. ---"})

@socketio.on('pause_task')
def handle_pause_task():
    """Pune task-ul pe pauza."""
    global GLOBAL_STATE
    if GLOBAL_STATE['task_running'] and not GLOBAL_STATE['task_paused']:
        GLOBAL_STATE['task_paused'] = True
        socketio.emit('task_paused')
        socketio.emit('agent_log', {'data': "\n--- Task paused by user. ---"})

@socketio.on('resume_task')
def handle_resume_task(data):
    """Reia task-ul din pauza."""
    global GLOBAL_STATE
    if GLOBAL_STATE['task_running'] and GLOBAL_STATE['task_paused']:
        # Actualizam obiectivul daca a fost modificat
        new_objective = data.get('data', '').strip()
        if new_objective and new_objective != GLOBAL_STATE['current_objective']:
            old_objective = GLOBAL_STATE['current_objective']
            GLOBAL_STATE['current_objective'] = new_objective
            log_msg = f"\n--- Objective updated during pause ---\nOld: {old_objective}\nNew: {new_objective}\n"
            socketio.emit('agent_log', {'data': log_msg})
            GLOBAL_STATE['last_session']['log'] += log_msg + '\n'
            GLOBAL_STATE['agent_history'] += f"\n\n--- USER INTERVENTION ---\nObjective changed from:\n{old_objective}\nTo:\n{new_objective}\n"
            socketio.emit('update_history', {'data': GLOBAL_STATE['agent_history']})
        
        GLOBAL_STATE['task_paused'] = False
        socketio.emit('task_resumed')
        socketio.emit('agent_log', {'data': "--- Task resumed. ---"})

@socketio.on('update_execution_mode')
def handle_update_execution_mode(data):
    """Actualizeaza modul de executie in timpul pauzei."""
    global GLOBAL_STATE
    if GLOBAL_STATE['task_paused']:
        new_mode = data.get('mode')
        if new_mode in ['independent', 'assisted']:
            GLOBAL_STATE['current_execution_mode'] = new_mode
            log_msg = f"--- Execution mode changed to: {new_mode} ---"
            socketio.emit('agent_log', {'data': log_msg})
            GLOBAL_STATE['last_session']['log'] += log_msg + '\n'

@socketio.on('toggle_validator')
def handle_toggle_validator(data):
    """Activeaza/Dezactiveaza validatorul de comenzi."""
    global GLOBAL_STATE
    is_enabled = data.get('enabled', True)
    GLOBAL_STATE['validator_enabled'] = is_enabled

    status_msg = "ENABLED" if is_enabled else "DISABLED"
    log_msg = f"--- Command Validator {status_msg} by user ---"

    socketio.emit('agent_log', {'data': log_msg})
    if GLOBAL_STATE.get('last_session'):
        GLOBAL_STATE['last_session']['log'] += log_msg + '\n'

@socketio.on('update_summarization_threshold')
def handle_update_summarization_threshold(data):
    """Updates summarization threshold live during pause."""
    global GLOBAL_STATE
    new_threshold = data.get('threshold')
    if new_threshold and isinstance(new_threshold, int) and new_threshold > 0:
        # Update config file
        cfg = get_config()
        cfg.set('Agent', 'summarization_threshold', str(new_threshold))
        with open(CONFIG_FILE_PATH, 'w') as f:
            cfg.write(f)

        if GLOBAL_STATE['task_paused']:
            log_msg = f"--- Summarization threshold updated to: {new_threshold} chars ---"
            socketio.emit('agent_log', {'data': log_msg})
            GLOBAL_STATE['last_session']['log'] += log_msg + '\n'

@socketio.on('update_timeout')
def handle_update_timeout(data):
    """Updates command timeout instantly during task execution."""
    global GLOBAL_STATE
    new_timeout = data.get('timeout')
    if new_timeout and isinstance(new_timeout, int) and new_timeout > 0:
        # Update global_state immediately - running timers will see this
        GLOBAL_STATE['command_timeout'] = new_timeout

        # Update config file for persistence
        cfg = get_config()
        cfg.set('Agent', 'command_timeout', str(new_timeout))
        with open(CONFIG_FILE_PATH, 'w') as f:
            cfg.write(f)

        # Notify all clients
        socketio.emit('timeout_updated', {'timeout': new_timeout})

        log_msg = f"--- Command timeout updated to: {new_timeout} seconds ---"
        socketio.emit('agent_log', {'data': log_msg})
        if GLOBAL_STATE.get('last_session'):
            GLOBAL_STATE['last_session']['log'] += log_msg + '\n'

# --- Handler-e pentru Aprobare Comenzi si Interactiuni ---

@socketio.on('approve_command')
def handle_approve_command(data):
    """Gestioneaza aprobarea/respingerea comenzilor."""
    global USER_RESPONSE, USER_APPROVAL_EVENT
    USER_RESPONSE.clear()
    USER_RESPONSE.update(data)
    USER_APPROVAL_EVENT.send()

@socketio.on('provide_answer')
def handle_provide_answer(data):
    """Gestioneaza raspunsul utilizatorului la intrebarile agentului."""
    global USER_ANSWER, USER_ANSWER_EVENT
    USER_ANSWER.clear()
    USER_ANSWER.update(data)
    USER_ANSWER_EVENT.send()

@socketio.on('summarize_decision')
def handle_summarize_decision(data):
    """Gestioneaza decizia de sumarizare."""
    global SUMMARIZATION_EVENT
    
    # Check if user wants to update threshold
    new_threshold = data.get('new_threshold')
    if new_threshold and isinstance(new_threshold, int) and new_threshold > 0:
        # Update config file
        cfg = get_config()
        cfg.set('Agent', 'summarization_threshold', str(new_threshold))
        with open(CONFIG_FILE_PATH, 'w') as f:
            cfg.write(f)
        
        log_msg = f"--- Summarization threshold updated to: {new_threshold} chars ---"
        socketio.emit('agent_log', {'data': log_msg})
        GLOBAL_STATE['last_session']['log'] += log_msg + '\n'
    
    if data.get('summarize'):
        # Apelam functia de sumarizare
        agent_core.summarize_history(socketio, GLOBAL_STATE)
    SUMMARIZATION_EVENT.send()

@socketio.on('manual_summarize')
def handle_manual_summarize():
    """Sumarizare manuala declansata de utilizator."""
    if not GLOBAL_STATE['task_running']:
        socketio.emit('agent_log', {'data': "\n--- Manual summarization requested ---", 'clear': False})
        agent_core.summarize_history(socketio, GLOBAL_STATE)
        save_app_state()

# --- Handler-e pentru Memory Management ---

@socketio.on('reset_agent')
def handle_reset_agent(data):
    """Reseteaza memoria agentului."""
    global GLOBAL_STATE
    if data.get('data') == 'reset':
        if GLOBAL_STATE['task_running']:
            socketio.emit('agent_log', {'data': "Cannot reset while a task is running. Please stop the task first."})
            return

        # 1. Resetam fisierele de pe disc (Sesiune & Exec Log)
        reset_state = session_manager.reset_all_memory(SESSION_FILE_PATH, EXECUTION_LOG_FILE_PATH)
        GLOBAL_STATE.update(reset_state)

        # 2. Resetam variabilele specifice din RAM care nu sunt in session_manager
        GLOBAL_STATE['persistent_vm_output'] = ""
        GLOBAL_STATE['current_objective'] = ""  # FIX: Explicitly clear the objective

        # 3. Reset Log Manager (Base Log, Context, Chat History)
        log_manager = GLOBAL_STATE.get('log_manager')
        if log_manager:
            log_manager.reset_all()  # This clears execution_log, llm_context, AND chat_history.json
            print("Log manager reset completed.")

        # 4. Emitem noua stare catre UI
        socketio.emit('initial_state', {
            'agent_history': GLOBAL_STATE['agent_history'],
            'vm_output': GLOBAL_STATE['persistent_vm_output'],
            'last_log': GLOBAL_STATE['last_session']['log'],
            'last_report': '',
            'raw_llm_responses': '',
            'task_running': False,
            'task_paused': False,
            'chat_history': []  # Clear chat in UI
        })

        # Confirmare vizuala
        socketio.emit('agent_log', {'data': "--- AGENT MEMORY & OBJECTIVE RESET ---", 'clear': True})
        socketio.emit('vm_screen', {'data': "--- VM OUTPUT RESET ---", 'clear': True})
        socketio.emit('chat_history_cleared')  # Signal chat UI to clear

@socketio.on('edit_history')
def handle_edit_history(data):
    """Editeaza istoricul agentului (WARNING: This edits the Full Log directly)."""
    global GLOBAL_STATE
    # Allow editing if task is NOT running OR if task IS running but PAUSED
    if GLOBAL_STATE['task_running'] and not GLOBAL_STATE['task_paused']:
        socketio.emit('agent_log', {'data': "Cannot edit history while a task is running actively. Please PAUSE the task first."})
        return

    new_history = data.get('data', '')
    
    # Actualizam variabila globala pentru UI imediat
    GLOBAL_STATE['agent_history'] = new_history

    # Folosim log manager pentru a procesa editarea cu logica de checkpoint
    log_manager = GLOBAL_STATE.get('log_manager')
    if log_manager:
        # Aceasta metoda va:
        # 1. Scrie un marker in fisierul de log (fara a sterge nimic)
        # 2. Seta new_history ca 'summarized_history'
        # 3. Seta offset-ul curent, astfel incat doar pasii viitori sa fie adaugati la acest context
        log_manager.log_manual_edit(new_history)
        print("Agent memory manually updated via checkpoint system.")

    socketio.emit('update_history', {'data': GLOBAL_STATE['agent_history']})
    save_app_state()

@socketio.on('human_search_started')
def handle_human_search_started(data):
    """Handler when human initiates a search - pauses agent execution."""
    global GLOBAL_STATE
    if GLOBAL_STATE.get('task_running', False):
        GLOBAL_STATE['human_search_pending'] = True
        socketio.emit('agent_log', {'data': "\n--- Agent paused: Human search in progress ---"})

@socketio.on('human_search_completed')
def handle_human_search_completed(data):
    """Handler when human search is completed - resumes agent execution."""
    global GLOBAL_STATE
    GLOBAL_STATE['human_search_pending'] = False
    if GLOBAL_STATE.get('task_running', False):
        socketio.emit('agent_log', {'data': "--- Agent resumed: Human search completed ---"})

@socketio.on('send_chat_message')
def handle_chat_message(data):
    """Handle incoming chat messages from the UI."""
    message = data.get('message', '').strip()
    if not message:
        return

    # Start a background task for the chat response
    socketio.start_background_task(
        agent_core.process_chat_message,
        socketio,
        GLOBAL_STATE,
        message
    )

@socketio.on('clear_chat')
def handle_clear_chat():
    """Clear persistent chat history."""
    log_manager = GLOBAL_STATE.get('log_manager')
    if log_manager:
        log_manager.clear_chat_history()
    socketio.emit('chat_history_cleared')

@socketio.on('analyze_task_result')
def handle_analyze_task_result():
    """
    Called by frontend when a chat-initiated task finishes.
    """
    global GLOBAL_STATE

    final_report = GLOBAL_STATE.get('last_session', {}).get('final_report', 'No report available.')
    current_objective = GLOBAL_STATE.get('current_objective', '')

    # Mark action plan step as completed if it matches
    log_manager = GLOBAL_STATE.get('log_manager')
    if log_manager and current_objective:
        step_marked = log_manager.mark_plan_step_completed(current_objective)
        if step_marked:
            print(f"Action plan step marked complete: {current_objective}")

            # Emit updated action plan data to UI
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

    # Improved Prompt: Contextual closing instead of "System Event"
    system_trigger = f"""
[ACTION COMPLETED]
The execution of the initiated task is finished.
Final Report: {final_report}

INSTRUCTION:
Based on the previous conversation, briefly inform the user that the task is done and summarize the outcome.
Be natural, and continue the conversation.
"""

    socketio.start_background_task(
        agent_core.process_chat_message,
        socketio,
        GLOBAL_STATE,
        system_trigger
    )

@socketio.on('get_action_plan')
def handle_get_action_plan():
    """
    Returns the current action plan data in a formatted structure.
    """
    log_manager = GLOBAL_STATE.get('log_manager')
    if not log_manager:
        socketio.emit('action_plan_data', {'exists': False})
        return

    # Load raw plan data (active plan from stack)
    plan_data = log_manager.action_plan.get_active_plan()

    if not plan_data or 'steps' not in plan_data:
        socketio.emit('action_plan_data', {'exists': False})
        return

    # Calculate progress
    total_steps = len(plan_data['steps'])
    completed_steps = sum(1 for step in plan_data['steps'] if step.get('completed', False))

    # Find next pending step
    next_step_index = None
    for idx, step in enumerate(plan_data['steps'], 1):
        if not step.get('completed', False):
            next_step_index = idx
            break

    # Format response
    response = {
        'exists': True,
        'title': plan_data.get('title', 'Action Plan'),
        'steps': plan_data['steps'],
        'total_steps': total_steps,
        'completed_steps': completed_steps,
        'next_step_index': next_step_index,
        'created_at': plan_data.get('created_at', '')
    }

    socketio.emit('action_plan_data', response)

@app.route('/update_action_plan', methods=['POST'])
def update_action_plan():
    """
    Updates or creates an action plan from the UI.
    Expected data: { title: "...", steps: [ {objective: "...", completed: boolean}, ... ] }
    """
    data = request.json
    if not data:
        return jsonify({'status': 'error', 'message': 'No data provided'})

    title = data.get('title', 'Action Plan')
    steps_data = data.get('steps', [])

    if not steps_data:
        return jsonify({'status': 'error', 'message': 'No steps provided'})

    log_manager = GLOBAL_STATE.get('log_manager')
    if log_manager:
        # Extract step objectives for set_action_plan (which expects List[str])
        step_objectives = [step.get('objective', '') for step in steps_data if step.get('objective')]

        # Set the plan (this creates/pushes a new plan)
        log_manager.set_action_plan(title, step_objectives)

        # Now we need to update the completion status for each step
        # Load the stack and update the active plan's completed flags
        stack = log_manager.action_plan.load_stack()
        if stack and 'steps' in stack[-1]:
            active_plan = stack[-1]
            for idx, step_data in enumerate(steps_data):
                if idx < len(active_plan['steps']) and step_data.get('completed', False):
                    active_plan['steps'][idx]['completed'] = True

            # Save the updated stack
            log_manager.action_plan._save_stack(stack)

        # Emit update to all clients
        updated_plan = log_manager.action_plan.get_active_plan()
        if updated_plan:
            socketio.emit('action_plan_data', {
                'exists': True,
                'title': updated_plan.get('title', 'Action Plan'),
                'steps': updated_plan.get('steps', []),
                'completed_steps': sum(1 for s in updated_plan.get('steps', []) if s.get('completed', False)),
                'total_steps': len(updated_plan.get('steps', [])),
                'next_step_index': next((i + 1 for i, s in enumerate(updated_plan.get('steps', [])) if not s.get('completed', False)), None)
            })

        return jsonify({'status': 'success'})

    return jsonify({'status': 'error', 'message': 'Log manager not available'})

@socketio.on('clear_action_plan')
def handle_clear_action_plan():
    """
    Clears the current action plan.
    """
    log_manager = GLOBAL_STATE.get('log_manager')
    if log_manager:
        log_manager.clear_action_plan()
        print("Action plan cleared via UI request")

    # Notify frontend
    socketio.emit('action_plan_cleared')

# ---
# --- Initializare Aplicatie (Module Level - runs on import) ---
# ---

# Initialize log system when module is loaded (for Gunicorn)
initialize_log_system()

# CRITICAL FIX: Initialize connections from config.ini immediately
# This ensures settings persist after Docker restart/rebuild when running via Gunicorn
try:
    print("Loading configuration from disk...")
    # Load app state first (to recover session history if available)
    load_app_state()

    # Then test/load connections based on config.ini
    initialize_ssh_status()
    initialize_llm_status()
    print("Configuration loaded and connections initialized.")
except Exception as e:
    print(f"Error during module-level initialization: {e}")
    # We don't raise here to allow the server to start even if config is partial

# ---
# --- Initializare Aplicatie (Main Block - runs only when executed directly) ---
# ---

if __name__ == '__main__':
    try:
        print("=" * 50)
        print("AI Agent Controller Starting...")
        print("=" * 50)

        # Initializam cheile SSH
        ssh_utils.initialize_ssh_key_if_needed()

        # Incarcam starea salvata
        load_app_state()
        print("Application state loaded.")
        print("Log system already initialized at module level.")

        # Testam conexiunile
        initialize_ssh_status()
        initialize_llm_status()
        
        print(f"SSH Status: {GLOBAL_STATE['ssh_connection_status']}")
        print(f"LLM Status: {GLOBAL_STATE['llm_connection_status']}")
        
        print("=" * 50)
        print("Server ready. Access at http://localhost:5000")
        print("=" * 50)
        
        # Pornim serverul
        socketio.run(app, debug=False, host='0.0.0.0', port=5000)
        
    except Exception as e:
        print(f"Fatal error during startup: {e}")
        traceback.print_exc()
