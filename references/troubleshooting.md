# Troubleshooting MT5 Headless Backtesting, Discovery & Portable Mode

Measured behaviors, safety constraints, and common error resolutions from production headless runs (MT5 build 6230).

---

## 1. Multi-Install Discovery, Binding & Portable Mode

### Multiple MT5 Installations / Data Folders Detected
- **Behavior**: Running `discover` scans `%APPDATA%\MetaQuotes\Terminal\*` (reading `origin.txt`), `Program Files`, and local `.mt5-portable` folders.
- **Fix**: Ask the user which numbered instance `[N]` to use and run `bind --select <N>` (persisted to `.mt5-headless.json`), or create an isolated project-local copy with `portable --from-select <N>`.

### Portable Instance Cannot Connect / Download Broker History
- **Symptom**: A newly created `.mt5-portable\` fails to log in or reports unknown server / missing symbol history.
- **Causes & Fixes**:
  1. Ensure `MT5_BROKER_SERVER`, `MT5_LOGIN`, and `MT5_PASSWORD` are filled in `.env` (server name must match the broker's server label).
  2. `portable` copies `config\servers.ini` / `brokers.dat` from the source data folder when available; if the source installation did not have that broker's server entry, open the portable terminal once manually (`terminal64.exe /portable`) to scan/add the broker server or copy `config\servers.ini` from the source data folder.

### `.env` Tracked by Git Warning
- **Symptom**: `bind` or `portable` warns `WARNING: .env is currently tracked by Git!`.
- **Cause**: `.env` was committed before `.gitignore` rules were added.
- **Fix**: Remove it from Git's index without deleting the local file: `git rm --cached .env`, then commit the removal.

---

## 2. Critical Safety & Concurrency Rules

### `terminal64.exe is running`
- **Symptom**: `test --run` or wrappers refuse to launch because `terminal64.exe` is running.
- **Cause**: Two terminals cannot safely share the same data folder, and `ShutdownTerminal=1` closes the running instance. (In `/portable` mode, `mt5_headless.py` checks executable paths and only blocks if the *same* portable `terminal64.exe` is already running.)
- **Fix**: Ask the user to close the conflicting MT5 gracefully via UI before running. **NEVER kill `terminal64.exe`**, as force-killing destroys unsaved chart layouts.

### Concurrent Backtest Lock
- **Symptom**: `Another test lock is active at .../runner.lock`.
- **Fix**: Wait for the active backtest to exit cleanly before launching the next test.

### Focus Stealing / Window Popups (Ghost MCP vs Built-in Alternatives)
- **Context**: `mcp__ghost_window` (Ghost MCP) is a **separate, optional external MCP server**. If the user declines to install Ghost MCP, `mt5_headless.py` and the shell wrappers provide built-in Win32 focus-safe alternatives configured via `--launch-mode` (or `.mt5-headless.json` / `MT5_LAUNCH_MODE`):

| Launch Strategy | Requires Ghost MCP? | How It Works | Focus / Taskbar Visibility |
|---|---|---|---|
| `--launch-mode hidden-desktop --run` | **No** (Built-in Python `ctypes`) | Runs `terminal64.exe` inside Win32 `Winsta0\MT5HeadlessDesktop` via `CreateDesktopW` + `CreateProcessW` (falls back to `minimized` if OS policy blocks desktop creation) | **Zero popup, zero taskbar icon, zero focus stealing** |
| `--launch-mode minimized --run` (default) / `--launch-minimized` | **No** (Built-in / CMD / Bash / PS1) | Launches via `SW_SHOWMINNOACTIVE` (`7`) or `start /MIN` / `ProcessWindowStyle.Minimized` | Appears minimized in taskbar; does not steal active window focus |
| `--launch-mode hidden --run` | **No** (Built-in Python) | Launches with `STARTUPINFO.wShowWindow = SW_HIDE` (`0`) | Hides initial window; stays off foreground |
| `--launch-mode ghost` (without `--run`) | **Yes** (External `mcp__ghost_window`) | Agent passes printed `GhostMCP` JSON payload to external Ghost MCP server, then runs `scrub-ini <ini>` after exit | Zero popup / zero focus stealing via external Ghost server |

---

## 3. Compilation Issues (`MetaEditor64.exe`)

| Symptom | Root Cause | Solution |
|---|---|---|
| Exit `1` with no `.log`/`.ex5`, or `0 errors` with no `.ex5` | **Spaces in `.mq5` filename** break MetaEditor CLI parsing | Use `compile` — it stages a space-free copy in `%TEMP%\mql5-compile\` automatically |
| `error 341: cannot open icon` | `#property icon "X.ico"` resolves relative to the `.mq5` file | `compile` copies `#property icon` files into the staging directory |
| `error 106: file ... Trade.mqh not found` | `/inc:` pointed at install folder instead of data folder | Default `--include` uses `<DataDir>\MQL5` |
| `error 135: cannot open "A;B\Include\..."` | `/inc:` accepts only a **single** path | Pass one `--include` root |
| Exit code `1` even when build succeeded | MetaEditor exit codes are unreliable | Check `Result: 0 errors` in `.log` **AND** `Built: True` |

---

## 4. Backtest Execution & Report Issues

| Symptom | Root Cause | Solution |
|---|---|---|
| No `.htm` report generated | Invalid `Expert=` path, spaces in `.ex5`, or `Report=` had path/extension | Ensure `Expert=` is relative to `MQL5\Experts\` using `\`, has no spaces, and `--report` is a bare name. Inspect `<DataDir>\logs\YYYYMMDD.log`. |
| MT5 silently runs wrong EA (e.g., `Moving Average.ex5`) | `Expert=` used `/` forward slashes or pointed to a missing `.ex5` (MT5 logs no error and falls back to last-selected EA) | `mt5_headless.py` automatically normalizes `/` -> `\`, verifies the `.ex5` exists before creating `.ini`, and checks the agent log (`expert file added:`) after `--run`. |
| `0` trades or very few trades | Partial `.ini`/`.set` inputs fell back to defaults, unresolved enum name written as text, or EA hit an expiry guard | Start from a real profile (`--profile`) or full `.set`; pass `--source <EA.mq5>` when overriding enum names; pass a baseline when exporting `.xml` passes (`report ... --profile`); check Tester Agent log for `INIT_FAILED`. |
| `Input '...' not present in base profile/set` | Typo in `Inp*` name or old profile missing new inputs | Run `inspect --source <ea.mq5> --search <term>` or pass `--allow-new`. |
| News Filter has no effect in backtest | `CalendarValueHistory()` fails in Strategy Tester (`-1`, error `4014`) | Expected MT5 behavior; disable news filter during deterministic tests. |
| Test ends early with fewer `Bars` | Account hit **stop-out** (`so` in Deal comment) | Check `report` output for `STOP OUT` warning and drawdown metrics. |
| Deals identical except timestamp on last row | Forced `end of test` close can differ by timestamp | `compare` ignores column 0 (`time`) by default (`IDENTICAL (N rows, time column ignored)`). |
| Closed-source `.ex5` has no `.mq5` for `inspect` | `.ex5` binaries are encrypted/packed | Run `probe --expert <name.ex5> --symbol <sym>` (`Model=2`, 1–2 days) then `inspect --report` / `set --from-report`. |
| MQL5 Market EA fails to load (`cannot load` / activation error) | Market `.ex5` DRM binds to the machine + MQL5 Community account of the original install | Run on the activated installation (`bind --select <N>`) rather than copying `.ex5` to an unactivated `.mt5-portable`, and keep the exact path `Market\<Name>.ex5`. |

---

## 5. VM / VPS / Remote Execution (Session 0) & Windows 11 Python Stub

### Running inside VM / VPS over PowerShell Direct, WinRM, or SSH (Session 0)
- **Symptom 1 (`Exit: 3221225794` / `0xC0000142` in `--launch-mode hidden-desktop`)**: Session 0 (`Service-0x0-*`) cannot bind to `Winsta0\MT5HeadlessDesktop`, causing user32/gdi32 initialization to fail immediately.
- **Symptom 2 (`terminal64.exe` exits `0` after backtest, logs `MDI create failed`, and writes NO `.htm`/`.png` report)**: When launched directly in Session 0, MT5 runs the test engine but fails to create the GUI MDI report frame, so no `.htm` / `.xml` report file is exported.
- **Built-in Fix (`mt5_headless.py`)**: `_get_win32_session_id()` detects Session `0` automatically (`init` prints `Win32 Session : Session 0 (...)`) and `_launch_via_interactive_schtask()` bridges execution into the logged-in user's active desktop session via a temporary one-shot Task Scheduler task running on `--launch-mode hidden-desktop` (zero popup/focus stealing) and cleans up the task afterwards.
- **Requirement on VM/VPS**: An interactive Windows desktop session (e.g., local console auto-logon or VM console session) must be signed in on the target machine for MT5 to export `.htm` / `.xml` reports.

### Windows 11 `WindowsApps\python.exe` Microsoft Store Stub (VM without Python installed)
- **Symptom**: Running `python` returns `"Python was not found; run without arguments to install from the Microsoft Store"`.
- **Built-in Fix**: All wrappers (`Invoke-MT5Headless.ps1`, `mt5-headless.cmd`, `mt5-headless.sh`) automatically ignore `*WindowsApps*` 0-byte stubs, validate real interpreters with `-c "import sys"`, search `<skill>\.python\python.exe` and standard install folders, and `Invoke-MT5Headless.ps1` / `mt5-headless.cmd` automatically bootstrap Python 3.12.8 embeddable into `<skill>\.python\` when no Python is present (or on `--bootstrap-python` / `-BootstrapPython`).
