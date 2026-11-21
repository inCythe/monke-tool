# 🐒 Monke Tool

**Roblox Auto-Rejoin Tool for Android (Termux)**

Monke Tool monitors your Roblox accounts and automatically rejoins them into their configured game servers if they disconnect or crash. Built for multi-instance setups — works with normal `com.roblox.*` packages **and renamed clones** (e.g. `free.nokaA`, `free.nokaB`).

---

## ✨ Features

- 🔄 **Auto rejoin** — detects disconnects/crashes and relaunches Roblox automatically
- 👥 **Multi-account** — monitor multiple Roblox instances at once
- 📦 **Clone support** — works with renamed Roblox packages, not just `com.roblox.client`
- 📊 **Live status dashboard** — real-time user status display
- 📨 **Discord webhooks** — optional status updates + screenshots
- ⚙️ **Configurable** — check intervals, launch timeouts, failure limits
- 💾 **Auto backup** — config backups before changes

---

## 📦 Requirements

- **Android** phone/tablet
- **Termux** (F-Droid version recommended)
- **Root access** (`su`) — required for launching apps and reading app data
- Roblox logged in on the device

---

## 🚀 Quick Install (One-Liner)

```bash
curl -fsSL "https://raw.githubusercontent.com/USERNAME/REPO/main/install.sh" | bash
```

The installer will:
1. Update Termux packages
2. Install Python + pip
3. Install dependencies (`requests`, `discord-webhook`, `rich`)
4. Download `monke_tool.py`
5. Launch the tool

---

## 🛠 Manual Install

### 1. Install Termux packages

```bash
yes | pkg update -y && yes | pkg upgrade -y && yes | termux-setup-storage && pkg install python python-pip && pip install requests discord-webhook rich
```

### 2. Download the tool

```bash
curl -fsSL "https://raw.githubusercontent.com/USERNAME/REPO/main/monke_tool.py" -o monke_tool.py
```

### 3. Run it

```bash
python monke_tool.py
```

---

## 📖 Usage

### Menu Options

| # | Option | Description |
|---|--------|-------------|
| 1 | **Auto Setup** | Scans the device for installed Roblox packages (including renamed clones like `free.nokaA`) and auto-configures logged-in users |
| 2 | **Start Monitoring** | Begins the auto-rejoin monitoring loop |
| 3 | **Add User** | Manually add a user — enter the **full package name** (e.g. `com.roblox.client`, `free.nokaA`) |
| 4 | **Remove User** | Remove a configured user |
| 5 | **Edit User** | Edit user ID, server, or package name |
| 6 | **Configuration** | Intervals, webhook, timeouts, failure limits |
| 7 | **Exit** | Quit |

### Adding Users

- **Auto Setup** is the easiest way — it finds all Roblox packages on the device and reads the logged-in account from each app's storage.
- **Manual**: enter the Roblox username or user ID, the server link/place ID (`none` to skip), and the **full package name**.

### Package Names

The tool accepts either format:

- Full package name: `com.roblox.client`, `free.nokaA`, `free.nokaB`
- Legacy short name: `clientu` → resolves to `com.roblox.clientu`

---

## ⚙️ Configuration

Settings are saved to `config.json` in the script directory (auto-created).

| Setting | Default | Description |
|---------|---------|-------------|
| Check Interval | 300s | How often to check user status |
| Double Check Interval | 60s | Delay between offline re-checks |
| Double Check Count | 2 | Number of re-checks before declaring offline |
| Launch Timeout | 120s | Max wait for a user to come online after launch |
| Webhook Interval | 600s | Discord status update frequency |
| Max Failures | 5 | Consecutive failures before giving up |

### Discord Webhook (optional)

Set a webhook URL in **Configuration → Webhook URL** to get status updates and screenshots in Discord.

---

## 📝 Notes

- Logs are written to `monke.log` (rotates automatically)
- Config backups are stored in `backups/`
- Press `Ctrl+C` to stop monitoring
- The tool uses `su` for app launching — make sure root works in Termux first (`su` should give a root shell)

---

## 📄 License

MIT
