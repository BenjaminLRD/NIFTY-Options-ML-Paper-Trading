# Windows launchers

Two small programs so you can start and stop the dashboard without typing commands. **Simulation only**: they run `python run_ml_demo.py` on your own PC, in Synthetic Mode with invented data. They never contact a broker, a data provider or the internet (they only talk to `127.0.0.1`).

| Program | What it does |
|---|---|
| `NIFTY_PaperTrading.exe` | Starts the server hidden, waits until it answers, then opens <http://127.0.0.1:8765/ml>. The main dashboard (`/`) is on the same server. |
| `Stop_Dashboard.exe` | Stops that server (asks it to quit like Ctrl+C; force-closes only if that fails). |

## Use
1. Double-click `NIFTY_PaperTrading.exe`. A small "Starting the dashboard" box shows for a second or two, then your browser opens.
2. Double-click it again at any time: it detects the running server and just opens the browser; no second server starts.
3. Double-click `Stop_Dashboard.exe` when finished. A box confirms the stop.

Closing the browser tab does not stop the server; use `Stop_Dashboard.exe`. Saved replays and the virtual account stay in `application\data\` exactly as when you run the commands by hand.

## Requirements
- Windows 10/11 (uses the .NET Framework 4 that ships with Windows; nothing to install).
- Python 3.12 or newer on the PATH (from python.org, "Add python.exe to PATH" ticked).

## Build (and rebuild)
No installs and no internet: the programs are compiled with `csc.exe`, the C# compiler already inside Windows. PyInstaller is not used, so no Python runtime is bundled.

```powershell
cd C:\path\to\NIFTY-Options-ML-Paper-Trading\launcher
powershell -ExecutionPolicy Bypass -File .\build.ps1            # builds launcher\dist\*.exe
powershell -ExecutionPolicy Bypass -File .\build.ps1 -Install  # also copies both to your Desktop
```

Source: `src\Common.cs` (shared helpers), `src\Launcher.cs`, `src\StopDashboard.cs`. `dist\` is git-ignored.

## How it finds the application (no hard-coded paths)
In this order: environment variable `NIFTY_APP_DIR`; a one-line file `app_dir.txt` next to the program; the folders above the program (so `launcher\dist\` works); a folder `NIFTY-Options-ML-Paper-Trading\application` next to the program (so the Desktop works when the project is on the Desktop). If the project lives elsewhere, put the full path of its `application` folder in `app_dir.txt` beside the `.exe` (or run `build.ps1 -Install` from the project, which writes it when needed).

## Behaviour and limits
- **Synthetic Mode**: the launcher passes no mode option, so the application's default (Synthetic) applies. If a server is already running in another mode, the launcher refuses to open it and tells you to stop it first.
- **Duplicates**: it probes `127.0.0.1:8765` first and recognises its own server by the banner in `/api/snapshot`. A second launcher waits for the first to finish.
- **Port 8765 busy** with another program: a message says so; nothing is started or stopped. `Stop_Dashboard.exe` only stops a Python process running this project's `run_ml_demo.py`/`run_dashboard.py`.
- **Not stopped by the launcher**: a server you started by hand in PowerShell is also stopped by `Stop_Dashboard.exe` (same program).
- The launcher always uses the default port 8765 and shows the newest saved replay. To make a new replay use `python run_ml_demo.py --new` by hand.
- Unsigned programs: Windows SmartScreen may warn the first time because they are not code-signed. They are built from the source here.

## Troubleshooting
| Message | Fix |
|---|---|
| "Python was not found" / "Python 3.x found, needs 3.12" | Install Python 3.12+ and tick "Add python.exe to PATH". The Microsoft Store shortcut does not count. |
| "Port 8765 is already used by another program" | Close that program (or restart the PC). |
| "Could not find the application folder" | See the section on finding the application above. |
| "The dashboard did not start" | The box shows the last lines of the server log. |
| Anything else | Logs: `%LOCALAPPDATA%\NIFTY_PaperTrading\launcher.log` and `server.log` (outside the project). |

## Testing hooks
`NIFTY_PaperTrading.exe --check` reports what would happen without starting anything; `--silent` suppresses message boxes and the browser (results go to the log and the exit code: 0 ok, 2 folder not found, 3 Python, 4 port busy, 5 startup failed, 6 not Synthetic). `Stop_Dashboard.exe --silent` likewise.
