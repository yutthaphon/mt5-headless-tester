# MT5 Strategy Tester `.ini`, `.set`, `/portable` & `.env` Reference

Reference for automating MetaTrader 5 Strategy Tester (`terminal64.exe [/portable] /config:<ini>`) and generating valid `.set` parameter files. Verified against MT5 build 6230.

---

## 1. File Locations (Standard vs Portable)

### Standard Installation
| Item | Path |
|---|---|
| Terminal & MetaEditor EXEs | `C:\Program Files\MetaTrader 5\terminal64.exe`, `MetaEditor64.exe` |
| Data Folder (linked via `origin.txt`) | `%APPDATA%\MetaQuotes\Terminal\<GUID>\` (e.g. `0123456789ABCDEF0123456789ABCDEF`) |
| Saved Tester Profiles & `.set` | `<DataDir>\MQL5\Profiles\Tester\` (`<EA>.<Symbol>.<TF>.<From>_<To>.<N>.ini` and `*.set`) |
| EA Presets / Compiled Experts | `<DataDir>\MQL5\Presets\`, `<DataDir>\MQL5\Experts\` |
| Report Output Root | `<DataDir>\<Report>.htm` + `<Report>*.png` |
| Tester Agent Logs | `%APPDATA%\MetaQuotes\Tester\<GUID>\Agent-127.0.0.1-3000\logs\YYYYMMDD.log` |
| Terminal & MQL5 Logs | `<DataDir>\logs\YYYYMMDD.log`, `<DataDir>\MQL5\Logs\YYYYMMDD.log` |

### Isolated Project-Local Portable Mode (`/portable`)
When provisioned via `portable` (e.g. `<project>\.mt5-portable\`), **both** binaries and data live inside the single folder:

| Item | Path |
|---|---|
| Terminal & MetaEditor EXEs | `<PortableDir>\terminal64.exe`, `MetaEditor64.exe` |
| Data Root & Reports | `<PortableDir>\` (`<PortableDir>\<Report>.htm`) |
| MQL5 Tree | `<PortableDir>\MQL5\{Experts,Include,Presets,Profiles\Tester,Logs}` |
| Tester Agent Logs | `<PortableDir>\Tester\Agent-*\logs\YYYYMMDD.log` |
| Project Binding File | `<ProjectDir>\.mt5-headless.json` (git-ignored) |

---

## 2. File Encoding

- **`.set` files and Tester `.ini` profiles**: **UTF-16LE with BOM (`0xFF 0xFE`)** and **CRLF (`\r\n`)** line endings.
- **Backtest `.htm` reports**: **UTF-16**.
- **Tester & Terminal `.log` files**: **UTF-16LE** (some builds omit BOM; detect NUL-byte density before decoding).
- **Optimization `.xml` reports**: **UTF-8**.

---

## 3. `[Common]` Section & `.env` Variables

For isolated `/portable` instances (or explicit broker authentication), MT5 reads account credentials from `[Common]` in the startup `.ini`:

| INI Key | Environment Variable (`.env`) | Notes |
|---|---|---|
| `Server` | `MT5_BROKER_SERVER` (or `MT5_SERVER`) | Broker server name (must match `config\servers.ini` / broker server list). |
| `Login` | `MT5_LOGIN` (or `MT5_USERNAME`) | Account number. |
| `Password` | `MT5_PASSWORD` | Account password (injected at runtime and scrubbed from `.ini` after terminal exit). |
| `KeepPrivate` | `1` (recommended) | Avoids persisting sensitive session state unnecessarily. |
| `ProxyEnable` | `0` | Disables proxy override. |
| `NewsEnable` | `0` | Disables news download during automated runs. |

> **Security Rule**: Users fill `.env` manually (`MT5_BROKER_SERVER=`, `MT5_LOGIN=`, `MT5_PASSWORD=`). AI agents must never read `.env` or print these values; scripts only report `[SET]` / `[UNSET]`. `.env`, `.mt5-headless.json`, and `.mt5-portable/` are excluded via `.gitignore` and `.git/info/exclude`.

---

## 4. `[Tester]` Section Keys

| Key | Value / Format | Notes |
|---|---|---|
| `Expert` | `MyEA\MyEA.ex5` | Relative path under `MQL5\Experts` using Windows backslashes `\`. Avoid spaces in `.ex5` filename. `mt5_headless.py` automatically normalizes `/` to `\` and verifies the `.ex5` exists before writing `.ini`. |
| `ExpertParameters` | `MyPreset.set` | Optional `.set` file under `MQL5\Profiles\Tester\`. When `[TesterInputs]` is included directly in the `.ini`, those values control the test. |
| `Symbol` | `XAUUSD` | Broker symbol name (exact match, including suffixes). |
| `Period` | `M1`, `M5`, `M15`, `M30`, `H1`, `H4`, `D1`, `W1`, `MN1` | Chart timeframe. |
| `Model` | `0`, `1`, `2`, `3`, `4` | Tick modeling mode (`0`=every tick, `1`=1m OHLC, `2`=open prices, `3`=math, `4`=real ticks). |
| `FromDate` / `ToDate` | `YYYY.MM.DD` | Backtest date range. Pair with `Dates=1` when specifying custom dates. |
| `Dates` | `0` or `1` | `1` uses custom `FromDate`/`ToDate`. |
| `Deposit` / `Currency` / `Leverage` | `10000` / `USD` / `500` | Initial account deposit, currency, and leverage (`1:N`). |
| `ExecutionMode` | `0` or delay in `ms` | Execution latency simulation (e.g. `0`, `250`). |
| `Optimization` | `0`, `1`, `2`, `3` | `0`=Disabled, `1`=Slow complete (parallel across local agents), `2`=Fast genetic (parallel across local agents), `3`=All Market Watch symbols (note: `Optimization=3` leaves `.symbols.xml` empty in tested builds). |
| `UseLocal` / `UseRemote` / `UseCloud` | `1` / `0` / `0` | Enables all local `metatester64.exe` CPU agents (`Agent-127.0.0.1-*`) while disabling remote/MQL5 Cloud agents. |
| `OptimizationCriterion` | `0..7` | `0`=Max Balance, `1`=Balance x PF, `2`=Balance x Payoff, `3`=Balance x Min DD, `4`=Balance x Recovery, `5`=Balance x Sharpe, `6`=Custom `OnTester`, `7`=Complex. |
| `ForwardMode` | `0` | `0` disables forward testing. |
| `Report` | `sample_run1` | **Must be a bare name** with no directory path and no extension. MT5 writes `<Report>.htm` (single test) or `<Report>.xml` (optimization) to `<DataDir>\`. |
| `ReplaceReport` | `1` | Overwrites existing `<Report>.htm`. Set a distinct `--report` name to keep history. |
| `ShutdownTerminal` | `1` | Closes `terminal64.exe` when test completes. Mandatory for headless batch runs. |
| `Visual` | `0` or `1` | `0` disables the visual chart window. |

---

## 5. `[TesterInputs]` and `.set` File Syntax

Each parameter line in a `.set` file or `[TesterInputs]` section follows:

```ini
InpKey=value||start||step||stop||Y_or_N
```

- **Enum Ordinals**: MT5 stores enum inputs as 0-based integer ordinals inside `.set` and `.ini` files (e.g. `InpTradeMode=2` for `MODE_TREND_FOLLOW`). Pass `--source <file.mq5>` to `inspect`, `set`, or `test` to automatically resolve symbolic names; passing an unresolved symbolic enum name without `--source` fails fast to prevent writing broken `.set` files.
- **Complete Baseline**: Always build `.set` and `.ini` files (including `report <opt.xml> --save-best-to-tester` / `--export-best-set`) from a real profile (`--profile`), complete `.set` (`--from-set`), probe report (`--from-report`), or full source defaults (`--from-source`) so omitted inputs do not fall back to unintended EA defaults.
- **Compiled `.ex5` Without Source**: Run `probe --expert <name.ex5> --symbol <sym>` (`Model=2`, no `[TesterInputs]`). MT5 writes every compiled `input` and `input group` into `<Report>.htm`, which `inspect --report` and `set --from-report` convert directly into UTF-16LE `.set` files with valid `||start||step||stop||N` tails.
