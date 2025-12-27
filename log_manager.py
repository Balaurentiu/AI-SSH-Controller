import os
import re
import json
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
from config import KEYS_DIR, APP_DIR, EXECUTION_LOG_LLM_CONTEXT_PATH

# ===========================
# === LOG PATHS ===
# ===========================

EXECUTION_LOG_PATH = os.path.join(KEYS_DIR, 'execution_log.txt')

# Ensure keys directory exists
os.makedirs(KEYS_DIR, exist_ok=True)


# ===========================
# === BASE LOG MANAGER ===
# ===========================

class BaseLogManager:
    """
    Manages the immutable Full Log stored in execution_log.txt.
    This is the single source of truth for all logging.
    Format matches the specification exactly.
    """

    def __init__(self, log_path: str = EXECUTION_LOG_PATH):
        self.log_path = log_path
        self._ensure_log_exists()
        self.current_step = 0
        self.current_objective = ""
        self.current_system_info = ""

    def _ensure_log_exists(self):
        """Ensure log file exists."""
        if not os.path.exists(self.log_path):
            with open(self.log_path, 'w', encoding='utf-8') as f:
                pass  # Create empty file

    def _append(self, text: str):
        """Append text to log file."""
        try:
            with open(self.log_path, 'a', encoding='utf-8') as f:
                f.write(text)
        except Exception as e:
            print(f"ERROR appending to full log: {e}")

    def log_new_task(self, objective: str, system_info: str):
        """Log a new task starting."""
        self.current_objective = objective
        self.current_system_info = system_info
        self.current_step = 0

        log_entry = f"""
=== NEW TASK STARTED ===
Objective: {objective}
System Info: {system_info}
=========================

"""
        self._append(log_entry)

    def log_step_start(self, step_num: int):
        """Log step start."""
        self.current_step = step_num
        self._append(f"--- STEP {step_num} ---\n")

    def log_reason(self, reason: str):
        """Log reasoning."""
        self._append(f"REASON: {reason}\n")

    def log_command_to_execute(self, command: str):
        """Log command that will be executed."""
        self._append(f"COMMAND TO EXECUTE: {command}\n")

    def log_validator_result(self, approved: bool, mode: str, reason: str = ""):
        """Log validation result."""
        if approved:
            if mode == 'independent':
                self._append("VALIDATOR: APPROVED (Independent mode)\n")
            else:
                self._append("VALIDATOR: APPROVED by user (Assisted mode)\n")
        else:
            self._append(f"VALIDATOR: REJECTED - {reason}\n")

    def log_command_executed(self, command: str):
        """Log actual command executed."""
        self._append(f"COMMAND EXECUTED: {command}\n")

    def log_output(self, output: str, success: bool):
        """Log command output."""
        self._append("OUTPUT:\n")
        if output.strip():
            self._append(f"{output}\n")
        else:
            if success:
                self._append("Success: Command executed with no output.\n")
            else:
                self._append("Error: Command failed with no output.\n")

    def log_step_end(self):
        """Log step end."""
        self._append("--- STEP END ---\n\n")

    def log_task_completed(self, report: str):
        """Log task completion."""
        log_entry = f"""=== TASK COMPLETED ===
REPORT: {report}
=======================

"""
        self._append(log_entry)

    def log_ask_question(self, question: str, reason: str = ""):
        """Log agent asking a question."""
        if reason:
            self._append(f"REASON: {reason}\n")
        self._append(f"ASK: {question}\n")

    def log_ask_answer(self, answer: str):
        """Log human answer."""
        self._append(f"HUMAN RESPONSE: {answer}\n")

    def log_intervention(self, intervention_type: str, details: str):
        """Log human intervention."""
        self._append(f"INTERVENTION: {intervention_type} - {details}\n")

    def log_search(self, query: str, results: str):
        """Log search operation."""
        log_entry = f"""--- SEARCH ---
Query: {query}
Results:
{results}
--- SEARCH END ---

"""
        self._append(log_entry)

    def log_file_content(self, path: str, content: str):
        """
        Log the content of a file being written.
        Wrapped in markers to be easily identifying during search.
        """
        header = f"--- FILE CONTENT WRITTEN TO {path} ---\n"
        footer = "\n--- END FILE CONTENT ---\n\n"
        self._append(header + content + footer)

    def read_full_log(self) -> str:
        """Read and return the entire Full Log."""
        try:
            if os.path.exists(self.log_path):
                with open(self.log_path, 'r', encoding='utf-8') as f:
                    return f.read()
            return ""
        except Exception as e:
            print(f"ERROR reading full log: {e}")
            return ""

    def reset_log(self):
        """Reset the full log (creates backup first)."""
        if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > 0:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(KEYS_DIR, f"execution_log_backup_{timestamp}.txt")
            try:
                import shutil
                shutil.copy(self.log_path, backup_path)
                print(f"Full log backed up to: {backup_path}")
            except Exception as e:
                print(f"ERROR backing up log: {e}")

        with open(self.log_path, 'w', encoding='utf-8') as f:
            f.write("")

        self.current_step = 0
        self.current_objective = ""
        self.current_system_info = ""


# ===========================
# === VIEW GENERATOR ===
# ===========================

class ViewGenerator:
    """Generates different views extracted from the Full Log."""

    def __init__(self, base_log_manager: BaseLogManager):
        self.base_log = base_log_manager

    def get_actions_view(self) -> str:
        """Extract Actions Mode view."""
        full_log = self.base_log.read_full_log()
        actions = []

        for line in full_log.splitlines():
            line_stripped = line.strip()
            if line_stripped.startswith("=== NEW TASK STARTED ==="):
                actions.append("Starting new task")
            elif line_stripped.startswith("--- STEP "):
                actions.append("Thinking...")
            elif line_stripped.startswith("VALIDATOR: APPROVED"):
                actions.append("Command approved")
            elif line_stripped.startswith("VALIDATOR: REJECTED"):
                actions.append("Command rejected by validator")
            elif line_stripped.startswith("COMMAND EXECUTED:"):
                actions.append("Executing command...")
            elif line_stripped.startswith("OUTPUT:"):
                actions.append("Command executed successfully")
            elif line_stripped.startswith("=== TASK COMPLETED ==="):
                actions.append("Task finished")
            elif line_stripped.startswith("REPORT:"):
                actions.append("Report generated")
            elif line_stripped.startswith("ASK:"):
                actions.append("Agent asking question")
            elif line_stripped.startswith("HUMAN RESPONSE:"):
                actions.append("Human responded")
            elif line_stripped.startswith("--- Summarization ---"):
                actions.append(line_stripped)
            elif line_stripped.startswith("=== USER MANUALLY EDITED"):
                actions.append("User manually edited memory/context")

        # PERFORMANCE FIX: Limit actions view
        MAX_ACTION_LINES = 2000
        if len(actions) > MAX_ACTION_LINES:
            return "... [Older actions truncated] ...\n" + '\n'.join(actions[-MAX_ACTION_LINES:])

        return '\n'.join(actions)

    def get_commands_view(self) -> str:
        """Extract Commands Mode view."""
        full_log = self.base_log.read_full_log()
        lines = []
        current_step = None
        in_task = False

        for line in full_log.splitlines():
            line_stripped = line.strip()
            if line_stripped.startswith("=== NEW TASK STARTED ==="):
                in_task = True
                lines.append("=== NEW TASK ===")
            elif line_stripped.startswith("--- STEP "):
                match = re.search(r'--- STEP (\d+) ---', line_stripped)
                if match:
                    current_step = match.group(1)
            elif line_stripped.startswith("COMMAND EXECUTED:") and current_step:
                command = line_stripped.replace("COMMAND EXECUTED:", "").strip()
                lines.append(f"STEP {current_step}: {command}")
            elif line_stripped.startswith("=== TASK COMPLETED ==="):
                lines.append("=== TASK END ===")
                in_task = False

        # PERFORMANCE FIX: Limit commands view
        MAX_CMD_LINES = 2000
        if len(lines) > MAX_CMD_LINES:
            return "... [Older commands truncated] ...\n" + '\n'.join(lines[-MAX_CMD_LINES:])

        return '\n'.join(lines)

    def get_vm_screen_view(self) -> str:
        """Extract VM Screen view."""
        full_log = self.base_log.read_full_log()
        lines = []
        objective = ""
        username = "user"
        ip = "remote"
        current_command = ""
        collecting_output = False
        output_lines = []

        for line in full_log.splitlines():
            line_stripped = line.strip()
            if line_stripped.startswith("Objective:"):
                objective = line_stripped.replace("Objective:", "").strip()
                lines.append("=== Objective ===")
                lines.append(objective)
                lines.append("=================")
                lines.append("")
            elif line_stripped.startswith("System Info:"):
                system_info = line_stripped.replace("System Info:", "").strip()
                user_match = re.search(r'user:\s*(\w+)', system_info, re.IGNORECASE)
                ip_match = re.search(r'IP:\s*([\d\.]+)', system_info)
                if user_match:
                    username = user_match.group(1)
                if ip_match:
                    ip = ip_match.group(1)
            elif line_stripped.startswith("COMMAND EXECUTED:"):
                current_command = line_stripped.replace("COMMAND EXECUTED:", "").strip()
                lines.append(f"{username}@{ip}~# {current_command}")
            elif line_stripped.startswith("OUTPUT:"):
                collecting_output = True
                output_lines = []
            elif collecting_output:
                if line_stripped.startswith("--- STEP END ---") or line_stripped.startswith("--- STEP "):
                    if output_lines:
                        lines.extend(output_lines)
                    else:
                        lines.append("(no output)")
                    lines.append("")
                    collecting_output = False
                    output_lines = []
                else:
                    output_lines.append(line.rstrip())
            elif line_stripped.startswith("=== TASK COMPLETED ==="):
                lines.append("===== TASK END =====")

        if collecting_output and output_lines:
            lines.extend(output_lines)
            lines.append("")

        # PERFORMANCE FIX: Truncate view if too large
        # Browser cannot handle 40MB textareas. Limit to last 5000 lines (~500KB-1MB).
        MAX_VIEW_LINES = 5000
        if len(lines) > MAX_VIEW_LINES:
            truncated_lines = [f"... [Older content truncated for performance. Showing last {MAX_VIEW_LINES} lines] ...\n"]
            truncated_lines.extend(lines[-MAX_VIEW_LINES:])
            return '\n'.join(truncated_lines)

        return '\n'.join(lines)


# ===========================
# === AGENT MEMORY MANAGER ===
# ===========================

class AgentMemoryManager:
    """
    Manages LLM Context (Working Memory) using a dedicated persistent file.
    Direct read/write access to EXECUTION_LOG_LLM_CONTEXT_PATH.
    """

    def __init__(self, base_log_manager: BaseLogManager):
        self.base_log = base_log_manager
        self.context_path = EXECUTION_LOG_LLM_CONTEXT_PATH
        self._ensure_context_exists()

    def _ensure_context_exists(self):
        """Ensure context file exists."""
        if not os.path.exists(self.context_path):
            with open(self.context_path, 'w', encoding='utf-8') as f:
                f.write("No commands have been executed yet.")

    def extract_llm_context(self, keep_last_n_steps: int = 0) -> str:
        """
        READ: Get current LLM context directly from the persistent file.
        """
        try:
            with open(self.context_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            return f"Error reading context file: {e}"

    def append_to_context(self, text: str):
        """
        APPEND: Add new activity to the LLM context file.
        """
        try:
            with open(self.context_path, 'a', encoding='utf-8') as f:
                f.write(text) # Text usually comes with newlines prepared
        except Exception as e:
            print(f"Error appending to LLM context: {e}")

    def overwrite_context(self, new_content: str):
        """
        WRITE: Completely replace the LLM context.
        """
        try:
            with open(self.context_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
        except Exception as e:
            print(f"Error overwriting LLM context: {e}")

    def set_summarized_history(self, summarized_text: str):
        """Alias for overwrite_context."""
        self.overwrite_context(summarized_text)

    def get_context_size(self) -> int:
        """Get size of current LLM context file."""
        try:
            return os.path.getsize(self.context_path)
        except:
            return 0


# ===========================
# === UNIFIED LOG FACADE ===
# ===========================

class UnifiedLogManager:
    """Unified interface for all logging operations."""

    def __init__(self):
        self.base_log = BaseLogManager()
        self.view_generator = ViewGenerator(self.base_log)
        self.agent_memory = AgentMemoryManager(self.base_log)

    # === Task Lifecycle ===
    def log_new_task(self, objective: str, system_info: str):
        self.base_log.log_new_task(objective, system_info)

        # Also append to LLM Context so agent sees the new goal
        context_entry = f"\n\n=== NEW TASK STARTED ===\nObjective: {objective}\nSystem Info: {system_info}\n=========================\n"
        self.agent_memory.append_to_context(context_entry)

    def log_task_completed(self, report: str):
        self.base_log.log_task_completed(report)

    # === Step Operations ===
    def log_step_start(self, step_num: int, reason: str, command: str):
        self.base_log.log_step_start(step_num)
        if reason: self.base_log.log_reason(reason)
        self.base_log.log_command_to_execute(command)

    def log_validator_result(self, approved: bool, mode: str, reason: str = ""):
        self.base_log.log_validator_result(approved, mode, reason)

    def log_command_execution(self, command: str, output: str, success: bool):
        self.base_log.log_command_executed(command)
        self.base_log.log_output(output, success)

    def log_step_end(self):
        self.base_log.log_step_end()

    # === Interactions ===
    def log_ask_question(self, question: str, reason: str = ""):
        self.base_log.log_ask_question(question, reason)

    def log_ask_answer(self, answer: str):
        self.base_log.log_ask_answer(answer)

    def log_intervention(self, intervention_type: str, details: str):
        self.base_log.log_intervention(intervention_type, details)

    def log_search(self, query: str, results: str):
        self.base_log.log_search(query, results)

    # === NEW: File Content Logging ===
    def log_file_content(self, path: str, content: str):
        """Log file content to full log for audit and searchability."""
        self.base_log.log_file_content(path, content)

    # === NEW: Manual Edit Logging ===
    def log_manual_edit(self, new_context_content: str):
        """
        Log a manual edit event to the full log and update memory.
        """
        # 1. Log to disk (so we have a record that user changed things)
        log_entry = f"\n\n=== USER MANUALLY EDITED MEMORY/CONTEXT ===\n" \
                    f"Note: The user has rewritten the agent's memory at this point.\n" \
                    f"===========================================\n\n"
        self.base_log._append(log_entry)

        # 2. Overwrite the memory file
        self.agent_memory.overwrite_context(new_context_content)

    # === Views & Context ===
    def get_full_log(self) -> str:
        """Get the Full Log (truncated for UI performance)."""
        full_text = self.base_log.read_full_log()

        # Hard limit for UI display (e.g. 1MB approx 100k chars)
        # If the user needs the REAL full log, they should use 'Save Session' (ZIP)
        MAX_CHARS = 500000 # 500KB

        if len(full_text) > MAX_CHARS:
            return f"... [Log too large for browser ({len(full_text)//1024} KB). Download Session to view full log.] ...\n\n" + full_text[-MAX_CHARS:]

        return full_text

    def get_actions_view(self) -> str:
        return self.view_generator.get_actions_view()

    def get_commands_view(self) -> str:
        return self.view_generator.get_commands_view()

    def get_vm_screen_view(self) -> str:
        return self.view_generator.get_vm_screen_view()

    def get_llm_context(self) -> str:
        """Get LLM Context (what the agent sees)."""
        return self.agent_memory.extract_llm_context()

    def set_summarized_history(self, summarized_text: str):
        self.agent_memory.set_summarized_history(summarized_text)

    def append_to_llm_context(self, text: str):
        """Directly append text to the agent's working memory file."""
        self.agent_memory.append_to_context(text)

    def get_context_size(self) -> int:
        return self.agent_memory.get_context_size()

    def search_past_context(self, query: str, limit: int = 50) -> str:
        full_log = self.base_log.read_full_log()
        if not full_log: return "No log data available."

        # CLEANUP: Remove surrounding quotes and backticks to improve matching hits
        # Agent often asks for SRCH: "/path/file" or `error`, but log has clean text
        # We strip: spaces, double quotes ("), single quotes ('), and backticks (`)
        clean_query = query.strip().strip('"\'`')

        lines = full_log.split('\n')
        matching_sections = []
        query_lower = clean_query.lower()
        for i, line in enumerate(lines):
            if query_lower in line.lower():
                start = max(0, i - 2)
                end = min(len(lines), i + 3)
                section = '\n'.join(lines[start:end])
                matching_sections.append(section)
                if len(matching_sections) >= limit: break
        if not matching_sections: return f"No matches found for: {clean_query}"
        result = f"=== SEARCH RESULTS FOR: {clean_query} ===\n\n"
        result += "\n\n---\n\n".join(matching_sections)
        result += f"\n\n=== END SEARCH ({len(matching_sections)} matches) ==="
        return result

    def reset_all(self):
        """Reset all logs and LLM context."""
        self.base_log.reset_log()
        # Reset LLM context file to initial state
        self.agent_memory.overwrite_context("No commands have been executed yet.")
