"""
Telegram Bot integration for AI Agent.

Allows controlling the agent chat and approving tasks/commands via Telegram.

Features:
  - Forward chat messages to the agent and receive responses
  - Approve/deny pending command approvals (execution agent)
  - Approve/deny pending task and switch proposals (chat agent)
  - Answer ASK prompts
  - Proactive notifications when agent needs input or task completes

Configuration ([Telegram] section in config.ini):
  enabled           = false
  bot_token         = <BotFather token>
  allowed_chat_ids  = 123456789,987654321   (empty = no restriction)
  notify_task_done  = true
"""

import re
import time
import threading
import traceback

import requests
import telebot

from config import get_config

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_MSG_LEN   = 4096   # Telegram hard limit per message
POLL_INTERVAL = 1.0    # seconds between chat-response polls
CHAT_TIMEOUT  = 300    # max seconds to wait for a chat response
WATCHER_TICK  = 2.0    # seconds between pending-state checks


# ---------------------------------------------------------------------------
# Helper: markdown → safe plain-text / minimal HTML for Telegram
# ---------------------------------------------------------------------------
def _format_for_telegram(text: str) -> str:
    """Convert agent markdown response to Telegram HTML (safe subset)."""
    # Remove mermaid diagrams entirely
    text = re.sub(r'```mermaid[\s\S]*?```', '[Diagram — view in Web UI]', text)

    # Extract fenced code blocks first (protect from other transforms)
    code_blocks = []
    def _stash_code(m):
        code_blocks.append(m.group(1))
        return f'\x00CODE{len(code_blocks)-1}\x00'
    text = re.sub(r'```(?:\w+)?\n?([\s\S]*?)```', _stash_code, text)

    # Markdown tables → plain text with pipes stripped to aligned rows
    def _table_to_text(m):
        lines = m.group(0).strip().split('\n')
        rows = []
        for line in lines:
            if re.match(r'\|?\s*[-:]+\s*(\|\s*[-:]+\s*)*\|?', line):
                continue  # skip separator line
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            rows.append('  '.join(cells))
        return '\n'.join(rows)
    text = re.sub(r'(\|[^\n]+\n)+', _table_to_text, text)

    # ATX headers → bold
    text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)

    # Bold **text** and __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)

    # Italic *text* and _text_ (single, not at word boundaries to avoid false positives)
    text = re.sub(r'\*([^*\n]+)\*', r'<i>\1</i>', text)

    # Inline code (before restoring blocks)
    text = re.sub(r'`([^`\n]+)`', r'<code>\1</code>', text)

    # Restore code blocks
    for i, block in enumerate(code_blocks):
        # Escape HTML inside code blocks
        block = block.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        text = text.replace(f'\x00CODE{i}\x00', f'<pre>{block.strip()}</pre>')

    # Escape any bare & < > that are NOT inside our tags
    # Simple approach: escape outside of tag spans
    def _escape_outside_tags(s):
        result = []
        i = 0
        while i < len(s):
            if s[i] == '<':
                # find closing >
                j = s.find('>', i)
                if j == -1:
                    result.append('&lt;')
                    i += 1
                else:
                    result.append(s[i:j+1])
                    i = j + 1
            elif s[i] == '&':
                # check if already an entity
                if re.match(r'&(?:amp|lt|gt|apos|quot);', s[i:]):
                    result.append(s[i])
                    i += 1
                else:
                    result.append('&amp;')
                    i += 1
            else:
                result.append(s[i])
                i += 1
        return ''.join(result)

    text = _escape_outside_tags(text)
    return text


def _split_message(text: str) -> list:
    """Split text into chunks ≤ MAX_MSG_LEN, preferring newline boundaries."""
    if len(text) <= MAX_MSG_LEN:
        return [text]
    chunks = []
    while text:
        if len(text) <= MAX_MSG_LEN:
            chunks.append(text)
            break
        # Try to split on a newline near the limit
        split_at = text.rfind('\n', 0, MAX_MSG_LEN)
        if split_at < MAX_MSG_LEN // 2:
            split_at = MAX_MSG_LEN
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip('\n')
    return chunks


# ---------------------------------------------------------------------------
# TelegramBot class
# ---------------------------------------------------------------------------
class TelegramBot:
    def __init__(self):
        self.bot            = None
        self.global_state   = None
        self._base_url      = 'http://localhost:5000'
        self._allowed_ids   = set()   # empty = allow all
        self._active_chats  = set()   # chat IDs that used /start or sent a message
        self._running       = False
        self._polling_thread  = None
        self._watcher_thread  = None

        # Watcher dedup: remember what we already notified about
        self._notified_approval_id  = None
        self._notified_ask_id       = None
        self._notified_switch_id    = None
        self._notified_task_prop_id = None  # dedup for task proposals
        self._notified_task_id      = None  # last task_id we notified completion for
        self._prev_task_running     = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def configure(self, global_state):
        self.global_state = global_state

    def start(self) -> bool:
        """Read config, create bot, start threads. Returns True if started."""
        cfg = get_config()
        if not cfg.getboolean('Telegram', 'enabled', fallback=False):
            print("[TELEGRAM] Bot disabled in config.")
            return False

        token = cfg.get('Telegram', 'bot_token', fallback='').strip()
        if not token:
            print("[TELEGRAM] bot_token not configured — bot not started.")
            return False

        raw_ids = cfg.get('Telegram', 'allowed_chat_ids', fallback='').strip()
        self._allowed_ids = set()
        for part in raw_ids.split(','):
            part = part.strip()
            if part:
                try:
                    self._allowed_ids.add(int(part))
                except ValueError:
                    print(f"[TELEGRAM] Invalid chat_id in config: {part!r}")

        try:
            self.bot = telebot.TeleBot(token, parse_mode=None)
            self._register_handlers()

            # Discard any messages queued while the bot was offline so they
            # don't trigger spurious "agent is busy" errors on reconnect.
            # The 2s sleep lets Telegram's server release any previous long-poll
            # session (avoids 409 "two getUpdates" on quick restart).
            try:
                self.bot.skip_updates()
            except Exception as e:
                print(f"[TELEGRAM] skip_updates warning: {e}")
            time.sleep(2)

            self._running = True

            self._polling_thread = threading.Thread(
                target=self._polling_loop, daemon=True, name='tg-polling')
            self._polling_thread.start()

            self._watcher_thread = threading.Thread(
                target=self._watcher_loop, daemon=True, name='tg-watcher')
            self._watcher_thread.start()

            restriction = f"allowed IDs: {self._allowed_ids}" if self._allowed_ids else "open to all"
            print(f"[TELEGRAM] Bot started ({restriction})")
            return True

        except Exception as e:
            print(f"[TELEGRAM] Failed to start: {e}")
            traceback.print_exc()
            return False

    def stop(self):
        self._running = False
        if self.bot:
            try:
                self.bot.stop_polling()
            except Exception:
                pass

        # Wait for the polling thread to fully exit before returning.
        # Without this, stop() → start() restarts while the old long-poll
        # request is still in flight → Telegram 409 "two getUpdates instances".
        if self._polling_thread and self._polling_thread.is_alive():
            self._polling_thread.join(timeout=15)
        if self._watcher_thread and self._watcher_thread.is_alive():
            self._watcher_thread.join(timeout=3)

        self._polling_thread = None
        self._watcher_thread = None
        self.bot = None
        print("[TELEGRAM] Bot stopped.")

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------
    def _polling_loop(self):
        while self._running:
            try:
                self.bot.infinity_polling(timeout=10, long_polling_timeout=5)
            except Exception as e:
                if self._running:
                    print(f"[TELEGRAM] Polling error: {e} — restarting in 5s")
                    time.sleep(5)

    # ------------------------------------------------------------------
    # Command / message handlers
    # ------------------------------------------------------------------
    def _register_handlers(self):
        bot = self.bot

        def guard(fn):
            """Decorator: reject unauthorized chat IDs."""
            def wrapper(msg):
                if self._allowed_ids and msg.chat.id not in self._allowed_ids:
                    return
                self._active_chats.add(msg.chat.id)
                fn(msg)
            return wrapper

        @bot.message_handler(commands=['start', 'help'])
        @guard
        def cmd_start(msg):
            self._send(msg.chat.id,
                "🤖 <b>AI Agent Bot</b>\n\n"
                "Send any message to chat with the agent.\n\n"
                "<b>Commands:</b>\n"
                "/status — Agent status\n"
                "/approve — Approve pending task / command / switch\n"
                "/deny — Deny pending task / command / switch\n"
                "/answer &lt;text&gt; — Answer an ASK prompt\n"
                "/stop — Stop running task\n"
                "/help — This message",
                parse_mode='HTML')

        @bot.message_handler(commands=['status'])
        @guard
        def cmd_status(msg):
            gs = self.global_state
            if not gs:
                self._send(msg.chat.id, "Agent not initialized.")
                return

            running  = gs.get('task_running', False)
            step     = gs.get('current_step', 0)
            max_step = gs.get('max_steps', 0)
            mode     = 'API' if gs.get('api_ui_locked') else 'Web'
            lines    = [f"<b>Mode:</b> {mode}"]

            if running:
                lines.append(f"<b>Status:</b> Running (step {step}/{max_step})")
                obj = gs.get('current_objective', '')
                if obj:
                    lines.append(f"<b>Task:</b> {obj[:120]}")
            else:
                lines.append("<b>Status:</b> Idle")

            if gs.get('_pending_approval_data'):
                lines.append("⚠️ <b>Waiting:</b> command approval — use /approve or /deny")
            if gs.get('_pending_ask_data'):
                lines.append("⚠️ <b>Waiting:</b> ASK answer — use /answer &lt;text&gt;")
            if gs.get('_pending_switch_target'):
                tgt = gs['_pending_switch_target']
                name = tgt.get('target_system', tgt) if isinstance(tgt, dict) else tgt
                lines.append(f"⚠️ <b>Waiting:</b> switch to <code>{name}</code> — /approve or /deny")
            if gs.get('_api_chat_pending_type'):
                lines.append(f"⚠️ <b>Waiting:</b> {gs['_api_chat_pending_type']} — /approve or /deny")

            self._send(msg.chat.id, '\n'.join(lines), parse_mode='HTML')

        @bot.message_handler(commands=['approve'])
        @guard
        def cmd_approve(msg):
            self._send(msg.chat.id, self._handle_approval(approved=True))

        @bot.message_handler(commands=['deny'])
        @guard
        def cmd_deny(msg):
            self._send(msg.chat.id, self._handle_approval(approved=False))

        @bot.message_handler(commands=['answer'])
        @guard
        def cmd_answer(msg):
            parts = msg.text.split(None, 1)
            if len(parts) < 2:
                self._send(msg.chat.id, "Usage: /answer &lt;your response&gt;", parse_mode='HTML')
                return
            self._send(msg.chat.id, self._handle_ask_answer(parts[1]))

        @bot.message_handler(commands=['stop'])
        @guard
        def cmd_stop(msg):
            gs = self.global_state
            if not gs:
                self._send(msg.chat.id, "Agent not initialized.")
                return
            stop_ev = gs.get('stop_event')
            if stop_ev and gs.get('task_running'):
                stop_ev.set()
                self._send(msg.chat.id, "⏹ Stop signal sent.")
            else:
                self._send(msg.chat.id, "No task is currently running.")

        @bot.message_handler(func=lambda m: True, content_types=['text'])
        @guard
        def handle_text(msg):
            if msg.text and msg.text.startswith('/'):
                self._send(msg.chat.id, "Unknown command. Use /help.")
                return
            self._forward_to_chat(msg.chat.id, msg.text or '')

    # ------------------------------------------------------------------
    # Approval / answer helpers
    # ------------------------------------------------------------------
    def _handle_approval(self, approved: bool) -> str:
        """Handle /approve or /deny — checks what is pending and responds."""
        gs = self.global_state
        if not gs:
            return "Agent not initialized."

        action = "approved ✅" if approved else "denied ❌"

        # 1. Execution-agent command approval (highest priority — blocks SSH execution)
        if gs.get('_pending_approval_data') and gs.get('user_approval_event'):
            gs['user_response']['approved'] = approved
            gs['user_approval_event'].set()
            return f"Command {action}."

        # 2. Execution-agent ASK-with-approve (some modals combine ASK + approve)
        #    — nothing extra needed here; handled by _handle_ask_answer

        # 3. Chat-agent switch proposal (web mode)
        if gs.get('_pending_switch_target') and not gs.get('api_ui_locked'):
            tgt = gs['_pending_switch_target']
            target_name = tgt.get('target_system', tgt) if isinstance(tgt, dict) else tgt
            if approved:
                fn = gs.get('_fn_approve_switch')
                if fn:
                    fn(target_name)
                    return f"Switch to <code>{target_name}</code> {action}."
            else:
                event = gs.get('system_switch_event')
                resp  = gs.get('system_switch_response', {})
                resp.update({'approved': False, 'target_system': target_name,
                             'error': 'Denied via Telegram'})
                if event:
                    event.set()
                return f"Switch to <code>{target_name}</code> {action}."

        # 4. Chat-agent task proposal (web mode)
        if gs.get('_pending_task_proposal') and not gs.get('api_ui_locked'):
            objective = gs['_pending_task_proposal']
            if approved:
                fn = gs.get('_fn_start_task')
                if fn:
                    fn(objective)
                    return f"Task {action} and started."
            else:
                gs.pop('_pending_task_proposal', None)
                return f"Task {action}."

        # 5. API-mode pending (task_proposal / switch_proposal via chat API)
        pending_type = gs.get('_api_chat_pending_type')
        req_id       = gs.get('api_chat_request_id', '')
        if pending_type and req_id:
            decision = 'approve' if approved else 'deny'
            try:
                r = requests.post(
                    f'{self._base_url}/api/chat/approve/{req_id}',
                    json={'decision': decision},
                    timeout=10)
                if r.status_code == 200:
                    return f"{pending_type} {action}."
                return f"API approve failed: {r.status_code}"
            except Exception as e:
                return f"Error calling approve API: {e}"

        return "Nothing pending to approve/deny right now."

    def _handle_ask_answer(self, answer_text: str) -> str:
        """Handle /answer <text> — provide response to a pending ASK prompt."""
        gs = self.global_state
        if not gs:
            return "Agent not initialized."

        if gs.get('_pending_ask_data') and gs.get('user_answer_event'):
            ua = gs.get('user_answer', {})
            if isinstance(ua, dict):
                ua.clear()
                ua['answer'] = answer_text
                ua['timeout'] = None
            gs['user_answer_event'].set()
            return f"Answer sent ✅"

        return "No pending ASK prompt found."

    # ------------------------------------------------------------------
    # Chat forwarding
    # ------------------------------------------------------------------
    def _forward_to_chat(self, chat_id: int, text: str):
        """Send user message to agent chat, wait for response, reply."""
        try:
            self.bot.send_chat_action(chat_id, 'typing')
        except Exception:
            pass

        try:
            r = requests.post(
                f'{self._base_url}/api/chat/message',
                json={'message': text},
                timeout=15)
        except Exception as e:
            self._send(chat_id, f"❌ Could not reach agent: {e}")
            return

        if r.status_code == 409:
            data = r.json()
            active_id = data.get('active_request_id', '')
            if active_id.startswith('auto_'):
                # Auto-analyze running after task completion — wait silently and retry
                for _ in range(15):  # up to ~30 s
                    time.sleep(2)
                    try:
                        r = requests.post(
                            f'{self._base_url}/api/chat/message',
                            json={'message': text},
                            timeout=15)
                    except Exception as e:
                        self._send(chat_id, f"❌ Could not reach agent: {e}")
                        return
                    if r.status_code != 409:
                        break  # proceed to normal handling below
                else:
                    self._send(chat_id, "⏱ Agent still busy after waiting. Please try again shortly.")
                    return
                # Fall through to normal status handling after the retry loop
            else:
                self._send(chat_id,
                    f"⏳ Agent is busy (another session in progress). "
                    f"Try again in a moment.")
                return

        if r.status_code == 423:
            self._send(chat_id,
                "🔒 Agent is in Web UI mode. Switch to API mode first.")
            return

        if r.status_code != 200:
            self._send(chat_id, f"❌ Chat error {r.status_code}: {r.text[:200]}")
            return

        request_id = r.json().get('request_id')
        if not request_id:
            self._send(chat_id, "❌ No request_id returned.")
            return

        # Poll for completion
        elapsed = 0
        typing_tick = 0
        approval_notified = False   # send awaiting_approval message only once
        while elapsed < CHAT_TIMEOUT:
            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
            typing_tick += POLL_INTERVAL

            # Refresh typing indicator every 5 s
            if typing_tick >= 5:
                typing_tick = 0
                try:
                    self.bot.send_chat_action(chat_id, 'typing')
                except Exception:
                    pass

            try:
                sr = requests.get(
                    f'{self._base_url}/api/chat/status/{request_id}',
                    timeout=5)
                if sr.status_code != 200:
                    continue
                entry = sr.json()
            except Exception:
                continue

            status = entry.get('status')

            if status == 'completed':
                response = entry.get('response', '')
                self._send_response(chat_id, response)
                return

            if status == 'error':
                self._send(chat_id, f"❌ Chat error: {entry.get('error', 'unknown')}")
                return

            if status == 'interrupted':
                self._send(chat_id, "⚠️ Chat session was interrupted (WebUI took control).")
                return

            if status == 'awaiting_approval' and not approval_notified:
                approval_notified = True
                pt  = entry.get('pending_type', 'proposal')
                pd  = entry.get('pending_data', {})
                obj = pd.get('objective') or pd.get('target_system') or ''
                desc = f"\n<i>{obj[:300]}</i>" if obj else ''
                label = 'Task proposal' if 'task' in pt else 'Switch proposal' if 'switch' in pt else pt
                self._send(chat_id,
                    f"⏳ <b>{label}</b>{desc}\n\nUse /approve or /deny",
                    parse_mode='HTML')
                # Keep polling — user may /approve from same chat

        self._send(chat_id, "⏱ Response timed out.")

    def _send_response(self, chat_id: int, text: str):
        """Send agent response formatted for Telegram, filtering internal control content."""
        if not text:
            self._send(chat_id, "(empty response)")
            return

        # Skip responses that are purely internal system messages (hidden in web chat too)
        stripped = text.strip()
        if (stripped.startswith('[SYSTEM EVENT]') or
                stripped.startswith('[ACTION COMPLETED]') or
                stripped.startswith('Search completed. The results have been added') or
                stripped.startswith('Knowledge search completed.')):
            return

        # Strip internal control tags from the visible text (keep surrounding prose)
        cleaned = re.sub(r'<<SWITCH_SYSTEM:[^>]*>>', '', text)
        cleaned = re.sub(r'<<REQUEST_TASK:[^>]*(>>|>|$)', '', cleaned, flags=re.DOTALL)
        cleaned = re.sub(r'<<MARK_STEP_COMPLETED:[^>]*>>', '', cleaned)
        cleaned = re.sub(r'<<ACTION_PLAN_START>>[\s\S]*?<<ACTION_PLAN_STOP>>', '', cleaned)
        cleaned = cleaned.strip()

        if not cleaned:
            return

        formatted = _format_for_telegram(cleaned)
        self._send(chat_id, formatted, parse_mode='HTML')

    # ------------------------------------------------------------------
    # Watcher: proactive notifications
    # ------------------------------------------------------------------
    def _watcher_loop(self):
        while self._running:
            try:
                if self._active_chats and self.global_state:
                    self._check_pending()
                    self._check_task_completion()
            except Exception as e:
                print(f"[TELEGRAM] Watcher error: {e}")
            time.sleep(WATCHER_TICK)

    def _check_pending(self):
        gs = self.global_state

        # Command approval
        approval = gs.get('_pending_approval_data')
        aid = id(approval) if approval else None
        if approval and aid != self._notified_approval_id:
            self._notified_approval_id = aid
            cmd = approval.get('command', '?')
            reason = approval.get('reason', '')
            msg = (
                f"⚠️ <b>Command approval required:</b>\n"
                f"<pre>{cmd[:600]}</pre>"
                + (f"\n<i>Reason: {reason}</i>" if reason else "")
                + "\n\nReply /approve or /deny"
            )
            self._broadcast(msg, parse_mode='HTML')
        elif not approval:
            self._notified_approval_id = None

        # ASK prompt
        ask = gs.get('_pending_ask_data')
        qid = id(ask) if ask else None
        if ask and qid != self._notified_ask_id:
            self._notified_ask_id = qid
            question = ask.get('question', ask.get('message', '?'))
            self._broadcast(
                f"❓ <b>Agent asks:</b>\n{question}\n\nReply: /answer &lt;text&gt;",
                parse_mode='HTML')
        elif not ask:
            self._notified_ask_id = None

        # Switch proposal
        switch = gs.get('_pending_switch_target')
        sid_val = id(switch) if switch else None
        if switch and sid_val != self._notified_switch_id:
            self._notified_switch_id = sid_val
            name = (switch.get('target_system', '?') if isinstance(switch, dict) else switch)
            reason = switch.get('reason', '') if isinstance(switch, dict) else ''
            msg = (
                f"🔄 <b>System switch requested:</b> <code>{name}</code>"
                + (f"\n<i>{reason}</i>" if reason else "")
                + "\n\nReply /approve or /deny"
            )
            self._broadcast(msg, parse_mode='HTML')
        elif not switch:
            self._notified_switch_id = None

        # Task proposal (web mode — chat agent proposed <<REQUEST_TASK:...>>)
        task_prop = gs.get('_pending_task_proposal')
        tpid = id(task_prop) if task_prop else None
        if task_prop and tpid != self._notified_task_prop_id:
            self._notified_task_prop_id = tpid
            self._broadcast(
                f"📋 <b>Task proposal:</b>\n<i>{str(task_prop)[:400]}</i>\n\nReply /approve or /deny",
                parse_mode='HTML')
        elif not task_prop:
            self._notified_task_prop_id = None

        # API-mode chat proposal — handled inline in _forward_to_chat; nothing extra needed here

    def _check_task_completion(self):
        gs = self.global_state
        cfg = get_config()
        if not cfg.getboolean('Telegram', 'notify_task_done', fallback=True):
            return

        task_running = gs.get('task_running', False)
        if self._prev_task_running and not task_running:
            task_id = gs.get('current_api_task_id', '')
            if task_id and task_id != self._notified_task_id:
                self._notified_task_id = task_id
                # Don't send raw final_report — wait for auto-analyze LLM summary instead
                threading.Thread(
                    target=self._wait_and_notify_completion,
                    args=(gs.get('current_objective', ''),),
                    daemon=True,
                    name='tg-completion-waiter'
                ).start()

        self._prev_task_running = task_running

    def _wait_and_notify_completion(self, objective: str):
        """
        Wait for the post-task auto-analyze chat response (the LLM summary shown
        in web chat) and forward it to Telegram instead of the raw final_report.
        """
        gs = self.global_state

        # Give the server up to 5 s to start an auto_ chat request
        req_id = None
        for _ in range(10):
            time.sleep(0.5)
            candidate = gs.get('api_chat_request_id', '')
            if candidate.startswith('auto_'):
                req_id = candidate
                break

        if not req_id:
            # No auto-analyze started (web-UI task without API mode) — send a plain notice
            obj_text = f": {objective[:200]}" if objective else ""
            self._broadcast(f"✅ <b>Task finished{obj_text}</b>", parse_mode='HTML')
            return

        # Poll for the auto-analyze result (same LLM response shown in web chat)
        elapsed = 0
        while elapsed < 120:
            time.sleep(2)
            elapsed += 2
            try:
                r = requests.get(
                    f'{self._base_url}/api/chat/status/{req_id}',
                    timeout=5)
                if r.status_code != 200:
                    continue
                entry = r.json()
                status = entry.get('status')
                if status == 'completed':
                    response = entry.get('response', '')
                    if response:
                        formatted = _format_for_telegram(response)
                        self._broadcast(
                            f"✅ <b>Task completed</b>\n\n{formatted}",
                            parse_mode='HTML')
                    else:
                        obj_text = f": {objective[:200]}" if objective else ""
                        self._broadcast(
                            f"✅ <b>Task finished{obj_text}</b>",
                            parse_mode='HTML')
                    return
                if status in ('error', 'interrupted'):
                    obj_text = f": {objective[:200]}" if objective else ""
                    self._broadcast(
                        f"✅ <b>Task finished{obj_text}</b>",
                        parse_mode='HTML')
                    return
            except Exception:
                continue

        # Timeout — send plain notice
        obj_text = f": {objective[:200]}" if objective else ""
        self._broadcast(f"✅ <b>Task finished{obj_text}</b>", parse_mode='HTML')

    # ------------------------------------------------------------------
    # Send helpers
    # ------------------------------------------------------------------
    def _send(self, chat_id: int, text: str, parse_mode=None):
        if not text or not self.bot:
            return
        for chunk in _split_message(text):
            try:
                self.bot.send_message(chat_id, chunk, parse_mode=parse_mode)
            except Exception as e:
                print(f"[TELEGRAM] Send error to {chat_id}: {e}")

    def _broadcast(self, text: str, parse_mode=None):
        for chat_id in list(self._active_chats):
            self._send(chat_id, text, parse_mode=parse_mode)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_instance: TelegramBot = None


def get_telegram_bot() -> TelegramBot:
    global _instance
    if _instance is None:
        _instance = TelegramBot()
    return _instance


def start_telegram_bot(global_state) -> bool:
    """Initialize and start the bot. Call once after app startup."""
    bot = get_telegram_bot()
    bot.configure(global_state)
    return bot.start()
