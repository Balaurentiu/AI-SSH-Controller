# GOOD_PROMPTS Directory - Prompt Mapping

This directory contains all the prompt templates used by the AI SSH Controller application.

## File to Config Section Mapping:

1. **Without ASK.txt**
   → OllamaPrompt (for Ollama provider)
   → CloudPrompt (for Gemini/Anthropic providers)
   - Used: Standard task execution without ASK capability
   - Variables: {objective}, {history}, {system_info}, {command_timeout}

2. **With ASK.txt**
   → OllamaPromptWithAsk
   → CloudPromptWithAsk
   - Used: Task execution with ASK capability enabled
   - Variables: Same as Without ASK + ASK action available

3. **Summarisation.txt**
   → OllamaSummarizePrompt
   → CloudSummarizePrompt
   - Used: History compression when context exceeds threshold
   - Variables: {objective}, {history}

4. **Step Summarisation.txt**
   → OllamaStepSummaryPrompt
   → CloudStepSummaryPrompt
   - Used: Summarize large command outputs
   - Variables: {output}

5. **Search Summarisation.txt**
   → OllamaSearchSummaryPrompt
   → CloudSearchSummaryPrompt
   - Used: Summarize SRCH results from execution history
   - Variables: {objective}, {results}

6. **Validator.txt**
   → OllamaValidatePrompt
   → CloudValidatePrompt
   - Used: Command validation in Independent mode
   - Variables: {system_info}, {sudo_available}, {command}, {reason}, {summarization_threshold}, {command_timeout}

7. **Chat.txt**
   → ChatPrompt
   - Used: Chat interface with the agent
   - Variables: {objective}, {system_info}, {history}, {chat_history}, {user_message}, {action_plan_status}

## Status: ✅ ALL PROMPTS PRESENT

All required prompts are accounted for. No missing prompts detected.

Last Updated: 2025-12-30
