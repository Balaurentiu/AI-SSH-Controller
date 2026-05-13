"""
Web Search Module - Autonomous web research agent with its own LLM.
Searches the web via DuckDuckGo, evaluates results, fetches pages,
converts content to markdown, and attaches findings to knowledge store.
"""

import re
import os
import json
import time
import traceback
import threading
import concurrent.futures
from datetime import datetime

# --- Constants ---
MAX_RETRIES = 5
DEFAULT_TIMEOUT = 120
DEFAULT_MAX_RESULTS = 5
DEFAULT_MAX_FETCH_PAGES = 3
DEFAULT_MAX_PAGE_SIZE = 50000
DEFAULT_BRIEF_THRESHOLD = 2000

# Web search status states
STATUS_IDLE = 'idle'
STATUS_SEARCHING = 'searching'
STATUS_EVALUATING = 'evaluating'
STATUS_FETCHING = 'fetching'
STATUS_PROCESSING = 'processing'
STATUS_DONE = 'done'
STATUS_ERROR = 'error'


class WebSearchStatus:
    """Tracks the live status of a web search operation."""

    def __init__(self, socketio=None):
        self.socketio = socketio
        self.status = STATUS_IDLE
        self.query = ''
        self.reason = ''
        self.current_step = 0
        self.total_steps = 5
        self.step_descriptions = [
            'Formulate search query',
            'Search the web',
            'Evaluate results',
            'Fetch & process content',
            'Generate summary'
        ]
        self.step_statuses = ['pending'] * 5  # pending, active, done, skipped
        self.log_lines = []
        self.result_summary = ''
        self.error_message = ''

    def _emit(self):
        """Emit current status via SocketIO."""
        if self.socketio:
            self.socketio.emit('web_search_status', self.to_dict())

    def log(self, message):
        """Add a timestamped log entry."""
        timestamp = datetime.now().strftime('%H:%M:%S')
        entry = f"[{timestamp}] {message}"
        self.log_lines.append(entry)
        print(f"[WEB_SEARCH] {message}", flush=True)
        self._emit()

    def set_step(self, step_index, status='active'):
        """Update step status and emit."""
        if 0 <= step_index < len(self.step_statuses):
            self.step_statuses[step_index] = status
            self.current_step = step_index
        self._emit()

    def complete_step(self, step_index):
        """Mark a step as done."""
        self.set_step(step_index, 'done')

    def skip_step(self, step_index):
        """Mark a step as skipped."""
        self.set_step(step_index, 'skipped')

    def set_status(self, status):
        """Set overall status."""
        self.status = status
        self._emit()

    def to_dict(self):
        return {
            'status': self.status,
            'query': self.query,
            'reason': self.reason,
            'current_step': self.current_step,
            'total_steps': self.total_steps,
            'step_descriptions': self.step_descriptions,
            'step_statuses': self.step_statuses,
            'log_lines': self.log_lines,
            'result_summary': self.result_summary,
            'error_message': self.error_message
        }

    def reset(self):
        """Reset to idle state."""
        self.status = STATUS_IDLE
        self.query = ''
        self.reason = ''
        self.current_step = 0
        self.step_statuses = ['pending'] * 5
        self.log_lines = []
        self.result_summary = ''
        self.error_message = ''


def _create_llm(config):
    """
    Create an LLM instance based on web search config.
    Config dict keys: provider, model_name, temperature, context_size, timeout,
                      ollama_url, gemini_api_key, anthropic_api_key
    """
    provider = config.get('provider', 'ollama')
    model_name = config.get('model_name', '')
    temperature = config.get('temperature', 0.3)
    timeout = config.get('timeout', DEFAULT_TIMEOUT)

    if provider == 'ollama':
        from langchain_community.llms import Ollama
        ollama_url = config.get('ollama_url', 'http://localhost:11434')
        context_size = config.get('context_size', 8192)
        from config import get_config as _gc
        keep_alive = _gc().get('Ollama', 'keep_alive', fallback='-1')
        return Ollama(
            model=model_name,
            base_url=ollama_url,
            timeout=timeout,
            num_ctx=context_size,
            temperature=temperature,
            keep_alive=keep_alive
        )
    elif provider == 'gemini':
        from langchain_google_genai import ChatGoogleGenerativeAI
        api_key = config.get('gemini_api_key', '')
        return ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=api_key,
            generation_config={"temperature": temperature},
            timeout=timeout,
            request_timeout=timeout
        )
    elif provider == 'anthropic':
        from langchain_anthropic import ChatAnthropic
        api_key = config.get('anthropic_api_key', '')
        return ChatAnthropic(
            model=model_name,
            api_key=api_key,
            temperature=temperature,
            timeout=timeout,
            default_request_timeout=timeout
        )
    else:
        raise ValueError(f"Unsupported web search LLM provider: {provider}")


def _invoke_llm(llm, prompt, timeout_seconds):
    """Invoke LLM with timeout using ThreadPoolExecutor."""
    def _invoke(l, p):
        result = l.invoke(p)
        if hasattr(result, 'content'):
            return result.content
        return str(result)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_invoke, llm, prompt)
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"Web search LLM did not respond within {timeout_seconds}s")


def _create_llm_from_spec(provider, model_name, cfg, timeout=120, temperature=0.3):
    """
    Create an LLM from a provider:model_name spec using global API keys.
    provider: 'ollama' | 'gemini' | 'anthropic'
    Returns None if the LLM cannot be created (missing key, bad provider, etc.).
    """
    try:
        if provider == 'ollama':
            from langchain_community.llms import Ollama
            ollama_url = cfg.get('Ollama', 'api_url', fallback='http://localhost:11434')
            num_ctx = cfg.getint('Ollama', 'num_ctx', fallback=8192)
            keep_alive = cfg.get('Ollama', 'keep_alive', fallback='-1')
            return Ollama(model=model_name, base_url=ollama_url, timeout=timeout,
                          num_ctx=num_ctx, temperature=temperature, keep_alive=keep_alive)
        elif provider == 'gemini':
            from langchain_google_genai import ChatGoogleGenerativeAI
            api_key = cfg.get('General', 'gemini_api_key', fallback='')
            if not api_key:
                return None
            return ChatGoogleGenerativeAI(model=model_name, google_api_key=api_key,
                                          generation_config={"temperature": temperature},
                                          timeout=timeout)
        elif provider == 'anthropic':
            from langchain_anthropic import ChatAnthropic
            api_key = cfg.get('General', 'anthropic_api_key', fallback='')
            if not api_key:
                return None
            return ChatAnthropic(model=model_name, api_key=api_key,
                                 temperature=temperature, timeout=timeout)
    except Exception as e:
        print(f"[WEB_SEARCH] Could not create fallback LLM {provider}/{model_name}: {e}", flush=True)
    return None


def _build_fallback_llm_chain(fallback_models_str, cfg, timeout=120, temperature=0.3):
    """
    Parse 'provider:model_name,...' string (up to 5 entries) and return a list
    of LLM instances to try in order when the primary LLM fails.
    Example: 'gemini:models/gemini-2.0-flash,anthropic:claude-haiku-4-5-20251001,ollama:llama3.2'
    """
    chain = []
    if not fallback_models_str:
        return chain
    for entry in fallback_models_str.split(',')[:5]:
        entry = entry.strip()
        if ':' not in entry:
            continue
        provider, model_name = entry.split(':', 1)
        provider = provider.strip().lower()
        model_name = model_name.strip()
        if not model_name:
            continue
        llm = _create_llm_from_spec(provider, model_name, cfg, timeout, temperature)
        if llm is not None:
            chain.append((f"{provider}:{model_name}", llm))
    return chain


def _invoke_llm_with_fallback(llm, fallback_chain, prompt, timeout_seconds):
    """
    Try the primary LLM; if it fails, try each entry in fallback_chain in order.
    Raises the last exception if all attempts fail.
    """
    try:
        return _invoke_llm(llm, prompt, timeout_seconds)
    except Exception as primary_err:
        if not fallback_chain:
            raise
        last_err = primary_err
        for label, fb_llm in fallback_chain:
            try:
                print(f"[WEB_SEARCH] Primary LLM failed ({primary_err}), trying fallback {label}...", flush=True)
                return _invoke_llm(fb_llm, prompt, timeout_seconds)
            except Exception as fb_err:
                last_err = fb_err
                print(f"[WEB_SEARCH] Fallback {label} also failed: {fb_err}", flush=True)
        raise last_err


def _search_ddg(query, max_results=5, region='wt-wt', safe_search='off'):
    """
    Search DuckDuckGo and return results.
    Returns list of dicts: [{title, url, snippet}, ...]
    """
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    results = []
    with DDGS() as ddgs:
        safesearch_map = {'off': 'off', 'moderate': 'moderate', 'strict': 'strict'}
        safesearch = safesearch_map.get(safe_search, 'off')

        for r in ddgs.text(query, region=region, safesearch=safesearch, max_results=max_results):
            results.append({
                'title': r.get('title', ''),
                'url': r.get('href', ''),
                'snippet': r.get('body', '')
            })

    return results


def _fetch_page_content(url, max_size=50000):
    """
    Fetch a web page and extract clean text content.
    Returns (text_content, content_type) or raises on failure.
    """
    import requests

    headers = {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    response = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
    response.raise_for_status()

    content_type = response.headers.get('Content-Type', '').lower()

    # PDF handling
    if 'application/pdf' in content_type or url.lower().endswith('.pdf'):
        try:
            from PyPDF2 import PdfReader
            from io import BytesIO
            reader = PdfReader(BytesIO(response.content))
            text_parts = []
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
            text = '\n\n'.join(text_parts)
            if text.strip():
                return text[:max_size], 'pdf'
        except Exception as e:
            raise ValueError(f"Failed to extract PDF text: {e}")

    # HTML handling
    if 'text/html' in content_type or not content_type:
        try:
            # Try trafilatura first (best quality)
            import trafilatura
            text = trafilatura.extract(response.text, include_links=False, include_tables=True)
            if text and len(text.strip()) > 100:
                return text[:max_size], 'html'
        except ImportError:
            pass

        # Fallback to BeautifulSoup
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response.text, 'html.parser')

            # Remove scripts, styles, nav, footer, header
            for element in soup.find_all(['script', 'style', 'nav', 'footer', 'header', 'aside']):
                element.decompose()

            text = soup.get_text(separator='\n', strip=True)
            # Clean up excessive newlines
            text = re.sub(r'\n{3,}', '\n\n', text)
            if text.strip():
                return text[:max_size], 'html'
        except ImportError:
            pass

        # Last resort: regex strip tags
        text = re.sub(r'<[^>]+>', '', response.text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:max_size], 'html'

    # Plain text
    if 'text/plain' in content_type:
        return response.text[:max_size], 'text'

    raise ValueError(f"Unsupported content type: {content_type}")


def _convert_to_markdown(title, url, content, content_type):
    """Convert fetched content to a clean markdown document."""
    timestamp = datetime.now().strftime('%d.%m.%Y %H:%M:%S')

    md = f"# {title}\n\n"
    md += f"**Source:** {url}\n"
    md += f"**Fetched:** {timestamp}\n"
    md += f"**Type:** {content_type}\n\n"
    md += "---\n\n"
    md += content

    return md


def _try_init_knowledge_manager(global_state, socketio=None):
    """
    Try to initialize a KnowledgeManager for web search storage when one isn't already available.
    Uses config settings to determine embedding provider/model.
    Returns KnowledgeManager or None on failure.
    """
    try:
        from config import get_config, KNOWLEDGE_DIR
        from knowledge_manager import KnowledgeManager

        cfg = get_config()
        # Use Knowledge config if available, otherwise derive from main config
        embedding_provider = cfg.get('Knowledge', 'embedding_provider', fallback='')
        embedding_model = cfg.get('Knowledge', 'embedding_model', fallback='')

        # If Knowledge section isn't configured, try to derive from the web search or main LLM config
        if not embedding_provider or not embedding_model:
            ws_provider = cfg.get('WebSearch', 'provider', fallback='')
            main_provider = cfg.get('General', 'provider', fallback='ollama')
            provider = ws_provider or main_provider

            if provider == 'ollama':
                embedding_provider = 'ollama'
                embedding_model = 'nomic-embed-text'
            elif provider == 'gemini':
                embedding_provider = 'gemini'
                embedding_model = 'models/embedding-001'
            else:
                print("[WEB_SEARCH] Cannot determine embedding provider for KM init", flush=True)
                return None

        km = KnowledgeManager(
            storage_dir=KNOWLEDGE_DIR,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            vector_store_type=cfg.get('Knowledge', 'vector_store', fallback='chromadb'),
            summarization_threshold=cfg.getint('Agent', 'summarization_threshold', fallback=15000),
            ollama_url=cfg.get('Ollama', 'api_url', fallback='http://localhost:11434'),
            gemini_api_key=cfg.get('General', 'gemini_api_key', fallback='')
        )
        global_state['knowledge_manager'] = km
        print(f"[WEB_SEARCH] Initialized KnowledgeManager for web search storage ({embedding_provider}/{embedding_model})", flush=True)
        return km
    except Exception as e:
        print(f"[WEB_SEARCH] Failed to initialize KnowledgeManager: {e}", flush=True)
        return None


def perform_web_search(reason, query, config, socketio=None, global_state=None):
    """
    Main entry point for web search. Called synchronously from chat flow.

    Args:
        reason: Why the agent needs this information
        query: What to search for
        config: Dict with all web search configuration
        socketio: SocketIO instance for live status updates
        global_state: App global state (for knowledge manager access)

    Returns:
        dict: {
            'success': bool,
            'summary': str (brief summary for chat context),
            'document_attached': bool,
            'document_name': str or None,
            'source_url': str or None,
            'content_size': int
        }
    """
    status = WebSearchStatus(socketio)
    status.query = query
    status.reason = reason
    status.set_status(STATUS_SEARCHING)

    # Extract config values
    llm_config = {
        'provider': config.get('provider', 'ollama'),
        'model_name': config.get('model_name', ''),
        'temperature': config.get('temperature', 0.3),
        'context_size': config.get('context_size', 8192),
        'timeout': config.get('timeout', DEFAULT_TIMEOUT),
        'ollama_url': config.get('ollama_url', 'http://localhost:11434'),
        'gemini_api_key': config.get('gemini_api_key', ''),
        'anthropic_api_key': config.get('anthropic_api_key', '')
    }

    max_results = config.get('max_results', DEFAULT_MAX_RESULTS)
    max_retries = config.get('max_retries', MAX_RETRIES)
    max_fetch_pages = config.get('max_fetch_pages', DEFAULT_MAX_FETCH_PAGES)
    max_page_size = config.get('max_page_size', DEFAULT_MAX_PAGE_SIZE)
    brief_threshold = config.get('brief_threshold', DEFAULT_BRIEF_THRESHOLD)
    region = config.get('region', 'wt-wt')
    safe_search = config.get('safe_search', 'off')
    prompt_template = config.get('prompt_template', '')
    timeout = config.get('timeout', DEFAULT_TIMEOUT)

    try:
        # --- STEP 1: Create LLM and formulate search query ---
        status.set_step(0, 'active')
        status.log(f"Reason: {reason}")
        status.log(f"Original query: {query}")

        llm = _create_llm(llm_config)

        # Build ordered fallback LLM chain from config (used when primary LLM fails)
        fallback_chain = []
        try:
            cfg_main = get_config()
            _fallback_str = config.get('fallback_models', '').strip()
            _timeout = llm_config.get('timeout', DEFAULT_TIMEOUT)
            _temp    = llm_config.get('temperature', 0.3)
            fallback_chain = _build_fallback_llm_chain(_fallback_str, cfg_main, _timeout, _temp)
            if fallback_chain:
                status.log(f"Fallback chain: {', '.join(lbl for lbl, _ in fallback_chain)}")
        except Exception as _fb_err:
            print(f"[WEB_SEARCH] Could not build fallback chain: {_fb_err}", flush=True)

        # Current datetime for prompt injection (allows LLM to filter by recency)
        from datetime import datetime
        current_datetime = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Build system preamble from the configurable WebSearchPrompt template
        # Supports {reason}, {query}, and {current_datetime} variables
        system_preamble = ""
        if prompt_template:
            try:
                system_preamble = prompt_template.format(
                    reason=reason, query=query, current_datetime=current_datetime
                )
            except (KeyError, IndexError):
                # Template doesn't use variables or has formatting issues — use as-is
                system_preamble = prompt_template
            system_preamble += "\n\n"

        # Ask LLM to optimize the search query
        optimize_prompt = f"""{system_preamble}Current date and time: {current_datetime}

Your task is to formulate the best search query for DuckDuckGo.

User's reason: {reason}
User's query: {query}

Instructions:
1. Optimize the query for DuckDuckGo search (add relevant technical terms).
2. Keep it concise (max 10 words).
3. DO NOT use search operators like site:, inurl:, intitle:, filetype: — DuckDuckGo does not support them well.
4. Return ONLY the optimized search query, nothing else. No quotes, no explanation."""

        try:
            optimized_query = _invoke_llm_with_fallback(llm, fallback_chain, optimize_prompt, timeout).strip()
            # Sanitize: remove quotes, limit length
            optimized_query = optimized_query.strip('"\'').strip()
            # Strip any site:/inurl:/intitle:/filetype: operators the LLM might still add
            optimized_query = re.sub(r'\b(site|inurl|intitle|filetype):\S+\s*', '', optimized_query).strip()
            if len(optimized_query) > 150 or len(optimized_query) < 3:
                optimized_query = query  # Fallback to original
            status.log(f"Optimized query: {optimized_query}")
        except Exception as e:
            status.log(f"Query optimization failed ({e}), using original query")
            optimized_query = query

        status.complete_step(0)

        # --- STEP 2: Search DuckDuckGo ---
        status.set_step(1, 'active')
        status.log(f"Searching DuckDuckGo (max {max_results} results, region={region})...")

        try:
            search_results = _search_ddg(optimized_query, max_results=max_results, region=region, safe_search=safe_search)
        except Exception as e:
            status.log(f"Search with optimized query failed: {e}. Retrying with original...")
            search_results = []

        # Fallback to original query if optimized returned no results
        if not search_results and optimized_query != query:
            status.log(f"No results with optimized query. Retrying with original: {query}")
            try:
                search_results = _search_ddg(query, max_results=max_results, region=region, safe_search=safe_search)
            except Exception as e:
                status.log(f"Search with original query also failed: {e}")
                search_results = []

        if not search_results:
            status.log("No search results found.")
            status.set_status(STATUS_ERROR)
            status.error_message = "No search results found."
            return {
                'success': False,
                'summary': f"Web search for '{query}' returned no results.",
                'document_attached': False,
                'document_name': None,
                'source_url': None,
                'content_size': 0
            }

        status.log(f"Found {len(search_results)} results")
        for i, r in enumerate(search_results, 1):
            status.log(f"  #{i}: {r['title'][:60]} - {r['url'][:80]}")

        status.complete_step(1)

        # --- STEP 3: LLM evaluates which results are most relevant ---
        status.set_step(2, 'active')
        status.set_status(STATUS_EVALUATING)

        results_text = ""
        for i, r in enumerate(search_results, 1):
            results_text += f"#{i}. Title: {r['title']}\n   URL: {r['url']}\n   Snippet: {r['snippet']}\n\n"

        evaluate_prompt = f"""{system_preamble}Current date and time: {current_datetime}

Evaluate these search results and rank them by relevance.

REASON the user needs this information:
{reason}

SEARCH QUERY: {query}

SEARCH RESULTS:
{results_text}

Instructions:
1. Rank the results by relevance to the REASON (most relevant first).
2. Exclude obviously irrelevant results (ads, unrelated topics).
3. Return ONLY the numbers of the top results in order, comma-separated.
   Example: 2,1,4
4. Maximum {max_fetch_pages} results."""

        try:
            ranking_response = _invoke_llm_with_fallback(llm, fallback_chain, evaluate_prompt, timeout).strip()
            # Parse ranking (extract numbers)
            ranked_indices = []
            for num in re.findall(r'\d+', ranking_response):
                idx = int(num) - 1  # Convert to 0-based
                if 0 <= idx < len(search_results) and idx not in ranked_indices:
                    ranked_indices.append(idx)
            if not ranked_indices:
                ranked_indices = list(range(min(max_fetch_pages, len(search_results))))

            ranked_indices = ranked_indices[:max_fetch_pages]
            status.log(f"LLM ranked results: {[i+1 for i in ranked_indices]}")
        except Exception as e:
            status.log(f"Ranking failed ({e}), using top {max_fetch_pages} results")
            ranked_indices = list(range(min(max_fetch_pages, len(search_results))))

        status.complete_step(2)

        # --- STEP 4: Fetch pages and evaluate content ---
        status.set_step(3, 'active')
        status.set_status(STATUS_FETCHING)

        best_content = None
        best_title = None
        best_url = None
        best_content_type = None
        attempts = 0

        for idx in ranked_indices:
            if attempts >= max_retries:
                break
            attempts += 1

            result = search_results[idx]
            url = result['url']
            title = result['title']

            status.log(f"Fetching #{idx+1}: {url[:80]}...")

            try:
                content, content_type = _fetch_page_content(url, max_size=max_page_size)
                status.log(f"Fetched {len(content)} chars ({content_type})")

                if len(content.strip()) < 100:
                    status.log(f"Content too short ({len(content)} chars), skipping")
                    continue

                # Ask LLM if content is relevant
                # Truncate content for evaluation to save context
                eval_content = content[:3000]
                relevance_prompt = f"""{system_preamble}Current date and time: {current_datetime}

Evaluate if this web page content is relevant to the user's needs.

REASON: {reason}
SEARCH QUERY: {query}

PAGE TITLE: {title}
PAGE CONTENT (first 3000 chars):
{eval_content}

Is this content relevant and useful? Respond with only YES or NO followed by a one-sentence explanation.
Example: YES - This page contains a detailed guide on VLAN configuration for Cisco switches."""

                try:
                    relevance_response = _invoke_llm_with_fallback(llm, fallback_chain, relevance_prompt, timeout).strip()
                    is_relevant = relevance_response.upper().startswith('YES')
                    status.log(f"Relevance check: {relevance_response[:100]}")

                    if is_relevant:
                        best_content = content
                        best_title = title
                        best_url = url
                        best_content_type = content_type
                        status.log(f"Selected: {title[:60]}")
                        break
                    else:
                        status.log(f"Not relevant, trying next result...")
                except Exception as e:
                    # If relevance check fails, accept the content anyway
                    status.log(f"Relevance check failed ({e}), accepting content")
                    best_content = content
                    best_title = title
                    best_url = url
                    best_content_type = content_type
                    break

            except Exception as e:
                status.log(f"Failed to fetch {url[:60]}: {e}")
                continue

        if not best_content:
            status.log("Could not fetch any relevant content from search results.")
            status.complete_step(3)

            # Return snippets as fallback
            snippets_text = "\n".join(
                f"- {r['title']}: {r['snippet']}" for r in search_results[:3]
            )
            fallback_summary = f"Web search for '{query}' found results but could not fetch page content. Search snippets:\n{snippets_text}"

            status.set_step(4, 'skipped')
            status.result_summary = fallback_summary
            status.set_status(STATUS_DONE)

            return {
                'success': True,
                'partial': True,
                'summary': fallback_summary,
                'document_attached': False,
                'document_name': None,
                'source_url': None,
                'content_size': len(snippets_text)
            }

        status.complete_step(3)

        # --- STEP 5: Process content and attach to knowledge ---
        status.set_step(4, 'active')
        status.set_status(STATUS_PROCESSING)

        # Convert to markdown
        md_content = _convert_to_markdown(best_title, best_url, best_content, best_content_type)
        content_size = len(md_content)
        status.log(f"Converted to markdown: {content_size} chars")

        # Generate summary using LLM
        summary_content = best_content[:4000] if len(best_content) > 4000 else best_content
        summary_prompt = f"""{system_preamble}Current date and time: {current_datetime}

Summarize the following web page content in relation to the user's needs.

REASON: {reason}
SEARCH QUERY: {query}
SOURCE: {best_title} ({best_url})

CONTENT:
{summary_content}

Instructions:
1. Provide a concise summary (3-5 sentences) of the key information relevant to the REASON.
2. Include specific technical details, commands, or configurations if present.
3. Start with what was found, not meta-commentary."""

        try:
            summary = _invoke_llm_with_fallback(llm, fallback_chain, summary_prompt, timeout).strip()
            status.log(f"Summary generated ({len(summary)} chars)")
        except Exception as e:
            summary = f"Found relevant content from {best_title} ({content_size} chars). URL: {best_url}"
            status.log(f"Summary generation failed ({e}), using fallback")

        # Decide: brief injection vs knowledge attachment
        document_attached = False
        document_name = None

        if content_size <= brief_threshold:
            # Brief content - inject directly into context
            status.log(f"Content is brief ({content_size} <= {brief_threshold} chars), will inject into context")
            # Summary includes the content for direct injection
            summary = f"[WEB SEARCH RESULT]\nSource: {best_title}\nURL: {best_url}\n\n{summary}\n\nFull content:\n{best_content}"
        else:
            # Large content - attach to knowledge store
            status.log(f"Content is large ({content_size} > {brief_threshold} chars), attaching to knowledge...")

            km = global_state.get('knowledge_manager') if global_state else None

            # If KM not initialized (Knowledge disabled), try to init one for web search storage
            if not km and global_state:
                km = _try_init_knowledge_manager(global_state, socketio)

            if km:
                try:
                    # Create a filename for the knowledge document
                    safe_title = re.sub(r'[^\w\s-]', '', best_title)[:50].strip()
                    safe_title = re.sub(r'\s+', '_', safe_title)
                    timestamp_str = datetime.now().strftime('%Y%m%d_%H%M%S')
                    doc_filename = f"WEB_{safe_title}_{timestamp_str}.md"

                    # Add to knowledge with WEB source metadata.
                    # Pass the web search LLM for semantic summary generation.
                    doc_meta = km.add_document(
                        filename=doc_filename,
                        content_bytes=md_content.encode('utf-8'),
                        file_type='md',
                        source_label='WEB',
                        extra_metadata={
                            'source': 'WEB',
                            'url': best_url,
                            'search_query': query,
                            'reason': reason,
                            'fetched_date': datetime.now().isoformat()
                        },
                        llm=llm
                    )
                    document_attached = True
                    document_name = doc_filename
                    status.log(f"Attached to knowledge: {doc_filename} (mode={doc_meta.get('mode', 'unknown')})")

                    # Emit knowledge update to UI
                    if socketio:
                        socketio.emit('knowledge_updated', {
                            'count': km.get_document_count(),
                            'documents': km.get_documents_list()
                        })
                        # Warn if near/at limit after adding
                        limit_st = km.get_limit_status()
                        if limit_st['at_limit'] or limit_st['near_limit']:
                            level = 'full' if limit_st['at_limit'] else 'warning'
                            socketio.emit('knowledge_limit_warning', {
                                'level': level,
                                'count': limit_st['count'],
                                'max': limit_st['max'],
                                'message': f"Knowledge store {'full' if limit_st['at_limit'] else 'almost full'}: {limit_st['count']}/{limit_st['max']} documents used."
                            })

                except ValueError as e:
                    # ValueError from add_document = limit reached or unsupported type — not a crash
                    err_msg = str(e)
                    status.log(f"Could not attach to knowledge: {err_msg}")
                    # Notify UI about limit
                    if socketio and 'limit' in err_msg.lower():
                        socketio.emit('knowledge_limit_warning', {
                            'level': 'full',
                            'message': f"Knowledge store full — web search document could not be saved. {err_msg}"
                        })
                    # Tell LLM explicitly that attachment FAILED — prevents hallucination of success
                    summary += f"\n\n[NOTE: Web search found relevant content but could NOT attach it to the knowledge store ({err_msg}). A short excerpt follows:]\n{best_content[:3000]}"
                except Exception as e:
                    status.log(f"Failed to attach to knowledge: {e}")
                    traceback.print_exc()
                    # Include full content in summary as fallback
                    summary += f"\n\n[NOTE: Could not attach to knowledge store due to an error. Content follows:]\n{best_content[:5000]}"
            else:
                status.log("Knowledge manager not available, including content in summary")
                summary += f"\n\n[Full content from {best_url}:]\n{best_content[:5000]}"

        status.complete_step(4)
        status.result_summary = summary
        status.set_status(STATUS_DONE)
        status.log("Web search completed successfully.")

        return {
            'success': True,
            'partial': False,
            'summary': summary,
            'document_attached': document_attached,
            'document_name': document_name,
            'source_url': best_url,
            'content_size': content_size
        }

    except Exception as e:
        error_msg = f"Web search error: {str(e)}"
        status.log(error_msg)
        traceback.print_exc()
        status.error_message = error_msg
        status.set_status(STATUS_ERROR)

        return {
            'success': False,
            'summary': f"Web search failed: {str(e)}",
            'document_attached': False,
            'document_name': None,
            'source_url': None,
            'content_size': 0
        }


def get_web_search_config(cfg):
    """
    Read web search configuration from config.ini and return as dict.
    WebSearch-specific ollama_url/api_key override main config if set.
    cfg: ConfigParser object from get_config()
    """
    # Read WebSearch-specific connection settings (may be empty = use main)
    ws_ollama_url = cfg.get('WebSearch', 'ollama_url', fallback='').strip()
    ws_api_key = cfg.get('WebSearch', 'api_key', fallback='').strip()

    # Fallback to main config if WebSearch fields are empty
    ollama_url = ws_ollama_url or cfg.get('Ollama', 'api_url', fallback='http://localhost:11434')
    gemini_api_key = ws_api_key or cfg.get('General', 'gemini_api_key', fallback='')
    anthropic_api_key = ws_api_key or cfg.get('General', 'anthropic_api_key', fallback='')

    return {
        'enabled': cfg.getboolean('WebSearch', 'enabled', fallback=False),
        'provider': cfg.get('WebSearch', 'provider', fallback='ollama'),
        'model_name': cfg.get('WebSearch', 'model_name', fallback=''),
        'temperature': cfg.getfloat('WebSearch', 'temperature', fallback=0.3),
        'context_size': cfg.getint('WebSearch', 'context_size', fallback=8192),
        'timeout': cfg.getint('WebSearch', 'timeout', fallback=DEFAULT_TIMEOUT),
        'max_retries': cfg.getint('WebSearch', 'max_retries', fallback=MAX_RETRIES),
        'max_results': cfg.getint('WebSearch', 'max_results', fallback=DEFAULT_MAX_RESULTS),
        'max_fetch_pages': cfg.getint('WebSearch', 'max_fetch_pages', fallback=DEFAULT_MAX_FETCH_PAGES),
        'max_page_size': cfg.getint('WebSearch', 'max_page_size', fallback=DEFAULT_MAX_PAGE_SIZE),
        'brief_threshold': cfg.getint('WebSearch', 'brief_threshold', fallback=DEFAULT_BRIEF_THRESHOLD),
        'search_engine': cfg.get('WebSearch', 'search_engine', fallback='duckduckgo'),
        'region': cfg.get('WebSearch', 'region', fallback='wt-wt'),
        'safe_search': cfg.get('WebSearch', 'safe_search', fallback='off'),
        'prompt_template': cfg.get('WebSearchPrompt', 'template', fallback=''),
        'fallback_models': cfg.get('WebSearch', 'fallback_models', fallback=''),
        # Connection settings (with fallback to main config)
        'ollama_url': ollama_url,
        'gemini_api_key': gemini_api_key,
        'anthropic_api_key': anthropic_api_key,
        # Raw WebSearch-specific values (for UI display, empty means "using main")
        'ws_ollama_url': ws_ollama_url,
        'ws_api_key': ws_api_key
    }
