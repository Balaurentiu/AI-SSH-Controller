# --- Importuri Python Standard & Pachete ---
# Note: Eventlet removed for PyInstaller compatibility. Flask-SocketIO will use simple-websocket instead.
import os
import re
import time
import uuid
import zipfile
import json
import traceback
import threading
from io import BytesIO, StringIO
from datetime import datetime
from functools import partial

# --- Importuri Flask & SocketIO ---
from flask import Flask, render_template, request, jsonify, send_file, Response
from flask_socketio import SocketIO

# --- Importurile Noilor Module Refactorizate ---
from config import (
    get_config, KEYS_DIR, CONFIG_FILE_PATH,
    SESSION_FILE_PATH, CONNECTIONS_FILE_PATH, EXECUTION_LOG_FILE_PATH,
    APP_DIR, KNOWLEDGE_DIR
)
import ssh_utils
import llm_utils
import session_manager
import agent_core
import web_search_module
import chat_export

# --- Importuri LangChain (Added for Search Summarization) ---
from langchain_community.llms import Ollama
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import PromptTemplate

# --- Initializam Aplicatia si WebSocket-ul ---
app = Flask(__name__, template_folder='templates')
# Use threading async_mode - eventlet blocks entire event loop on slow LLM calls
# because there's no monkey_patch. Threading uses real OS threads so blocking I/O
# in one thread (e.g. Gemini API timeout) doesn't freeze the whole server.
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

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
    "current_allow_ask_mode": True, # True sau False
    "validator_enabled": True, # NEW: Master switch for the LLM Validator
    "system_username": "", # Numele utilizatorului sistemului tinta
    "system_ip": "", # IP-ul sistemului tinta
    "sudo_available": False, # IMPROVEMENT: Detectat automat - True daca sudo fara parola este disponibil
    # Status-uri
    "task_running": False,
    "task_paused": False,
    "human_search_pending": False,  # Flag to pause agent execution during human-initiated search
    "ssh_connection_status": {"status": "unknown", "message": "Not tested yet."},
    "llm_connection_status": {"status": "unknown", "message": "Not tested yet."},
    "api_ui_locked": False,          # Stays True until user clicks "Take Control" or /api/chat/release
    "webui_watching_only": False,    # User opened browser but ceded control back to API (Watch Only)
    "auto_accept_tasks": False,      # Auto-start task proposals without human approval
    "auto_switch": False,            # Auto-approve system switch proposals without human approval
}

# --- Lock for task-start critical section ---
# Protects the check-then-set sequence on GLOBAL_STATE['task_running'] to prevent
# two concurrent callers (e.g. SocketIO execute_task + REST api/execute_ssh) from
# both seeing task_running=False and both starting a task.
TASK_START_LOCK = threading.Lock()

# --- Background Health Check Thread ---
_HEALTH_CHECK_STOP = threading.Event()
_HEALTH_CHECK_INTERVAL = 60  # seconds between checks


def _background_health_check():
    """
    Daemon thread: checks SSH and LLM reachability every 60 s.

    SSH check is skipped while a task is running to avoid competing with the
    agent for network resources.  LLM check runs only for Ollama (cloud providers
    are skipped to avoid unnecessary API calls / quota consumption).

    Emits 'ssh_status_update' / 'llm_status_update' only when the status
    actually changes, so the UI chips stay accurate without flooding the socket.
    """
    import copy

    print(f"[HEALTH] Background health check thread started (interval={_HEALTH_CHECK_INTERVAL}s).", flush=True)

    while not _HEALTH_CHECK_STOP.wait(_HEALTH_CHECK_INTERVAL):
        try:
            # ---- SSH check ----
            if not GLOBAL_STATE.get('task_running', False):
                prev_ssh = dict(GLOBAL_STATE['ssh_connection_status'])
                cfg = get_config()
                ip   = cfg.get('System', 'ip_address', fallback='').strip()
                user = cfg.get('System', 'username',   fallback='').strip()

                if not ip or not user:
                    new_ssh = {'status': 'failure', 'message': 'System not configured.'}
                else:
                    ok, msg = ssh_utils.get_ssh_health()
                    new_ssh = {'status': 'success' if ok else 'failure', 'message': msg}

                if new_ssh != prev_ssh:
                    GLOBAL_STATE['ssh_connection_status'] = new_ssh
                    socketio.emit('ssh_status_update', new_ssh)
                    print(f"[HEALTH] SSH status → {new_ssh['status']}: {new_ssh['message']}", flush=True)

            # ---- LLM check (Ollama only) ----
            cfg = get_config()
            provider = cfg.get('General', 'provider', fallback='').strip()
            if provider == 'ollama':
                prev_llm = dict(GLOBAL_STATE['llm_connection_status'])
                # Preserve existing chat_llm — health check should not recreate it
                # (recreating mid-session causes unnecessary reinit logs and disrupts active chats)
                existing_chat_llm = GLOBAL_STATE.get('chat_llm')
                initialize_llm_status()
                if existing_chat_llm is not None:
                    GLOBAL_STATE['chat_llm'] = existing_chat_llm
                new_llm = GLOBAL_STATE['llm_connection_status']
                if new_llm != prev_llm:
                    socketio.emit('llm_status_update', new_llm)
                    print(f"[HEALTH] LLM status → {new_llm['status']}: {new_llm['message']}", flush=True)

            # ---- Registry cleanup (BUG-1: memory leak) ----
            # Remove completed/errored entries older than 2 hours
            _registry_ttl = 7200
            _now = time.time()
            stale_chat = [
                rid for rid, e in list(API_CHAT_REGISTRY.items())
                if e.get('completed_at') and _now - e['completed_at'] > _registry_ttl
            ]
            for rid in stale_chat:
                del API_CHAT_REGISTRY[rid]
            if stale_chat:
                print(f"[HEALTH] Cleaned {len(stale_chat)} stale API_CHAT_REGISTRY entries.", flush=True)

            from agent_core import API_TASK_REGISTRY as _ATR
            stale_tasks = [
                tid for tid, e in list(_ATR.items())
                if e.get('status') not in ('running',)
                and e.get('start_time') and _now - e['start_time'] > _registry_ttl
            ]
            for tid in stale_tasks:
                del _ATR[tid]
            if stale_tasks:
                print(f"[HEALTH] Cleaned {len(stale_tasks)} stale API_TASK_REGISTRY entries.", flush=True)

            # ---- api_ui_locked auto-release (BUG-2: abandoned lock) ----
            # If UI has been locked by API for >30min with no active task or chat, auto-release
            if GLOBAL_STATE.get('api_ui_locked') and \
               not GLOBAL_STATE.get('task_running') and \
               not GLOBAL_STATE.get('api_chat_active'):
                last_activity = GLOBAL_STATE.get('_api_last_activity', 0)
                if _now - last_activity > 1800:
                    GLOBAL_STATE['api_ui_locked'] = False
                    GLOBAL_STATE['api_chat_active'] = False
                    print("[HEALTH] api_ui_locked auto-released after 30min inactivity.", flush=True)
                    socketio.emit('api_lock_status', {'active': False})

        except Exception as e:
            print(f"[HEALTH] Health check error: {e}", flush=True)

    print("[HEALTH] Background health check thread stopped.", flush=True)


def _start_health_check_thread():
    """Start the background health check daemon thread (idempotent)."""
    _HEALTH_CHECK_STOP.clear()
    t = threading.Thread(target=_background_health_check, name='health-check', daemon=True)
    t.start()


# --- Flag-uri si Evenimente de Control pentru Thread-ul Agentului ---
CONTROL_FLAGS = {
    "is_running": lambda: GLOBAL_STATE['task_running'],
    "is_paused": lambda: GLOBAL_STATE['task_paused'],
    "set_running": lambda val: setattr_safe('task_running', val),
    "set_paused": lambda val: setattr_safe('task_paused', val)
}

# --- Evenimente pentru Comunicare Thread-UI ---
USER_APPROVAL_EVENT = threading.Event()
USER_RESPONSE = {}
SUMMARIZATION_EVENT = threading.Event()
USER_ANSWER_EVENT = threading.Event()
USER_ANSWER = {}
SYSTEM_SWITCH_EVENT = threading.Event()
SYSTEM_SWITCH_RESPONSE = {}  # {'approved': bool, 'target_system': str}
PENDING_SWITCH_TARGET = None  # Set when waiting for user approval, cleared on response
PENDING_ASK_DATA = None  # Set when execution agent asks user, cleared on answer
PENDING_APPROVAL_DATA = None  # Set when execution agent waits for command approval
PENDING_TASK_PROPOSAL = None  # Set when chat agent proposes a task, cleared on accept/reject/new chat

CONNECTED_UI_CLIENTS = set()   # SIDs of browser clients currently connected
API_CHAT_REGISTRY = {}          # {request_id: {status, message, response, pending_type, pending_data, ...}}

# Share events and pending flags via GLOBAL_STATE so agent_core can access them
# without importing app module (which causes duplicate module instances in threading mode)
GLOBAL_STATE['_switch_event'] = SYSTEM_SWITCH_EVENT
GLOBAL_STATE['_switch_response'] = SYSTEM_SWITCH_RESPONSE
GLOBAL_STATE['_pending_switch_target'] = None
GLOBAL_STATE['_pending_ask_data'] = None
GLOBAL_STATE['_pending_approval_data'] = None
GLOBAL_STATE['_pending_task_proposal'] = None

EVENT_OBJECTS = {
    "user_approval_event": USER_APPROVAL_EVENT,
    "user_response": USER_RESPONSE,
    "summarization_event": SUMMARIZATION_EVENT,
    "user_answer_event": USER_ANSWER_EVENT,
    "user_answer": USER_ANSWER,
    "system_switch_event": SYSTEM_SWITCH_EVENT,
    "system_switch_response": SYSTEM_SWITCH_RESPONSE
}

# --- Functie helper pentru setari sigure ---
def setattr_safe(key, val):
    """Seteaza o valoare in GLOBAL_STATE in mod thread-safe."""
    GLOBAL_STATE[key] = val

# ---
# --- Functii Helper ---
# ---

def _emit_knowledge_limit_warning(km):
    """Emit a SocketIO warning if knowledge store is near or at capacity."""
    try:
        status = km.get_limit_status()
        if status['at_limit']:
            socketio.emit('knowledge_limit_warning', {
                'level': 'full',
                'count': status['count'],
                'max': status['max'],
                'message': f"Knowledge store is full ({status['count']}/{status['max']} documents). New documents cannot be added until existing ones are removed."
            })
        elif status['near_limit']:
            socketio.emit('knowledge_limit_warning', {
                'level': 'warning',
                'count': status['count'],
                'max': status['max'],
                'message': f"Knowledge store almost full: {status['count']}/{status['max']} documents used. Consider removing unused documents."
            })
    except Exception:
        pass


def append_live_log(message):
    """Append a message to the persistent agent live log on disk."""
    try:
        from config import AGENT_LIVE_LOG_PATH
        with open(AGENT_LIVE_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(message + '\n')
    except Exception:
        pass

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
                temperature = cfg.getfloat('Agent', 'temperature', fallback=0.5)
                if provider == 'ollama':
                    api_url = cfg.get('Ollama', 'api_url', fallback='')
                    keep_alive = cfg.get('Ollama', 'keep_alive', fallback='-1')
                    llm = Ollama(model=model_name, base_url=api_url, timeout=60, temperature=temperature, keep_alive=keep_alive)
                elif provider == 'gemini':
                    api_key = cfg.get('General', 'gemini_api_key', fallback='')
                    llm = ChatGoogleGenerativeAI(model=model_name, google_api_key=api_key, generation_config={"temperature": temperature})
                elif provider == 'anthropic':
                    api_key = cfg.get('General', 'anthropic_api_key', fallback='')
                    llm = ChatAnthropic(model=model_name, api_key=api_key, temperature=temperature)

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

        # Read API key from [General] section based on provider (same as execution LLM)
        if chat_provider == 'gemini':
            chat_api_key = cfg.get('General', 'gemini_api_key', fallback='')
        elif chat_provider == 'anthropic':
            chat_api_key = cfg.get('General', 'anthropic_api_key', fallback='')
        else:
            chat_api_key = ''

        print(f"[CHAT LLM INIT] Provider: {chat_provider}, Model: {chat_model}", flush=True)

        # Use ChatLLM-specific Ollama URL if configured; fall back to main [Ollama] URL
        chat_ollama_url = cfg.get('ChatLLM', 'ollama_url', fallback='').strip()
        ollama_url = chat_ollama_url if chat_ollama_url else cfg.get('Ollama', 'api_url', fallback='http://localhost:11434')

        temperature = cfg.getfloat('Agent', 'temperature', fallback=0.5)
        llm_timeout = cfg.getint('Agent', 'llm_timeout', fallback=120)

        try:
            # Initialize chat LLM
            if chat_provider == 'ollama':
                from langchain_community.llms import Ollama
                print(f"[CHAT LLM INIT] Creating Ollama instance: model={chat_model}, url={ollama_url}", flush=True)
                ollama_num_ctx = cfg.getint('Ollama', 'num_ctx', fallback=32768)
                keep_alive = cfg.get('Ollama', 'keep_alive', fallback='-1')
                GLOBAL_STATE['chat_llm'] = Ollama(model=chat_model, base_url=ollama_url, timeout=llm_timeout, num_ctx=ollama_num_ctx, temperature=temperature, keep_alive=keep_alive)
                print(f"[CHAT LLM INIT] ✓ Chat LLM initialized: Ollama ({chat_model}) on {ollama_url}, num_ctx={ollama_num_ctx}, temp={temperature}, timeout={llm_timeout}s", flush=True)
            elif chat_provider == 'gemini':
                from langchain_google_genai import ChatGoogleGenerativeAI
                print(f"[CHAT LLM INIT] Creating Gemini instance: model={chat_model}", flush=True)
                GLOBAL_STATE['chat_llm'] = ChatGoogleGenerativeAI(
                    model=chat_model,
                    google_api_key=chat_api_key,
                    generation_config={"temperature": temperature},
                    convert_system_message_to_human=True,
                    timeout=llm_timeout,
                    request_timeout=llm_timeout
                )
                print(f"[CHAT LLM INIT] ✓ Chat LLM initialized: Gemini ({chat_model}), temp={temperature}, timeout={llm_timeout}s", flush=True)
            elif chat_provider == 'anthropic':
                from langchain_anthropic import ChatAnthropic
                print(f"[CHAT LLM INIT] Creating Anthropic instance: model={chat_model}", flush=True)
                GLOBAL_STATE['chat_llm'] = ChatAnthropic(
                    model=chat_model,
                    api_key=chat_api_key,
                    temperature=temperature,
                    timeout=llm_timeout,
                    default_request_timeout=llm_timeout
                )
                print(f"[CHAT LLM INIT] ✓ Chat LLM initialized: Anthropic ({chat_model}), temp={temperature}, timeout={llm_timeout}s", flush=True)
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

    # --- Chat LLM Failback initialization ---
    GLOBAL_STATE['_chat_llm_fallback'] = None
    GLOBAL_STATE['_using_chat_fallback'] = False
    if cfg.getboolean('ChatLLMFallback', 'enabled', fallback=False):
        fb_model = cfg.get('ChatLLMFallback', 'model_name', fallback='').strip()
        fb_provider = cfg.get('ChatLLMFallback', 'provider', fallback='ollama')
        if fb_model:
            try:
                fb_timeout = cfg.getint('Agent', 'llm_timeout', fallback=120)
                fb_temp = cfg.getfloat('Agent', 'temperature', fallback=0.5)
                fb_num_ctx = cfg.getint('Ollama', 'num_ctx', fallback=32768)
                fb_keep_alive = cfg.get('Ollama', 'keep_alive', fallback='-1')
                if fb_provider == 'ollama':
                    fb_url = cfg.get('ChatLLMFallback', 'ollama_url', fallback='').strip() or cfg.get('Ollama', 'api_url', fallback='http://localhost:11434')
                    GLOBAL_STATE['_chat_llm_fallback'] = Ollama(model=fb_model, base_url=fb_url, timeout=fb_timeout, num_ctx=fb_num_ctx, temperature=fb_temp, keep_alive=fb_keep_alive)
                elif fb_provider == 'gemini':
                    fb_key = cfg.get('ChatLLMFallback', 'api_key', fallback='').strip() or cfg.get('General', 'gemini_api_key', fallback='')
                    GLOBAL_STATE['_chat_llm_fallback'] = ChatGoogleGenerativeAI(model=fb_model, google_api_key=fb_key, generation_config={"temperature": fb_temp})
                elif fb_provider == 'anthropic':
                    fb_key = cfg.get('ChatLLMFallback', 'api_key', fallback='').strip() or cfg.get('General', 'anthropic_api_key', fallback='')
                    GLOBAL_STATE['_chat_llm_fallback'] = ChatAnthropic(model=fb_model, api_key=fb_key, temperature=fb_temp)
                if GLOBAL_STATE['_chat_llm_fallback']:
                    print(f"[CHAT LLM FAILBACK] ✓ Chat fallback ready: {fb_provider}/{fb_model}", flush=True)
            except Exception as fb_err:
                print(f"[CHAT LLM FAILBACK] ✗ Could not init chat failback: {fb_err}", flush=True)

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
    # ollama_url key migration: old configs may have stored it as 'ollama_api_url'
    _chat_ollama_url = (cfg.get('ChatLLM', 'ollama_url', fallback='') or
                        cfg.get('ChatLLM', 'ollama_api_url', fallback=''))
    chat_llm_config = {
        'enabled': cfg.getboolean('ChatLLM', 'enabled', fallback=False),
        'provider': cfg.get('ChatLLM', 'provider', fallback='ollama'),
        'model_name': cfg.get('ChatLLM', 'model_name', fallback=''),
        'api_key': (cfg.get('ChatLLM', 'api_key', fallback='') or
                    cfg.get('ChatLLM', 'gemini_api_key', fallback='') or
                    cfg.get('ChatLLM', 'anthropic_api_key', fallback='')),
        'ollama_url': _chat_ollama_url
    }

    # Load Knowledge configuration if exists
    knowledge_config = {
        'enabled': cfg.getboolean('Knowledge', 'enabled', fallback=False),
        'embedding_provider': cfg.get('Knowledge', 'embedding_provider', fallback='ollama'),
        'embedding_model': cfg.get('Knowledge', 'embedding_model', fallback=''),
        'vector_store': cfg.get('Knowledge', 'vector_store', fallback='chromadb'),
        'max_documents': cfg.getint('Knowledge', 'max_documents', fallback=10),
        'max_file_size_mb': cfg.getint('Knowledge', 'max_file_size_mb', fallback=50),
    }

    def _llm_section_config(section):
        return {
            'enabled': cfg.getboolean(section, 'enabled', fallback=False),
            'provider': cfg.get(section, 'provider', fallback='ollama'),
            'model_name': cfg.get(section, 'model_name', fallback=''),
            'api_key': cfg.get(section, 'api_key', fallback=''),
            'ollama_url': cfg.get(section, 'ollama_url', fallback='')
        }

    return jsonify({
        'provider': cfg.get('General', 'provider', fallback='ollama'),
        'model_name': cfg.get('Agent', 'model_name', fallback=''),
        'gemini_api_key': cfg.get('General', 'gemini_api_key', fallback=''),
        'anthropic_api_key': cfg.get('General', 'anthropic_api_key', fallback=''),
        'ollama_api_url': cfg.get('Ollama', 'api_url', fallback='http://localhost:11434'),
        'ollama_num_ctx': cfg.getint('Ollama', 'num_ctx', fallback=32768),
        'ollama_keep_alive': cfg.get('Ollama', 'keep_alive', fallback='-1'),
        'max_steps': cfg.getint('Agent', 'max_steps', fallback=50),
        'summarization_threshold': cfg.getint('Agent', 'summarization_threshold', fallback=15000),
        'llm_timeout': cfg.getint('Agent', 'llm_timeout', fallback=120),
        'chat_history_message_count': cfg.getint('Agent', 'chat_history_message_count', fallback=20),
        'temperature': cfg.getfloat('Agent', 'temperature', fallback=0.5),
        'command_timeout': cfg.getint('Agent', 'command_timeout', fallback=300),
        'chat_llm': chat_llm_config,
        'knowledge': knowledge_config,
        'validator_llm': _llm_section_config('ValidatorLLM'),
        'exec_llm_fallback': _llm_section_config('ExecLLMFallback'),
        'chat_llm_fallback': _llm_section_config('ChatLLMFallback'),
        'validator_llm_fallback': _llm_section_config('ValidatorLLMFallback'),
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
        cfg.set('Ollama', 'num_ctx', str(data.get('ollama_num_ctx', 32768)))
        cfg.set('Ollama', 'keep_alive', str(data.get('ollama_keep_alive', '-1')))
        cfg.set('Agent', 'temperature', str(data.get('temperature', 0.5)))

        # Save Chat LLM Configuration
        if 'chat_llm' in data:
            if not cfg.has_section('ChatLLM'):
                cfg.add_section('ChatLLM')
            chat_llm = data['chat_llm']
            cfg.set('ChatLLM', 'enabled', str(chat_llm.get('enabled', False)))
            cfg.set('ChatLLM', 'provider', chat_llm.get('provider', 'ollama'))
            cfg.set('ChatLLM', 'model_name', chat_llm.get('model_name', ''))
            cfg.set('ChatLLM', 'api_key', chat_llm.get('api_key', ''))
            cfg.set('ChatLLM', 'ollama_url', chat_llm.get('ollama_url', ''))

        # Save Knowledge Configuration
        if 'knowledge' in data:
            if not cfg.has_section('Knowledge'):
                cfg.add_section('Knowledge')
            knowledge = data['knowledge']
            cfg.set('Knowledge', 'enabled', str(knowledge.get('enabled', False)))
            cfg.set('Knowledge', 'embedding_provider', knowledge.get('embedding_provider', 'ollama'))
            cfg.set('Knowledge', 'embedding_model', knowledge.get('embedding_model', ''))
            cfg.set('Knowledge', 'vector_store', knowledge.get('vector_store', 'chromadb'))
            cfg.set('Knowledge', 'max_documents', str(knowledge.get('max_documents', 10)))
            cfg.set('Knowledge', 'max_file_size_mb', str(knowledge.get('max_file_size_mb', 50)))

        def _save_llm_section(section, d):
            if not cfg.has_section(section):
                cfg.add_section(section)
            cfg.set(section, 'enabled', str(d.get('enabled', False)))
            cfg.set(section, 'provider', d.get('provider', 'ollama'))
            cfg.set(section, 'model_name', d.get('model_name', ''))
            cfg.set(section, 'api_key', d.get('api_key', ''))
            cfg.set(section, 'ollama_url', d.get('ollama_url', ''))

        if 'validator_llm' in data:
            _save_llm_section('ValidatorLLM', data['validator_llm'])
        if 'exec_llm_fallback' in data:
            _save_llm_section('ExecLLMFallback', data['exec_llm_fallback'])
        if 'chat_llm_fallback' in data:
            _save_llm_section('ChatLLMFallback', data['chat_llm_fallback'])
        if 'validator_llm_fallback' in data:
            _save_llm_section('ValidatorLLMFallback', data['validator_llm_fallback'])

        with open(CONFIG_FILE_PATH, 'w') as f:
            cfg.write(f)

        # Re-testam conexiunea
        initialize_llm_status()
        socketio.emit('llm_status_update', GLOBAL_STATE['llm_connection_status'])

        # Reinitialize Knowledge Manager if config changed
        initialize_knowledge_manager()

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
            # Use URL from request if provided (e.g. chat LLM with different server), else main config
            url = data.get('ollama_url', '').strip() or cfg.get('Ollama', 'api_url', fallback='http://localhost:11434')
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

@app.route('/get_embedding_models', methods=['POST'])
def get_embedding_models_route():
    """Fetches available embedding models for the specified provider."""
    try:
        data = request.json
        provider = data.get('provider', 'ollama')

        cfg = get_config()
        models = []

        if provider == 'ollama':
            url = cfg.get('Ollama', 'api_url', fallback='http://localhost:11434')
            success, msg, models = llm_utils.check_ollama_embedding_models(url)

        elif provider == 'gemini':
            key = cfg.get('General', 'gemini_api_key', fallback='')
            if not key:
                return jsonify({'status': 'error', 'message': 'Gemini API Key not configured. Set it in the Execution LLM section first.'})
            success, msg, models = llm_utils.check_gemini_embedding_models(key)

        else:
            return jsonify({'status': 'error', 'message': f'Embedding not supported for provider: {provider}'})

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
        'system_name': cfg.get('System', 'system_name', fallback=''),  # Friendly name
        'auth_method': cfg.get('System', 'auth_method', fallback='key'),  # key, password, key_or_password
        'auth_password': cfg.get('System', 'auth_password', fallback=''),  # Stored password for SSH auth
        'device_type': cfg.get('System', 'device_type', fallback='linux'),  # linux, windows, cisco, etc.
        'enable_password': cfg.get('System', 'enable_password', fallback=''),  # Cisco enable/secret password
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
        # NEW: Get friendly name (alias) for the connection
        system_name = data.get('system_name', '').strip()
        # NEW: Authentication method and password
        auth_method = data.get('auth_method', 'key').strip()  # key, password, key_or_password
        auth_password = data.get('auth_password', '')  # Password for SSH auth (not deployment)
        # NEW: Device type and enable password
        device_type = data.get('device_type', 'linux').strip()  # linux, windows, cisco, nxos, etc.
        enable_password = data.get('enable_password', '')  # Cisco enable/secret password

        if not ip or not username:
            return jsonify({'status': 'error', 'message': 'IP and Username are required.'}), 400

        # Validate auth settings
        if auth_method in ['password', 'key_or_password'] and not auth_password:
            return jsonify({'status': 'error', 'message': 'Password is required for the selected authentication method.'}), 400

        # IMPROVED: Read previous values from config.ini BEFORE modifying (fallback to GLOBAL_STATE)
        # This ensures we capture the previous connection even on first app load
        previous_username = cfg.get('System', 'username', fallback='') or GLOBAL_STATE.get('system_username', '')
        previous_ip = cfg.get('System', 'ip_address', fallback='') or GLOBAL_STATE.get('system_ip', '')
        previous_name = cfg.get('System', 'system_name', fallback='') or GLOBAL_STATE.get('system_name', '')

        # Salvam in config.ini
        cfg.set('System', 'ip_address', ip)
        cfg.set('System', 'username', username)
        cfg.set('System', 'ssh_port', str(ssh_port))
        cfg.set('System', 'ssh_key_path', ssh_key_path)
        cfg.set('System', 'auth_method', auth_method)
        cfg.set('System', 'auth_password', auth_password)  # Note: Consider encryption for production
        cfg.set('System', 'device_type', device_type)
        cfg.set('System', 'enable_password', enable_password)
        # Store current system name for display
        if system_name:
            cfg.set('System', 'system_name', system_name)

        with open(CONFIG_FILE_PATH, 'w') as f:
            cfg.write(f)

        # Actualizam GLOBAL_STATE
        GLOBAL_STATE['system_ip'] = ip
        GLOBAL_STATE['system_username'] = username
        GLOBAL_STATE['system_name'] = system_name  # Store friendly name

        # Log SSH connection change to Full Log AND LLM Context (single source of truth)
        # Only log if there's an actual change (different IP or username)
        is_actual_change = (previous_ip != ip) or (previous_username != username)

        # Drop persistent SSH connection whenever the target system changes
        if is_actual_change:
            ssh_utils.close_persistent_client()

        log_manager = GLOBAL_STATE.get('log_manager')
        if log_manager and is_actual_change:
            log_manager.log_ssh_connection_change(username, ip, previous_username, previous_ip, system_name)
            # Track last logged system to prevent duplicate entries from agent_core
            GLOBAL_STATE['last_logged_system'] = {'username': username, 'ip': ip, 'name': system_name}
            if previous_ip and previous_username:
                print(f"SSH connection change logged: {previous_name or previous_username}@{previous_ip} -> {system_name or username}@{ip}")
            else:
                print(f"SSH connection logged: {system_name or username}@{ip} (first connection)")
        elif log_manager and not is_actual_change:
            print(f"SSH connection unchanged: {system_name or username}@{ip} (no log entry needed)")

        # Salvam in connections.json (istoric)
        connections = session_manager.load_connections()

        # Verificam daca conexiunea exista deja (by IP, username, port)
        existing = next((c for c in connections if c['ip'] == ip and c['username'] == username and c.get('port', 22) == ssh_port), None)
        if existing:
            # Update the existing connection's name and auth settings
            if system_name:
                existing['name'] = system_name
            existing['auth_method'] = auth_method
            existing['auth_password'] = auth_password
            existing['device_type'] = device_type
            existing['enable_password'] = enable_password
            session_manager.save_connections(connections)
        else:
            # Add new connection with friendly name and auth settings
            connections.append({
                'name': system_name or f"{username}@{ip}",  # Default to user@ip if no name
                'ip': ip,
                'username': username,
                'port': ssh_port,
                'ssh_key_path': ssh_key_path,
                'auth_method': auth_method,
                'auth_password': auth_password,
                'device_type': device_type,
                'enable_password': enable_password,
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

@app.route('/get_validator_whitelist')
def get_validator_whitelist():
    """Return the validator whitelist for the given or currently active system.
    Optional query param: ?system_name=<name> to load whitelist for a specific system.
    """
    requested = request.args.get('system_name', '').strip()
    if requested:
        system_name = requested
    else:
        cfg = get_config()
        system_name = cfg.get('System', 'system_name', fallback='').strip()
    whitelist = agent_core._load_validator_whitelist()
    return jsonify({
        'system_name': system_name,
        'commands': whitelist.get(system_name, []),
        'all_systems': list(whitelist.keys())
    })

@app.route('/save_validator_whitelist', methods=['POST'])
def save_validator_whitelist():
    """Save (replace) the whitelist for a specific system."""
    try:
        data = request.json or {}
        system_name = data.get('system_name', '').strip()
        commands = data.get('commands', [])
        if not isinstance(commands, list):
            return jsonify({'status': 'error', 'message': 'commands must be a list'}), 400
        whitelist = agent_core._load_validator_whitelist()
        cleaned = [c.strip() for c in commands if c.strip()]
        if cleaned:
            whitelist[system_name] = cleaned
        elif system_name in whitelist:
            del whitelist[system_name]
        agent_core._save_validator_whitelist(whitelist)
        return jsonify({'status': 'success', 'count': len(cleaned)})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/export_connections')
def export_connections():
    """Export saved connections as JSON file for download."""
    try:
        connections = session_manager.load_connections()
        response = jsonify(connections)
        response.headers['Content-Disposition'] = 'attachment; filename=connections.json'
        response.headers['Content-Type'] = 'application/json'
        return response
    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/import_connections', methods=['POST'])
def import_connections():
    """Import connections from uploaded JSON. Validates format before saving."""
    try:
        data = request.json
        connections = data.get('connections', [])

        # Validate: must be a list
        if not isinstance(connections, list):
            return jsonify({'status': 'error', 'message': 'Invalid format: expected array'}), 400

        # Validate each connection
        required_fields = ['ip', 'username']
        for i, conn in enumerate(connections):
            if not isinstance(conn, dict):
                return jsonify({'status': 'error', 'message': f'Invalid connection at index {i}: not an object'}), 400
            for field in required_fields:
                if field not in conn or not isinstance(conn[field], str) or not conn[field].strip():
                    return jsonify({'status': 'error', 'message': f'Invalid connection at index {i}: missing or invalid "{field}"'}), 400

        # Normalize connections (ensure all have expected fields)
        normalized = []
        for conn in connections:
            normalized.append({
                'name': conn.get('name', f"{conn['username']}@{conn['ip']}"),
                'ip': conn['ip'].strip(),
                'username': conn['username'].strip(),
                'port': conn.get('port', 22),
                'ssh_key_path': conn.get('ssh_key_path', '/app/keys/id_rsa'),
                'added_at': conn.get('added_at', datetime.now().isoformat())
            })

        # Save the imported connections (replaces existing)
        session_manager.save_connections(normalized)

        return jsonify({'status': 'success', 'count': len(normalized)})
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

                # --- Remove non-serializable keys that may have leaked into saved state ---
                for key in ('log_manager', 'chat_llm', 'knowledge_manager', 'web_search_status'):
                    if key in loaded_state_data:
                        del loaded_state_data[key]

                GLOBAL_STATE.update(loaded_state_data)
                print("Global State memory updated from session file.")

                # 3. Update Log Manager RAM
                if 'log_manager' not in GLOBAL_STATE or not hasattr(GLOBAL_STATE['log_manager'], 'reload_state'):
                    print("Log Manager instance missing or invalid. Re-initializing...")
                    initialize_log_system()

                log_manager = GLOBAL_STATE.get('log_manager')
                if log_manager:
                    log_manager.reload_state()

                # 4. Re-initialize subsystems from restored config
                print("[SESSION RESTORE] Re-initializing subsystems...")
                initialize_ssh_status()
                initialize_llm_status()
                initialize_knowledge_manager()

                # 5. Update last_logged_system from restored config
                cfg = get_config()
                restored_ip = cfg.get('System', 'ip_address', fallback='').strip()
                restored_user = cfg.get('System', 'username', fallback='').strip()
                restored_name = cfg.get('System', 'system_name', fallback='').strip()
                if restored_ip and restored_user:
                    GLOBAL_STATE['last_logged_system'] = {
                        'username': restored_user,
                        'ip': restored_ip,
                        'name': restored_name
                    }
                    GLOBAL_STATE['system_ip'] = restored_ip
                    GLOBAL_STATE['system_username'] = restored_user
                    GLOBAL_STATE['system_name'] = restored_name
                print("[SESSION RESTORE] Subsystems re-initialized successfully.")

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

    if mode == 'system_messages':
        data['task_completed'] = cfg.get('SystemMessages', 'task_completed', fallback='')
        data['search_completed'] = cfg.get('SystemMessages', 'search_completed', fallback='')
        data['knowledge_completed'] = cfg.get('SystemMessages', 'knowledge_completed', fallback='')
        data['switch_timeout'] = cfg.get('SystemMessages', 'switch_timeout', fallback='')
        data['switch_approved'] = cfg.get('SystemMessages', 'switch_approved', fallback='')
        data['switch_denied'] = cfg.get('SystemMessages', 'switch_denied', fallback='')
        data['web_search_completed'] = cfg.get('SystemMessages', 'web_search_completed', fallback='')
        data['web_search_completed_injected'] = cfg.get('SystemMessages', 'web_search_completed_injected', fallback='')
        data['web_search_completed_attached'] = cfg.get('SystemMessages', 'web_search_completed_attached', fallback='')
    elif mode == 'websearch_injection':
        data['websearch_injection'] = cfg.get('WebSearchInjection', 'template', fallback='')
    elif mode == 'report_validator':
        data['ollama_prompt'] = cfg.get('OllamaValidateReportPrompt', 'template', fallback='')
        data['cloud_prompt'] = cfg.get('CloudValidateReportPrompt', 'template', fallback='')
    elif mode == 'knowledge':
        knowledge_prompt = cfg.get('KnowledgePrompt', 'template', fallback='')
        data['knowledge_prompt'] = knowledge_prompt
    elif mode == 'chat':
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

        if mode == 'system_messages':
            if not cfg.has_section('SystemMessages'): cfg.add_section('SystemMessages')
            for key in ['task_completed', 'search_completed', 'knowledge_completed', 'switch_timeout', 'switch_approved', 'switch_denied', 'web_search_completed', 'web_search_completed_injected', 'web_search_completed_attached']:
                value = request.form.get(key, '')
                cfg.set('SystemMessages', key, value)

        elif mode == 'websearch_injection':
            ws_injection = request.form.get('websearch_injection', '')
            if not cfg.has_section('WebSearchInjection'): cfg.add_section('WebSearchInjection')
            cfg.set('WebSearchInjection', 'template', ws_injection)

        elif mode == 'report_validator':
            ollama_prompt = request.form.get('ollama_prompt', '')
            cloud_prompt = request.form.get('cloud_prompt', '')
            if not cfg.has_section('OllamaValidateReportPrompt'): cfg.add_section('OllamaValidateReportPrompt')
            cfg.set('OllamaValidateReportPrompt', 'template', ollama_prompt)
            if not cfg.has_section('CloudValidateReportPrompt'): cfg.add_section('CloudValidateReportPrompt')
            cfg.set('CloudValidateReportPrompt', 'template', cloud_prompt)

        elif mode == 'knowledge':
            knowledge_prompt = request.form.get('knowledge_prompt', '')
            # Validate: must contain {documents}
            if '{documents}' not in knowledge_prompt:
                return jsonify({'status': 'error', 'message': 'Knowledge template must contain {documents} variable.'}), 400

            if not cfg.has_section('KnowledgePrompt'): cfg.add_section('KnowledgePrompt')
            cfg.set('KnowledgePrompt', 'template', knowledge_prompt)

        elif mode == 'chat':
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
            'OllamaValidateReportPrompt',
            'CloudValidateReportPrompt',
            'OllamaSummarizePrompt',
            'CloudSummarizePrompt',
            'OllamaStepSummaryPrompt',
            'CloudStepSummaryPrompt',
            'OllamaSearchSummaryPrompt',
            'CloudSearchSummaryPrompt',
            'KnowledgePrompt',
            'WebSearchPrompt',
            'WebSearchInjection'
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

@app.route('/export_chat', methods=['POST'])
def export_chat():
    """Export selected chat messages as PDF or DOCX with markdown formatting."""
    try:
        data = request.json
        messages = data.get('messages', [])
        format_type = data.get('format', 'pdf')
        theme = data.get('theme', 'dark')

        if not messages:
            return jsonify({'status': 'error', 'message': 'No messages to export'}), 400

        if format_type not in ('pdf', 'docx'):
            return jsonify({'status': 'error', 'message': 'Invalid format. Use pdf or docx.'}), 400

        if theme not in ('dark', 'light'):
            return jsonify({'status': 'error', 'message': 'Invalid theme. Use dark or light.'}), 400

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        if format_type == 'pdf':
            buffer = chat_export.generate_pdf(messages, theme)
            return send_file(
                buffer,
                mimetype='application/pdf',
                as_attachment=True,
                download_name=f'chat_export_{timestamp}.pdf'
            )
        else:
            buffer = chat_export.generate_docx(messages, theme)
            return send_file(
                buffer,
                mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                as_attachment=True,
                download_name=f'chat_export_{timestamp}.docx'
            )

    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': f'Export failed: {e}'}), 500

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

def _is_chat_processing():
    """Check if the chat LLM is currently processing a message."""
    try:
        from agent_core import _chat_processing_lock
        return _chat_processing_lock.locked()
    except Exception:
        return False

@socketio.on('connect')
def handle_connect(auth=None):
    """Gestioneaza o noua conexiune client (ex: deschiderea paginii, refresh)."""
    global GLOBAL_STATE
    CONNECTED_UI_CLIENTS.add(request.sid)
    print(f"Client connected: {request.sid}")
    
    # Trimite starea *curenta* (inclusiv din task-ul care ruleaza)
    try:
        # Flush write buffer before reading state — ensures reconnecting browser
        # gets the complete log even if a buffered write hasn't hit disk yet.
        log_manager_for_flush = GLOBAL_STATE.get('log_manager')
        if log_manager_for_flush:
            log_manager_for_flush.flush()

        # Always filter the log for initial display — parse_command_log extracts
        # the relevant lines (STEP, REASON, COMMAND, ERROR, etc.) from the raw log
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
            'chat_history': chat_history,
            # Session snapshot: UI state sync
            'current_objective': GLOBAL_STATE.get('current_objective', ''),
            'current_execution_mode': GLOBAL_STATE.get('current_execution_mode', 'independent'),
            'current_allow_ask_mode': GLOBAL_STATE.get('current_allow_ask_mode', False),
            'current_summarization_mode': GLOBAL_STATE.get('current_summarization_mode', 'automatic'),
            'command_timeout': GLOBAL_STATE.get('command_timeout', 300)
        }
        socketio.emit('initial_state', initial_data, to=request.sid)
        
        # Trimitem si statusurile curente
        socketio.emit('ssh_status_update', GLOBAL_STATE['ssh_connection_status'], to=request.sid)
        socketio.emit('llm_status_update', GLOBAL_STATE['llm_connection_status'], to=request.sid)

        # Re-emit pending modals if agent is waiting for user interaction
        # Skip switch proposals when in API mode — the API client handles them via HTTP polling
        pending_switch = GLOBAL_STATE.get('_pending_switch_target')
        if pending_switch and not GLOBAL_STATE.get('api_ui_locked'):
            # _pending_switch_target is now a dict {target_system, reason} or legacy string
            if isinstance(pending_switch, dict):
                ps_target = pending_switch.get('target_system', '')
                ps_reason = pending_switch.get('reason', '')
            else:
                ps_target = pending_switch
                ps_reason = ''
            print(f"[CONNECT] Re-emitting pending switch proposal for: {ps_target}", flush=True)
            socketio.emit('chat_switch_proposal', {'target_system': ps_target, 'reason': ps_reason}, to=request.sid)
            socketio.emit('chat_status', {'status': f'awaiting approval for switch to {ps_target}'}, to=request.sid)

        pending_ask = GLOBAL_STATE.get('_pending_ask_data')
        if pending_ask:
            print(f"[CONNECT] Re-emitting pending ASK modal", flush=True)
            socketio.emit('awaiting_user_answer', pending_ask, to=request.sid)

        pending_approval = GLOBAL_STATE.get('_pending_approval_data')
        if pending_approval:
            print(f"[CONNECT] Re-emitting pending command approval modal", flush=True)
            socketio.emit('awaiting_command_approval', pending_approval, to=request.sid)

        pending_task = GLOBAL_STATE.get('_pending_task_proposal')
        if pending_task and not GLOBAL_STATE.get('api_ui_locked'):
            print(f"[CONNECT] Re-emitting pending task proposal", flush=True)
            socketio.emit('chat_task_proposal', {'objective': pending_task}, to=request.sid)

        # Reset chat status to idle if no chat is currently processing
        if not _is_chat_processing():
            socketio.emit('chat_status', {'status': 'idle'}, to=request.sid)

        # Always emit current control mode so banner shows correctly on connect/reconnect
        socketio.emit('api_session_status',
                      {'active': bool(GLOBAL_STATE.get('api_ui_locked', False))},
                      to=request.sid)

        # Emit current auto-flag state so browser checkboxes stay in sync with server
        socketio.emit('auto_flags_state', {
            'auto_accept_tasks': bool(GLOBAL_STATE.get('auto_accept_tasks', False)),
            'auto_switch': bool(GLOBAL_STATE.get('auto_switch', False))
        }, to=request.sid)

    except Exception as e:
        print(f"Error sending initial state: {e}")
        traceback.print_exc()

@socketio.on('disconnect')
def handle_disconnect():
    """Gestioneaza deconectarea clientului."""
    CONNECTED_UI_CLIENTS.discard(request.sid)
    print(f"Client disconnected: {request.sid}. Task continues if running.")

def _is_webui_active():
    """
    Return True if a human is actively controlling the WebUI (blocks API access).
    When api_ui_locked=True the browser is in read-only mode, so the API is free to operate
    regardless of how many browser clients are connected.
    When api_ui_locked=False (user took control), any connected browser blocks the API.
    """
    if GLOBAL_STATE.get('api_ui_locked'):
        return False   # Browser is watching only — API has control
    return len(CONNECTED_UI_CLIENTS) > 0


@socketio.on('take_api_control')
def handle_take_api_control():
    """WebUI user takes over from an active API chat session → releases read-only lock."""
    request_id = GLOBAL_STATE.get('api_chat_request_id')
    GLOBAL_STATE['api_chat_active'] = False
    GLOBAL_STATE['api_ui_locked'] = False
    GLOBAL_STATE['webui_watching_only'] = False
    if request_id and request_id in API_CHAT_REGISTRY:
        API_CHAT_REGISTRY[request_id]['status'] = 'interrupted'
        API_CHAT_REGISTRY[request_id]['error'] = 'WebUI user took control of the session'
    save_app_state()
    socketio.emit('api_session_status', {'active': False})
    print(f"[API CHAT] WebUI user took control — request {request_id} interrupted", flush=True)


@socketio.on('set_api_mode')
def handle_set_api_mode():
    """User clicks 'Give API Control' → switches to API mode, browser becomes read-only."""
    GLOBAL_STATE['api_ui_locked'] = True
    GLOBAL_STATE['webui_watching_only'] = True
    save_app_state()
    socketio.emit('api_session_status', {'active': True})
    print(f"[CONTROL] Switched to API mode — browser read-only", flush=True)


@socketio.on('set_watch_only')
def handle_set_watch_only():
    """Legacy alias for set_api_mode (backward compat)."""
    handle_set_api_mode()


@socketio.on('set_auto_flags')
def handle_set_auto_flags(data):
    """
    Sync auto-accept / auto-switch checkbox state from the browser to the server.
    When set, task proposals and switch proposals are approved automatically
    on the server without requiring any human or API client action.
    """
    global GLOBAL_STATE
    prev_accept = GLOBAL_STATE.get('auto_accept_tasks', False)
    prev_switch = GLOBAL_STATE.get('auto_switch', False)
    GLOBAL_STATE['auto_accept_tasks'] = bool(data.get('auto_accept_tasks', False))
    GLOBAL_STATE['auto_switch'] = bool(data.get('auto_switch', False))
    if GLOBAL_STATE['auto_accept_tasks'] != prev_accept or GLOBAL_STATE['auto_switch'] != prev_switch:
        print(f"[AUTO FLAGS] auto_accept_tasks={GLOBAL_STATE['auto_accept_tasks']}, auto_switch={GLOBAL_STATE['auto_switch']}", flush=True)
    save_app_state()


# ============================================================================
# SYNCHRONOUS API ENDPOINT FOR SSH EXECUTION
# ============================================================================
# This endpoint allows external systems to trigger SSH tasks and wait for completion.
# It's designed as a foundation for building hybrid agents and automation pipelines.

@app.route('/api/docs', methods=['GET'])
def api_docs():
    """Returns full API documentation and application usage guide."""
    cfg = get_config()
    current_system = GLOBAL_STATE.get('system_name') or cfg.get('System', 'system_name', fallback='')
    app_version = "2026-04"

    docs = {
        "application": {
            "name": "AI Agent Controller",
            "version": app_version,
            "description": (
                "Web-based system for executing commands on remote systems via SSH "
                "using LLM-powered autonomous agents. Features: autonomous execution, "
                "chat interface, knowledge base (RAG), web search, multi-system support, "
                "network device support (Cisco/Juniper/Arista/Brocade)."
            ),
            "port": 5001,
            "current_system": current_system,
            "web_ui": "http://<host>:5001/",
            "docker_container": "ai-agent",
            "persistent_volume": "/app/keys/  (config.ini, connections.json, knowledge/, session.json)"
        },

        "architecture": {
            "execution_llm": "Runs SSH commands autonomously step by step until task complete or max_steps reached",
            "chat_llm": "Separate conversational LLM — can be different model/server than execution LLM",
            "web_search_llm": "Autonomous DuckDuckGo search pipeline with dedicated LLM",
            "knowledge_base": "Documents uploaded by user → embedded (vectorized) → retrieved via semantic search",
            "dual_log": "Full immutable log + summarized LLM context (auto-summarized when threshold reached)",
            "modes": {
                "independent": "Agent runs autonomously until REPORT or max_steps",
                "assisted": "Agent proposes each command, user approves before execution"
            }
        },

        "agent_actions": {
            "description": "Actions the execution LLM can use in its responses",
            "actions": {
                "COMMAND: <cmd>": "Execute SSH command on remote system",
                "BLOCK: / CONFIG:": "Multi-line interactive session (for network devices: conf t, interface, etc.)",
                "SRCH: <query>": "Search execution history (full log, case-insensitive substring)",
                "KNOWLEDGE: <query>": "Vector similarity search in uploaded knowledge documents",
                "WRITE_FILE: <path>": "Create/write file on remote system",
                "ASK: <question>": "Request human input (only if ASK mode enabled)",
                "TIMEOUT: <seconds>": "Adjust command timeout for next step (clamped to UI max)",
                "REPORT: <text>": "Final task report — ends the task"
            }
        },

        "chat_actions": {
            "description": "Actions and tags the Chat LLM can use",
            "actions": {
                "SRCH: <query>": "Search execution history — results injected back into chat context",
                "KNOWLEDGE: <query>": "Vector search in knowledge documents",
                "WEB_SEARCH: <query>": "Autonomous web research (requires REASON: line before it)",
                "<<REQUEST_TASK: objective>>": "Propose a new execution task to the user",
                "<<SWITCH_SYSTEM: name>>": "Request switching to another configured system",
                "<<MARK_STEP_COMPLETED: X>>": "Mark step X of action plan as done",
                "<<ACTION_PLAN_START>>..<<ACTION_PLAN_STOP>>": "Create a multi-step action plan"
            }
        },

        "api_endpoints": {
            "execution": {
                "POST /api/execute_ssh": {
                    "description": "Start an autonomous SSH task asynchronously",
                    "request_body": {
                        "objective": "(string, required) Task description for the agent",
                        "system_name": "(string, optional) Target system alias — uses current if omitted",
                        "allow_ask": "(bool, optional) Allow agent to use ASK: action. Default: false"
                    },
                    "response": {
                        "status": "success | error",
                        "task_id": "UUID string — use for polling",
                        "message": "Human-readable status"
                    },
                    "example_request": {
                        "objective": "Check disk usage on all partitions and report any over 80%",
                        "system_name": "Production Server",
                        "allow_ask": False
                    },
                    "example_response": {
                        "status": "success",
                        "task_id": "a1b2c3d4-...",
                        "message": "Task started"
                    }
                },
                "GET /api/task_status/<task_id>": {
                    "description": "Poll the status of a running or completed task",
                    "response": {
                        "status": "running | completed | failed | stopped",
                        "current_step": "int — current step number",
                        "latest_activity": "string — last log line",
                        "result": "string | null — final REPORT text when completed",
                        "start_time": "ISO timestamp"
                    }
                },
                "POST /api/stop": {
                    "description": "Stop the currently running task",
                    "response": {"status": "success | error", "message": "..."}
                },
                "GET /api/status": {
                    "description": "Health check — returns app state",
                    "response": {
                        "status": "ok",
                        "task_running": "bool",
                        "current_system": "string",
                        "llm_status": "object"
                    }
                }
            },
            "systems": {
                "GET /api/list_systems": {
                    "description": "List all configured systems and which one is active",
                    "response": {
                        "systems": ["list of system names/aliases"],
                        "current_system": "active system name"
                    }
                },
                "POST /api/switch_system": {
                    "description": "Switch the active SSH target system",
                    "request_body": {
                        "system_name": "(string, required) Name/alias of target system"
                    },
                    "response": {"status": "success | error", "message": "..."}
                }
            },
            "chat": {
                "POST /api/chat/message": {
                    "description": "Send a message to the Chat LLM",
                    "request_body": {
                        "message": "(string, required) User message",
                        "request_id": "(string, optional) ID for polling status"
                    },
                    "response": {
                        "status": "queued | processing | completed | error",
                        "request_id": "string",
                        "response": "string (if completed synchronously)"
                    }
                },
                "GET /api/chat/status/<request_id>": {
                    "description": "Poll the status of a chat message",
                    "response": {
                        "status": "queued | processing | completed | failed",
                        "response": "string — agent reply when completed"
                    }
                },
                "POST /api/chat/approve/<request_id>": {
                    "description": "Approve or deny a pending agent action (ASK, system switch, command approval)",
                    "request_body": {
                        "approved": "(bool, required) true to approve, false to deny",
                        "answer": "(string, optional) Text answer for ASK actions"
                    }
                },
                "GET /api/chat/capabilities": {
                    "description": "Returns what the chat agent supports in current configuration",
                    "response": {
                        "knowledge_enabled": "bool",
                        "web_search_enabled": "bool",
                        "ask_enabled": "bool",
                        "available_systems": ["list"]
                    }
                },
                "POST /api/set_control_mode": {
                    "description": "Set whether API or Web UI controls the chat session",
                    "request_body": {"mode": "api | web"}
                },
                "POST /api/chat/release": {
                    "description": "Release API control back to the web UI"
                }
            },
            "documentation": {
                "GET /api/docs": {
                    "description": "This endpoint — returns full API documentation and usage guide"
                }
            }
        },

        "configuration": {
            "key_sections": {
                "[General]": "provider (ollama/gemini/anthropic), API keys",
                "[Agent]": "model_name, max_steps, summarization_threshold, command_timeout, llm_timeout, temperature",
                "[Ollama]": "api_url, num_ctx (context window), keep_alive (default -1 = stay in VRAM indefinitely)",
                "[ChatLLM]": "enabled, provider, model_name, ollama_url (independent from exec LLM URL)",
                "[WebSearch]": "enabled, provider, model_name, ollama_url, search/content settings",
                "[System]": "ip_address, username, ssh_port, auth_method, device_type, system_name, enable_password"
            },
            "auth_methods": ["key", "password", "key_or_password"],
            "device_types": ["linux", "windows", "cisco", "nxos", "iosxr", "iosxe", "brocade", "juniper", "arista", "other"],
            "keep_alive_values": {
                "-1": "Model stays in VRAM indefinitely (recommended for low-latency)",
                "30m": "30 minutes",
                "1h": "1 hour",
                "0": "Unload immediately after each request"
            }
        },

        "knowledge_base": {
            "description": "Upload documents → auto-embedded + LLM summary → injected into every prompt as index",
            "how_it_works": (
                "All documents are vectorized. A ≤900-char LLM-generated summary per document "
                "is injected into every prompt (currently ~7400 chars total for 8 docs vs old 51879 chars — 86% reduction). "
                "Agent uses KNOWLEDGE: <query> to retrieve full relevant chunks via semantic similarity search."
            ),
            "supported_formats": ["txt", "md", "json", "csv", "pdf", "docx", "png", "jpg", "gif", "bmp", "tiff (OCR via Tesseract)"],
            "vector_stores": ["ChromaDB (persistent)", "FAISS (manual persistence)"],
            "source_labels": {
                "USER": "Manually uploaded documents",
                "WEB": "Documents attached automatically by web search module"
            }
        },

        "web_search": {
            "description": "Autonomous 5-step web research pipeline triggered by Chat LLM",
            "pipeline": [
                "1. LLM optimizes search query",
                "2. DuckDuckGo search",
                "3. LLM ranks results",
                "4. Page fetch + relevance check (trafilatura → beautifulsoup4 fallback)",
                "5. LLM generates summary → injected into chat context or attached to knowledge base"
            ],
            "trigger_format": "REASON: <why you need this>\nWEB_SEARCH: <search query>"
        },

        "debugging": {
            "check_logs": "docker logs ai-agent --tail 50",
            "restart": "docker restart ai-agent",
            "syntax_check": "python3 -m py_compile app.py agent_core.py ssh_utils.py config.py session_manager.py llm_utils.py log_manager.py knowledge_manager.py web_search_module.py chat_export.py",
            "common_issues": {
                "chat_srch_loop": "Model outputs SRCH + inline answer in same response — code detects inline answer >150 chars and skips re-search",
                "ollama_500_error": "kimi-k2:1t-cloud server-side issue — 5 retries with progressive temperature bump handle it",
                "model_not_loading": "Set keep_alive=-1 in [Ollama] config or Advanced Settings in UI",
                "wrong_chat_llm_url": "ChatLLM has its own ollama_url field — set separately from exec LLM URL",
                "modal_not_appearing": "Check PENDING_ASK_DATA / PENDING_APPROVAL_DATA globals in app.py + socketio.sleep(0.1) flush",
                "request_task_truncated": "REQUEST_TASK regex searches original response_text (not backtick-stripped copy)",
                "multiple_commands": "LLM generates 2 COMMAND: lines — only first executed, skipped ones injected as SYSTEM NOTE for next step"
            }
        }
    }

    return jsonify(docs)


@app.route('/api/execute_ssh', methods=['POST'])
def api_execute_ssh():
    """
    Asynchronous API endpoint for SSH task execution.

    Accepts an objective, starts execution in the background, and immediately
    returns a task_id for status polling.

    Request Body:
        {
            "objective": "string - The task objective to execute",
            "system_name": "string - Name or connection string of target system (optional)",
            "mode": "independent|assisted" (optional, default: independent)
        }

    Response:
        {
            "status": "queued",
            "task_id": "uuid",
            "objective": "string",
            "system": { "name": "...", "connection": "..." },
            "info": "Task started. Check status at /api/task_status/<task_id>"
        }
    """
    global GLOBAL_STATE, USER_RESPONSE, USER_ANSWER

    # Parse request
    data = request.json or {}
    objective = data.get('objective', '').strip()
    system_name = data.get('system_name', '').strip()  # Optional: target specific system
    execution_mode = data.get('mode', 'independent')

    # Validate input
    if not objective:
        return jsonify({
            'status': 'error',
            'message': 'No objective provided. Please include "objective" in request body.'
        }), 400

    if execution_mode not in ['independent', 'assisted']:
        return jsonify({
            'status': 'error',
            'message': 'Invalid mode. Must be "independent" or "assisted".'
        }), 400

    # Block execution when WebUI has control — same guard as /api/chat/message
    if _is_webui_active():
        return jsonify({
            'error': 'WebUI active — control blocked by human intervention',
            'code': 'WEBUI_ACTIVE',
            'hint': 'Click "Give API Control" in the browser banner to grant API access'
        }), 423

    # Atomic check-then-reserve: prevents two concurrent API calls from both starting a task.
    # We set task_running=True immediately under the lock to claim the slot.
    # If any subsequent setup step fails we reset it before returning the error.
    with TASK_START_LOCK:
        if GLOBAL_STATE['task_running']:
            return jsonify({
                'status': 'error',
                'message': 'A task is already running. Please wait for it to complete or stop it first.'
            }), 409  # Conflict
        GLOBAL_STATE['task_running'] = True  # Reserve the slot atomically

    # === SYSTEM TARGETING: Switch to specified system if provided ===
    target_system_info = None
    if system_name:
        # Load saved connections and find the target system
        connections = session_manager.load_connections()
        target_system = None

        for conn in connections:
            conn_name = conn.get('name', '')
            conn_string = f"{conn.get('username', '')}@{conn.get('ip', '')}"

            if system_name == conn_name or system_name == conn_string:
                target_system = conn
                break

        if not target_system:
            GLOBAL_STATE['task_running'] = False  # Release reserved slot
            return jsonify({
                'status': 'error',
                'message': f"System not found: {system_name}. Use /api/list_systems to see available systems."
            }), 404

        # Update config.ini with the target system
        cfg = get_config()
        cfg.set('System', 'ip_address', target_system.get('ip', ''))
        cfg.set('System', 'username', target_system.get('username', ''))
        cfg.set('System', 'ssh_port', str(target_system.get('port', 22)))
        cfg.set('System', 'system_name', target_system.get('name', ''))

        with open(CONFIG_FILE_PATH, 'w') as f:
            cfg.write(f)

        # Update GLOBAL_STATE
        GLOBAL_STATE['system_ip'] = target_system.get('ip', '')
        GLOBAL_STATE['system_username'] = target_system.get('username', '')
        GLOBAL_STATE['system_name'] = target_system.get('name', '')

        # Test SSH connection to the new system
        initialize_ssh_status()

        if GLOBAL_STATE['ssh_connection_status'].get('status') != 'success':
            GLOBAL_STATE['task_running'] = False  # Release reserved slot
            return jsonify({
                'status': 'error',
                'message': f"Failed to connect to system '{system_name}': {GLOBAL_STATE['ssh_connection_status'].get('message', 'Unknown error')}"
            }), 503  # Service Unavailable

        target_system_info = {
            'name': target_system.get('name', f"{target_system.get('username')}@{target_system.get('ip')}"),
            'connection': f"{target_system.get('username')}@{target_system.get('ip')}",
            'ip': target_system.get('ip'),
            'username': target_system.get('username')
        }

        # Broadcast system change to UI
        socketio.emit('system_changed', target_system_info)
        socketio.emit('ssh_status_update', GLOBAL_STATE['ssh_connection_status'])

        print(f"[API] Switched to system: {target_system_info['name']}")

    # If no specific system was targeted, get current system from config
    if not target_system_info:
        target_system_info = {
            'name': GLOBAL_STATE.get('system_name', '') or f"{GLOBAL_STATE.get('system_username', '')}@{GLOBAL_STATE.get('system_ip', '')}",
            'connection': f"{GLOBAL_STATE.get('system_username', '')}@{GLOBAL_STATE.get('system_ip', '')}",
            'ip': GLOBAL_STATE.get('system_ip', ''),
            'username': GLOBAL_STATE.get('system_username', '')
        }

    # === GENERATE TASK ID FOR ASYNC TRACKING ===
    task_id = str(uuid.uuid4())

    # Initialize task registry entry
    agent_core.API_TASK_REGISTRY[task_id] = {
        'status': 'queued',
        'start_time': time.time(),
        'objective': objective,
        'system': target_system_info,
        'current_step': 0,
        'latest_activity': 'Queued for execution...',
        'result': None,
        'last_updated': time.time()
    }

    # Clear the previous report to ensure we get fresh results
    GLOBAL_STATE['last_session']['final_report'] = None

    # Set up execution state (task_running already set to True under TASK_START_LOCK above)
    GLOBAL_STATE['current_objective'] = objective
    GLOBAL_STATE['current_execution_mode'] = execution_mode
    GLOBAL_STATE['current_summarization_mode'] = 'automatic'
    GLOBAL_STATE['current_allow_ask_mode'] = False  # API mode doesn't support ASK
    GLOBAL_STATE['task_paused'] = False

    # Reset communication variables
    USER_RESPONSE.clear()
    USER_ANSWER.clear()

    # Load system info from config
    cfg = get_config()
    GLOBAL_STATE['system_username'] = cfg.get('System', 'username', fallback='unknown')
    GLOBAL_STATE['system_ip'] = cfg.get('System', 'ip_address', fallback='unknown')

    # Mark this as an API-triggered task for broadcast logic
    GLOBAL_STATE['is_api_task'] = True
    GLOBAL_STATE['current_task_id'] = task_id  # Store task_id for reference

    # === LIVE VIEW: Broadcast to all connected UI clients ===
    socketio.emit('external_objective_update', {'objective': objective})
    socketio.emit('api_task_started', {
        'objective': objective,
        'mode': execution_mode,
        'source': 'api',
        'task_id': task_id
    })
    socketio.emit('task_started')

    # Start the agent in a background thread (non-blocking)
    print(f"[API] Starting async SSH task (ID: {task_id}): {objective[:100]}...")
    socketio.start_background_task(
        run_agent_and_update_state,
        socketio,
        GLOBAL_STATE,
        CONTROL_FLAGS,
        EVENT_OBJECTS,
        task_id  # Pass task_id for registry updates
    )

    # Return immediately with task_id
    return jsonify({
        'status': 'queued',
        'task_id': task_id,
        'objective': objective,
        'system': target_system_info,
        'info': f'Task started. Check status at /api/task_status/{task_id}'
    }), 202  # Accepted


@app.route('/api/task_status/<task_id>', methods=['GET'])
def api_task_status(task_id):
    """
    Returns the status and progress of an async task.

    Response (running):
        {
            "status": "running",
            "current_step": 3,
            "latest_activity": "[Executing command] Checking disk space...",
            "duration_seconds": 45.2,
            "objective": "...",
            "system": {...}
        }

    Response (completed):
        {
            "status": "completed",
            "result": "Final report text...",
            "current_step": 5,
            "duration_seconds": 120.5,
            "objective": "...",
            "system": {...}
        }

    Response (failed/stopped):
        {
            "status": "failed|stopped",
            "result": "Error message or stopped reason",
            "current_step": 2,
            "duration_seconds": 30.1,
            "objective": "...",
            "system": {...}
        }
    """
    # Check if task exists
    if task_id not in agent_core.API_TASK_REGISTRY:
        return jsonify({
            'status': 'error',
            'message': f'Task not found: {task_id}'
        }), 404

    task_info = agent_core.API_TASK_REGISTRY[task_id]

    # Calculate duration
    duration = time.time() - task_info.get('start_time', time.time())

    # Build response
    response = {
        'status': task_info.get('status', 'unknown'),
        'current_step': task_info.get('current_step', 0),
        'latest_activity': task_info.get('latest_activity', 'Unknown'),
        'duration_seconds': round(duration, 2),
        'objective': task_info.get('objective', ''),
        'system': task_info.get('system', {})
    }

    # Include result if task is finished
    if task_info.get('result'):
        response['result'] = task_info['result']

    # Include auto-analyze request_id when available (set by _auto_analyze_task_result)
    # The client should poll GET /api/chat/status/<auto_analyze_request_id> until completed,
    # then send the next message (avoids 409 from hitting /api/chat/message too early).
    if task_info.get('auto_analyze_request_id'):
        response['auto_analyze_request_id'] = task_info['auto_analyze_request_id']

    return jsonify(response)


@app.route('/api/status', methods=['GET'])
def api_status():
    """
    Returns the current status of the SSH agent.

    Response:
        {
            "task_running": bool,
            "task_paused": bool,
            "current_objective": string or null,
            "ssh_connected": bool,
            "llm_connected": bool
        }
    """
    return jsonify({
        'task_running': GLOBAL_STATE.get('task_running', False),
        'task_paused': GLOBAL_STATE.get('task_paused', False),
        'current_objective': GLOBAL_STATE.get('current_objective', '') or None,
        'ssh_status': GLOBAL_STATE.get('ssh_connection_status', {}).get('status', 'unknown'),
        'llm_status': GLOBAL_STATE.get('llm_connection_status', {}).get('status', 'unknown')
    })


@app.route('/api/stop', methods=['POST'])
def api_stop():
    """
    Stops the currently running task.

    Response:
        {
            "status": "success|error",
            "message": string
        }
    """
    if not GLOBAL_STATE.get('task_running', False):
        return jsonify({
            'status': 'error',
            'message': 'No task is currently running.'
        }), 400

    # Stop the task
    GLOBAL_STATE['task_running'] = False
    GLOBAL_STATE['task_paused'] = False
    ssh_utils.abort_active_connection()

    return jsonify({
        'status': 'success',
        'message': 'Task stop signal sent.'
    })


@app.route('/api/list_systems', methods=['GET'])
def api_list_systems():
    """
    Returns a list of all saved remote systems.

    Response:
        {
            "status": "success",
            "systems": [
                {"name": "Production Server", "connection": "root@192.168.1.50", "port": 22},
                {"name": "Test Server", "connection": "ubuntu@10.0.0.5", "port": 22}
            ],
            "current_system": {
                "name": "Production Server",
                "connection": "root@192.168.1.50"
            }
        }
    """
    try:
        # Load saved connections
        connections = session_manager.load_connections()

        # Get current system from config
        cfg = get_config()
        current_ip = cfg.get('System', 'ip_address', fallback='')
        current_user = cfg.get('System', 'username', fallback='')
        current_name = cfg.get('System', 'system_name', fallback='')

        # Build systems list
        systems = []
        for conn in connections:
            system_entry = {
                'name': conn.get('name', f"{conn.get('username', '')}@{conn.get('ip', '')}"),
                'connection': f"{conn.get('username', '')}@{conn.get('ip', '')}",
                'ip': conn.get('ip', ''),
                'username': conn.get('username', ''),
                'port': conn.get('port', 22)
            }
            systems.append(system_entry)

        # Build current system info
        current_system = None
        if current_ip and current_user:
            current_system = {
                'name': current_name or f"{current_user}@{current_ip}",
                'connection': f"{current_user}@{current_ip}",
                'ip': current_ip,
                'username': current_user
            }

        return jsonify({
            'status': 'success',
            'systems': systems,
            'current_system': current_system
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/api/switch_system', methods=['POST'])
def api_switch_system():
    """
    Switch to a different remote system by name or connection string.

    Request Body:
        {
            "system_name": "Production Server"  // Name or connection string (user@ip)
        }

    Response:
        {
            "status": "success",
            "message": "Switched to system: Production Server",
            "system": { "name": "...", "connection": "...", "ip": "...", "username": "..." }
        }
    """
    try:
        data = request.json or {}
        system_identifier = data.get('system_name', '').strip()

        if not system_identifier:
            return jsonify({
                'status': 'error',
                'message': 'system_name is required'
            }), 400

        # Load saved connections
        connections = session_manager.load_connections()

        # Find the system by name or connection string
        target_system = None
        for conn in connections:
            conn_name = conn.get('name', '')
            conn_string = f"{conn.get('username', '')}@{conn.get('ip', '')}"

            if system_identifier == conn_name or system_identifier == conn_string:
                target_system = conn
                break

        if not target_system:
            return jsonify({
                'status': 'error',
                'message': f"System not found: {system_identifier}"
            }), 404

        # IMPROVED: Get previous connection info BEFORE updating (from config.ini as fallback)
        cfg = get_config()
        previous_username = cfg.get('System', 'username', fallback='') or GLOBAL_STATE.get('system_username', '')
        previous_ip = cfg.get('System', 'ip_address', fallback='') or GLOBAL_STATE.get('system_ip', '')
        previous_name = cfg.get('System', 'system_name', fallback='') or GLOBAL_STATE.get('system_name', '')

        # Extract new connection info
        new_ip = target_system.get('ip', '')
        new_username = target_system.get('username', '')
        new_name = target_system.get('name', f"{new_username}@{new_ip}")

        # Update config.ini with the new system
        cfg.set('System', 'ip_address', new_ip)
        cfg.set('System', 'username', new_username)
        cfg.set('System', 'ssh_port', str(target_system.get('port', 22)))
        cfg.set('System', 'system_name', new_name)

        with open(CONFIG_FILE_PATH, 'w') as f:
            cfg.write(f)

        # Update GLOBAL_STATE
        GLOBAL_STATE['system_ip'] = new_ip
        GLOBAL_STATE['system_username'] = new_username
        GLOBAL_STATE['system_name'] = new_name

        # Drop persistent connection — next command will connect to the new system
        ssh_utils.close_persistent_client()

        # Test the new connection
        initialize_ssh_status()

        # Log SSH connection change to Full Log AND LLM Context
        ssh_status = GLOBAL_STATE.get('ssh_connection_status', {})
        if ssh_status.get('status') == 'success':
            log_manager = GLOBAL_STATE.get('log_manager')
            if log_manager:
                log_manager.log_ssh_connection_change(new_username, new_ip, previous_username, previous_ip, new_name)
                # Track last logged system to prevent duplicate entries from agent_core
                GLOBAL_STATE['last_logged_system'] = {'username': new_username, 'ip': new_ip, 'name': new_name}
                if previous_ip and previous_username:
                    print(f"[API SWITCH] Connection change logged: {previous_name or previous_username}@{previous_ip} -> {new_name}", flush=True)
                else:
                    print(f"[API SWITCH] Connection logged: {new_name} (first connection)", flush=True)

        # Detect OS, user, sudo on the new system
        if ssh_status.get('status') == 'success':
            agent_core.detect_system_info(GLOBAL_STATE)

        # Broadcast system change to all connected UI clients
        system_info = {
            'name': new_name,
            'connection': f"{new_username}@{new_ip}",
            'ip': new_ip,
            'username': new_username
        }
        socketio.emit('system_changed', system_info)
        socketio.emit('ssh_status_update', GLOBAL_STATE['ssh_connection_status'])

        return jsonify({
            'status': 'success',
            'message': f"Switched to system: {system_info['name']}",
            'system': system_info,
            'ssh_status': GLOBAL_STATE['ssh_connection_status']
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

# ============================================================================

# --- Wrapper pentru Task-ul Agentului ---

def run_agent_and_update_state(socketio, global_state, control_flags, event_objects, task_id=None):
    """
    Wrapper care ruleaza agent_task_runner si gestioneaza curatarea
    si salvarea starii la final.

    Args:
        task_id: Optional UUID for API task tracking (passed to agent_task_runner)
    """
    try:
        # Apelam functia principala din agent_core
        agent_core.agent_task_runner(socketio, global_state, control_flags, event_objects, task_id=task_id)
    except Exception as e:
        print(f"Agent task runner exception: {e}")
        traceback.print_exc()
        global_state['last_session']['final_report'] = f"Task failed with exception: {e}"
        # Update API task registry on exception
        if task_id and task_id in agent_core.API_TASK_REGISTRY:
            agent_core.API_TASK_REGISTRY[task_id].update({
                'status': 'failed',
                'result': f"Task failed with exception: {e}",
                'latest_activity': f'Exception: {type(e).__name__}',
                'last_updated': time.time()
            })
    finally:
        # Asiguram resetarea flag-urilor
        global_state['task_running'] = False
        global_state['task_paused'] = False
        socketio.emit('task_finished')

        # If task was started from an API chat session, auto-trigger chat analysis
        # (normally done by the browser via sessionStorage.isTaskFromChat, unavailable in API mode)
        # Also trigger when Telegram has active chats — browser-side SocketIO trigger won't reach it.
        _tg_has_chats = False
        try:
            from telegram_bot import get_telegram_bot as _get_tg
            _tg = _get_tg()
            _tg_has_chats = bool(_tg and _tg._active_chats)
        except Exception:
            pass

        if global_state.get('task_from_api_chat') or _tg_has_chats:
            global_state['task_from_api_chat'] = False
            print("[API CHAT] Task finished — auto-triggering chat analysis", flush=True)
            socketio.start_background_task(_auto_analyze_task_result, global_state)

        # Salvam starea
        save_app_state()

# --- Handler-e SocketIO pentru Controlul Task-ului ---

@socketio.on('execute_task')
def handle_execute_task(data):
    """Porneste executia unui task nou."""
    global GLOBAL_STATE, USER_RESPONSE, USER_ANSWER

    # Clear pending task proposal - task is being executed
    GLOBAL_STATE['_pending_task_proposal'] = None

    objective = data.get('data', '').strip()
    if not objective:
        socketio.emit('agent_log', {'data': "No objective provided."})
        return

    # Atomic check-then-set: prevents two concurrent callers from both starting a task
    with TASK_START_LOCK:
        if GLOBAL_STATE['task_running']:
            socketio.emit('agent_log', {'data': "A task is already running. Please stop it first."})
            return
        GLOBAL_STATE['task_running'] = True

    # Actualizam starea (task_running already set above under lock)
    GLOBAL_STATE['current_objective'] = objective
    GLOBAL_STATE['current_execution_mode'] = data.get('mode', 'independent')
    GLOBAL_STATE['current_summarization_mode'] = data.get('summarization_mode', 'automatic')
    GLOBAL_STATE['current_allow_ask_mode'] = data.get('allow_ask', False)
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

@socketio.on('abort_command')
def handle_abort_command():
    """Abort the currently running command (kill SSH) and pause execution."""
    global GLOBAL_STATE
    if GLOBAL_STATE['task_running']:
        # 1. Kill only the running channel — persistent connection stays alive so execution can resume
        ssh_utils.abort_active_channel()

        # 2. Pause execution (not stop — user can resume after adjustments)
        GLOBAL_STATE['task_paused'] = True
        socketio.emit('task_paused')
        socketio.emit('agent_log', {'data': "\n--- Command aborted by user. Execution paused. ---"})
        print("[ABORT] Command aborted by user. Channel closed, persistent connection kept, execution paused.", flush=True)

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
            append_live_log(log_msg)
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
            append_live_log(log_msg)

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
            append_live_log(log_msg)

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
            append_live_log(log_msg)

# --- Handler-e pentru Aprobare Comenzi si Interactiuni ---

@socketio.on('approve_command')
def handle_approve_command(data):
    """Gestioneaza aprobarea/respingerea comenzilor."""
    global USER_RESPONSE, USER_APPROVAL_EVENT
    USER_RESPONSE.clear()
    USER_RESPONSE.update(data)
    USER_APPROVAL_EVENT.set()

@socketio.on('provide_answer')
def handle_provide_answer(data):
    """Gestioneaza raspunsul utilizatorului la intrebarile agentului."""
    global USER_ANSWER, USER_ANSWER_EVENT
    USER_ANSWER.clear()
    USER_ANSWER.update(data)
    USER_ANSWER_EVENT.set()

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
    SUMMARIZATION_EVENT.set()

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

    # Clear pending task proposal - user is continuing conversation
    GLOBAL_STATE['_pending_task_proposal'] = None

    # Start a background task for the chat response
    socketio.start_background_task(
        agent_core.process_chat_message,
        socketio,
        GLOBAL_STATE,
        message
    )

@socketio.on('cancel_chat')
def handle_cancel_chat():
    """Cancel ongoing chat processing so the user can recall and edit their message."""
    print("[CHAT] User requested cancel/recall", flush=True)
    agent_core.cancel_chat_processing()

@socketio.on('clear_chat')
def handle_clear_chat():
    """Clear persistent chat history."""
    log_manager = GLOBAL_STATE.get('log_manager')
    if log_manager:
        log_manager.clear_chat_history()
    socketio.emit('chat_history_cleared')

def _do_approve_system_switch(target_system):
    """Core logic for approving a system switch. Called by both socket handler and HTTP API."""
    global GLOBAL_STATE, SYSTEM_SWITCH_RESPONSE, SYSTEM_SWITCH_EVENT

    print(f"[SYSTEM SWITCH] Approving switch to: {target_system}", flush=True)

    if not target_system:
        SYSTEM_SWITCH_RESPONSE['approved'] = False
        SYSTEM_SWITCH_RESPONSE['target_system'] = ''
        SYSTEM_SWITCH_RESPONSE['error'] = 'No target system specified'
        SYSTEM_SWITCH_EVENT.set()
        return

    connections = session_manager.load_connections()
    target_conn = None
    for conn in connections:
        conn_name = conn.get('name', f"{conn.get('username')}@{conn.get('ip')}")
        if conn_name.lower() == target_system.lower() or \
           f"{conn.get('username')}@{conn.get('ip')}".lower() == target_system.lower():
            target_conn = conn
            break

    if not target_conn:
        SYSTEM_SWITCH_RESPONSE['approved'] = False
        SYSTEM_SWITCH_RESPONSE['target_system'] = target_system
        SYSTEM_SWITCH_RESPONSE['error'] = f"System '{target_system}' not found in saved connections"
        SYSTEM_SWITCH_EVENT.set()
        return

    cfg = get_config()
    previous_username = cfg.get('System', 'username', fallback='') or GLOBAL_STATE.get('system_username', '')
    previous_ip = cfg.get('System', 'ip_address', fallback='') or GLOBAL_STATE.get('system_ip', '')
    previous_name = cfg.get('System', 'system_name', fallback='') or GLOBAL_STATE.get('system_name', '')

    new_ip = target_conn.get('ip', '')
    new_username = target_conn.get('username', '')
    new_name = target_conn.get('name', f"{new_username}@{new_ip}")

    cfg.set('System', 'ip_address', new_ip)
    cfg.set('System', 'username', new_username)
    cfg.set('System', 'ssh_port', str(target_conn.get('port', 22)))
    cfg.set('System', 'ssh_key_path', target_conn.get('ssh_key_path', '/app/keys/id_rsa'))
    cfg.set('System', 'system_name', new_name)
    with open(CONFIG_FILE_PATH, 'w') as configfile:
        cfg.write(configfile)

    GLOBAL_STATE['system_ip'] = new_ip
    GLOBAL_STATE['system_username'] = new_username
    GLOBAL_STATE['system_name'] = new_name

    ssh_utils.close_persistent_client()
    initialize_ssh_status()
    ssh_status = GLOBAL_STATE.get('ssh_connection_status', {})

    if ssh_status.get('status') == 'success':
        log_manager = GLOBAL_STATE.get('log_manager')
        if log_manager:
            log_manager.log_ssh_connection_change(new_username, new_ip, previous_username, previous_ip, new_name)
            GLOBAL_STATE['last_logged_system'] = {'username': new_username, 'ip': new_ip, 'name': new_name}
        agent_core.detect_system_info(GLOBAL_STATE)
        socketio.emit('system_changed', {
            'system_name': GLOBAL_STATE['system_name'],
            'ip': new_ip,
            'username': new_username
        })
        SYSTEM_SWITCH_RESPONSE['approved'] = True
        SYSTEM_SWITCH_RESPONSE['target_system'] = GLOBAL_STATE['system_name']
        SYSTEM_SWITCH_RESPONSE['error'] = None
        print(f"[SYSTEM SWITCH] Successfully switched to: {GLOBAL_STATE['system_name']}", flush=True)
    else:
        SYSTEM_SWITCH_RESPONSE['approved'] = False
        SYSTEM_SWITCH_RESPONSE['target_system'] = target_system
        SYSTEM_SWITCH_RESPONSE['error'] = ssh_status.get('message', 'SSH connection failed')
        print(f"[SYSTEM SWITCH] Failed to switch to {target_system}: {ssh_status.get('message')}", flush=True)

    print(f"[SYSTEM SWITCH DEBUG] About to set SYSTEM_SWITCH_EVENT (id={id(SYSTEM_SWITCH_EVENT)})", flush=True)
    SYSTEM_SWITCH_EVENT.set()
    print(f"[SYSTEM SWITCH DEBUG] SYSTEM_SWITCH_EVENT.set() called, is_set={SYSTEM_SWITCH_EVENT.is_set()}", flush=True)


def _do_deny_system_switch(target_system, denial_reason=''):
    """Core logic for denying a system switch. Called by both socket handler and HTTP API."""
    global SYSTEM_SWITCH_RESPONSE, SYSTEM_SWITCH_EVENT
    print(f"[SYSTEM SWITCH] Denying switch to: {target_system}{(' — Reason: ' + denial_reason) if denial_reason else ''}", flush=True)
    SYSTEM_SWITCH_RESPONSE['approved'] = False
    SYSTEM_SWITCH_RESPONSE['target_system'] = target_system
    SYSTEM_SWITCH_RESPONSE['error'] = f'Switch denied. Reason: {denial_reason}' if denial_reason else 'Switch denied'
    SYSTEM_SWITCH_EVENT.set()


def _auto_start_task(objective):
    """
    Start a task automatically (used when auto_accept_tasks=True).
    Mirrors the logic in api_chat_approve for task_proposal approval.
    Returns task_id on success, None if a task is already running.
    """
    global GLOBAL_STATE
    with TASK_START_LOCK:
        if GLOBAL_STATE.get('task_running'):
            print(f"[AUTO TASK] Cannot auto-start: task already running", flush=True)
            return None
        GLOBAL_STATE['task_running'] = True

    GLOBAL_STATE['current_objective'] = objective
    GLOBAL_STATE['current_execution_mode'] = 'independent'
    GLOBAL_STATE['current_summarization_mode'] = 'automatic'
    GLOBAL_STATE['current_allow_ask_mode'] = False
    GLOBAL_STATE['task_paused'] = False
    USER_RESPONSE.clear()
    USER_ANSWER.clear()
    cfg = get_config()
    GLOBAL_STATE['system_username'] = cfg.get('System', 'username', fallback='unknown')
    GLOBAL_STATE['system_ip'] = cfg.get('System', 'ip_address', fallback='unknown')
    socketio.emit('task_started')

    task_id = str(uuid.uuid4())[:8]
    from agent_core import API_TASK_REGISTRY as _ATR
    _ATR[task_id] = {
        'status': 'running', 'objective': objective, 'result': None,
        'current_step': 0, 'latest_activity': 'Starting...', 'start_time': time.time()
    }
    GLOBAL_STATE['current_api_task_id'] = task_id
    GLOBAL_STATE['task_from_api_chat'] = True   # triggers auto-analyze on completion
    GLOBAL_STATE['_api_chat_pending_type'] = None
    GLOBAL_STATE['_api_chat_pending_data'] = None
    GLOBAL_STATE['_pending_task_proposal'] = None

    socketio.start_background_task(
        run_agent_and_update_state,
        socketio, GLOBAL_STATE, CONTROL_FLAGS, EVENT_OBJECTS, task_id
    )
    # Do NOT set api_chat_active=False here — let process_chat_message's finally block
    # do it so it can mark _api_chat_status='completed' for the original request first.
    print(f"[AUTO TASK] Auto-started task: {objective[:80]}, task_id={task_id}", flush=True)
    return task_id


# Store server-side auto-approval callables so agent_core can invoke them
# without a circular import (agent_core cannot import from app).
GLOBAL_STATE['_fn_approve_switch'] = _do_approve_system_switch
GLOBAL_STATE['_fn_start_task'] = _auto_start_task


@socketio.on('approve_system_switch')
def handle_approve_system_switch(data):
    """
    Handle approved system switch request from chat.
    Sets the response and triggers the event for waiting agent_core.
    """
    _do_approve_system_switch(data.get('target_system', '').strip())

@socketio.on('deny_system_switch')
def handle_deny_system_switch(data):
    """
    Handle denied system switch request from chat.
    Sets the response and triggers the event.
    """
    _do_deny_system_switch(data.get('target_system', 'Unknown'), data.get('reason', '').strip())

def _auto_analyze_task_result(global_state):
    """
    Core logic for post-task chat analysis.
    Called directly (server-side) for API sessions, or via socket for WebUI sessions.
    Guard flag prevents double-execution when both server and browser trigger simultaneously.
    """
    if global_state.get('_auto_analyze_running'):
        print("[AUTO ANALYZE] Already running — skipping duplicate trigger", flush=True)
        return
    global_state['_auto_analyze_running'] = True
    try:
        _auto_analyze_task_result_inner(global_state)
    finally:
        global_state['_auto_analyze_running'] = False


def _auto_analyze_task_result_inner(global_state):
    """Actual implementation — called only when guard flag is clear."""
    final_report = global_state.get('last_session', {}).get('final_report', 'No report available.')
    current_objective = global_state.get('current_objective', '')

    # Mark action plan step as completed if it matches
    log_manager = global_state.get('log_manager')
    if log_manager and current_objective:
        step_marked = log_manager.mark_plan_step_completed(current_objective)
        if step_marked:
            print(f"Action plan step marked complete: {current_objective}")
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

    # Load system message template from config (editable via UI)
    cfg = get_config()
    task_completed_template = cfg.get('SystemMessages', 'task_completed', fallback='[ACTION COMPLETED]\nThe execution of the initiated task is finished.\nFinal Report: {final_report}\n\nINSTRUCTION:\nBased on the previous conversation, briefly inform the user that the task is done and summarize the outcome.\nBe natural, and continue the conversation.')
    system_trigger = task_completed_template.replace('{final_report}', final_report)

    # If in API mode, mark api_chat_active so switch/task proposals go to HTTP polling
    # instead of emitting WebUI modals that would block execution waiting for manual approval.
    # Also set _api_chat_unattended so switch proposals are auto-denied immediately — there is
    # no API client polling during auto-analyze, so we must not block on switch_event.wait().
    if global_state.get('api_ui_locked'):
        import uuid as _uuid
        auto_request_id = 'auto_' + str(_uuid.uuid4())[:8]

        # Persist 'completed' for the original request before overwriting api_chat_request_id.
        # Without this, the original request stays 'processing' in API_CHAT_REGISTRY forever
        # because process_chat_message's finally block already ran (api_chat_active was False).
        prev_req_id = global_state.get('api_chat_request_id', '')
        if prev_req_id and not prev_req_id.startswith('auto_') and prev_req_id in API_CHAT_REGISTRY:
            if API_CHAT_REGISTRY[prev_req_id].get('status') == 'processing':
                API_CHAT_REGISTRY[prev_req_id]['status'] = 'completed'
                API_CHAT_REGISTRY[prev_req_id]['completed_at'] = time.time()
                print(f"[API CHAT] Persisted completed for original request {prev_req_id} before auto-analyze", flush=True)

        global_state['api_chat_active'] = True
        global_state['_api_chat_unattended'] = True
        global_state['api_chat_request_id'] = auto_request_id
        global_state['_api_chat_status'] = 'processing'

        # Register in API_CHAT_REGISTRY so the client can poll it to know when analysis is done.
        # Include the ID in the triggering task's registry entry for discoverability.
        API_CHAT_REGISTRY[auto_request_id] = {
            'status': 'processing',
            'message': '[auto-analyze]',
            'response': None,
            'pending_type': None,
            'pending_data': None,
            'error': None,
            'created_at': time.time(),
            'completed_at': None
        }
        task_id = global_state.get('current_api_task_id')
        if task_id:
            from agent_core import API_TASK_REGISTRY as _ATR
            if task_id in _ATR:
                _ATR[task_id]['auto_analyze_request_id'] = auto_request_id
        print(f"[API CHAT] Auto-analyze running in unattended API mode, request_id={auto_request_id}", flush=True)

    try:
        agent_core.process_chat_message(socketio, global_state, system_trigger, True)
    finally:
        global_state.pop('_api_chat_unattended', None)
        # Mark auto-analyze as completed in registry
        auto_id = global_state.get('api_chat_request_id', '')
        if auto_id.startswith('auto_') and auto_id in API_CHAT_REGISTRY:
            API_CHAT_REGISTRY[auto_id]['status'] = 'completed'
            API_CHAT_REGISTRY[auto_id]['completed_at'] = time.time()
            resp = global_state.get('_api_chat_response')
            if resp:
                API_CHAT_REGISTRY[auto_id]['response'] = resp


@socketio.on('analyze_task_result')
def handle_analyze_task_result():
    """
    Called by frontend when a chat-initiated task finishes.
    Delegates to _auto_analyze_task_result via background task.
    """
    global GLOBAL_STATE
    socketio.start_background_task(_auto_analyze_task_result, GLOBAL_STATE)

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
        # Load the existing stack
        stack = log_manager.action_plan.load_stack()

        if stack:
            # UPDATE existing plan (don't create a new one)
            active_plan = stack[-1]
            active_plan['title'] = title

            # Update steps with new objectives and completed flags
            active_plan['steps'] = [
                {'objective': step.get('objective', ''), 'completed': step.get('completed', False)}
                for step in steps_data if step.get('objective')
            ]
        else:
            # CREATE new plan if none exists
            step_objectives = [step.get('objective', '') for step in steps_data if step.get('objective')]
            log_manager.set_action_plan(title, step_objectives)

            # Update completion flags for newly created plan
            stack = log_manager.action_plan.load_stack()
            if stack:
                active_plan = stack[-1]
                for idx, step_data in enumerate(steps_data):
                    if idx < len(active_plan['steps']) and step_data.get('completed', False):
                        active_plan['steps'][idx]['completed'] = True

        # Save the updated stack
        if stack:
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

# --- Knowledge Document Endpoints ---

def initialize_knowledge_manager():
    """Initialize or reinitialize the KnowledgeManager based on config."""
    global GLOBAL_STATE
    cfg = get_config()
    enabled = cfg.getboolean('Knowledge', 'enabled', fallback=False)
    embedding_model = cfg.get('Knowledge', 'embedding_model', fallback='')

    if enabled and embedding_model:
        try:
            from knowledge_manager import KnowledgeManager
            km = KnowledgeManager(
                storage_dir=KNOWLEDGE_DIR,
                embedding_provider=cfg.get('Knowledge', 'embedding_provider', fallback='ollama'),
                embedding_model=embedding_model,
                vector_store_type=cfg.get('Knowledge', 'vector_store', fallback='chromadb'),
                summarization_threshold=cfg.getint('Agent', 'summarization_threshold', fallback=15000),
                ollama_url=cfg.get('Ollama', 'api_url', fallback='http://localhost:11434'),
                gemini_api_key=cfg.get('General', 'gemini_api_key', fallback='')
            )
            GLOBAL_STATE['knowledge_manager'] = km
            print(f"Knowledge Manager initialized: {km.get_document_count()} documents loaded.")

            # Migrate existing documents: re-embed 'direct' mode docs and generate missing summaries.
            # Runs in a background thread to avoid blocking startup.
            # chat_llm may be None if not configured — fallback to auto-summary in that case.
            def _run_migration(km_ref):
                try:
                    chat_llm = GLOBAL_STATE.get('chat_llm')
                    n = km_ref.generate_missing_summaries(chat_llm)
                    if n:
                        print(f"[KNOWLEDGE] Migration complete: {n} document(s) updated.", flush=True)
                except Exception as mig_err:
                    print(f"[KNOWLEDGE] Migration error: {mig_err}", flush=True)

            import threading as _threading
            _threading.Thread(target=_run_migration, args=(km,), daemon=True).start()

        except Exception as e:
            print(f"Error initializing Knowledge Manager: {e}")
            traceback.print_exc()
            GLOBAL_STATE['knowledge_manager'] = None
    else:
        GLOBAL_STATE['knowledge_manager'] = None


@app.route('/upload_knowledge', methods=['POST'])
def upload_knowledge():
    """Upload a document to the knowledge store."""
    try:
        km = GLOBAL_STATE.get('knowledge_manager')
        if not km:
            return jsonify({'status': 'error', 'message': 'Knowledge system is not configured. Set an embedding model in Agent & LLM settings.'}), 400

        if km.get_document_count() >= km.MAX_DOCUMENTS:
            return jsonify({'status': 'error', 'message': f'Maximum document limit ({km.MAX_DOCUMENTS}) reached.'}), 400

        # Early size check before reading the file into memory
        content_length = request.content_length
        if content_length and content_length > km.MAX_FILE_SIZE_MB * 1024 * 1024:
            return jsonify({'status': 'error', 'message': f'File too large: {content_length / (1024 * 1024):.1f} MB exceeds limit of {km.MAX_FILE_SIZE_MB} MB.'}), 400

        if 'file' not in request.files:
            return jsonify({'status': 'error', 'message': 'No file provided.'}), 400

        file = request.files['file']
        if not file.filename:
            return jsonify({'status': 'error', 'message': 'No file selected.'}), 400

        # Get file type from extension
        filename = file.filename
        file_ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''

        if file_ext not in km.ALLOWED_TYPES:
            return jsonify({'status': 'error', 'message': f'Unsupported file type: .{file_ext}. Allowed: {", ".join(km.ALLOWED_TYPES)}'}), 400

        content_bytes = file.read()
        # Pass chat LLM for semantic summary generation; falls back to auto-summary if None
        chat_llm = GLOBAL_STATE.get('chat_llm')
        doc_meta = km.add_document(filename, content_bytes, file_ext, llm=chat_llm)

        # Emit update to all clients
        socketio.emit('knowledge_updated', {
            'documents': km.get_documents_list(),
            'count': km.get_document_count()
        })

        # Emit limit warning if store is near/at capacity
        _emit_knowledge_limit_warning(km)

        return jsonify({'status': 'success', 'document': doc_meta})

    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': f'Upload failed: {str(e)}'}), 500


@app.route('/remove_knowledge', methods=['POST'])
def remove_knowledge():
    """Remove a document from the knowledge store."""
    try:
        km = GLOBAL_STATE.get('knowledge_manager')
        if not km:
            return jsonify({'status': 'error', 'message': 'Knowledge system is not configured.'}), 400

        data = request.json
        doc_id = data.get('doc_id')
        if not doc_id:
            return jsonify({'status': 'error', 'message': 'No document ID provided.'}), 400

        success = km.remove_document(doc_id)
        if not success:
            return jsonify({'status': 'error', 'message': 'Document not found.'}), 404

        # Emit update to all clients
        socketio.emit('knowledge_updated', {
            'documents': km.get_documents_list(),
            'count': km.get_document_count()
        })

        return jsonify({'status': 'success', 'message': 'Document removed.'})

    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/list_knowledge')
def list_knowledge():
    """Return list of uploaded knowledge documents."""
    km = GLOBAL_STATE.get('knowledge_manager')
    if not km:
        return jsonify({'documents': [], 'count': 0, 'max': 10})

    return jsonify({
        'documents': km.get_documents_list(),
        'count': km.get_document_count(),
        'max': km.MAX_DOCUMENTS
    })


@app.route('/get_knowledge_doc/<doc_id>')
def get_knowledge_doc(doc_id):
    """Return the text content and metadata of a knowledge document."""
    try:
        km = GLOBAL_STATE.get('knowledge_manager')
        if not km:
            return jsonify({'status': 'error', 'message': 'Knowledge system is not configured.'}), 400

        # Find document metadata
        doc_meta = next((d for d in km.get_documents_list() if d['id'] == doc_id), None)
        if not doc_meta:
            return jsonify({'status': 'error', 'message': 'Document not found.'}), 404

        # Get text content
        content = km.get_document_text(doc_id)
        if content is None:
            return jsonify({'status': 'error', 'message': 'Document content not available.'}), 404

        return jsonify({
            'status': 'success',
            'filename': doc_meta.get('filename', ''),
            'type': doc_meta.get('type', ''),
            'mode': doc_meta.get('mode', ''),
            'size': doc_meta.get('size', 0),
            'uploaded_at': doc_meta.get('uploaded_at', ''),
            'source': doc_meta.get('source', ''),
            'content': content
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/clear_knowledge', methods=['POST'])
def clear_knowledge():
    """Clear all knowledge documents."""
    try:
        km = GLOBAL_STATE.get('knowledge_manager')
        if not km:
            return jsonify({'status': 'error', 'message': 'Knowledge system is not configured.'}), 400

        km.clear_all()

        # Emit update to all clients
        socketio.emit('knowledge_updated', {
            'documents': [],
            'count': 0
        })

        return jsonify({'status': 'success', 'message': 'All knowledge documents cleared.'})

    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ---
# --- Web Search Module Routes ---
# ---

@app.route('/get_telegram_config')
def get_telegram_config():
    """Return Telegram bot configuration (token masked)."""
    cfg = get_config()
    token = cfg.get('Telegram', 'bot_token', fallback='').strip()
    return jsonify({
        'enabled':          cfg.getboolean('Telegram', 'enabled', fallback=False),
        'bot_token':        token,
        'allowed_chat_ids': cfg.get('Telegram', 'allowed_chat_ids', fallback='').strip(),
        'notify_task_done': cfg.getboolean('Telegram', 'notify_task_done', fallback=True),
    })


@app.route('/save_telegram_config', methods=['POST'])
def save_telegram_config():
    """Save Telegram bot configuration and (re)start the bot."""
    data = request.json or {}
    cfg = get_config()
    if not cfg.has_section('Telegram'):
        cfg.add_section('Telegram')
    cfg.set('Telegram', 'enabled',          'true' if data.get('enabled') else 'false')
    cfg.set('Telegram', 'bot_token',        str(data.get('bot_token', '')).strip())
    cfg.set('Telegram', 'allowed_chat_ids', str(data.get('allowed_chat_ids', '')).strip())
    cfg.set('Telegram', 'notify_task_done', 'true' if data.get('notify_task_done', True) else 'false')
    try:
        with open(CONFIG_FILE_PATH, 'w') as f:
            cfg.write(f)
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500

    # (Re)start the bot with the new config
    try:
        from telegram_bot import get_telegram_bot
        bot = get_telegram_bot()
        bot.stop()
        started = bot.start()
        msg = 'Bot started.' if started else 'Config saved. Bot disabled or token missing.'
    except Exception as e:
        msg = f'Config saved. Bot restart failed: {e}'

    return jsonify({'status': 'ok', 'message': msg})


@app.route('/get_web_search_config')
def get_web_search_config():
    """Return web search module configuration."""
    cfg = get_config()
    ws_config = web_search_module.get_web_search_config(cfg)
    return jsonify(ws_config)


@app.route('/save_web_search_config', methods=['POST'])
def save_web_search_config():
    """Save web search module configuration."""
    try:
        data = request.json
        cfg = get_config()

        if not cfg.has_section('WebSearch'):
            cfg.add_section('WebSearch')

        cfg.set('WebSearch', 'enabled', str(data.get('enabled', False)))
        cfg.set('WebSearch', 'provider', data.get('provider', 'ollama'))
        cfg.set('WebSearch', 'model_name', data.get('model_name', ''))
        cfg.set('WebSearch', 'temperature', str(data.get('temperature', 0.3)))
        cfg.set('WebSearch', 'context_size', str(data.get('context_size', 8192)))
        cfg.set('WebSearch', 'timeout', str(data.get('timeout', 120)))
        cfg.set('WebSearch', 'max_retries', str(data.get('max_retries', 5)))
        cfg.set('WebSearch', 'max_results', str(data.get('max_results', 5)))
        cfg.set('WebSearch', 'max_fetch_pages', str(data.get('max_fetch_pages', 3)))
        cfg.set('WebSearch', 'max_page_size', str(data.get('max_page_size', 50000)))
        cfg.set('WebSearch', 'brief_threshold', str(data.get('brief_threshold', 2000)))
        cfg.set('WebSearch', 'search_engine', data.get('search_engine', 'duckduckgo'))
        cfg.set('WebSearch', 'region', data.get('region', 'wt-wt'))
        cfg.set('WebSearch', 'safe_search', data.get('safe_search', 'off'))
        cfg.set('WebSearch', 'ollama_url', data.get('ollama_url', ''))
        cfg.set('WebSearch', 'api_key', data.get('api_key', ''))
        cfg.set('WebSearch', 'fallback_models', data.get('fallback_models', ''))

        # Save prompt
        if 'prompt_template' in data:
            if not cfg.has_section('WebSearchPrompt'):
                cfg.add_section('WebSearchPrompt')
            cfg.set('WebSearchPrompt', 'template', data['prompt_template'])

        with open(CONFIG_FILE_PATH, 'w') as f:
            cfg.write(f)

        return jsonify({'status': 'success', 'message': 'Web Search configuration saved!'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/get_web_search_prompt')
def get_web_search_prompt():
    """Return web search prompt template."""
    cfg = get_config()
    return jsonify({
        'template': cfg.get('WebSearchPrompt', 'template', fallback='')
    })


@app.route('/clear_web_knowledge', methods=['POST'])
def clear_web_knowledge():
    """Clear all knowledge documents with source=WEB."""
    try:
        km = GLOBAL_STATE.get('knowledge_manager')
        if not km:
            return jsonify({'status': 'error', 'message': 'Knowledge system is not configured.'}), 400

        count = km.clear_by_source('WEB')

        socketio.emit('knowledge_updated', {
            'documents': km.get_documents_list(),
            'count': km.get_document_count()
        })

        return jsonify({'status': 'success', 'message': f'Cleared {count} web search documents.'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500


@socketio.on('get_web_search_status')
def handle_get_web_search_status():
    """Return current web search status."""
    ws_status = GLOBAL_STATE.get('web_search_status')
    if ws_status:
        socketio.emit('web_search_status', ws_status.to_dict())
    else:
        socketio.emit('web_search_status', web_search_module.WebSearchStatus().to_dict())


# ============================================================================
# CHAT API ENDPOINTS
# ============================================================================

@app.route('/api/chat/message', methods=['POST'])
def api_chat_message():
    """
    Send a message to the chat agent and get a request_id for polling.

    Body: {"message": "..."}
    Returns: {"request_id": "...", "status": "processing"}
    Errors:
      423 — WebUI client is connected (human has control)
      409 — Another API chat session is already active
      400 — Missing message
    """
    if _is_webui_active():
        return jsonify({
            'error': 'WebUI active — control blocked by human intervention',
            'code': 'WEBUI_ACTIVE'
        }), 423

    if GLOBAL_STATE.get('api_chat_active'):
        return jsonify({
            'error': 'Another API chat session is already active',
            'code': 'SESSION_BUSY',
            'active_request_id': GLOBAL_STATE.get('api_chat_request_id')
        }), 409

    data = request.json or {}
    message = data.get('message', '').strip()
    if not message:
        return jsonify({'error': 'message field is required'}), 400

    request_id = str(uuid.uuid4())[:12]

    API_CHAT_REGISTRY[request_id] = {
        'status': 'processing',
        'message': message,
        'response': None,
        'pending_type': None,
        'pending_data': None,
        'error': None,
        'created_at': time.time(),
        'completed_at': None
    }

    # Initialize API state in GLOBAL_STATE
    GLOBAL_STATE['api_chat_active'] = True
    GLOBAL_STATE['api_ui_locked'] = True
    GLOBAL_STATE['webui_watching_only'] = False
    GLOBAL_STATE['api_chat_request_id'] = request_id
    GLOBAL_STATE['_api_last_activity'] = time.time()
    GLOBAL_STATE['_api_chat_response'] = None
    GLOBAL_STATE['_api_chat_status'] = 'processing'
    GLOBAL_STATE['_api_chat_pending_type'] = None
    GLOBAL_STATE['_api_chat_pending_data'] = None
    GLOBAL_STATE['_pending_task_proposal'] = None
    # Clear stale unattended flag — this is an attended session regardless of any auto-analyze still running
    GLOBAL_STATE['_api_chat_unattended'] = False

    # Persist lock state so browser shows correct mode after reconnect / Docker restart
    save_app_state()

    # Notify any connected browser clients to enter read-only mode
    socketio.emit('api_session_status', {'active': True})

    print(f"[API CHAT] New session: request_id={request_id}, message={message[:80]}", flush=True)

    # Show the API message immediately in any connected browser (with "API" label)
    socketio.emit('chat_response', {'role': 'api', 'content': message})

    # Persist with role='api' so the label survives page refresh / container rebuild.
    # Set a flag so process_chat_message skips its own log_chat_message('user', ...) call.
    log_manager = GLOBAL_STATE.get('log_manager')
    if log_manager:
        log_manager.log_chat_message('api', message)
    GLOBAL_STATE['_api_message_logged'] = True

    # Start background chat processing
    socketio.start_background_task(
        agent_core.process_chat_message,
        socketio,
        GLOBAL_STATE,
        message
    )

    return jsonify({
        'request_id': request_id,
        'status': 'processing',
        'info': f'Poll status at GET /api/chat/status/{request_id}'
    })


@app.route('/api/chat/status/<request_id>', methods=['GET'])
def api_chat_status(request_id):
    """
    Poll the status of a chat API request.

    Returns:
      status: processing | awaiting_approval | completed | interrupted | error
      response: assistant reply (when completed)
      pending_type: task_proposal | switch_proposal (when awaiting_approval)
      pending_data: {objective} or {target_system, reason}
    """
    if request_id not in API_CHAT_REGISTRY:
        return jsonify({'error': 'Unknown request_id'}), 404

    entry = dict(API_CHAT_REGISTRY[request_id])

    # Sync live state from GLOBAL_STATE while this is the active request
    if GLOBAL_STATE.get('api_chat_request_id') == request_id:
        live_status = GLOBAL_STATE.get('_api_chat_status', 'processing')
        live_response = GLOBAL_STATE.get('_api_chat_response')
        live_pending_type = GLOBAL_STATE.get('_api_chat_pending_type')
        live_pending_data = GLOBAL_STATE.get('_api_chat_pending_data')

        entry['status'] = live_status
        if live_response is not None:
            entry['response'] = live_response
        if live_pending_type:
            entry['pending_type'] = live_pending_type
            entry['pending_data'] = live_pending_data

        # Persist terminal states back to registry
        if live_status in ('completed', 'error', 'interrupted'):
            API_CHAT_REGISTRY[request_id].update({
                'status': live_status,
                'response': live_response,
                'pending_type': live_pending_type,
                'pending_data': live_pending_data,
                'completed_at': API_CHAT_REGISTRY[request_id].get('completed_at') or time.time()
            })

    return jsonify(entry)


@app.route('/api/chat/approve/<request_id>', methods=['POST'])
def api_chat_approve(request_id):
    """
    Approve or deny a pending task_proposal or switch_proposal.

    Body: {
      "decision": "approve" | "deny",
      "reason": "optional denial reason"   (for switch denial)
    }

    For task_proposal approval, returns a task_id trackable via /api/task_status/<task_id>.
    """
    if request_id not in API_CHAT_REGISTRY:
        return jsonify({'error': 'Unknown request_id'}), 404

    # Pending type lives in GLOBAL_STATE while session is active; fall back to registry
    if GLOBAL_STATE.get('api_chat_request_id') == request_id:
        pending_type = GLOBAL_STATE.get('_api_chat_pending_type')
        pending_data = GLOBAL_STATE.get('_api_chat_pending_data') or {}
    else:
        pending_type = API_CHAT_REGISTRY[request_id].get('pending_type')
        pending_data = API_CHAT_REGISTRY[request_id].get('pending_data') or {}

    if not pending_type:
        return jsonify({'error': 'No pending approval for this request'}), 400

    data = request.json or {}
    decision = data.get('decision', 'approve').lower()
    reason = data.get('reason', '').strip()
    GLOBAL_STATE['_api_last_activity'] = time.time()

    if pending_type == 'switch_proposal':
        target_system = pending_data.get('target_system', '')
        # Clear pending before triggering event (agent thread resumes immediately)
        GLOBAL_STATE['_api_chat_pending_type'] = None
        GLOBAL_STATE['_api_chat_pending_data'] = None
        GLOBAL_STATE['_api_chat_status'] = 'processing'
        API_CHAT_REGISTRY[request_id]['pending_type'] = None
        API_CHAT_REGISTRY[request_id]['pending_data'] = None

        if decision == 'approve':
            _do_approve_system_switch(target_system)
        else:
            _do_deny_system_switch(target_system, reason)

        # Note: switch approval has no task_id — agent continues in the same chat session.
        # Poll GET /api/chat/status/<request_id> to see the next pending action.
        return jsonify({
            'status': 'ok',
            'decision': decision,
            'task_id': None,
            'next': f'poll GET /api/chat/status/{request_id}'
        })

    elif pending_type == 'task_proposal':
        objective = pending_data.get('objective', '')
        # Clear pending
        GLOBAL_STATE['_api_chat_pending_type'] = None
        GLOBAL_STATE['_api_chat_pending_data'] = None
        GLOBAL_STATE['_pending_task_proposal'] = None
        API_CHAT_REGISTRY[request_id]['pending_type'] = None
        API_CHAT_REGISTRY[request_id]['pending_data'] = None

        if decision == 'approve' and objective:
            # Start execution task — reuse same setup as handle_execute_task
            with TASK_START_LOCK:
                if GLOBAL_STATE.get('task_running'):
                    return jsonify({'error': 'A task is already running'}), 409
                GLOBAL_STATE['task_running'] = True

            GLOBAL_STATE['current_objective'] = objective
            GLOBAL_STATE['current_execution_mode'] = 'independent'
            GLOBAL_STATE['current_summarization_mode'] = 'automatic'
            GLOBAL_STATE['current_allow_ask_mode'] = False
            GLOBAL_STATE['task_paused'] = False
            USER_RESPONSE.clear()
            USER_ANSWER.clear()
            cfg = get_config()
            GLOBAL_STATE['system_username'] = cfg.get('System', 'username', fallback='unknown')
            GLOBAL_STATE['system_ip'] = cfg.get('System', 'ip_address', fallback='unknown')
            socketio.emit('task_started')

            task_id = str(uuid.uuid4())[:8]
            from agent_core import API_TASK_REGISTRY as _ATR
            _ATR[task_id] = {
                'status': 'running', 'objective': objective, 'result': None,
                'current_step': 0, 'latest_activity': 'Starting...', 'start_time': time.time()
            }
            GLOBAL_STATE['current_api_task_id'] = task_id
            GLOBAL_STATE['task_from_api_chat'] = True  # Triggers auto-analyze when task finishes

            socketio.start_background_task(
                run_agent_and_update_state,
                socketio, GLOBAL_STATE, CONTROL_FLAGS, EVENT_OBJECTS, task_id
            )
            # Release api_chat_active — task runs independently.
            # api_ui_locked stays True until "Take Control" or /api/chat/release.
            GLOBAL_STATE['api_chat_active'] = False
            return jsonify({'status': 'ok', 'decision': 'approve', 'task_id': task_id,
                            'info': f'Track at GET /api/task_status/{task_id}'})
        else:
            # Deny — release api_chat_active but keep ui locked
            GLOBAL_STATE['api_chat_active'] = False
            return jsonify({'status': 'ok', 'decision': 'deny'})

    return jsonify({'error': f'Unknown pending_type: {pending_type}'}), 400


@app.route('/api/chat/capabilities', methods=['GET'])
def api_chat_capabilities():
    """
    Machine-readable manifest of all API capabilities.
    Intended for 3rd-party integrations (e.g. OpenClaw) to auto-discover how to communicate
    with this agent without any prior documentation.
    """
    base = request.host_url.rstrip('/')
    current_mode = 'api' if GLOBAL_STATE.get('api_ui_locked') else 'web'
    return jsonify({
        'name': 'AI Agent Controller — HTTP API',
        'version': '2.0',
        'base_url': base,
        'current_control_mode': current_mode,

        'description': (
            'HTTP polling API for the AI Agent Controller. Supports two independent modules: '
            '(1) Chat API — conversational interaction with the LLM assistant; '
            '(2) Execution API — direct SSH task execution on remote systems. '
            'The chat agent can search history, query a knowledge base, search the web, '
            'propose execution tasks, and request system switches, all controllable via polling.'
        ),

        'control_mode': {
            'description': (
                'The application operates in one of two exclusive control modes. '
                '"web" means a human controls via the browser — API calls return HTTP 423. '
                '"api" means the API has control — the browser enters read-only mode. '
                'Switching TO api mode requires human action in the browser ("Give API Control" button). '
                'API clients can only release control back to web mode.'
            ),
            'current': current_mode,
            'release_to_web': {
                'method': 'POST',
                'url': f'{base}/api/set_control_mode',
                'body': {'mode': 'web'},
                'note': 'Releases API control — browser regains full access. Only valid when already in API mode.'
            },
            'acquire_api_mode': {
                'how': 'Must be activated by the human user in the browser ("Give API Control" banner button)',
                'note': 'Cannot be forced via HTTP — POST with mode=api returns HTTP 403 when in web mode'
            }
        },

        'chat_api': {
            'description': 'Send messages to the LLM chat agent and handle proposals.',
            'single_session': 'Only one chat request can be active at a time (HTTP 409 if busy).',
            'endpoints': {
                'send_message': {
                    'method': 'POST',
                    'url': f'{base}/api/chat/message',
                    'body': {'message': 'string — the user message'},
                    'returns': {'request_id': 'string', 'status': 'processing'},
                    'errors': {
                        '423': 'App is in WEB mode — switch to API mode first',
                        '409': 'Another chat session is already active',
                        '400': 'Missing message field'
                    }
                },
                'poll_status': {
                    'method': 'GET',
                    'url': f'{base}/api/chat/status/{{request_id}}',
                    'poll_interval': '1-3 seconds recommended',
                    'returns': {
                        'status': 'processing | awaiting_approval | completed | interrupted | error',
                        'response': 'string | null — the assistant reply (when completed)',
                        'pending_type': 'null | task_proposal | switch_proposal',
                        'pending_data': {
                            'task_proposal': {'objective': 'string — proposed task description'},
                            'switch_proposal': {'target_system': 'string', 'reason': 'string'}
                        },
                        'error': 'string | null'
                    }
                },
                'approve_or_deny': {
                    'method': 'POST',
                    'url': f'{base}/api/chat/approve/{{request_id}}',
                    'body': {
                        'decision': 'approve | deny',
                        'reason': 'string (optional — sent to agent on deny)'
                    },
                    'use_when': 'pending_type is not null',
                    'on_task_proposal_approve': f'Starts SSH execution task → returns task_id, track at GET {base}/api/task_status/{{task_id}}',
                    'on_switch_proposal_approve': 'Switches active SSH target system, chat thread resumes',
                    'on_deny': 'Agent receives denial reason and continues conversation'
                },
                'release': {
                    'method': 'POST',
                    'url': f'{base}/api/chat/release',
                    'note': 'Explicitly release API mode and restore WEB control (same as clicking "Take Control" in browser)'
                }
            },
            'interaction_types': {
                'task_proposal': {
                    'trigger': 'Agent decides an SSH command or task is needed to answer the request',
                    'status_when_pending': 'completed — chat turn is done, but pending_type=task_proposal',
                    'what_to_do': f'POST {base}/api/chat/approve/<id> with decision=approve to start execution, then poll GET {base}/api/task_status/<task_id>',
                    'after_task_completes': 'Chat agent automatically receives the execution report and generates a follow-up response'
                },
                'switch_proposal': {
                    'trigger': 'Agent wants to switch the active target system (e.g. move from Server A to Server B)',
                    'status_when_pending': 'awaiting_approval — chat thread is BLOCKED waiting for your decision',
                    'what_to_do': f'POST {base}/api/chat/approve/<id> with decision=approve|deny',
                    'timeout': '5 minutes — agent continues without switching if no response'
                }
            },
            'workflow_examples': {
                'simple_query': [
                    f'POST {base}/api/chat/message  →  {{request_id}}',
                    f'GET  {base}/api/chat/status/<id>  →  {{status: "processing"}}  (repeat)',
                    f'GET  {base}/api/chat/status/<id>  →  {{status: "completed", response: "..."}}'
                ],
                'query_with_task_execution': [
                    f'POST {base}/api/chat/message  →  {{request_id}}',
                    f'GET  {base}/api/chat/status/<id>  →  {{status: "completed", pending_type: "task_proposal", pending_data: {{objective: "..."}}}}'  ,
                    f'POST {base}/api/chat/approve/<id>  {{decision: "approve"}}  →  {{task_id: "..."}}',
                    f'GET  {base}/api/task_status/<task_id>  →  {{status: "running", current_step: 2, latest_activity: "..."}}  (repeat)',
                    f'GET  {base}/api/task_status/<task_id>  →  {{status: "completed", result: "..."}}',
                    '(chat agent auto-receives execution report and sends follow-up response)'
                ],
                'query_with_system_switch': [
                    f'POST {base}/api/chat/message  →  {{request_id}}',
                    f'GET  {base}/api/chat/status/<id>  →  {{status: "awaiting_approval", pending_type: "switch_proposal"}}',
                    f'POST {base}/api/chat/approve/<id>  {{decision: "approve"}}',
                    f'GET  {base}/api/chat/status/<id>  →  {{status: "completed", response: "..."}}'
                ]
            }
        },

        'execution_api': {
            'description': 'Direct SSH task execution without chat interaction.',
            'endpoints': {
                'start_task': {
                    'method': 'POST',
                    'url': f'{base}/api/execute_ssh',
                    'body': {
                        'objective': 'string — task description for the agent',
                        'mode': 'independent | assisted (optional, default: independent)',
                        'system_name': 'string (optional — target system alias from saved connections)'
                    },
                    'returns': {'task_id': 'string', 'status': 'started'}
                },
                'poll_task': {
                    'method': 'GET',
                    'url': f'{base}/api/task_status/{{task_id}}',
                    'returns': {
                        'status': 'running | completed | failed',
                        'current_step': 'integer',
                        'latest_activity': 'string — last action description',
                        'result': 'string | null — final report when completed',
                        'duration_seconds': 'float'
                    }
                },
                'stop_task': {
                    'method': 'POST',
                    'url': f'{base}/api/stop',
                    'note': 'Stops any currently running task'
                },
                'health_check': {
                    'method': 'GET',
                    'url': f'{base}/api/status',
                    'returns': {
                        'task_running': 'bool',
                        'task_paused': 'bool',
                        'ssh_status': 'success | error | unknown',
                        'llm_status': 'success | error | unknown',
                        'current_objective': 'string | null'
                    }
                },
                'list_systems': {
                    'method': 'GET',
                    'url': f'{base}/api/list_systems',
                    'returns': 'Array of saved system connections with name, ip, username, device_type'
                },
                'switch_system': {
                    'method': 'POST',
                    'url': f'{base}/api/switch_system',
                    'body': {'system_name': 'string — alias of target system'},
                    'note': 'Validates SSH connectivity before switching'
                }
            }
        },

        'notes': [
            'All endpoints return JSON.',
            'Authentication: none (deploy behind a firewall or VPN).',
            'The chat agent has memory of the conversation — subsequent messages build on context.',
            'To reset context between independent sessions, start a new task or clear chat history.',
            f'This manifest is always available at GET {base}/api/chat/capabilities'
        ]
    })


@app.route('/api/set_control_mode', methods=['POST'])
def api_set_control_mode():
    """
    Programmatically switch between WEB and API control modes.
    Body: {"mode": "api" | "web"}
    """
    data = request.json or {}
    mode = data.get('mode', '').strip().lower()
    if mode not in ('api', 'web'):
        return jsonify({'error': 'mode must be "api" or "web"'}), 400

    if mode == 'api':
        # Switching TO api mode via HTTP is forbidden — it must be initiated by the human
        # in the browser ("Give API Control" button). This prevents any remote agent from
        # forcibly taking control when the user has explicitly chosen web mode.
        if not GLOBAL_STATE.get('api_ui_locked'):
            return jsonify({
                'error': 'Cannot switch to API mode via HTTP — must be activated by the user in the browser',
                'code': 'MODE_CHANGE_FORBIDDEN',
                'hint': 'Click "Give API Control" in the browser banner to grant API access'
            }), 403
        # Already in API mode — no-op, return current state
        return jsonify({'status': 'ok', 'control_mode': 'api', 'message': 'Already in API mode'})
    else:
        GLOBAL_STATE['api_ui_locked'] = False
        GLOBAL_STATE['api_chat_active'] = False
        GLOBAL_STATE['webui_watching_only'] = False
        save_app_state()
        socketio.emit('api_session_status', {'active': False})
        print("[CONTROL] API set control mode → WEB", flush=True)
        return jsonify({'status': 'ok', 'control_mode': 'web', 'message': 'Switched to WEB mode — browser has full control'})


@app.route('/api/chat/release', methods=['POST'])
def api_chat_release():
    """
    Explicitly release the UI lock held by an API session.
    Clears both api_chat_active and api_ui_locked, restoring full WebUI control.
    Equivalent to the "Take Control" button in the browser.
    """
    GLOBAL_STATE['api_chat_active'] = False
    GLOBAL_STATE['api_ui_locked'] = False
    GLOBAL_STATE['webui_watching_only'] = False
    save_app_state()
    socketio.emit('api_session_status', {'active': False})
    print("[API CHAT] UI lock released via /api/chat/release", flush=True)
    return jsonify({'status': 'ok', 'message': 'UI lock released — WebUI restored to full control'})


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

    # Initialize last_logged_system from config.ini to prevent ghost entries after restart
    cfg = get_config()
    init_ip = cfg.get('System', 'ip_address', fallback='').strip()
    init_username = cfg.get('System', 'username', fallback='').strip()
    init_name = cfg.get('System', 'system_name', fallback='').strip()
    if init_ip and init_username:
        GLOBAL_STATE['last_logged_system'] = {
            'username': init_username,
            'ip': init_ip,
            'name': init_name
        }
        print(f"Initialized last_logged_system: {init_name or init_username}@{init_ip}")

    # Then test/load connections based on config.ini
    initialize_ssh_status()
    initialize_llm_status()
    initialize_knowledge_manager()
    _start_health_check_thread()
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

        # Start Telegram bot (non-blocking background threads)
        try:
            from telegram_bot import start_telegram_bot
            start_telegram_bot(GLOBAL_STATE)
        except Exception as tg_err:
            print(f"[TELEGRAM] Startup error (non-fatal): {tg_err}")

        # Pornim serverul (threading mode, allow_unsafe_werkzeug for production use without gunicorn)
        socketio.run(app, debug=False, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
        
    except Exception as e:
        print(f"Fatal error during startup: {e}")
        traceback.print_exc()
