import os
import json
import traceback
import zipfile
from datetime import datetime
from typing import Dict, List, Optional

from config import (
    APP_DIR, KEYS_DIR, SESSION_FILE_PATH, CONNECTIONS_FILE_PATH,
    EXECUTION_LOG_FILE_PATH, CHAT_LOG_FILE_PATH, ACTION_PLAN_FILE_PATH,
    CONFIG_FILE_PATH, EXECUTION_LOG_LLM_CONTEXT_PATH
)
from log_manager import UnifiedLogManager

# ---
# --- Functii pentru Conexiuni SSH (Istoric) ---
# ---

def load_connections():
    """Incarca istoricul conexiunilor SSH din connections.json."""
    if not os.path.exists(CONNECTIONS_FILE_PATH):
        return []
    try:
        with open(CONNECTIONS_FILE_PATH, 'r') as f:
            data = json.load(f)
        if not isinstance(data, list):
            print(f"Warning: connections.json nu contine o lista. Se reseteaza.")
            return []
        return data
    except (json.JSONDecodeError, FileNotFoundError):
        print(f"Error reading connections file {CONNECTIONS_FILE_PATH}, returning empty list.")
        return []

def save_connections(connections):
    """Salveaza istoricul conexiunilor SSH in connections.json."""
    if not isinstance(connections, list):
        print(f"Error: save_connections a primit date invalide (nu o lista).")
        return
    try:
        with open(CONNECTIONS_FILE_PATH, 'w') as f:
            json.dump(connections, f, indent=4)
    except Exception as e:
        print(f"Error saving connections: {e}")
        traceback.print_exc()

# ---
# --- Functii pentru Starea Aplicatiei (Sesiune & Log Executie) ---
# ---

def save_current_session_to_disk(current_state, session_path, log_path):
    """
    Salveaza starea curenta a aplicatiei:
    1.  Datele de sesiune (istoric, etc.) in 'session.json'.
    NOTE: execution_log.txt is now managed exclusively by UnifiedLogManager (append-only).
    """
    success_json = False

    # Fix: Daca session_path este un director (bug Docker volume mount), il stergem si cream fisierul
    if os.path.exists(session_path) and os.path.isdir(session_path):
        print(f"WARNING: {session_path} is a directory (Docker mount issue). Fixing...")
        try:
            os.rmdir(session_path)  # Remove empty directory
            with open(session_path, 'w') as f:
                f.write('{}')  # Create empty JSON file
            print(f"Fixed: Created {session_path} as a file.")
        except Exception as fix_err:
            print(f"ERROR: Could not fix session.json directory issue: {fix_err}")
            return False

    # 1. Pregatim si salvam 'session.json'
    try:
        # Extragem doar partile serializabile din starea globala
        session_data_for_json = {
            'agent_history': current_state.get('agent_history', ""),
            'system_os_info': current_state.get('system_os_info', ""),
            'persistent_vm_output': current_state.get('persistent_vm_output', ""),
            'last_session': {
                "final_report": current_state.get('last_session', {}).get('final_report', ""),
                "raw_llm_responses": current_state.get('last_session', {}).get('raw_llm_responses', [])
                # 'log' este exclus intentionat din JSON
            },
            'full_history_backups': current_state.get('full_history_backups', [])
        }

        with open(session_path, 'w') as f:
            json.dump(session_data_for_json, f, indent=4)
        success_json = True

    except Exception as e:
        print(f"CRITICAL ERROR saving session data to {session_path}: {e}")
        traceback.print_exc()

    # We DO NOT overwrite the execution log here anymore.
    # The UnifiedLogManager handles that file in append-only mode to preserve data.

    return success_json

def load_session_from_disk(session_path, log_path):
    """
    Incarca starea aplicatiei de pe disc ('session.json' si 'execution_log.txt').
    Returneaza un dictionar cu starea incarcata.
    """
    loaded_state = {}

    # Fix: Daca session_path este un director (bug Docker volume mount), il stergem si cream fisierul
    if os.path.exists(session_path) and os.path.isdir(session_path):
        print(f"WARNING: {session_path} is a directory (Docker mount issue). Fixing...")
        try:
            os.rmdir(session_path)  # Remove empty directory
            with open(session_path, 'w') as f:
                f.write('{}')  # Create empty JSON file
            print(f"Fixed: Created {session_path} as a file.")
        except Exception as fix_err:
            print(f"ERROR: Could not fix session.json directory issue: {fix_err}")
            return _get_default_session_data()

    # 1. Incarcam 'session.json'
    if os.path.exists(session_path):
        try:
            with open(session_path, 'r') as f:
                data = json.load(f)
            
            # Reconstituim starea pe baza datelor JSON
            loaded_state['agent_history'] = data.get('agent_history', "No history loaded.")
            loaded_state['system_os_info'] = data.get('system_os_info', "Unknown OS.")
            loaded_state['persistent_vm_output'] = data.get('persistent_vm_output', "")
            loaded_state['full_history_backups'] = data.get('full_history_backups', [])
            
            # Asiguram ca 'last_session' exista
            loaded_state['last_session'] = {
                "final_report": data.get('last_session', {}).get('final_report', ""),
                "raw_llm_responses": data.get('last_session', {}).get('raw_llm_responses', [])
            }
            print(f"Session state loaded from {session_path}.")
            
        except (json.JSONDecodeError, FileNotFoundError, TypeError) as e:
            print(f"Error loading session file ({e}), starting with fresh session data.")
            loaded_state = _get_default_session_data() # Folosim starea default
    else:
        print(f"No session file found at {session_path}, starting fresh.")
        loaded_state = _get_default_session_data() # Folosim starea default

    # 2. Incarcam 'execution_log.txt'
    log_content = ""
    if os.path.exists(log_path):
        try:
            with open(log_path, 'r', encoding='utf-8') as f:
                log_content = f.read()
            print(f"Execution log loaded from {log_path}.")
        except Exception as e:
            print(f"Error reading execution log file {log_path}: {e}")
            log_content = f"Error loading log: {e}"
    else:
        print(f"No execution log found at {log_path}, starting with default message.")
        log_content = "No previous execution log found. Ready for new task."
        
    # Adaugam log-ul la starea incarcata
    if 'last_session' not in loaded_state:
        loaded_state['last_session'] = {}
    loaded_state['last_session']['log'] = log_content

    return loaded_state

def reset_all_memory(session_path, log_path):
    """
    Sterge fisierele de sesiune si log de pe disc.
    Returneaza starea default.
    """
    files_to_delete = [session_path, log_path]
    for f in files_to_delete:
        if os.path.exists(f):
            try:
                os.remove(f)
                print(f"Deleted file: {f}")
            except Exception as e:
                print(f"Error deleting session file {f}: {e}")
                traceback.print_exc()
                
    # Returnam starea default
    default_state = _get_default_session_data()
    default_state['last_session']['log'] = "--- AGENT MEMORY RESET ---"
    return default_state

def _get_default_session_data():
    """Returneaza un dictionar cu starea de baza a sesiunii."""
    return {
        'agent_history': "No commands have been executed yet.",
        'system_os_info': "Unknown. The first step should be to determine the OS.",
        'persistent_vm_output': "",
        'full_history_backups': [],
        'last_session': {
            "log": "Application started. Ready for task.",
            "final_report": "",
            "raw_llm_responses": []
        }
    }

def migrate_session_to_new_logs():
    """
    Initialize UnifiedLogManager for the new multi-log system.
    This function should be called during app initialization.
    Returns a UnifiedLogManager instance.
    """
    try:
        print("Initializing unified log system...")
        return UnifiedLogManager()
    except Exception as e:
        print(f"Error initializing log system: {e}")
        traceback.print_exc()
        print("Starting with fresh log system.")
        return UnifiedLogManager()

def save_session_state(global_state):
    """
    Saves the full application state to a ZIP file.
    Dumps in-memory GLOBAL_STATE to session.json first.
    """
    try:
        # 1. Dump in-memory state to session.json
        with open(SESSION_FILE_PATH, 'w') as f:
            json.dump(global_state, f, indent=4, default=str)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_filename = f"session_{timestamp}.zip"
        zip_path = os.path.join(APP_DIR, zip_filename)

        with zipfile.ZipFile(zip_path, 'w') as zipf:
            # Core State
            zipf.write(SESSION_FILE_PATH, arcname='session.json')

            # Config
            if os.path.exists(CONFIG_FILE_PATH):
                zipf.write(CONFIG_FILE_PATH, arcname='config.ini')

            # SSH Connections
            if os.path.exists(CONNECTIONS_FILE_PATH):
                zipf.write(CONNECTIONS_FILE_PATH, arcname='connections.json')

            # Logs & History
            if os.path.exists(EXECUTION_LOG_FILE_PATH):
                zipf.write(EXECUTION_LOG_FILE_PATH, arcname='execution_log.txt')

            if os.path.exists(EXECUTION_LOG_LLM_CONTEXT_PATH):
                zipf.write(EXECUTION_LOG_LLM_CONTEXT_PATH, arcname='execution_log_llm_context.txt')

            if os.path.exists(CHAT_LOG_FILE_PATH):
                zipf.write(CHAT_LOG_FILE_PATH, arcname='chat_history.json')

            if os.path.exists(ACTION_PLAN_FILE_PATH):
                zipf.write(ACTION_PLAN_FILE_PATH, arcname='action_plan.json')

        print(f"Session saved to {zip_path}")
        return zip_path

    except Exception as e:
        print(f"Error saving session: {e}")
        traceback.print_exc()
        return None

def load_session_state(zip_path):
    """
    Restores files from ZIP to disk and returns the loaded session.json data.
    """
    try:
        print(f"Loading session from {zip_path}...")

        with zipfile.ZipFile(zip_path, 'r') as zipf:
            # Extract session.json
            if 'session.json' in zipf.namelist():
                zipf.extract('session.json', APP_DIR)

            # Extract Config (to keys/config.ini)
            if 'config.ini' in zipf.namelist():
                with open(CONFIG_FILE_PATH, 'wb') as f:
                    f.write(zipf.read('config.ini'))

            # Extract Connections
            if 'connections.json' in zipf.namelist():
                with open(CONNECTIONS_FILE_PATH, 'wb') as f:
                    f.write(zipf.read('connections.json'))

            # Extract Logs
            if 'execution_log.txt' in zipf.namelist():
                with open(EXECUTION_LOG_FILE_PATH, 'wb') as f:
                    f.write(zipf.read('execution_log.txt'))

            if 'execution_log_llm_context.txt' in zipf.namelist():
                with open(EXECUTION_LOG_LLM_CONTEXT_PATH, 'wb') as f:
                    f.write(zipf.read('execution_log_llm_context.txt'))

            # Extract Chat & Plan
            if 'chat_history.json' in zipf.namelist():
                with open(CHAT_LOG_FILE_PATH, 'wb') as f:
                    f.write(zipf.read('chat_history.json'))

            if 'action_plan.json' in zipf.namelist():
                with open(ACTION_PLAN_FILE_PATH, 'wb') as f:
                    f.write(zipf.read('action_plan.json'))

        # Read the state back into memory to return it
        with open(SESSION_FILE_PATH, 'r') as f:
            state = json.load(f)

        print("Session files restored successfully.")
        return state

    except Exception as e:
        print(f"Error loading session: {e}")
        traceback.print_exc()
        return None

