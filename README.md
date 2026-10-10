# NanoAgent

A small remote-control agent for lab machines (A-GUI) and remote assistance.
The agent makes **one outbound WebSocket** to an Azure Web PubSub rendezvous, so
there are no inbound ports and nothing to configure on NAT or firewalls beyond
outbound 443. A controller (your script, the CLI, or Claude) joins the same
group and sends signed commands; results come back in well under a second.

```
 A-GUI / support PC                     Azure                        You
 ┌──────────────┐  wss (outbound)  ┌──────────────┐  wss  ┌──────────────────────┐
 │ nano_agent.py│◄────────────────►│  Web PubSub  │◄─────►│ nano_cli.py          │
 │  screenshot  │                  │  group:      │       │ nano_client (Python) │
 │  mouse/keys  │                  │  lab-a-gui   │       │ claude_driver.py ────┼──► Claude API
 │  PowerShell  │                  └──────▲───────┘       └──────────────────────┘
 └──────┬───────┘                         │ tokens (1 group, 60 min)
        └─────── GET /api/negotiate ──────┴─── Azure Function (Flex Consumption)
```

| File | Runs on | Purpose |
|---|---|---|
| `nano_agent.py`, `nano_proto.py` | A-GUI | The agent. Screenshot, zoom, mouse, keyboard, PowerShell, windows, files |
| `nano_client.py` | your PC | Python library for scripted demos (`click`, `type`, `click_image`, `run`...) |
| `nano_cli.py` | your PC | One-shot commands, a REPL, and `play` for JSON demo scripts |
| `claude_driver.py` | your PC | Claude drives A-GUI via the computer use toolset + PowerShell |
| `function/`, `infra/main.bicep`, `deploy.ps1` | Azure | Web PubSub + negotiate Function |
| `local_relay.py` | anywhere | Web PubSub look-alike for LAN use and testing (no Azure needed) |
| `Install-NanoAgent.ps1` | A-GUI | Installs Python (if missing), venv, agent, optional logon start |
| `Uninstall-NanoAgent.ps1` | A-GUI | Stops the agent, removes the logon task and `C:\NanoAgent` |
| `Remove-NanoAzure.ps1` | your PC | Deletes everything in Azure (one resource group) |
| `NanoAzure.ps1` | your PC | **Menu** for all of the above: status, deploy, test, tear down, rotate, settings |

## Security model

* **Two independent locks.** The negotiate function key gets you a Web PubSub token for
  one group only (`lab-*` / `sup-*`), valid 60 minutes. Every message is also
  **HMAC-SHA256 signed and AES-256-GCM encrypted** end to end with keys derived from a
  32-byte shared secret held only in `agent.json` / `controller.json` (zlib-compressed
  first, which also cuts Web PubSub billing). Azure, and anyone holding a leaked function
  key, sees only `{"kind":"enc","n":…,"c":…}` blobs: no screenshots, commands or output.
  Messages that fail decryption are dropped and logged; commands older than 2 minutes or
  replayed are rejected. The group name is bound into the encryption, so a message can't
  be replayed into another group.
* **Visible and stoppable.** On Windows a red "NanoAgent: …" strip sits at the top of the
  screen (click-through and excluded from screenshots, so it never confuses Claude).
  **Ctrl+Alt+Shift+K** kills the agent instantly.
* **Audited.** Every command is logged to `nano_agent.log` next to the agent.
* `--no-run` / `--no-files` (or `allow_run` / `allow_files` in agent.json) remove PowerShell
  and file transfer for less trusted situations.

Treat `agent.json` and `controller.json` like passwords: anyone with both the function key
and the secret can drive the machine.

## 1. Deploy the rendezvous (once)

Needs the Azure CLI and ideally Azure Functions Core Tools (`func`). The easy way:

```powershell
.\NanoAzure.ps1
```

This opens a menu covering login and subscription choice, status, deploy, testing the
Function and the agent, opening the config folder, tear down (keeping or wiping the
config), rotating secrets, and settings. Resource group, location, tier and group are
remembered in `config\settings.json`. The scripts below do the same things unattended.

```powershell
.\deploy.ps1 -ResourceGroup rg-nanoagent -Location uksouth            # Free tier
.\deploy.ps1 -ResourceGroup rg-nanoagent -Sku Standard_S1 -Group lab-a-gui
```

It deploys `infra/main.bicep` (Web PubSub, storage, managed identity, Flex Consumption
function), publishes `function/`, and writes `config\agent.json` and `config\controller.json`
with a fresh secret.

**Tier choice.** Web PubSub bills outbound traffic in 2 KB units. A 1280×800 JPEG screenshot
is typically 100–250 KB, i.e. roughly 50–125 billed messages. Free (20k messages/day,
20 connections) is enough for trying it out and light demo use. Standard_S1 includes
1M messages/unit/day, which is plenty for daily Claude-driven work; check the pricing
calculator for the current daily unit price. Lower `jpeg_quality` or `max_long_edge` in
agent.json if you want smaller screenshots.

### What lives in Azure, and removing it

Everything is in one resource group. Idle cost is effectively zero: Web PubSub Free has
no charge, the Function scales to zero, and storage holds only a few MB. Nothing passing
through the agent (screenshots, commands, output) is ever stored in Azure; Web PubSub
relays messages and keeps nothing.

| Resource | What it holds | Persists? |
|---|---|---|
| Web PubSub | nothing (live relay only) | the instance, idle |
| Function App + plan | negotiate code | scales to zero |
| Storage account | function code package (a few MB) and the Functions host's key/lock blobs | yes |
| Managed identity + role assignments | lets the Function read its storage without keys | yes |

```powershell
.\Remove-NanoAzure.ps1                  # deletes the resource group (confirm prompt)
.\deploy.ps1                            # later: same URL, key and secret, A-GUI keeps working
.\deploy.ps1 -NewSecrets                # rotate key + secret (redistribute agent.json)
.\Remove-NanoAzure.ps1 -RemoveConfig    # also delete local config\*.json
```

Redeploying reuses `config\controller.json`. With the same resource group name and
subscription, the Function host name is the same and the old function key is set
again, so you can tear Azure down whenever it's idle.

## 2. Install the agent on A-GUI

```powershell
# with agent.json next to the script:
.\Install-NanoAgent.ps1 -AutoStart

# or from GitHub, config fetched from a URL you control (e.g. a short-lived SAS link).
# While the repo is private, GITHUB_TOKEN (fine-grained, Contents: read on this repo) is required
# both to fetch the script and for the script to fetch the agent:
$env:GITHUB_TOKEN = 'github_pat_...'
$env:NANO_CONFIG_URL = 'https://.../agent.json'
$h = @{ Authorization = "Bearer $env:GITHUB_TOKEN"; Accept = 'application/vnd.github.raw' }
& ([scriptblock]::Create((irm https://api.github.com/repos/Don-Paterson/NanoAgent/contents/Install-NanoAgent.ps1 -Headers $h))) -AutoStart
```

Run from a copy of the repo (copied folder, USB, Skillable file drop), the installer uses the
local files and needs no GitHub access at all.

This installs to `C:\NanoAgent` with its own venv, creates `Start-NanoAgent.cmd`,
locks `agent.json` down to the current user, and with `-AutoStart` registers an
interactive logon task (the agent must run in the desktop session to see the screen).

Manual run: `C:\NanoAgent\Start-NanoAgent.cmd`
Remove: `C:\NanoAgent\Uninstall-NanoAgent.ps1` (stops it, removes the task and the folder)

## 3. Drive it

```powershell
pip install -r requirements-controller.txt
copy config\controller.json .

python nano_cli.py info
python nano_cli.py shot -o now.jpg
python nano_cli.py windows
python nano_cli.py place SmartConsole 0 0 1280 760     # pin the window for fixed-coordinate demos
python nano_cli.py click 120 85
python nano_cli.py type "web-srv"
python nano_cli.py key ctrl+s
python nano_cli.py run "Get-Service | ? Name -like 'CP*'"
python nano_cli.py repl
```

All coordinates are **screenshot pixels** (`info` shows the screenshot size; the agent
scales to the real screen). Read coordinates straight off a saved `shot`.

### Scripted demos (fixed environment)

Pin the window with `place`, use fixed coordinates for things that never move, and image
anchors for popups whose timing varies:

```powershell
python nano_cli.py shot -o ref.jpg
python nano_cli.py crop ref.jpg 1040 690 1110 715 -o demos\anchors\publish.png
python nano_cli.py play demos\smartconsole_publish.json --step     # --step pauses between steps
```

Or in Python:

```python
from nano_client import NanoClient
with NanoClient.from_config("controller.json") as a:
    a.wait_for_agent()
    a.move_window("SmartConsole", 0, 0, 1280, 760)
    a.click(1180, 120)
    a.type("web-srv", interval=0.06)        # visible typing for the projector
    a.click_image("demos/anchors/publish.png", timeout=15)
```

### Let Claude drive

```powershell
$env:ANTHROPIC_API_KEY = '...'
python claude_driver.py "In SmartConsole, create host web-srv 10.1.1.50, add it to the Web group and publish"
python claude_driver.py --task-file tasks\ccse-lab3.md --log-dir runs\lab3 --model claude-opus-5-5
```

Claude gets the computer use toolset (`computer_toolset_20260801`) executed by the agent,
plus `run_powershell`, `list_windows` and `focus_window`. The PowerShell tool is the big
accelerator: Claude can use `mgmt_cli`, ssh, logs and services directly instead of
clicking through the GUI for everything. Batches that don't end with a screenshot get a fresh
one attached automatically, saving a round trip per turn. `--log-dir` keeps every
screenshot and the transcript, useful for writing up lab guides. `--confirm` asks you to approve
each action.

## Remote assistance

On the other PC (after `Install-NanoAgent.ps1`, no config needed):

```
Start-NanoAgent.cmd --support --negotiate-url "https://<function-host>/api/negotiate?code=<key>"
```

It prints an 8-digit code (for example `4821-9930`) and exits after 30 idle minutes. The code
selects a private `sup-` group and derives the signing key, so you connect with:

```powershell
python nano_cli.py --support 4821-9930 shot -o user.jpg
python claude_driver.py --support 4821-9930 --confirm "Find out why Outlook won't start"
```

The support code is short by design (it is read out over the phone). It still sits behind
the function key, and the session is visible, killable and time-limited. Get the user's
agreement before connecting, as you would with any remote-support tool.

## Local relay (no Azure)

```powershell
python local_relay.py --port 8765 --key somekey
# prints: negotiate_url: http://localhost:8765/negotiate?code=somekey
```

Put that URL (with the relay host's address) in both configs. Use it on a lab network
or for testing. It is plain `ws://`, so use the Azure path for anything that crosses the internet.

## Agent ops

`ping info screenshot zoom cursor_position left_click right_click middle_click double_click
triple_click left_click_drag mouse_move left_mouse_down left_mouse_up scroll type key hold_key
wait run windows focus_window move_window file_get file_put`

The mouse and keyboard ops use the same names and arguments as Claude's computer use
members, so the driver forwards Claude's calls unchanged. Keys accept xdotool-style names
(`Return`, `ctrl+s`, `alt+Tab`, `Page_Down`, `super`). Typing on Windows uses Unicode
SendInput, so it works for any character and any keyboard layout.

## Tests

```bash
Xvfb :99 -screen 0 1600x1000x24 &  DISPLAY=:99 python tests/test_e2e.py
```

This runs the local relay, the agent and a Tk target window, then checks scaling, clicks,
typing, keys, zoom, run, files, anchors, replay rejection, encryption (a spy with a valid
relay token sees only ciphertext and can't inject; wrong key, wrong group and tampered
messages are rejected; a worst-case screenshot fits the 1 MB frame limit), and the Claude
driver's batch and loop handling against a scripted fake API.
