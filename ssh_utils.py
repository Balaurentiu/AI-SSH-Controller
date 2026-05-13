import os
import re
import time
import socket
import paramiko
import ipaddress
import subprocess
import traceback
from config import get_config, KEYS_DIR

# --- Legacy SSH Algorithm Support ---
# These algorithms are disabled by default in modern SSH but required for older devices
# like Brocade switches, older Cisco equipment, legacy servers, etc.

LEGACY_CIPHERS = [
    'aes128-cbc', 'aes192-cbc', 'aes256-cbc',
    '3des-cbc', 'blowfish-cbc', 'cast128-cbc',
]

LEGACY_KEY_EXCHANGE = [
    'diffie-hellman-group14-sha1',
    'diffie-hellman-group1-sha1',
    'diffie-hellman-group-exchange-sha1',
]

LEGACY_HOST_KEYS = [
    'ssh-rsa',
    'ssh-dss',
]

def load_private_key(key_path, password=None):
    """
    Load a private key file, auto-detecting the key type (RSA, DSA, ECDSA, Ed25519).

    Args:
        key_path: Path to the private key file
        password: Optional passphrase for encrypted keys

    Returns:
        paramiko key object

    Raises:
        paramiko.SSHException: If key cannot be loaded
    """
    # Build list of available key classes (some may not exist in all paramiko versions)
    key_classes = []

    # RSA - always available
    key_classes.append(paramiko.RSAKey)

    # DSA - may be named DSSKey or not available
    if hasattr(paramiko, 'DSSKey'):
        key_classes.append(paramiko.DSSKey)

    # ECDSA - may not be available in older versions
    if hasattr(paramiko, 'ECDSAKey'):
        key_classes.append(paramiko.ECDSAKey)

    # Ed25519 - may not be available in older versions
    if hasattr(paramiko, 'Ed25519Key'):
        key_classes.append(paramiko.Ed25519Key)

    last_error = None
    for key_class in key_classes:
        try:
            return key_class.from_private_key_file(key_path, password=password)
        except paramiko.SSHException as e:
            last_error = e
            continue
        except Exception as e:
            last_error = e
            continue

    raise paramiko.SSHException(f"Cannot load private key from {key_path}: {last_error}")

def get_legacy_transport_options():
    """
    Returns paramiko Transport security options configured for legacy device support.
    Call this after connect() to enable legacy algorithms.
    """
    return {
        'ciphers': LEGACY_CIPHERS,
        'kex': LEGACY_KEY_EXCHANGE,
        'keys': LEGACY_HOST_KEYS,
    }

def create_ssh_client_with_legacy_support():
    """
    Creates a paramiko SSHClient configured to support legacy SSH algorithms.
    This is needed for older network devices like Brocade 300 switches.
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    return client

def connect_with_legacy_support(client, hostname, username, password=None, pkey=None, port=22, timeout=30):
    """
    Connect to SSH host with legacy algorithm support for older devices.

    This function first tries a standard connection, and if it fails due to
    algorithm negotiation issues, it retries with legacy algorithms enabled.

    Args:
        client: paramiko.SSHClient instance
        hostname: Target host IP or hostname
        username: SSH username
        password: Optional password for authentication
        pkey: Optional private key for authentication
        port: SSH port (default 22)
        timeout: Connection timeout in seconds

    Returns:
        True on successful connection

    Raises:
        paramiko.AuthenticationException: If authentication fails
        paramiko.SSHException: If SSH protocol error occurs
        Exception: For other connection errors
    """
    # First, try standard connection
    try:
        if password:
            client.connect(hostname=hostname, port=port, username=username,
                          password=password, timeout=timeout,
                          allow_agent=False, look_for_keys=False)
        else:
            client.connect(hostname=hostname, port=port, username=username,
                          pkey=pkey, timeout=timeout,
                          allow_agent=False, look_for_keys=False)
        print(f"[SSH_UTILS] Connected to {hostname} using standard algorithms", flush=True)
        return True
    except paramiko.SSHException as e:
        error_msg = str(e).lower()
        # Check if this is an algorithm negotiation failure
        if 'no acceptable' in error_msg or 'unable to agree' in error_msg or 'incompatible' in error_msg:
            print(f"[SSH_UTILS] Standard connection failed, trying legacy algorithms: {e}", flush=True)
        else:
            # Re-raise if it's a different SSH error
            raise

    # Retry with legacy algorithm support using Transport directly
    sock = None
    transport = None
    try:
        import socket
        sock = socket.create_connection((hostname, port), timeout=timeout)
        transport = paramiko.Transport(sock)

        # Enable legacy algorithms on the transport
        sec_opts = transport.get_security_options()

        # Add legacy ciphers (prepend to maintain priority)
        current_ciphers = list(sec_opts.ciphers)
        for cipher in LEGACY_CIPHERS:
            if cipher not in current_ciphers:
                current_ciphers.append(cipher)
        sec_opts.ciphers = tuple(current_ciphers)

        # Add legacy key exchange algorithms
        current_kex = list(sec_opts.kex)
        for kex in LEGACY_KEY_EXCHANGE:
            if kex not in current_kex:
                current_kex.append(kex)
        sec_opts.kex = tuple(current_kex)

        # Add legacy host key types
        current_keys = list(sec_opts.key_types)
        for key_type in LEGACY_HOST_KEYS:
            if key_type not in current_keys:
                current_keys.append(key_type)
        sec_opts.key_types = tuple(current_keys)

        print(f"[SSH_UTILS] Legacy algorithms enabled - Ciphers: {sec_opts.ciphers[:3]}..., KEX: {sec_opts.kex[:3]}...", flush=True)

        # Start the connection
        transport.connect(username=username, password=password, pkey=pkey)

        # Attach the transport to the client
        # We need to replace the client's internal transport
        client._transport = transport

        print(f"[SSH_UTILS] Connected to {hostname} using LEGACY algorithms", flush=True)
        return True

    except Exception as e:
        if transport:
            transport.close()
        if sock:
            sock.close()
        raise

# --- Constante pentru căile cheilor ---
PRIVATE_KEY_PATH = os.path.join(KEYS_DIR, 'id_rsa')
PUBLIC_KEY_PATH = os.path.join(KEYS_DIR, 'id_rsa.pub')

# --- Persistent SSH Connection State ---
# Reuse the same TCP+SSH session across consecutive commands in a task.
# This eliminates ~2-3s per-command TCP handshake + key exchange overhead.

_PERSISTENT_CLIENT = None       # Reused SSHClient across commands in a task
_PERSISTENT_CLIENT_KEY = None   # (ip, user, port) — detects target system change
_ACTIVE_CHANNEL = None          # Current exec_command / shell channel (for per-command abort)

# Legacy alias kept for any callers that reference ACTIVE_SSH_CLIENT directly
ACTIVE_SSH_CLIENT = None

# Global variable to store detected OS type
DETECTED_OS = None  # Will be set to 'windows', 'linux', 'macos', or None

# Exponential backoff schedule for SSH reconnect retries (seconds)
SSH_RECONNECT_DELAYS = [1, 2, 4, 8, 16, 30]


def _is_connection_error(exc):
    """Return True if exc is a transient network/transport failure worth retrying.

    Returns False for permanent failures (auth, intentional abort) so those
    bubble up immediately without burning through the retry budget.
    """
    if isinstance(exc, paramiko.AuthenticationException):
        return False  # wrong credentials — retrying won't help
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return False  # likely an intentional abort via abort_active_channel()
    try:
        if isinstance(exc, paramiko.buffered_pipe.PipeTimeout):
            return False
    except AttributeError:
        pass
    return isinstance(exc, (EOFError, OSError, socket.error, paramiko.SSHException))


def _get_connection_key(cfg):
    """Return (ip, user, port) tuple identifying the current SSH target."""
    ip   = cfg.get('System', 'ip_address', fallback='').strip()
    user = cfg.get('System', 'username',   fallback='').strip()
    port = cfg.getint('System', 'ssh_port', fallback=22)
    return (ip, user, port)


def _is_client_alive(client):
    """Return True if the client's SSH transport is still connected."""
    try:
        transport = client.get_transport()
        return transport is not None and transport.is_active()
    except Exception:
        return False


def _get_persistent_client(cfg):
    """
    Return a live SSHClient for the current system config.
    Reuses the existing connection when available and active.
    Creates + connects a new client otherwise.
    Keepalive (30 s) is enabled on every new connection.
    """
    global _PERSISTENT_CLIENT, _PERSISTENT_CLIENT_KEY

    current_key = _get_connection_key(cfg)

    # Reuse existing connection if same target and transport alive
    if (_PERSISTENT_CLIENT is not None
            and _PERSISTENT_CLIENT_KEY == current_key
            and _is_client_alive(_PERSISTENT_CLIENT)):
        print(f"[SSH_UTILS] Reusing persistent connection to {current_key[0]}", flush=True)
        return _PERSISTENT_CLIENT

    # Discard stale client if any
    if _PERSISTENT_CLIENT is not None:
        try:
            _PERSISTENT_CLIENT.close()
        except Exception:
            pass
        _PERSISTENT_CLIENT     = None
        _PERSISTENT_CLIENT_KEY = None

    # Build new client
    ip           = current_key[0]
    user         = current_key[1]
    port         = current_key[2]
    key_path     = cfg.get('System', 'ssh_key_path',  fallback='').strip()
    auth_method  = cfg.get('System', 'auth_method',   fallback='key').strip()
    auth_password = cfg.get('System', 'auth_password', fallback='').strip()

    client = create_ssh_client_with_legacy_support()

    if auth_method == 'password':
        connect_with_legacy_support(client, hostname=ip, username=user,
                                    password=auth_password, port=port, timeout=30)
    elif auth_method == 'key':
        pkey = load_private_key(key_path)
        connect_with_legacy_support(client, hostname=ip, username=user,
                                    pkey=pkey, port=port, timeout=30)
    else:  # key_or_password
        connected = False
        if key_path and os.path.exists(key_path):
            try:
                pkey = load_private_key(key_path)
                connect_with_legacy_support(client, hostname=ip, username=user,
                                            pkey=pkey, port=port, timeout=30)
                connected = True
            except (paramiko.AuthenticationException, paramiko.SSHException) as key_err:
                print(f"[SSH_UTILS] Key auth failed, trying password: {key_err}", flush=True)
                client.close()
                client = create_ssh_client_with_legacy_support()
        if not connected:
            if auth_password:
                connect_with_legacy_support(client, hostname=ip, username=user,
                                            password=auth_password, port=port, timeout=30)
            else:
                raise Exception("Key auth failed and no password configured for fallback.")

    # Enable keepalive so idle connections survive server-side timeouts
    try:
        transport = client.get_transport()
        if transport:
            transport.set_keepalive(30)
    except Exception:
        pass

    _PERSISTENT_CLIENT     = client
    _PERSISTENT_CLIENT_KEY = current_key
    print(f"[SSH_UTILS] New persistent connection established to {ip}", flush=True)
    return client


def close_persistent_client():
    """
    Close and discard the persistent SSH client.
    Call this on: task stop, system switch, system config change.
    """
    global _PERSISTENT_CLIENT, _PERSISTENT_CLIENT_KEY, _ACTIVE_CHANNEL, ACTIVE_SSH_CLIENT
    _ACTIVE_CHANNEL = None
    if _PERSISTENT_CLIENT is not None:
        try:
            _PERSISTENT_CLIENT.close()
            print("[SSH_UTILS] Persistent connection closed.", flush=True)
        except Exception:
            pass
        _PERSISTENT_CLIENT     = None
        _PERSISTENT_CLIENT_KEY = None
    ACTIVE_SSH_CLIENT = None


def get_ssh_health():
    """
    Lightweight SSH reachability check for periodic background health monitoring.

    Priority:
    1. If the persistent client is already alive → SSH is up (zero overhead, no new connection).
    2. Otherwise → TCP connect to (ip, port) with a short timeout.  This confirms the remote
       host is listening on the SSH port without performing a full handshake or auth exchange.

    Returns:
        (bool, str) — (reachable, human-readable message)
    """
    cfg = get_config()
    ip   = cfg.get('System', 'ip_address', fallback='').strip()
    user = cfg.get('System', 'username',   fallback='').strip()
    port = cfg.getint('System', 'ssh_port', fallback=22)

    if not ip or not user:
        return False, "System not configured."

    # Fastest path: reuse the liveness of the existing persistent connection
    if _PERSISTENT_CLIENT is not None and _is_client_alive(_PERSISTENT_CLIENT):
        return True, f"Connected to {user}@{ip}."

    # TCP-level port check — lighter than a full SSH handshake
    try:
        with socket.create_connection((ip, port), timeout=5):
            pass
        return True, f"SSH port reachable at {user}@{ip}."
    except socket.timeout:
        return False, f"SSH port timeout on {ip}:{port}."
    except ConnectionRefusedError:
        return False, f"SSH port {port} refused on {ip}."
    except OSError as e:
        return False, f"Cannot reach {ip}:{port} — {type(e).__name__}: {e}"


def abort_active_channel():
    """
    Close only the current command channel without touching the persistent connection.
    Used by 'Abort Cmd' — pauses execution but keeps TCP session alive for resume.
    """
    global _ACTIVE_CHANNEL
    if _ACTIVE_CHANNEL is not None:
        try:
            print("[SSH_UTILS] Aborting active channel...", flush=True)
            _ACTIVE_CHANNEL.close()
        except Exception as e:
            print(f"[SSH_UTILS] Error closing active channel: {e}", flush=True)
        _ACTIVE_CHANNEL = None

# --- Functie pentru curatarea ANSI escape sequences ---
def strip_ansi_sequences(text):
    """
    Sterge ANSI escape sequences si artefactele specifice Windows OpenSSH (Window Title).
    Rezolva problema aparitiei '0;C:\\WINDOWS\\system32\\conhost.exe'.
    """
    # 1. Regex principal pentru secvente ANSI standard (CSI)
    # Acopera culori, miscari de cursor etc.
    ansi_csi_pattern = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

    # 2. Regex specific pentru Windows OSC (Operating System Command) - Window Title
    # Windows trimite adesea: ESC ] 0 ; Titlu BEL  sau ESC ] 0 ; Titlu ESC \
    # Acest pattern cauta secventa de start \x1B]0; si merge pana la BEL (\x07) sau ST (\x1B\\)
    ansi_osc_pattern = re.compile(r'\x1B\]0;.*?(?:\x07|\x1B\\)', re.DOTALL)

    # Aplicam curatarea in pasi
    text = ansi_osc_pattern.sub('', text) # Scoatem titlurile intai (sunt lungi)
    text = ansi_csi_pattern.sub('', text) # Scoatem culorile

    # 3. Curatare Fallback pentru artefacte 'orphaned'
    # Uneori, daca buffer-ul taie secventa la mijloc, raman resturi.
    # Filtram liniile care arata exact a artefact de conhost
    lines = text.splitlines()
    cleaned_lines = []
    for line in lines:
        clean_line = line.strip()
        # Daca linia incepe cu '0;' si contine o cale de sistem sau conhost, e gunoi
        if clean_line.startswith("0;") and ("conhost.exe" in clean_line or ":\\" in clean_line):
            continue
        # Daca linia e goala, o pastram doar daca nu suntem in mijlocul unui bloc de gunoi
        cleaned_lines.append(line)

    result = '\n'.join(cleaned_lines)
    return result

# --- CORECTIE: Functii "getter" care lipseau ---
def get_private_key_path():
    """Returneaza calea standard catre cheia privata."""
    return PRIVATE_KEY_PATH

def get_public_key_path():
    """Returneaza calea standard catre cheia publica."""
    return PUBLIC_KEY_PATH

def set_detected_os(os_type):
    """
    Sets the detected OS type for proper PTY handling.

    Args:
        os_type (str): OS type string from detection. Can be:
            - "Linux"
            - "Windows (or non-Unix)"
            - "Windows"
            - "macOS"
            - "Darwin"
            - Or any other format

    This function normalizes the OS type to: 'windows', 'linux', 'macos', or None
    """
    global DETECTED_OS

    if not os_type:
        DETECTED_OS = None
        return

    os_lower = os_type.lower()

    if 'windows' in os_lower:
        DETECTED_OS = 'windows'
    elif 'linux' in os_lower:
        DETECTED_OS = 'linux'
    elif 'darwin' in os_lower or 'macos' in os_lower:
        DETECTED_OS = 'macos'
    else:
        DETECTED_OS = None

    print(f"[SSH_UTILS] OS type set to: {DETECTED_OS} (from: {os_type})", flush=True)

def get_detected_os():
    """Returns the currently detected OS type."""
    return DETECTED_OS

# --- Functii de generare si deploy chei ---

def generate_new_ssh_key():
    """
    Genereaza o noua pereche de chei SSH RSA 4096 si o salveaza in KEYS_DIR.
    Returneaza (True, "mesaj") la succes, (False, "mesaj_eroare") la esec.
    """
    try:
        # Asiguram ca directorul exista
        os.makedirs(KEYS_DIR, exist_ok=True)
        
        # Stergem cheile vechi daca exista
        if os.path.exists(PRIVATE_KEY_PATH):
            os.remove(PRIVATE_KEY_PATH)
        if os.path.exists(PUBLIC_KEY_PATH):
            os.remove(PUBLIC_KEY_PATH)
            
        print("Generating new 4096-bit RSA key pair...")
        key = paramiko.RSAKey.generate(4096)
        
        # Salvam cheia privata cu permisiuni stricte
        key.write_private_key_file(PRIVATE_KEY_PATH)
        os.chmod(PRIVATE_KEY_PATH, 0o600)
        
        # Salvam cheia publica
        pub_key_string = f"ssh-rsa {key.get_base64()} generated-by-ai-agent"
        with open(PUBLIC_KEY_PATH, "w") as f:
            f.write(pub_key_string)
            
        print(f"Successfully generated new SSH key pair at {KEYS_DIR}")
        return True, "Key generated successfully."
    except Exception as e:
        print(f"Failed to generate SSH key: {e}")
        traceback.print_exc()
        return False, f"Failed to generate SSH key: {e}"

# --- CORECTIE: Functia care lipsea a fost adaugata la loc ---
def initialize_ssh_key_if_needed():
    """
    Verifica daca cheia privata exista. Daca nu, genereaza una noua.
    """
    if not os.path.exists(PRIVATE_KEY_PATH) or not os.path.exists(PUBLIC_KEY_PATH):
        print("SSH key pair not found. Generating new key pair...")
        generate_new_ssh_key()
    else:
        print("Existing SSH key pair found.")

# --- CORECTIE: Semnatura functiei a fost actualizata ---
def get_public_key_content(force_generate=False) -> str:
    """
    Citeste si returneaza continutul cheii publice.
    Daca 'force_generate' este True, va genera cheile daca nu exista.
    """
    if force_generate:
        initialize_ssh_key_if_needed() # Asiguram ca cheia exista
        
    try:
        with open(PUBLIC_KEY_PATH, 'r') as f:
            return f.read().strip()
    except FileNotFoundError:
        print("Public key file not found.")
        return ""
    except Exception as e:
        print(f"Error reading public key: {e}")
        return ""

def deploy_ssh_key(ip, user, pwd, pub_key_content, socketio_instance):
    """
    Incearca sa copieze cheia publica pe sistemul remote folosind parola.
    Supports legacy SSH algorithms for older devices (Brocade, Cisco, etc.)
    """
    client = None
    try:
        client = create_ssh_client_with_legacy_support()
        socketio_instance.emit('deploy_log', {'data': f"Connecting to {ip} (with legacy algorithm support)...\n"})
        connect_with_legacy_support(client, hostname=ip, username=user, password=pwd, timeout=10)
        
        # Detectam OS-ul (simplificat)
        is_windows = False
        try:
            _, stdout, _ = client.exec_command('uname', timeout=5)
            if not (stdout.channel.recv_exit_status() == 0 and stdout.read()):
                is_windows = True
        except Exception:
            is_windows = True
            
        socketio_instance.emit('deploy_log', {'data': f"Target OS: {'Windows' if is_windows else 'UNIX-like'}.\n"})
        
        if is_windows:
            # Comanda PowerShell pentru Windows
            socketio_instance.emit('deploy_log', {'data': "Attempting Windows deployment (User & Admin paths)...\n"})

            # Calea utilizatorului
            cmd_user = f"powershell -Command \"$sshDir=Join-Path $env:USERPROFILE '.ssh'; if (-not (Test-Path $sshDir)){{New-Item -Path $sshDir -ItemType Directory}}; $authKeysFile=Join-Path $sshDir 'authorized_keys'; if (-not (Test-Path $authKeysFile) -or -not ((Get-Content $authKeysFile -EA SilentlyContinue) | Where-Object{{$_ -eq '{pub_key_content}'}})){{Add-Content -Path $authKeysFile -Value '{pub_key_content}'}}\""
            _, so_u, se_u = client.exec_command(cmd_user)
            es_u = so_u.channel.recv_exit_status()
            err_u = se_u.read().decode()
            socketio_instance.emit('deploy_log', {'data': f" User path exit: {es_u}. Errors: {'None' if not err_u else err_u}\n"})

            # Calea administratorului
            cmd_admin = f"powershell -Command \"$adminAuthFile=Join-Path $env:ProgramData 'ssh\\administrators_authorized_keys'; if (-not (Test-Path $adminAuthFile) -or -not ((Get-Content $adminAuthFile -EA SilentlyContinue) | Where-Object{{$_ -eq '{pub_key_content}'}})){{Add-Content -Path $adminAuthFile -Value '{pub_key_content}'}}\""
            _, so_a, se_a = client.exec_command(cmd_admin)
            es_a = so_a.channel.recv_exit_status()
            err_a = se_a.read().decode()
            socketio_instance.emit('deploy_log', {'data': f" Admin path exit: {es_a}. Errors: {'None' if not err_a else err_a}\n"})

            success = es_u == 0 or es_a == 0
            msg = "Key deployed to at least one location." if success else f"Deployment failed. User: {err_u}. Admin: {err_a}."
            return success, msg
        else:
            # Comanda UNIX - try standard authorized_keys location
            socketio_instance.emit('deploy_log', {'data': "Attempting UNIX deployment (~/.ssh/authorized_keys)...\n"})
            cmd_unix = f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && grep -qF '{pub_key_content}' ~/.ssh/authorized_keys || echo '{pub_key_content}' >> ~/.ssh/authorized_keys"
            _, stdout, stderr = client.exec_command(cmd_unix)
            exit_status = stdout.channel.recv_exit_status()
            error_output = stderr.read().decode()

            if exit_status == 0:
                # Key was written, but warn that this may not work for all devices
                socketio_instance.emit('deploy_log', {'data': "Key written to ~/.ssh/authorized_keys.\n"})
                socketio_instance.emit('deploy_log', {'data': "NOTE: Some devices (Brocade, Cisco, etc.) use different key storage.\n"})
                socketio_instance.emit('deploy_log', {'data': "If key auth fails, try Password authentication or manual key import.\n"})
                return True, "Key written to authorized_keys. NOTE: May not work on network devices - use Password auth if key auth fails."
            else:
                return False, f"Key deployment failed (UNIX): {error_output}"
                
    except paramiko.AuthenticationException:
        return False, "Authentication failed. Please check the password."
    except Exception as e:
        traceback.print_exc()
        return False, f"Key deployment exception: {type(e).__name__} - {e}"
    finally:
        if client:
            client.close()

# --- Functii de Conexiune si Comanda ---

def check_host_availability(ip_str: str):
    """Verifica daca host-ul este reachable folosind TCP pe portul SSH.
    TCP check este mai fiabil decat ping deoarece ICMP poate fi blocat
    de firewall (ex: Windows blocheaza ping by default).
    """
    if not ip_str:
        return False, "System IP cannot be empty."
    try:
        ipaddress.ip_address(ip_str)
    except ValueError:
        return False, f"Invalid IP address format: {ip_str}"

    cfg = get_config()
    port = cfg.getint('System', 'ssh_port', fallback=22)
    try:
        with socket.create_connection((ip_str, port), timeout=3):
            pass
        return True, f"Host {ip_str} is reachable (port {port} open)."
    except socket.timeout:
        return False, f"Host {ip_str} unreachable: TCP timeout on port {port}."
    except ConnectionRefusedError:
        return False, f"Host {ip_str}: port {port} refused."
    except OSError as e:
        return False, f"Host {ip_str} unreachable: {type(e).__name__}: {e}"

def check_ssh_connection():
    """
    Verifica conexiunea SSH folosind metoda de autentificare configurata.
    Supports: key, password, key_or_password (fallback)
    """
    cfg = get_config()
    ip = cfg.get('System', 'ip_address', fallback='').strip()
    user = cfg.get('System', 'username', fallback='').strip()
    key_path = cfg.get('System', 'ssh_key_path', fallback='').strip()
    auth_method = cfg.get('System', 'auth_method', fallback='key').strip()
    auth_password = cfg.get('System', 'auth_password', fallback='').strip()

    if not ip or not user:
        return False, "SSH Failed: System IP or Username is missing in config."

    # Validate auth requirements
    if auth_method == 'key' and (not key_path or not os.path.exists(key_path)):
        return False, f"SSH Failed: Key authentication selected but key file missing at: {key_path}"
    if auth_method == 'password' and not auth_password:
        return False, "SSH Failed: Password authentication selected but no password configured."
    if auth_method == 'key_or_password' and not auth_password and (not key_path or not os.path.exists(key_path)):
        return False, "SSH Failed: Neither key file nor password is available."

    client = None
    try:
        client = create_ssh_client_with_legacy_support()

        if auth_method == 'password':
            # Password only
            connect_with_legacy_support(client, hostname=ip, username=user, password=auth_password, timeout=5)
            return True, "SSH connection successful (password auth, legacy algorithm support)."

        elif auth_method == 'key':
            # Key only
            pkey = load_private_key(key_path)
            connect_with_legacy_support(client, hostname=ip, username=user, pkey=pkey, timeout=5)
            return True, "SSH connection successful (key auth, legacy algorithm support)."

        else:  # key_or_password - try key first, fallback to password
            try:
                if key_path and os.path.exists(key_path):
                    pkey = load_private_key(key_path)
                    connect_with_legacy_support(client, hostname=ip, username=user, pkey=pkey, timeout=5)
                    return True, "SSH connection successful (key auth, legacy algorithm support)."
            except (paramiko.AuthenticationException, paramiko.SSHException) as key_err:
                print(f"[SSH_UTILS] Key auth failed, trying password: {key_err}", flush=True)
                if client:
                    client.close()
                client = create_ssh_client_with_legacy_support()

            # Fallback to password
            if auth_password:
                connect_with_legacy_support(client, hostname=ip, username=user, password=auth_password, timeout=5)
                return True, "SSH connection successful (password fallback, legacy algorithm support)."
            else:
                return False, "SSH Authentication Failed: Key auth failed and no password configured for fallback."

    except paramiko.AuthenticationException:
        return False, "SSH Authentication Failed: Invalid credentials or key not authorized."
    except paramiko.SSHException as e:
        return False, f"SSH Protocol Error: {e}"
    except Exception as e:
        return False, f"SSH Connection Failed: {type(e).__name__} - {e}"
    finally:
        if client:
            client.close()

def _inject_noninteractive(command: str) -> str:
    """
    Preprocess commands to prevent interactive blocking on Linux/Debian targets.

    Injections (applied at every occurrence, handles compound bash -c '...' commands):
      - apt/apt-get/apt-cache/dpkg (any subcommand):
            DEBIAN_FRONTEND=noninteractive UCF_FORCE_CONFFNEW=1 APT_LISTCHANGES_FRONTEND=none PAGER=cat
        Covers both interactive prompts (debconf) and pager blocking (apt list, apt-get update).
      - Pager-prone tools (git log/diff/show/blame, systemctl, journalctl):
            PAGER=cat

    Injected directly before each matched keyword, so sudo/flags before it are preserved:
      sudo -E apt-get install x  ->  sudo -E DEBIAN_FRONTEND=... PAGER=cat apt-get install x
      bash -c '... apt-get update; ... apt list ...'
                                 ->  bash -c '... DEBIAN_FRONTEND=... PAGER=cat apt-get update;
                                                 ... DEBIAN_FRONTEND=... PAGER=cat apt list ...'

    Matches inside double-quoted strings (e.g. echo "apt-get update") are skipped to avoid
    false positives. Multiple occurrences processed right-to-left to preserve positions.
    """
    # ANY apt/apt-get/apt-cache subcommand, plus dpkg
    _APT_RE = re.compile(
        r'\bapt(?:-get|-cache)?\s+\w+'
        r'|\bdpkg\b',
        re.IGNORECASE
    )
    # Pager-prone tools that are not apt (handled separately above)
    _PAGER_RE = re.compile(
        r'\bgit\s+(?:log|diff|show|blame|grep|shortlog|stash\s+show)\b'
        r'|\bsystemctl\b'
        r'|\bjournalctl\b',
        re.IGNORECASE
    )

    def _in_double_quotes(text, pos):
        """Return True if pos is inside a double-quoted string (counts unescaped " before pos)."""
        count = 0
        i = 0
        while i < pos:
            if text[i] == '\\' and i + 1 < len(text):
                i += 2
                continue
            if text[i] == '"':
                count += 1
            i += 1
        return count % 2 == 1

    no_debian = 'DEBIAN_FRONTEND' not in command
    no_pager  = 'PAGER' not in command

    # Collect (position, prefix) pairs — will be applied right-to-left
    injections = []

    for m in _APT_RE.finditer(command):
        if _in_double_quotes(command, m.start()):
            continue  # skip: inside echo "..." or similar string argument
        parts = []
        if no_debian:
            parts.append('DEBIAN_FRONTEND=noninteractive UCF_FORCE_CONFFNEW=1 APT_LISTCHANGES_FRONTEND=none')
        if no_pager:
            parts.append('PAGER=cat')
        if parts:
            injections.append((m.start(), ' '.join(parts) + ' '))

    if no_pager:
        for m in _PAGER_RE.finditer(command):
            if _in_double_quotes(command, m.start()):
                continue
            injections.append((m.start(), 'PAGER=cat '))

    # Apply right-to-left so earlier positions are not shifted by later insertions
    result = command
    for pos, prefix in sorted(injections, key=lambda x: x[0], reverse=True):
        result = result[:pos] + prefix + result[pos:]

    # Inject XDG_RUNTIME_DIR + DBUS_SESSION_BUS_ADDRESS for systemctl --user commands.
    # Non-interactive SSH sessions (exec_command) don't load the user login environment,
    # so XDG_RUNTIME_DIR is unset and systemctl --user hangs indefinitely waiting for
    # the D-Bus socket — causing paramiko timeout. The fallback uses $(id -u) evaluated
    # at runtime so it works even when running as root (id -u = 0) or any other user.
    if (re.search(r'\bsystemctl\b', result, re.IGNORECASE)
            and '--user' in result
            and 'XDG_RUNTIME_DIR' not in result):
        uid_expr = '$(id -u)'
        xdg_prefix = (
            f'export XDG_RUNTIME_DIR=${{XDG_RUNTIME_DIR:-/run/user/{uid_expr}}} '
            f'DBUS_SESSION_BUS_ADDRESS=${{DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/{uid_expr}/bus}}; '
        )
        result = xdg_prefix + result
        print(f"[SSH_UTILS] Injected XDG_RUNTIME_DIR for systemctl --user command", flush=True)

    return result


def execute_ssh_command(command: str, force_pty: bool = None, on_chunk=None) -> str:
    """
    Executa o comanda pe sistemul remote si returneaza output-ul.

    Args:
        command: The command to execute
        force_pty: Optional override for PTY mode.
                   None = auto-detect based on OS (default)
                   False = force get_pty=False (useful for OS detection commands)
                   True = force get_pty=True
        on_chunk: Optional callback(text: str) called with each decoded, ANSI-stripped
                  chunk as it arrives from the remote host.  Use this for live streaming
                  to the UI.  The full output is still accumulated and returned normally.

    Uses a persistent SSH connection: the TCP+SSH session is reused across
    consecutive commands in the same task, avoiding 2-3s handshake overhead
    per command. The connection is closed by close_persistent_client() on
    task stop, system switch, or system config change.
    """
    global _ACTIVE_CHANNEL, ACTIVE_SSH_CLIENT
    cfg = get_config()
    ip = cfg.get('System', 'ip_address', fallback='').strip()
    user = cfg.get('System', 'username', fallback='').strip()
    key_path = cfg.get('System', 'ssh_key_path', fallback='').strip()
    auth_method = cfg.get('System', 'auth_method', fallback='key').strip()
    auth_password = cfg.get('System', 'auth_password', fallback='').strip()

    if not ip or not user:
        return "Error: System IP or Username is missing."

    # Validate auth requirements
    if auth_method == 'key' and (not key_path or not os.path.exists(key_path)):
        return f"Error: Key authentication selected but key file missing at: {key_path}"
    if auth_method == 'password' and not auth_password:
        return "Error: Password authentication selected but no password configured."

    last_error = None
    for attempt in range(len(SSH_RECONNECT_DELAYS) + 1):
        output_parts = []  # reset per-attempt so retry check (not output_parts) is accurate
        try:
            client = _get_persistent_client(cfg)
            ACTIVE_SSH_CLIENT = client  # Legacy alias for any external references

            # Determine PTY usage
            if force_pty is not None:
                use_pty = force_pty
            elif DETECTED_OS is not None:
                use_pty = (DETECTED_OS != 'windows')
            else:
                is_windows = any(cmd in command.lower() for cmd in
                                 ['ver', 'w32tm', 'net start', 'net stop', 'powershell', 'cmd.exe', 'dir ', 'cls'])
                use_pty = not is_windows

            # Inject DEBIAN_FRONTEND=noninteractive for apt/dpkg commands
            command = _inject_noninteractive(command)

            # Paramiko channel timeout: 1.5× the user-configured command_timeout so it acts
            # as a last-resort backstop, not a premature cut-off. The real timeout logic
            # (agent_core execute_ssh_command_with_timeout) will interrupt earlier via
            # abort_active_channel(); Paramiko's own timeout fires only if that layer fails.
            command_timeout_cfg = cfg.getint('Agent', 'command_timeout', fallback=300)
            paramiko_timeout = max(command_timeout_cfg * 1.5, 60)  # floor at 60s

            # get_pty=True pentru Unix (evita blocaje cu pager), False pentru Windows (evita ANSI escape sequences)
            stdin, stdout, stderr = client.exec_command(command, timeout=paramiko_timeout, get_pty=use_pty)

            # Register channel for per-command abort (abort_active_channel closes this without
            # touching the persistent connection, so execution can resume immediately)
            _ACTIVE_CHANNEL = stdout.channel

            # Close STDIN immediately — prevents sudo/psql/docker from hanging on input
            stdin.close()

            channel = stdout.channel

            # Streaming read: consume the channel in 4 KB chunks.
            # Each chunk is ANSI-stripped and forwarded to on_chunk (if provided) so
            # the caller can emit it to the UI in real-time without waiting for EOF.
            # channel.recv() blocks briefly until data arrives or the channel closes;
            # returning b'' signals EOF — same contract as the old stdout.read() call.
            while True:
                chunk = channel.recv(4096)
                if not chunk:
                    break
                decoded = chunk.decode('utf-8', 'ignore')
                output_parts.append(decoded)
                if on_chunk:
                    try:
                        on_chunk(strip_ansi_sequences(decoded))
                    except Exception:
                        pass  # Never let a UI callback crash the SSH read loop

            # When not using PTY, stderr travels on a separate channel
            # (PTY merges stderr into stdout, so recv_stderr has nothing in that case)
            error_parts = []
            if not use_pty:
                while channel.recv_stderr_ready():
                    err_chunk = channel.recv_stderr(4096)
                    if err_chunk:
                        error_parts.append(err_chunk.decode('utf-8', 'ignore'))

            exit_status = channel.recv_exit_status()

            output = ''.join(output_parts)
            error_output = ''.join(error_parts)

            print(f"[SSH_UTILS] Command: {command[:50]}... | output={len(output)}B stderr={len(error_output)}B pty={use_pty} exit={exit_status}", flush=True)

            # Stergem ANSI escape sequences din output-ul complet acumulat
            output_stripped = strip_ansi_sequences(output)
            error_output_stripped = strip_ansi_sequences(error_output)

            full_output = ""
            if output_stripped:
                full_output += output_stripped
            if error_output_stripped:
                full_output += ("\n---\nSTDERR:\n" if full_output else "") + error_output_stripped

            if exit_status != 0:
                return f"Error (Exit Code {exit_status}):\n{full_output if full_output else 'Command failed with no output.'}"
            else:
                return full_output if full_output else "Success: Command executed with no output."

        except paramiko.AuthenticationException:
            # Auth failure means persistent client is useless — discard it. No retry.
            close_persistent_client()
            return "An SSH function exception occurred: Authentication Failed."
        except (TimeoutError, socket.timeout, paramiko.buffered_pipe.PipeTimeout):
            # Expected when abort_active_channel() closes the channel mid-read.
            # This is intentional interruption — do not retry.
            print("[SSH_UTILS] Command interrupted by timeout (channel closed mid-read).", flush=True)
            return "Command timed out: SSH channel closed while waiting for output."
        except Exception as e:
            last_error = e
            # Retry only if: (a) it's a transient network/transport error, (b) the command
            # hadn't started streaming yet (output_parts empty — no side-effects to worry about),
            # and (c) we still have retry budget left.
            if _is_connection_error(e) and not output_parts and attempt < len(SSH_RECONNECT_DELAYS):
                delay = SSH_RECONNECT_DELAYS[attempt]
                print(f"[SSH_UTILS] Connection error on attempt {attempt + 1}/{len(SSH_RECONNECT_DELAYS) + 1}, "
                      f"reconnecting in {delay}s: {type(e).__name__}: {e}", flush=True)
                close_persistent_client()
                time.sleep(delay)
                continue  # next attempt — finally below clears _ACTIVE_CHANNEL first
            # Non-retryable error or retry budget exhausted
            print(f"SSH execution error: {e}")
            traceback.print_exc()
            close_persistent_client()
            return f"An SSH function exception occurred: {type(e).__name__} - {e}"
        finally:
            # Always clear the active channel reference, even on continue/return
            _ACTIVE_CHANNEL = None

    # All reconnect attempts exhausted without a successful return inside the loop
    close_persistent_client()
    return f"SSH connection failed after {len(SSH_RECONNECT_DELAYS) + 1} reconnect attempts: {last_error}"


def execute_ssh_block_command(commands: list, enable_password: str = None, timeout_per_command: int = 30) -> str:
    """
    Execute multiple commands in an interactive shell session to maintain context.
    This is essential for Cisco/network device configuration where commands like
    'configure terminal' must persist across subsequent commands.

    Args:
        commands: List of commands to execute in sequence
        enable_password: Optional enable/secret password for privileged mode
        timeout_per_command: Timeout in seconds to wait for each command's output

    Returns:
        Combined output from all commands
    """
    global _ACTIVE_CHANNEL, ACTIVE_SSH_CLIENT
    cfg = get_config()
    ip = cfg.get('System', 'ip_address', fallback='').strip()
    user = cfg.get('System', 'username', fallback='').strip()
    key_path = cfg.get('System', 'ssh_key_path', fallback='').strip()
    auth_method = cfg.get('System', 'auth_method', fallback='key').strip()
    auth_password = cfg.get('System', 'auth_password', fallback='').strip()
    device_type = cfg.get('System', 'device_type', fallback='linux').strip().lower()

    if not ip or not user:
        return "Error: System IP or Username is missing."

    # Validate auth requirements
    if auth_method == 'key' and (not key_path or not os.path.exists(key_path)):
        return f"Error: Key authentication selected but key file missing at: {key_path}"
    if auth_method == 'password' and not auth_password:
        return "Error: Password authentication selected but no password configured."

    channel = None
    try:
        client = _get_persistent_client(cfg)
        ACTIVE_SSH_CLIENT = client  # Legacy alias

        # Open interactive shell
        channel = client.invoke_shell(term='vt100', width=200, height=50)
        channel.settimeout(timeout_per_command)
        _ACTIVE_CHANNEL = channel  # Register for per-command abort

        import time
        all_output = []

        def read_until_prompt(channel, timeout=30, prompts=None):
            """Read channel output until a prompt is detected or timeout."""
            if prompts is None:
                # Common prompts for various devices
                prompts = ['#', '>', '$', ':', '(config)#', '(config-if)#', '(config-vlan)#']

            output = ""
            start_time = time.time()
            while True:
                if time.time() - start_time > timeout:
                    break
                if channel.recv_ready():
                    chunk = channel.recv(4096).decode('utf-8', 'ignore')
                    output += chunk
                    # Check if we've reached a prompt
                    stripped = output.strip()
                    if stripped and any(stripped.endswith(p) for p in prompts):
                        break
                else:
                    time.sleep(0.1)
            return output

        # Wait for initial prompt
        initial_output = read_until_prompt(channel, timeout=10)
        all_output.append(f"=== Connected ===\n{initial_output}")
        print(f"[SSH_BLOCK] Initial prompt: {initial_output[-100:]}", flush=True)

        # Handle enable mode for Cisco devices
        if device_type in ['cisco', 'ios', 'nxos', 'iosxe', 'iosxr']:
            # Check if we need to enter enable mode
            if enable_password and not initial_output.strip().endswith('#'):
                channel.send('enable\n')
                time.sleep(0.5)
                enable_output = read_until_prompt(channel, timeout=10, prompts=['Password:', 'password:', '#'])
                if 'Password' in enable_output or 'password' in enable_output:
                    channel.send(enable_password + '\n')
                    time.sleep(0.5)
                    enable_result = read_until_prompt(channel, timeout=10)
                    all_output.append(f"=== Enable Mode ===\n{enable_result}")
                    if '#' not in enable_result:
                        return f"Error: Failed to enter enable mode.\n{enable_result}"

        # --- DISABLE PAGING ---
        # Send device-specific command to disable --More-- paging
        # This prevents interactive prompts from blocking execution
        paging_cmd = None
        if device_type in ['cisco', 'ios', 'iosxe', 'iosxr']:
            paging_cmd = 'terminal length 0'
        elif device_type == 'nxos':
            paging_cmd = 'terminal length 0'  # NX-OS also uses this
        elif device_type == 'arista':
            paging_cmd = 'terminal length 0'  # Arista EOS uses same command
        elif device_type == 'juniper':
            paging_cmd = 'set cli screen-length 0'
        elif device_type == 'brocade':
            # Brocade Fabric OS - try to disable paging
            paging_cmd = 'paging off'  # Some Brocade versions support this

        if paging_cmd:
            print(f"[SSH_BLOCK] Disabling paging with: {paging_cmd}", flush=True)
            channel.send(paging_cmd + '\n')
            time.sleep(0.3)
            paging_output = read_until_prompt(channel, timeout=5)
            all_output.append(f"=== Paging Disabled ({paging_cmd}) ===\n{paging_output}")
            # Don't fail if paging command has issues - some devices might not support it

        # Execute each command
        for i, cmd in enumerate(commands):
            cmd = cmd.strip()
            if not cmd:
                continue

            # Inject DEBIAN_FRONTEND=noninteractive for apt/dpkg commands
            cmd = _inject_noninteractive(cmd)

            print(f"[SSH_BLOCK] Sending command {i+1}/{len(commands)}: {cmd}", flush=True)
            channel.send(cmd + '\n')
            time.sleep(0.3)  # Small delay for command processing

            # Read output until next prompt
            cmd_output = read_until_prompt(channel, timeout=timeout_per_command)
            all_output.append(f"\n=== Command: {cmd} ===\n{cmd_output}")
            print(f"[SSH_BLOCK] Output length: {len(cmd_output)}", flush=True)

            # Check for error indicators
            error_indicators = ['% Invalid', '% Ambiguous', '% Incomplete', 'Error:', 'error:', 'Command rejected']
            if any(err in cmd_output for err in error_indicators):
                print(f"[SSH_BLOCK] Error detected in command output", flush=True)
                # Continue anyway to let the agent see the error

        # Combine all output
        combined_output = '\n'.join(all_output)
        cleaned_output = strip_ansi_sequences(combined_output)

        return cleaned_output if cleaned_output else "Success: Block commands executed with no output."

    except paramiko.AuthenticationException:
        close_persistent_client()
        return "An SSH function exception occurred: Authentication Failed."
    except Exception as e:
        print(f"SSH block execution error: {e}")
        traceback.print_exc()
        close_persistent_client()
        return f"An SSH function exception occurred: {type(e).__name__} - {e}"
    finally:
        # Close the interactive shell channel (it's a stateful PTY session — always discard after use).
        # The underlying TCP+SSH connection (_PERSISTENT_CLIENT) stays alive for the next command.
        if channel:
            try:
                channel.close()
            except Exception:
                pass
        _ACTIVE_CHANNEL = None


def is_block_command(command: str) -> bool:
    """
    Detect if a command should be executed in block mode.
    Block mode is ONLY used for network devices (Cisco, Brocade, Juniper, Arista, etc.)
    where an interactive shell session is required to maintain config context.

    For Linux/Windows systems, all commands (including multi-line and heredocs) are
    executed via standard exec mode which handles them correctly.
    """
    if not command:
        return False

    cfg = get_config()
    device_type = cfg.get('System', 'device_type', fallback='linux').strip().lower()

    network_device_types = ['cisco', 'ios', 'nxos', 'iosxe', 'iosxr', 'brocade', 'juniper', 'arista', 'other']
    if device_type in network_device_types:
        print(f"[SSH_UTILS] Block mode enabled for network device: {device_type}", flush=True)
        return True

    return False


def parse_block_command(command: str) -> list:
    """
    Parse a block command into a list of individual commands.

    Handles:
    - BLOCK: or CONFIG: prefix markers
    - Multi-line commands
    - Commands separated by semicolons (for shell) or newlines (for network devices)
    """
    # Remove block markers
    for marker in ['BLOCK:', 'CONFIG:', 'INTERACTIVE:']:
        if command.strip().upper().startswith(marker):
            command = command.strip()[len(marker):].strip()
            break

    # Split by newlines
    lines = command.strip().split('\n')

    # Clean up each line
    commands = []
    for line in lines:
        line = line.strip()
        if line and not line.startswith('#'):  # Skip empty lines and comments
            commands.append(line)

    return commands


def abort_active_connection():
    """
    Forcibly close the current channel AND the persistent SSH connection.
    Used when the user clicks 'Stop Task' — full teardown, next task starts fresh.
    For 'Abort Cmd' (pause without stop), use abort_active_channel() instead.
    """
    abort_active_channel()       # Unblock any pending stdout.read()
    close_persistent_client()    # Discard persistent connection (next task reconnects)
    print("[SSH_UTILS] Active connection fully aborted.", flush=True)
