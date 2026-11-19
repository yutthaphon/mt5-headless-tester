---
name: mt5-headless-tester
description: >-
  Discover all MetaTrader 5 installations/data folders, bind or provision isolated
  project-local /portable MT5 environments with .env credential & Git protection, discover
  MQL5 EA inputs and enum ordinals, generate validated .set and tester .ini files, run
  non-intrusive headless backtests across CMD, Bash, or PowerShell, compare runs
  deal-by-deal, and export reports to Markdown (.md), JSON (.json), or HTML (.html). Use
  whenever the user asks to configure EA inputs, create .set files, set up portable MT5,
  run headless backtests, export reports, or compare EA builds/presets.
---

# MT5 Headless Tester

Automate MetaTrader 5 installation discovery, isolated `/portable` setup, EA parameter discovery, `.set` preset creation, headless `.mq5` compilation, non-intrusive Strategy Tester execution, multi-format report export (`md` / `json` / `html`), and deal-by-deal regression comparison across **CMD**, **Bash**, **PowerShell**, or **Python**.

---

## Shell Entry Points (`cmd`, `bash`, `powershell`, `python`)

Every subcommand works identically across all shells (each wrapper silently loads `.env` into process environment variables before invoking [mt5_headless.py](./scripts/mt5_headless.py)):

| Shell | Wrapper / Command | Minimized Launch Helper |
|---|---|---|
| **Windows CMD** | `scripts\mt5-headless.cmd <cmd> ...` | `scripts\mt5-headless.cmd --launch-minimized <ini> [--portable]` |
| **Bash / Git Bash** | `scripts/mt5-headless.sh <cmd> ...` | `scripts/mt5-headless.sh --launch-minimized <ini> [--portable]` |
| **PowerShell** | `powershell -NoProfile -File scripts\Invoke-MT5Headless.ps1 <cmd> ...` | `powershell -NoProfile -File scripts\Invoke-MT5Headless.ps1 -LaunchMinimized [-Portable] -IniPath <ini>` |
| **Direct Python** | `python scripts\mt5_headless.py <cmd> ...` | `python scripts\mt5_headless.py test ... --run` |

---

## Non-Negotiable Safety, Focus & Credential Rules

1. **Close matching MT5 before running tests — NEVER kill `terminal64.exe`**: Launching `/config:<ini>` sets `ShutdownTerminal=1`, which closes a running terminal using the same data folder. Check `init` first and ask the user to close MT5 gracefully via UI if open. Force-killing `terminal64.exe` destroys chart layouts. (An isolated `/portable` terminal in its own folder can run independently of another installation.)
2. **Run one test per data folder at a time**: Wait for `terminal64.exe` to exit completely before launching the next test or reading `.htm` / `.log` outputs.
3. **Do not steal window focus — Ask the user about Ghost MCP vs built-in launchers**:
   - **Ghost MCP (`mcp__ghost_window`) is an optional, separate external MCP server** — do **not** assume it is installed.
   - Before the first launch, **ask the user**: *"คุณได้ติดตั้ง / ต้องการติดตั้ง Ghost MCP (`mcp__ghost_window`) ไหม หรือต้องการให้รันผ่านตัวเลือกในตัวที่ไม่ต้องใช้ Ghost MCP?"*
   - Choose one of the supported `--launch-mode` strategies (persisted in `.mt5-headless.json` or via `MT5_LAUNCH_MODE`):
     1. **`hidden-desktop`** *(Best built-in alternative — no Ghost MCP needed)*: Python uses Win32 `CreateDesktopW` + `CreateProcessW` (`Winsta0\MT5HeadlessDesktop`) via `ctypes` so MT5 runs completely off-screen with zero taskbar icon and zero focus stealing (auto-falls back to `minimized` if OS policy blocks desktop creation).
     2. **`minimized`** *(Default CLI / shell wrapper `--run` or `--launch-minimized`)*: Launches via Win32 `SW_SHOWMINNOACTIVE` (`start /MIN`) so MT5 starts minimized in the taskbar without stealing active focus.
     3. **`hidden`**: Launches via Win32 `SW_HIDE` (`wShowWindow = 0`), hiding the main window on startup.
     4. **`ghost`** *(Only if external Ghost MCP is installed)*: Run `test` *without* `--run` to generate `<Report>.ini`, then launch the printed `GhostMCP` JSON payload via `mcp__ghost_window` and wait until `terminal64.exe` exits.
4. **Strict `.env` & Credential Secrecy**:
   - **NEVER read `.env`** (never call `read_file`, `cat`, `type`, `Get-Content`, or `grep` on `.env`) and **NEVER** print, echo, or log `MT5_BROKER_SERVER`, `MT5_LOGIN`, or `MT5_PASSWORD`.
   - Ask the user to fill `MT5_BROKER_SERVER`, `MT5_LOGIN`, and `MT5_PASSWORD` into `.env` themselves. The wrappers and `mt5_headless.py` load them via environment variables into `[Common]` at runtime and mask console output (`[SET]`/`[UNSET]`).
   - Always scrub `Password=` from temporary `.ini` files after exit (`--run` and `--launch-minimized` do this automatically; after external Ghost MCP runs, execute `scrub-ini <ini_path>`).
5. **Strict Git Exclusion**:
   - `.env`, `.env.*` (except `!.env.example`), `.mt5-headless.json`, and `.mt5-portable/` are automatically added to `.gitignore` and `.git/info/exclude` by `bind` and `portable`. **NEVER** `git add`, commit, or push those files.
6. **Never write partial inputs from scratch**: Always base new `.set` and `.ini` files on a saved Tester Profile (`--profile`), an existing `.set` (`--from-set`), or full source defaults (`--from-source`).
7. **Respect `--report` naming**: `--report` must be a bare name without path or extension (e.g., `sample_run1`). MT5 writes `<Report>.htm` and `<Report>*.png` directly into the bound data folder root.

---

## Core Workflows & Commands

Examples below use CMD / Bash / Python interchangeably. Override paths anytime via `--project-dir`, `--portable`, `--terminal-dir`, `--data-dir`, `--tester-dir`, `--work-dir` or `.mt5-headless.json`.

### Workflow 00: `install` — Multi-AI Skill & `/mt5-headless` Slash Command Installer

If the user shared a Git link or asked you (any AI assistant: **Antigravity**, **Claude Code**, **OpenAI Codex**, **Cursor**, **Windsurf**, **Cline**, **Roo Code**, **GitHub Copilot**, or **`AGENTS.md`-compatible agents**) to install this skill so it can be invoked via a special slash command (`/mt5-headless`), run `install`:

```cmd
:: Auto-detect installed AI tools on this machine and install Skill + /mt5-headless command
scripts\mt5-headless.cmd install --target auto --scope global

:: Install for ALL supported AI assistants (both User Global and Current Project)
scripts\mt5-headless.cmd install --target all --scope both --project-dir .

:: Clone directly from a Git URL and install across specific AI tools
python scripts\mt5_headless.py install --git-url "https://github.com/yutthaphon/mt5-headless-tester.git" --target auto --scope global
```

Available `--target` values: `auto`, `all`, `antigravity`, `claude`, `codex`, `cursor`, `windsurf`, `cline`, `roo`, `copilot`, `agents`.
Add `--dry-run` to preview destination folders without writing files.

### Workflow 0: `discover`, `bind`, & `portable` — Multi-MT5 Discovery & Isolated Setup

Scan the machine for all MetaTrader 5 installations, `%APPDATA%\MetaQuotes\Terminal\*` data folders (reading `origin.txt` and broker folders), and existing portable runtimes:

```cmd
scripts\mt5-headless.cmd discover
```

**Always ask the user**:
1. Which numbered installation `[N]` to bind, or whether they want an isolated project-local `/portable` environment.
2. Whether they have/want to install **Ghost MCP (`mcp__ghost_window`)** or prefer a built-in launcher (`--launch-mode hidden-desktop`, `minimized`, or `hidden`) when binding/provisioning:

```cmd
:: Option A: Bind project (.mt5-headless.json) to discovered installation #1
:: Uses built-in Win32 hidden desktop (no Ghost MCP required)
scripts\mt5-headless.cmd bind --select 1 --launch-mode hidden-desktop

:: Option B: Provision an isolated project-local .mt5-portable\ from installation #1
:: Creates .env template, .env.example, .mt5-headless.json, and adds Git exclusions
scripts\mt5-headless.cmd portable --from-select 1 --launch-mode minimized
```

If using Option B (`portable`), instruct the user to open `.env` themselves and fill in:
- `MT5_BROKER_SERVER=`
- `MT5_LOGIN=`
- `MT5_PASSWORD=`

### Workflow 1: `init` — Environment, Auth Status & Safety Check

Verify active binding (`.mt5-headless.json`), portable flag, whether env auth vars are `[SET]` (without revealing values), running `terminal64.exe` processes, profiles, `.set` files, and `.ex5` experts:

```bash
scripts/mt5-headless.sh init
scripts/mt5-headless.sh init --json
```

### Workflow 2: `inspect` & `set` — AI Input Discovery & `.set` Generation

Discover EA parameters, comments, groups, and enum ordinal mappings directly from `.mq5`/`.mqh` source files (resolving `#include` chains automatically) or cross-reference an existing `.ini` / `.set`:

```bash
# List all enums and 0-based integer ordinals from EA source
scripts/mt5-headless.sh inspect --source "path/to/EA.mq5" --enums-only

# Search inputs matching a keyword or group, cross-referenced with a saved profile
scripts/mt5-headless.sh inspect --source "path/to/EA.mq5" --profile "MyEA.XAUUSD.M15.ini" --search "TradeMode|MAPeriod" --format md
```

Generate or update a UTF-16LE (with BOM) `.set` preset ready for Strategy Tester or live loading. Passing `--source` lets the AI specify **symbolic enum names** (e.g., `InpTradeMode=MODE_TREND_FOLLOW`), which are converted to MT5's integer ordinals (e.g., `2`), while preserving existing `||start||step||stop||Y/N` tails (or synthesizing valid `||start||step||stop||N` tails for `--from-report` / `--from-source`) and rejecting unresolved enum names or type mismatches when `--source` is omitted:

```bash
scripts/mt5-headless.sh set \
  --from-profile "MyEA.XAUUSD.M15.ini" \
  --source "src/MyEA.mq5" \
  --set InpTradeMode=MODE_TREND_FOLLOW \
  --set InpDisableLogsInBacktest=true \
  --set InpEnableNewsFilter=false \
  --save-to-tester "MyEA_Trend_v1"

# Diff two .set or .ini files
scripts/mt5-headless.sh set --diff "MyEA_a.set" "MyEA_b.set" --source "src/MyEA.mq5" --format md
```

### Workflow 2B: Closed-Source `.ex5` & MQL5 Market EAs (No `.mq5` Source)

Compiled `.ex5` binaries are encrypted/packed, so `input` declarations cannot be read directly from `.ex5` bytes. Instead, MT5 automatically writes **all compiled inputs, `input group` headers, and default values** into `<Report>.htm` whenever a backtest runs without `[TesterInputs]`:

1. **Run a 1-second `probe` (`Model=2` Open Prices)**:
   ```bash
   # Built-in non-Ghost run (hidden-desktop / minimized / hidden, parses .htm, writes UTF-16LE .set)
   scripts/mt5-headless.sh probe --expert "MyBot.ex5" --symbol "XAUUSD" --save-to-tester "MyBot_defaults" --launch-mode hidden-desktop --run

   # Or prepare .ini + GhostMCP payload (only if external Ghost MCP is installed)
   scripts/mt5-headless.sh probe --expert "MyBot.ex5" --symbol "XAUUSD" --save-to-tester "MyBot_defaults"
   ```
2. **Inspect & Customize from the Probe Report**:
   ```bash
   scripts/mt5-headless.sh inspect --report probe_MyBot --format md
   scripts/mt5-headless.sh set --from-report probe_MyBot --set InpLots=0.05 --save-to-tester "MyBot_tuned"
   ```

> **MQL5 Market EA Rules (`MQL5\Experts\Market\*.ex5`)**:
> - **Activation / DRM**: Market `.ex5` files are encrypted to your machine + MQL5 Community account in the MT5 installation where they were downloaded. Bind to that activated installation (`bind --select 1`), **not** an unactivated `.mt5-portable` copy (unless you log into your MQL5 Community account and re-download the Market product inside that portable instance).
> - **Keep original path/name**: Reference `--expert "Market\ProductName.ex5"` as installed; do not rename or move Market `.ex5` files.
> - **Enum values**: Without `.mq5` source, `probe` / `.htm` shows numeric ordinals (`0, 1, 2...`) for enum inputs; verify which ordinal corresponds to each mode from the EA's GUI Inputs tab or vendor documentation before overriding. Passing a symbolic enum name to `set --from-report` without `--source` fails fast to prevent writing an unresolved identifier into `.set`.

### Workflow 3: `compile` — Headless Compile Gate

Compile `.mq5` sources with `MetaEditor64.exe` (automatically stages space-free copies when filenames contain spaces, carries `#property icon` targets, verifies the UTF-16LE `.log`, and optionally deploys the `.ex5`):

```bash
scripts/mt5-headless.sh compile --source "src/MyEA.mq5" --deploy-to "MyEA\MyEA_test.ex5"
```

Pass gate: `Built: True` **and** `Result: 0 errors`.

### Workflow 4: `test` — Prepare & Run Headless Backtest

`mt5_headless.py` normalizes `/` to `\` in `Expert=`, verifies the `.ex5` exists under `<DataDir>\MQL5\Experts\` before generating the `.ini`, and confirms the agent log loaded the expected `.ex5` after `--run` completes.

#### Option A: Built-in Non-Ghost Launchers (`--run` — No External MCP Required)

Checks process locks, passes `/portable` automatically when bound in portable mode, runs using your chosen `--launch-mode` (`hidden-desktop`, `minimized`, or `hidden`) without stealing focus, scrubs `Password=` from the `.ini` in `finally:`, and exports the report:

```cmd
:: Win32 hidden off-screen desktop (CreateDesktopW — best Ghost alternative)
scripts\mt5-headless.cmd test --profile "MyProfile.ini" --expert "MyEA.ex5" --report run1 --model 1 --launch-mode hidden-desktop --run --format html --out "reports\run1.html" --embed-images

:: Minimized without stealing focus (SW_SHOWMINNOACTIVE — default)
scripts\mt5-headless.cmd test --profile "MyProfile.ini" --expert "MyEA.ex5" --report run1 --model 1 --launch-mode minimized --run

:: Hidden main window (SW_HIDE)
scripts\mt5-headless.cmd test --profile "MyProfile.ini" --expert "MyEA.ex5" --report run1 --model 1 --launch-mode hidden --run
```

#### Option B: External Ghost MCP Mode (Optional — Only If `mcp__ghost_window` Is Installed)

Generate `<Report>.ini` without launching MT5 (`--run` omitted). If `.env` or `MT5_*` env vars are set, `[Common]` (`Server`, `Login`, `Password`) is injected automatically:

```bash
scripts/mt5-headless.sh test \
  --profile "MyEA.XAUUSD.M15.ini" \
  --set-file "MyEA_Trend_v1.set" \
  --expert "MyEA\MyEA.ex5" \
  --report sample_run1 \
  --model 0
```

If the user opted to install Ghost MCP (`mcp__ghost_window`), launch the printed `GhostMCP` JSON payload via `mcp__ghost_window`, wait until `terminal64.exe` exits, scrub any injected password from the `.ini`, and run `report`:

```bash
scripts/mt5-headless.sh scrub-ini "<path_to_ini>"
```

### Workflow 4B: `optimize` — Parallel Multi-Core Sweeps & Auto `.set` Generation

Run MT5's native parallel **Optimization** (`Optimization=1` Complete or `Optimization=2` Genetic with `UseLocal=1`), fanning out parameter combinations across all local CPU agents (`metatester64.exe` `Agent-127.0.0.1-*`) and automatically writing the top pass to a ready-to-use UTF-16LE `.set` file:

```bash
# Sweep parameters in parallel across local CPU cores, rank by Sharpe, and export best .set
scripts/mt5-headless.sh optimize \
  --profile "MyEA.XAUUSD.M15.ini" \
  --source "src/MyEA.mq5" \
  --expert "MyEA\MyEA.ex5" \
  --report sample_opt1 \
  --model 1 \
  --mode 1 \
  --opt InpTradeMode=MODE_BREAKOUT:1:MODE_TREND_FOLLOW \
  --opt InpFastMAPeriod=10:10:40 \
  --min-trades 50 \
  --max-dd-pct 25 \
  --sort-by sharpe \
  --save-best-to-tester "MyEA_opt_best" \
  --format md --out "reports/sample_opt1.md" \
  --run
```

> **Two Ways to Run Parallel Backtests**:
> 1. **Single EA / Parameter Grid (`optimize`, recommended)**: One `terminal64.exe` automatically schedules passes across all local `metatester64.exe` CPU core agents (`Agent-127.0.0.1-3000+`) and writes `<Report>.xml` with all metrics.
> 2. **Multiple Full-Deal `.htm` Backtests Simultaneously (Different EAs / Symbols)**: Because one Data Folder allows only one `terminal64.exe` process at a time, create separate project-local `/portable` worker folders (`portable --dest .mt5-portable-w1`, `portable --dest .mt5-portable-w2`), optionally sharing `MQL5` / `bases` via directory junctions while keeping independent `Tester` and `logs` folders.

### Workflow 5: `report` — Export Backtests (`.htm`) or Optimization Leaderboards (`.xml`)

Parse any MT5 Strategy Tester `.htm` report or `.xml` optimization report, and export to `.md`, `.json`, or standalone `.html`. Exporting a pass to `.set` (`--save-best-to-tester` / `--export-best-set`) requires a complete baseline (`--profile`, `--set-file`, `--from-report`, or `--from-source`) so non-swept inputs do not fall back to EA defaults:

```bash
# Backtest (.htm)
scripts/mt5-headless.sh report sample_run1 --format md --out "reports/sample_run1.md" --include-deals --deals "deals_sample_run1.txt"
scripts/mt5-headless.sh report sample_run1 --format json --out "reports/sample_run1.json"
scripts/mt5-headless.sh report sample_run1 --format html --out "reports/sample_run1.html" --embed-images

# Optimization (.xml) leaderboard + pick Pass #33 to .set (requires baseline)
scripts/mt5-headless.sh report sample_opt1.xml --sort-by profit_factor --top 15 --format html --out "reports/opt_leaderboard.html"
scripts/mt5-headless.sh report sample_opt1.xml --profile "MyProfile.ini" --pick-pass 33 --save-best-to-tester "MyEA_pass33"
```

### Workflow 6: `compare` — Regression & A/B Run Comparison

Compare two runs (`base` vs `target`) using bare report names, `.htm` files, `.json` reports, or `deals_*.txt` files (ignoring column 0 `time` for forced `end of test` closes), and export comparison reports:

```bash
scripts/mt5-headless.sh compare sample_base sample_new
scripts/mt5-headless.sh compare sample_base sample_new --format md --out "reports/compare.md" --no-fail-exit
scripts/mt5-headless.sh compare sample_base sample_new --format html --out "reports/compare.html" --no-fail-exit
```

---

## Model Selection Guide

| Model | Mode | Typical Speed (M1, 1 month) | Recommended Use |
|---|---|---|---|
| `1` | 1-minute OHLC | ~1 second | Rapid parameter/preset sanity checks & ablation loops |
| `0` | Every tick | ~1 minute | Regression & build-vs-build `IDENTICAL` verification |
| `2` | Open prices only | Fastest | Bar-open-only strategies |
| `3` | Mathematical calculation | Instant | Math-only parameter evaluations |
| `4` | Every tick based on real ticks | Slowest | Final acceptance check (requires full broker tick history) |

---

## References

- [INI & `.set` Format Reference](./references/ini-and-set-format.md) — `[Common]`, `[Tester]`, `[TesterInputs]`, `/portable`, and `.env` variables.
- [Troubleshooting Guide](./references/troubleshooting.md) — Multi-MT5 discovery, portable login/server errors, Git/env protection, and compile/test pitfalls.
- [mt5_headless.py](./scripts/mt5_headless.py) — Main CLI implementation.
- [mt5-headless.cmd](./scripts/mt5-headless.cmd) — Windows CMD wrapper.
- [mt5-headless.sh](./scripts/mt5-headless.sh) — Bash / Git Bash wrapper.
- [Invoke-MT5Headless.ps1](./scripts/Invoke-MT5Headless.ps1) — PowerShell wrapper.
