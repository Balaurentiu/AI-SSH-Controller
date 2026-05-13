import os
import configparser
import traceback

# --- CAI PERSISTENTE ---
# Directorul radacina al aplicatiei (unde se afla acest fisier)
# Support for PyInstaller: use environment variables if set (from run.py)
APP_DIR = os.environ.get('APP_DIR', os.path.dirname(os.path.abspath(__file__)))
# Directorul pentru chei SSH, conexiuni si loguri
KEYS_DIR = os.environ.get('KEYS_DIR', os.path.join(APP_DIR, 'keys'))
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
# --- Fisierul pentru Agent Execution Log (Live) - persistent across rebuilds ---
AGENT_LIVE_LOG_PATH = os.path.join(KEYS_DIR, 'agent_live_log.txt')
# --- NOU: Fisierul pentru istoricul conversatiei chat ---
CHAT_LOG_FILE_PATH = os.path.join(KEYS_DIR, 'chat_history.json')
# --- NOU: Fisierul pentru planul de actiune multi-step ---
ACTION_PLAN_FILE_PATH = os.path.join(KEYS_DIR, 'action_plan.json')
# --- Validator per-system command whitelist ---
VALIDATOR_WHITELIST_PATH = os.path.join(KEYS_DIR, 'validator_whitelist.json')
# --- NOU: Directorul pentru documente knowledge ---
KNOWLEDGE_DIR = os.path.join(KEYS_DIR, 'knowledge')

# Asiguram ca directorul pentru chei exista la importarea modulului
try:
    os.makedirs(KEYS_DIR, exist_ok=True)
    os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
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
        config['Agent'] = {'model_name': 'llama3:latest', 'max_steps': '50', 'summarization_threshold': '15000', 'command_timeout': '120', 'llm_timeout': '120', 'chat_history_message_count': '20', 'temperature': '0.5'}
        # Calea SSH default este acum relativa la KEYS_DIR
        config['System'] = {'ip_address': '', 'username': '', 'ssh_port': '22', 'ssh_key_path': os.path.join(KEYS_DIR, 'id_rsa')}
        config['Ollama'] = {'api_url': 'http://localhost:11434', 'num_ctx': '32768'}
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

        # --- NEW: Chat Prompt ---
        # Available variables: {objective}, {system_info}, {history}, {chat_history}, {user_message}
        default_chat_prompt = """You are an intelligent DevOps Assistant connected to a remote system.
You have read access to the EXECUTION HISTORY of tasks performed so far.
GENERAL TONE: Helpful, technical, concise.

CONTEXT:
Current Objective: {objective}
System Info: {system_info}

EXECUTION HISTORY (Read-only memory of past actions):
{history}

USER MESSAGE:
{user_message}

INSTRUCTIONS:
1. ANALYZE HISTORY: Always check the 'EXECUTION HISTORY' first. If the user asks about past errors, outputs, or configs, answer strictly based on what is logged there.
2. SYSTEM STATUS: If the user asks about system state (uptime, disk space, services) and this info is NOT in the history, you must request a new task to check it.

3. PROPOSING TASKS: If the user wants to act (install software, fix an error, check the status), DO NOT claim you are doing it. You cannot execute commands directly in this chat. Instead, PROPOSE the task using this exact format:
<<REQUEST_TASK: [Clear, concise objective for the new task]>>

Example:
User: "Check why Apache isn't running."
You: "I don't see the status in the recent logs. I can check the service for you.
<<REQUEST_TASK: Check apache2 service status and logs>>"

4. Search in the full execution history log.
* WHEN TO USE:
a). Use this when the current context is summarised, and you have lost track of specific file paths, configuration values, command outputs from previous steps or any other details that you need.
b). When the user asks to modify/fix a specific file from a previous task, and its content or name is not visible in the current context.
c). When you need to find something from a large output that was summarised to prevent flooding the context window.

*HOW TO SRCH: Use the format below.

REASON: [Explain what information you are looking for and why.]
SRCH: [Keywords or specific string to find in logs]

A result of the search will be added to the current history, and you will have access to the requested data.

5. Create an action plan with multiple consecutive objectives that need to be completed to achieve the master goal.
* WHEN TO USE:
a). When a single REQUEST_TASK action is not enough to complete the master goal.
b). When the user or the situation requests multiple tasks to be completed.
c). When the request is more complex and needs to be split into multiple tasks.

* HOW TO USE IT: Use the format below.

Example:
<<ACTION_PLAN_START>>
Title: My Workflow
Step 1. First task
Step 2: Second task (colon works)
Step 3) Third task (parenthesis works)
step 4 Fourth task (lowercase works)
<<ACTION_PLAN_STOP>>

The application will help you keep track of the executed steps and when the plan is complete or not.
"""
        config['ChatPrompt'] = {'template': default_chat_prompt}

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

        # --- Report Validator Prompts ---
        default_validate_report_prompt = """You are a strict auditor. Your job is to verify that the agent's final REPORT accurately reflects what was actually executed on the remote system.

TASK OBJECTIVE:
{objective}

EXECUTION HISTORY (commands actually run and their outputs):
{history}

AGENT'S FINAL REPORT:
{report}

IMPORTANT — HOW TO READ THE EXECUTION HISTORY:
The execution history may begin with a SUMMARIZED SECTION. When the history grows long, earlier steps are automatically condensed into a summary paragraph. This means:
- A step mentioned in the report may appear only in summarized form (e.g. "Checked disk usage and memory — both within normal limits") rather than as raw command output.
- A summary that confirms a step was completed IS valid evidence. Do not reject a claim simply because its raw output is absent — if the summary covers it, accept it.
- Only reject claims that appear in NEITHER the detailed history NOR any summary block.

INSTRUCTIONS:
Carefully compare the REPORT against the EXECUTION HISTORY (including any summarized sections).
- Does the report claim commands were executed that are not covered by either detailed output or a summary?
- Does the report claim verification steps that were never performed and not mentioned in any summary?
- Does the report state outcomes without any corresponding evidence (direct output or summary) in the history?

If the report accurately reflects what appears in the execution history (detailed or summarized), respond:
APPROVE

If the report contains claims not backed by any evidence in the history, respond:
REJECT
REASON: [Specific description of each claim not supported by execution history or summaries]

Respond ONLY with APPROVE or REJECT followed by REASON on a new line."""
        config['OllamaValidateReportPrompt'] = {'template': default_validate_report_prompt}
        config['CloudValidateReportPrompt'] = {'template': default_validate_report_prompt}

        # --- Knowledge Injection Prompt ---
        default_knowledge_prompt = """--- UPLOADED KNOWLEDGE DOCUMENTS ---
The following reference documents have been uploaded for your use.
Use this information to complete the objective when relevant.

{documents}

--- END KNOWLEDGE DOCUMENTS ---

You can search for more specific information from large uploaded documents using:
KNOWLEDGE: <search query>
"""
        config['KnowledgePrompt'] = {'template': default_knowledge_prompt}

        # --- Web Search Module Configuration ---
        config['WebSearch'] = {
            'enabled': 'False',
            'provider': 'ollama',
            'model_name': '',
            'temperature': '0.3',
            'context_size': '8192',
            'timeout': '120',
            'max_retries': '5',
            'max_results': '5',
            'max_fetch_pages': '3',
            'max_page_size': '50000',
            'brief_threshold': '2000',
            'search_engine': 'duckduckgo',
            'region': 'wt-wt',
            'safe_search': 'off',
            'ollama_url': '',
            'api_key': ''
        }

        default_websearch_prompt = """You are an autonomous web research assistant. Your job is to find accurate, relevant information from the web.

REASON: {reason}
QUERY: {query}

Your approach:
1. Formulate optimal search queries
2. Evaluate search results for relevance
3. Extract and summarize key information
4. Provide clear, actionable answers

Always prioritize:
- Official documentation and authoritative sources
- Recent and up-to-date information
- Specific technical details (commands, configurations, code examples)
- Accuracy over completeness"""
        config['WebSearchPrompt'] = {'template': default_websearch_prompt}

        # --- Web Search Chat Injection Template ---
        default_websearch_injection = """--- WEB SEARCH CAPABILITY ---
You have access to web search. Use it when you need information that is not in your training data, the execution history, or uploaded knowledge documents.

WHEN TO USE:
- When you need technical documentation, guides, or how-to information
- When the user explicitly asks you to look something up online
- When you need current information (latest versions, release notes, CVEs, etc.)
- When the execution history and your own knowledge are insufficient

HOW TO USE: Use the format below.

REASON: [Explain what information you need and why]
WEB_SEARCH: [Concise search query]

Example:
REASON: I need to find the specific VLAN configuration commands for Cisco Catalyst switches.
WEB_SEARCH: cisco catalyst switch vlan configuration guide

The system will search the web, evaluate results, and provide the relevant information back to you.
If the content is large, it will be attached as a knowledge document that you can query later with KNOWLEDGE: action.
--- END WEB SEARCH CAPABILITY ---"""
        config['WebSearchInjection'] = {'template': default_websearch_injection}

        # --- System Messages (hidden auto-injected chat messages) ---
        config['SystemMessages'] = {
            'task_completed': """[ACTION COMPLETED]
The execution task has finished successfully.
Final Report: {final_report}

INSTRUCTION:
Inform the user that the task is done. Summarize the outcome in 2-3 sentences using your own words — do NOT copy the report verbatim. If the user asked a specific question before the task started, answer it directly based on the report. Then ask if they need anything else.""",
            'search_completed': "Search completed. The results have been added to the context above. Analyze them and answer the user's original question directly, citing specific findings. Do not ask the user to wait for more information.",
            'knowledge_completed': "Knowledge search completed. The relevant document excerpts are available above. Use them to answer the user's original question with specific details. If the results are insufficient, you may search again with a more specific query.",
            'switch_timeout': "[SYSTEM EVENT] Your request to switch to '{target_system}' timed out - no user response was received within the time limit. Please acknowledge this to the user.",
            'switch_approved': "[SYSTEM EVENT] SUCCESS: The user APPROVED your request. System context has been switched to '{new_system}'. You are now connected to this system. Please confirm the switch to the user and continue assisting them.",
            'switch_denied': "[SYSTEM EVENT] DENIED: The user REJECTED your request to switch to '{target_system}'. Reason: {error_msg}. Please acknowledge this to the user and continue assisting them with the current system.",
            'web_search_completed': "Web search completed. The results have been added to the context. Please analyze the web search findings and provide relevant information to the user.",
            'web_search_completed_injected': "Web search completed. The findings are included directly above in the context. Analyze them and answer the user's question with specific details from the source.",
            'web_search_completed_attached': "Web search completed. The full content has been attached to the Knowledge store as a document. Use KNOWLEDGE: <query> to retrieve specific information from it, then answer the user's question."
        }

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

        # Backfill missing sections for existing config files (e.g. after upgrade)
        needs_write = False
        if not config.has_section('SystemMessages'):
            config['SystemMessages'] = {
                'task_completed': """[ACTION COMPLETED]
The execution of the initiated task is finished.
Final Report: {final_report}

INSTRUCTION:
Based on the previous conversation, briefly inform the user that the task is done and summarize the outcome.
Be natural, and continue the conversation.""",
                'search_completed': "Search completed. The results have been added to the execution history. Please analyze the search results and provide relevant findings to the user.",
                'knowledge_completed': "Knowledge search completed. The results have been added to the context. Please analyze the knowledge results and provide relevant findings to the user.",
                'switch_timeout': "[SYSTEM EVENT] Your request to switch to '{target_system}' timed out - no user response was received within the time limit. Please acknowledge this to the user.",
                'switch_approved': "[SYSTEM EVENT] SUCCESS: The user APPROVED your request. System context has been switched to '{new_system}'. You are now connected to this system. Please confirm the switch to the user and continue assisting them.",
                'switch_denied': "[SYSTEM EVENT] DENIED: The user REJECTED your request to switch to '{target_system}'. Reason: {error_msg}. Please acknowledge this to the user and continue assisting them with the current system."
            }
            needs_write = True
            print("Backfilled [SystemMessages] section into existing config.")

        # Backfill WebSearch section
        if not config.has_section('WebSearch'):
            config['WebSearch'] = {
                'enabled': 'False',
                'provider': 'ollama',
                'model_name': '',
                'temperature': '0.3',
                'context_size': '8192',
                'timeout': '120',
                'max_retries': '5',
                'max_results': '5',
                'max_fetch_pages': '3',
                'max_page_size': '50000',
                'brief_threshold': '2000',
                'search_engine': 'duckduckgo',
                'region': 'wt-wt',
                'safe_search': 'off',
                'ollama_url': '',
                'api_key': '',
                'fallback_models': ''
            }
            needs_write = True
            print("Backfilled [WebSearch] section into existing config.")

        # Backfill fallback_models into existing WebSearch sections
        if config.has_section('WebSearch') and not config.has_option('WebSearch', 'fallback_models'):
            config.set('WebSearch', 'fallback_models', '')
            needs_write = True

        if not config.has_section('WebSearchPrompt'):
            config['WebSearchPrompt'] = {
                'template': """You are an autonomous web research assistant. Your job is to find accurate, relevant information from the web.

Given a REASON (why the information is needed) and a QUERY (what to search for), you will:
1. Formulate optimal search queries
2. Evaluate search results for relevance
3. Extract and summarize key information
4. Provide clear, actionable answers

Always prioritize:
- Official documentation and authoritative sources
- Recent and up-to-date information
- Specific technical details (commands, configurations, code examples)
- Accuracy over completeness"""
            }
            needs_write = True
            print("Backfilled [WebSearchPrompt] section into existing config.")

        # Backfill web_search_completed into SystemMessages
        if config.has_section('SystemMessages') and not config.get('SystemMessages', 'web_search_completed', fallback=''):
            config.set('SystemMessages', 'web_search_completed',
                       "Web search completed. The results have been added to the context. Please analyze the web search findings and provide relevant information to the user.")
            needs_write = True
            print("Backfilled web_search_completed into [SystemMessages].")

        # Backfill improved SystemMessages (task_completed, search_completed, knowledge_completed)
        _sm = 'SystemMessages'
        if config.has_section(_sm):
            _new_task = """[ACTION COMPLETED]
The execution task has finished successfully.
Final Report: {final_report}

INSTRUCTION:
Inform the user that the task is done. Summarize the outcome in 2-3 sentences using your own words — do NOT copy the report verbatim. If the user asked a specific question before the task started, answer it directly based on the report. Then ask if they need anything else."""
            _new_search = "Search completed. The results have been added to the context above. Analyze them and answer the user's original question directly, citing specific findings. Do not ask the user to wait for more information."
            _new_knowledge = "Knowledge search completed. The relevant document excerpts are available above. Use them to answer the user's original question with specific details. If the results are insufficient, you may search again with a more specific query."
            _new_ws_injected = "Web search completed. The findings are included directly above in the context. Analyze them and answer the user's question with specific details from the source."
            _new_ws_attached = "Web search completed. The full content has been attached to the Knowledge store as a document. Use KNOWLEDGE: <query> to retrieve specific information from it, then answer the user's question."

            for _key, _val in [
                ('task_completed', _new_task),
                ('search_completed', _new_search),
                ('knowledge_completed', _new_knowledge),
                ('web_search_completed_injected', _new_ws_injected),
                ('web_search_completed_attached', _new_ws_attached),
            ]:
                if not config.get(_sm, _key, fallback=''):
                    config.set(_sm, _key, _val)
                    needs_write = True
                    print(f"Backfilled {_key} into [SystemMessages].")

        # Backfill WebSearchInjection template
        if not config.has_section('WebSearchInjection'):
            config['WebSearchInjection'] = {
                'template': """--- WEB SEARCH CAPABILITY ---
You have access to web search. Use it when you need information that is not in your training data, the execution history, or uploaded knowledge documents.

WHEN TO USE:
- When you need technical documentation, guides, or how-to information
- When the user explicitly asks you to look something up online
- When you need current information (latest versions, release notes, CVEs, etc.)
- When the execution history and your own knowledge are insufficient

HOW TO USE: Use the format below.

REASON: [Explain what information you need and why]
WEB_SEARCH: [Concise search query]

Example:
REASON: I need to find the specific VLAN configuration commands for Cisco Catalyst switches.
WEB_SEARCH: cisco catalyst switch vlan configuration guide

The system will search the web, evaluate results, and provide the relevant information back to you.
If the content is large, it will be attached as a knowledge document that you can query later with KNOWLEDGE: action.
--- END WEB SEARCH CAPABILITY ---"""
            }
            needs_write = True
            print("Backfilled [WebSearchInjection] section into existing config.")

        # Backfill Knowledge section
        if not config.has_section('Knowledge'):
            config['Knowledge'] = {
                'max_documents': '10',
                'max_file_size_mb': '50'
            }
            needs_write = True
            print("Backfilled [Knowledge] section into existing config.")
        else:
            if not config.has_option('Knowledge', 'max_documents'):
                config.set('Knowledge', 'max_documents', '10')
                needs_write = True
                print("Backfilled max_documents into [Knowledge] section.")
            if not config.has_option('Knowledge', 'max_file_size_mb'):
                config.set('Knowledge', 'max_file_size_mb', '50')
                needs_write = True
                print("Backfilled max_file_size_mb into [Knowledge] section.")

        # Backfill Telegram section
        if not config.has_section('Telegram'):
            config['Telegram'] = {
                'enabled': 'false',
                'bot_token': '',
                'allowed_chat_ids': '',
                'notify_task_done': 'true',
            }
            needs_write = True
            print("Backfilled [Telegram] section into existing config.")

        # Backfill OllamaValidateReportPrompt / CloudValidateReportPrompt
        _default_report_prompt = """You are a strict auditor. Your job is to verify that the agent's final REPORT accurately reflects what was actually executed on the remote system.

TASK OBJECTIVE:
{objective}

EXECUTION HISTORY (commands actually run and their outputs):
{history}

AGENT'S FINAL REPORT:
{report}

IMPORTANT — HOW TO READ THE EXECUTION HISTORY:
The execution history may begin with a SUMMARIZED SECTION. When the history grows long, earlier steps are automatically condensed into a summary paragraph. This means:
- A step mentioned in the report may appear only in summarized form (e.g. "Checked disk usage and memory — both within normal limits") rather than as raw command output.
- A summary that confirms a step was completed IS valid evidence. Do not reject a claim simply because its raw output is absent — if the summary covers it, accept it.
- Only reject claims that appear in NEITHER the detailed history NOR any summary block.

INSTRUCTIONS:
Carefully compare the REPORT against the EXECUTION HISTORY (including any summarized sections).
- Does the report claim commands were executed that are not covered by either detailed output or a summary?
- Does the report claim verification steps that were never performed and not mentioned in any summary?
- Does the report state outcomes without any corresponding evidence (direct output or summary) in the history?

If the report accurately reflects what appears in the execution history (detailed or summarized), respond:
APPROVE

If the report contains claims not backed by any evidence in the history, respond:
REJECT
REASON: [Specific description of each claim not supported by execution history or summaries]

Respond ONLY with APPROVE or REJECT followed by REASON on a new line."""
        if not config.has_section('OllamaValidateReportPrompt'):
            config['OllamaValidateReportPrompt'] = {'template': _default_report_prompt}
            needs_write = True
            print("Backfilled [OllamaValidateReportPrompt] section into existing config.")
        if not config.has_section('CloudValidateReportPrompt'):
            config['CloudValidateReportPrompt'] = {'template': _default_report_prompt}
            needs_write = True
            print("Backfilled [CloudValidateReportPrompt] section into existing config.")

        # Backfill ValidatorLLM section
        if not config.has_section('ValidatorLLM'):
            config['ValidatorLLM'] = {
                'enabled': 'False',
                'provider': 'ollama',
                'model_name': '',
                'ollama_url': '',
                'api_key': ''
            }
            needs_write = True
            print("Backfilled [ValidatorLLM] section into existing config.")

        # Backfill ExecLLMFallback section
        if not config.has_section('ExecLLMFallback'):
            config['ExecLLMFallback'] = {
                'enabled': 'False',
                'provider': 'ollama',
                'model_name': '',
                'ollama_url': '',
                'api_key': ''
            }
            needs_write = True
            print("Backfilled [ExecLLMFallback] section into existing config.")

        # Backfill ChatLLMFallback section
        if not config.has_section('ChatLLMFallback'):
            config['ChatLLMFallback'] = {
                'enabled': 'False',
                'provider': 'ollama',
                'model_name': '',
                'ollama_url': '',
                'api_key': ''
            }
            needs_write = True
            print("Backfilled [ChatLLMFallback] section into existing config.")

        # Backfill ValidatorLLMFallback section
        if not config.has_section('ValidatorLLMFallback'):
            config['ValidatorLLMFallback'] = {
                'enabled': 'False',
                'provider': 'ollama',
                'model_name': '',
                'ollama_url': '',
                'api_key': ''
            }
            needs_write = True
            print("Backfilled [ValidatorLLMFallback] section into existing config.")

        if needs_write:
            try:
                with open(CONFIG_FILE_PATH, 'w') as configfile:
                    config.write(configfile)
            except Exception as e:
                print(f"WARNING: Could not write backfilled config: {e}")

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

