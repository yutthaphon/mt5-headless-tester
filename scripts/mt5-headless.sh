#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="${SCRIPT_DIR}/mt5_headless.py"

_test_py() {
  local cand="$1"
  [[ -z "${cand}" ]] && return 1
  if [[ "${cand}" == *WindowsApps* ]]; then
    return 1
  fi
  "${cand}" -c "import sys" >/dev/null 2>&1
}

resolve_python() {
  local cand
  if [[ -n "${MT5_PYTHON:-}" ]] && _test_py "${MT5_PYTHON}"; then
    printf '%s' "${MT5_PYTHON}"
    return 0
  fi
  for cand in "${SCRIPT_DIR}/../.python/python.exe" ".python/python.exe" "${SCRIPT_DIR}/../.python/bin/python3" ".python/bin/python3"; do
    if [[ -f "${cand}" ]] && _test_py "${cand}"; then
      printf '%s' "${cand}"
      return 0
    fi
  done
  local name path_hit
  for name in python python3; do
    while IFS= read -r path_hit; do
      if _test_py "${path_hit}"; then
        printf '%s' "${path_hit}"
        return 0
      fi
    done < <(which -a "${name}" 2>/dev/null || command -v "${name}" 2>/dev/null || true)
  done
  for cand in /c/Python*/python.exe "${LOCALAPPDATA:-}/Programs/Python"/Python*/python.exe; do
    if [[ -f "${cand}" ]] && _test_py "${cand}"; then
      printf '%s' "${cand}"
      return 0
    fi
  done
  return 1
}

if ! PYTHON_BIN="$(resolve_python)"; then
  echo "ERROR: Valid Python 3 interpreter not found (Microsoft Store WindowsApps stub ignored)." >&2
  echo "Run scripts\\mt5-headless.cmd --bootstrap-python or scripts\\Invoke-MT5Headless.ps1 -BootstrapPython" >&2
  exit 1
fi

# Silently load .env without echoing credentials
load_dotenv_silent() {
  local env_file=""
  if [[ -n "${MT5_ENV_FILE:-}" && -f "${MT5_ENV_FILE}" ]]; then
    env_file="${MT5_ENV_FILE}"
  elif [[ -f ".env" ]]; then
    env_file=".env"
  fi
  [[ -z "${env_file}" ]] && return 0

  while IFS= read -r raw_line || [[ -n "${raw_line}" ]]; do
    raw_line="${raw_line%$'\r'}"
    [[ -z "${raw_line}" || "${raw_line}" =~ ^[[:space:]]*# ]] && continue
    raw_line="${raw_line#export }"
    [[ "${raw_line}" != *"="* ]] && continue
    local key="${raw_line%%=*}"
    local val="${raw_line#*=}"
    key="$(printf '%s' "${key}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    val="$(printf '%s' "${val}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    if [[ ${#val} -ge 2 ]]; then
      if [[ ( "${val:0:1}" == '"' && "${val: -1}" == '"' ) || ( "${val:0:1}" == "'" && "${val: -1}" == "'" ) ]]; then
        val="${val:1:${#val}-2}"
      fi
    fi
    if [[ -n "${key}" && -z "${!key:-}" && -n "${val}" ]]; then
      export "${key}=${val}"
    fi
  done < "${env_file}"
}

load_dotenv_silent

if [[ $# -eq 0 ]]; then
  exec "${PYTHON_BIN}" "${PY_SCRIPT}" --help
fi

if [[ "${1}" == "--launch-minimized" ]]; then
  shift
  INI_PATH="${1:-}"
  if [[ -z "${INI_PATH}" || ! -f "${INI_PATH}" ]]; then
    echo "ERROR: Specify an existing <ini_path> after --launch-minimized" >&2
    exit 2
  fi
  shift

  PORTABLE_FLAG=""
  TERM_EXE="${MT5_TERMINAL_EXE:-}"
  while [[ $# -gt 0 ]]; do
    case "${1}" in
      --portable)
        PORTABLE_FLAG="/portable"
        shift
        ;;
      --terminal-exe)
        TERM_EXE="${2:-}"
        shift 2
        ;;
      *)
        shift
        ;;
    esac
  done

  if [[ -z "${TERM_EXE}" ]]; then
    if [[ -n "${MT5_TERMINAL_DIR:-}" ]]; then
      TERM_EXE="${MT5_TERMINAL_DIR}/terminal64.exe"
    elif [[ -n "${PORTABLE_FLAG}" && -f ".mt5-portable/terminal64.exe" ]]; then
      TERM_EXE="$(pwd)/.mt5-portable/terminal64.exe"
    else
      TERM_EXE='C:\Program Files\MetaTrader 5\terminal64.exe'
    fi
  fi

  if [[ -z "${PORTABLE_FLAG}" ]] && command -v tasklist.exe >/dev/null 2>&1; then
    if tasklist.exe /FI "IMAGENAME eq terminal64.exe" /NH 2>/dev/null | grep -qi "terminal64.exe"; then
      echo "ERROR: terminal64.exe is running. Close MT5 gracefully via UI first; NEVER kill terminal64.exe." >&2
      exit 1
    fi
  fi

  ABS_INI="$(cd "$(dirname "${INI_PATH}")" && pwd)/$(basename "${INI_PATH}")"
  if command -v cygpath >/dev/null 2>&1; then
    ABS_INI="$(cygpath -w "${ABS_INI}")"
    TERM_EXE="$(cygpath -w "${TERM_EXE}")"
  fi

  cleanup_ini() {
    "${PYTHON_BIN}" "${PY_SCRIPT}" scrub-ini "${ABS_INI}" >/dev/null 2>&1 || true
  }
  trap cleanup_ini EXIT

  set +e
  if command -v cmd.exe >/dev/null 2>&1; then
    if [[ -n "${PORTABLE_FLAG}" ]]; then
      cmd.exe /c start "" /MIN /WAIT "${TERM_EXE}" /portable "/config:${ABS_INI}"
    else
      cmd.exe /c start "" /MIN /WAIT "${TERM_EXE}" "/config:${ABS_INI}"
    fi
  else
    if [[ -n "${PORTABLE_FLAG}" ]]; then
      "${TERM_EXE}" /portable "/config:${ABS_INI}"
    else
      "${TERM_EXE}" "/config:${ABS_INI}"
    fi
  fi
  MT5_EXIT=$?
  set -e
  echo "Exit     : ${MT5_EXIT}  (not a verdict; inspect report and logs)"
  exit "${MT5_EXIT}"
fi

exec "${PYTHON_BIN}" "${PY_SCRIPT}" "$@"
