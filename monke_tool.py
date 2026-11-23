#!/usr/bin/env python3
"""
Monke Tool - Roblox Auto-Rejoin Script
Automatically monitors and rejoins Roblox games if disconnected or crashed.
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

import requests
from discord_webhook import DiscordEmbed, DiscordWebhook
from rich import box
from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text
from rich.layout import Layout
from rich.live import Live
from rich import print as rprint

# --- CONSTANTS ---
ASCII_ART = """
█▀▄▀█ ████▄ ██   █ █  █▀ ▄███▄        ▀▀█▀▀ ████▄ ████▄ █
█ █ █ █   █ █ █  █ █▄█   █▀             █   █   █ █   █ █
█   █ █   █ █  █ █ █▀▄   ██▄▄           █   █   █ █   █ █
█   █ ▀████ █   ██ █  █  █▄   ▄         █   ▀████ ▀████ █▄▄▄▄
    █                 ▀   ▀███▀         ▀                    
"""

# Get the directory where the script is located
SCRIPT_DIR = Path(__file__).resolve().parent
LOG_FILE = SCRIPT_DIR / "monke.log"
CONFIG_FILE = SCRIPT_DIR / "config.json"
BACKUP_DIR = SCRIPT_DIR / "backups"

# Default values
DEFAULT_CHECK_INTERVAL = 60
DEFAULT_DOUBLE_CHECK_INTERVAL = 15
DEFAULT_DOUBLE_CHECK_COUNT = 1
DEFAULT_LAUNCH_TIMEOUT = 120
DEFAULT_WEBHOOK_INTERVAL = 600
MAX_CONSECUTIVE_FAILURES = 5
RATE_LIMIT_DELAY = 2

ROBLOX_ACTIVITY = "com.roblox.client.ActivityProtocolLaunch"
ROBLOX_LAUNCHER = "com.roblox.client.startup.LauncherAliasMain"


def resolve_package(client_name: str) -> str:
    """Resolve a client name to a full Android package name.

    Accepts a full package name (e.g. 'com.roblox.client', 'free.nokaA')
    or a legacy short suffix (e.g. 'clientu' -> 'com.roblox.clientu').
    """
    client_name = (client_name or "").strip()
    if not client_name:
        return ""
    if "." in client_name:
        return client_name
    return f"com.roblox.{client_name}"


_ROOT_SHELL = None

def shell_prefix() -> str:
    """Return the root-shell prefix for device commands ('su -c', 'vu -c', ...).

    Some devices (e.g. cloud phones) have no 'su' binary but ship an
    alternative like /system/bin/vu that provides root. Probe candidates
    once and cache the first one that yields uid=0.
    """
    global _ROOT_SHELL
    if _ROOT_SHELL is None:
        for cand in ("su", "/system/bin/vu", "/system/bin/su", "/mnt/.xu/su"):
            try:
                r = subprocess.run(
                    [cand, "-c", "id"],
                    capture_output=True, text=True, timeout=10
                )
                if r.returncode == 0 and "uid=0" in r.stdout:
                    _ROOT_SHELL = f"{cand} -c"
                    break
            except Exception:
                continue
        if _ROOT_SHELL is None:
            _ROOT_SHELL = "su -c"
        logger.info(f"Root shell: {_ROOT_SHELL}")
    return _ROOT_SHELL

# Serialization keys
SERIALIZED_CONFIG_KEYS: Set[str] = frozenset([
    "users", "check_interval", "double_check_interval", "double_check_count",
    "launch_timeout", "webhook_url", "webhook_interval", "max_failures"
])

SERIALIZED_USER_KEYS: Set[str] = frozenset([
    "username", "user_id", "server", "client_name"
])

# Change working directory
os.chdir(SCRIPT_DIR)

# Create backup directory if it doesn't exist
BACKUP_DIR.mkdir(exist_ok=True)

# --- LOGGING SETUP ---
class CustomLogger:
    """Custom logger with rotation and better formatting."""
    
    def __init__(self, log_file: Path, max_size_mb: int = 10, backup_count: int = 3):
        self.log_file = log_file
        self.max_size = max_size_mb * 1024 * 1024
        self.backup_count = backup_count
        self._setup_logging()
    
    def _setup_logging(self):
        """Configure logging with rotation."""
        # Clear old log if it exists and is too large
        if self.log_file.exists() and self.log_file.stat().st_size > self.max_size:
            self._rotate_logs()
        
        logging.basicConfig(
            filename=self.log_file,
            level=logging.DEBUG,
            format="%(asctime)s - %(levelname)s - [%(threadName)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        self.logger = logging.getLogger(__name__)
    
    def _rotate_logs(self):
        """Rotate log files."""
        for i in range(self.backup_count - 1, 0, -1):
            old_backup = self.log_file.with_suffix(f".log.{i}")
            new_backup = self.log_file.with_suffix(f".log.{i+1}")
            if old_backup.exists():
                old_backup.rename(new_backup)
        
        if self.log_file.exists():
            self.log_file.rename(self.log_file.with_suffix(".log.1"))
    
    def debug(self, msg): self.logger.debug(msg)
    def info(self, msg): self.logger.info(msg)
    def warning(self, msg): self.logger.warning(msg)
    def error(self, msg): self.logger.error(msg)
    def exception(self, msg): self.logger.exception(msg)

logger = CustomLogger(LOG_FILE)
console = Console()

# --- GLOBAL STATE ---
user_statuses: Dict[str, str] = {}
user_failures: Dict[str, int] = {}
_status_lock = threading.Lock()
_monitoring_active = True
_stop_event = threading.Event()  # Avoids busy-wait polling loops
_start_time = time.time()  # Track program start time for uptime
_last_webhook_time = 0  # Track last webhook send time

# Reuse HTTP connections per thread - avoids a new TCP/TLS handshake on
# every presence check, which is expensive on low-end Android/Termux.
_thread_local = threading.local()


def http_session():
    """Return a per-thread requests.Session (connection pooling)."""
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        _thread_local.session = session
    return session

# --- HELPER FUNCTIONS ---

def clear_screen():
    """Clear the console screen without spawning a subprocess."""
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()

def print_centered(text: str, style: str = "bold white") -> None:
    """Print centered text."""
    console.print(Align.center(f"[{style}]{text}[/{style}]"))

def print_spaced_centered(text: str, style: str = "bold white", top_spacing: int = 1, bottom_spacing: int = 1):
    """Print centered text with spacing above and below."""
    if top_spacing:
        console.print()
    console.print(Align.center(f"[{style}]{text}[/{style}]"))
    if bottom_spacing:
        console.print()

def show_message(message: str, style: str = "yellow", duration: int = 2):
    """Show a centered message and pause."""
    clear_screen()
    banner()
    console.print()
    print_centered(message, style)
    console.print()
    if duration:
        time.sleep(duration)

def _exit_on_eof():
    """Exit gracefully when stdin is closed (e.g. piped installer)."""
    print_centered("Input closed - run the script directly in a terminal", "red")
    sys.exit(0)

def wait_for_input(message: str = "Press Enter to continue..."):
    """Wait for user input with centered prompt."""
    console.print()
    print_centered(message, "dim")
    console.print()
    try:
        input()
    except EOFError:
        _exit_on_eof()

def centered_prompt(message: str, default: str = None, password: bool = False) -> str:
    """Show a centered prompt and return user input."""
    console.print()
    print_centered(message, "bold cyan")
    if default:
        print_centered(f"(default: {default})", "dim")
    console.print()

    # Position cursor in center by calculating approximate position
    width = console.width
    padding = " " * (width // 3)

    try:
        if password:
            return Prompt.ask(padding, password=True, default=default)
        else:
            return Prompt.ask(padding, default=default)
    except EOFError:
        _exit_on_eof()

def centered_confirm(message: str, default: str = "n") -> bool:
    """Show a centered confirmation prompt."""
    console.print()
    print_centered(message, "bold yellow")
    print_centered("y/n", "bold cyan")
    console.print()

    width = console.width
    padding = " " * (width // 3)

    try:
        response = Prompt.ask(padding, choices=["y", "n", "Y", "N"], default=default)
    except EOFError:
        _exit_on_eof()
    return response.lower() == "y"

def get_uptime() -> str:
    """Get formatted uptime string showing hours and minutes."""
    uptime_seconds = int(time.time() - _start_time)
    hours = uptime_seconds // 3600
    minutes = (uptime_seconds % 3600) // 60
    
    if hours > 0:
        if hours == 1:
            return f"{hours} hour {minutes} min"
        else:
            return f"{hours} hours {minutes} min"
    else:
        return f"{minutes} min"

# --- DATACLASSES ---
@dataclass
class User:
    """Stores information about a single Roblox user."""
    username: str = ""
    user_id: str = ""
    server: str = ""
    client_name: str = ""
    start_time: float = None
    last_check: float = None
    consecutive_failures: int = 0
    last_status: str = ""

    def __post_init__(self):
        """Called after creation; fetches missing info."""
        self.update_user_info()
        reset_user_status(self.user_id)

    def update_user_info(self):
        """Fetches username from ID or vice-versa."""
        if self.user_id and not self.username:
            self.username = self.get_username_from_id(self.user_id)
        elif self.username and not self.user_id:
            self.user_id = self.get_user_id_from_name(self.username)

    @staticmethod
    def get_username_from_id(user_id: str) -> str:
        """Fetch username from Roblox API with retry logic."""
        for attempt in range(3):
            try:
                response = http_session().get(
                    f"https://users.roblox.com/v1/users/{user_id}",
                    timeout=10
                )
                if response.status_code == 200:
                    return response.json().get("name", "")
                elif response.status_code == 429:  # Rate limited
                    time.sleep(RATE_LIMIT_DELAY * (attempt + 1))
                    continue
            except requests.RequestException as e:
                logger.error(f"Error fetching username for ID {user_id}: {e}")
                if attempt < 2:
                    time.sleep(RATE_LIMIT_DELAY)
        return ""

    @staticmethod
    def get_user_id_from_name(username: str) -> str:
        """Fetch user ID from Roblox API with retry logic."""
        url = "https://users.roblox.com/v1/usernames/users"
        for attempt in range(3):
            try:
                response = http_session().post(
                    url,
                    json={"usernames": [username]},
                    timeout=10
                )
                if response.status_code == 200:
                    data = response.json().get("data", [])
                    return str(data[0]["id"]) if data else ""
                elif response.status_code == 429:
                    time.sleep(RATE_LIMIT_DELAY * (attempt + 1))
                    continue
            except requests.RequestException as e:
                logger.error(f"Error fetching user ID for {username}: {e}")
                if attempt < 2:
                    time.sleep(RATE_LIMIT_DELAY)
        return ""

    @property
    def status(self):
        """Get user's current status."""
        return get_user_status(self.user_id)

@dataclass
class Config:
    """Application configuration."""
    users: List[User] = field(default_factory=list)
    check_interval: int = DEFAULT_CHECK_INTERVAL
    double_check_interval: int = DEFAULT_DOUBLE_CHECK_INTERVAL
    double_check_count: int = DEFAULT_DOUBLE_CHECK_COUNT
    launch_timeout: int = DEFAULT_LAUNCH_TIMEOUT
    webhook_url: str = ""
    webhook_interval: int = DEFAULT_WEBHOOK_INTERVAL
    max_failures: int = MAX_CONSECUTIVE_FAILURES

# --- CONFIGURATION MANAGEMENT ---

def backup_config():
    """Create a backup of the current config file."""
    if CONFIG_FILE.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = BACKUP_DIR / f"config_{timestamp}.json"
        try:
            import shutil
            shutil.copy2(CONFIG_FILE, backup_path)
            logger.info(f"Config backup created: {backup_path}")
        except Exception as e:
            logger.error(f"Failed to create config backup: {e}")

def load_config() -> Config:
    """Load configuration from file with error handling."""
    logger.info("Loading configuration")
    
    if not CONFIG_FILE.exists():
        logger.info("Config file not found, creating new one")
        return Config()

    try:
        data = json.loads(CONFIG_FILE.read_text())
        
        # Validate required fields
        if not isinstance(data, dict):
            raise ValueError("Config file must contain a JSON object")
        
        # Filter main config keys
        filtered_data = {k: v for k, v in data.items() 
                        if k in SERIALIZED_CONFIG_KEYS}
        
        # Filter and validate user data
        users_data = filtered_data.get("users", [])
        if not isinstance(users_data, list):
            users_data = []
        
        filtered_data["users"] = [
            {k: v for k, v in user.items() if k in SERIALIZED_USER_KEYS}
            for user in users_data if isinstance(user, dict)
        ]
        
        # Create config object
        config = Config(
            **{key: filtered_data.get(key, getattr(Config, key))
               for key in SERIALIZED_CONFIG_KEYS - {"users"}},
            users=[User(**user) for user in filtered_data["users"]],
        )
        
        logger.info(f"Loaded {len(config.users)} users from config")
        return config

    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in config file: {e}")
        backup_config()  # Backup corrupted file
        return Config()
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        return Config()

def save_config(config: Config) -> bool:
    """Save configuration to file with validation."""
    logger.info("Saving configuration")
    
    try:
        # Convert to dict and filter
        save_data = asdict(config)
        save_data = {k: v for k, v in save_data.items() 
                    if k in SERIALIZED_CONFIG_KEYS}
        
        save_data["users"] = [
            {k: v for k, v in asdict(user).items() 
             if k in SERIALIZED_USER_KEYS and v}
            for user in config.users
        ]
        
        # Write to temp file first, then rename
        temp_file = CONFIG_FILE.with_suffix('.tmp')
        temp_file.write_text(json.dumps(save_data, indent=4))
        temp_file.rename(CONFIG_FILE)
        
        logger.info("Configuration saved successfully")
        return True
        
    except Exception as e:
        logger.error(f"Error saving config: {e}")
        return False

# --- MENU OPTIONS ---

def validate_server_input(server: str) -> bool:
    """Validate server input format."""
    if not server or server.lower() == "none":
        return True
    if server.isdigit():
        return True
    if server.startswith(("https://", "roblox://")):
        return True
    return False

def auto_setup(config: Config):
    """Automatically detect and add Roblox clients."""
    logger.info("Starting auto setup")
    
    clear_screen()
    banner()
    console.print()
    print_centered("Auto Setup", "bold cyan")
    console.print()
    print_centered("⚠️  Make sure you're logged into Roblox instances before using this feature!", "bold yellow")
    console.print()
    
    if not centered_confirm("Continue with auto setup?"):
        show_message("Auto setup cancelled", "yellow")
        return

    clear_screen()
    banner()
    console.print()
    print_centered("Auto Setup - Scanning", "bold cyan")
    console.print()
    print_centered("Scanning for Roblox packages...", "bold yellow")
    console.print()
    
    try:
        # Find Roblox apps by their LocalStorage file - only Roblox packages
        # have /data/data/<pkg>/files/appData/LocalStorage/appStorage.json.
        # dumpsys/pm can't detect renamed clones without root, so we glob
        # the storage path directly via su instead.
        result = subprocess.run(
            f"{shell_prefix()} 'ls /data/data/*/files/appData/LocalStorage/appStorage.json 2>/dev/null'",
            shell=True,
            text=True,
            timeout=30,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        if result.returncode != 0:
            logger.error(f"Error listing packages: {result.stderr}")
            show_message(f"Error: {result.stderr}", "red")
            return

        packages = []
        for line in result.stdout.splitlines():
            parts = line.strip().split("/")
            # Expected: /data/data/<package>/files/appData/LocalStorage/appStorage.json
            if len(parts) >= 4 and parts[1] == "data" and parts[2] == "data":
                packages.append(parts[3])
        if not packages:
            show_message("No Roblox packages found", "yellow")
            return
        
        # Extract package names (may be com.roblox.* or renamed clones like free.nokaA)
        clients = []
        clear_screen()
        banner()
        console.print()
        print_centered("Found Packages:", "bold green")
        console.print()
        for line in packages:
            package = line.strip()
            if not package:
                continue
            client_name = package
            json_path = f"/data/data/{package}/files/appData/LocalStorage/appStorage.json"
            clients.append((client_name, json_path))
            print_centered(f"📦 {client_name}", "cyan")
        
        wait_for_input()
        
        # Separate existing and new clients
        existing = []
        new_clients = []
        
        for client_name, json_path in clients:
            if any(resolve_package(u.client_name) == resolve_package(client_name)
                   for u in config.users):
                existing.append(client_name)
            else:
                new_clients.append((client_name, json_path))
        
        if existing:
            clear_screen()
            banner()
            console.print()
            print_centered("Already Configured", "bold yellow")
            console.print()
            for client in existing:
                print_centered(f"• {client}", "cyan")
            console.print()
            wait_for_input()
        
        if not new_clients:
            show_message("No new clients to add", "yellow")
            return
        
        # Process new clients one by one
        added = []
        for client_name, json_path in new_clients:
            clear_screen()
            banner()
            console.print()
            print_centered(f"Processing: {client_name}", "bold cyan")
            console.print()
            
            try:
                # Read appStorage.json with proper subprocess configuration
                result = subprocess.run(
                    f"{shell_prefix()} 'cat {json_path}'",
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.PIPE,
                    timeout=10
                )
                
                if result.returncode != 0:
                    logger.error(f"Failed to read {json_path}")
                    stderr = result.stderr.decode().strip() if result.stderr else "Unknown error"
                    print_centered(f"Failed to read storage file for {client_name}", "red")
                    print_centered(f"Error: {stderr}", "dim")
                    console.print()
                    wait_for_input()
                    continue
                
                data = json.loads(result.stdout)
                username = data.get("Username")
                user_id = data.get("UserId")
                
                if not user_id or user_id == "-1":
                    print_centered(f"No user logged in for {client_name}", "yellow")
                    console.print()
                    wait_for_input()
                    continue
                
                console.print()
                print_centered(f"Found user: {username}", "bold green")
                console.print()
                print_centered("Enter 'none' to skip, 'exit' to cancel", "dim")
                console.print()
                
                server = centered_prompt("Enter server link or place ID:", default="none")
                
                if server.lower() == "exit":
                    show_message("Auto setup cancelled", "yellow")
                    return
                
                if not validate_server_input(server):
                    print_centered("Invalid server format", "red")
                    console.print()
                    wait_for_input()
                    continue
                
                if server.lower() == "none":
                    server = ""
                    print_centered("Skipping server configuration", "yellow")
                    console.print()
                    time.sleep(1)
                
                # Add user
                new_user = User(
                    username=username,
                    user_id=user_id,
                    server=server,
                    client_name=client_name,
                )
                new_user.update_user_info()
                config.users.append(new_user)
                added.append(new_user)
                
                clear_screen()
                banner()
                console.print()
                print_centered(f"✓ Added {username} successfully!", "green")
                wait_for_input()
                
            except json.JSONDecodeError:
                logger.error(f"Invalid JSON for {client_name}")
                print_centered(f"Invalid storage data for {client_name}", "red")
                console.print()
                wait_for_input()
            except subprocess.TimeoutExpired:
                logger.error(f"Timeout reading {client_name}")
                print_centered(f"Timeout reading {client_name}", "red")
                console.print()
                wait_for_input()
            except Exception as e:
                logger.error(f"Error processing {client_name}: {e}")
                print_centered(f"Error: {e}", "red")
                console.print()
                wait_for_input()
        
        if added:
            if save_config(config):
                show_message(f"✓ Added {len(added)} user(s) successfully!", "green")
            else:
                show_message("Failed to save configuration", "red")
        
    except subprocess.TimeoutExpired:
        show_message("Package listing timeout", "red")
    except Exception as e:
        logger.error(f"Auto setup error: {e}")
        show_message(f"Error: {e}", "red")

def add_user(config: Config):
    """Manually add a new user."""
    logger.info("Adding new user")
    
    clear_screen()
    banner()
    console.print()
    print_centered("Add User", "bold cyan")
    console.print()
    
    if config.users:
        display_users(config.users)
        console.print()
    
    print_centered("Enter 'exit' to cancel", "dim")
    console.print()
    
    identifier = centered_prompt("Enter User ID or Username:", default="none")
    
    if identifier.lower() == "exit":
        show_message("Operation cancelled", "yellow")
        return
    
    console.print()
    print_centered("Enter 'none' to skip, 'exit' to cancel", "dim")
    console.print()
    
    server = centered_prompt("Enter Server Link or Place ID:", default="none")
    
    if server.lower() == "exit":
        show_message("Operation cancelled", "yellow")
        return
    
    if server.lower() == "none":
        server = ""
    
    if not validate_server_input(server):
        show_message("Invalid server format", "red")
        return
    
    console.print()
    print_centered("Enter 'exit' to cancel", "dim")
    console.print()
    
    client_name = centered_prompt(
        "Enter Package Name (e.g., com.roblox.client, free.nokaA):",
        default="exit"
    )
    
    if client_name.lower() == "exit":
        show_message("Operation cancelled", "yellow")
        return
    
    show_message("Fetching user info...", "yellow", 1)
    
    new_user = User(
        username="" if identifier.isdigit() else identifier,
        user_id=identifier if identifier.isdigit() else "",
        server=server,
        client_name=client_name,
    )
    
    new_user.update_user_info()
    
    if not new_user.user_id or not new_user.username:
        show_message("Failed to fetch user info from Roblox", "red")
        return
    
    config.users.append(new_user)
    
    if save_config(config):
        show_message(f"✓ Added {new_user.username} successfully!", "green")
    else:
        show_message("Failed to save configuration", "red")

def remove_user(config: Config):
    """Remove a user by index."""
    logger.info("Removing user")
    
    if not config.users:
        show_message("No users to remove", "yellow")
        return
    
    clear_screen()
    banner()
    console.print()
    print_centered("Remove User", "bold cyan")
    console.print()
    display_users(config.users)
    console.print()
    
    print_centered("Enter 'exit' to cancel", "dim")
    console.print()
    
    index = centered_prompt("Enter the index of the user to remove:", default="exit")
    
    if index.lower() == "exit":
        show_message("Operation cancelled", "yellow")
        return
    
    if not index.isdigit():
        show_message("Invalid index - must be a number", "red")
        return
    
    idx = int(index) - 1
    if idx < 0 or idx >= len(config.users):
        show_message("Index out of range", "red")
        return
    
    user = config.users[idx]
    
    clear_screen()
    banner()
    console.print()
    print_centered(f"Remove {user.username}?", "bold yellow")
    console.print()
    
    if centered_confirm("Confirm removal?"):
        config.users.pop(idx)
        if save_config(config):
            show_message(f"✓ Removed {user.username}", "green")
        else:
            show_message("Failed to save configuration", "red")
    else:
        show_message("Removal cancelled", "yellow")

def edit_user(config: Config):
    """Edit an existing user."""
    logger.info("Editing user")
    
    if not config.users:
        show_message("No users to edit", "yellow")
        return
    
    clear_screen()
    banner()
    console.print()
    print_centered("Edit User", "bold cyan")
    console.print()
    display_users(config.users)
    console.print()
    
    print_centered("Enter 'exit' to cancel", "dim")
    console.print()
    
    index = centered_prompt("Enter the index of the user to edit:", default="exit")
    
    if index.lower() == "exit":
        show_message("Operation cancelled", "yellow")
        return
    
    if not index.isdigit():
        show_message("Invalid index - must be a number", "red")
        return
    
    idx = int(index) - 1
    if idx < 0 or idx >= len(config.users):
        show_message("Index out of range", "red")
        return
    
    user = config.users[idx]
    
    clear_screen()
    banner()
    console.print()
    print_centered(f"Editing: {user.username}", "bold cyan")
    print_centered("Press Enter to keep current value", "dim")
    console.print()
    
    # Edit identifier
    new_identifier = centered_prompt(
        "User ID/Username:",
        default=user.user_id or user.username
    )
    if new_identifier.lower() == "exit":
        show_message("Edit cancelled", "yellow")
        return
    
    console.print()
    
    # Edit server
    new_server = centered_prompt(
        "Server Link/Place ID:",
        default=user.server or "none"
    )
    if new_server.lower() == "exit":
        show_message("Edit cancelled", "yellow")
        return
    if new_server.lower() == "none":
        new_server = ""
    
    if not validate_server_input(new_server):
        show_message("Invalid server format", "red")
        return
    
    console.print()
    
    # Edit client/package name
    new_client = centered_prompt(
        "Package Name:",
        default=user.client_name
    )
    if new_client.lower() == "exit":
        show_message("Edit cancelled", "yellow")
        return
    
    show_message("Fetching user info...", "yellow", 1)
    
    updated = User(
        username="" if new_identifier.isdigit() else new_identifier,
        user_id=new_identifier if new_identifier.isdigit() else "",
        server=new_server,
        client_name=new_client,
    )
    
    updated.update_user_info()
    
    if not updated.user_id or not updated.username:
        show_message("Failed to fetch user info from Roblox", "red")
        return
    
    config.users[idx] = updated
    
    if save_config(config):
        show_message(f"✓ Updated {updated.username}", "green")
    else:
        show_message("Failed to save configuration", "red")

# --- CONFIGURATION OPTIONS ---

def set_interval(config: Config, interval_type: str):
    """Set check interval."""
    logger.info(f"Setting {interval_type} interval")
    
    clear_screen()
    banner()
    console.print()
    print_centered(f"{interval_type} Interval", "bold cyan")
    console.print()
    
    current = (config.check_interval if interval_type == "Check" 
               else config.double_check_interval)
    
    print_centered(f"Current: {current}s", "blue")
    console.print()
    print_centered("Enter 'exit' to cancel", "dim")
    console.print()
    
    value = centered_prompt("New interval in seconds:", default="exit")
    
    if value.lower() == "exit":
        show_message("Operation cancelled", "yellow")
        return
    
    if not value.isdigit() or int(value) < 5:
        show_message("Must be a number ≥ 5", "red")
        return
    
    if interval_type == "Check":
        config.check_interval = int(value)
    else:
        config.double_check_interval = int(value)
    
    if save_config(config):
        show_message(f"✓ Updated to {value}s", "green")
    else:
        show_message("Failed to save configuration", "red")

def set_double_check_count(config: Config):
    """Set double check count."""
    logger.info("Setting double check count")
    
    clear_screen()
    banner()
    console.print()
    print_centered("Double Check Count", "bold cyan")
    console.print()
    print_centered(f"Current: {config.double_check_count}", "blue")
    console.print()
    print_centered("Enter 'exit' to cancel", "dim")
    console.print()
    
    value = centered_prompt("New count:", default="exit")
    
    if value.lower() == "exit":
        show_message("Operation cancelled", "yellow")
        return
    
    if not value.isdigit() or int(value) < 1:
        show_message("Must be a positive number", "red")
        return
    
    config.double_check_count = int(value)
    
    if save_config(config):
        show_message(f"✓ Updated to {value}", "green")
    else:
        show_message("Failed to save configuration", "red")

def set_launch_timeout(config: Config):
    """Set launch timeout."""
    logger.info("Setting launch timeout")
    
    clear_screen()
    banner()
    console.print()
    print_centered("Launch Timeout", "bold cyan")
    console.print()
    print_centered(f"Current: {config.launch_timeout}s", "blue")
    console.print()
    print_centered("Enter 'exit' to cancel", "dim")
    console.print()
    
    value = centered_prompt("New timeout in seconds:", default="exit")
    
    if value.lower() == "exit":
        show_message("Operation cancelled", "yellow")
        return
    
    if not value.isdigit() or int(value) < 10:
        show_message("Must be a number ≥ 10", "red")
        return
    
    config.launch_timeout = int(value)
    
    if save_config(config):
        show_message(f"✓ Updated to {value}s", "green")
    else:
        show_message("Failed to save configuration", "red")

def set_webhook_url(config: Config):
    """Set Discord webhook URL."""
    logger.info("Setting webhook URL")
    
    clear_screen()
    banner()
    console.print()
    print_centered("Webhook URL", "bold cyan")
    console.print()
    
    if config.webhook_url:
        truncated = (config.webhook_url[:50] + "...") if len(config.webhook_url) > 50 else config.webhook_url
        print_centered(f"Current: {truncated}", "blue")
        console.print()
    
    print_centered("Enter 'exit' to cancel", "dim")
    console.print()
    
    url = centered_prompt("Enter Discord webhook URL:", default="exit")
    
    if url.lower() == "exit":
        show_message("Operation cancelled", "yellow")
        return
    
    # Test webhook
    show_message("Testing webhook...", "yellow", 1)
    
    try:
        webhook = DiscordWebhook(url=url)
        embed = DiscordEmbed(
            title="Monke Tool Test",
            description="✅ Webhook configured successfully!",
            color="03b2f8"
        )
        webhook.add_embed(embed)
        response = webhook.execute()
        
        if response and response.status_code == 200:
            config.webhook_url = url
            if save_config(config):
                show_message("✓ Webhook configured and tested!", "green")
            else:
                show_message("Failed to save configuration", "red")
        else:
            show_message("Webhook test failed - check URL", "red")
            
    except Exception as e:
        logger.error(f"Webhook test failed: {e}")
        show_message(f"Invalid webhook URL: {e}", "red")

def set_webhook_interval(config: Config):
    """Set webhook interval."""
    logger.info("Setting webhook interval")
    
    clear_screen()
    banner()
    console.print()
    print_centered("Webhook Interval", "bold cyan")
    console.print()
    print_centered(f"Current: {config.webhook_interval}s", "blue")
    console.print()
    print_centered("Enter 'exit' to cancel", "dim")
    console.print()
    
    value = centered_prompt("New interval in seconds:", default="exit")
    
    if value.lower() == "exit":
        show_message("Operation cancelled", "yellow")
        return
    
    if not value.isdigit() or int(value) < 30:
        show_message("Must be a number ≥ 30", "red")
        return
    
    config.webhook_interval = int(value)
    
    if save_config(config):
        show_message(f"✓ Updated to {value}s", "green")
    else:
        show_message("Failed to save configuration", "red")

def set_max_failures(config: Config):
    """Set maximum consecutive failures."""
    logger.info("Setting max failures")
    
    clear_screen()
    banner()
    console.print()
    print_centered("Max Failures", "bold cyan")
    console.print()
    print_centered(f"Current: {config.max_failures}", "blue")
    console.print()
    print_centered("Enter 'exit' to cancel", "dim")
    console.print()
    
    value = centered_prompt("New max failures:", default="exit")
    
    if value.lower() == "exit":
        show_message("Operation cancelled", "yellow")
        return
    
    if not value.isdigit() or int(value) < 1:
        show_message("Must be a positive number", "red")
        return
    
    config.max_failures = int(value)
    
    if save_config(config):
        show_message(f"✓ Updated to {value}", "green")
    else:
        show_message("Failed to save configuration", "red")

# --- STATUS FUNCTIONS ---

def update_user_status(config: Config, user_id: str, status: str):
    """Thread-safe status update."""
    global _last_webhook_time
    
    with _status_lock:
        old_status = user_statuses.get(user_id)
        user_statuses[user_id] = status
        
        # Reset failures on success
        if status == "Online":
            user_failures[user_id] = 0
        
        # Log status change
        if old_status != status:
            logger.info(f"Status change: {user_id} -> {status}")
            
            # Send webhook for status change (excluding Initializing)
            if config.webhook_url and status not in ["Initializing"]:
                current_time = time.time()
                # Send immediately for status changes, don't wait for interval
                _last_webhook_time = current_time
                threading.Thread(
                    target=send_webhook,
                    args=(config,),
                    name=f"Webhook-StatusChange"
                ).start()

def reset_user_status(user_id: str):
    """Reset user status."""
    with _status_lock:
        user_statuses[user_id] = "Offline"
        user_failures[user_id] = 0

def get_user_status(user_id: str) -> str:
    """Thread-safe status retrieval."""
    with _status_lock:
        return user_statuses.get(user_id, "Unknown")

def increment_failure(user_id: str) -> int:
    """Increment failure count."""
    with _status_lock:
        current_failures = user_failures.get(user_id, 0)
        user_failures[user_id] = current_failures + 1
        return user_failures[user_id]

# --- PRESENCE CHECKING ---

def check_user_presence(user: User, config: Config) -> bool:
    """Check if user is online with retry logic."""
    logger.debug(f"Checking presence for {user.username}")
    
    url = "https://presence.roblox.com/v1/presence/users"
    headers = {"Content-Type": "application/json"}
    payload = {"userIds": [int(user.user_id)]}
    
    for attempt in range(3):
        try:
            response = http_session().post(
                url, headers=headers, json=payload, timeout=10
            )
            response.raise_for_status()
            data = response.json()
            
            presence = data["userPresences"][0]
            location = presence.get("lastLocation", "Website")
            
            if location != "Website":
                logger.info(f"{user.username} is online")
                return True
            
            # Double check if offline
            if attempt == 0:
                logger.debug(f"{user.username} offline, double-checking...")
                for check in range(config.double_check_count):
                    time.sleep(config.double_check_interval)
                    
                    response = http_session().post(
                        url, headers=headers, json=payload, timeout=10
                    )
                    response.raise_for_status()
                    data = response.json()
                    
                    if data["userPresences"][0]["lastLocation"] != "Website":
                        logger.info(f"{user.username} online after double-check")
                        return True
                    
                    logger.debug(f"Double-check {check + 1}: still offline")
            
            return False
            
        except requests.exceptions.Timeout:
            logger.warning(f"Timeout checking {user.username}, attempt {attempt + 1}")
            if attempt < 2:
                time.sleep(RATE_LIMIT_DELAY)
        except requests.exceptions.RequestException as e:
            logger.error(f"API error for {user.username}: {e}")
            if attempt < 2:
                time.sleep(RATE_LIMIT_DELAY)
    
    return False

def check_user_presence_launch(user: User, config: Config, delay: int = 2) -> bool:
    """Check presence after launch."""
    logger.debug(f"Post-launch check for {user.username}")
    
    url = "https://presence.roblox.com/v1/presence/users"
    headers = {"Content-Type": "application/json"}
    payload = {"userIds": [int(user.user_id)]}
    
    try:
        response = http_session().post(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data["userPresences"][0]["lastLocation"] != "Website":
            if get_user_status(user.user_id) != "Online":
                update_user_status(config, user.user_id, "Online")
                logger.info(f"{user.username} online after launch")
                time.sleep(delay)
            return True
        
        return False
        
    except requests.RequestException as e:
        logger.error(f"Post-launch check error for {user.username}: {e}")
        return True  # Assume online to prevent loops

def check_process_running(client_name: str) -> bool:
    """Check if Roblox process is running with proper PIPE handling."""
    logger.debug(f"Checking process: {client_name}")
    
    try:
        result = subprocess.run(
            f"{shell_prefix()} 'pidof {resolve_package(client_name)}'",
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            timeout=5
        )
        
        return result.returncode == 0 and result.stdout.strip()
        
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout checking {client_name}")
        return True  # Assume running
    except Exception as e:
        logger.error(f"Error checking process {client_name}: {e}")
        return True

# --- ROBLOX LAUNCHING ---

def execute_command(command: str, description: str) -> bool:
    """Execute a shell command with error handling and proper PIPE."""
    try:
        logger.debug(f"Executing: {description}")
        result = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            timeout=30
        )
        
        if result.returncode != 0:
            stderr = result.stderr.decode().strip() if result.stderr else "Unknown error"
            logger.error(f"Command failed ({description}): {stderr}")
            return False
        
        return True
        
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout executing: {description}")
        return False
    except Exception as e:
        logger.error(f"Error executing {description}: {e}")
        return False

def restart_roblox(client_name: str, user: User, users: List[User],
                   server: str, action: str, config: Config):
    """Force restart Roblox."""
    logger.info(f"{action} Roblox for {user.username}")
    
    if not server:
        logger.warning(f"No server for {user.username}")
        return
    
    try:
        update_user_status(config, user.user_id, action)
        
        # Switch to different client first
        current_idx = users.index(user)
        next_idx = (current_idx + 1) % len(users)
        next_package = resolve_package(users[next_idx].client_name)
        package = resolve_package(client_name)

        execute_command(
            f"{shell_prefix()} 'am start -a android.intent.action.MAIN {next_package}'",
            "switch app"
        )

        time.sleep(5)

        # Force restart target (VIEW action required or Roblox ignores the deep link)
        if server.isdigit():
            join_cmd = f"{shell_prefix()} 'am start -S -a android.intent.action.VIEW -n {package}/{ROBLOX_ACTIVITY} -d roblox://placeID={server}'"
        else:
            join_cmd = f"{shell_prefix()} 'am start -S -a android.intent.action.VIEW -n {package}/{ROBLOX_ACTIVITY} -d \"{server}\"'"
        
        if not execute_command(join_cmd, "restart app"):
            return
        
        user.start_time = time.time()
        
        # Wait for presence
        start = time.time()
        while time.time() - start < config.launch_timeout:
            if check_user_presence_launch(user, config):
                return
            time.sleep(config.launch_timeout // 4)
        
        logger.warning(f"Restart timeout for {user.username}")
        update_user_status(config, user.user_id, "Timeout")
        
    except Exception as e:
        logger.error(f"Restart error for {user.username}: {e}")
        update_user_status(config, user.user_id, "Error")

# --- MONITORING ---

def monitor_user(user: User, config: Config):
    """Main monitoring loop for a user."""
    thread_name = f"Monitor-{user.username}"
    threading.current_thread().name = thread_name
    
    logger.info(f"Starting monitor for {user.username}")
    
    while _monitoring_active:
        try:
            if not user.server:
                logger.debug(f"{user.username} has no server, sleeping")
                _stop_event.wait(config.check_interval)
                continue
            
            process_running = check_process_running(user.client_name)
            
            if not process_running:
                # Process is dead: restart immediately, no presence re-check.
                logger.info(f"{user.username} process not running, restarting")
                restart_roblox(user.client_name, user, config.users,
                               user.server, "Restarting", config)
            else:
                online = check_user_presence(user, config)
                
                if not online:
                    failures = increment_failure(user.user_id)
                    logger.info(f"{user.username} offline (failure {failures}/{config.max_failures})")
                    
                    if failures > config.max_failures:
                        logger.error(f"{user.username} exceeded max failures ({failures})")
                        update_user_status(config, user.user_id, "Failed")
                        _stop_event.wait(config.check_interval * 2)
                        continue
                    
                    # Process is up but user is offline: force restart.
                    restart_roblox(user.client_name, user, config.users,
                                   user.server, "Restarting", config)
                else:
                    # User is online - reset failures
                    with _status_lock:
                        user_failures[user.user_id] = 0
                    
                    if get_user_status(user.user_id) != "Online":
                        update_user_status(config, user.user_id, "Online")
            
            user.last_check = time.time()
            _stop_event.wait(config.check_interval)
            
        except Exception as e:
            logger.exception(f"Error monitoring {user.username}: {e}")
            update_user_status(config, user.user_id, "Error")
            _stop_event.wait(60)

def initial_launch(user: User, config: Config):
    """Initial launch when monitoring starts."""
    logger.info(f"Initial launch for {user.username}")
    
    try:
        if not user.server:
            logger.warning(f"{user.username} has no server")
            return
        
        update_user_status(config, user.user_id, "Initializing")
        
        process_running = check_process_running(user.client_name)
        
        if not process_running:
            logger.info(f"{user.username} process not running, restarting")
            restart_roblox(user.client_name, user, config.users,
                           user.server, "Restarting", config)
            
        elif not check_user_presence(user, config):
            logger.info(f"{user.username} offline but process running, restarting")
            restart_roblox(user.client_name, user, config.users,
                           user.server, "Restarting", config)
            
        else:
            logger.info(f"{user.username} already online")
            with _status_lock:
                user_failures[user.user_id] = 0
            if get_user_status(user.user_id) != "Online":
                update_user_status(config, user.user_id, "Online")
            time.sleep(5)
            
    except Exception as e:
        logger.error(f"Initial launch error for {user.username}: {e}")
        update_user_status(config, user.user_id, "Error")

def monitor_all_users(config: Config):
    """Start monitoring all users."""
    global _monitoring_active
    _monitoring_active = True
    _stop_event.clear()
    
    logger.info("Starting monitoring for all users")
    
    if not config.users:
        show_message("No users configured", "yellow")
        return
    
    show_message("Starting Monitoring", "bold blue", 1)

    # Kill all configured Roblox clients first so monitoring starts clean
    killed = set()
    for user in config.users:
        package = resolve_package(user.client_name)
        if package and package not in killed:
            killed.add(package)
            execute_command(
                f"{shell_prefix()} 'kill $(pidof {package}) 2>/dev/null'",
                f"kill {package}"
            )
    if killed:
        time.sleep(2)

    # Initial launches
    for user in config.users:
        initial_launch(user, config)
        time.sleep(5)
    
    # Start monitoring threads
    threads = []
    for user in config.users:
        thread = threading.Thread(
            target=monitor_user,
            args=(user, config),
            name=f"Monitor-{user.username}"
        )
        thread.daemon = True
        thread.start()
        threads.append(thread)
        time.sleep(2)
    
    # Start periodic webhook thread
    if config.webhook_url:
        webhook_thread = threading.Thread(
            target=periodic_webhook_thread,
            args=(config,),
            name="PeriodicWebhook"
        )
        webhook_thread.daemon = True
        webhook_thread.start()
    
    show_message("✓ Monitoring started - Press Ctrl+C to stop", "green", 1)
    
    # Status display thread
    def status_display():
        while _monitoring_active:
            clear_screen()
            banner()
            console.print()
            display_users(config.users)
            console.print()
            print_centered(f"Uptime: {get_uptime()} | Monitoring active - Press Ctrl+C to stop", "dim")
            console.print()
            # Redrawing the full Rich table is relatively heavy on Termux.
            _stop_event.wait(15)
    
    display_thread = threading.Thread(target=status_display, name="Display")
    display_thread.daemon = True
    display_thread.start()
    
    # Wait for interrupt - block on an event instead of polling every second
    try:
        _stop_event.wait()
    except KeyboardInterrupt:
        logger.info("Monitoring stopped by user")
        _monitoring_active = False
        _stop_event.set()
        show_message("Monitoring stopped", "yellow")

# --- WEBHOOK FUNCTIONS ---

def take_screenshot() -> Optional[Path]:
    """Take a screenshot using screencap."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            screenshot = Path(f.name)
        
        result = subprocess.run(
            f"{shell_prefix()} 'screencap {screenshot}'",
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            timeout=30
        )
        
        if result.returncode == 0 and screenshot.exists():
            return screenshot
        
        if screenshot.exists():
            screenshot.unlink()
        return None
        
    except Exception as e:
        logger.error(f"Screenshot failed: {e}")
        return None

def send_webhook(config: Config):
    """Send status update to Discord."""
    if not config.webhook_url:
        return
    
    try:
        webhook = DiscordWebhook(url=config.webhook_url)
        
        # Create embed
        embed = DiscordEmbed(
            title="Monke Tool Status Update",
            color="03b2f8",
            timestamp=datetime.now().isoformat()
        )
        
        # Build status table
        status_lines = []
        if config.users:
            max_len = max(len(u.username or "Unknown") for u in config.users) + 2
            
            for user in config.users:
                status = get_user_status(user.user_id)
                emoji = {
                    "Online": "🟢",
                    "Offline": "🔴",
                    "Error": "🔴",
                    "Launching": "🟡",
                    "Relaunching": "🟡",
                    "Restarting": "🟡",
                    "Checking": "🟡",
                    "Initializing": "🟡",
                    "Timeout": "🔴",
                    "Failed": "🔴"
                }.get(status, "⚪")
                
                padded = f"{user.username:<{max_len}}"
                status_lines.append(f"{emoji} **{padded}**: {status}")
        
        if status_lines:
            embed.add_embed_field(
                name="User Status",
                value="\n".join(status_lines),
                inline=False
            )
        else:
            embed.add_embed_field(
                name="User Status",
                value="No users configured",
                inline=False
            )
        
        # Add metadata
        embed.add_embed_field(
            name="System",
            value=f"⏱️ **Uptime:** {get_uptime()}",
            inline=True
        )
        
        embed.add_embed_field(
            name="Users",
            value=f"👥 {len(config.users)}",
            inline=True
        )
        
        webhook.add_embed(embed)
        
        # Try to add screenshot
        screenshot = take_screenshot()
        if screenshot:
            try:
                embed.set_image(url="attachment://screenshot.png")
                with open(screenshot, "rb") as f:
                    webhook.add_file(file=f.read(), filename="screenshot.png")
            finally:
                screenshot.unlink()
        
        # Send
        response = webhook.execute()
        
        if response and response.status_code == 200:
            logger.info("Webhook sent successfully")
        else:
            logger.warning(f"Webhook failed: {response.status_code if response else 'No response'}")
            
    except Exception as e:
        logger.error(f"Webhook failed: {e}")

def periodic_webhook_thread(config: Config):
    """Periodic webhook thread that sends updates at configured intervals."""
    logger.info("Periodic webhook thread started")
    if _stop_event.wait(config.webhook_interval):  # Wait for first interval
        return
    
    while _monitoring_active:
        try:
            send_webhook(config)
        except Exception as e:
            logger.error(f"Periodic webhook thread error: {e}")
        # One blocking wait per interval instead of waking every second
        if _stop_event.wait(config.webhook_interval):
            break

# --- DISPLAY FUNCTIONS ---

def banner():
    """Display the main banner."""
    console.print(Align.center(Panel(ASCII_ART, box=box.ROUNDED)))

def display_users(users: List[User]):
    """Display users in a formatted table."""
    if not users:
        print_centered("No users configured", "yellow")
        return
    
    table = Table(box=box.ROUNDED, width=console.width - 4)
    table.add_column("Index", style="cyan", justify="center")
    table.add_column("Username", style="cyan")
    table.add_column("Client", style="cyan")
    table.add_column("Status", style="cyan", justify="center")
    
    for i, user in enumerate(users, 1):
        status = get_user_status(user.user_id)
        status_colors = {
            "Online": "green",
            "Offline": "red",
            "Error": "red",
            "Timeout": "red",
            "Failed": "red",
            "Launching": "yellow",
            "Relaunching": "yellow",
            "Restarting": "yellow",
            "Checking": "yellow",
            "Initializing": "yellow"
        }
        color = status_colors.get(status, "white")
        
        table.add_row(
            str(i),
            user.username or "N/A",
            user.client_name or "N/A",
            f"[{color}]{status}[/{color}]"
        )
    
    console.print(Align.center(table))

def display_menu():
    """Display main menu."""
    menu_items = [
        "[1] Auto Setup",
        "[2] Start Monitoring",
        "[3] Add User",
        "[4] Remove User",
        "[5] Edit User",
        "[6] Configuration",
        "[7] Exit"
    ]
    
    panel = Panel(
        Align.center("\n".join(menu_items)),
        title="Main Menu",
        width=console.width - 4
    )
    console.print(Align.center(panel))

def display_config_menu(config: Config):
    """Display configuration menu."""
    items = [
        f"[1] Check Interval          [cyan]{config.check_interval}s[/cyan]",
        f"[2] Double Check Interval   [cyan]{config.double_check_interval}s[/cyan]",
        f"[3] Double Check Count      [cyan]{config.double_check_count}[/cyan]",
        f"[4] Launch Timeout          [cyan]{config.launch_timeout}s[/cyan]",
        f"[5] Webhook URL             [cyan]{'Set' if config.webhook_url else 'None'}[/cyan]",
        f"[6] Webhook Interval        [cyan]{config.webhook_interval}s[/cyan]",
        f"[7] Max Failures            [cyan]{config.max_failures}[/cyan]",
        "[8] Back to Main Menu"
    ]
    
    panel = Panel(
        Align.center("\n".join(items)),
        title="Configuration",
        width=console.width - 4
    )
    console.print(Align.center(panel))

# --- MENU FUNCTIONS ---

def configuration_menu(config: Config):
    """Configuration submenu."""
    while True:
        clear_screen()
        banner()
        console.print()
        display_config_menu(config)
        
        choice = centered_prompt("Choose option:", default="8")
        
        if choice == "1":
            set_interval(config, "Check")
        elif choice == "2":
            set_interval(config, "Double Check")
        elif choice == "3":
            set_double_check_count(config)
        elif choice == "4":
            set_launch_timeout(config)
        elif choice == "5":
            set_webhook_url(config)
        elif choice == "6":
            set_webhook_interval(config)
        elif choice == "7":
            set_max_failures(config)
        elif choice == "8" or choice.lower() == "back":
            break
        elif choice.lower() == "exit":
            sys.exit(0)
        else:
            show_message("Invalid option", "red")

def main_menu():
    """Main menu loop."""
    config = load_config()
    
    while True:
        clear_screen()
        banner()
        console.print()
        display_users(config.users)
        console.print()
        display_menu()
        
        choice = centered_prompt("Choose option:", default="7")
        
        if choice == "1":
            auto_setup(config)
            save_config(config)
        elif choice == "2":
            if config.users:
                monitor_all_users(config)
            else:
                show_message("No users configured", "yellow")
        elif choice == "3":
            add_user(config)
        elif choice == "4":
            remove_user(config)
        elif choice == "5":
            edit_user(config)
        elif choice == "6":
            configuration_menu(config)
            save_config(config)
        elif choice == "7" or choice.lower() == "exit":
            clear_screen()
            banner()
            console.print()
            print_centered("Exit Monke Tool?", "bold yellow")
            console.print()
            
            if centered_confirm("Confirm exit?"):
                logger.info("Program terminated by user")
                clear_screen()
                banner()
                console.print()
                print_centered(f"Uptime: {get_uptime()} | Goodbye! 👋", "bold green")
                console.print()
                sys.exit(0)
        else:
            show_message("Invalid option", "red")

# --- MAIN ---

def main():
    """Main entry point."""
    global _start_time
    _start_time = time.time()  # Record start time
    
    try:
        logger.info("Monke Tool started")
        main_menu()
        
    except KeyboardInterrupt:
        logger.info("Program interrupted")
        clear_screen()
        banner()
        console.print()
        print_centered(f"Uptime: {get_uptime()} | Goodbye! 👋", "bold yellow")
        console.print()
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        clear_screen()
        banner()
        console.print()
        print_centered(f"Fatal error: {e}", "bold red")
        console.print()
        sys.exit(1)

if __name__ == "__main__":
    main()