import requests
import google.generativeai as genai
import traceback
from langchain_anthropic import ChatAnthropic

# --- LLM Utility Functions ---

def check_ollama_connection(api_url: str):
    """
    Checks if the Ollama API is reachable and returns available models.
    Returns (True, "Success message", [models]) or (False, "Error message", []).
    """
    if not api_url:
        return False, "Ollama API URL cannot be empty.", []
    
    try:
        # The /api/tags endpoint lists all local models
        response = requests.get(f"{api_url}/api/tags", timeout=5)
        response.raise_for_status() # Raise an exception for bad status codes (4xx, 5xx)
        
        models_data = response.json()
        model_names = [model['name'] for model in models_data.get('models', [])]
        
        if not model_names:
            return True, "Ollama connection successful, but no models found.", []
            
        return True, "Ollama connection successful.", sorted(model_names)
        
    except requests.exceptions.ConnectionError:
        return False, f"Connection refused at {api_url}. Is Ollama running?", []
    except requests.exceptions.Timeout:
        return False, f"Connection timed out when trying to reach {api_url}.", []
    except requests.exceptions.RequestException as e:
        return False, f"Ollama connection error: {e}", []
    except Exception as e:
        # Catch other potential errors like JSON parsing
        traceback.print_exc()
        return False, f"Unexpected error checking Ollama: {e}", []

def check_gemini_connection(api_key: str):
    """
    Checks if the Gemini API key is valid and returns compatible models.
    Returns (True, "Success message", [models]) or (False, "Error message", []).
    """
    if not api_key:
        return False, "Gemini API Key cannot be empty.", []
        
    try:
        genai.configure(api_key=api_key)
        
        # List models and filter for those that support 'generateContent'
        gemini_models = [
            model.name for model in genai.list_models()
            if 'generateContent' in model.supported_generation_methods
        ]
        
        if not gemini_models:
            return False, "API Key is valid, but no compatible models found.", []
            
        return True, "Gemini API Key is valid.", sorted(gemini_models)
        
    except Exception as e:
        # Catch auth errors, permission errors, etc.
        traceback.print_exc()
        return False, f"Gemini API Key validation failed: {str(e)}", []

def check_anthropic_connection(api_key: str):
    """
    Checks Anthropic API key validity and FETCHES the dynamic model list from their API.
    Returns (True, "Success", [models]) or (False, "Error", []).
    """
    if not api_key:
        return False, "Anthropic API Key cannot be empty.", []

    try:
        # 1. Fetch Models dynamically from Anthropic API
        # This ensures we always have the latest models without hardcoding
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01"
        }
        # We use standard requests since langchain might not wrap the list endpoint yet
        response = requests.get("https://api.anthropic.com/v1/models", headers=headers, timeout=10)

        if response.status_code != 200:
            return False, f"Anthropic API Error: {response.status_code} - {response.text}", []

        data = response.json()

        # Extract model IDs (filter for actual models if needed)
        models = [item['id'] for item in data.get('data', []) if item.get('type') == 'model']

        if not models:
            return False, "API Key valid but no models returned by Anthropic.", []

        # 2. Optional: Quick generation test (sanity check)
        # Use the first available model to verify generation rights
        test_model = models[0]
        llm = ChatAnthropic(model=test_model, api_key=api_key, max_tokens=1)
        llm.invoke("Hi")

        return True, f"Anthropic connected. Found {len(models)} models.", sorted(models)

    except Exception as e:
        traceback.print_exc()
        return False, f"Anthropic connection failed: {str(e)}", []

# Optional: Test block if run directly
if __name__ == '__main__':
    print("Running llm_utils.py directly...")
    
    # --- Test Ollama ---
    # print("\nTesting Ollama Connection (http://localhost:11434)...")
    # ollama_ok, ollama_msg, ollama_models = check_ollama_connection("http://localhost:11434")
    # print(f"Ollama OK: {ollama_ok}")
    # print(f"Message: {ollama_msg}")
    # if ollama_models:
    #     print(f"Models: {ollama_models[:5]}") # Print first 5 models
        
    # --- Test Gemini ---
    # print("\nTesting Gemini Connection (requires API key)...")
    # # IMPORTANT: Replace with a real key for testing
    # TEST_API_KEY = "YOUR_API_KEY_HERE" 
    # if TEST_API_KEY == "YOUR_API_KEY_HERE":
    #     print("Skipping Gemini test. Please set TEST_API_KEY.")
    # else:
    #     gemini_ok, gemini_msg, gemini_models = check_gemini_connection(TEST_API_KEY)
    #     print(f"Gemini OK: {gemini_ok}")
    #     print(f"Message: {gemini_msg}")
    #     if gemini_models:
    #         print(f"Models: {gemini_models[:5]}") # Print first 5 models

