import os
import configparser
import traceback

# --- CAI PERSISTENTE ---
# Directorul radacina al aplicatiei (unde se afla acest fisier)
APP_DIR = os.path.dirname(os.path.abspath(__file__))
# Directorul pentru chei SSH, conexiuni si loguri
KEYS_DIR = os.path.join(APP_DIR, 'keys')
# Fisierul principal de configurare
# MOVED to KEYS_DIR to ensure persistence across Docker rebuilds
CONFIG_FILE_PATH = os.path.join(KEYS_DIR, 'config.ini')
# Fisierul pentru stocarea conexiunilor SSH salvate
CONNECTIONS_FILE_PATH = os.path.join(KEYS_DIR, 'connections.json')
# Fisierul pentru stocarea starii sesiunii agentului (istoric, etc.)
SESSION_FILE_PATH = os.path.join(APP_DIR, 'session.json')
# --- NOU: Fisierul pentru log-ul detaliat al executiei ---
EXECUTION_LOG_FILE_PATH = os.path.join(KEYS_DIR, 'execution_log.txt')
# --- NOU: Fisierul pentru memoria de lucru a agentului (LLM Context) ---
EXECUTION_LOG_LLM_CONTEXT_PATH = os.path.join(KEYS_DIR, 'execution_log_llm_context.txt')

# Asiguram ca directorul pentru chei exista la importarea modulului
try:
    os.makedirs(KEYS_DIR, exist_ok=True)
    print(f"Directory ensured: {KEYS_DIR}")
except OSError as e:
    print(f"ERROR: Could not create directory {KEYS_DIR}: {e}")
    # Putem alege sa oprim aplicatia aici sau sa continuam cu functionalitate limitata
    # raise e # Ridica exceptia pentru a opri aplicatia


def get_config():
    """Citeste fisierul config.ini si returneaza un obiect ConfigParser.
       Creeaza fisierul cu valori default daca nu exista."""
    config = configparser.ConfigParser(interpolation=None) # Dezactivam interpolarea

    if not os.path.exists(CONFIG_FILE_PATH):
        print(f"Config file not found at {CONFIG_FILE_PATH}. Creating with defaults.")
        # Sectiuni si valori default
        config['General'] = {'provider': 'ollama', 'gemini_api_key': '', 'anthropic_api_key': ''}
        config['Agent'] = {'model_name': 'llama3:latest', 'max_steps': '50', 'summarization_threshold': '15000', 'command_timeout': '120', 'llm_timeout': '120'}
        # Calea SSH default este acum relativa la KEYS_DIR
        config['System'] = {'ip_address': '', 'username': '', 'ssh_port': '22', 'ssh_key_path': os.path.join(KEYS_DIR, 'id_rsa')}
        config['Ollama'] = {'api_url': 'http://localhost:11434'}
        # Prompturi default simple cu SRCH capability
        srch_documentation = """

You have access to search past execution history using:
SRCH: <search query>

Use SRCH when you need to recall specific information from earlier in this session, such as:
- Previously executed commands and their outputs
- Configuration values discovered earlier
- Error messages or warnings from past steps
- Any information that was in context but may have been summarized

The search will find relevant historical entries and add them to your context."""

        default_prompt = "Objective: {objective}\nHistory: {history}\nSystem: {system_info}" + srch_documentation + "\n\nProvide COMMAND or SRCH or REPORT:"
        config['OllamaPrompt'] = {'template': default_prompt}
        config['CloudPrompt'] = {'template': default_prompt}
        config['OllamaPromptWithAsk'] = {'template': default_prompt + "\nOr ASK:"}
        config['CloudPromptWithAsk'] = {'template': default_prompt + "\nOr ASK:"}
        config['OllamaSummarizePrompt'] = {'template': "Summarize history based on objective: {objective}\nHistory: {history}"}
        config['CloudSummarizePrompt'] = {'template': "Summarize history based on objective: {objective}\nHistory: {history}"}

        # --- NEW: Step Output Summarization Prompts ---
        default_step_summary = """The following command output is too long. Summarize it concisely.
Rules:
1. Preserve all error messages, warnings, and exit codes.
2. Preserve the last 5-10 lines of output exactly.
3. Keep key data points (IPs, paths, IDs).
4. State clearly that this is a summary.

Output to summarize:
{output}"""

        config['OllamaStepSummaryPrompt'] = {'template': default_step_summary}
        config['CloudStepSummaryPrompt'] = {'template': default_step_summary}

        # --- NEW: Search Results Summarization Prompts ---
        default_search_summary = """Analyze the following search results from the execution history and extract information relevant to the objective.
Objective: {objective}

Search Results:
{results}

Instructions:
1. Synthesize the findings into a concise answer.
2. If commands or paths are found, list them explicitly.
3. Ignore irrelevant logs."""

        config['OllamaSearchSummaryPrompt'] = {'template': default_search_summary}
        config['CloudSearchSummaryPrompt'] = {'template': default_search_summary}

        # Validator prompts with system_info for OS-specific validation
        default_validator_prompt = """Validate this command for safety and output size.
System: {system_info}
Sudo Available: {sudo_available}
Command: {command}
Reason: {reason}
Summarization Threshold: {summarization_threshold} chars
Command Timeout: {command_timeout} seconds

Respond APPROVE or REJECT with reason. Consider:
- OS compatibility (Windows vs Linux commands)
- Output size (reject if likely > threshold)
- Blocking commands (interactive prompts, pagers)
- Long-running commands (reject if likely > timeout)
- Destructive operations"""
        config['OllamaValidatePrompt'] = {'template': default_validator_prompt}
        config['CloudValidatePrompt'] = {'template': default_validator_prompt}

        try:
            with open(CONFIG_FILE_PATH, 'w') as configfile:
                config.write(configfile)
            print(f"Default config file created successfully at {CONFIG_FILE_PATH}")
        except Exception as e:
            print(f"ERROR: Could not create default config file: {e}")
            traceback.print_exc()
            # Returnam un obiect gol in caz de eroare la scriere
            return configparser.ConfigParser(interpolation=None)
    else:
        # Fisierul exista, incercam sa il citim
        try:
            config.read(CONFIG_FILE_PATH)
        except configparser.Error as e:
            print(f"ERROR reading config file {CONFIG_FILE_PATH}: {e}. Returning empty config.")
            return configparser.ConfigParser(interpolation=None) # Returnam obiect gol

    return config

# --- Bloc optional pentru testare ---
if __name__ == '__main__':
    print("Testing config.py...")
    print(f"APP_DIR: {APP_DIR}")
    print(f"KEYS_DIR: {KEYS_DIR}")
    print(f"CONFIG_FILE_PATH: {CONFIG_FILE_PATH}")
    print(f"CONNECTIONS_FILE_PATH: {CONNECTIONS_FILE_PATH}")
    print(f"SESSION_FILE_PATH: {SESSION_FILE_PATH}")
    print(f"EXECUTION_LOG_FILE_PATH: {EXECUTION_LOG_FILE_PATH}") # Afisam noua cale

    # Incercam sa citim configuratia
    cfg = get_config()
    if cfg.sections():
        print("\nConfig sections found:")
        for section in cfg.sections():
            print(f"- {section}")
        # print("\nGeneral provider:", cfg.get('General', 'provider', fallback='Not Set'))
    else:
        print("\nConfig file might be empty or unreadable.")

    print("\nconfig.py tests finished.")

