#!/usr/bin/env python3
"""MT5 Headless Tester CLI.

Provides non-intrusive, command-line workflows for MetaTrader 5 EA input
discovery, .set preset generation, headless MQL5 compilation, Strategy Tester
execution (via Ghost hidden desktop or background minimized launch), report
parsing/export (text, md, json, html), and run-to-run deal comparisons.

Standard library only (Python 3.9+).
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import datetime as dt
import difflib
import hashlib
import html as html_lib
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_DATA_GUID = "0123456789ABCDEF0123456789ABCDEF"
DEFAULT_TERMINAL_DIR = r"C:\Program Files\MetaTrader 5"
BINDING_FILENAME = ".mt5-headless.json"
SW_HIDE = 0
SW_SHOWMINNOACTIVE = 7
STARTF_USESHOWWINDOW = 0x00000001
LAUNCH_MODES = ("minimized", "hidden-desktop", "hidden", "ghost")
AI_INSTALL_TARGETS = (
    "auto",
    "all",
    "antigravity",
    "claude",
    "codex",
    "cursor",
    "windsurf",
    "cline",
    "roo",
    "copilot",
    "agents",
)

DEAL_COLUMNS = [
    "time",
    "deal",
    "symbol",
    "type",
    "direction",
    "volume",
    "price",
    "order",
    "commission",
    "swap",
    "profit",
    "balance",
    "comment",
]

ORDER_COLUMNS = [
    "open_time",
    "order",
    "symbol",
    "type",
    "volume",
    "price",
    "sl",
    "tp",
    "time",
    "state",
    "comment",
]

BUILTIN_ENUMS: Dict[str, Dict[str, int]] = {
    "ENUM_TIMEFRAMES": {
        "PERIOD_CURRENT": 0,
        "PERIOD_M1": 1,
        "PERIOD_M2": 2,
        "PERIOD_M3": 3,
        "PERIOD_M4": 4,
        "PERIOD_M5": 5,
        "PERIOD_M6": 6,
        "PERIOD_M10": 10,
        "PERIOD_M12": 12,
        "PERIOD_M15": 15,
        "PERIOD_M20": 20,
        "PERIOD_M30": 30,
        "PERIOD_H1": 16385,
        "PERIOD_H2": 16386,
        "PERIOD_H3": 16387,
        "PERIOD_H4": 16388,
        "PERIOD_H6": 16390,
        "PERIOD_H8": 16392,
        "PERIOD_H12": 16396,
        "PERIOD_D1": 16408,
        "PERIOD_W1": 32769,
        "PERIOD_MN1": 49153,
    },
    "ENUM_MA_METHOD": {
        "MODE_SMA": 0,
        "MODE_EMA": 1,
        "MODE_SMMA": 2,
        "MODE_LWMA": 3,
    },
    "ENUM_APPLIED_PRICE": {
        "PRICE_CLOSE": 1,
        "PRICE_OPEN": 2,
        "PRICE_HIGH": 3,
        "PRICE_LOW": 4,
        "PRICE_MEDIAN": 5,
        "PRICE_TYPICAL": 6,
        "PRICE_WEIGHTED": 7,
    },
    "ENUM_STO_PRICE": {
        "STO_LOWHIGH": 0,
        "STO_CLOSECLOSE": 1,
    },
}


# ---------------------------------------------------------------------------
# Environment, Project Binding (.mt5-headless.json), .env & Git Protection
# ---------------------------------------------------------------------------


def find_project_binding(start_dir: Optional[str] = None) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Walk up from start_dir (or CWD) looking for .mt5-headless.json."""
    base = os.path.abspath(start_dir or os.getcwd())
    cur = base
    while True:
        cfg_path = os.path.join(cur, BINDING_FILENAME)
        if os.path.isfile(cfg_path):
            try:
                data = json.loads(read_text_auto(cfg_path))
                if isinstance(data, dict):
                    return cur, data
            except Exception:
                pass
        if os.path.exists(os.path.join(cur, ".git")):
            break
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        cur = parent
    return base, None


def load_dotenv_into_environ(project_dir: str) -> Optional[str]:
    """Silently load MT5_* variables from .env into os.environ without logging values."""
    candidates: List[str] = []
    env_override = os.environ.get("MT5_ENV_FILE")
    if env_override:
        candidates.append(env_override)
    candidates.append(os.path.join(project_dir, ".env"))
    cwd_env = os.path.join(os.getcwd(), ".env")
    if os.path.normcase(os.path.abspath(cwd_env)) != os.path.normcase(os.path.abspath(candidates[-1])):
        candidates.append(cwd_env)

    loaded_from: Optional[str] = None
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            content = read_text_auto(path)
        except OSError:
            continue
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and ((val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")):
                val = val[1:-1]
            if key and key not in os.environ and val != "":
                os.environ[key] = val
        loaded_from = os.path.abspath(path)
        break
    return loaded_from


def _resolve_maybe_relative(path_val: Optional[str], base_dir: str) -> Optional[str]:
    if not path_val:
        return None
    if os.path.isabs(path_val):
        return os.path.normpath(path_val)
    return os.path.normpath(os.path.join(base_dir, path_val))


def resolve_env_paths(args: argparse.Namespace) -> Dict[str, str]:
    """Resolve MT5 terminal, data, tester, portable mode, and working directories."""
    appdata = os.environ.get("APPDATA", "")
    temp_dir = os.environ.get("TEMP") or os.environ.get("TMP") or os.path.expanduser("~")

    proj_arg = getattr(args, "project_dir", None)
    project_dir, binding = find_project_binding(proj_arg)
    dotenv_path = load_dotenv_into_environ(project_dir)

    cli_portable = getattr(args, "portable", None)
    env_portable = os.environ.get("MT5_PORTABLE", "").strip().lower() in ("1", "true", "yes")
    portable = bool(
        cli_portable
        if cli_portable is True
        else (env_portable or (binding and binding.get("portable") is True))
    )

    raw_launch_mode = (
        getattr(args, "launch_mode", None)
        or os.environ.get("MT5_LAUNCH_MODE")
        or (binding.get("launch_mode") if binding else None)
        or "minimized"
    )
    launch_mode = str(raw_launch_mode).strip().lower()
    if launch_mode not in LAUNCH_MODES:
        launch_mode = "minimized"

    terminal_dir = (
        getattr(args, "terminal_dir", None)
        or os.environ.get("MT5_TERMINAL_DIR")
        or _resolve_maybe_relative(binding.get("terminal_dir") if binding else None, project_dir)
        or DEFAULT_TERMINAL_DIR
    )

    data_dir = (
        getattr(args, "data_dir", None)
        or os.environ.get("MT5_DATA_DIR")
        or (terminal_dir if portable and not (binding and binding.get("data_dir")) else None)
        or _resolve_maybe_relative(binding.get("data_dir") if binding else None, project_dir)
    )
    if not data_dir:
        candidate = os.path.join(appdata, "MetaQuotes", "Terminal", DEFAULT_DATA_GUID)
        if os.path.isdir(candidate):
            data_dir = candidate
        else:
            term_root = os.path.join(appdata, "MetaQuotes", "Terminal")
            if os.path.isdir(term_root):
                term_norm = os.path.normcase(os.path.abspath(terminal_dir))
                ranked_dirs: List[Tuple[int, str]] = []
                for entry in sorted(os.listdir(term_root)):
                    if entry.lower() in ("common", "community"):
                        continue
                    probe = os.path.join(term_root, entry)
                    if not os.path.isdir(probe):
                        continue
                    origin_file = os.path.join(probe, "origin.txt")
                    origin_dir = read_text_auto(origin_file).strip() if os.path.isfile(origin_file) else ""
                    origin_ok = bool(origin_dir and os.path.isfile(os.path.join(origin_dir, "terminal64.exe")))
                    origin_match = bool(
                        origin_ok
                        and os.path.normcase(os.path.abspath(origin_dir)) == term_norm
                    )
                    has_tester = os.path.isdir(os.path.join(probe, "MQL5", "Profiles", "Tester"))
                    has_experts = os.path.isdir(os.path.join(probe, "MQL5", "Experts"))
                    score = (4 if origin_match else 0) + (2 if origin_ok else 0) + (2 if has_tester else 0) + (1 if has_experts else 0)
                    if score > 0:
                        ranked_dirs.append((-score, probe))
                if ranked_dirs:
                    ranked_dirs.sort()
                    data_dir = ranked_dirs[0][1]
            if not data_dir:
                data_dir = candidate

    tester_dir = (
        getattr(args, "tester_dir", None)
        or os.environ.get("MT5_TESTER_DIR")
        or _resolve_maybe_relative(binding.get("tester_dir") if binding else None, project_dir)
    )
    if not tester_dir:
        if portable or (
            os.path.normcase(os.path.abspath(terminal_dir))
            == os.path.normcase(os.path.abspath(data_dir))
        ):
            tester_dir = os.path.join(data_dir, "Tester")
        else:
            guid = os.path.basename(os.path.normpath(data_dir)) or DEFAULT_DATA_GUID
            tester_dir = os.path.join(appdata, "MetaQuotes", "Tester", guid)

    work_dir = (
        getattr(args, "work_dir", None)
        or os.environ.get("MT5_WORK_DIR")
        or _resolve_maybe_relative(binding.get("work_dir") if binding else None, project_dir)
        or os.path.join(temp_dir, "mt5-headless-bt")
    )

    return {
        "project_dir": os.path.normpath(project_dir),
        "binding_file": os.path.join(project_dir, BINDING_FILENAME) if binding else "",
        "dotenv_file": dotenv_path or "",
        "portable": portable,
        "launch_mode": launch_mode,
        "terminal_dir": os.path.normpath(terminal_dir),
        "terminal_exe": os.path.join(terminal_dir, "terminal64.exe"),
        "metaeditor_exe": os.path.join(terminal_dir, "MetaEditor64.exe"),
        "data_dir": os.path.normpath(data_dir),
        "mql5_dir": os.path.join(data_dir, "MQL5"),
        "profiles_tester_dir": os.path.join(data_dir, "MQL5", "Profiles", "Tester"),
        "presets_dir": os.path.join(data_dir, "MQL5", "Presets"),
        "experts_dir": os.path.join(data_dir, "MQL5", "Experts"),
        "tester_dir": os.path.normpath(tester_dir),
        "work_dir": os.path.normpath(work_dir),
    }


def _find_git_root(start_dir: str) -> Optional[str]:
    cur = os.path.abspath(start_dir)
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            return None
        cur = parent


def ensure_git_and_env_protection(
    project_dir: str,
    create_dotenv: bool = False,
    portable_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Protect .env, .mt5-headless.json, and portable runtime from ever entering Git."""
    project_dir = os.path.abspath(project_dir)
    patterns = [
        ".env",
        ".env.*",
        "!.env.example",
        BINDING_FILENAME,
        ".mt5-portable/",
    ]
    if portable_dir:
        try:
            rel = os.path.relpath(os.path.abspath(portable_dir), project_dir).replace("\\", "/")
            if not rel.startswith(".."):
                norm_rel = rel.rstrip("/") + "/"
                if norm_rel not in patterns:
                    patterns.append(norm_rel)
        except ValueError:
            pass

    def _append_missing_patterns(ignore_file: str) -> bool:
        existing_lines: List[str] = []
        if os.path.isfile(ignore_file):
            existing_lines = read_text_auto(ignore_file).splitlines()
        existing_set = {ln.strip() for ln in existing_lines}
        missing = [p for p in patterns if p not in existing_set]
        if not missing:
            return False
        new_lines = list(existing_lines)
        if new_lines and new_lines[-1].strip() != "":
            new_lines.append("")
        header = "# MT5 Headless Tester (secrets & local portable runtime - NEVER COMMIT)"
        if header not in existing_set:
            new_lines.append(header)
        new_lines.extend(missing)
        write_utf8(ignore_file, "\n".join(new_lines) + "\n")
        return True

    gitignore_path = os.path.join(project_dir, ".gitignore")
    gitignore_updated = _append_missing_patterns(gitignore_path)

    git_root = _find_git_root(project_dir)
    git_exclude_updated = False
    env_tracked_warning = False
    if git_root:
        exclude_path = os.path.join(git_root, ".git", "info", "exclude")
        if os.path.isdir(os.path.dirname(exclude_path)):
            git_exclude_updated = _append_missing_patterns(exclude_path)
        try:
            chk = subprocess.run(
                ["git", "-C", git_root, "ls-files", "--error-unmatch", ".env"],
                capture_output=True,
                text=True,
                check=False,
            )
            if chk.returncode == 0:
                env_tracked_warning = True
        except OSError:
            pass

    env_example_path = os.path.join(project_dir, ".env.example")
    if not os.path.isfile(env_example_path):
        write_utf8(
            env_example_path,
            "\n".join(
                [
                    "# MT5 Broker Credentials for Portable / Headless Mode",
                    "# Fill these values inside .env (NOT .env.example).",
                    "# NEVER commit .env to Git or paste secrets into chat.",
                    "MT5_BROKER_SERVER=YourBroker-Demo",
                    "MT5_LOGIN=12345678",
                    "MT5_PASSWORD=YourPasswordHere",
                    "",
                ]
            ),
        )

    dotenv_path = os.path.join(project_dir, ".env")
    dotenv_created = False
    if create_dotenv and not os.path.isfile(dotenv_path):
        write_utf8(
            dotenv_path,
            "\n".join(
                [
                    "# Local MT5 credentials - DO NOT COMMIT OR SHARE",
                    "# Fill in your broker server, account login, and password:",
                    "MT5_BROKER_SERVER=",
                    "MT5_LOGIN=",
                    "MT5_PASSWORD=",
                    "",
                ]
            ),
        )
        dotenv_created = True

    return {
        "gitignore": gitignore_path,
        "gitignore_updated": gitignore_updated,
        "git_root": git_root,
        "git_exclude_updated": git_exclude_updated,
        "env_example": env_example_path,
        "dotenv": dotenv_path,
        "dotenv_created": dotenv_created,
        "env_tracked_warning": env_tracked_warning,
    }


def scrub_ini_credentials(ini_path: str) -> bool:
    """Erase plaintext Password= value from a generated tester .ini file."""
    if not ini_path or not os.path.isfile(ini_path):
        return False
    lines = read_text_auto(ini_path).splitlines()
    changed = False
    out_lines: List[str] = []
    for ln in lines:
        if re.match(r"^\s*Password\s*=", ln, re.I):
            out_lines.append("Password=")
            changed = True
        else:
            out_lines.append(ln)
    if changed:
        write_utf16le_bom(ini_path, out_lines)
    return changed


# ---------------------------------------------------------------------------
# Encoding & Process Helpers
# ---------------------------------------------------------------------------


def read_text_auto(path: str) -> str:
    """Read a text file with automatic BOM and NUL-byte UTF-16LE detection."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le", errors="replace")
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    sample = raw[:4096]
    if sample and sample.count(b"\x00") > max(4, len(sample) // 5):
        return raw.decode("utf-16-le", errors="replace")
    return raw.decode("utf-8", errors="replace")


def write_utf16le_bom(path: str, content: str | Sequence[str]) -> None:
    """Write text or lines as UTF-16LE with BOM and CRLF line endings."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    if isinstance(content, str):
        normalized = content.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    else:
        normalized = "\r\n".join(str(line).rstrip("\r\n") for line in content) + "\r\n"
    encoded = b"\xff\xfe" + normalized.encode("utf-16-le")
    with open(path, "wb") as fh:
        fh.write(encoded)


def write_utf8(path: str, content: str) -> None:
    """Write UTF-8 text ensuring parent directories exist."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def _get_process_exe_path_win32(pid: int) -> Optional[str]:
    """Query full executable path for a Windows PID using kernel32 without spawning PowerShell."""
    if os.name != "nt":
        return None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    try:
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            buf_len = ctypes.c_uint(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(buf_len)):
                return buf.value
            return None
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return None


def _get_win32_session_id() -> int:
    """Return current Windows Session ID (0 = non-interactive Session 0; >0 = desktop session)."""
    if os.name != "nt":
        return 1
    try:
        sid = ctypes.c_uint(1)
        ok = ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
        return int(sid.value) if ok else 1
    except Exception:
        return 1


def get_running_terminals() -> List[Dict[str, Any]]:
    """Return list of running terminal64.exe processes with PID and executable path."""
    if os.name != "nt":
        return []
    procs: List[Dict[str, Any]] = []
    try:
        res = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq terminal64.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        for ln in (res.stdout or "").splitlines():
            if "terminal64.exe" not in ln.lower():
                continue
            parts = [p.strip('"') for p in ln.strip().split('","')]
            pid = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
            exe_path = _get_process_exe_path_win32(pid) if pid is not None else None
            procs.append({"pid": pid, "exe": exe_path or "<unknown>", "raw": ln.strip()})
    except Exception:
        pass
    return procs


def is_terminal_running(
    target_terminal_exe: Optional[str] = None, portable: bool = False
) -> Tuple[bool, List[Dict[str, Any]]]:
    """Check whether terminal64.exe is running (scoped to matching exe in portable mode)."""
    procs = get_running_terminals()
    if not procs:
        return False, []
    if not portable or not target_terminal_exe:
        return True, procs

    target_norm = os.path.normcase(os.path.abspath(target_terminal_exe))
    blocking = [
        p
        for p in procs
        if p["exe"] == "<unknown>"
        or os.path.normcase(os.path.abspath(p["exe"])) == target_norm
    ]
    return len(blocking) > 0, procs


def resolve_profile_path(profile_arg: str, paths: Dict[str, str]) -> str:
    """Resolve an INI profile path either directly or under Profiles\\Tester."""
    if os.path.isfile(profile_arg):
        return os.path.abspath(profile_arg)
    candidate = os.path.join(paths["profiles_tester_dir"], profile_arg)
    if os.path.isfile(candidate):
        return candidate
    if not profile_arg.lower().endswith(".ini"):
        candidate_ini = candidate + ".ini"
        if os.path.isfile(candidate_ini):
            return candidate_ini
    raise FileNotFoundError(f"Tester profile not found: {profile_arg} (checked {candidate})")


def resolve_set_path(set_arg: str, paths: Dict[str, str]) -> str:
    """Resolve a .set file path directly or under Profiles\\Tester or Presets."""
    if os.path.isfile(set_arg):
        return os.path.abspath(set_arg)
    for base_dir in (paths["profiles_tester_dir"], paths["presets_dir"]):
        for name in (set_arg, f"{set_arg}.set"):
            candidate = os.path.join(base_dir, name)
            if os.path.isfile(candidate):
                return candidate
    raise FileNotFoundError(f".set file not found: {set_arg}")


def resolve_report_htm_path(report_arg: str, paths: Dict[str, str]) -> Tuple[str, str]:
    """Resolve a report argument (bare name or .htm path) to (report_name, htm_path)."""
    if os.path.isfile(report_arg):
        abs_path = os.path.abspath(report_arg)
        stem = pathlib.Path(abs_path).stem
        return stem, abs_path
    clean = report_arg[:-4] if report_arg.lower().endswith(".htm") else report_arg
    candidate = os.path.join(paths["data_dir"], f"{clean}.htm")
    if os.path.isfile(candidate):
        return clean, candidate
    raise FileNotFoundError(f"Report .htm not found: {report_arg} (checked {candidate})")


def resolve_expert_ini_path(expert_arg: str, paths: Dict[str, str]) -> str:
    """Normalize Expert= path to Windows backslashes and verify the .ex5 exists under MQL5\\Experts."""
    raw = (expert_arg or "").strip().strip('"').strip("'")
    if not raw:
        raise ValueError("--expert cannot be empty")
    norm = raw.replace("/", "\\")
    if not norm.lower().endswith(".ex5"):
        norm += ".ex5"

    experts_dir = os.path.abspath(paths["experts_dir"])
    if os.path.isabs(norm):
        abs_ex5 = os.path.abspath(norm)
        if not os.path.isfile(abs_ex5):
            raise FileNotFoundError(f"Expert .ex5 not found: {abs_ex5}")
        try:
            common = os.path.commonpath([os.path.normcase(experts_dir), os.path.normcase(abs_ex5)])
        except ValueError:
            common = ""
        if common != os.path.normcase(experts_dir):
            raise ValueError(
                f"MT5 Strategy Tester requires the .ex5 inside '{experts_dir}', got: {abs_ex5}"
            )
        rel = os.path.relpath(abs_ex5, experts_dir)
    else:
        rel = norm.lstrip("\\")
        if rel.startswith(".."):
            raise ValueError(
                f"Expert path must stay inside '{experts_dir}', got: {expert_arg}"
            )
        if not os.path.isfile(os.path.join(experts_dir, rel)):
            m_prefix = re.match(r"^(?:mql5\\)?experts\\(.+)$", rel, re.I)
            if m_prefix and os.path.isfile(os.path.join(experts_dir, m_prefix.group(1))):
                rel = m_prefix.group(1)

    full_ex5 = os.path.normpath(os.path.join(experts_dir, rel))
    if not os.path.isfile(full_ex5):
        suggestions: List[str] = []
        if os.path.isdir(experts_dir):
            rel_low = rel.lower()
            base_low = os.path.basename(rel).lower()
            for root, _, files in os.walk(experts_dir):
                for fname in files:
                    if not fname.lower().endswith(".ex5"):
                        continue
                    cand_rel = os.path.relpath(os.path.join(root, fname), experts_dir).replace("/", "\\")
                    cand_low = cand_rel.lower()
                    if cand_low.endswith("\\" + rel_low) or fname.lower() == base_low:
                        suggestions.append(cand_rel)
        hint = (
            f' Did you mean: --expert "{suggestions[0]}"?'
            if len(suggestions) == 1
            else (
                " Matching .ex5 files: " + ", ".join(f'"{s}"' for s in suggestions[:5])
                if suggestions
                else ""
            )
        )
        raise FileNotFoundError(
            f"Expert .ex5 not found under MQL5\\Experts: {full_ex5} (from Expert={rel}).{hint}"
        )

    return rel.replace("/", "\\")


# ---------------------------------------------------------------------------
# MQL5 Source Parsing (Inputs & Enums)
# ---------------------------------------------------------------------------


def _strip_block_comments_preserve_strings(text: str) -> str:
    """Remove /* ... */ block comments outside string literals while preserving // comments."""
    out: List[str] = []
    i = 0
    n = len(text)
    in_str = False
    in_char = False
    in_line_comment = False
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_line_comment:
            out.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(nxt)
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if in_char:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(nxt)
                i += 2
                continue
            if ch == "'":
                in_char = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
        elif ch == "'":
            in_char = True
            out.append(ch)
            i += 1
        elif ch == "/" and nxt == "/":
            in_line_comment = True
            out.append("//")
            i += 2
        elif ch == "/" and nxt == "*":
            i += 2
            while i < n - 1 and not (text[i] == "*" and text[i + 1] == "/"):
                if text[i] == "\n":
                    out.append("\n")
                i += 1
            i += 2 if i < n - 1 else 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _split_top_level_statement(line_or_block: str) -> Tuple[str, str]:
    """Split 'code; // comment' into (code_before_semicolon, inline_comment)."""
    in_str = False
    in_char = False
    n = len(line_or_block)
    i = 0
    while i < n:
        ch = line_or_block[i]
        nxt = line_or_block[i + 1] if i + 1 < n else ""
        if in_str:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                in_str = False
        elif in_char:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == "'":
                in_char = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "'":
                in_char = True
            elif ch == "/" and nxt == "/":
                return line_or_block[:i].strip(), line_or_block[i + 2 :].strip()
            elif ch == ";":
                rest = line_or_block[i + 1 :].strip()
                comment = ""
                if "//" in rest:
                    comment = rest.split("//", 1)[1].strip()
                return line_or_block[:i].strip(), comment
        i += 1
    return line_or_block.strip(), ""


def _collect_mql5_files(source_path: str, mql5_include_dir: Optional[str]) -> List[str]:
    """Collect .mq5/.mqh files from a directory or recursively via #include."""
    abs_path = os.path.abspath(source_path)
    if os.path.isdir(abs_path):
        found: List[str] = []
        for root, _, files in os.walk(abs_path):
            for fname in files:
                if fname.lower().endswith((".mq5", ".mqh")):
                    found.append(os.path.join(root, fname))

        def _sort_key(p: str) -> Tuple[int, str]:
            low = os.path.basename(p).lower()
            if "enum" in low:
                return (0, p)
            if "define" in low:
                return (1, p)
            if "input" in low:
                return (2, p)
            if low.endswith(".mqh"):
                return (3, p)
            return (4, p)

        return sorted(found, key=_sort_key)

    if not os.path.isfile(abs_path):
        raise FileNotFoundError(f"MQL5 source not found: {source_path}")

    visited: set[str] = set()
    ordered: List[str] = []
    base_dir = os.path.dirname(abs_path)

    include_re = re.compile(r'^\s*#include\s*([<"])([^>"]+)[>"]', re.MULTILINE)

    def _resolve_include(inc_target: str, current_file: str, bracket: str) -> Optional[str]:
        inc_norm = inc_target.replace("/", os.sep).replace("\\", os.sep)
        search_dirs = [os.path.dirname(current_file), base_dir]
        # Walk up up to 3 parent dirs from base_dir for project-relative paths
        parent = base_dir
        for _ in range(3):
            parent = os.path.dirname(parent)
            if parent and parent not in search_dirs:
                search_dirs.append(parent)
        if mql5_include_dir and os.path.isdir(mql5_include_dir):
            search_dirs.append(mql5_include_dir)
        for d in search_dirs:
            cand = os.path.normpath(os.path.join(d, inc_norm))
            if os.path.isfile(cand):
                return cand
        return None

    def _visit(fpath: str) -> None:
        real = os.path.normcase(os.path.abspath(fpath))
        if real in visited:
            return
        visited.add(real)
        try:
            text = read_text_auto(fpath)
        except OSError:
            return
        for m in include_re.finditer(text):
            bracket, target = m.group(1), m.group(2)
            resolved = _resolve_include(target, fpath, bracket)
            if resolved:
                _visit(resolved)
        ordered.append(fpath)

    _visit(abs_path)
    return ordered


def _parse_enum_body(body: str) -> Tuple[Dict[str, int], Dict[str, str]]:
    """Parse entries inside an enum { ... } block."""
    values: Dict[str, int] = {}
    comments: Dict[str, str] = {}
    next_val = 0

    # Parse entry by entry, preserving inline comments after commas
    # Handle single-line and multi-line declarations
    # Replace newlines inside with spaces? Wait: we need // comments to end at newline!
    raw_lines = body.splitlines()
    tokens: List[Tuple[str, str]] = []
    for line in raw_lines:
        code_part = line
        comment_part = ""
        if "//" in line:
            idx = line.find("//")
            code_part = line[:idx]
            comment_part = line[idx + 2 :].strip()
        parts = [p.strip() for p in code_part.split(",")]
        non_empty = [p for p in parts if p]
        for idx_p, part in enumerate(non_empty):
            c = comment_part if idx_p == len(non_empty) - 1 else ""
            tokens.append((part, c))

    for decl, comment in tokens:
        if "=" in decl:
            name_part, val_part = decl.split("=", 1)
            name = name_part.strip()
            val_str = val_part.strip()
            try:
                val = int(val_str, 0)
            except ValueError:
                if val_str in values:
                    val = values[val_str]
                else:
                    val = next_val
            values[name] = val
            next_val = val + 1
        else:
            name = decl.strip()
            if not re.match(r"^[A-Za-z_]\w*$", name):
                continue
            values[name] = next_val
            next_val += 1
        if comment:
            comments[name] = comment

    return values, comments


def parse_mql5_source(
    source_path: str, mql5_include_dir: Optional[str] = None
) -> Dict[str, Any]:
    """Parse MQL5 enums and input declarations from .mq5/.mqh files."""
    files = _collect_mql5_files(source_path, mql5_include_dir)
    enums: Dict[str, Dict[str, Any]] = {}
    for ename, emap in BUILTIN_ENUMS.items():
        enums[ename] = {
            "name": ename,
            "values": dict(emap),
            "reverse": {v: k for k, v in emap.items()},
            "comments": {},
            "file": "<builtin>",
        }

    inputs: List[Dict[str, Any]] = []
    seen_input_names: set[str] = set()

    enum_start_re = re.compile(r"^\s*enum\s+([A-Za-z_]\w*)(?:\s*:\s*\w+)?\s*(?:\{|$)")
    group_re = re.compile(r'^\s*(?:s?input)\s+group\s+"([^"]*)"')
    input_re = re.compile(
        r"^\s*(s?input)\s+(?:const\s+)?([A-Za-z_]\w*)\s+([A-Za-z_]\w*)(?:\s*=\s*(.+))?$"
    )

    current_group = ""

    for fpath in files:
        raw_text = read_text_auto(fpath)
        clean_text = _strip_block_comments_preserve_strings(raw_text)
        lines = clean_text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                i += 1
                continue

            # Parse enum block
            em = enum_start_re.match(line)
            if em:
                enum_name = em.group(1)
                buf = [line]
                brace_depth = line.count("{") - line.count("}")
                saw_open = "{" in line
                while (not saw_open or brace_depth > 0) and i + 1 < len(lines):
                    i += 1
                    nxt_line = lines[i]
                    buf.append(nxt_line)
                    if "{" in nxt_line:
                        saw_open = True
                    brace_depth += nxt_line.count("{") - nxt_line.count("}")
                block_text = "\n".join(buf)
                lbrace = block_text.find("{")
                rbrace = block_text.rfind("}")
                if lbrace != -1 and rbrace != -1 and rbrace > lbrace:
                    body = block_text[lbrace + 1 : rbrace]
                    ev, ec = _parse_enum_body(body)
                    enums[enum_name] = {
                        "name": enum_name,
                        "values": ev,
                        "reverse": {v: k for k, v in ev.items()},
                        "comments": ec,
                        "file": fpath,
                    }
                i += 1
                continue

            # Parse input group
            gm = group_re.match(line)
            if gm:
                current_group = gm.group(1)
                i += 1
                continue

            # Parse input / sinput declaration
            if re.match(r"^\s*(?:s?input)\b", line):
                stmt = line
                while ";" not in stmt and i + 1 < len(lines):
                    i += 1
                    stmt += " " + lines[i].strip()
                code_part, inline_comment = _split_top_level_statement(stmt)
                im = input_re.match(code_part)
                if im:
                    storage, dtype, name, default_raw = (
                        im.group(1),
                        im.group(2),
                        im.group(3),
                        (im.group(4) or "").strip(),
                    )
                    if name not in seen_input_names:
                        seen_input_names.add(name)
                        if dtype == "string" and len(default_raw) >= 2 and default_raw.startswith('"') and default_raw.endswith('"'):
                            default_val = default_raw[1:-1]
                        else:
                            default_val = default_raw
                        inputs.append(
                            {
                                "name": name,
                                "storage": storage,
                                "type": dtype,
                                "default": default_val,
                                "default_raw": default_raw,
                                "group": current_group,
                                "comment": inline_comment,
                                "file": fpath,
                            }
                        )
            i += 1

    # Second pass: resolve enum metadata on inputs
    for inp in inputs:
        dtype = inp["type"]
        if dtype in enums:
            einfo = enums[dtype]
            inp["is_enum"] = True
            inp["enum_values"] = einfo["values"]
            default_val = inp["default"]
            if default_val in einfo["values"]:
                inp["default_ordinal"] = einfo["values"][default_val]
                inp["default_symbol"] = default_val
            else:
                try:
                    ord_int = int(default_val, 0)
                    inp["default_ordinal"] = ord_int
                    inp["default_symbol"] = einfo["reverse"].get(ord_int)
                except (ValueError, TypeError):
                    inp["default_ordinal"] = None
                    inp["default_symbol"] = None
        else:
            inp["is_enum"] = False

    return {
        "files_scanned": files,
        "enums": enums,
        "inputs": inputs,
        "inputs_by_name": {x["name"]: x for x in inputs},
    }


# ---------------------------------------------------------------------------
# INI / .set Parsing & Serialization
# ---------------------------------------------------------------------------


def parse_input_entry(raw_val: str) -> Dict[str, Any]:
    """Parse MT5 input value format: value||start||step||stop||Y/N."""
    parts = raw_val.split("||")
    if len(parts) >= 5:
        return {
            "value": parts[0],
            "start": parts[1],
            "step": parts[2],
            "stop": parts[3],
            "optimize": parts[4].upper(),
            "has_tail": True,
            "tail": "||" + "||".join(parts[1:]),
        }
    return {
        "value": raw_val,
        "start": "",
        "step": "",
        "stop": "",
        "optimize": "N",
        "has_tail": False,
        "tail": "",
    }


def format_input_entry(entry: Dict[str, Any]) -> str:
    """Serialize an input entry back to value or value||start||step||stop||Y/N."""
    if entry.get("has_tail"):
        return f"{entry['value']}||{entry['start']}||{entry['step']}||{entry['stop']}||{entry['optimize']}"
    return str(entry["value"])


def parse_ini_or_set(path: str) -> Dict[str, Any]:
    """Parse a MT5 tester .ini profile or .set file preserving order and comments."""
    text = read_text_auto(path)
    lines = text.splitlines()
    is_ini = any(ln.strip().startswith("[Tester]") for ln in lines)

    tester: Dict[str, str] = {}
    inputs: Dict[str, Dict[str, Any]] = {}
    comments: List[str] = []
    raw_lines: List[str] = lines

    section = ""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(";"):
            if not section:
                comments.append(line)
            continue
        sec_m = re.match(r"^\[(.+)\]$", stripped)
        if sec_m:
            section = sec_m.group(1)
            continue
        if "=" in line:
            key, val = line.split("=", 1)
            key = key.strip()
            if is_ini:
                if section == "Tester":
                    tester[key] = val.strip()
                elif section == "TesterInputs":
                    inputs[key] = parse_input_entry(val)
            else:
                inputs[key] = parse_input_entry(val)

    return {
        "path": os.path.abspath(path),
        "is_ini": is_ini,
        "comments": comments,
        "tester": tester,
        "inputs": inputs,
        "raw_lines": raw_lines,
    }


def parse_kv_overrides(set_args: Sequence[str], set_json: Optional[str]) -> Dict[str, str]:
    """Parse --set Key=Val (preserving commas inside string values) and --set-json."""
    overrides: Dict[str, str] = {}

    if set_json:
        if os.path.isfile(set_json):
            payload = json.loads(read_text_auto(set_json))
        else:
            payload = json.loads(set_json)
        if not isinstance(payload, dict):
            raise ValueError("--set-json must decode to a JSON object {key: value}")
        for k, v in payload.items():
            if isinstance(v, bool):
                overrides[str(k)] = "true" if v else "false"
            else:
                overrides[str(k)] = str(v)

    for item in set_args or []:
        # Split on comma only when followed by Identifier= so comma-lists inside string values survive
        chunks = re.split(r",(?=\s*[A-Za-z_]\w*\s*=)", item)
        for chunk in chunks:
            chunk = chunk.strip()
            if not chunk:
                continue
            if "=" not in chunk:
                raise ValueError(f"Invalid --set item '{chunk}' (expected Key=Value)")
            k, v = chunk.split("=", 1)
            overrides[k.strip()] = v.strip()

    return overrides


def parse_opt_overrides(opt_args: Sequence[str]) -> Dict[str, Tuple[str, str, str, str]]:
    """Parse --opt Key=start:step:stop (or Key=start:step:stop:Y/N or Key=off)."""
    opts: Dict[str, Tuple[str, str, str, str]] = {}
    for item in opt_args or []:
        if "=" not in item:
            raise ValueError(f"Invalid --opt item '{item}' (expected Key=start:step:stop)")
        k, spec = item.split("=", 1)
        k = k.strip()
        spec = spec.strip()
        if spec.lower() in ("off", "n", "false", "0"):
            opts[k] = ("", "", "", "N")
            continue
        parts = spec.split(":")
        if len(parts) == 3:
            opts[k] = (parts[0].strip(), parts[1].strip(), parts[2].strip(), "Y")
        elif len(parts) == 4:
            opts[k] = (
                parts[0].strip(),
                parts[1].strip(),
                parts[2].strip(),
                parts[3].strip().upper(),
            )
        else:
            raise ValueError(
                f"Invalid --opt range '{item}' (expected Key=start:step:stop or Key=off)"
            )
    return opts


def _ensure_default_tail(
    entry: Dict[str, Any],
    key: Optional[str] = None,
    source_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Synthesize a valid MT5 optimization tail (||start||step||stop||N) when absent."""
    if entry.get("has_tail"):
        return entry

    inp = source_meta["inputs_by_name"].get(key) if (source_meta and key) else None
    opt_flag = entry.get("optimize") or "N"
    val_str = str(entry.get("value", "")).strip()

    if inp and inp.get("is_enum"):
        ev = inp.get("enum_values") or {}
        min_ord = min(ev.values()) if ev else 0
        max_ord = max(ev.values()) if ev else 0
        entry.update(
            {
                "start": str(min_ord),
                "step": "0",
                "stop": str(max_ord),
                "optimize": opt_flag,
                "has_tail": True,
            }
        )
        return entry

    dtype = inp["type"] if inp else _infer_input_type(val_str)

    if dtype == "bool" or val_str.lower() in ("true", "false"):
        entry.update(
            {
                "start": "false",
                "step": "0",
                "stop": "true",
                "optimize": opt_flag,
                "has_tail": True,
            }
        )
    elif dtype in ("int", "long", "uint", "ulong", "short", "ushort", "char", "uchar", "int/enum") or re.match(
        r"^[+-]?\d+$", val_str
    ):
        try:
            n = int(val_str, 0)
            stop_n = n * 10 if n != 0 else 10
            entry.update(
                {
                    "start": str(n),
                    "step": "1",
                    "stop": str(stop_n),
                    "optimize": opt_flag,
                    "has_tail": True,
                }
            )
        except ValueError:
            entry["has_tail"] = False
    elif dtype in ("double", "float") or re.match(r"^[+-]?\d+\.\d+$", val_str):
        try:
            f = float(val_str)
            step_f = abs(f) / 10.0 if f != 0.0 else 0.1
            stop_f = f * 10.0 if f != 0.0 else 1.0
            entry.update(
                {
                    "start": val_str,
                    "step": f"{step_f:.6f}",
                    "stop": f"{stop_f:.6f}",
                    "optimize": opt_flag,
                    "has_tail": True,
                }
            )
        except ValueError:
            entry["has_tail"] = False
    else:
        entry["has_tail"] = False

    return entry


def resolve_override_value(
    key: str,
    raw_val: str,
    source_meta: Optional[Dict[str, Any]],
    existing_val: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """Resolve boolean normalization and symbolic enum names to ordinals, validating types."""
    notes: Optional[str] = None
    val = raw_val
    expected_type = ""

    if source_meta:
        inp = source_meta["inputs_by_name"].get(key)
        if inp:
            dtype = inp["type"]
            expected_type = "enum" if inp.get("is_enum") else dtype
            if dtype == "bool" and val.lower() in ("true", "false", "1", "0"):
                val = "true" if val.lower() in ("true", "1") else "false"
                return val, None
            if inp.get("is_enum"):
                ev = inp["enum_values"]
                if val in ev:
                    ord_val = str(ev[val])
                    return ord_val, f"{val} -> {ord_val}"
                # Case-insensitive fallback match on enum member
                ci = {k.lower(): (k, v) for k, v in ev.items()}
                if val.lower() in ci:
                    canon, num = ci[val.lower()]
                    return str(num), f"{canon} -> {num}"
                # Try integer validation
                try:
                    num = int(val, 0)
                    rev = source_meta["enums"][dtype]["reverse"]
                    if num in rev:
                        return str(num), f"{num} ({rev[num]})"
                    return str(num), None
                except ValueError:
                    valid = ", ".join(f"{k}={v}" for k, v in list(ev.items())[:12])
                    raise ValueError(
                        f"Unknown enum member '{val}' for {key} ({dtype}). Examples: {valid}"
                    )

        # Even if key isn't in inputs_by_name, check all known enums for an exact symbolic match
        for ename, einfo in source_meta["enums"].items():
            if val in einfo["values"]:
                ord_val = str(einfo["values"][val])
                return ord_val, f"{val} ({ename}) -> {ord_val}"

    if not expected_type and existing_val is not None and existing_val != "":
        expected_type = _infer_input_type(existing_val)

    if not source_meta and expected_type in ("int/enum", ""):
        for ename, emap in BUILTIN_ENUMS.items():
            if val in emap:
                ord_val = str(emap[val])
                return ord_val, f"{val} ({ename}) -> {ord_val}"
            ci_b = {k.lower(): (k, v) for k, v in emap.items()}
            if val.lower() in ci_b:
                canon, num = ci_b[val.lower()]
                return str(num), f"{canon} ({ename}) -> {num}"

    if expected_type == "bool":
        if val.lower() in ("true", "false", "1", "0"):
            val = "true" if val.lower() in ("true", "1") else "false"
            return val, notes
        raise ValueError(
            f"Invalid boolean value '{raw_val}' for input '{key}' (expected true/false or 1/0)."
        )

    if expected_type in (
        "int/enum",
        "enum",
        "int",
        "long",
        "uint",
        "ulong",
        "short",
        "ushort",
        "char",
        "uchar",
        "color",
    ):
        try:
            int(val, 0)
        except ValueError:
            if not source_meta:
                raise ValueError(
                    f"Cannot set numeric/enum input '{key}' (base value '{existing_val}') to symbolic name '{raw_val}' without --source <EA.mq5>. "
                    f"Pass --source <path/to/EA.mq5> to resolve enum names to integer ordinals, or pass the numeric ordinal directly."
                )
            raise ValueError(
                f"Invalid integer/enum value '{raw_val}' for input '{key}' (type {expected_type})."
            )

    if expected_type in ("double", "float"):
        try:
            float(val)
        except ValueError:
            raise ValueError(
                f"Invalid numeric value '{raw_val}' for floating-point input '{key}' (base value '{existing_val}')."
            )

    if val.lower() in ("true", "false"):
        val = val.lower()
    return val, notes


# ---------------------------------------------------------------------------
# Multi-MT5 Discovery, Binding, Portable Provisioning & Init
# ---------------------------------------------------------------------------


def _count_files(directory: str, extensions: Tuple[str, ...], recursive: bool = False) -> int:
    if not os.path.isdir(directory):
        return 0
    count = 0
    if recursive:
        for _, _, files in os.walk(directory):
            for f in files:
                if f.lower().endswith(extensions):
                    count += 1
    else:
        for f in os.listdir(directory):
            if f.lower().endswith(extensions):
                count += 1
    return count


def _list_broker_folders(data_dir: str) -> List[str]:
    bases_dir = os.path.join(data_dir, "bases")
    if not os.path.isdir(bases_dir):
        bases_dir = os.path.join(data_dir, "Bases")
    if not os.path.isdir(bases_dir):
        return []
    skip = {"default", "signals", "chats", "custom"}
    found: List[str] = []
    for entry in sorted(os.listdir(bases_dir)):
        full = os.path.join(bases_dir, entry)
        if os.path.isdir(full) and entry.lower() not in skip:
            found.append(entry)
    return found


def _build_instance_record(
    terminal_dir: str,
    data_dir: str,
    mode: str,
    guid: Optional[str] = None,
) -> Dict[str, Any]:
    t_norm = os.path.normpath(terminal_dir) if terminal_dir else ""
    d_norm = os.path.normpath(data_dir) if data_dir else ""
    is_portable = mode == "portable" or (
        bool(t_norm)
        and bool(d_norm)
        and os.path.normcase(os.path.abspath(t_norm)) == os.path.normcase(os.path.abspath(d_norm))
    )
    tester_dir = (
        os.path.join(d_norm, "Tester")
        if is_portable
        else (
            os.path.join(os.environ.get("APPDATA", ""), "MetaQuotes", "Tester", guid)
            if guid
            else os.path.join(d_norm, "Tester")
        )
    )
    pt_dir = os.path.join(d_norm, "MQL5", "Profiles", "Tester")
    presets_dir = os.path.join(d_norm, "MQL5", "Presets")
    experts_dir = os.path.join(d_norm, "MQL5", "Experts")

    profiles_cnt = _count_files(pt_dir, (".ini",), recursive=False)
    sets_cnt = _count_files(pt_dir, (".set",), recursive=False) + _count_files(
        presets_dir, (".set",), recursive=True
    )
    experts_cnt = _count_files(experts_dir, (".ex5",), recursive=True)

    return {
        "mode": "portable" if is_portable else mode,
        "portable": is_portable,
        "guid": guid or "",
        "terminal_dir": t_norm,
        "terminal_exe_exists": bool(t_norm and os.path.isfile(os.path.join(t_norm, "terminal64.exe"))),
        "metaeditor_exe_exists": bool(t_norm and os.path.isfile(os.path.join(t_norm, "MetaEditor64.exe"))),
        "data_dir": d_norm,
        "mql5_exists": bool(d_norm and os.path.isdir(os.path.join(d_norm, "MQL5"))),
        "tester_dir": os.path.normpath(tester_dir) if tester_dir else "",
        "servers": _list_broker_folders(d_norm) if d_norm else [],
        "profiles_count": profiles_cnt,
        "sets_count": sets_cnt,
        "experts_count": experts_cnt,
    }


def discover_mt5_instances(
    project_dir: Optional[str] = None,
    scan_dirs: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Discover all MT5 installations, AppData data folders, and portable runtimes."""
    appdata = os.environ.get("APPDATA", "")
    proj_dir = os.path.abspath(project_dir or os.getcwd())
    instances: List[Dict[str, Any]] = []
    seen_pairs: set[Tuple[str, str]] = set()

    def _add(rec: Dict[str, Any]) -> None:
        key = (
            os.path.normcase(os.path.abspath(rec["terminal_dir"])) if rec["terminal_dir"] else "",
            os.path.normcase(os.path.abspath(rec["data_dir"])) if rec["data_dir"] else "",
        )
        if key in seen_pairs:
            return
        seen_pairs.add(key)
        instances.append(rec)

    # 1. Scan explicit/project/Program Files folders for terminal64.exe
    candidate_installs: List[str] = []
    for root_base in (
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ):
        if root_base and os.path.isdir(root_base):
            for entry in sorted(os.listdir(root_base)):
                if "metatrader" in entry.lower() or "mt5" in entry.lower():
                    candidate_installs.append(os.path.join(root_base, entry))
    candidate_installs.append(os.path.join(proj_dir, ".mt5-portable"))
    for sd in scan_dirs or []:
        if sd:
            candidate_installs.append(os.path.abspath(sd))

    # 2. Scan AppData\MetaQuotes\Terminal\<GUID>
    term_root = os.path.join(appdata, "MetaQuotes", "Terminal")
    if os.path.isdir(term_root):
        for guid in sorted(os.listdir(term_root)):
            if guid.lower() in ("common", "community"):
                continue
            data_path = os.path.join(term_root, guid)
            if not os.path.isdir(data_path):
                continue
            origin_file = os.path.join(data_path, "origin.txt")
            origin_dir = ""
            if os.path.isfile(origin_file):
                origin_dir = read_text_auto(origin_file).strip()

            origin_has_exe = bool(
                origin_dir and os.path.isfile(os.path.join(origin_dir, "terminal64.exe"))
            )
            origin_has_mql5 = bool(origin_dir and os.path.isdir(os.path.join(origin_dir, "MQL5")))

            if origin_has_exe and origin_has_mql5:
                # origin_dir is itself a self-contained portable install
                _add(_build_instance_record(origin_dir, origin_dir, "portable"))
                # Include AppData GUID too only if it holds its own MQL5 artifacts
                if os.path.isdir(os.path.join(data_path, "MQL5")):
                    rec_app = _build_instance_record(origin_dir, data_path, "standard", guid=guid)
                    if rec_app["profiles_count"] or rec_app["sets_count"] or rec_app["experts_count"]:
                        _add(rec_app)
            elif origin_has_exe:
                _add(_build_instance_record(origin_dir, data_path, "standard", guid=guid))
            else:
                mode = "data-only" if os.path.isdir(os.path.join(data_path, "MQL5")) else "stale-data"
                _add(_build_instance_record(origin_dir, data_path, mode, guid=guid))

    for inst_dir in candidate_installs:
        if os.path.isfile(os.path.join(inst_dir, "terminal64.exe")):
            if os.path.isdir(os.path.join(inst_dir, "MQL5")):
                _add(_build_instance_record(inst_dir, inst_dir, "portable"))
            else:
                # Standard install with no AppData link discovered yet
                already_used = any(
                    os.path.normcase(os.path.abspath(r["terminal_dir"]))
                    == os.path.normcase(os.path.abspath(inst_dir))
                    for r in instances
                    if r["terminal_dir"]
                )
                if not already_used:
                    _add(_build_instance_record(inst_dir, "", "install-only"))

    # Prioritize runnable installs with profiles/experts first, then portable, then stale/unlinked
    def _rank(r: Dict[str, Any]) -> Tuple[int, int, int, str]:
        runnable = 0 if (r["terminal_exe_exists"] and r["mql5_exists"]) else 1
        has_profiles = 0 if r["profiles_count"] > 0 else (1 if r["experts_count"] > 0 else 2)
        pref_mode = 0 if (r["mode"] == "portable" or r["profiles_count"] > 0) else 1
        return (runnable, has_profiles, pref_mode, r["data_dir"] or r["terminal_dir"])

    instances.sort(key=_rank)
    for idx, rec in enumerate(instances, start=1):
        rec["index"] = idx
    return instances


def cmd_discover(args: argparse.Namespace) -> int:
    paths = resolve_env_paths(args)
    instances = discover_mt5_instances(paths["project_dir"], args.scan_dir)
    active_t = os.path.normcase(os.path.abspath(paths["terminal_dir"]))
    active_d = os.path.normcase(os.path.abspath(paths["data_dir"]))
    for rec in instances:
        rt = os.path.normcase(os.path.abspath(rec["terminal_dir"])) if rec["terminal_dir"] else ""
        rd = os.path.normcase(os.path.abspath(rec["data_dir"])) if rec["data_dir"] else ""
        rec["active"] = (rt == active_t and rd == active_d)

    if args.json:
        print(
            json.dumps(
                {
                    "project_dir": paths["project_dir"],
                    "binding_file": paths["binding_file"] or None,
                    "instances": instances,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    print("=== Discovered MT5 Installations & Data Folders ===")
    print(f"Project Dir : {paths['project_dir']}")
    print(f"Binding     : {paths['binding_file'] or '<none - using defaults>'}")
    print("")
    if not instances:
        print("No MT5 installations or data folders found.")
        return 0

    for rec in instances:
        active_tag = " [ACTIVE]" if rec["active"] else ""
        exe_tag = "EXE:OK" if rec["terminal_exe_exists"] else "EXE:MISSING"
        mql_tag = "MQL5:OK" if rec["mql5_exists"] else "MQL5:NO"
        srv_txt = ", ".join(rec["servers"][:5]) if rec["servers"] else "-"
        print(
            f"[{rec['index']}] {rec['mode'].upper()}{active_tag}  ({exe_tag}, {mql_tag})"
        )
        print(f"    Terminal : {rec['terminal_dir'] or '<none>'}")
        print(f"    Data Dir : {rec['data_dir'] or '<none>'}")
        print(
            f"    Assets   : profiles={rec['profiles_count']}, sets={rec['sets_count']}, experts={rec['experts_count']}, servers=[{srv_txt}]"
        )
    print("\nNext steps:")
    print("  Bind existing instance : python scripts\\mt5_headless.py bind --select <N>")
    print("  Create local portable  : python scripts\\mt5_headless.py portable --from-select <N>")
    return 0


def _write_project_binding(
    project_dir: str,
    terminal_dir: str,
    data_dir: str,
    tester_dir: str,
    portable: bool,
    launch_mode: str = "minimized",
) -> str:
    proj_abs = os.path.abspath(project_dir)

    def _rel_if_inside(p: str) -> str:
        p_abs = os.path.abspath(p)
        try:
            rel = os.path.relpath(p_abs, proj_abs)
            if not rel.startswith(".."):
                return rel
        except ValueError:
            pass
        return p_abs

    cfg = {
        "portable": bool(portable),
        "launch_mode": launch_mode if launch_mode in LAUNCH_MODES else "minimized",
        "terminal_dir": _rel_if_inside(terminal_dir),
        "data_dir": _rel_if_inside(data_dir),
        "tester_dir": _rel_if_inside(tester_dir),
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    out_path = os.path.join(proj_abs, BINDING_FILENAME)
    write_utf8(out_path, json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    return out_path


def cmd_bind(args: argparse.Namespace) -> int:
    proj_dir = os.path.abspath(args.project_dir or os.getcwd())
    instances = discover_mt5_instances(proj_dir, args.scan_dir)

    terminal_dir = args.terminal_dir
    data_dir = args.data_dir
    tester_dir = args.tester_dir
    portable = bool(args.portable)

    if args.portable_dir:
        pdir = os.path.abspath(args.portable_dir)
        terminal_dir = pdir
        data_dir = pdir
        tester_dir = os.path.join(pdir, "Tester")
        portable = True
    elif args.select is not None:
        match = next((r for r in instances if r["index"] == args.select), None)
        if not match:
            raise ValueError(
                f"Invalid --select {args.select}; run 'discover' to see valid indices 1..{len(instances)}."
            )
        if not match["terminal_dir"] or not match["data_dir"]:
            raise ValueError(
                f"Selected instance [{args.select}] is incomplete (missing terminal_dir or data_dir)."
            )
        terminal_dir = match["terminal_dir"]
        data_dir = match["data_dir"]
        tester_dir = match["tester_dir"]
        portable = match["portable"]

    if not terminal_dir or not data_dir:
        raise ValueError("Provide --select <N>, --portable-dir <dir>, or both --terminal-dir and --data-dir")

    if not tester_dir:
        if portable:
            tester_dir = os.path.join(data_dir, "Tester")
        else:
            guid = os.path.basename(os.path.normpath(data_dir)) or DEFAULT_DATA_GUID
            tester_dir = os.path.join(os.environ.get("APPDATA", ""), "MetaQuotes", "Tester", guid)

    launch_mode = getattr(args, "launch_mode", None) or "minimized"
    binding_path = _write_project_binding(
        proj_dir, terminal_dir, data_dir, tester_dir, portable, launch_mode=launch_mode
    )
    prot = ensure_git_and_env_protection(
        proj_dir,
        create_dotenv=args.init_env,
        portable_dir=data_dir if portable else None,
    )

    payload = {
        "binding_file": binding_path,
        "portable": portable,
        "launch_mode": launch_mode,
        "terminal_dir": os.path.abspath(terminal_dir),
        "data_dir": os.path.abspath(data_dir),
        "tester_dir": os.path.abspath(tester_dir),
        "git_protection": prot,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print(f"Bound project : {proj_dir}")
    print(f"Config File   : {binding_path}")
    print(f"Mode          : {'PORTABLE (/portable)' if portable else 'STANDARD'}")
    print(f"Launch Mode   : {launch_mode} (options: {', '.join(LAUNCH_MODES)})")
    print(f"Terminal Dir  : {payload['terminal_dir']}")
    print(f"Data Dir      : {payload['data_dir']}")
    print(f"Tester Dir    : {payload['tester_dir']}")
    print(f".gitignore    : {prot['gitignore']} ({'updated' if prot['gitignore_updated'] else 'already protected'})")
    if prot["git_root"]:
        print(f".git/exclude  : {'updated' if prot['git_exclude_updated'] else 'already protected'}")
    if prot["env_tracked_warning"]:
        print("WARNING       : .env is currently tracked by Git! Run: git rm --cached .env")
    if prot["dotenv_created"]:
        print(f"Created .env  : {prot['dotenv']} (fill in MT5_BROKER_SERVER, MT5_LOGIN, MT5_PASSWORD)")
    return 0


def cmd_portable(args: argparse.Namespace) -> int:
    proj_dir = os.path.abspath(args.project_dir or os.getcwd())
    dest_dir = os.path.abspath(args.dest or os.path.join(proj_dir, ".mt5-portable"))
    instances = discover_mt5_instances(proj_dir, args.scan_dir)

    src_terminal = args.from_terminal_dir
    src_data = args.from_data_dir

    if args.from_select is not None:
        match = next((r for r in instances if r["index"] == args.from_select), None)
        if not match:
            raise ValueError(f"Invalid --from-select {args.from_select}")
        src_terminal = src_terminal or match["terminal_dir"]
        src_data = src_data or match["data_dir"]
    elif not src_terminal:
        best = next(
            (r for r in instances if r["terminal_exe_exists"] and r["mql5_exists"]),
            next((r for r in instances if r["terminal_exe_exists"]), None),
        )
        if best:
            src_terminal = best["terminal_dir"]
            src_data = src_data or best["data_dir"]

    if not src_terminal or not os.path.isfile(os.path.join(src_terminal, "terminal64.exe")):
        raise FileNotFoundError(
            f"Source MT5 installation with terminal64.exe not found: {src_terminal or '<none>'}"
        )

    os.makedirs(dest_dir, exist_ok=True)
    binaries = ["terminal64.exe", "MetaEditor64.exe", "metatester64.exe", "Terminal.ico"]
    copied_bins: List[str] = []
    for bname in binaries:
        src_b = os.path.join(src_terminal, bname)
        dst_b = os.path.join(dest_dir, bname)
        if os.path.isfile(src_b):
            needs_copy = not os.path.isfile(dst_b) or (
                os.path.getsize(src_b) != os.path.getsize(dst_b)
            )
            if needs_copy:
                shutil.copy2(src_b, dst_b)
                copied_bins.append(bname)

    for rel_sub in (
        os.path.join("MQL5", "Experts"),
        os.path.join("MQL5", "Include"),
        os.path.join("MQL5", "Indicators"),
        os.path.join("MQL5", "Libraries"),
        os.path.join("MQL5", "Files"),
        os.path.join("MQL5", "Presets"),
        os.path.join("MQL5", "Profiles", "Tester"),
        os.path.join("MQL5", "Logs"),
        "config",
        "bases",
        "Tester",
        "logs",
    ):
        os.makedirs(os.path.join(dest_dir, rel_sub), exist_ok=True)

    copied_include = False
    copied_servers = False
    if src_data and os.path.isdir(src_data):
        src_inc = os.path.join(src_data, "MQL5", "Include")
        dst_inc = os.path.join(dest_dir, "MQL5", "Include")
        if os.path.isdir(src_inc) and not os.path.isfile(os.path.join(dst_inc, "Trade", "Trade.mqh")):
            shutil.copytree(src_inc, dst_inc, dirs_exist_ok=True)
            copied_include = True

        # Copy broker server lists ONLY (never accounts.dat)
        for cfg_name in ("servers.ini", "brokers.dat"):
            src_cfg = os.path.join(src_data, "config", cfg_name)
            dst_cfg = os.path.join(dest_dir, "config", cfg_name)
            if os.path.isfile(src_cfg) and not os.path.isfile(dst_cfg):
                shutil.copy2(src_cfg, dst_cfg)
                copied_servers = True

        if args.copy_experts:
            src_exp = os.path.join(src_data, "MQL5", "Experts")
            if os.path.isdir(src_exp):
                shutil.copytree(src_exp, os.path.join(dest_dir, "MQL5", "Experts"), dirs_exist_ok=True)

        if args.copy_profiles:
            src_prof = os.path.join(src_data, "MQL5", "Profiles", "Tester")
            if os.path.isdir(src_prof):
                shutil.copytree(
                    src_prof, os.path.join(dest_dir, "MQL5", "Profiles", "Tester"), dirs_exist_ok=True
                )

    tester_dir = os.path.join(dest_dir, "Tester")
    launch_mode = getattr(args, "launch_mode", None) or "minimized"
    binding_path = _write_project_binding(
        proj_dir, dest_dir, dest_dir, tester_dir, portable=True, launch_mode=launch_mode
    )
    prot = ensure_git_and_env_protection(
        proj_dir, create_dotenv=True, portable_dir=dest_dir
    )

    payload = {
        "portable_dir": dest_dir,
        "launch_mode": launch_mode,
        "source_terminal": src_terminal,
        "source_data": src_data or "",
        "copied_binaries": copied_bins,
        "copied_include": copied_include,
        "copied_servers": copied_servers,
        "binding_file": binding_path,
        "git_protection": prot,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print("=== Provisioned Isolated MT5 Portable Environment ===")
    print(f"Portable Dir  : {dest_dir}")
    print(f"Launch Mode   : {launch_mode} (options: {', '.join(LAUNCH_MODES)})")
    print(f"Source Bin    : {src_terminal} (copied: {', '.join(copied_bins) or 'up-to-date'})")
    print(f"Include Tree  : {'copied MQL5\\Include' if copied_include else 'present/skipped'}")
    print(f"Broker List   : {'copied servers.ini/brokers.dat' if copied_servers else 'existing/manual'}")
    print(f"Binding File  : {binding_path}")
    print(f".gitignore    : {prot['gitignore']} ({'updated' if prot['gitignore_updated'] else 'already protected'})")
    if prot["git_root"]:
        print(f".git/exclude  : {'updated' if prot['git_exclude_updated'] else 'already protected'}")
    if prot["env_tracked_warning"]:
        print("WARNING       : .env is currently tracked by Git! Run: git rm --cached .env")
    print(f".env Template : {prot['dotenv']} ({'created' if prot['dotenv_created'] else 'already exists'})")
    print("\nIMPORTANT: Open .env yourself and fill in MT5_BROKER_SERVER, MT5_LOGIN, and MT5_PASSWORD.")
    print("Do NOT ask AI agents to read .env or paste credentials into chat; scripts inject them via env vars.")
    return 0


def cmd_scrub_ini(args: argparse.Namespace) -> int:
    changed = scrub_ini_credentials(args.ini_path)
    print(f"Scrubbed credentials in {os.path.abspath(args.ini_path)}: {changed}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: init
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    paths = resolve_env_paths(args)
    running, proc_list = is_terminal_running(paths["terminal_exe"], paths["portable"])

    profiles: List[str] = []
    sets: List[str] = []
    presets: List[str] = []
    experts: List[str] = []

    pt_dir = paths["profiles_tester_dir"]
    if os.path.isdir(pt_dir):
        for fname in sorted(os.listdir(pt_dir)):
            low = fname.lower()
            if low.endswith(".ini"):
                profiles.append(fname)
            elif low.endswith(".set"):
                sets.append(fname)

    if os.path.isdir(paths["presets_dir"]):
        for root, _, files in os.walk(paths["presets_dir"]):
            for fname in sorted(files):
                if fname.lower().endswith(".set"):
                    rel = os.path.relpath(os.path.join(root, fname), paths["presets_dir"])
                    presets.append(rel)

    if os.path.isdir(paths["experts_dir"]):
        for root, _, files in os.walk(paths["experts_dir"]):
            for fname in sorted(files):
                if fname.lower().endswith(".ex5"):
                    rel = os.path.relpath(os.path.join(root, fname), paths["experts_dir"])
                    experts.append(rel)

    env_auth = {
        "MT5_BROKER_SERVER": bool(os.environ.get("MT5_BROKER_SERVER") or os.environ.get("MT5_SERVER")),
        "MT5_LOGIN": bool(os.environ.get("MT5_LOGIN") or os.environ.get("MT5_USERNAME")),
        "MT5_PASSWORD": bool(os.environ.get("MT5_PASSWORD")),
    }

    sess_id = _get_win32_session_id()
    payload = {
        "paths": paths,
        "session_id": sess_id,
        "exists": {
            "terminal_exe": os.path.isfile(paths["terminal_exe"]),
            "metaeditor_exe": os.path.isfile(paths["metaeditor_exe"]),
            "data_dir": os.path.isdir(paths["data_dir"]),
            "profiles_tester_dir": os.path.isdir(pt_dir),
            "tester_dir": os.path.isdir(paths["tester_dir"]),
        },
        "env_auth_set": env_auth,
        "terminal_running": running,
        "terminal_processes": proc_list,
        "profiles": profiles,
        "tester_sets": sets,
        "presets": presets,
        "compiled_experts": experts,
    }

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    sess_desc = (
        "Session 0 (non-interactive; terminal64.exe will auto-bridge via interactive scheduled task)"
        if sess_id == 0
        else f"Session {sess_id} (interactive desktop)"
    )
    print("=== MT5 Headless Tester Environment ===")
    print(f"Project Dir      : {paths['project_dir']}")
    print(f"Binding File     : {paths['binding_file'] or '<none - using defaults>'}")
    print(f"Mode             : {'PORTABLE (/portable)' if paths['portable'] else 'STANDARD'}")
    print(f"Win32 Session    : {sess_desc}")
    print(f"Launch Mode      : {paths['launch_mode']} (options: {', '.join(LAUNCH_MODES)})")
    print(f"Terminal EXE     : {paths['terminal_exe']} [{'OK' if payload['exists']['terminal_exe'] else 'MISSING'}]")
    print(f"MetaEditor EXE   : {paths['metaeditor_exe']} [{'OK' if payload['exists']['metaeditor_exe'] else 'MISSING'}]")
    print(f"Data Dir         : {paths['data_dir']} [{'OK' if payload['exists']['data_dir'] else 'MISSING'}]")
    print(f"Profiles\\Tester  : {pt_dir} [{'OK' if payload['exists']['profiles_tester_dir'] else 'MISSING'}]")
    print(f"Tester Agents    : {paths['tester_dir']} [{'OK' if payload['exists']['tester_dir'] else 'MISSING'}]")
    print(f"Scratch Work Dir : {paths['work_dir']}")
    print(
        "Env Auth Vars    : "
        + f"SERVER={'[SET]' if env_auth['MT5_BROKER_SERVER'] else '[UNSET]'}, "
        + f"LOGIN={'[SET]' if env_auth['MT5_LOGIN'] else '[UNSET]'}, "
        + f"PASSWORD={'[SET]' if env_auth['MT5_PASSWORD'] else '[UNSET]'}"
    )
    print(
        f"terminal64.exe   : {'BLOCKED - matching terminal running (close via UI; NEVER kill)' if running else ('OTHER INSTANCE RUNNING (portable target is free)' if proc_list else 'NOT RUNNING (safe to launch headless test)')}"
    )
    for pr in proc_list:
        print(f"  - PID {pr['pid']}: {pr['exe']}")
    print(f"\nProfiles ({len(profiles)}):")
    for p in profiles[:25]:
        print(f"  - {p}")
    if len(profiles) > 25:
        print(f"  ... ({len(profiles) - 25} more)")
    print(f"\nTester .set Files ({len(sets)}):")
    for s in sets[:25]:
        print(f"  - {s}")
    if presets:
        print(f"\nPresets .set Files ({len(presets)}):")
        for s in presets[:25]:
            print(f"  - {s}")
    print(f"\nCompiled Experts (.ex5) ({len(experts)}):")
    for e in experts[:30]:
        print(f"  - {e}")
    if len(experts) > 30:
        print(f"  ... ({len(experts) - 30} more)")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: inspect
# ---------------------------------------------------------------------------


def cmd_inspect(args: argparse.Namespace) -> int:
    paths = resolve_env_paths(args)
    if not args.source and not args.profile and not args.set_file and not args.report:
        raise ValueError("Provide at least one of --source, --profile, --set-file, or --report")

    source_meta = (
        parse_mql5_source(args.source, os.path.join(paths["mql5_dir"], "Include"))
        if args.source
        else None
    )

    loaded_cfg = None
    loaded_rep = None
    if args.profile:
        loaded_cfg = parse_ini_or_set(resolve_profile_path(args.profile, paths))
    elif args.set_file:
        loaded_cfg = parse_ini_or_set(resolve_set_path(args.set_file, paths))
    elif args.report:
        loaded_rep = parse_htm_report(args.report, paths)

    search_re = re.compile(args.search, re.I) if args.search else None
    group_re = re.compile(args.group, re.I) if args.group else None

    if args.enums_only:
        if not source_meta:
            raise ValueError("--enums-only requires --source")
        enums_out: Dict[str, Any] = {}
        for ename, einfo in source_meta["enums"].items():
            if search_re and not (
                search_re.search(ename)
                or any(search_re.search(m) for m in einfo["values"])
            ):
                continue
            enums_out[ename] = {
                "file": einfo["file"],
                "members": [
                    {
                        "ordinal": val,
                        "name": mname,
                        "comment": einfo["comments"].get(mname, ""),
                    }
                    for mname, val in einfo["values"].items()
                ],
            }
        output_text = _format_inspect_enums(enums_out, args.format)
        return _emit_text_or_file(output_text, args.out)

    records: List[Dict[str, Any]] = []
    if loaded_rep:
        for meta_item in loaded_rep.get("inputs_meta", []):
            key = meta_item["name"]
            val = meta_item["value"]
            src_info = source_meta["inputs_by_name"].get(key) if source_meta else None
            resolved_sym = None
            dtype = src_info["type"] if src_info else meta_item.get("inferred_type", "")
            group = (src_info["group"] if src_info and src_info.get("group") else meta_item.get("group", ""))
            comment = src_info["comment"] if src_info else ""
            if src_info and src_info.get("is_enum"):
                try:
                    ord_int = int(val, 0)
                    resolved_sym = source_meta["enums"][dtype]["reverse"].get(ord_int)
                except ValueError:
                    pass
            rec = {
                "name": key,
                "value": val,
                "resolved_enum": resolved_sym,
                "type": dtype,
                "group": group,
                "comment": comment,
                "optimize": "N",
                "range": "",
            }
            if _matches_filters(rec, search_re, group_re):
                records.append(rec)
    elif loaded_cfg:
        for key, entry in loaded_cfg["inputs"].items():
            src_info = source_meta["inputs_by_name"].get(key) if source_meta else None
            resolved_sym = None
            dtype = src_info["type"] if src_info else _infer_input_type(entry["value"])
            group = src_info["group"] if src_info else ""
            comment = src_info["comment"] if src_info else ""
            if src_info and src_info.get("is_enum"):
                try:
                    ord_int = int(entry["value"], 0)
                    resolved_sym = source_meta["enums"][dtype]["reverse"].get(ord_int)
                except ValueError:
                    pass
            rec = {
                "name": key,
                "value": entry["value"],
                "resolved_enum": resolved_sym,
                "type": dtype,
                "group": group,
                "comment": comment,
                "optimize": entry["optimize"],
                "range": (
                    f"{entry['start']}..{entry['stop']} (step {entry['step']})"
                    if entry["has_tail"]
                    else ""
                ),
            }
            if _matches_filters(rec, search_re, group_re):
                records.append(rec)
    elif source_meta:
        for inp in source_meta["inputs"]:
            rec = {
                "name": inp["name"],
                "value": (
                    str(inp["default_ordinal"])
                    if inp.get("is_enum") and inp.get("default_ordinal") is not None
                    else inp["default"]
                ),
                "resolved_enum": inp.get("default_symbol"),
                "type": inp["type"],
                "group": inp["group"],
                "comment": inp["comment"],
                "optimize": "N",
                "range": "",
                "enum_values": inp.get("enum_values") if inp.get("is_enum") else None,
            }
            if _matches_filters(rec, search_re, group_re):
                records.append(rec)

    meta_header = {
        "source": os.path.abspath(args.source) if args.source else None,
        "profile_or_set": (
            loaded_cfg["path"]
            if loaded_cfg
            else (loaded_rep["source_htm"] if loaded_rep else None)
        ),
        "tester": (
            loaded_cfg["tester"]
            if loaded_cfg
            else (loaded_rep["settings"] if loaded_rep else {})
        ),
        "count": len(records),
    }
    output_text = _format_inspect_inputs(records, meta_header, args.format)
    return _emit_text_or_file(output_text, args.out)


def _matches_filters(
    rec: Dict[str, Any],
    search_re: Optional[re.Pattern[str]],
    group_re: Optional[re.Pattern[str]],
) -> bool:
    if group_re and not group_re.search(rec.get("group", "")):
        return False
    if search_re:
        haystack = " ".join(
            [
                rec.get("name", ""),
                rec.get("type", ""),
                rec.get("group", ""),
                rec.get("comment", ""),
                str(rec.get("value", "")),
                str(rec.get("resolved_enum") or ""),
            ]
        )
        if not search_re.search(haystack):
            return False
    return True


def _format_inspect_enums(enums_out: Dict[str, Any], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(enums_out, indent=2, ensure_ascii=False)
    lines: List[str] = []
    if fmt == "md":
        for ename, info in enums_out.items():
            lines.append(f"### `{ename}`")
            lines.append("")
            lines.append("| Ordinal | Member | Comment |")
            lines.append("|---:|---|---|")
            for m in info["members"]:
                lines.append(f"| {m['ordinal']} | `{m['name']}` | {m['comment']} |")
            lines.append("")
        return "\n".join(lines)

    for ename, info in enums_out.items():
        lines.append(f"[{ename}] ({info['file']})")
        for m in info["members"]:
            cmt = f"  // {m['comment']}" if m["comment"] else ""
            lines.append(f"  {m['ordinal']:>3} = {m['name']}{cmt}")
        lines.append("")
    return "\n".join(lines)


def _format_inspect_inputs(
    records: List[Dict[str, Any]], meta: Dict[str, Any], fmt: str
) -> str:
    if fmt == "json":
        return json.dumps({"meta": meta, "inputs": records}, indent=2, ensure_ascii=False)
    if fmt == "md":
        lines = [f"# EA Inputs ({meta['count']} items)", ""]
        if meta.get("tester"):
            lines.append("## `[Tester]` Settings")
            lines.append("")
            lines.append("| Key | Value |")
            lines.append("|---|---|")
            for k, v in meta["tester"].items():
                lines.append(f"| `{k}` | `{v}` |")
            lines.append("")
        lines.append("| Name | Value | Type / Enum | Group | Comment |")
        lines.append("|---|---|---|---|---|")
        for r in records:
            val_disp = f"`{r['value']}`"
            if r.get("resolved_enum"):
                val_disp += f" (`{r['resolved_enum']}`)"
            lines.append(
                f"| `{r['name']}` | {val_disp} | `{r.get('type','')}` | {r.get('group','')} | {r.get('comment','')} |"
            )
        return "\n".join(lines)

    lines = [f"=== Inspected Inputs ({meta['count']}) ==="]
    if meta.get("tester"):
        lines.append("[Tester]")
        for k, v in meta["tester"].items():
            lines.append(f"  {k}={v}")
        lines.append("")
    last_group = None
    for r in records:
        grp = r.get("group") or ""
        if grp and grp != last_group:
            lines.append(f"\n--- {grp} ---")
            last_group = grp
        enum_txt = f" ({r['resolved_enum']})" if r.get("resolved_enum") else ""
        type_txt = f" [{r['type']}]" if r.get("type") else ""
        cmt_txt = f"  // {r['comment']}" if r.get("comment") else ""
        opt_txt = f"  [OPT: {r['range']}]" if r.get("optimize") == "Y" else ""
        lines.append(f"  {r['name']}={r['value']}{enum_txt}{type_txt}{opt_txt}{cmt_txt}")
    return "\n".join(lines)


def _emit_text_or_file(content: str, out_path: Optional[str]) -> int:
    if out_path:
        write_utf8(out_path, content)
        print(f"Wrote: {os.path.abspath(out_path)}")
    else:
        print(content)
    return 0


# ---------------------------------------------------------------------------
# Subcommand: set
# ---------------------------------------------------------------------------


def _build_base_inputs(
    paths: Dict[str, str],
    from_profile: Optional[str],
    from_set: Optional[str],
    from_source: Optional[str],
    source_ref: Optional[str],
    from_report: Optional[str] = None,
) -> Tuple[Dict[str, Dict[str, Any]], Optional[Dict[str, str]], Optional[Dict[str, Any]]]:
    """Assemble base [TesterInputs], optional [Tester], and optional parsed MQL5 metadata."""
    ref_path = source_ref or from_source
    source_meta = (
        parse_mql5_source(ref_path, os.path.join(paths["mql5_dir"], "Include"))
        if ref_path
        else None
    )

    inputs: Dict[str, Dict[str, Any]] = {}
    tester: Dict[str, str] = {}

    if from_source and source_meta:
        for inp in source_meta["inputs"]:
            if inp.get("is_enum") and inp.get("default_ordinal") is not None:
                val = str(inp["default_ordinal"])
            else:
                val = inp["default"]
            inputs[inp["name"]] = _ensure_default_tail(
                parse_input_entry(val), inp["name"], source_meta
            )

    if from_report:
        rep_data = parse_htm_report(from_report, paths)
        for k, v in rep_data.get("inputs", {}).items():
            inputs[k] = _ensure_default_tail(parse_input_entry(v), k, source_meta)

    if from_profile:
        prof = parse_ini_or_set(resolve_profile_path(from_profile, paths))
        tester.update(prof["tester"])
        inputs.update(prof["inputs"])

    if from_set:
        st = parse_ini_or_set(resolve_set_path(from_set, paths))
        inputs.update(st["inputs"])

    return inputs, tester, source_meta


def _apply_input_overrides(
    inputs: Dict[str, Dict[str, Any]],
    overrides: Dict[str, str],
    opt_overrides: Dict[str, Tuple[str, str, str, str]],
    source_meta: Optional[Dict[str, Any]],
    allow_new: bool,
) -> List[str]:
    """Validate and apply --set and --opt overrides in-place. Returns change notes."""
    change_notes: List[str] = []
    known_keys = list(inputs.keys())

    for key, raw_val in overrides.items():
        if key not in inputs:
            if source_meta and key in source_meta["inputs_by_name"] and not inputs:
                inputs[key] = parse_input_entry("")
            elif not allow_new:
                suggestions = difflib.get_close_matches(key, known_keys, n=5, cutoff=0.5)
                hint = f" Did you mean: {', '.join(suggestions)}" if suggestions else ""
                raise KeyError(f"Input '{key}' not present in base profile/set.{hint}")
            else:
                inputs[key] = parse_input_entry("")

        entry = inputs[key]
        resolved_val, enum_note = resolve_override_value(
            key, raw_val, source_meta, existing_val=entry.get("value")
        )
        old_val = entry["value"]
        entry["value"] = resolved_val
        _ensure_default_tail(entry, key, source_meta)
        note = f"{key}: {old_val} -> {resolved_val}"
        if enum_note:
            note += f" ({enum_note})"
        change_notes.append(note)

    for key, (start, step, stop, opt_flag) in opt_overrides.items():
        if key not in inputs:
            if source_meta and key in source_meta["inputs_by_name"] and not inputs:
                inputs[key] = parse_input_entry("")
            elif not allow_new:
                suggestions = difflib.get_close_matches(key, known_keys, n=5, cutoff=0.5)
                hint = f" Did you mean: {', '.join(suggestions)}" if suggestions else ""
                raise KeyError(f"Input '{key}' not present in base profile/set.{hint}")
            else:
                inputs[key] = parse_input_entry("")
        entry = inputs[key]
        if opt_flag == "N":
            entry["optimize"] = "N"
            change_notes.append(f"{key}: optimization disabled")
        else:
            r_start, _ = resolve_override_value(
                key, start, source_meta, existing_val=entry.get("value")
            )
            r_stop, _ = resolve_override_value(
                key, stop, source_meta, existing_val=entry.get("value")
            )
            for bound_name, bound_val in (("start", r_start), ("stop", r_stop)):
                if bound_val.lower() not in ("true", "false"):
                    try:
                        float(bound_val)
                    except ValueError:
                        raise ValueError(
                            f"Optimization {bound_name} value '{bound_val}' for '{key}' could not be resolved to a number or boolean."
                        )
            if not entry["value"]:
                entry["value"] = r_start
            entry["start"] = r_start
            entry["step"] = step
            entry["stop"] = r_stop
            entry["optimize"] = opt_flag
            entry["has_tail"] = True
            change_notes.append(f"{key}: opt={r_start}||{step}||{r_stop}||{opt_flag}")

    return change_notes


def _diff_set_files(
    path_a: str,
    path_b: str,
    paths: Dict[str, str],
    source_meta: Optional[Dict[str, Any]],
    fmt: str,
    out_path: Optional[str],
) -> int:
    a_cfg = parse_ini_or_set(
        resolve_profile_path(path_a, paths)
        if path_a.lower().endswith(".ini") and not os.path.isfile(path_a)
        else (resolve_set_path(path_a, paths) if not os.path.isfile(path_a) else path_a)
    )
    b_cfg = parse_ini_or_set(
        resolve_profile_path(path_b, paths)
        if path_b.lower().endswith(".ini") and not os.path.isfile(path_b)
        else (resolve_set_path(path_b, paths) if not os.path.isfile(path_b) else path_b)
    )

    all_keys = list(dict.fromkeys(list(a_cfg["inputs"].keys()) + list(b_cfg["inputs"].keys())))
    diffs: List[Dict[str, Any]] = []
    for k in all_keys:
        ea = a_cfg["inputs"].get(k)
        eb = b_cfg["inputs"].get(k)
        sa = format_input_entry(ea) if ea else "<missing>"
        sb = format_input_entry(eb) if eb else "<missing>"
        if sa != sb:
            inp_meta = source_meta["inputs_by_name"].get(k) if source_meta else None
            enum_a = enum_b = None
            if inp_meta and inp_meta.get("is_enum"):
                rev = source_meta["enums"][inp_meta["type"]]["reverse"]
                if ea:
                    try:
                        enum_a = rev.get(int(ea["value"], 0))
                    except ValueError:
                        pass
                if eb:
                    try:
                        enum_b = rev.get(int(eb["value"], 0))
                    except ValueError:
                        pass
            diffs.append(
                {
                    "name": k,
                    "a": sa,
                    "b": sb,
                    "a_enum": enum_a,
                    "b_enum": enum_b,
                    "comment": inp_meta["comment"] if inp_meta else "",
                }
            )

    if fmt == "json":
        text = json.dumps(
            {"file_a": a_cfg["path"], "file_b": b_cfg["path"], "differences": diffs},
            indent=2,
            ensure_ascii=False,
        )
    elif fmt == "md":
        lines = [
            f"# `.set` Input Diff ({len(diffs)} differences)",
            "",
            f"- **A**: `{a_cfg['path']}`",
            f"- **B**: `{b_cfg['path']}`",
            "",
            "| Input | A | B | Comment |",
            "|---|---|---|---|",
        ]
        for d in diffs:
            a_str = f"`{d['a']}`" + (f" (`{d['a_enum']}`)" if d["a_enum"] else "")
            b_str = f"`{d['b']}`" + (f" (`{d['b_enum']}`)" if d["b_enum"] else "")
            lines.append(f"| `{d['name']}` | {a_str} | {b_str} | {d['comment']} |")
        text = "\n".join(lines)
    else:
        lines = [
            f"=== Input Diff ({len(diffs)} differences) ===",
            f"A: {a_cfg['path']}",
            f"B: {b_cfg['path']}",
        ]
        for d in diffs:
            ea_s = f" ({d['a_enum']})" if d["a_enum"] else ""
            eb_s = f" ({d['b_enum']})" if d["b_enum"] else ""
            lines.append(f"  {d['name']}: {d['a']}{ea_s}  ->  {d['b']}{eb_s}")
        text = "\n".join(lines)

    return _emit_text_or_file(text, out_path)


def cmd_set(args: argparse.Namespace) -> int:
    paths = resolve_env_paths(args)

    if args.diff:
        source_meta = (
            parse_mql5_source(args.source, os.path.join(paths["mql5_dir"], "Include"))
            if args.source
            else None
        )
        return _diff_set_files(
            args.diff[0], args.diff[1], paths, source_meta, args.format, args.out
        )

    if not args.from_profile and not args.from_set and not args.from_source and not args.from_report:
        raise ValueError(
            "Provide at least one base: --from-profile, --from-set, --from-report, or --from-source"
        )

    inputs, _, source_meta = _build_base_inputs(
        paths, args.from_profile, args.from_set, args.from_source, args.source, args.from_report
    )
    overrides = parse_kv_overrides(args.set, args.set_json)
    opt_overrides = parse_opt_overrides(args.opt)
    change_notes = _apply_input_overrides(
        inputs, overrides, opt_overrides, source_meta, args.allow_new
    )

    out_path = args.out
    if args.save_to_tester:
        name = args.save_to_tester
        if not name.lower().endswith(".set"):
            name += ".set"
        out_path = os.path.join(paths["profiles_tester_dir"], name)

    if not out_path:
        raise ValueError("Specify --out <path> or --save-to-tester <name>")

    now_str = dt.datetime.now().strftime("%Y.%m.%d %H:%M:%S")
    lines = [
        f"; saved by mt5-headless-tester on {now_str}",
        "; encoding: UTF-16LE with BOM",
        ";",
    ]
    for k, entry in inputs.items():
        lines.append(f"{k}={format_input_entry(entry)}")

    write_utf16le_bom(out_path, lines)
    print(f"Wrote .set : {os.path.abspath(out_path)} ({len(inputs)} inputs, UTF-16LE BOM)")
    for note in change_notes:
        print(f"  * {note}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: compile
# ---------------------------------------------------------------------------


def cmd_compile(args: argparse.Namespace) -> int:
    paths = resolve_env_paths(args)
    editor = paths["metaeditor_exe"]
    if not os.path.isfile(editor):
        raise FileNotFoundError(f"MetaEditor64.exe not found: {editor}")

    src = os.path.abspath(args.source)
    if not os.path.isfile(src):
        raise FileNotFoundError(f"Source .mq5 not found: {src}")

    inc_dir = os.path.abspath(args.include or paths["mql5_dir"])
    trade_mqh = os.path.join(inc_dir, "Include", "Trade", "Trade.mqh")
    if not os.path.isfile(trade_mqh):
        raise FileNotFoundError(
            f"Include root missing Include\\Trade\\Trade.mqh: {inc_dir}. Pass --include explicitly."
        )

    built_from = src
    stage_dir: Optional[str] = None
    src_name = os.path.basename(src)

    # MetaEditor silently breaks when source filenames contain whitespace
    if re.search(r"\s", src_name):
        stem = pathlib.Path(src).stem
        safe_stem = re.sub(r"\s+", "_", stem)
        safe_name = safe_stem + ".mq5"
        temp_base = os.environ.get("TEMP") or os.environ.get("TMP") or paths["work_dir"]
        stage_dir = os.path.join(temp_base, "mql5-compile", safe_stem)
        if os.path.isdir(stage_dir):
            shutil.rmtree(stage_dir, ignore_errors=True)
        os.makedirs(stage_dir, exist_ok=True)
        built_from = os.path.join(stage_dir, safe_name)
        shutil.copy2(src, built_from)

        # Carry #property icon "..." relative assets along
        src_text = read_text_auto(src)
        for m in re.finditer(r'#property\s+icon\s+"([^"]+)"', src_text):
            icon_rel = m.group(1)
            icon_src = os.path.join(os.path.dirname(src), icon_rel)
            icon_dst = os.path.join(stage_dir, icon_rel)
            if os.path.isfile(icon_src):
                os.makedirs(os.path.dirname(icon_dst), exist_ok=True)
                shutil.copy2(icon_src, icon_dst)

    log_path = str(pathlib.Path(built_from).with_suffix(".log"))
    ex5_path = str(pathlib.Path(built_from).with_suffix(".ex5"))
    for stale in (log_path, ex5_path):
        if os.path.isfile(stale):
            os.remove(stale)

    try:
        subprocess.run(
            [editor, f"/compile:{built_from}", f"/inc:{inc_dir}", "/log"],
            capture_output=True,
            check=False,
        )

        # Wait asynchronously for log file
        deadline = time.time() + 90.0
        while not os.path.isfile(log_path) and time.time() < deadline:
            time.sleep(0.2)
        if not os.path.isfile(log_path):
            raise RuntimeError(
                f"MetaEditor produced no log for {built_from} (exit code is unreliable)."
            )

        summary = None
        lines: List[str] = []
        ex5_deadline = time.time() + 15.0
        while time.time() < ex5_deadline:
            lines = read_text_auto(log_path).splitlines()
            matches = [ln for ln in lines if ln.startswith("Result:")]
            if matches:
                summary = matches[-1]
                break
            time.sleep(0.2)

        if not lines:
            lines = read_text_auto(log_path).splitlines()
        problems = [ln for ln in lines if re.search(r":\s+(?:error|warning)\s+\d+:", ln)]
        errors = [ln for ln in lines if re.search(r":\s+error\s+\d+:", ln)]
        built = os.path.isfile(ex5_path)
        passed = built and len(errors) == 0

        deployed_path = None
        if passed and args.deploy_to:
            target = args.deploy_to
            if not os.path.isabs(target):
                target = os.path.join(paths["experts_dir"], target)
            if not target.lower().endswith(".ex5"):
                target += ".ex5"
            if re.search(r"\s", os.path.basename(target)):
                raise ValueError(f"Deployed .ex5 filename must not contain spaces: {target}")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(ex5_path, target)
            deployed_path = os.path.abspath(target)

        res = {
            "source": src,
            "compiled": built_from,
            "include": inc_dir,
            "log": log_path,
            "ex5": ex5_path,
            "deployed_to": deployed_path,
            "built": built,
            "passed": passed,
            "result": summary,
            "problems": problems,
        }

        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(f"Source   : {src}")
            print(f"Compiled : {built_from}")
            print(f"Include  : {inc_dir}")
            print(f"Log      : {log_path}")
            print(f"Ex5      : {ex5_path}")
            if deployed_path:
                print(f"Deployed : {deployed_path}")
            print(f"Built    : {built}")
            print(f"Result   : {summary or '<missing>'}")
            if problems:
                print("Problems :")
                for p in problems:
                    print(f"  {p}")
            elif not built:
                print("WARNING  : No .ex5 was produced despite no compiler errors -> FAILED build.")
        return 0 if passed else 1
    finally:
        if stage_dir and not args.keep_stage:
            shutil.rmtree(stage_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Subcommands: test & probe
# ---------------------------------------------------------------------------


def _build_common_auth_lines(require_all: bool = False) -> Tuple[List[str], Dict[str, bool]]:
    """Build [Common] INI lines from env vars without ever exposing credential values."""
    auth_server = os.environ.get("MT5_BROKER_SERVER") or os.environ.get("MT5_SERVER") or ""
    auth_login = os.environ.get("MT5_LOGIN") or os.environ.get("MT5_USERNAME") or ""
    auth_password = os.environ.get("MT5_PASSWORD") or ""
    use_auth = bool(auth_server or auth_login or auth_password)
    if require_all and not (auth_server and auth_login and auth_password):
        raise ValueError(
            "--use-env-auth requested, but MT5_BROKER_SERVER / MT5_LOGIN / MT5_PASSWORD are not all set in env or .env."
        )
    lines: List[str] = []
    if use_auth:
        lines.extend(
            [
                "[Common]",
                "KeepPrivate=1",
                "ProxyEnable=0",
                "NewsEnable=0",
            ]
        )
        if auth_login:
            lines.append(f"Login={auth_login}")
        if auth_password:
            lines.append(f"Password={auth_password}")
        if auth_server:
            lines.append(f"Server={auth_server}")
    status = {
        "server": bool(auth_server),
        "login": bool(auth_login),
        "password": bool(auth_password),
        "any": use_auth,
    }
    return lines, status


def _launch_on_win32_hidden_desktop(
    command_line: str, desktop_name: str = "MT5HeadlessDesktop"
) -> Optional[int]:
    """Run a Windows process inside an isolated hidden Win32 desktop (built-in Ghost alternative)."""
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32

    u32.CreateDesktopW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    u32.CreateDesktopW.restype = wintypes.HANDLE
    u32.CloseDesktop.argtypes = [wintypes.HANDLE]
    u32.CloseDesktop.restype = wintypes.BOOL

    k32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOW),
        ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    k32.CreateProcessW.restype = wintypes.BOOL
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    GENERIC_ALL = 0x10000000
    INFINITE = 0xFFFFFFFF

    hdesk = u32.CreateDesktopW(desktop_name, None, None, 0, GENERIC_ALL, None)
    if not hdesk:
        return None

    try:
        si = _STARTUPINFOW()
        si.cb = ctypes.sizeof(_STARTUPINFOW)
        si.lpDesktop = f"Winsta0\\{desktop_name}"
        si.dwFlags = STARTF_USESHOWWINDOW
        si.wShowWindow = 1
        pi = _PROCESS_INFORMATION()

        cmd_buf = ctypes.create_unicode_buffer(command_line)
        ok = k32.CreateProcessW(
            None,
            cmd_buf,
            None,
            None,
            False,
            0,
            None,
            None,
            ctypes.byref(si),
            ctypes.byref(pi),
        )
        if not ok:
            return None

        try:
            k32.WaitForSingleObject(pi.hProcess, INFINITE)
            exit_code = wintypes.DWORD(0)
            k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(exit_code))
            return int(exit_code.value)
        finally:
            k32.CloseHandle(pi.hThread)
            k32.CloseHandle(pi.hProcess)
    finally:
        u32.CloseDesktop(hdesk)


def _launch_via_interactive_schtask(
    exe_args: List[str],
    mode: str,
    work_dir: str,
) -> int:
    """Bridge Session 0 execution into the active desktop session via a one-shot Task Scheduler job.

    MT5 Strategy Tester fails to export .htm/.xml reports (MDI create failed) and crashes under
    Winsta0\\MT5HeadlessDesktop (0xC0000142) when started directly in Windows Session 0 (WinRM,
    PowerShell Direct, or non-interactive services).
    """
    pid = os.getpid()
    ts_tag = int(time.time() * 1000) % 1000000
    task_name = f"MT5HeadlessBridge_{pid}_{ts_tag}"
    launcher_py = os.path.join(work_dir, f"s1_launch_{pid}_{ts_tag}.py")
    exit_txt = os.path.join(work_dir, f"s1_exit_{pid}_{ts_tag}.txt")
    err_txt = os.path.join(work_dir, f"s1_err_{pid}_{ts_tag}.txt")
    scripts_dir = os.path.dirname(os.path.abspath(__file__))

    launcher_src = "\n".join(
        [
            "import os, sys, subprocess",
            f"sys.path.insert(0, {scripts_dir!r})",
            "import mt5_headless as mh",
            f"exe_args = {exe_args!r}",
            f"mode = {mode!r}",
            f"exit_file = {exit_txt!r}",
            f"err_file = {err_txt!r}",
            "rc = None",
            "try:",
            "    if mode == 'hidden-desktop':",
            "        rc = mh._launch_on_win32_hidden_desktop(subprocess.list2cmdline(exe_args))",
            "    if rc is None:",
            "        si = subprocess.STARTUPINFO()",
            "        si.dwFlags |= mh.STARTF_USESHOWWINDOW",
            "        si.wShowWindow = mh.SW_HIDE if mode == 'hidden' else mh.SW_SHOWMINNOACTIVE",
            "        rc = subprocess.Popen(exe_args, startupinfo=si).wait()",
            "except Exception as exc:",
            "    with open(err_file, 'w', encoding='utf-8') as ef:",
            "        ef.write(str(exc))",
            "with open(exit_file, 'w', encoding='utf-8') as fh:",
            "    fh.write(str(-1 if rc is None else rc))",
            "",
        ]
    )
    write_utf8(launcher_py, launcher_src)

    py_dir = os.path.dirname(sys.executable)
    pythonw = os.path.join(py_dir, "pythonw.exe")
    runner_exe = pythonw if os.path.isfile(pythonw) else sys.executable
    tr_cmd = f'"{runner_exe}" "{launcher_py}"'

    try:
        res_create = subprocess.run(
            ["schtasks", "/Create", "/TN", task_name, "/TR", tr_cmd, "/SC", "ONCE", "/ST", "00:00", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res_create.returncode != 0:
            msg = (res_create.stderr or res_create.stdout or "").strip()
            raise RuntimeError(f"Failed to register interactive bridge task '{task_name}': {msg}")

        res_run = subprocess.run(
            ["schtasks", "/Run", "/TN", task_name],
            capture_output=True,
            text=True,
            check=False,
        )
        if res_run.returncode != 0:
            msg = (res_run.stderr or res_run.stdout or "").strip()
            raise RuntimeError(f"Failed to trigger interactive bridge task '{task_name}': {msg}")

        start_ts = time.time()
        last_probe_ts = start_ts
        while not os.path.isfile(exit_txt):
            time.sleep(0.5)
            now = time.time()
            if (now - start_ts) >= 12.0 and (now - last_probe_ts) >= 5.0:
                last_probe_ts = now
                q = subprocess.run(
                    ["schtasks", "/Query", "/TN", task_name, "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                task_running = "running" in (q.stdout or "").lower()
                if not task_running and not get_running_terminals():
                    time.sleep(1.0)
                    if os.path.isfile(exit_txt):
                        break
                    v = subprocess.run(
                        ["schtasks", "/Query", "/TN", task_name, "/V", "/FO", "LIST"],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    detail = "\n".join(
                        ln for ln in (v.stdout or "").splitlines() if "Last Result" in ln or "Status" in ln
                    )
                    raise RuntimeError(
                        "MT5 Strategy Tester was started from Windows Session 0 (non-interactive). "
                        "MT5 requires an active logged-in desktop session to export .htm/.xml reports, and "
                        f"interactive Task Scheduler bridging did not launch terminal64.exe. {detail}"
                    )

        raw_rc = read_text_auto(exit_txt).strip()
        exit_code = int(raw_rc) if re.match(r"^-?\d+$", raw_rc) else -1
        if os.path.isfile(err_txt):
            err_msg = read_text_auto(err_txt).strip()
            if err_msg:
                print(f"WARNING  : Interactive bridge error: {err_msg}", file=sys.stderr)
        return exit_code
    finally:
        subprocess.run(["schtasks", "/Delete", "/TN", task_name, "/F"], capture_output=True, check=False)
        for tmp_f in (launcher_py, exit_txt, err_txt):
            if os.path.isfile(tmp_f):
                try:
                    os.remove(tmp_f)
                except OSError:
                    pass


def _run_terminal_minimized(
    paths: Dict[str, str],
    abs_ini: str,
    report_name: str,
    launch_mode: Optional[str] = None,
) -> int:
    """Safely launch terminal64.exe without stealing focus and scrub INI afterwards."""
    terminal_exe = paths["terminal_exe"]
    if not os.path.isfile(terminal_exe):
        raise FileNotFoundError(f"terminal64.exe not found: {terminal_exe}")

    running, _ = is_terminal_running(terminal_exe, paths["portable"])
    if running:
        raise RuntimeError(
            "Matching terminal64.exe is running: it owns the target data folder and ShutdownTerminal=1 would close it. "
            "Close MT5 gracefully via its UI first (NEVER kill terminal64.exe)."
        )

    os.makedirs(paths["work_dir"], exist_ok=True)
    lock_path = os.path.join(paths["work_dir"], "runner.lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as lf:
            lf.write(f"pid={os.getpid()} report={report_name} started={dt.datetime.now().isoformat()}\n")
    except FileExistsError:
        raise RuntimeError(
            f"Another test lock is active at {lock_path}. MT5 uses a single data folder and can run only one test at a time."
        )

    try:
        exe_args = [terminal_exe]
        if paths["portable"]:
            exe_args.append("/portable")
        exe_args.append(f"/config:{abs_ini}")

        mode = (launch_mode or paths.get("launch_mode") or "minimized").lower()
        exit_code: Optional[int] = None

        if os.name == "nt" and _get_win32_session_id() == 0:
            bridge_mode = "hidden-desktop" if mode in ("hidden-desktop", "ghost") else mode
            print(f"Launcher : session0-schtasks-bridge -> {bridge_mode} (interactive desktop session)")
            exit_code = _launch_via_interactive_schtask(exe_args, bridge_mode, paths["work_dir"])
        else:
            if mode == "hidden-desktop" and os.name == "nt":
                cmd_str = subprocess.list2cmdline(exe_args)
                print("Launcher : win32-hidden-desktop (Winsta0\\MT5HeadlessDesktop)")
                exit_code = _launch_on_win32_hidden_desktop(cmd_str)
                if exit_code is None:
                    print("WARNING  : Win32 hidden desktop unavailable; falling back to SW_SHOWMINNOACTIVE.")

            if exit_code is None:
                popen_kwargs: Dict[str, Any] = {}
                if os.name == "nt":
                    si = subprocess.STARTUPINFO()
                    si.dwFlags |= STARTF_USESHOWWINDOW
                    if mode == "hidden":
                        si.wShowWindow = SW_HIDE
                        print("Launcher : win32-hidden (SW_HIDE)")
                    else:
                        si.wShowWindow = SW_SHOWMINNOACTIVE
                        print("Launcher : win32-minimized-noactive (SW_SHOWMINNOACTIVE)")
                    popen_kwargs["startupinfo"] = si
                proc = subprocess.Popen(exe_args, **popen_kwargs)
                exit_code = proc.wait()

        print(f"Exit     : {exit_code}  (not a verdict; inspecting report and logs)")
        return exit_code
    finally:
        if os.path.isfile(lock_path):
            os.remove(lock_path)
        scrub_ini_credentials(abs_ini)


def _verify_loaded_expert(expected_expert_rel: str, rep: Dict[str, Any]) -> None:
    """Verify MT5 actually executed the intended .ex5 and did not silently fall back to another EA."""
    expected_norm = expected_expert_rel.replace("/", "\\").lstrip("\\").lower()
    expected_stem = pathlib.Path(expected_norm).stem.lower()
    loaded_expert = ((rep.get("diagnostics") or {}).get("loaded_expert") or "").strip()
    if loaded_expert:
        loaded_rel = re.sub(
            r"^(?:mql5\\)?experts\\",
            "",
            loaded_expert.replace("/", "\\").lstrip("\\"),
            flags=re.I,
        )
        loaded_stem = pathlib.Path(loaded_rel).stem.lower()
        mismatch = (
            loaded_rel.lower() != expected_norm
            if ("\\" in loaded_rel and "\\" in expected_norm)
            else (loaded_stem != expected_stem)
        )
        if mismatch:
            raise RuntimeError(
                f"MT5 ran the wrong Expert Advisor! Expected '{expected_expert_rel}', "
                f"but agent log loaded '{loaded_expert}'."
            )
    rep_expert = ((rep.get("settings") or {}).get("Expert") or "").strip()
    if rep_expert:
        rep_stem = pathlib.Path(rep_expert.replace("/", "\\")).stem.lower()
        if rep_stem != expected_stem:
            raise RuntimeError(
                f"MT5 ran the wrong Expert Advisor! Expected '{expected_expert_rel}' "
                f"(stem '{expected_stem}'), but report shows Expert='{rep_expert}'."
            )


def cmd_probe(args: argparse.Namespace) -> int:
    paths = resolve_env_paths(args)
    expert_rel = resolve_expert_ini_path(args.expert, paths)
    if args.report:
        report_name = args.report
        if re.search(r"[\\/:.]", report_name):
            raise ValueError(f"--report must be a bare name without paths or extension: {report_name}")
    else:
        stem = pathlib.Path(expert_rel).stem
        sanitized = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_") or "ea"
        report_name = f"probe_{sanitized[:40]}"

    common_lines, auth_status = _build_common_auth_lines(require_all=False)
    ini_path = args.ini_out or os.path.join(paths["work_dir"], f"{report_name}.ini")
    ini_lines = [
        f"; Generated by mt5-headless-tester probe on {dt.datetime.now().strftime('%Y.%m.%d %H:%M:%S')}",
        *common_lines,
        "[Tester]",
        f"Expert={expert_rel}",
        f"Symbol={args.symbol}",
        f"Period={args.period}",
        f"Model={args.model}",
        f"FromDate={args.from_date}",
        f"ToDate={args.to_date}",
        "Dates=1",
        "Deposit=10000",
        "Currency=USD",
        "Leverage=100",
        "ForwardMode=0",
        "Optimization=0",
        "OptimizationCriterion=0",
        f"Report={report_name}",
        "ReplaceReport=1",
        "ShutdownTerminal=1",
        "Visual=0",
    ]
    write_utf16le_bom(ini_path, ini_lines)
    abs_ini = os.path.abspath(ini_path)
    terminal_exe = paths["terminal_exe"]
    portable_flag = " /portable" if paths["portable"] else ""
    raw_cmd = f'"{terminal_exe}"{portable_flag} /config:{abs_ini}'
    ghost_payload = json.dumps({"op": "launch", "exe": raw_cmd})

    print(f"Probe INI: {abs_ini}")
    print(f"Mode     : {'PORTABLE (/portable)' if paths['portable'] else 'STANDARD'}")
    print(f"Launch   : {paths['launch_mode']} (--run uses built-in {paths['launch_mode']} launcher; or use external Ghost MCP if installed)")
    print(f"Expert   : {expert_rel} | {args.symbol} {args.period} | Model={args.model} ({args.from_date} - {args.to_date})")
    if auth_status["any"]:
        print(
            "Auth     : "
            + f"SERVER={'[SET]' if auth_status['server'] else '[UNSET]'}, "
            + f"LOGIN={'[SET]' if auth_status['login'] else '[UNSET]'}, "
            + f"PASSWORD={'[SET]' if auth_status['password'] else '[UNSET]'} (injected via env vars)"
        )
    print(f"GhostMCP : {ghost_payload}  (optional external mcp__ghost_window)")
    print(f"Command  : {raw_cmd}")

    if not args.run:
        if auth_status["password"]:
            print(f"Security : After terminal exits, run 'scrub-ini \"{abs_ini}\"'")
        print("\nAfter terminal exits, extract all compiled inputs & create .set via:")
        print(f"  inspect --report {report_name}")
        if args.out_set:
            print(f"  set --from-report {report_name} --out \"{args.out_set}\"")
        elif args.save_to_tester:
            print(f"  set --from-report {report_name} --save-to-tester \"{args.save_to_tester}\"")
        else:
            print(f"  set --from-report {report_name} --save-to-tester \"{pathlib.Path(expert_rel).stem}_defaults\"")
        return 0

    _run_terminal_minimized(paths, abs_ini, report_name)
    rep = parse_htm_report(report_name, paths)
    _verify_loaded_expert(expert_rel, rep)
    inputs_meta = rep.get("inputs_meta", [])
    groups = list(dict.fromkeys(x["group"] for x in inputs_meta if x.get("group")))
    print(f"\nProbe Discovered : {len(inputs_meta)} compiled input(s) across {len(groups)} group(s) from {rep['source_htm']}")

    out_set = args.out_set
    if args.save_to_tester:
        name = args.save_to_tester
        if not name.lower().endswith(".set"):
            name += ".set"
        out_set = os.path.join(paths["profiles_tester_dir"], name)

    if out_set:
        now_str = dt.datetime.now().strftime("%Y.%m.%d %H:%M:%S")
        set_lines = [
            f"; probed from {expert_rel} via {report_name}.htm on {now_str}",
            "; encoding: UTF-16LE with BOM",
            ";",
        ]
        for k, v in rep.get("inputs", {}).items():
            entry = _ensure_default_tail(parse_input_entry(v), k, None)
            set_lines.append(f"{k}={format_input_entry(entry)}")
        write_utf16le_bom(out_set, set_lines)
        print(f"Wrote .set       : {os.path.abspath(out_set)} ({len(rep.get('inputs', {}))} inputs)")

    records = [
        {
            "name": item["name"],
            "value": item["value"],
            "resolved_enum": None,
            "type": item["inferred_type"],
            "group": item["group"],
            "comment": "",
            "optimize": "N",
            "range": "",
        }
        for item in inputs_meta
    ]
    print(_format_inspect_inputs(records, {"count": len(records), "tester": rep["settings"]}, args.format))
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    paths = resolve_env_paths(args)
    report_name = args.report
    if re.search(r"[\\/:.]", report_name):
        raise ValueError(f"--report must be a bare name without paths or extension: {report_name}")

    if not args.profile and not args.set_file and not args.from_source and not args.from_report:
        if not (args.expert and args.symbol and args.period):
            raise ValueError(
                "Provide --profile (recommended), --set-file, --from-report, or --from-source (or specify --expert, --symbol, --period)."
            )

    inputs, tester_from_prof, source_meta = _build_base_inputs(
        paths, args.profile, args.set_file, args.from_source, args.source, args.from_report
    )

    overrides = parse_kv_overrides(args.set, args.set_json)
    opt_overrides = parse_opt_overrides(args.opt)
    change_notes = _apply_input_overrides(
        inputs, overrides, opt_overrides, source_meta, args.allow_new
    )

    # Assemble [Tester] preserving base profile defaults where not overridden
    tester: Dict[str, str] = dict(tester_from_prof or {})
    if args.expert:
        tester["Expert"] = args.expert
    if args.symbol:
        tester["Symbol"] = args.symbol
    if args.period:
        tester["Period"] = args.period
    if args.model is not None:
        tester["Model"] = str(args.model)
    if args.from_date:
        tester["FromDate"] = args.from_date
        tester.setdefault("Dates", "1")
    if args.to_date:
        tester["ToDate"] = args.to_date
        tester.setdefault("Dates", "1")
    if args.deposit is not None:
        tester["Deposit"] = str(args.deposit)
    if args.currency:
        tester["Currency"] = args.currency
    if args.leverage is not None:
        tester["Leverage"] = str(args.leverage)
    if args.execution_mode is not None:
        tester["ExecutionMode"] = str(args.execution_mode)
    if args.optimization is not None:
        tester["Optimization"] = str(args.optimization)
    if args.criterion is not None:
        tester["OptimizationCriterion"] = str(args.criterion)

    # Required batch flags
    tester["Report"] = report_name
    tester["ReplaceReport"] = "1"
    tester["ShutdownTerminal"] = "1"
    tester["Visual"] = "1" if args.visual else "0"
    tester.setdefault("ForwardMode", "0")
    tester.setdefault("Optimization", "0")
    tester.setdefault("OptimizationCriterion", "0")
    tester.setdefault("Model", "1")

    if "Expert" not in tester or "Symbol" not in tester or "Period" not in tester:
        raise ValueError("[Tester] requires Expert, Symbol, and Period (pass --profile or CLI flags)")
    tester["Expert"] = resolve_expert_ini_path(tester["Expert"], paths)

    common_lines, auth_status = _build_common_auth_lines(
        require_all=getattr(args, "use_env_auth", False)
    )

    ini_path = args.ini_out or os.path.join(paths["work_dir"], f"{report_name}.ini")
    ini_lines = [
        f"; Generated by mt5-headless-tester on {dt.datetime.now().strftime('%Y.%m.%d %H:%M:%S')}",
        *common_lines,
        "[Tester]",
    ]
    for k, v in tester.items():
        ini_lines.append(f"{k}={v}")
    if inputs:
        ini_lines.append("[TesterInputs]")
        for k, entry in inputs.items():
            ini_lines.append(f"{k}={format_input_entry(entry)}")

    write_utf16le_bom(ini_path, ini_lines)
    abs_ini = os.path.abspath(ini_path)
    terminal_exe = paths["terminal_exe"]
    portable_flag = " /portable" if paths["portable"] else ""
    raw_cmd = f'"{terminal_exe}"{portable_flag} /config:{abs_ini}'
    ghost_payload = json.dumps({"op": "launch", "exe": raw_cmd})

    print(f"Ini      : {abs_ini}")
    print(f"Mode     : {'PORTABLE (/portable)' if paths['portable'] else 'STANDARD'}")
    print(f"Launch   : {paths['launch_mode']} (--run uses built-in {paths['launch_mode']} launcher; or use external Ghost MCP if installed)")
    print(f"Expert   : {tester['Expert']} | {tester['Symbol']} {tester['Period']} | Model={tester['Model']}")
    if auth_status["any"]:
        print(
            "Auth     : "
            + f"SERVER={'[SET]' if auth_status['server'] else '[UNSET]'}, "
            + f"LOGIN={'[SET]' if auth_status['login'] else '[UNSET]'}, "
            + f"PASSWORD={'[SET]' if auth_status['password'] else '[UNSET]'} (injected via env vars)"
        )
    if change_notes:
        print(f"Overrides: {len(change_notes)} applied")
        for n in change_notes:
            print(f"  * {n}")
    print(f"GhostMCP : {ghost_payload}  (optional external mcp__ghost_window)")
    print(f"Command  : {raw_cmd}")

    if not args.run:
        if auth_status["password"]:
            print(f"Security : After terminal exits, run 'scrub-ini \"{abs_ini}\"' to erase Password= from disk.")
        return 0

    _run_terminal_minimized(paths, abs_ini, report_name)
    _verify_loaded_expert(tester["Expert"], parse_htm_report(report_name, paths))

    deals_out = args.deals or os.path.join(paths["work_dir"], f"deals_{report_name}.txt")
    rep_args = argparse.Namespace(
        report=report_name,
        format=args.format or "text",
        out=args.out,
        deals=deals_out,
        include_deals=False,
        max_deals=200,
        include_orders=False,
        embed_images=args.embed_images,
        project_dir=getattr(args, "project_dir", None),
        portable=getattr(args, "portable", None),
        terminal_dir=args.terminal_dir,
        data_dir=args.data_dir,
        tester_dir=args.tester_dir,
        work_dir=args.work_dir,
    )
    return cmd_report(rep_args)


# ---------------------------------------------------------------------------
# Subcommand: optimize (Parallel Multi-Core Optimization + XML Leaderboard)
# ---------------------------------------------------------------------------

_OPT_HEADER_MAP: Dict[str, str] = {
    "pass": "pass",
    "paso": "pass",
    "symbol": "symbol",
    "símbolo": "symbol",
    "simbolo": "symbol",
    "result": "result",
    "resultado": "result",
    "custom": "result",
    "ontester": "result",
    "ontester result": "result",
    "profit": "profit",
    "beneficio": "profit",
    "net profit": "profit",
    "total trades": "trades",
    "trades": "trades",
    "total de operaciones": "trades",
    "profit factor": "profit_factor",
    "factor de rentabilidad": "profit_factor",
    "expected payoff": "expected_payoff",
    "beneficio esperado": "expected_payoff",
    "drawdown %": "drawdown_pct",
    "equity dd %": "drawdown_pct",
    "drawdown": "drawdown_pct",
    "reducción %": "drawdown_pct",
    "reduccion %": "drawdown_pct",
    "recovery factor": "recovery_factor",
    "factor de recuperación": "recovery_factor",
    "factor de recuperacion": "recovery_factor",
    "sharpe ratio": "sharpe",
    "sharpe": "sharpe",
    "ratio de sharpe": "sharpe",
}

_OPT_NUMERIC_METRICS = {
    "pass",
    "result",
    "profit",
    "trades",
    "profit_factor",
    "expected_payoff",
    "recovery_factor",
    "sharpe",
    "drawdown_pct",
}


def resolve_report_xml_path(report_arg: str, paths: Dict[str, str]) -> Tuple[str, str]:
    """Resolve an optimization report argument (bare name or .xml path) to (name, xml_path)."""
    if os.path.isfile(report_arg):
        abs_path = os.path.abspath(report_arg)
        return pathlib.Path(abs_path).stem, abs_path
    clean = report_arg[:-4] if report_arg.lower().endswith(".xml") else report_arg
    candidate = os.path.join(paths["data_dir"], f"{clean}.xml")
    if os.path.isfile(candidate):
        return clean, candidate
    raise FileNotFoundError(f"Optimization .xml not found: {report_arg} (checked {candidate})")


def _count_local_agents(tester_dir: str) -> int:
    if not tester_dir or not os.path.isdir(tester_dir):
        return 0
    return sum(
        1
        for entry in os.listdir(tester_dir)
        if entry.lower().startswith("agent-127.0.0.1-")
        and os.path.isdir(os.path.join(tester_dir, entry))
    )


def _estimate_opt_combinations(inputs: Dict[str, Dict[str, Any]]) -> Tuple[int, List[str]]:
    """Estimate total parallel grid passes across active ||Y inputs."""
    active_params: List[str] = []
    total = 1
    for k, entry in inputs.items():
        if entry.get("optimize") != "Y":
            continue
        active_params.append(k)
        try:
            start = float(entry["start"])
            step = float(entry["step"])
            stop = float(entry["stop"])
            if step > 0:
                cnt = max(1, int(math.floor((stop - start) / step + 1e-9)) + 1)
                total *= cnt
        except (ValueError, TypeError):
            pass
    return (total if active_params else 0), active_params


def _parse_spreadsheetml_rows(xml_text: str) -> List[List[str]]:
    """Parse SpreadsheetML XML rows (handling ss:Index sparse cells)."""
    root = ET.fromstring(xml_text.lstrip("\ufeff \t\r\n"))
    rows: List[List[str]] = []
    for row_el in root.iter():
        if not row_el.tag.endswith("Row"):
            continue
        cells: List[str] = []
        col_idx = 0
        for cell_el in row_el:
            if not cell_el.tag.endswith("Cell"):
                continue
            idx_val = None
            for ak, av in cell_el.attrib.items():
                if ak.endswith("Index") and av.isdigit():
                    idx_val = int(av)
                    break
            if idx_val is not None:
                target_idx = idx_val - 1
                while col_idx < target_idx:
                    cells.append("")
                    col_idx += 1
            text_val = ""
            for child in cell_el:
                if child.tag.endswith("Data") and child.text is not None:
                    text_val = child.text.strip()
                    break
            if not text_val and cell_el.text:
                text_val = cell_el.text.strip()
            cells.append(text_val)
            col_idx += 1
        if any(c != "" for c in cells):
            rows.append(cells)
    return rows


def parse_optimization_xml(report_arg: str, paths: Dict[str, str]) -> Dict[str, Any]:
    """Parse an MT5 optimization SpreadsheetML (.xml) or HTML table report."""
    report_name, xml_path = resolve_report_xml_path(report_arg, paths)
    raw_text = read_text_auto(xml_path)

    if "<Workbook" in raw_text or "<Row" in raw_text:
        rows = _parse_spreadsheetml_rows(raw_text)
    else:
        rows = [
            _extract_cells(r)
            for r in re.findall(r"<tr[^>]*>(.*?)</tr>", raw_text, re.S | re.I)
        ]
        rows = [r for r in rows if any(c != "" for c in r)]

    header_idx: Optional[int] = None
    for i, r in enumerate(rows):
        mapped = sum(1 for c in r if c.strip().lower() in _OPT_HEADER_MAP)
        if mapped >= 2:
            header_idx = i
            break

    if header_idx is None:
        return {
            "report_name": report_name,
            "source_xml": xml_path,
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "total_passes": 0,
            "profitable_passes": 0,
            "input_columns": [],
            "varied_inputs": [],
            "passes": [],
        }

    headers = rows[header_idx]
    plan: List[Tuple[str, str]] = []
    input_columns: List[str] = []
    for raw_h in headers:
        h_clean = raw_h.strip()
        canon = _OPT_HEADER_MAP.get(h_clean.lower())
        if canon:
            plan.append(("metric", canon))
        elif h_clean:
            plan.append(("input", h_clean))
            input_columns.append(h_clean)
        else:
            plan.append(("skip", ""))

    passes: List[Dict[str, Any]] = []
    for r in rows[header_idx + 1 :]:
        if not any(c.strip() for c in r):
            continue
        rec: Dict[str, Any] = {"inputs": {}}
        for idx, val in enumerate(r):
            if idx >= len(plan):
                break
            kind, key = plan[idx]
            if kind == "metric":
                if key in _OPT_NUMERIC_METRICS:
                    num = _parse_mt5_float(val)
                    if key in ("pass", "trades") and num is not None:
                        rec[key] = int(round(num))
                    else:
                        rec[key] = num
                else:
                    rec[key] = val.strip()
            elif kind == "input":
                rec["inputs"][key] = val.strip()
        if rec.get("profit") is not None or rec.get("result") is not None:
            passes.append(rec)

    varied_inputs: List[str] = []
    for col in input_columns:
        distinct = {p["inputs"].get(col, "") for p in passes}
        if len(distinct) > 1:
            varied_inputs.append(col)

    profitable = sum(1 for p in passes if (p.get("profit") or 0) > 0)
    return {
        "report_name": report_name,
        "source_xml": xml_path,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "total_passes": len(passes),
        "profitable_passes": profitable,
        "input_columns": input_columns,
        "varied_inputs": varied_inputs or input_columns,
        "passes": passes,
    }


def filter_and_sort_opt_passes(
    opt_data: Dict[str, Any],
    sort_by: str = "result",
    min_trades: Optional[int] = None,
    min_pf: Optional[float] = None,
    max_dd_pct: Optional[float] = None,
    require_positive: bool = False,
) -> List[Dict[str, Any]]:
    """Filter and rank optimization passes by the chosen metric."""
    filtered: List[Dict[str, Any]] = []
    for p in opt_data.get("passes", []):
        trades = p.get("trades")
        pf = p.get("profit_factor")
        dd = p.get("drawdown_pct")
        profit = p.get("profit")
        if min_trades is not None and (trades is None or trades < min_trades):
            continue
        if min_pf is not None and (pf is None or pf < min_pf):
            continue
        if max_dd_pct is not None and (dd is None or dd > max_dd_pct):
            continue
        if require_positive and (profit is None or profit <= 0):
            continue
        filtered.append(p)

    reverse = sort_by != "drawdown_pct"

    def _sort_key(p: Dict[str, Any]) -> Tuple[int, float]:
        val = p.get(sort_by)
        if val is None and sort_by == "result":
            val = p.get("profit")
        if isinstance(val, (int, float)):
            return (1 if reverse else 0, float(val))
        return (0 if reverse else 1, float("-inf") if reverse else float("inf"))

    filtered.sort(key=_sort_key, reverse=reverse)
    return filtered


def _export_pass_to_set(
    picked_pass: Dict[str, Any],
    out_set_path: str,
    base_inputs: Optional[Dict[str, Dict[str, Any]]],
    source_label: str,
) -> int:
    """Merge winning optimization pass inputs with baseline and write UTF-16LE .set."""
    merged: Dict[str, Dict[str, Any]] = {}
    if base_inputs:
        for k, entry in base_inputs.items():
            e_copy = dict(entry)
            e_copy["optimize"] = "N"
            merged[k] = e_copy

    for k, val in picked_pass.get("inputs", {}).items():
        if k in merged:
            merged[k]["value"] = str(val)
            merged[k]["optimize"] = "N"
        else:
            merged[k] = parse_input_entry(str(val))

    now_str = dt.datetime.now().strftime("%Y.%m.%d %H:%M:%S")
    lines = [
        f"; exported from optimization pass #{picked_pass.get('pass', '?')} ({source_label}) on {now_str}",
        "; encoding: UTF-16LE with BOM (all optimization flags reset to N)",
        ";",
    ]
    for k, entry in merged.items():
        _ensure_default_tail(entry, k, None)
        lines.append(f"{k}={format_input_entry(entry)}")

    write_utf16le_bom(out_set_path, lines)
    return len(merged)


def export_opt_text(
    opt_data: Dict[str, Any], ranked: List[Dict[str, Any]], top_n: int, sort_by: str
) -> str:
    shown = ranked[:top_n]
    varied = opt_data.get("varied_inputs", [])[:8]
    lines = [
        f"=== Optimization Report: {opt_data['report_name']} ===",
        f"Source XML   : {opt_data['source_xml']}",
        f"Total Passes : {opt_data['total_passes']} (Profitable: {opt_data['profitable_passes']}, Matched: {len(ranked)}/{opt_data['total_passes']}, Shown: {len(shown)})",
        f"Ranked By    : {sort_by}",
        "",
    ]
    if not shown:
        lines.append("No passes matched the specified filters.")
        return "\n".join(lines)

    best = shown[0]
    lines.append(
        f"Best Pass    : #{best.get('pass','?')} | Profit={best.get('profit')} | PF={best.get('profit_factor')} | "
        f"DD%={best.get('drawdown_pct')} | Sharpe={best.get('sharpe')} | Trades={best.get('trades')}"
    )
    lines.append("Best Inputs  : " + ", ".join(f"{k}={v}" for k, v in best.get("inputs", {}).items()))
    lines.append("")
    lines.append(f"Top {len(shown)} Passes:")
    for rank, p in enumerate(shown, start=1):
        inps_txt = ", ".join(f"{k}={p['inputs'].get(k, '')}" for k in varied)
        lines.append(
            f"  {rank:>2}. Pass #{str(p.get('pass','?')):<4} | Res={str(p.get('result','-')):<7} | "
            f"Profit={str(p.get('profit','-')):<10} | PF={str(p.get('profit_factor','-')):<5} | "
            f"DD={str(p.get('drawdown_pct','-')):<5}% | Trades={str(p.get('trades','-')):<5} | {inps_txt}"
        )
    return "\n".join(lines)


def export_opt_md(
    opt_data: Dict[str, Any], ranked: List[Dict[str, Any]], top_n: int, sort_by: str
) -> str:
    shown = ranked[:top_n]
    varied = opt_data.get("varied_inputs", [])
    lines = [
        f"# Parallel Optimization Report: `{opt_data['report_name']}`",
        "",
        f"- **Source XML**: `{opt_data['source_xml']}`",
        f"- **Total Passes**: `{opt_data['total_passes']}` | **Profitable**: `{opt_data['profitable_passes']}` | **Matched**: `{len(ranked)} / {opt_data['total_passes']}` | **Shown**: `{len(shown)}`",
        f"- **Ranked By**: `{sort_by}`",
        "",
    ]
    if not shown:
        lines.append("> No passes matched the specified filters.")
        return "\n".join(lines)

    best = shown[0]
    lines.extend(
        [
            f"## Top Pass (`#{best.get('pass', '?')}`)",
            "",
            f"- **Result**: `{best.get('result')}` | **Profit**: `{best.get('profit')}` | **PF**: `{best.get('profit_factor')}` | **DD %**: `{best.get('drawdown_pct')}%` | **Sharpe**: `{best.get('sharpe')}` | **Trades**: `{best.get('trades')}`",
            "",
            "| Parameter | Value |",
            "|---|---|",
        ]
    )
    for k, v in best.get("inputs", {}).items():
        lines.append(f"| `{k}` | `{v}` |")
    lines.append("")

    cols = ["Rank", "Pass", "Symbol", "Result", "Profit", "Trades", "PF", "DD %", "Sharpe", "Recovery"]
    lines.extend(
        [
            f"## Leaderboard (Top {len(shown)})",
            "",
            "| " + " | ".join(cols + varied) + " |",
            "|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|" + "---:|" * len(varied),
        ]
    )
    for idx, p in enumerate(shown, start=1):
        row = [
            str(idx),
            str(p.get("pass", "")),
            str(p.get("symbol", "")),
            str(p.get("result", "")),
            str(p.get("profit", "")),
            str(p.get("trades", "")),
            str(p.get("profit_factor", "")),
            str(p.get("drawdown_pct", "")),
            str(p.get("sharpe", "")),
            str(p.get("recovery_factor", "")),
        ] + [str(p["inputs"].get(v, "")) for v in varied]
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def export_opt_json(
    opt_data: Dict[str, Any], ranked: List[Dict[str, Any]], top_n: int, sort_by: str
) -> str:
    payload = {
        "report_name": opt_data["report_name"],
        "source_xml": opt_data["source_xml"],
        "generated_at": opt_data["generated_at"],
        "sort_by": sort_by,
        "total_passes": opt_data["total_passes"],
        "profitable_passes": opt_data["profitable_passes"],
        "matched_passes": len(ranked),
        "shown_passes": len(ranked[:top_n]),
        "filtered_passes": len(ranked),
        "varied_inputs": opt_data["varied_inputs"],
        "best_pass": ranked[0] if ranked else None,
        "top_passes": ranked[:top_n],
        "all_passes": ranked,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _shared_html_head(title: str) -> str:
    esc = html_lib.escape
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{esc(title)}</title>
<script>
(function() {{
  try {{
    var t = localStorage.getItem('mt5_report_theme');
    if (!t && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) t = 'dark';
    if (t === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
  }} catch (e) {{}}
}})();
</script>
<style>
  :root {{
    --bg: #f1f5f9; --card: #ffffff; --card-alt: #f8fafc; --border: #e2e8f0;
    --text: #0f172a; --muted: #64748b; --accent: #0284c7; --accent-soft: rgba(2,132,199,0.10);
    --good: #059669; --good-bg: rgba(16,185,129,0.12);
    --bad: #dc2626; --bad-bg: rgba(239,68,68,0.12);
    --warn: #d97706; --warn-bg: rgba(245,158,11,0.13);
    --code-bg: #f1f5f9; --shadow: 0 1px 3px rgba(15,23,42,0.06), 0 1px 2px rgba(15,23,42,0.04);
  }}
  html[data-theme="dark"] {{
    --bg: #0b1120; --card: #111827; --card-alt: #1e293b; --border: #1f2937;
    --text: #f8fafc; --muted: #94a3b8; --accent: #38bdf8; --accent-soft: rgba(56,189,248,0.14);
    --good: #34d399; --good-bg: rgba(52,211,153,0.15);
    --bad: #f87171; --bad-bg: rgba(248,113,113,0.15);
    --warn: #fbbf24; --warn-bg: rgba(251,191,36,0.15);
    --code-bg: #1e293b; --shadow: 0 4px 12px rgba(0,0,0,0.35);
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; }}
  .wrap {{ max-width: 1360px; margin: 0 auto; padding: 24px 20px 48px; }}
  .header {{ display: flex; flex-wrap: wrap; justify-content: space-between; align-items: flex-start; gap: 16px; background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 20px 24px; margin-bottom: 20px; box-shadow: var(--shadow); }}
  .header-right {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .title {{ font-size: 1.42rem; font-weight: 750; margin: 0; letter-spacing: -0.015em; }}
  .subtitle {{ color: var(--muted); font-size: 0.88rem; margin-top: 6px; display: flex; flex-wrap: wrap; gap: 8px 14px; align-items: center; }}
  .badge {{ display: inline-flex; align-items: center; padding: 5px 13px; border-radius: 999px; font-weight: 700; font-size: 0.8rem; letter-spacing: 0.04em; color: #fff; }}
  .pill {{ display: inline-flex; align-items: center; padding: 2px 9px; border-radius: 999px; font-size: 0.78rem; font-weight: 600; background: var(--card-alt); border: 1px solid var(--border); color: var(--text); }}
  .pill.good {{ background: var(--good-bg); color: var(--good); border-color: transparent; }}
  .pill.bad {{ background: var(--bad-bg); color: var(--bad); border-color: transparent; }}
  .pill.warn {{ background: var(--warn-bg); color: var(--warn); border-color: transparent; }}
  .pill.accent {{ background: var(--accent-soft); color: var(--accent); border-color: transparent; }}
  .btn {{ cursor: pointer; border: 1px solid var(--border); background: var(--card-alt); color: var(--text); border-radius: 9px; padding: 6px 12px; font-size: 0.82rem; font-weight: 600; transition: all 0.15s ease; }}
  .btn:hover {{ border-color: var(--accent); color: var(--accent); }}
  .btn.primary {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
  .btn.primary:hover {{ opacity: 0.92; color: #fff; }}
  .btn.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
  .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(195px, 1fr)); gap: 12px; margin-bottom: 20px; }}
  .kpi {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; box-shadow: var(--shadow); }}
  .kpi-label {{ font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); font-weight: 600; }}
  .kpi-val {{ font-size: 1.24rem; font-weight: 750; margin-top: 5px; font-variant-numeric: tabular-nums; }}
  .kpi-sub {{ font-size: 0.78rem; color: var(--muted); margin-top: 3px; font-variant-numeric: tabular-nums; }}
  .kpi-val.good, .good {{ color: var(--good); }}
  .kpi-val.bad, .bad {{ color: var(--bad); }}
  .kpi-val.warn, .warn {{ color: var(--warn); }}
  .panel {{ background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 20px; margin-bottom: 20px; box-shadow: var(--shadow); }}
  .panel-head {{ display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 14px; }}
  .panel h2 {{ margin: 0; font-size: 1.06rem; font-weight: 700; }}
  .grid2 {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 20px; }}
  .charts-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(380px, 1fr)); gap: 16px; }}
  .charts-grid-3 {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }}
  .chart-card {{ background: var(--card-alt); border: 1px solid var(--border); border-radius: 10px; padding: 12px; }}
  .chart-card img {{ max-width: 100%; height: auto; border-radius: 6px; display: block; }}
  .chart-title {{ font-size: 0.8rem; color: var(--muted); margin-bottom: 8px; font-weight: 600; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.84rem; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }}
  th {{ background: var(--card-alt); position: sticky; top: 0; font-weight: 650; color: var(--muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.03em; z-index: 1; }}
  th.sortable {{ cursor: pointer; user-select: none; }}
  th.sortable:hover {{ color: var(--accent); }}
  tbody tr:hover {{ background: var(--accent-soft); }}
  td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .scroll-box {{ max-height: 460px; overflow: auto; border: 1px solid var(--border); border-radius: 10px; }}
  .toolbar {{ display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-bottom: 12px; }}
  .search-input {{ width: 100%; max-width: 340px; padding: 7px 12px; border: 1px solid var(--border); background: var(--card-alt); color: var(--text); border-radius: 8px; font-size: 0.86rem; outline: none; }}
  .search-input:focus {{ border-color: var(--accent); }}
  .muted {{ color: var(--muted); font-size: 0.84rem; }}
  .log-box {{ background: #0f172a; color: #e2e8f0; padding: 14px; border-radius: 10px; overflow-x: auto; font-size: 0.8rem; border: 1px solid var(--border); margin: 10px 0 0; }}
  code {{ background: var(--code-bg); padding: 2px 6px; border-radius: 5px; font-size: 0.83em; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
  .winbar-wrap {{ margin-top: 8px; height: 8px; border-radius: 999px; background: var(--bad-bg); overflow: hidden; display: flex; }}
  .winbar-fill {{ background: var(--good); height: 100%; }}
  .group-row td {{ background: var(--card-alt); font-weight: 700; color: var(--accent); font-size: 0.78rem; letter-spacing: 0.03em; }}
  .svg-wrap {{ width: 100%; overflow: hidden; }}
  .svg-legend {{ display: flex; flex-wrap: wrap; gap: 16px; margin-bottom: 10px; font-size: 0.84rem; }}
  .legend-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }}
  .diff-cell {{ background: var(--warn-bg); font-weight: 700; }}
  @media (max-width: 640px) {{ .grid2, .charts-grid, .charts-grid-3 {{ grid-template-columns: 1fr; }} }}
</style>
</head>"""


def _shared_theme_script() -> str:
    return """<script>
function toggleTheme() {
  const root = document.documentElement;
  const isDark = root.getAttribute('data-theme') === 'dark';
  const next = isDark ? 'light' : 'dark';
  if (next === 'dark') root.setAttribute('data-theme', 'dark');
  else root.removeAttribute('data-theme');
  try { localStorage.setItem('mt5_report_theme', next); } catch (e) {}
  updateThemeBtn();
}
function updateThemeBtn() {
  const btn = document.getElementById('themeBtn');
  if (!btn) return;
  const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  btn.textContent = isDark ? 'Light Mode' : 'Dark Mode';
}
updateThemeBtn();
</script>"""


def _extract_balance_series(deals: List[Dict[str, Any]], max_points: int = 400) -> List[float]:
    pts: List[float] = []
    for dl in deals:
        val = _parse_mt5_float(str(dl.get("balance", "")))
        if val is not None:
            pts.append(val)
    if len(pts) <= max_points:
        return pts
    step = (len(pts) - 1) / float(max_points - 1)
    sampled = [pts[int(round(i * step))] for i in range(max_points - 1)]
    sampled.append(pts[-1])
    return sampled


def _build_svg_balance_curve(
    series_list: List[Tuple[str, str, List[float]]],
    width: int = 960,
    height: int = 230,
) -> str:
    valid = [(lbl, clr, pts) for lbl, clr, pts in series_list if len(pts) >= 2]
    if not valid:
        return "<p class='muted'>Not enough balance points in deals table to render equity curve.</p>"

    esc = html_lib.escape
    all_vals = [v for _, _, pts in valid for v in pts]
    min_y = min(all_vals)
    max_y = max(all_vals)
    if abs(max_y - min_y) < 1e-9:
        min_y -= 1.0
        max_y += 1.0
    pad_l, pad_r, pad_t, pad_b = 76, 18, 16, 26
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    grid_lines: List[str] = []
    for i in range(5):
        frac = i / 4.0
        y = pad_t + frac * plot_h
        val = max_y - frac * (max_y - min_y)
        grid_lines.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
            f'stroke="var(--border)" stroke-dasharray="4 4" stroke-width="1"/>'
        )
        grid_lines.append(
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" '
            f'fill="var(--muted)" font-size="11">{val:,.0f}</text>'
        )

    paths_svg: List[str] = []
    legend_items: List[str] = []
    for s_idx, (lbl, clr, pts) in enumerate(valid):
        n = len(pts)
        coords: List[str] = []
        for idx, val in enumerate(pts):
            x = pad_l + (idx / float(n - 1)) * plot_w
            y = pad_t + (1.0 - (val - min_y) / (max_y - min_y)) * plot_h
            coords.append(f"{x:.1f},{y:.1f}")
        pts_str = " ".join(coords)
        if s_idx == 0:
            area_pts = f"{pad_l:.1f},{pad_t + plot_h:.1f} {pts_str} {pad_l + plot_w:.1f},{pad_t + plot_h:.1f}"
            paths_svg.append(
                f'<defs><linearGradient id="balGrad" x1="0" y1="0" x2="0" y2="1">'
                f'<stop offset="0%" stop-color="{clr}" stop-opacity="0.22"/>'
                f'<stop offset="100%" stop-color="{clr}" stop-opacity="0.01"/>'
                f'</linearGradient></defs>'
                f'<polygon points="{area_pts}" fill="url(#balGrad)"/>'
            )
        paths_svg.append(
            f'<polyline fill="none" stroke="{clr}" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round" points="{pts_str}"/>'
        )
        last_x, last_y = coords[-1].split(",")
        paths_svg.append(f'<circle cx="{last_x}" cy="{last_y}" r="4" fill="{clr}" />')

        delta = pts[-1] - pts[0]
        delta_cls = "good" if delta >= 0 else "bad"
        legend_items.append(
            f'<span><span class="legend-dot" style="background:{clr}"></span>'
            f'<b>{esc(lbl)}</b>: {pts[0]:,.2f} &rarr; <b>{pts[-1]:,.2f}</b> '
            f'<span class="{delta_cls}">({delta:+,.2f})</span></span>'
        )

    return (
        f'<div class="svg-legend">{"".join(legend_items)}</div>'
        f'<div class="svg-wrap"><svg viewBox="0 0 {width} {height}" width="100%" height="auto" preserveAspectRatio="xMidYMid meet">'
        + "".join(grid_lines)
        + "".join(paths_svg)
        + f'<text x="{pad_l}" y="{height - 6}" fill="var(--muted)" font-size="11">Start</text>'
        + f'<text x="{width - pad_r}" y="{height - 6}" text-anchor="end" fill="var(--muted)" font-size="11">End of Test</text>'
        + "</svg></div>"
    )


def _build_svg_opt_bars(ranked: List[Dict[str, Any]], sort_by: str, max_bars: int = 10) -> str:
    items = ranked[:max_bars]
    if not items:
        return "<p class='muted'>No passes to visualize.</p>"
    esc = html_lib.escape
    metric_key = sort_by if sort_by in ("profit", "profit_factor", "sharpe", "recovery_factor", "expected_payoff", "drawdown_pct", "result", "trades") else "profit"

    vals: List[float] = []
    for p in items:
        v = p.get(metric_key)
        if v is None:
            v = p.get("profit") or 0.0
        vals.append(float(v))

    max_abs = max((abs(v) for v in vals), default=1.0) or 1.0
    row_h = 28
    pad_l, pad_r, pad_t = 150, 95, 10
    width = 640
    height = pad_t + len(items) * row_h + 10
    bar_max_w = width - pad_l - pad_r

    bars: List[str] = []
    for idx, (p, val) in enumerate(zip(items, vals)):
        y = pad_t + idx * row_h
        bw = max(4.0, (abs(val) / max_abs) * bar_max_w)
        clr = "#10b981" if val >= 0 else "#ef4444"
        if metric_key == "drawdown_pct":
            clr = "#0284c7"
        lbl = f"Pass #{p.get('pass', '?')} ({p.get('symbol', '')})"
        if len(lbl) > 20:
            lbl = lbl[:19] + "…"
        bars.append(
            f'<text x="{pad_l - 10}" y="{y + 17}" text-anchor="end" fill="var(--text)" font-size="11.5" font-weight="600">{esc(lbl)}</text>'
            f'<rect x="{pad_l}" y="{y + 5}" width="{bw:.1f}" height="17" rx="4" fill="{clr}" opacity="0.88"/>'
            f'<text x="{pad_l + bw + 8:.1f}" y="{y + 17}" fill="var(--muted)" font-size="11.5" font-weight="600">{val:,.2f}</text>'
        )

    return f'<div class="svg-wrap"><svg viewBox="0 0 {width} {height}" width="100%" height="auto">{"".join(bars)}</svg></div>'


def export_opt_html(
    opt_data: Dict[str, Any], ranked: List[Dict[str, Any]], top_n: int, sort_by: str
) -> str:
    esc = html_lib.escape
    shown = ranked[:top_n]
    varied = opt_data.get("varied_inputs", [])
    best = shown[0] if shown else {}
    total_p = opt_data["total_passes"] or 0
    prof_p = opt_data["profitable_passes"] or 0
    prof_pct = (prof_p / total_p * 100.0) if total_p else 0.0

    best_set_lines = "\n".join(f"{k}={v}" for k, v in best.get("inputs", {}).items())
    best_param_rows = "\n".join(
        f"<tr><td><code>{esc(k)}</code></td><td class='num'><b>{esc(str(v))}</b></td></tr>"
        for k, v in best.get("inputs", {}).items()
    )

    headers = ["Rank", "Pass", "Symbol", "Result", "Profit", "Trades", "PF", "DD %", "Sharpe", "Recovery"] + varied
    th_html = "".join(
        f'<th class="sortable {"num" if h not in ("Symbol",) else ""}" onclick="sortOpt({i})">{esc(h)}</th>'
        for i, h in enumerate(headers)
    )

    rows_html = []
    for idx, p in enumerate(shown, start=1):
        rank_badge = f'<span class="pill {"good" if idx == 1 else ("accent" if idx <= 3 else "")}">#{idx}</span>'
        profit_val = p.get("profit")
        profit_cls = "good" if isinstance(profit_val, (int, float)) and profit_val > 0 else ("bad" if isinstance(profit_val, (int, float)) and profit_val < 0 else "")
        cells = [
            f'<td data-val="{idx}">{rank_badge}</td>',
            f'<td class="num" data-val="{esc(str(p.get("pass", "")))}"><b>#{esc(str(p.get("pass", "")))}</b></td>',
            f'<td data-val="{esc(str(p.get("symbol", "")))}"><b>{esc(str(p.get("symbol", "")))}</b></td>',
            f'<td class="num" data-val="{esc(str(p.get("result", "")))}">{esc(str(p.get("result", "")))}</td>',
            f'<td class="num {profit_cls}" data-val="{esc(str(profit_val if profit_val is not None else ""))}"><b>{f"{profit_val:,.2f}" if isinstance(profit_val, (int, float)) else ""}</b></td>',
            f'<td class="num" data-val="{esc(str(p.get("trades", "")))}">{esc(str(p.get("trades", "")))}</td>',
            f'<td class="num" data-val="{esc(str(p.get("profit_factor", "")))}">{esc(str(p.get("profit_factor", "")))}</td>',
            f'<td class="num" data-val="{esc(str(p.get("drawdown_pct", "")))}">{esc(str(p.get("drawdown_pct", "")))}%</td>',
            f'<td class="num" data-val="{esc(str(p.get("sharpe", "")))}">{esc(str(p.get("sharpe", "")))}</td>',
            f'<td class="num" data-val="{esc(str(p.get("recovery_factor", "")))}">{esc(str(p.get("recovery_factor", "")))}</td>',
        ]
        for v in varied:
            iv = p["inputs"].get(v, "")
            cells.append(f'<td class="num" data-val="{esc(str(iv))}"><code>{esc(str(iv))}</code></td>')
        is_prof = "1" if isinstance(profit_val, (int, float)) and profit_val > 0 else "0"
        rows_html.append(f'<tr data-prof="{is_prof}">' + "".join(cells) + "</tr>")

    bars_svg = _build_svg_opt_bars(ranked, sort_by=sort_by, max_bars=10)
    best_prof = best.get("profit")
    best_prof_cls = "good" if isinstance(best_prof, (int, float)) and best_prof > 0 else "bad"

    return (
        _shared_html_head(f"MT5 Parallel Optimization — {opt_data['report_name']}")
        + f"""
<body>
<div class="wrap">
  <div class="header">
    <div>
      <h1 class="title">Parallel Optimization: {esc(opt_data['report_name'])}</h1>
      <div class="subtitle">
        <span>Source: <code>{esc(opt_data['source_xml'])}</code></span>
        <span class="pill accent">Ranked by: {esc(sort_by)}</span>
        <span class="pill">Matched: {len(ranked)} / {total_p} passes (Shown: {len(shown)})</span>
      </div>
    </div>
    <div class="header-right">
      <span class="badge" style="background:#0284c7">OPTIMIZATION XML</span>
      <button id="themeBtn" class="btn" onclick="toggleTheme()">Dark Mode</button>
    </div>
  </div>

  <div class="kpis">
    <div class="kpi"><div class="kpi-label">Total Passes</div><div class="kpi-val">{total_p}</div><div class="kpi-sub">Multi-core agent sweep</div></div>
    <div class="kpi"><div class="kpi-label">Profitable Passes</div><div class="kpi-val good">{prof_p}</div><div class="kpi-sub">{prof_pct:.1f}% profitable</div></div>
    <div class="kpi"><div class="kpi-label">Winning Pass</div><div class="kpi-val">#{esc(str(best.get('pass','-')))}</div><div class="kpi-sub">{esc(str(best.get('symbol','-')))}</div></div>
    <div class="kpi"><div class="kpi-label">Best Net Profit</div><div class="kpi-val {best_prof_cls}">{f"{best_prof:,.2f}" if isinstance(best_prof, (int, float)) else "-"}</div><div class="kpi-sub">Trades: {esc(str(best.get('trades','-')))}</div></div>
    <div class="kpi"><div class="kpi-label">Best Profit Factor</div><div class="kpi-val">{esc(str(best.get('profit_factor','-')))}</div><div class="kpi-sub">Sharpe: {esc(str(best.get('sharpe','-')))}</div></div>
    <div class="kpi"><div class="kpi-label">Best Drawdown %</div><div class="kpi-val warn">{esc(str(best.get('drawdown_pct','-')))}%</div><div class="kpi-sub">Recovery: {esc(str(best.get('recovery_factor','-')))}</div></div>
  </div>

  <div class="grid2">
    <div class="panel">
      <div class="panel-head">
        <h2>Top Pass Spotlight (#{esc(str(best.get('pass','-')))})</h2>
        <button id="copySetBtn" class="btn primary" onclick="copyBestSet()">Copy .set Inputs</button>
      </div>
      <textarea id="bestSetPayload" style="position:absolute;left:-9999px">{esc(best_set_lines)}</textarea>
      <div class="scroll-box" style="max-height:310px">
        <table><thead><tr><th>Parameter</th><th class="num">Value</th></tr></thead><tbody>{best_param_rows}</tbody></table>
      </div>
    </div>
    <div class="panel">
      <div class="panel-head">
        <h2>Top 10 Passes ({esc(sort_by)})</h2>
      </div>
      {bars_svg}
    </div>
  </div>

  <div class="panel">
    <div class="panel-head">
      <h2>Leaderboard (Top {len(shown)} Passes)</h2>
      <span id="optCount" class="muted">{len(shown)} visible</span>
    </div>
    <div class="toolbar">
      <input class="search-input" id="optFilter" placeholder="Filter by symbol, pass #, or parameter..." oninput="filterOpt()" />
      <label class="btn" style="display:inline-flex;align-items:center;gap:6px">
        <input type="checkbox" id="profOnly" onchange="filterOpt()" /> Profitable only
      </label>
    </div>
    <div class="scroll-box" style="max-height:560px">
      <table id="optTable">
        <thead><tr>{th_html}</tr></thead>
        <tbody>{''.join(rows_html)}</tbody>
      </table>
    </div>
  </div>
</div>
<script>
let sortCol = -1, sortAsc = false;
function filterOpt() {{
  const q = document.getElementById('optFilter').value.toLowerCase();
  const profOnly = document.getElementById('profOnly').checked;
  let visible = 0;
  for (const r of document.querySelectorAll('#optTable tbody tr')) {{
    const okQ = r.textContent.toLowerCase().includes(q);
    const okP = !profOnly || r.getAttribute('data-prof') === '1';
    const show = okQ && okP;
    r.style.display = show ? '' : 'none';
    if (show) visible++;
  }}
  document.getElementById('optCount').textContent = visible + ' visible';
}}
function sortOpt(col) {{
  const tbody = document.querySelector('#optTable tbody');
  const rows = Array.from(tbody.querySelectorAll('tr'));
  sortAsc = (sortCol === col) ? !sortAsc : (col === 0 || col === 7);
  sortCol = col;
  rows.sort((a, b) => {{
    const va = (a.children[col].getAttribute('data-val') || a.children[col].textContent).trim();
    const vb = (b.children[col].getAttribute('data-val') || b.children[col].textContent).trim();
    const na = parseFloat(va), nb = parseFloat(vb);
    if (!isNaN(na) && !isNaN(nb)) return sortAsc ? na - nb : nb - na;
    return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
  }});
  rows.forEach(r => tbody.appendChild(r));
}}
function copyBestSet() {{
  const text = document.getElementById('bestSetPayload').value;
  const btn = document.getElementById('copySetBtn');
  if (navigator.clipboard) navigator.clipboard.writeText(text);
  btn.textContent = 'Copied!';
  setTimeout(() => {{ btn.textContent = 'Copy .set Inputs'; }}, 1800);
}}
</script>
"""
        + _shared_theme_script()
        + "</body>\n</html>\n"
    )


def cmd_optimize(args: argparse.Namespace) -> int:
    paths = resolve_env_paths(args)
    report_name = args.report
    if re.search(r"[\\/:.]", report_name):
        raise ValueError(f"--report must be a bare name without paths or extension: {report_name}")

    if not args.profile and not args.set_file and not args.from_source and not args.from_report:
        if not (args.expert and args.symbol and args.period):
            raise ValueError(
                "Provide --profile (recommended), --set-file, --from-report, or --from-source."
            )

    inputs, tester_from_prof, source_meta = _build_base_inputs(
        paths, args.profile, args.set_file, args.from_source, args.source, args.from_report
    )

    if not args.keep_profile_opt:
        for entry in inputs.values():
            entry["optimize"] = "N"

    overrides = parse_kv_overrides(args.set, args.set_json)
    opt_overrides = parse_opt_overrides(args.opt)
    change_notes = _apply_input_overrides(
        inputs, overrides, opt_overrides, source_meta, args.allow_new
    )

    combos, active_params = _estimate_opt_combinations(inputs)
    if not active_params:
        raise ValueError(
            "No optimization parameters enabled (||Y). Pass --opt Key=start:step:stop (e.g. --opt InpFastMAPeriod=10:5:40)."
        )

    tester: Dict[str, str] = dict(tester_from_prof or {})
    if args.expert:
        tester["Expert"] = args.expert
    if args.symbol:
        tester["Symbol"] = args.symbol
    if args.period:
        tester["Period"] = args.period
    if args.model is not None:
        tester["Model"] = str(args.model)
    if args.from_date:
        tester["FromDate"] = args.from_date
        tester.setdefault("Dates", "1")
    if args.to_date:
        tester["ToDate"] = args.to_date
        tester.setdefault("Dates", "1")
    if args.deposit is not None:
        tester["Deposit"] = str(args.deposit)
    if args.currency:
        tester["Currency"] = args.currency
    if args.leverage is not None:
        tester["Leverage"] = str(args.leverage)

    tester["Optimization"] = str(args.mode)
    tester["OptimizationCriterion"] = str(args.criterion)
    tester["UseLocal"] = "1"
    tester["UseRemote"] = "0"
    tester["UseCloud"] = "0"
    tester["Report"] = report_name
    tester["ReplaceReport"] = "1"
    tester["ShutdownTerminal"] = "1"
    tester["Visual"] = "0"
    tester.setdefault("ForwardMode", "0")
    tester.setdefault("Model", "1")

    if "Expert" not in tester or "Symbol" not in tester or "Period" not in tester:
        raise ValueError("[Tester] requires Expert, Symbol, and Period")
    tester["Expert"] = resolve_expert_ini_path(tester["Expert"], paths)

    common_lines, auth_status = _build_common_auth_lines(
        require_all=getattr(args, "use_env_auth", False)
    )

    ini_path = args.ini_out or os.path.join(paths["work_dir"], f"{report_name}.ini")
    ini_lines = [
        f"; Generated by mt5-headless-tester optimize on {dt.datetime.now().strftime('%Y.%m.%d %H:%M:%S')}",
        *common_lines,
        "[Tester]",
    ]
    for k, v in tester.items():
        ini_lines.append(f"{k}={v}")
    ini_lines.append("[TesterInputs]")
    for k, entry in inputs.items():
        ini_lines.append(f"{k}={format_input_entry(entry)}")

    write_utf16le_bom(ini_path, ini_lines)
    abs_ini = os.path.abspath(ini_path)
    terminal_exe = paths["terminal_exe"]
    portable_flag = " /portable" if paths["portable"] else ""
    raw_cmd = f'"{terminal_exe}"{portable_flag} /config:{abs_ini}'
    ghost_payload = json.dumps({"op": "launch", "exe": raw_cmd})
    agents_cnt = _count_local_agents(paths["tester_dir"])

    print(f"Opt INI  : {abs_ini}")
    print(f"Mode     : {'PORTABLE (/portable)' if paths['portable'] else 'STANDARD'} | Opt={tester['Optimization']} ({'Complete' if tester['Optimization']=='1' else 'Genetic'}), Criterion={tester['OptimizationCriterion']}")
    print(f"Launch   : {paths['launch_mode']} (--run uses built-in {paths['launch_mode']} launcher; or use external Ghost MCP if installed)")
    print(f"Expert   : {tester['Expert']} | {tester['Symbol']} {tester['Period']} | Model={tester['Model']}")
    print(f"Parallel : {len(active_params)} parameter(s) ({', '.join(active_params)}), ~{combos} grid combinations, {agents_cnt} local agent(s) cached")
    if auth_status["any"]:
        print(
            "Auth     : "
            + f"SERVER={'[SET]' if auth_status['server'] else '[UNSET]'}, "
            + f"LOGIN={'[SET]' if auth_status['login'] else '[UNSET]'}, "
            + f"PASSWORD={'[SET]' if auth_status['password'] else '[UNSET]'} (injected via env vars)"
        )
    for n in change_notes:
        print(f"  * {n}")
    print(f"GhostMCP : {ghost_payload}  (optional external mcp__ghost_window)")
    print(f"Command  : {raw_cmd}")

    if not args.run:
        if auth_status["password"]:
            print(f"Security : After terminal exits, run 'scrub-ini \"{abs_ini}\"'")
        print(f"\nAfter terminal exits, export leaderboard & best .set via:")
        print(f"  report {report_name}.xml --profile \"{abs_ini}\" --sort-by {args.sort_by} --top {args.top} --save-best-to-tester \"{report_name}_best\"")
        return 0

    _run_terminal_minimized(paths, abs_ini, report_name)
    opt_data = parse_optimization_xml(report_name, paths)
    ranked = filter_and_sort_opt_passes(
        opt_data,
        sort_by=args.sort_by,
        min_trades=args.min_trades,
        min_pf=args.min_pf,
        max_dd_pct=args.max_dd_pct,
        require_positive=args.require_positive,
    )

    out_set = args.export_best_set
    if args.save_best_to_tester:
        name = args.save_best_to_tester
        if not name.lower().endswith(".set"):
            name += ".set"
        out_set = os.path.join(paths["profiles_tester_dir"], name)

    if out_set and (ranked or opt_data["passes"]):
        pool = ranked or opt_data["passes"]
        picked = (
            next((p for p in pool if p.get("pass") == args.pick_pass), None)
            if args.pick_pass is not None
            else pool[0]
        )
        if not picked:
            raise ValueError(f"Pass #{args.pick_pass} not found in optimization XML.")
        cnt = _export_pass_to_set(picked, out_set, inputs, report_name)
        print(f"Best .set: {os.path.abspath(out_set)} (pass #{picked.get('pass','?')}, {cnt} inputs, UTF-16LE BOM)")

    fmt = _infer_format(args.format, args.out, default="text")
    if fmt == "md":
        content = export_opt_md(opt_data, ranked, args.top, args.sort_by)
    elif fmt == "json":
        content = export_opt_json(opt_data, ranked, args.top, args.sort_by)
    elif fmt == "html":
        content = export_opt_html(opt_data, ranked, args.top, args.sort_by)
    else:
        content = export_opt_text(opt_data, ranked, args.top, args.sort_by)
    return _emit_text_or_file(content, args.out)


# ---------------------------------------------------------------------------
# Subcommand: report (Parser + Exporters)
# ---------------------------------------------------------------------------


def _extract_cells(row_html: str) -> List[str]:
    return [
        html_lib.unescape(re.sub(r"<[^>]+>", "", c)).strip()
        for c in re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S | re.I)
    ]


def _parse_mt5_float(text: str) -> Optional[float]:
    cleaned = text.replace("\xa0", " ").replace(" ", "").strip()
    m = re.match(r"^([+-]?\d+(?:\.\d+)?)", cleaned)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _parse_metric_structured(raw: str) -> Dict[str, Any]:
    """Parse MT5 metric formatting like '15 797.98 (42.31%)' or '42.31% (15 797.98)'."""
    s = raw.strip()
    out: Dict[str, Any] = {"raw": s}
    m_val_pct = re.match(r"^([+-]?[\d\s.]+)\s*\(([+-]?[\d\s.]+)%\)$", s)
    if m_val_pct:
        out["value"] = _parse_mt5_float(m_val_pct.group(1))
        out["pct"] = _parse_mt5_float(m_val_pct.group(2))
        return out
    m_pct_val = re.match(r"^([+-]?[\d\s.]+)%\s*\(([+-]?[\d\s.]+)\)$", s)
    if m_pct_val:
        out["pct"] = _parse_mt5_float(m_pct_val.group(1))
        out["value"] = _parse_mt5_float(m_pct_val.group(2))
        return out
    m_val_num = re.match(r"^([+-]?[\d\s.]+)\s*\(([+-]?[\d\s.]+)\)$", s)
    if m_val_num:
        out["value"] = _parse_mt5_float(m_val_num.group(1))
        out["secondary"] = _parse_mt5_float(m_val_num.group(2))
        return out
    val = _parse_mt5_float(s)
    if val is not None:
        out["value"] = val
    return out


def _scan_mt5_logs(paths: Dict[str, str], htm_path: str) -> Dict[str, Any]:
    """Read newest tester agent and terminal logs for pass status, duration, and errors."""
    htm_mtime = os.path.getmtime(htm_path) if os.path.isfile(htm_path) else time.time()
    agent_logs: List[Tuple[float, str]] = []
    if os.path.isdir(paths["tester_dir"]):
        for agent in os.listdir(paths["tester_dir"]):
            logs_dir = os.path.join(paths["tester_dir"], agent, "logs")
            if os.path.isdir(logs_dir):
                for fname in os.listdir(logs_dir):
                    if fname.lower().endswith(".log"):
                        fpath = os.path.join(logs_dir, fname)
                        agent_logs.append((abs(os.path.getmtime(fpath) - htm_mtime), fpath))

    agent_logs.sort(key=lambda x: x[0])
    newest_agent_log = agent_logs[0][1] if agent_logs else None

    term_logs: List[Tuple[float, str]] = []
    term_logs_dir = os.path.join(paths["data_dir"], "logs")
    if os.path.isdir(term_logs_dir):
        for fname in os.listdir(term_logs_dir):
            if fname.lower().endswith(".log"):
                fpath = os.path.join(term_logs_dir, fname)
                term_logs.append((abs(os.path.getmtime(fpath) - htm_mtime), fpath))
    term_logs.sort(key=lambda x: x[0])
    newest_term_log = term_logs[0][1] if term_logs else None

    test_passed_duration = None
    final_balance_line = None
    loaded_expert = None
    errors: List[str] = []

    err_re = re.compile(r"\b(error|init_failed|cannot|not found|failed)\b", re.I)

    if newest_agent_log:
        content = read_text_auto(newest_agent_log)
        passed = re.findall(r"Test passed in ([0-9:.]+)", content)
        if passed:
            test_passed_duration = passed[-1].rstrip(".")
        fb = re.findall(r"final balance [^\r\n]+", content)
        if fb:
            final_balance_line = fb[-1].strip()
        loaded_experts = re.findall(
            r"expert file added:\s*([^\r\n]+?)(?:\.\s+\d+\s+bytes loaded|$)",
            content,
            re.I,
        )
        if loaded_experts:
            loaded_expert = loaded_experts[-1].strip()
        for ln in content.splitlines()[-400:]:
            if err_re.search(ln):
                errors.append(f"[agent] {ln.strip()}")

    if newest_term_log:
        content = read_text_auto(newest_term_log)
        for ln in content.splitlines()[-250:]:
            if err_re.search(ln):
                errors.append(f"[terminal] {ln.strip()}")

    return {
        "agent_log": newest_agent_log,
        "terminal_log": newest_term_log,
        "test_passed_in": test_passed_duration,
        "final_balance_line": final_balance_line,
        "loaded_expert": loaded_expert,
        "error_lines": errors[-25:],
    }


def _infer_input_type(val: str) -> str:
    """Infer MQL5 input type from a serialized string value when .mq5 source is unavailable."""
    s = val.strip()
    if s.lower() in ("true", "false"):
        return "bool"
    if re.match(r"^[+-]?\d+$", s):
        return "int/enum"
    if re.match(r"^[+-]?\d+\.\d+$", s):
        return "double"
    return "string"


def parse_htm_report(
    report_arg: str, paths: Dict[str, str], embed_images: bool = False
) -> Dict[str, Any]:
    """Parse an MT5 Strategy Tester .htm report into structured data."""
    report_name, htm_path = resolve_report_htm_path(report_arg, paths)
    page = read_text_auto(htm_path)

    settings: Dict[str, str] = {}
    inputs: Dict[str, str] = {}
    inputs_meta: List[Dict[str, str]] = []
    metrics_raw: Dict[str, str] = {}

    results_idx = page.find("<b>Results</b>")
    orders_idx = page.find("<b>Orders</b>")
    deals_idx = page.find("<b>Deals</b>")

    settings_slice = page[:results_idx] if results_idx != -1 else page
    in_inputs = False
    current_group = ""
    kv_re = re.compile(r"^([A-Za-z_]\w*)=(.*)$")

    def _record_htm_input(cell_txt: str) -> None:
        nonlocal current_group
        m_kv = kv_re.match(cell_txt)
        if m_kv:
            k, v = m_kv.group(1), m_kv.group(2).strip()
            inputs[k] = v
            inputs_meta.append(
                {
                    "name": k,
                    "value": v,
                    "group": current_group,
                    "inferred_type": _infer_input_type(v),
                }
            )
        elif cell_txt:
            current_group = cell_txt

    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", settings_slice, re.S | re.I):
        c = _extract_cells(row)
        if not c:
            continue
        if len(c) >= 2 and c[0] == "Inputs:":
            in_inputs = True
            _record_htm_input(c[1].strip())
            continue
        if in_inputs and len(c) >= 2 and c[0] == "":
            _record_htm_input(c[1].strip())
            continue
        if len(c) >= 2 and c[0].endswith(":"):
            in_inputs = False
            settings[c[0][:-1].strip()] = c[1].strip()

    if results_idx != -1:
        end_res = orders_idx if orders_idx != -1 else (deals_idx if deals_idx != -1 else len(page))
        res_slice = page[results_idx:end_res]
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", res_slice, re.S | re.I):
            c = _extract_cells(row)
            for i in range(len(c) - 1):
                if c[i].endswith(":") and not c[i + 1].endswith(":"):
                    metrics_raw.setdefault(c[i][:-1].strip(), c[i + 1].strip())

    orders: List[Dict[str, str]] = []
    if orders_idx != -1:
        end_ord = deals_idx if deals_idx != -1 else len(page)
        ord_slice = page[orders_idx:end_ord]
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", ord_slice, re.S | re.I):
            c = _extract_cells(row)
            if len(c) >= 11 and c[1].isdigit():
                orders.append(dict(zip(ORDER_COLUMNS, c[:11])))

    deals: List[Dict[str, str]] = []
    deals_pipe_rows: List[str] = []
    stop_outs: List[Dict[str, str]] = []
    if deals_idx != -1:
        deals_slice = page[deals_idx:]
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", deals_slice, re.S | re.I):
            c = _extract_cells(row)
            if len(c) >= 13 and c[1].isdigit():
                row_vals = c[:13]
                deals_pipe_rows.append("|".join(row_vals))
                deal_obj = dict(zip(DEAL_COLUMNS, row_vals))
                deals.append(deal_obj)
                cmt = deal_obj.get("comment", "").strip().lower()
                if cmt.startswith("so") or "stop out" in cmt:
                    stop_outs.append(deal_obj)

    deals_sha256 = hashlib.sha256("\n".join(deals_pipe_rows).encode("utf-8")).hexdigest()[:16]
    no_time_rows = [r.split("|", 1)[1] for r in deals_pipe_rows]
    deals_no_time_sha256 = hashlib.sha256("\n".join(no_time_rows).encode("utf-8")).hexdigest()[:16]

    metrics_numeric = {k: _parse_metric_structured(v) for k, v in metrics_raw.items()}

    # Locate chart PNGs referenced in the HTML or beside the .htm
    htm_dir = os.path.dirname(htm_path)
    charts: List[Dict[str, str]] = []
    seen_imgs: set[str] = set()
    for src_attr in re.findall(r'<img[^>]+src="([^"]+)"', page, re.I):
        img_path = os.path.join(htm_dir, src_attr)
        if os.path.isfile(img_path) and img_path not in seen_imgs:
            seen_imgs.add(img_path)
            entry = {"filename": os.path.basename(img_path), "path": img_path}
            if embed_images:
                with open(img_path, "rb") as imf:
                    b64 = base64.b64encode(imf.read()).decode("ascii")
                entry["data_uri"] = f"data:image/png;base64,{b64}"
            charts.append(entry)

    diagnostics = _scan_mt5_logs(paths, htm_path)
    diagnostics["stop_out_detected"] = len(stop_outs) > 0
    diagnostics["stop_out_count"] = len(stop_outs)

    total_trades_val = metrics_numeric.get("Total Trades", {}).get("value", 0) or 0
    if diagnostics["stop_out_detected"]:
        verdict = "STOP OUT"
    elif total_trades_val == 0 and len(deals) <= 1:
        verdict = "NO TRADES"
    elif diagnostics["test_passed_in"]:
        verdict = "PASSED"
    else:
        verdict = "CHECK LOGS"

    return {
        "report_name": report_name,
        "source_htm": htm_path,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "verdict": verdict,
        "settings": settings,
        "inputs": inputs,
        "inputs_meta": inputs_meta,
        "metrics_raw": metrics_raw,
        "metrics_numeric": metrics_numeric,
        "deals_count": len(deals),
        "deals_sha256": deals_sha256,
        "deals_no_time_sha256": deals_no_time_sha256,
        "orders_count": len(orders),
        "charts": charts,
        "diagnostics": diagnostics,
        "orders": orders,
        "deals": deals,
        "deals_pipe_rows": deals_pipe_rows,
    }


def export_report_text(rep: Dict[str, Any]) -> str:
    m = rep["metrics_raw"]
    d = rep["diagnostics"]
    s = rep["settings"]
    lines = [
        f"Report                   : {rep['report_name']} ({rep['verdict']})",
        f"Expert / Symbol / Period : {s.get('Expert','?')} | {s.get('Symbol','?')} | {s.get('Period','?')}",
    ]
    for key in [
        "Total Net Profit",
        "Total Trades",
        "Balance Drawdown Maximal",
        "Equity Drawdown Maximal",
        "Profit Factor",
        "Sharpe Ratio",
        "Recovery Factor",
        "Expected Payoff",
    ]:
        lines.append(f"{key:25}: {m.get(key, '?')}")
    lines.append(
        f"{'Deals':25}: {rep['deals_count']} rows, sha256 {rep['deals_sha256']} (no-time {rep['deals_no_time_sha256']})"
    )
    lines.append(
        f"{'Test passed in':25}: {d.get('test_passed_in') or '?'}  (log: {d.get('agent_log') or '?'})"
    )
    if d.get("stop_out_detected"):
        lines.append(f"{'WARNING':25}: Stop-out detected ({d['stop_out_count']} deal(s))!")
    return "\n".join(lines)


def export_report_md(
    rep: Dict[str, Any],
    include_deals: bool = False,
    max_deals: int = 200,
    include_orders: bool = False,
) -> str:
    s = rep["settings"]
    m = rep["metrics_raw"]
    d = rep["diagnostics"]

    lines = [
        f"# Strategy Tester Report: `{rep['report_name']}`",
        "",
        f"- **Status**: **{rep['verdict']}**",
        f"- **Expert**: `{s.get('Expert', '?')}` | **Symbol**: `{s.get('Symbol', '?')}` | **Period**: `{s.get('Period', '?')}`",
        f"- **Broker / Currency / Deposit / Leverage**: {s.get('Company', '?')} | {s.get('Currency', '?')} | {s.get('Initial Deposit', '?')} | {s.get('Leverage', '?')}",
        f"- **Deals Fingerprint**: `{rep['deals_count']}` rows | `sha256`: `{rep['deals_sha256']}` | `no-time sha256`: `{rep['deals_no_time_sha256']}`",
        f"- **Test Duration**: `{d.get('test_passed_in') or '?'}`",
        "",
    ]
    if d.get("stop_out_detected"):
        lines.extend(
            [
                f"> [!WARNING]",
                f"> Account stop-out detected in {d['stop_out_count']} deal(s). Check `Bars` and `Equity Drawdown`.",
                "",
            ]
        )

    lines.extend(["## Performance & Risk Metrics", "", "| Metric | Value |", "|---|---|"])
    ordered_keys = [
        "History Quality",
        "Bars",
        "Ticks",
        "Total Net Profit",
        "Gross Profit",
        "Gross Loss",
        "Profit Factor",
        "Expected Payoff",
        "Recovery Factor",
        "Sharpe Ratio",
        "LR Correlation",
        "OnTester result",
        "Balance Drawdown Absolute",
        "Balance Drawdown Maximal",
        "Balance Drawdown Relative",
        "Equity Drawdown Absolute",
        "Equity Drawdown Maximal",
        "Equity Drawdown Relative",
        "Total Trades",
        "Total Deals",
        "Short Trades (won %)",
        "Long Trades (won %)",
        "Profit Trades (% of total)",
        "Loss Trades (% of total)",
        "Largest profit trade",
        "Largest loss trade",
        "Average profit trade",
        "Average loss trade",
        "Maximum consecutive wins ($)",
        "Maximum consecutive losses ($)",
    ]
    seen_keys: set[str] = set()
    for k in ordered_keys:
        if k in m:
            lines.append(f"| {k} | `{m[k]}` |")
            seen_keys.add(k)
    for k, v in m.items():
        if k not in seen_keys:
            lines.append(f"| {k} | `{v}` |")
    lines.append("")

    if rep["inputs"]:
        lines.extend(
            [
                "<details>",
                f"<summary>EA Inputs ({len(rep['inputs'])} parameters)</summary>",
                "",
                "| Input | Value |",
                "|---|---|",
            ]
        )
        for k, v in rep["inputs"].items():
            lines.append(f"| `{k}` | `{v}` |")
        lines.extend(["", "</details>", ""])

    if rep["charts"]:
        lines.extend(["## Charts", ""])
        for ch in rep["charts"]:
            uri = pathlib.Path(ch["path"]).as_uri()
            lines.append(f"### {ch['filename']}")
            lines.append(f"![{ch['filename']}]({uri})")
            lines.append("")

    lines.extend(
        [
            "## Execution & Log Diagnostics",
            "",
            f"- **Source HTM**: `{rep['source_htm']}`",
            f"- **Tester Agent Log**: `{d.get('agent_log') or 'N/A'}`",
            f"- **Terminal Log**: `{d.get('terminal_log') or 'N/A'}`",
            f"- **Final Balance Log**: `{d.get('final_balance_line') or 'N/A'}`",
            "",
        ]
    )
    if d.get("error_lines"):
        lines.append("### Log Warnings / Errors")
        lines.append("```text")
        lines.extend(d["error_lines"])
        lines.append("```")
        lines.append("")

    if include_orders and rep["orders"]:
        lines.extend(["## Orders", "", "| " + " | ".join(ORDER_COLUMNS) + " |", "|" + "---|" * len(ORDER_COLUMNS)])
        for o in rep["orders"][:max_deals]:
            lines.append("| " + " | ".join(str(o.get(c, "")) for c in ORDER_COLUMNS) + " |")
        lines.append("")

    if include_deals and rep["deals"]:
        shown = rep["deals"][:max_deals]
        lines.extend(
            [
                f"## Deals (showing {len(shown)} of {rep['deals_count']})",
                "",
                "| " + " | ".join(DEAL_COLUMNS) + " |",
                "|" + "---|" * len(DEAL_COLUMNS),
            ]
        )
        for dl in shown:
            lines.append("| " + " | ".join(str(dl.get(c, "")) for c in DEAL_COLUMNS) + " |")
        lines.append("")

    return "\n".join(lines)


def export_report_json(rep: Dict[str, Any]) -> str:
    clean = dict(rep)
    clean.pop("deals_pipe_rows", None)
    return json.dumps(clean, indent=2, ensure_ascii=False)


def _compute_trade_analytics(rep: Dict[str, Any]) -> Dict[str, Any]:
    """Compute entry distributions, P/L distributions, holding times, and excursion proxies from Orders & Deals."""
    entries_hour = [0] * 24
    entries_wday = [0] * 7
    entries_month = [0] * 12

    prof_hour = [0.0] * 24
    loss_hour = [0.0] * 24
    prof_wday = [0.0] * 7
    loss_wday = [0.0] * 7
    prof_month = [0.0] * 12
    loss_month = [0.0] * 12

    orders_by_id = {str(o.get("order", "")): o for o in rep.get("orders", [])}
    open_q: List[Dict[str, Any]] = []
    positions: List[Dict[str, Any]] = []

    for dl in rep.get("deals", []):
        direction = str(dl.get("direction", "")).strip().lower()
        t_str = str(dl.get("time", "")).strip()
        if not t_str or direction not in ("in", "out", "in/out"):
            continue
        try:
            t_dt = dt.datetime.strptime(t_str, "%Y.%m.%d %H:%M:%S")
        except ValueError:
            continue

        dtype = str(dl.get("type", "")).strip().lower()
        sym = str(dl.get("symbol", "")).strip()
        price = _parse_mt5_float(str(dl.get("price", ""))) or 0.0
        vol = _parse_mt5_float(str(dl.get("volume", ""))) or 0.0
        net_pl = (
            (_parse_mt5_float(str(dl.get("profit", ""))) or 0.0)
            + (_parse_mt5_float(str(dl.get("commission", ""))) or 0.0)
            + (_parse_mt5_float(str(dl.get("swap", ""))) or 0.0)
        )

        if direction in ("in", "in/out"):
            entries_hour[t_dt.hour] += 1
            entries_wday[t_dt.weekday()] += 1
            entries_month[t_dt.month - 1] += 1

        if direction == "in":
            ord_info = orders_by_id.get(str(dl.get("order", "")), {})
            open_q.append(
                {
                    "t": t_dt,
                    "symbol": sym,
                    "type": dtype,
                    "price": price,
                    "vol": vol,
                    "sl": str(ord_info.get("sl", "")),
                    "tp": str(ord_info.get("tp", "")),
                }
            )
        elif direction in ("out", "in/out"):
            if net_pl > 0:
                prof_hour[t_dt.hour] += net_pl
                prof_wday[t_dt.weekday()] += net_pl
                prof_month[t_dt.month - 1] += net_pl
            elif net_pl < 0:
                abs_l = abs(net_pl)
                loss_hour[t_dt.hour] += abs_l
                loss_wday[t_dt.weekday()] += abs_l
                loss_month[t_dt.month - 1] += abs_l

            target_side = "sell" if "buy" in dtype else "buy"
            cmt = str(dl.get("comment", "")).strip().lower()
            cands = [
                i
                for i, x in enumerate(open_q)
                if x["symbol"] == sym and target_side in x["type"]
            ]
            idx = None
            for i in cands:
                if (open_q[i]["sl"] and open_q[i]["sl"] in cmt) or (
                    open_q[i]["tp"] and open_q[i]["tp"] in cmt
                ):
                    idx = i
                    break
            if idx is None and cands:
                idx = cands[0]

            if idx is not None:
                op = open_q.pop(idx)
                hold_sec = max(0, int((t_dt - op["t"]).total_seconds()))
                if "buy" in op["type"]:
                    move = price - op["price"]
                else:
                    move = op["price"] - price
                sl_f = _parse_mt5_float(op["sl"])
                tp_f = _parse_mt5_float(op["tp"])
                fav = max(0.0, move)
                adv = max(0.0, -move)
                # When closed via TP/SL, use planned TP/SL distance if larger than zero
                if "tp " in cmt and tp_f and tp_f > 0:
                    fav = max(fav, abs(tp_f - op["price"]))
                if "sl " in cmt and sl_f and sl_f > 0 and net_pl < 0:
                    adv = max(adv, abs(op["price"] - sl_f))
                positions.append(
                    {
                        "symbol": sym,
                        "side": op["type"],
                        "holding_sec": hold_sec,
                        "profit": net_pl,
                        "open_price": op["price"],
                        "close_price": price,
                        "favorable": fav,
                        "adverse": adv,
                    }
                )

    return {
        "entries_hour": entries_hour,
        "entries_wday": entries_wday,
        "entries_month": entries_month,
        "prof_hour": prof_hour,
        "loss_hour": loss_hour,
        "prof_wday": prof_wday,
        "loss_wday": loss_wday,
        "prof_month": prof_month,
        "loss_month": loss_month,
        "positions": positions,
    }


def _build_svg_histogram_single(
    title: str, labels: List[str], counts: List[int], color: str = "#d97706"
) -> str:
    esc = html_lib.escape
    w, h = 420, 195
    pad_l, pad_r, pad_t, pad_b = 40, 12, 16, 28
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b
    max_v = max(counts) if any(counts) else 1
    n = len(labels)
    slot = plot_w / float(max(1, n))
    bw = max(4.0, slot * 0.66)

    svg_parts: List[str] = []
    for i in range(4):
        frac = i / 3.0
        y = pad_t + frac * plot_h
        val = int(round(max_v * (1.0 - frac)))
        svg_parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w - pad_r}" y2="{y:.1f}" '
            f'stroke="var(--border)" stroke-dasharray="3 3" stroke-width="1"/>'
        )
        svg_parts.append(
            f'<text x="{pad_l - 6}" y="{y + 3:.1f}" text-anchor="end" fill="var(--muted)" font-size="9.5">{val}</text>'
        )

    label_step = 2 if n >= 20 else 1
    for idx, (lbl, cnt) in enumerate(zip(labels, counts)):
        x = pad_l + idx * slot + (slot - bw) / 2.0
        bh = (cnt / float(max_v)) * plot_h if max_v > 0 else 0.0
        y = pad_t + plot_h - bh
        if cnt > 0:
            svg_parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{max(1.5, bh):.1f}" '
                f'rx="2.5" fill="{color}" opacity="0.88"><title>{esc(lbl)}: {cnt} entries</title></rect>'
            )
        if idx % label_step == 0:
            svg_parts.append(
                f'<text x="{x + bw / 2.0:.1f}" y="{h - 8}" text-anchor="middle" fill="var(--muted)" font-size="9.5">{esc(lbl)}</text>'
            )

    return (
        f'<div class="chart-card"><div class="chart-title">{esc(title)}</div>'
        f'<div class="svg-wrap"><svg viewBox="0 0 {w} {h}" width="100%" height="auto">'
        + "".join(svg_parts)
        + "</svg></div></div>"
    )


def _build_svg_histogram_dual(
    title: str, labels: List[str], profits: List[float], losses: List[float]
) -> str:
    esc = html_lib.escape
    w, h = 420, 195
    pad_l, pad_r, pad_t, pad_b = 46, 12, 16, 28
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b
    max_v = max([*profits, *losses, 1.0])
    n = len(labels)
    slot = plot_w / float(max(1, n))
    bw = max(2.5, (slot * 0.72) / 2.0)

    svg_parts: List[str] = []
    for i in range(4):
        frac = i / 3.0
        y = pad_t + frac * plot_h
        val = max_v * (1.0 - frac)
        svg_parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w - pad_r}" y2="{y:.1f}" '
            f'stroke="var(--border)" stroke-dasharray="3 3" stroke-width="1"/>'
        )
        svg_parts.append(
            f'<text x="{pad_l - 6}" y="{y + 3:.1f}" text-anchor="end" fill="var(--muted)" font-size="9.5">{val:,.0f}</text>'
        )

    label_step = 2 if n >= 20 else 1
    for idx, (lbl, pv, lv) in enumerate(zip(labels, profits, losses)):
        gx = pad_l + idx * slot + (slot - bw * 2.0) / 2.0
        if pv > 0:
            ph = (pv / max_v) * plot_h
            svg_parts.append(
                f'<rect x="{gx:.1f}" y="{pad_t + plot_h - ph:.1f}" width="{bw:.1f}" height="{max(1.5, ph):.1f}" '
                f'rx="2" fill="#10b981" opacity="0.9"><title>{esc(lbl)} Profit: +{pv:,.2f}</title></rect>'
            )
        if lv > 0:
            lh = (lv / max_v) * plot_h
            svg_parts.append(
                f'<rect x="{gx + bw:.1f}" y="{pad_t + plot_h - lh:.1f}" width="{bw:.1f}" height="{max(1.5, lh):.1f}" '
                f'rx="2" fill="#ef4444" opacity="0.9"><title>{esc(lbl)} Loss: -{lv:,.2f}</title></rect>'
            )
        if idx % label_step == 0:
            svg_parts.append(
                f'<text x="{gx + bw:.1f}" y="{h - 8}" text-anchor="middle" fill="var(--muted)" font-size="9.5">{esc(lbl)}</text>'
            )

    return (
        f'<div class="chart-card"><div class="chart-title">{esc(title)} '
        f'<span class="good" style="float:right">Profit</span><span class="bad" style="float:right;margin-right:8px">Loss</span></div>'
        f'<div class="svg-wrap"><svg viewBox="0 0 {w} {h}" width="100%" height="auto">'
        + "".join(svg_parts)
        + "</svg></div></div>"
    )


def _build_svg_holding_scatter(
    positions: List[Dict[str, Any]], min_ht: str, max_ht: str, avg_ht: str
) -> str:
    esc = html_lib.escape
    if not positions:
        return "<div class='chart-card'><div class='chart-title'>Position Holding Time vs Profit</div><p class='muted'>No matched closed trades to plot.</p></div>"

    w, h = 620, 250
    pad_l, pad_r, pad_t, pad_b = 58, 20, 18, 32
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b

    max_sec = max(max(p["holding_sec"] for p in positions), 60)
    profits = [p["profit"] for p in positions]
    min_y = min(min(profits), 0.0)
    max_y = max(max(profits), 0.0)
    if abs(max_y - min_y) < 1e-9:
        min_y -= 1.0
        max_y += 1.0

    use_hours = max_sec >= 7200
    x_unit = "h" if use_hours else "m"
    x_div = 3600.0 if use_hours else 60.0

    parts: List[str] = []
    for i in range(5):
        frac = i / 4.0
        y = pad_t + frac * plot_h
        yv = max_y - frac * (max_y - min_y)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w - pad_r}" y2="{y:.1f}" stroke="var(--border)" stroke-dasharray="3 3" stroke-width="1"/>'
            f'<text x="{pad_l - 8}" y="{y + 3.5:.1f}" text-anchor="end" fill="var(--muted)" font-size="10">{yv:,.1f}</text>'
        )
    # Zero baseline
    if min_y < 0 < max_y:
        y0 = pad_t + (1.0 - (0.0 - min_y) / (max_y - min_y)) * plot_h
        parts.append(f'<line x1="{pad_l}" y1="{y0:.1f}" x2="{w - pad_r}" y2="{y0:.1f}" stroke="var(--muted)" stroke-width="1" opacity="0.6"/>')

    for i in range(5):
        frac = i / 4.0
        x = pad_l + frac * plot_w
        xv = (frac * max_sec) / x_div
        parts.append(
            f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{pad_t + plot_h}" stroke="var(--border)" stroke-dasharray="3 3" stroke-width="1"/>'
            f'<text x="{x:.1f}" y="{h - 10}" text-anchor="middle" fill="var(--muted)" font-size="10">{xv:.1f}{x_unit}</text>'
        )

    for p in positions:
        x = pad_l + (p["holding_sec"] / float(max_sec)) * plot_w
        y = pad_t + (1.0 - (p["profit"] - min_y) / (max_y - min_y)) * plot_h
        clr = "#10b981" if p["profit"] >= 0 else "#ef4444"
        hs = p["holding_sec"]
        dur_str = f"{hs // 3600}:{(hs % 3600) // 60:02d}:{hs % 60:02d}"
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.8" fill="{clr}" opacity="0.82">'
            f'<title>{esc(p["side"].upper())} | Hold {dur_str} | Profit {p["profit"]:+,.2f}</title></circle>'
        )

    return (
        f'<div class="chart-card"><div class="chart-title">Position Holding Time vs Profit (SVG)'
        f'<span class="muted" style="float:right">Min {esc(min_ht)} &bull; Avg {esc(avg_ht)} &bull; Max {esc(max_ht)}</span></div>'
        f'<div class="svg-wrap"><svg viewBox="0 0 {w} {h}" width="100%" height="auto">{"".join(parts)}</svg></div></div>'
    )


def _linear_regression(xs: List[float], ys: List[float]) -> Optional[Tuple[float, float]]:
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den < 1e-12:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    intercept = my - slope * mx
    return slope, intercept


def _build_svg_excursion_scatter(
    positions: List[Dict[str, Any]], corr_mfe: str, corr_mae: str, corr_mm: str
) -> str:
    esc = html_lib.escape
    if not positions:
        return "<div class='chart-card'><div class='chart-title'>Favorable / Adverse Excursion vs Profit</div><p class='muted'>No matched closed trades to plot.</p></div>"

    w, h = 620, 250
    half_h = 112
    pad_l, pad_r = 58, 20

    def _scatter_sub(
        pts: List[Tuple[float, float]], y_offset: int, title_lbl: str, color: str
    ) -> str:
        sub_h = half_h - 22
        top = y_offset + 14
        xs = [a for a, _ in pts]
        ys = [b for _, b in pts]
        max_x = max(max(xs), 1e-6)
        min_y = min(min(ys), 0.0)
        max_y = max(max(ys), 0.0)
        if abs(max_y - min_y) < 1e-9:
            min_y -= 1.0
            max_y += 1.0
        plot_w = w - pad_l - pad_r
        out: List[str] = [
            f'<text x="{pad_l + 4}" y="{top + 10}" fill="var(--muted)" font-size="10" font-weight="600">{esc(title_lbl)}</text>',
            f'<line x1="{pad_l}" y1="{top}" x2="{w - pad_r}" y2="{top}" stroke="var(--border)" stroke-dasharray="3 3" stroke-width="1"/>',
            f'<line x1="{pad_l}" y1="{top + sub_h}" x2="{w - pad_r}" y2="{top + sub_h}" stroke="var(--border)" stroke-width="1"/>',
            f'<text x="{pad_l - 8}" y="{top + 4}" text-anchor="end" fill="var(--muted)" font-size="9.5">{max_y:,.1f}</text>',
            f'<text x="{pad_l - 8}" y="{top + sub_h}" text-anchor="end" fill="var(--muted)" font-size="9.5">{min_y:,.1f}</text>',
        ]
        if min_y < 0 < max_y:
            y0 = top + (1.0 - (0.0 - min_y) / (max_y - min_y)) * sub_h
            out.append(f'<line x1="{pad_l}" y1="{y0:.1f}" x2="{w - pad_r}" y2="{y0:.1f}" stroke="var(--muted)" stroke-width="1" opacity="0.45"/>')

        reg = _linear_regression(xs, ys)
        if reg:
            slope, inter = reg
            y_s = max(min_y, min(max_y, inter))
            y_e = max(min_y, min(max_y, slope * max_x + inter))
            py_s = top + (1.0 - (y_s - min_y) / (max_y - min_y)) * sub_h
            py_e = top + (1.0 - (y_e - min_y) / (max_y - min_y)) * sub_h
            out.append(f'<line x1="{pad_l}" y1="{py_s:.1f}" x2="{w - pad_r}" y2="{py_e:.1f}" stroke="{color}" stroke-width="1.8" opacity="0.85"/>')

        for xv, yv in pts:
            px = pad_l + (xv / max_x) * plot_w
            py = top + (1.0 - (yv - min_y) / (max_y - min_y)) * sub_h
            out.append(
                f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3.2" fill="{color}" opacity="0.82">'
                f'<title>{esc(title_lbl)} | Excursion {xv:.2f} | Profit {yv:+,.2f}</title></circle>'
            )
        return "".join(out)

    fav_pts = [(p["favorable"], p["profit"]) for p in positions]
    adv_pts = [(p["adverse"], p["profit"]) for p in positions]
    svg_body = _scatter_sub(fav_pts, 4, "Profits vs Favorable Price Move (Order/Deal Proxy)", "#0284c7") + _scatter_sub(
        adv_pts, 126, "Profits vs Adverse Price Move (Order/Deal Proxy)", "#ef4444"
    )

    return (
        f'<div class="chart-card"><div class="chart-title">Excursion vs Profit &amp; Regression (SVG)'
        f'<span class="muted" style="float:right">MT5 Corr MFE: {esc(corr_mfe)} &bull; MAE: {esc(corr_mae)} &bull; MFE/MAE: {esc(corr_mm)}</span></div>'
        f'<div class="svg-wrap"><svg viewBox="0 0 {w} {h}" width="100%" height="auto">{svg_body}</svg></div></div>'
    )


def export_report_html(rep: Dict[str, Any]) -> str:
    esc = html_lib.escape
    s = rep["settings"]
    m = rep["metrics_raw"]
    mn = rep.get("metrics_numeric", {})
    d = rep["diagnostics"]

    verdict = rep["verdict"]
    badge_bg = {
        "PASSED": "#059669",
        "STOP OUT": "#dc2626",
        "NO TRADES": "#d97706",
        "CHECK LOGS": "#0284c7",
    }.get(verdict, "#475569")

    net_profit = mn.get("Total Net Profit", {}).get("value")
    pf_val = mn.get("Profit Factor", {}).get("value")
    eq_dd_pct = mn.get("Equity Drawdown Maximal", {}).get("pct")
    win_pct = mn.get("Profit Trades (% of total)", {}).get("pct")

    np_cls = "good" if isinstance(net_profit, (int, float)) and net_profit > 0 else ("bad" if isinstance(net_profit, (int, float)) and net_profit < 0 else "")
    pf_cls = "good" if isinstance(pf_val, (int, float)) and pf_val >= 1.5 else ("warn" if isinstance(pf_val, (int, float)) and pf_val >= 1.0 else ("bad" if isinstance(pf_val, (int, float)) else ""))
    dd_cls = "good" if isinstance(eq_dd_pct, (int, float)) and eq_dd_pct < 15.0 else ("warn" if isinstance(eq_dd_pct, (int, float)) and eq_dd_pct < 30.0 else ("bad" if isinstance(eq_dd_pct, (int, float)) else ""))

    win_bar_html = ""
    if isinstance(win_pct, (int, float)):
        clamped = max(0.0, min(100.0, float(win_pct)))
        win_bar_html = f'<div class="winbar-wrap" title="Win {clamped:.2f}% / Loss {100-clamped:.2f}%"><div class="winbar-fill" style="width:{clamped:.1f}%"></div></div>'

    kpi_items = [
        ("Total Net Profit", m.get("Total Net Profit", "0.00"), np_cls, f"Gross: +{m.get('Gross Profit', '0')} / {m.get('Gross Loss', '0')}"),
        ("Profit Factor", m.get("Profit Factor", "0.00"), pf_cls, f"Recovery: {m.get('Recovery Factor', '0.00')}"),
        ("Max Equity DD", m.get("Equity Drawdown Maximal", "0.00"), dd_cls, f"Relative: {m.get('Equity Drawdown Relative', '0.00')}"),
        ("Max Balance DD", m.get("Balance Drawdown Maximal", "0.00"), "", f"Absolute: {m.get('Balance Drawdown Absolute', '0.00')}"),
        ("Total Trades", m.get("Total Trades", "0"), "", f"Win: {m.get('Profit Trades (% of total)', '0')}{win_bar_html}"),
        ("Sharpe Ratio", m.get("Sharpe Ratio", "0.00"), "", f"LR Corr: {m.get('LR Correlation', '0.00')}"),
        ("Expected Payoff", m.get("Expected Payoff", "0.00"), "", f"Avg Win: {m.get('Average profit trade', '0.00')}"),
        ("Test Duration", d.get("test_passed_in") or "?", "", f"Bars: {m.get('Bars', '?')} | Ticks: {m.get('Ticks', '?')}"),
    ]

    kpi_cards = "\n".join(
        f'<div class="kpi"><div class="kpi-label">{esc(lbl)}</div>'
        f'<div class="kpi-val {cls}">{esc(str(val))}</div>'
        f'<div class="kpi-sub">{sub}</div></div>'
        for lbl, val, cls, sub in kpi_items
    )

    metrics_rows = "\n".join(
        f"<tr><td>{esc(k)}</td><td class='num'><b>{esc(str(v))}</b></td></tr>"
        for k, v in m.items()
    )

    input_rows_list: List[str] = []
    inputs_meta = rep.get("inputs_meta") or []
    if inputs_meta:
        last_g = None
        for item in inputs_meta:
            grp = item.get("group") or ""
            if grp and grp != last_g:
                input_rows_list.append(f'<tr class="group-row"><td colspan="2">{esc(grp)}</td></tr>')
                last_g = grp
            input_rows_list.append(
                f"<tr><td><code>{esc(item['name'])}</code></td><td class='num'><code>{esc(str(item['value']))}</code></td></tr>"
            )
    else:
        for k, v in rep["inputs"].items():
            input_rows_list.append(
                f"<tr><td><code>{esc(k)}</code></td><td class='num'><code>{esc(str(v))}</code></td></tr>"
            )
    input_rows = "\n".join(input_rows_list)

    balances = _extract_balance_series(rep.get("deals", []))
    balance_svg = _build_svg_balance_curve([("Balance Curve", "#0284c7", balances)])

    analytics = _compute_trade_analytics(rep)
    hour_labels = [f"{h:02d}" for h in range(24)]
    wday_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    hist_grid_html = (
        '<div class="charts-grid-3" style="margin-top:18px">'
        + _build_svg_histogram_single("Entries by Hours", hour_labels, analytics["entries_hour"], "#d97706")
        + _build_svg_histogram_single("Entries by Weekdays", wday_labels, analytics["entries_wday"], "#10b981")
        + _build_svg_histogram_single("Entries by Months", month_labels, analytics["entries_month"], "#0284c7")
        + _build_svg_histogram_dual("Profits & Losses by Hours", hour_labels, analytics["prof_hour"], analytics["loss_hour"])
        + _build_svg_histogram_dual("Profits & Losses by Weekdays", wday_labels, analytics["prof_wday"], analytics["loss_wday"])
        + _build_svg_histogram_dual("Profits & Losses by Months", month_labels, analytics["prof_month"], analytics["loss_month"])
        + "</div>"
    )

    scatter_grid_html = (
        '<div class="grid2" style="margin-top:16px">'
        + _build_svg_holding_scatter(
            analytics["positions"],
            m.get("Minimal position holding time", "?"),
            m.get("Maximal position holding time", "?"),
            m.get("Average position holding time", "?"),
        )
        + _build_svg_excursion_scatter(
            analytics["positions"],
            m.get("Correlation (Profits,MFE)", "?"),
            m.get("Correlation (Profits,MAE)", "?"),
            m.get("Correlation (MFE,MAE)", "?"),
        )
        + "</div>"
    )

    chart_blocks: List[str] = []
    for ch in rep["charts"]:
        src = ch.get("data_uri") or pathlib.Path(ch["path"]).as_uri()
        chart_blocks.append(
            f'<div class="chart-card"><div class="chart-title">{esc(ch["filename"])}</div>'
            f'<img src="{esc(src)}" alt="{esc(ch["filename"])}" /></div>'
        )
    legacy_png_html = (
        f'<details style="margin-top:16px"><summary class="btn" style="display:inline-block">View Original MT5 Raster Charts (.png)</summary>'
        f'<div class="charts-grid" style="margin-top:12px">{"".join(chart_blocks)}</div></details>'
        if chart_blocks
        else ""
    )

    deal_headers = "".join(
        f'<th class="{"num" if c in ("deal","volume","price","order","commission","swap","profit","balance") else ""}">{esc(c)}</th>'
        for c in DEAL_COLUMNS
    )
    deal_rows_list: List[str] = []
    for dl in rep["deals"]:
        dtype = str(dl.get("type", "")).lower()
        p_num = _parse_mt5_float(str(dl.get("profit", "")))
        p_cls = "good" if isinstance(p_num, (int, float)) and p_num > 0 else ("bad" if isinstance(p_num, (int, float)) and p_num < 0 else "")
        tds: List[str] = []
        for col in DEAL_COLUMNS:
            val_s = str(dl.get(col, ""))
            if col in ("deal", "volume", "price", "order", "commission", "swap", "balance"):
                tds.append(f'<td class="num">{esc(val_s)}</td>')
            elif col == "profit":
                tds.append(f'<td class="num {p_cls}"><b>{esc(val_s)}</b></td>')
            elif col == "type":
                pill_c = "good" if "buy" in dtype else ("bad" if "sell" in dtype else "")
                tds.append(f'<td><span class="pill {pill_c}">{esc(val_s)}</span></td>')
            else:
                tds.append(f"<td>{esc(val_s)}</td>")
        is_win = "1" if isinstance(p_num, (int, float)) and p_num > 0 else ("-1" if isinstance(p_num, (int, float)) and p_num < 0 else "0")
        deal_rows_list.append(f'<tr data-type="{esc(dtype)}" data-win="{is_win}">' + "".join(tds) + "</tr>")

    err_html = ""
    if d.get("error_lines"):
        err_html = "<pre class='log-box'>" + esc("\n".join(d["error_lines"])) + "</pre>"

    return (
        _shared_html_head(f"MT5 Report — {rep['report_name']}")
        + f"""
<body>
<div class="wrap">
  <div class="header">
    <div>
      <h1 class="title">Strategy Tester Report: {esc(rep['report_name'])}</h1>
      <div class="subtitle">
        <span>Expert: <b>{esc(s.get('Expert','?'))}</b></span>
        <span>Symbol: <b>{esc(s.get('Symbol','?'))}</b></span>
        <span>Period: <b>{esc(s.get('Period','?'))}</b></span>
        <span>Deposit: <b>{esc(s.get('Initial Deposit','?'))} {esc(s.get('Currency',''))}</b></span>
        <span>Leverage: <b>{esc(s.get('Leverage','?'))}</b></span>
      </div>
      <div class="subtitle">
        <span class="pill">Deals: {rep['deals_count']}</span>
        <span class="pill">SHA-256: <code>{esc(rep['deals_sha256'])}</code></span>
        <span class="pill">No-Time: <code>{esc(rep['deals_no_time_sha256'])}</code></span>
      </div>
    </div>
    <div class="header-right">
      <span class="badge" style="background:{badge_bg}">{esc(verdict)}</span>
      <button id="themeBtn" class="btn" onclick="toggleTheme()">Dark Mode</button>
    </div>
  </div>

  <div class="kpis">{kpi_cards}</div>

  <div class="panel">
    <div class="panel-head">
      <h2>Native Vector Analytics (SVG)</h2>
      <span class="pill accent">Theme-aware &bull; Interactive tooltips</span>
    </div>
    {balance_svg}
    {hist_grid_html}
    {scatter_grid_html}
    {legacy_png_html}
  </div>

  <div class="grid2">
    <div class="panel">
      <div class="panel-head">
        <h2>Strategy Tester Metrics</h2>
        <input class="search-input" style="max-width:210px" id="metricFilter" placeholder="Filter metrics..." oninput="filterRows('metricFilter','metricsTable')" />
      </div>
      <div class="scroll-box">
        <table id="metricsTable"><tbody>{metrics_rows}</tbody></table>
      </div>
    </div>
    <div class="panel">
      <div class="panel-head">
        <h2>EA Inputs ({len(rep['inputs'])})</h2>
        <input class="search-input" style="max-width:210px" id="inputFilter" placeholder="Filter inputs..." oninput="filterRows('inputFilter','inputsTable')" />
      </div>
      <div class="scroll-box">
        <table id="inputsTable"><thead><tr><th>Input</th><th class="num">Value</th></tr></thead><tbody>{input_rows}</tbody></table>
      </div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-head"><h2>Execution Diagnostics &amp; Logs</h2></div>
    <p class="muted">
      Source HTM: <code>{esc(rep['source_htm'])}</code><br/>
      Agent Log: <code>{esc(str(d.get('agent_log') or 'N/A'))}</code><br/>
      Terminal Log: <code>{esc(str(d.get('terminal_log') or 'N/A'))}</code><br/>
      Final Balance: <code>{esc(str(d.get('final_balance_line') or 'N/A'))}</code>
    </p>
    {err_html}
  </div>

  <div class="panel">
    <div class="panel-head">
      <h2>Deals Ledger ({rep['deals_count']} rows)</h2>
      <span id="dealCount" class="muted">{rep['deals_count']} visible</span>
    </div>
    <div class="toolbar">
      <input class="search-input" type="text" id="dealFilter" placeholder="Search symbol, comment, ticket, profit..." oninput="filterDeals()" />
      <button class="btn active" onclick="setDealMode('all', this)">All</button>
      <button class="btn" onclick="setDealMode('buy', this)">Buy</button>
      <button class="btn" onclick="setDealMode('sell', this)">Sell</button>
      <button class="btn" onclick="setDealMode('win', this)">Winners</button>
      <button class="btn" onclick="setDealMode('loss', this)">Losers</button>
    </div>
    <div class="scroll-box">
      <table id="dealsTable">
        <thead><tr>{deal_headers}</tr></thead>
        <tbody>{''.join(deal_rows_list)}</tbody>
      </table>
    </div>
  </div>
</div>
<script>
let dealMode = 'all';
function setDealMode(mode, btn) {{
  dealMode = mode;
  btn.parentElement.querySelectorAll('.btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  filterDeals();
}}
function filterDeals() {{
  const q = document.getElementById('dealFilter').value.toLowerCase();
  const rows = document.querySelectorAll('#dealsTable tbody tr');
  let vis = 0;
  for (const r of rows) {{
    const t = r.getAttribute('data-type') || '';
    const w = r.getAttribute('data-win') || '0';
    let okMode = true;
    if (dealMode === 'buy') okMode = t.includes('buy');
    else if (dealMode === 'sell') okMode = t.includes('sell');
    else if (dealMode === 'win') okMode = w === '1';
    else if (dealMode === 'loss') okMode = w === '-1';
    const show = okMode && r.textContent.toLowerCase().includes(q);
    r.style.display = show ? '' : 'none';
    if (show) vis++;
  }}
  document.getElementById('dealCount').textContent = vis + ' visible';
}}
function filterRows(inputId, tableId) {{
  const q = document.getElementById(inputId).value.toLowerCase();
  for (const r of document.querySelectorAll('#' + tableId + ' tbody tr')) {{
    r.style.display = r.textContent.toLowerCase().includes(q) ? '' : 'none';
  }}
}}
</script>
"""
        + _shared_theme_script()
        + "</body>\n</html>\n"
    )


def _infer_format(fmt_arg: Optional[str], out_path: Optional[str], default: str = "text") -> str:
    if fmt_arg:
        return fmt_arg
    if out_path:
        ext = pathlib.Path(out_path).suffix.lower()
        if ext in (".md", ".markdown"):
            return "md"
        if ext == ".json":
            return "json"
        if ext in (".html", ".htm"):
            return "html"
    return default


def cmd_report(args: argparse.Namespace) -> int:
    paths = resolve_env_paths(args)
    is_xml = bool(getattr(args, "xml", False) or str(args.report).lower().endswith(".xml"))
    if not is_xml:
        try:
            resolve_report_htm_path(args.report, paths)
        except FileNotFoundError:
            try:
                resolve_report_xml_path(args.report, paths)
                is_xml = True
            except FileNotFoundError:
                pass

    if is_xml:
        opt_data = parse_optimization_xml(args.report, paths)
        sort_by = getattr(args, "sort_by", "result")
        top_n = getattr(args, "top", 20)
        ranked = filter_and_sort_opt_passes(
            opt_data,
            sort_by=sort_by,
            min_trades=getattr(args, "min_trades", None),
            min_pf=getattr(args, "min_pf", None),
            max_dd_pct=getattr(args, "max_dd_pct", None),
            require_positive=getattr(args, "require_positive", False),
        )

        out_set = getattr(args, "export_best_set", None)
        save_tester = getattr(args, "save_best_to_tester", None)
        if save_tester:
            name = save_tester if save_tester.lower().endswith(".set") else f"{save_tester}.set"
            out_set = os.path.join(paths["profiles_tester_dir"], name)

        if out_set and (ranked or opt_data["passes"]):
            has_base = bool(
                getattr(args, "profile", None)
                or getattr(args, "set_file", None)
                or getattr(args, "from_source", None)
                or getattr(args, "from_report", None)
            )
            pool = ranked or opt_data["passes"]
            pick_pass = getattr(args, "pick_pass", None)
            picked = (
                next((p for p in pool if p.get("pass") == pick_pass), None)
                if pick_pass is not None
                else pool[0]
            )
            if not picked:
                raise ValueError(f"Pass #{pick_pass} not found in optimization XML.")

            if not has_base and not getattr(args, "allow_partial_set", False):
                candidate_ini = os.path.join(paths["work_dir"], f"{opt_data['report_name']}.ini")
                hint = (
                    f' (e.g. --profile "{candidate_ini}")'
                    if os.path.isfile(candidate_ini)
                    else " (e.g. --profile <Profile.ini> --set-file <Base.set>)"
                )
                raise ValueError(
                    f"Refusing to write partial .set with only {len(picked.get('inputs', {}))} optimized input(s) "
                    f"({', '.join(picked.get('inputs', {}).keys())}) from {opt_data['report_name']}.xml — "
                    f"omitted inputs would silently reset to EA defaults in Strategy Tester. "
                    f"Pass --profile, --set-file, --from-report, or --from-source to merge the winning pass into a complete baseline{hint}, "
                    f"or pass --allow-partial-set to export only the optimized columns."
                )

            base_inputs = None
            if has_base:
                base_inputs, _, _ = _build_base_inputs(
                    paths,
                    getattr(args, "profile", None),
                    getattr(args, "set_file", None),
                    getattr(args, "from_source", None),
                    getattr(args, "source", None),
                    getattr(args, "from_report", None),
                )
            cnt = _export_pass_to_set(picked, out_set, base_inputs, opt_data["report_name"])
            print(f"Best .set: {os.path.abspath(out_set)} (pass #{picked.get('pass','?')}, {cnt} inputs, UTF-16LE BOM)")

        fmt = _infer_format(args.format, args.out, default="text")
        if fmt == "md":
            content = export_opt_md(opt_data, ranked, top_n, sort_by)
        elif fmt == "json":
            content = export_opt_json(opt_data, ranked, top_n, sort_by)
        elif fmt == "html":
            content = export_opt_html(opt_data, ranked, top_n, sort_by)
        else:
            content = export_opt_text(opt_data, ranked, top_n, sort_by)
        return _emit_text_or_file(content, args.out)

    rep = parse_htm_report(args.report, paths, embed_images=args.embed_images)

    if args.deals:
        write_utf8(args.deals, "\n".join(rep["deals_pipe_rows"]))

    fmt = _infer_format(args.format, args.out, default="text")
    if fmt == "md":
        content = export_report_md(
            rep,
            include_deals=args.include_deals,
            max_deals=args.max_deals,
            include_orders=args.include_orders,
        )
    elif fmt == "json":
        content = export_report_json(rep)
    elif fmt == "html":
        content = export_report_html(rep)
    else:
        content = export_report_text(rep)

    return _emit_text_or_file(content, args.out)


# ---------------------------------------------------------------------------
# Subcommand: compare
# ---------------------------------------------------------------------------


def _load_comparable(spec: str, paths: Dict[str, str]) -> Dict[str, Any]:
    """Load a report (.htm / bare name / .json) or deals txt file for comparison."""
    if os.path.isfile(spec):
        low = spec.lower()
        if low.endswith(".json"):
            data = json.loads(read_text_auto(spec))
            pipe_rows = [
                "|".join(str(dl.get(c, "")) for c in DEAL_COLUMNS)
                for dl in data.get("deals", [])
            ]
            data["deals_pipe_rows"] = pipe_rows
            data["is_full_report"] = True
            return data
        if low.endswith((".htm", ".html")):
            rep = parse_htm_report(spec, paths)
            rep["is_full_report"] = True
            return rep
        # Pipe-delimited deals txt
        rows = [ln.strip() for ln in read_text_auto(spec).splitlines() if ln.strip()]
        return {
            "report_name": pathlib.Path(spec).stem,
            "source_htm": os.path.abspath(spec),
            "is_full_report": False,
            "deals_pipe_rows": rows,
            "deals_count": len(rows),
            "settings": {},
            "inputs": {},
            "metrics_raw": {},
            "metrics_numeric": {},
            "diagnostics": {},
        }

    # Try bare report name in data_dir
    rep = parse_htm_report(spec, paths)
    rep["is_full_report"] = True
    return rep


def _compare_metrics(a: Dict[str, Any], b: Dict[str, Any]) -> List[Dict[str, Any]]:
    keys = [
        "Total Net Profit",
        "Profit Factor",
        "Total Trades",
        "Total Deals",
        "Balance Drawdown Maximal",
        "Equity Drawdown Maximal",
        "Sharpe Ratio",
        "Recovery Factor",
        "Expected Payoff",
        "Profit Trades (% of total)",
    ]
    rows: List[Dict[str, Any]] = []
    for k in keys:
        raw_a = a["metrics_raw"].get(k, "?")
        raw_b = b["metrics_raw"].get(k, "?")
        num_a = a["metrics_numeric"].get(k, {}).get("value")
        num_b = b["metrics_numeric"].get(k, {}).get("value")
        delta = None
        delta_pct = None
        if num_a is not None and num_b is not None:
            delta = round(num_b - num_a, 4)
            if num_a != 0:
                delta_pct = round(((num_b - num_a) / abs(num_a)) * 100.0, 2)
        rows.append(
            {
                "metric": k,
                "base": raw_a,
                "target": raw_b,
                "delta": delta,
                "delta_pct": delta_pct,
            }
        )
    return rows


def _compare_inputs(a: Dict[str, Any], b: Dict[str, Any]) -> List[Dict[str, str]]:
    ia = a.get("inputs", {})
    ib = b.get("inputs", {})
    all_k = list(dict.fromkeys(list(ia.keys()) + list(ib.keys())))
    diffs: List[Dict[str, str]] = []
    for k in all_k:
        va = ia.get(k, "<missing>")
        vb = ib.get(k, "<missing>")
        if va != vb:
            diffs.append({"name": k, "base": va, "target": vb})
    return diffs


def cmd_compare(args: argparse.Namespace) -> int:
    paths = resolve_env_paths(args)
    a = _load_comparable(args.base, paths)
    b = _load_comparable(args.target, paths)

    rows_a = [r.split("|", 1)[1] if "|" in r else r for r in a["deals_pipe_rows"]]
    rows_b = [r.split("|", 1)[1] if "|" in r else r for r in b["deals_pipe_rows"]]

    identical = rows_a == rows_b
    first_diff = None
    sample_diffs: List[Dict[str, Any]] = []

    if not identical:
        limit = max(len(rows_a), len(rows_b))
        for idx in range(limit):
            ra = rows_a[idx] if idx < len(rows_a) else "<EOF>"
            rb = rows_b[idx] if idx < len(rows_b) else "<EOF>"
            if ra != rb:
                if first_diff is None:
                    first_diff = idx + 1
                if len(sample_diffs) < args.max_diffs:
                    sample_diffs.append({"row": idx + 1, "base": ra, "target": rb})

    timestamp_only_diffs = 0
    if identical:
        for ra_full, rb_full in zip(a["deals_pipe_rows"], b["deals_pipe_rows"]):
            if ra_full != rb_full:
                timestamp_only_diffs += 1

    both_full = bool(a.get("is_full_report") and b.get("is_full_report"))
    metric_deltas = _compare_metrics(a, b) if both_full else []
    input_diffs = _compare_inputs(a, b) if both_full else []

    cmp_data = {
        "verdict": "IDENTICAL" if identical else "DIFFER",
        "base_name": a["report_name"],
        "target_name": b["report_name"],
        "base_rows": len(rows_a),
        "target_rows": len(rows_b),
        "first_diff_row": first_diff,
        "timestamp_only_diffs": timestamp_only_diffs,
        "sample_diffs": sample_diffs,
        "metric_deltas": metric_deltas,
        "input_diffs": input_diffs,
        "base_settings": a.get("settings", {}),
        "target_settings": b.get("settings", {}),
        "base_balances": _extract_balance_series(a.get("deals", [])) if both_full else [],
        "target_balances": _extract_balance_series(b.get("deals", [])) if both_full else [],
    }

    fmt = _infer_format(args.format, args.out, default="text")
    if fmt == "json":
        text = json.dumps(cmp_data, indent=2, ensure_ascii=False)
    elif fmt == "md":
        text = _export_compare_md(cmp_data)
    elif fmt == "html":
        text = _export_compare_html(cmp_data)
    else:
        text = _export_compare_text(cmp_data)

    _emit_text_or_file(text, args.out)
    if identical or args.no_fail_exit:
        return 0
    return 1


def _export_compare_text(c: Dict[str, Any]) -> str:
    lines: List[str] = []
    if c["verdict"] == "IDENTICAL":
        lines.append(f"IDENTICAL ({c['base_rows']} rows, time column ignored)")
        if c["timestamp_only_diffs"]:
            lines.append(
                f"Note: {c['timestamp_only_diffs']} row(s) differed only in timestamp (e.g., end-of-test closes)."
            )
    else:
        lines.append(
            f"DIFFER: {c['base_rows']} vs {c['target_rows']} rows, first difference at row {c['first_diff_row']}"
        )
        for d in c["sample_diffs"][:5]:
            lines.append(f"  Row {d['row']}:")
            lines.append(f"    < {d['base']}")
            lines.append(f"    > {d['target']}")

    if c["metric_deltas"]:
        lines.append("\nMetric Deltas:")
        for m in c["metric_deltas"]:
            delta_s = (
                f"  ({m['delta']:+g}, {m['delta_pct']:+.2f}%)"
                if m["delta"] is not None and m["delta_pct"] is not None
                else (f"  ({m['delta']:+g})" if m["delta"] is not None else "")
            )
            lines.append(f"  {m['metric']:26}: {m['base']}  ->  {m['target']}{delta_s}")

    if c["input_diffs"]:
        lines.append(f"\nInput Differences ({len(c['input_diffs'])}):")
        for inp in c["input_diffs"]:
            lines.append(f"  {inp['name']}: {inp['base']} -> {inp['target']}")

    return "\n".join(lines)


def _export_compare_md(c: Dict[str, Any]) -> str:
    lines = [
        f"# Comparison: `{c['base_name']}` vs `{c['target_name']}`",
        "",
        f"- **Deals Verdict**: **{c['verdict']}** (`{c['base_rows']}` vs `{c['target_rows']}` rows, time column ignored)",
    ]
    if c["verdict"] == "IDENTICAL" and c["timestamp_only_diffs"]:
        lines.append(
            f"- **Timestamp-only differences**: {c['timestamp_only_diffs']} row(s) (typical for forced `end of test` closes)"
        )
    if c["first_diff_row"] is not None:
        lines.append(f"- **First Deal Difference**: Row `{c['first_diff_row']}`")
    lines.append("")

    if c["metric_deltas"]:
        lines.extend(
            [
                "## Metrics Comparison",
                "",
                "| Metric | Base | Target | Delta | Delta % |",
                "|---|---|---|---|---|",
            ]
        )
        for m in c["metric_deltas"]:
            d_s = f"`{m['delta']:+g}`" if m["delta"] is not None else "-"
            dp_s = f"`{m['delta_pct']:+.2f}%`" if m["delta_pct"] is not None else "-"
            lines.append(
                f"| {m['metric']} | `{m['base']}` | `{m['target']}` | {d_s} | {dp_s} |"
            )
        lines.append("")

    if c["input_diffs"]:
        lines.extend(
            [
                f"## Input Differences ({len(c['input_diffs'])})",
                "",
                "| Input | Base | Target |",
                "|---|---|---|",
            ]
        )
        for inp in c["input_diffs"]:
            lines.append(f"| `{inp['name']}` | `{inp['base']}` | `{inp['target']}` |")
        lines.append("")

    if c["sample_diffs"]:
        lines.extend(
            [
                f"## First {len(c['sample_diffs'])} Differing Deal Rows",
                "",
                "| Row | Side | Deal (without timestamp) |",
                "|---:|---|---|",
            ]
        )
        for d in c["sample_diffs"]:
            lines.append(f"| {d['row']} | Base (`<`) | `{d['base']}` |")
            lines.append(f"| {d['row']} | Target (`>`) | `{d['target']}` |")
        lines.append("")

    return "\n".join(lines)


def _export_compare_html(c: Dict[str, Any]) -> str:
    esc = html_lib.escape
    badge_color = "#059669" if c["verdict"] == "IDENTICAL" else "#dc2626"
    lower_is_better = {"Balance Drawdown Maximal", "Equity Drawdown Maximal"}

    metrics_rows: List[str] = []
    for m in c["metric_deltas"]:
        d_val = m["delta"]
        dp_val = m["delta_pct"]
        pill_cls = ""
        if isinstance(d_val, (int, float)) and d_val != 0:
            is_good = (d_val < 0) if m["metric"] in lower_is_better else (d_val > 0)
            pill_cls = "good" if is_good else "bad"
        d_txt = f"{d_val:+g}" if d_val is not None else "-"
        dp_txt = f"{dp_val:+.2f}%" if dp_val is not None else "-"
        delta_badge = f'<span class="pill {pill_cls}">{esc(d_txt)} ({esc(dp_txt)})</span>' if d_val is not None else "-"
        metrics_rows.append(
            f"<tr><td><b>{esc(m['metric'])}</b></td>"
            f"<td class='num'>{esc(str(m['base']))}</td>"
            f"<td class='num'>{esc(str(m['target']))}</td>"
            f"<td class='num'>{delta_badge}</td></tr>"
        )
    metrics_html = "\n".join(metrics_rows)

    inputs_html = "\n".join(
        f"<tr><td><code>{esc(i['name'])}</code></td><td><code>{esc(i['base'])}</code></td><td><code>{esc(i['target'])}</code></td></tr>"
        for i in c["input_diffs"]
    )

    deal_no_time_cols = DEAL_COLUMNS[1:]
    diffs_rows: List[str] = []
    for d in c["sample_diffs"]:
        b_parts = d["base"].split("|")
        t_parts = d["target"].split("|")
        max_len = max(len(b_parts), len(t_parts), len(deal_no_time_cols))
        b_cells = []
        t_cells = []
        for idx in range(len(deal_no_time_cols)):
            bv = b_parts[idx] if idx < len(b_parts) else ""
            tv = t_parts[idx] if idx < len(t_parts) else ""
            cls = "diff-cell" if bv != tv else ""
            b_cells.append(f'<td class="{cls}"><code>{esc(bv)}</code></td>')
            t_cells.append(f'<td class="{cls}"><code>{esc(tv)}</code></td>')
        diffs_rows.append(
            f'<tr><td rowspan="2" class="num"><b>#{d["row"]}</b></td><td><span class="pill">Base</span></td>{"".join(b_cells)}</tr>'
            f'<tr><td><span class="pill accent">Target</span></td>{"".join(t_cells)}</tr>'
        )
    diffs_headers = "<th>Row</th><th>Run</th>" + "".join(f"<th>{esc(col)}</th>" for col in deal_no_time_cols)
    diffs_html = "\n".join(diffs_rows)

    bs = c.get("base_settings") or {}
    ts = c.get("target_settings") or {}
    curve_svg = _build_svg_balance_curve(
        [
            (f"Base: {c['base_name']}", "#0284c7", c.get("base_balances") or []),
            (f"Target: {c['target_name']}", "#10b981", c.get("target_balances") or []),
        ]
    )

    kpis_html = f"""
  <div class="kpis">
    <div class="kpi"><div class="kpi-label">Base Run</div><div class="kpi-val">{esc(c['base_name'])}</div><div class="kpi-sub">{esc(bs.get('Symbol','?'))} {esc(bs.get('Period','?'))} &bull; {c['base_rows']} deals</div></div>
    <div class="kpi"><div class="kpi-label">Target Run</div><div class="kpi-val">{esc(c['target_name'])}</div><div class="kpi-sub">{esc(ts.get('Symbol','?'))} {esc(ts.get('Period','?'))} &bull; {c['target_rows']} deals</div></div>
    <div class="kpi"><div class="kpi-label">Deals Parity</div><div class="kpi-val {'good' if c['verdict']=='IDENTICAL' else 'bad'}">{esc(c['verdict'])}</div><div class="kpi-sub">{'First diff at row #' + str(c['first_diff_row']) if c['first_diff_row'] else '100% deal match (time ignored)'}</div></div>
    <div class="kpi"><div class="kpi-label">Changed Inputs</div><div class="kpi-val">{len(c['input_diffs'])}</div><div class="kpi-sub">Parameters differing</div></div>
  </div>"""

    return (
        _shared_html_head(f"MT5 Comparison — {c['base_name']} vs {c['target_name']}")
        + f"""
<body>
<div class="wrap">
  <div class="header">
    <div>
      <h1 class="title">Run Comparison: {esc(c['base_name'])} vs {esc(c['target_name'])}</h1>
      <div class="subtitle">
        <span>Base: <b>{c['base_rows']}</b> deals</span>
        <span>Target: <b>{c['target_rows']}</b> deals</span>
        <span class="pill">Timestamp column ignored</span>
      </div>
    </div>
    <div class="header-right">
      <span class="badge" style="background:{badge_color}">{esc(c['verdict'])}</span>
      <button id="themeBtn" class="btn" onclick="toggleTheme()">Dark Mode</button>
    </div>
  </div>

  {kpis_html}

  <div class="panel">
    <div class="panel-head"><h2>Overlay Balance Curve (Base vs Target)</h2></div>
    {curve_svg}
  </div>

  {"<div class='panel'><div class='panel-head'><h2>Metrics Delta</h2></div><div class='scroll-box'><table><thead><tr><th>Metric</th><th class='num'>Base</th><th class='num'>Target</th><th class='num'>Delta</th></tr></thead><tbody>" + metrics_html + "</tbody></table></div></div>" if metrics_html else ""}
  {"<div class='panel'><div class='panel-head'><h2>Input Differences (" + str(len(c['input_diffs'])) + ")</h2></div><div class='scroll-box'><table><thead><tr><th>Input</th><th>Base</th><th>Target</th></tr></thead><tbody>" + inputs_html + "</tbody></table></div></div>" if inputs_html else ""}
  {"<div class='panel'><div class='panel-head'><h2>Sample Differing Deals (highlighted cells show exact divergence)</h2></div><div class='scroll-box'><table><thead><tr>" + diffs_headers + "</tr></thead><tbody>" + diffs_html + "</tbody></table></div></div>" if diffs_html else ""}
</div>
"""
        + _shared_theme_script()
        + "</body>\n</html>\n"
    )


# ---------------------------------------------------------------------------
# Subcommand: install (Multi-AI Skill & /mt5-headless Slash Command Installer)
# ---------------------------------------------------------------------------

_SKILL_COPY_ITEMS = ("SKILL.md", "README.md", "README.th.md", "scripts", "references", "examples")
_SKILL_SKIP_NAMES = {".git", "__pycache__", ".python", ".mt5-portable", ".env", ".mt5-headless.json"}


def _ignore_skill_junk(_dir: str, names: List[str]) -> set[str]:
    return {
        n
        for n in names
        if n in _SKILL_SKIP_NAMES or n.endswith(".pyc") or (n.startswith(".env.") and n != ".env.example")
    }


def _copy_skill_tree(src_root: str, dst_dir: str, dry_run: bool = False) -> None:
    if os.path.normcase(os.path.abspath(src_root)) == os.path.normcase(os.path.abspath(dst_dir)):
        return
    if dry_run:
        return
    os.makedirs(dst_dir, exist_ok=True)
    for item in _SKILL_COPY_ITEMS:
        s_path = os.path.join(src_root, item)
        d_path = os.path.join(dst_dir, item)
        if os.path.isdir(s_path):
            shutil.copytree(s_path, d_path, ignore=_ignore_skill_junk, dirs_exist_ok=True)
        elif os.path.isfile(s_path):
            shutil.copy2(s_path, d_path)


def _render_slash_command_md(skill_dir: str, flavor: str = "generic") -> str:
    sd = skill_dir.replace("\\", "/")
    py_script = f"{sd}/scripts/mt5_headless.py"
    cmd_script = f"{sd}/scripts/mt5-headless.cmd"
    ps_script = f"{sd}/scripts/Invoke-MT5Headless.ps1"
    skill_md = f"{sd}/SKILL.md"
    desc = "Run MT5 Headless Tester workflows (discover, bind, portable, inspect, probe, set, compile, test, optimize, report, compare)"

    body = (
        f"# /mt5-headless — MT5 Headless Tester\n\n"
        f"Load and strictly follow the complete instructions and safety rules in `{skill_md}`.\n\n"
        f"## Skill Entry Points\n"
        f"- **Python CLI**: `python \"{py_script}\" <subcommand> ...`\n"
        f"- **CMD Wrapper**: `\"{cmd_script}\" <subcommand> ...`\n"
        f"- **PowerShell Wrapper**: `powershell -NoProfile -ExecutionPolicy Bypass -File \"{ps_script}\" <subcommand> ...`\n\n"
        f"## Interactive Setup Rules\n"
        f"1. If `.mt5-headless.json` does not exist in the current workspace, run `discover` first and **ask the user**:\n"
        f"   - Which MT5 installation `[N]` to bind (`bind --select N`), or whether to provision an isolated `.mt5-portable/` (`portable --from-select N`).\n"
        f"   - Whether they use Ghost MCP (`mcp__ghost_window`) or a built-in launcher (`--launch-mode hidden-desktop` recommended, `minimized`, or `hidden`).\n"
        f"2. Never read `.env`, never commit `.env`/`.mt5-portable/` to Git, and never kill `terminal64.exe`.\n\n"
        f"## User Arguments / Task\n"
        f"$ARGUMENTS\n"
    )

    if flavor in ("claude", "cursor"):
        return body
    if flavor == "copilot":
        return f"---\nmode: 'agent'\ndescription: '{desc}'\n---\n\n" + body
    if flavor == "cursor-mdc":
        return (
            f"---\ndescription: '{desc}'\n"
            f'globs: ["*.mq5", "*.mqh", "*.ex5", "*.set", "*.ini"]\n'
            f"alwaysApply: false\n---\n\n"
            + body
        )
    if flavor == "copilot-instructions":
        return f"---\napplyTo: '**/*.{{mq5,mqh,set,ini}}'\n---\n\n" + body
    return f"---\ndescription: {desc}\n---\n\n" + body


def _upsert_agents_md(agents_md_path: str, skill_dir: str, dry_run: bool = False) -> None:
    sd = skill_dir.replace("\\", "/")
    start_mk = "<!-- mt5-headless-tester:start -->"
    end_mk = "<!-- mt5-headless-tester:end -->"
    block = (
        f"{start_mk}\n"
        f"## Skill & Command: `/mt5-headless` (`mt5-headless-tester`)\n"
        f"- When the user invokes `/mt5-headless` or asks to configure EA inputs, generate `.set` files, "
        f"compile `.mq5`, probe `.ex5`/Market EAs, run headless MT5 backtests, run parallel optimizations, or compare/export reports:\n"
        f"  1. Read `{sd}/SKILL.md` for workflow and safety rules (never read `.env`, never kill `terminal64.exe`, ask before binding MT5 & choosing `--launch-mode`).\n"
        f"  2. Execute via `\"{sd}/scripts/mt5-headless.cmd\"` or `python \"{sd}/scripts/mt5_headless.py\"`.\n"
        f"{end_mk}\n"
    )
    if dry_run:
        return
    existing = read_text_auto(agents_md_path) if os.path.isfile(agents_md_path) else ""
    if start_mk in existing and end_mk in existing:
        updated = re.sub(
            re.escape(start_mk) + r".*?" + re.escape(end_mk) + r"\r?\n?",
            block,
            existing,
            flags=re.S,
        )
    else:
        sep = "\n\n" if existing and not existing.endswith("\n\n") else ""
        updated = existing + sep + block
    write_utf8(agents_md_path, updated)


def _detect_auto_targets(home_dir: str, proj_dir: str, scope: str) -> List[str]:
    checks = {
        "antigravity": [
            os.path.join(home_dir, ".gemini"),
            os.path.join(proj_dir, ".agent"),
        ],
        "claude": [
            os.path.join(home_dir, ".claude"),
            os.path.join(proj_dir, ".claude"),
        ],
        "codex": [
            os.path.join(home_dir, ".codex"),
            os.path.join(home_dir, ".agents"),
            os.path.join(proj_dir, ".agents"),
        ],
        "cursor": [
            os.path.join(home_dir, ".cursor"),
            os.path.join(proj_dir, ".cursor"),
        ],
        "windsurf": [
            os.path.join(home_dir, ".codeium", "windsurf"),
            os.path.join(home_dir, ".windsurf"),
            os.path.join(proj_dir, ".windsurf"),
        ],
        "cline": [
            os.path.join(home_dir, "Documents", "Cline"),
            os.path.join(home_dir, ".cline"),
            os.path.join(proj_dir, ".clinerules"),
            os.path.join(proj_dir, ".cline"),
        ],
        "roo": [
            os.path.join(home_dir, ".roo"),
            os.path.join(proj_dir, ".roo"),
        ],
        "copilot": [
            os.path.join(home_dir, ".copilot"),
            os.path.join(proj_dir, ".github"),
        ],
        "agents": [
            os.path.join(home_dir, ".agents"),
            os.path.join(proj_dir, "AGENTS.md"),
        ],
    }
    selected = [t for t, paths_to_probe in checks.items() if any(os.path.exists(p) for p in paths_to_probe)]
    if scope in ("global", "both"):
        for core_t in ("antigravity", "claude", "codex", "agents"):
            if core_t not in selected:
                selected.append(core_t)
    elif not selected:
        selected = ["antigravity", "claude", "codex", "cursor", "windsurf", "agents"]
    return selected


def _resolve_skill_source_root(base_dir: str) -> str:
    if os.path.isfile(os.path.join(base_dir, "SKILL.md")):
        return base_dir
    for root, _, files in os.walk(base_dir):
        if "SKILL.md" in files and os.path.isfile(os.path.join(root, "scripts", "mt5_headless.py")):
            return root
    raise FileNotFoundError(f"Could not find SKILL.md + scripts/mt5_headless.py under: {base_dir}")


def cmd_install(args: argparse.Namespace) -> int:
    home_dir = os.path.abspath(args.dest_root or os.path.expanduser("~"))
    proj_dir = os.path.abspath(args.project_dir or os.getcwd())
    scope = args.scope
    dry_run = bool(args.dry_run)

    raw_targets = args.target or ["auto"]
    expanded: List[str] = []
    for t in raw_targets:
        if t == "all":
            expanded.extend(["antigravity", "claude", "codex", "cursor", "windsurf", "cline", "roo", "copilot", "agents"])
        elif t == "auto":
            expanded.extend(_detect_auto_targets(home_dir, proj_dir, scope))
        else:
            expanded.append(t)
    targets = list(dict.fromkeys(expanded))

    git_stage: Optional[str] = None
    if args.git_url:
        tmp_base = os.environ.get("TEMP") or os.environ.get("TMP") or home_dir
        git_stage = os.path.join(tmp_base, f"mt5-headless-git-{os.getpid()}")
        if os.path.isdir(git_stage):
            shutil.rmtree(git_stage, ignore_errors=True)
        res_git = subprocess.run(
            ["git", "clone", "--depth", "1", args.git_url, git_stage],
            capture_output=True,
            text=True,
            check=False,
        )
        if res_git.returncode != 0:
            raise RuntimeError(f"git clone failed: {(res_git.stderr or res_git.stdout).strip()}")
        src_root = _resolve_skill_source_root(git_stage)
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        candidate_root = os.path.dirname(script_dir)
        src_root = _resolve_skill_source_root(candidate_root)

    actions: List[Dict[str, str]] = []
    seen_skill_dirs: set[str] = set()
    seen_cmd_files: set[str] = set()

    def _install_skill(dst_dir: str, target_name: str, sc_label: str) -> None:
        norm = os.path.normcase(os.path.abspath(dst_dir))
        if norm not in seen_skill_dirs:
            seen_skill_dirs.add(norm)
            _copy_skill_tree(src_root, dst_dir, dry_run=dry_run)
        actions.append({"target": target_name, "scope": sc_label, "kind": "skill", "path": os.path.abspath(dst_dir)})

    def _write_cmd(path: str, skill_dir: str, flavor: str, target_name: str, sc_label: str, invoke_hint: str) -> None:
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen_cmd_files:
            return
        seen_cmd_files.add(norm)
        if not dry_run:
            write_utf8(path, _render_slash_command_md(skill_dir, flavor=flavor))
        actions.append(
            {
                "target": target_name,
                "scope": sc_label,
                "kind": "command",
                "path": os.path.abspath(path),
                "invoke": invoke_hint,
            }
        )

    def _update_agents(path: str, skill_dir: str, target_name: str, sc_label: str) -> None:
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen_cmd_files:
            return
        seen_cmd_files.add(norm)
        _upsert_agents_md(path, skill_dir, dry_run=dry_run)
        actions.append(
            {
                "target": target_name,
                "scope": sc_label,
                "kind": "agents-md",
                "path": os.path.abspath(path),
                "invoke": "/mt5-headless",
            }
        )

    try:
        scopes_to_run = ["global", "project"] if scope == "both" else [scope]
        for sc in scopes_to_run:
            for t in targets:
                if sc == "global":
                    if t == "antigravity":
                        sdir = os.path.join(home_dir, ".gemini", "antigravity", "skills", "mt5-headless-tester")
                        _install_skill(sdir, t, sc)
                        _write_cmd(
                            os.path.join(home_dir, ".gemini", "antigravity", "global_workflows", "mt5-headless.md"),
                            sdir, "antigravity", t, sc, "/mt5-headless",
                        )
                    elif t == "claude":
                        sdir = os.path.join(home_dir, ".claude", "skills", "mt5-headless-tester")
                        _install_skill(sdir, t, sc)
                        _write_cmd(
                            os.path.join(home_dir, ".claude", "commands", "mt5-headless.md"),
                            sdir, "claude", t, sc, "/mt5-headless",
                        )
                    elif t == "codex":
                        sdir = os.path.join(home_dir, ".agents", "skills", "mt5-headless-tester")
                        _install_skill(sdir, t, sc)
                        _write_cmd(
                            os.path.join(home_dir, ".codex", "prompts", "mt5-headless.md"),
                            sdir, "generic", t, sc, "/prompts:mt5-headless",
                        )
                        _update_agents(os.path.join(home_dir, ".codex", "AGENTS.md"), sdir, t, sc)
                    elif t == "cursor":
                        sdir = os.path.join(home_dir, ".cursor", "skills", "mt5-headless-tester")
                        _install_skill(sdir, t, sc)
                        _write_cmd(
                            os.path.join(home_dir, ".cursor", "commands", "mt5-headless.md"),
                            sdir, "cursor", t, sc, "/mt5-headless",
                        )
                    elif t == "windsurf":
                        sdir = os.path.join(home_dir, ".codeium", "windsurf", "skills", "mt5-headless-tester")
                        _install_skill(sdir, t, sc)
                        _write_cmd(
                            os.path.join(home_dir, ".codeium", "windsurf", "global_workflows", "mt5-headless.md"),
                            sdir, "windsurf", t, sc, "/mt5-headless",
                        )
                    elif t == "cline":
                        sdir = os.path.join(home_dir, ".cline", "skills", "mt5-headless-tester")
                        _install_skill(sdir, t, sc)
                        _write_cmd(
                            os.path.join(home_dir, "Documents", "Cline", "Workflows", "mt5-headless.md"),
                            sdir, "cline", t, sc, "/mt5-headless.md",
                        )
                    elif t == "roo":
                        sdir = os.path.join(home_dir, ".roo", "skills", "mt5-headless-tester")
                        _install_skill(sdir, t, sc)
                        _write_cmd(
                            os.path.join(home_dir, ".roo", "commands", "mt5-headless.md"),
                            sdir, "claude", t, sc, "/mt5-headless",
                        )
                    elif t == "copilot":
                        sdir = os.path.join(home_dir, ".copilot", "skills", "mt5-headless-tester")
                        _install_skill(sdir, t, sc)
                        _write_cmd(
                            os.path.join(home_dir, ".copilot", "prompts", "mt5-headless.prompt.md"),
                            sdir, "copilot", t, sc, "/mt5-headless",
                        )
                    elif t == "agents":
                        sdir = os.path.join(home_dir, ".agents", "skills", "mt5-headless-tester")
                        _install_skill(sdir, t, sc)
                        _update_agents(os.path.join(home_dir, ".agents", "AGENTS.md"), sdir, t, sc)
                else:  # project scope
                    if t == "antigravity":
                        sdir = os.path.join(proj_dir, ".agent", "skills", "mt5-headless-tester")
                        _install_skill(sdir, t, sc)
                        _write_cmd(
                            os.path.join(proj_dir, ".agent", "workflows", "mt5-headless.md"),
                            sdir, "antigravity", t, sc, "/mt5-headless",
                        )
                    elif t == "claude":
                        sdir = os.path.join(proj_dir, ".claude", "skills", "mt5-headless-tester")
                        _install_skill(sdir, t, sc)
                        _write_cmd(
                            os.path.join(proj_dir, ".claude", "commands", "mt5-headless.md"),
                            sdir, "claude", t, sc, "/mt5-headless",
                        )
                    elif t == "codex":
                        sdir = os.path.join(proj_dir, ".agents", "skills", "mt5-headless-tester")
                        _install_skill(sdir, t, sc)
                        _update_agents(os.path.join(proj_dir, "AGENTS.md"), sdir, t, sc)
                    elif t == "cursor":
                        sdir = os.path.join(proj_dir, ".cursor", "skills", "mt5-headless-tester")
                        _install_skill(sdir, t, sc)
                        _write_cmd(
                            os.path.join(proj_dir, ".cursor", "commands", "mt5-headless.md"),
                            sdir, "cursor", t, sc, "/mt5-headless",
                        )
                        _write_cmd(
                            os.path.join(proj_dir, ".cursor", "rules", "mt5-headless-tester.mdc"),
                            sdir, "cursor-mdc", t, sc, "@mt5-headless-tester",
                        )
                    elif t == "windsurf":
                        sdir = os.path.join(proj_dir, ".windsurf", "skills", "mt5-headless-tester")
                        _install_skill(sdir, t, sc)
                        _write_cmd(
                            os.path.join(proj_dir, ".windsurf", "workflows", "mt5-headless.md"),
                            sdir, "windsurf", t, sc, "/mt5-headless",
                        )
                    elif t == "cline":
                        sdir = os.path.join(proj_dir, ".cline", "skills", "mt5-headless-tester")
                        _install_skill(sdir, t, sc)
                        _write_cmd(
                            os.path.join(proj_dir, ".clinerules", "workflows", "mt5-headless.md"),
                            sdir, "cline", t, sc, "/mt5-headless.md",
                        )
                    elif t == "roo":
                        sdir = os.path.join(proj_dir, ".roo", "skills", "mt5-headless-tester")
                        _install_skill(sdir, t, sc)
                        _write_cmd(
                            os.path.join(proj_dir, ".roo", "commands", "mt5-headless.md"),
                            sdir, "claude", t, sc, "/mt5-headless",
                        )
                    elif t == "copilot":
                        sdir = os.path.join(proj_dir, ".agents", "skills", "mt5-headless-tester")
                        _install_skill(sdir, t, sc)
                        _write_cmd(
                            os.path.join(proj_dir, ".github", "prompts", "mt5-headless.prompt.md"),
                            sdir, "copilot", t, sc, "/mt5-headless",
                        )
                        _write_cmd(
                            os.path.join(proj_dir, ".github", "instructions", "mt5-headless.instructions.md"),
                            sdir, "copilot-instructions", t, sc, "auto-attached",
                        )
                    elif t == "agents":
                        sdir = os.path.join(proj_dir, ".agents", "skills", "mt5-headless-tester")
                        _install_skill(sdir, t, sc)
                        _update_agents(os.path.join(proj_dir, "AGENTS.md"), sdir, t, sc)

        summary = {
            "source_root": src_root,
            "scope": scope,
            "targets": targets,
            "dry_run": dry_run,
            "actions": actions,
        }
        if args.json:
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            return 0

        print(f"=== MT5 Headless Tester Multi-AI Installer ({'DRY-RUN' if dry_run else 'INSTALLED'}) ===")
        print(f"Source  : {src_root}")
        print(f"Scope   : {scope}")
        print(f"Targets : {', '.join(targets)}")
        print("")
        for act in actions:
            inv = f"  [{act['invoke']}]" if act.get("invoke") else ""
            print(f"  + {act['target']:<12} ({act['scope']:<7}) {act['kind']:<9} -> {act['path']}{inv}")
        print("\nSpecial command installed: invoke '/mt5-headless' (or '/prompts:mt5-headless' in Codex CLI) inside your AI assistant.")
        return 0
    finally:
        if git_stage and os.path.isdir(git_stage):
            shutil.rmtree(git_stage, ignore_errors=True)


# ---------------------------------------------------------------------------
# Argument Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project-dir", help="Project root containing .mt5-headless.json / .env (default: CWD)")
    common.add_argument("--portable", action="store_true", default=None, help="Run target MT5 in /portable mode")
    common.add_argument(
        "--launch-mode",
        choices=LAUNCH_MODES,
        default=None,
        help="Focus-safe launch strategy: minimized (SW_SHOWMINNOACTIVE, default), hidden-desktop (Win32 CreateDesktopW), hidden (SW_HIDE), or ghost (external Ghost MCP)",
    )
    common.add_argument("--terminal-dir", help="MT5 install directory (default: C:\\Program Files\\MetaTrader 5)")
    common.add_argument("--data-dir", help="MT5 terminal data folder (default: Main Install GUID)")
    common.add_argument("--tester-dir", help="MT5 tester agent root folder")
    common.add_argument("--work-dir", help="Temporary working folder for generated .ini and deal dumps")

    ap = argparse.ArgumentParser(
        prog="mt5_headless.py",
        description="MT5 Headless Tester CLI: discover MT5 installs, bind/provision portable, inspect EA inputs/enums, build .set files, compile, run headless tests, export reports, and compare runs.",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    # discover
    p_disc = sub.add_parser("discover", parents=[common], help="Scan for all MT5 installations, AppData data folders, and portable runtimes")
    p_disc.add_argument("--scan-dir", action="append", default=[], help="Extra directory to scan for terminal64.exe (repeatable)")
    p_disc.add_argument("--json", action="store_true", help="Output discovered instances as JSON")
    p_disc.set_defaults(func=cmd_discover)

    # bind
    p_bind = sub.add_parser("bind", parents=[common], help="Bind the project (.mt5-headless.json) to a chosen MT5 installation / data folder")
    p_bind.add_argument("--select", type=int, help="Instance index from 'discover'")
    p_bind.add_argument("--portable-dir", help="Bind directly to an existing portable MT5 folder")
    p_bind.add_argument("--scan-dir", action="append", default=[], help="Extra directory passed to discovery when using --select")
    p_bind.add_argument("--init-env", action="store_true", help="Create .env template (protected by .gitignore & .git/info/exclude)")
    p_bind.add_argument("--json", action="store_true", help="Output binding info as JSON")
    p_bind.set_defaults(func=cmd_bind)

    # portable
    p_port = sub.add_parser("portable", parents=[common], help="Provision an isolated project-local /portable MT5 + .env template + Git exclusions")
    p_port.add_argument("--dest", help="Portable directory path (default: <project-dir>\\.mt5-portable)")
    p_port.add_argument("--from-select", type=int, help="Source MT5 index from 'discover' to copy binaries/include/servers from")
    p_port.add_argument("--from-terminal-dir", help="Source MT5 install folder containing terminal64.exe")
    p_port.add_argument("--from-data-dir", help="Source MT5 data folder for MQL5\\Include and config\\servers.ini")
    p_port.add_argument("--scan-dir", action="append", default=[], help="Extra directory passed to discovery when using --from-select")
    p_port.add_argument("--copy-experts", action="store_true", help="Also copy MQL5\\Experts from source data folder")
    p_port.add_argument("--copy-profiles", action="store_true", help="Also copy MQL5\\Profiles\\Tester from source data folder")
    p_port.add_argument("--json", action="store_true", help="Output provisioning summary as JSON")
    p_port.set_defaults(func=cmd_portable)

    # scrub-ini
    p_scrub = sub.add_parser("scrub-ini", parents=[common], help="Erase plaintext Password= from a generated tester .ini file")
    p_scrub.add_argument("ini_path", help="Path to tester .ini file")
    p_scrub.set_defaults(func=cmd_scrub_ini)

    # init
    p_init = sub.add_parser("init", parents=[common], help="Verify MT5 paths, terminal status, env auth, profiles, presets, and experts")
    p_init.add_argument("--json", action="store_true", help="Output environment check as JSON")
    p_init.set_defaults(func=cmd_init)

    # inspect
    p_ins = sub.add_parser("inspect", parents=[common], help="Inspect EA inputs and enums from .mq5/.mqh, .ini profiles, .set files, or .htm reports")
    p_ins.add_argument("--source", help="Path to .mq5 file or MQL5 source directory")
    p_ins.add_argument("--profile", help="Tester profile .ini name or path")
    p_ins.add_argument("--set-file", help=".set file name or path")
    p_ins.add_argument("--report", help="Bare report name or .htm path (for compiled .ex5 without .mq5 source)")
    p_ins.add_argument("--search", help="Filter inputs/enums by regex/substring")
    p_ins.add_argument("--group", help="Filter inputs by input group regex/substring")
    p_ins.add_argument("--enums-only", action="store_true", help="List enum definitions and ordinals from --source")
    p_ins.add_argument("--format", choices=["text", "json", "md"], default="text", help="Output format")
    p_ins.add_argument("--out", help="Write output to file instead of stdout")
    p_ins.set_defaults(func=cmd_inspect)

    # probe
    p_prb = sub.add_parser("probe", parents=[common], help="Run or prepare a fast 1s probe test on a compiled .ex5 (incl. Market EAs) to dump all inputs and generate a .set")
    p_prb.add_argument("--expert", required=True, help="Path under MQL5\\Experts including .ex5 (e.g. Market\\MyBot.ex5)")
    p_prb.add_argument("--symbol", required=True, help="Symbol name (e.g. XAUUSD)")
    p_prb.add_argument("--period", default="M15", help="Timeframe (default: M15)")
    p_prb.add_argument("--model", choices=["0", "1", "2", "3", "4"], default="2", help="Modeling mode (default: 2 = Open prices only for fast probe)")
    p_prb.add_argument("--from-date", default="2026.10.01", help="Start date yyyy.MM.dd (default: 2026.10.01)")
    p_prb.add_argument("--to-date", default="2026.10.03", help="End date yyyy.MM.dd (default: 2026.10.03)")
    p_prb.add_argument("--report", help="Bare probe report name (default: probe_<expert_stem>)")
    p_prb.add_argument("--out-set", help="Write extracted defaults as a UTF-16LE .set file")
    p_prb.add_argument("--save-to-tester", help="Save extracted defaults into MQL5\\Profiles\\Tester\\<name>.set")
    p_prb.add_argument("--ini-out", help="Custom output .ini path")
    p_prb.add_argument("--run", action="store_true", help="Execute terminal64.exe minimized, parse probe .htm, and write .set")
    p_prb.add_argument("--format", choices=["text", "json", "md"], default="text", help="Printed summary format when --run is used")
    p_prb.set_defaults(func=cmd_probe)

    # set
    p_set = sub.add_parser("set", parents=[common], help="Build, update, validate, or diff UTF-16LE .set files")
    p_set.add_argument("--from-profile", "--profile", dest="from_profile", help="Base tester .ini profile name or path")
    p_set.add_argument("--from-set", "--set-file", dest="from_set", help="Base .set file name or path")
    p_set.add_argument("--from-report", help="Base inputs extracted from a Strategy Tester .htm report (for compiled .ex5 without source)")
    p_set.add_argument("--from-source", help="Build base inputs directly from .mq5/.mqh defaults")
    p_set.add_argument("--source", help="Reference .mq5/.mqh source for enum name -> ordinal resolution")
    p_set.add_argument("--set", action="append", default=[], help="Override input(s): Key=Val (repeatable)")
    p_set.add_argument("--set-json", help="JSON object string or file path of Key:Val overrides")
    p_set.add_argument("--opt", action="append", default=[], help="Set optimization range: Key=start:step:stop (or Key=off)")
    p_set.add_argument("--allow-new", action="store_true", help="Allow adding inputs not present in base profile/set")
    p_set.add_argument("--diff", nargs=2, metavar=("FILE_A", "FILE_B"), help="Compare two .set or .ini files")
    p_set.add_argument("--format", choices=["text", "json", "md"], default="text", help="Output format for --diff")
    p_set.add_argument("--out", help="Output .set path (or diff report path)")
    p_set.add_argument("--save-to-tester", help="Save directly into MQL5\\Profiles\\Tester\\<name>.set")
    p_set.set_defaults(func=cmd_set)

    # compile
    p_cmp = sub.add_parser("compile", parents=[common], help="Headless MetaEditor64.exe compile gate (handles spaces in filenames & icons)")
    p_cmp.add_argument("--source", required=True, help="Path to .mq5 source file")
    p_cmp.add_argument("--include", help="Single MQL5 include root (default: <DataDir>\\MQL5)")
    p_cmp.add_argument("--deploy-to", help="Copy compiled .ex5 to relative MQL5\\Experts path or absolute path")
    p_cmp.add_argument("--keep-stage", action="store_true", help="Keep temporary staging directory")
    p_cmp.add_argument("--json", action="store_true", help="Output compile result as JSON")
    p_cmp.set_defaults(func=cmd_compile)

    # test
    p_tst = sub.add_parser("test", parents=[common], help="Generate tester .ini (and optionally run headless Strategy Tester)")
    p_tst.add_argument("--report", required=True, help="Bare report name without path or extension (MT5 writes <Report>.htm)")
    p_tst.add_argument("--profile", help="Base tester .ini profile name or path")
    p_tst.add_argument("--set-file", "--from-set", dest="set_file", help="Overlay .set file name or path")
    p_tst.add_argument("--from-report", help="Base inputs extracted from a Strategy Tester .htm report")
    p_tst.add_argument("--from-source", help="Build base inputs from .mq5/.mqh defaults if no profile/set is used")
    p_tst.add_argument("--source", help="Reference .mq5/.mqh source for enum name -> ordinal resolution")
    p_tst.add_argument("--expert", help="Path under MQL5\\Experts including .ex5")
    p_tst.add_argument("--symbol", help="Symbol name (e.g. XAUUSD)")
    p_tst.add_argument("--period", help="Timeframe (e.g. M1, M15, H1)")
    p_tst.add_argument("--model", choices=["0", "1", "2", "3", "4"], help="0=every tick, 1=1m OHLC, 2=open prices, 3=math, 4=real ticks")
    p_tst.add_argument("--from-date", help="Start date yyyy.MM.dd")
    p_tst.add_argument("--to-date", help="End date yyyy.MM.dd")
    p_tst.add_argument("--deposit", type=int, help="Initial deposit")
    p_tst.add_argument("--currency", help="Deposit currency (e.g. USD)")
    p_tst.add_argument("--leverage", type=int, help="Leverage 1:N (pass N)")
    p_tst.add_argument("--execution-mode", type=int, help="Execution delay in ms")
    p_tst.add_argument("--optimization", choices=["0", "1", "2", "3"], help="Optimization mode")
    p_tst.add_argument("--criterion", choices=[str(i) for i in range(8)], help="Optimization criterion 0..7")
    p_tst.add_argument("--visual", action="store_true", help="Enable visual mode (disabled by default)")
    p_tst.add_argument("--set", action="append", default=[], help="Override input(s): Key=Val (repeatable)")
    p_tst.add_argument("--set-json", help="JSON object string or file path of Key:Val overrides")
    p_tst.add_argument("--opt", action="append", default=[], help="Set optimization range: Key=start:step:stop")
    p_tst.add_argument("--allow-new", action="store_true", help="Allow overrides not present in base profile/set")
    p_tst.add_argument("--ini-out", help="Custom output .ini path (default: <work-dir>\\<Report>.ini)")
    p_tst.add_argument("--use-env-auth", action="store_true", help="Require MT5_BROKER_SERVER, MT5_LOGIN, MT5_PASSWORD from env/.env")
    p_tst.add_argument("--run", action="store_true", help="Execute terminal64.exe minimized without stealing focus and summarize report")
    p_tst.add_argument("--format", choices=["text", "md", "json", "html"], help="Post-run report export format (when --run is used)")
    p_tst.add_argument("--out", help="Post-run report export path (when --run is used)")
    p_tst.add_argument("--deals", help="Post-run deals txt export path")
    p_tst.add_argument("--embed-images", action="store_true", help="Embed chart PNGs as base64 in HTML report")
    p_tst.set_defaults(func=cmd_test)

    # optimize
    opt_sort_choices = [
        "result",
        "profit",
        "profit_factor",
        "sharpe",
        "recovery_factor",
        "expected_payoff",
        "drawdown_pct",
        "trades",
    ]
    p_opt = sub.add_parser("optimize", parents=[common], help="Parallel multi-core Optimization (local metatester64 agents) + XML ranking & best .set export")
    p_opt.add_argument("--report", required=True, help="Bare report name (MT5 writes <Report>.xml)")
    p_opt.add_argument("--profile", help="Base tester .ini profile name or path")
    p_opt.add_argument("--set-file", "--from-set", dest="set_file", help="Overlay .set file name or path")
    p_opt.add_argument("--from-report", help="Base inputs extracted from a Strategy Tester .htm report")
    p_opt.add_argument("--from-source", help="Build base inputs from .mq5/.mqh defaults")
    p_opt.add_argument("--source", help="Reference .mq5/.mqh source for enum name -> ordinal resolution")
    p_opt.add_argument("--expert", help="Path under MQL5\\Experts including .ex5")
    p_opt.add_argument("--symbol", help="Symbol name")
    p_opt.add_argument("--period", help="Timeframe (e.g. M1, M15, H1)")
    p_opt.add_argument("--model", choices=["0", "1", "2", "3", "4"], help="Modeling mode (0=every tick, 1=1m OHLC, 4=real ticks)")
    p_opt.add_argument("--from-date", help="Start date yyyy.MM.dd")
    p_opt.add_argument("--to-date", help="End date yyyy.MM.dd")
    p_opt.add_argument("--deposit", type=int, help="Initial deposit")
    p_opt.add_argument("--currency", help="Deposit currency")
    p_opt.add_argument("--leverage", type=int, help="Leverage 1:N (pass N)")
    p_opt.add_argument("--mode", choices=["1", "2"], default="1", help="1=Slow complete grid (default), 2=Fast genetic algorithm")
    p_opt.add_argument("--criterion", choices=[str(i) for i in range(8)], default="0", help="Optimization criterion 0..7 (default: 0 Max Balance)")
    p_opt.add_argument("--set", action="append", default=[], help="Fixed input override(s): Key=Val (repeatable)")
    p_opt.add_argument("--set-json", help="JSON object string or file path of fixed overrides")
    p_opt.add_argument("--opt", action="append", default=[], help="Parameter sweep: Key=start:step:stop (repeatable; supports enum names)")
    p_opt.add_argument("--keep-profile-opt", action="store_true", help="Keep existing ||Y flags from base profile instead of resetting to ||N first")
    p_opt.add_argument("--allow-new", action="store_true", help="Allow overrides not present in base profile/set")
    p_opt.add_argument("--ini-out", help="Custom output .ini path")
    p_opt.add_argument("--use-env-auth", action="store_true", help="Require MT5_BROKER_SERVER, MT5_LOGIN, MT5_PASSWORD from env/.env")
    p_opt.add_argument("--run", action="store_true", help="Execute parallel optimization minimized, parse .xml, and export leaderboard/.set")
    p_opt.add_argument("--sort-by", choices=opt_sort_choices, default="result", help="Metric to rank passes (default: result)")
    p_opt.add_argument("--top", type=int, default=20, help="Number of top passes to display (default: 20)")
    p_opt.add_argument("--min-trades", type=int, help="Filter minimum trades")
    p_opt.add_argument("--min-pf", type=float, help="Filter minimum Profit Factor")
    p_opt.add_argument("--max-dd-pct", type=float, help="Filter maximum Drawdown %%")
    p_opt.add_argument("--require-positive", action="store_true", help="Keep only passes with Profit > 0")
    p_opt.add_argument("--pick-pass", type=int, help="Export a specific Pass # instead of rank #1")
    p_opt.add_argument("--export-best-set", help="Save winning pass as UTF-16LE .set file")
    p_opt.add_argument("--save-best-to-tester", help="Save winning pass to MQL5\\Profiles\\Tester\\<name>.set")
    p_opt.add_argument("--format", choices=["text", "md", "json", "html"], help="Leaderboard export format")
    p_opt.add_argument("--out", help="Leaderboard export file path")
    p_opt.set_defaults(func=cmd_optimize)

    # report
    p_rep = sub.add_parser("report", parents=[common], help="Parse an MT5 .htm backtest report or .xml optimization report and export to text, md, json, or html")
    p_rep.add_argument("report", help="Bare report name (under DataDir) or path to .htm/.xml file")
    p_rep.add_argument("--xml", action="store_true", help="Force parsing as Optimization XML report")
    p_rep.add_argument("--format", choices=["text", "md", "json", "html"], help="Export format (inferred from --out if omitted)")
    p_rep.add_argument("--out", help="Output file path")
    p_rep.add_argument("--deals", help="Write pipe-delimited deals table to file (.htm only)")
    p_rep.add_argument("--include-deals", action="store_true", help="Include Deals table in Markdown output (.htm only)")
    p_rep.add_argument("--max-deals", type=int, default=200, help="Max deal rows shown in Markdown output (default: 200)")
    p_rep.add_argument("--include-orders", action="store_true", help="Include Orders table in Markdown output (.htm only)")
    p_rep.add_argument("--embed-images", action="store_true", help="Embed chart PNGs as base64 data URIs in HTML export (.htm only)")
    p_rep.add_argument("--sort-by", choices=opt_sort_choices, default="result", help="Ranking metric for .xml optimization reports")
    p_rep.add_argument("--top", type=int, default=20, help="Top N passes for .xml optimization reports (default: 20)")
    p_rep.add_argument("--min-trades", type=int, help="Filter minimum trades (.xml only)")
    p_rep.add_argument("--min-pf", type=float, help="Filter minimum Profit Factor (.xml only)")
    p_rep.add_argument("--max-dd-pct", type=float, help="Filter maximum Drawdown %% (.xml only)")
    p_rep.add_argument("--require-positive", action="store_true", help="Keep only Profit > 0 (.xml only)")
    p_rep.add_argument("--pick-pass", type=int, help="Pick a specific Pass # when exporting .set from .xml")
    p_rep.add_argument("--export-best-set", help="Export winning XML pass as UTF-16LE .set file")
    p_rep.add_argument("--save-best-to-tester", help="Save winning XML pass to MQL5\\Profiles\\Tester\\<name>.set")
    p_rep.add_argument("--profile", help="Baseline .ini profile when exporting .set from .xml")
    p_rep.add_argument("--set-file", "--from-set", dest="set_file", help="Baseline .set file when exporting .set from .xml")
    p_rep.add_argument("--from-report", help="Baseline .htm report when exporting .set from .xml")
    p_rep.add_argument("--from-source", help="Baseline .mq5 source when exporting .set from .xml")
    p_rep.add_argument("--source", help="Optional .mq5 source reference")
    p_rep.add_argument("--allow-partial-set", action="store_true", help="Allow exporting a partial .set containing only optimized columns from .xml without a baseline")
    p_rep.set_defaults(func=cmd_report)

    # compare
    p_cmp_runs = sub.add_parser("compare", parents=[common], help="Compare two runs (deals row-by-row ignoring timestamp, metrics delta, inputs diff)")
    p_cmp_runs.add_argument("base", help="Base report name, .htm, .json, or deals_*.txt")
    p_cmp_runs.add_argument("target", help="Target report name, .htm, .json, or deals_*.txt")
    p_cmp_runs.add_argument("--format", choices=["text", "md", "json", "html"], help="Export format (inferred from --out if omitted)")
    p_cmp_runs.add_argument("--out", help="Output file path")
    p_cmp_runs.add_argument("--max-diffs", type=int, default=20, help="Max differing deal rows to include (default: 20)")
    p_cmp_runs.add_argument("--no-fail-exit", action="store_true", help="Return exit code 0 even when runs DIFFER")
    p_cmp_runs.set_defaults(func=cmd_compare)

    # install
    p_inst = sub.add_parser(
        "install",
        parents=[common],
        help="Install skill bundle and /mt5-headless slash command across AI assistants (Antigravity, Claude Code, Codex, Cursor, Windsurf, Cline, Roo Code, Copilot, AGENTS.md)",
    )
    p_inst.add_argument(
        "--target",
        action="append",
        choices=AI_INSTALL_TARGETS,
        default=None,
        help="Target AI assistant(s): auto (default), all, antigravity, claude, codex, cursor, windsurf, cline, roo, copilot, agents (repeatable)",
    )
    p_inst.add_argument("--scope", choices=["global", "project", "both"], default="global", help="Install globally in user home, in current project, or both")
    p_inst.add_argument("--dest-root", help="Custom home directory root (useful for testing or custom portable homes)")
    p_inst.add_argument("--git-url", help="Optional Git repository URL to clone and install from")
    p_inst.add_argument("--dry-run", action="store_true", help="Preview installation paths without copying or writing files")
    p_inst.add_argument("--json", action="store_true", help="Output installation manifest as JSON")
    p_inst.set_defaults(func=cmd_install)

    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
