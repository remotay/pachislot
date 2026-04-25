# Pachislot Controller

Local Windows web controller for an ESP32 + PCA9685 pachislot controller on USB serial.

## Serial Protocol

The serial protocol is intentionally fixed and tiny. The server writes exactly one lowercase ASCII byte per command, with no newline, JSON, wrapper, or multi-character message:

| Key | Action |
| --- | --- |
| `a` | lever |
| `s` | left stop |
| `d` | middle stop |
| `f` | right stop |
| `g` | max bet |

Default local config is at the top of `app.py`:

```python
SERIAL_PORT = "COM3"
BAUD_RATE = 115200
INACTIVITY_TIMEOUT_SECONDS = 60
COMMAND_COOLDOWN_SECONDS = 2
STREAM_PUBLIC_BASE_URL = "http://192.168.1.23:8889"
STREAM_NAME = "pachislot"
OBS_WS_HOST = "127.0.0.1"
OBS_WS_PORT = 4455
OBS_WS_PASSWORD = ""
```

The browser-facing video iframe uses:

```text
{STREAM_PUBLIC_BASE_URL}/{STREAM_NAME}?controls=false&muted=false&autoplay=true&playsInline=true
```

If your LAN or public stream address changes, edit `STREAM_PUBLIC_BASE_URL` near the top of `app.py`, then restart the server.

On hosted environments, you can set these as environment variables instead of editing code.

## First Run

From PowerShell in this folder:

```powershell
cd C:\pcontrol
& "C:\Users\remot\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8000
```

Open:

```text
http://127.0.0.1:8000
```

From another device on the same LAN, open this computer's LAN address, for example:

```text
http://192.168.1.23:8000
```

## Later Runs

```powershell
cd C:\pcontrol
.\start.ps1
```

To stop a background copy of the local server:

```powershell
cd C:\pcontrol
.\stop.ps1
```

If `COM3` shows as unavailable or access denied, close anything else that might have the port open, such as Arduino IDE Serial Monitor, another terminal, or another copy of this app.

## Behavior

- Click **Take Control** before sending commands.
- Only one browser session has control at a time.
- Other users can watch status and events, but buttons are disabled.
- The MediaMTX WebRTC iframe is only rendered for the browser session that currently has control.
- When control is granted, the server asks OBS WebSocket to start streaming.
- When control is released, disconnected, or timed out, the server asks OBS WebSocket to stop streaming if nobody else has control.
- Control is released immediately when the active user clicks **Quit** or disconnects.
- Control is automatically released after 60 seconds without activity.
- Each command has its own 2 second cooldown. For example, `s` then `d` is allowed, while two `s` presses inside 2 seconds are blocked.
- The server enforces all cooldown and exclusive-control rules.
- OBS WebSocket errors are reported in the UI event log and OBS status panel.

## Render Deployment

This repo includes `render.yaml` for Render Blueprint deployment. Render can host the FastAPI/WebSocket browser UI, but a normal Render web service cannot directly access hardware on this Windows PC, including `COM3`, or OBS WebSocket on this PC at `127.0.0.1:4455`. Those local hardware controls are intended for the Windows-local app.

Use Render only for a public web UI / status surface unless you add a separate secure local agent or tunnel back to the Windows machine.

Manual Render setup:

1. Push this project to GitHub.
2. In Render, choose **New** > **Web Service**.
3. Connect the GitHub repo.
4. Use:
   - Runtime: `Python`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
5. Add or edit environment variables:
   - `STREAM_PUBLIC_BASE_URL=http://192.168.1.23:8889`
   - `STREAM_NAME=pachislot`

If your public or LAN stream address changes later, update `STREAM_PUBLIC_BASE_URL` in the Render service environment variables, then redeploy.
