import os
import re
import paramiko
import ipaddress
import subprocess
import traceback
from config import get_config, KEYS_DIR

# --- Constante pentru căile cheilor ---
PRIVATE_KEY_PATH = os.path.join(KEYS_DIR, 'id_rsa')
PUBLIC_KEY_PATH = os.path.join(KEYS_DIR, 'id_rsa.pub')

# Global variable to track the currently active SSH client
ACTIVE_SSH_CLIENT = None

# Global variable to store detected OS type
DETECTED_OS = None  # Will be set to 'windows', 'linux', 'macos', or None

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
    """
    client = None
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(ip, username=user, password=pwd, timeout=10)
        
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
            # Comanda UNIX
            cmd_unix = f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && grep -qF '{pub_key_content}' ~/.ssh/authorized_keys || echo '{pub_key_content}' >> ~/.ssh/authorized_keys"
            _, stdout, stderr = client.exec_command(cmd_unix)
            exit_status = stdout.channel.recv_exit_status()
            error_output = stderr.read().decode()
            
            if exit_status == 0:
                return True, "Key deployed successfully (UNIX)."
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
    """Verifica daca host-ul este reachable folosind ping."""
    if not ip_str:
        return False, "System IP cannot be empty."
    try:
        ipaddress.ip_address(ip_str)
    except ValueError:
        return False, f"Invalid IP address format: {ip_str}"
        
    try:
        timeout_param = "-W" if os.name != 'nt' else "-w"
        timeout_value = "1" if os.name != 'nt' else "1000"
        result = subprocess.run(
            ["ping", "-c", "1", timeout_param, timeout_value, ip_str],
            capture_output=True, text=True, check=False, timeout=2
        )
        return (True, f"Host {ip_str} is reachable.") if result.returncode == 0 else (False, f"Host {ip_str} unreachable/timeout.")
    except subprocess.TimeoutExpired:
        return False, f"Ping to {ip_str} timed out (2s)."
    except Exception as e:
        return False, f"Ping error: {str(e)}"

def check_ssh_connection():
    """Verifica conexiunea SSH folosind cheia privata."""
    cfg = get_config()
    ip = cfg.get('System', 'ip_address', fallback='').strip()
    user = cfg.get('System', 'username', fallback='').strip()
    key_path = cfg.get('System', 'ssh_key_path', fallback='').strip()
    
    if not all([ip, user, key_path]):
        return False, "SSH Failed: System IP, Username, or SSH Key Path is missing in config."
    if not os.path.exists(key_path):
        return False, f"SSH Failed: Key file missing at path: {key_path}."
        
    client = None
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        pkey = paramiko.RSAKey.from_private_key_file(key_path)
        client.connect(hostname=ip, username=user, pkey=pkey, timeout=5)
        return True, "SSH connection successful."
    except paramiko.AuthenticationException:
        return False, "SSH Authentication Failed: Invalid credentials or key not authorized."
    except paramiko.SSHException as e:
        return False, f"SSH Protocol Error: {e}"
    except Exception as e:
        return False, f"SSH Connection Failed: {type(e).__name__} - {e}"
    finally:
        if client:
            client.close()

def execute_ssh_command(command: str) -> str:
    """Executa o comanda pe sistemul remote si returneaza output-ul."""
    global ACTIVE_SSH_CLIENT
    cfg = get_config()
    ip = cfg.get('System', 'ip_address', fallback='').strip()
    user = cfg.get('System', 'username', fallback='').strip()
    port = cfg.getint('System', 'ssh_port', fallback=22)
    key_path = cfg.get('System', 'ssh_key_path', fallback='').strip()

    if not all([ip, user, key_path]):
        return "Error: System IP, Username, or SSH Key Path is missing."

    client = None
    try:
        client = paramiko.SSHClient()
        ACTIVE_SSH_CLIENT = client  # Register active client
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        pkey = paramiko.RSAKey.from_private_key_file(key_path)
        client.connect(hostname=ip, port=port, username=user, pkey=pkey, timeout=30)

        # Determine PTY usage based on detected OS
        # Use actual detected OS if available, otherwise fallback to keyword heuristic
        if DETECTED_OS is not None:
            # Use the OS that was actually detected at task start
            is_windows = (DETECTED_OS == 'windows')
        else:
            # Fallback: Heuristic detection based on command keywords (less reliable)
            # Windows commands: ver, dir, cd, cls, etc.
            is_windows = any(cmd in command.lower() for cmd in ['ver', 'w32tm', 'net start', 'net stop', 'powershell', 'cmd.exe', 'dir ', 'cls'])

        # get_pty=True pentru Unix (evita blocaje cu pager), False pentru Windows (evita ANSI escape sequences)
        use_pty = not is_windows
        stdin, stdout, stderr = client.exec_command(command, timeout=1200, get_pty=use_pty)

        # --- FIX: Close STDIN immediately ---
        # This prevents commands like 'sudo', 'psql', or 'docker' from hanging
        # while waiting for input that will never come.
        stdin.close()
        # ------------------------------------

        output = stdout.read().decode('utf-8', 'ignore').strip()
        error_output = stderr.read().decode('utf-8', 'ignore').strip()
        exit_status = stdout.channel.recv_exit_status()

        # DEBUG: Log raw output before stripping
        print(f"[SSH_UTILS DEBUG] Command: {command[:50]}...", flush=True)
        print(f"[SSH_UTILS DEBUG] Raw stdout length: {len(output)}, stderr length: {len(error_output)}", flush=True)
        print(f"[SSH_UTILS DEBUG] Raw stdout (first 200 chars): {repr(output[:200])}", flush=True)
        print(f"[SSH_UTILS DEBUG] get_pty={use_pty}, exit_status={exit_status}", flush=True)

        # Stergem ANSI escape sequences din output
        output_stripped = strip_ansi_sequences(output)
        error_output_stripped = strip_ansi_sequences(error_output)

        # DEBUG: Log after stripping
        print(f"[SSH_UTILS DEBUG] After strip stdout length: {len(output_stripped)}, stderr length: {len(error_output_stripped)}", flush=True)
        print(f"[SSH_UTILS DEBUG] After strip stdout (first 200 chars): {repr(output_stripped[:200])}", flush=True)

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
        return "An SSH function exception occurred: Authentication Failed."
    except Exception as e:
        print(f"SSH execution error: {e}")
        traceback.print_exc()
        return f"An SSH function exception occurred: {type(e).__name__} - {e}"
    finally:
        if client:
            client.close()
        ACTIVE_SSH_CLIENT = None  # Unregister


def abort_active_connection():
    """
    Forcibly close the active SSH connection to unblock pending IO operations.
    Used when the user clicks 'Stop Task'.
    """
    global ACTIVE_SSH_CLIENT
    if ACTIVE_SSH_CLIENT:
        try:
            print("Force closing active SSH connection...")
            ACTIVE_SSH_CLIENT.close()
            ACTIVE_SSH_CLIENT = None
        except Exception as e:
            print(f"Error closing SSH connection: {e}")
