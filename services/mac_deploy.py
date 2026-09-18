"""service_id: "mac_deploy"

params:
    action (str, required) - "wifi_status" | "start_deploy" | "deploy_status"

result:
    wifi_status   -> { "ssid": "..." | null }
    start_deploy  -> { "status": "running" }
    deploy_status -> { "status": "idle" | "running" | "success" | "failed", "log_tail": "..." | null }

Lets the phone trigger a real rebuild+reinstall of the YTRun app onto
whichever device is currently paired with this Mac, without touching
it directly - see install_to_device.sh (in the yt-run repo itself,
unmodified and unparameterized - nothing from `params` ever reaches
the shell) for the actual git-pull/build/install steps this runs.

Split into three actions instead of one blocking call because a real
deploy is a clean build (several minutes) and holding one HTTP request
open that long through Cloudflare's edge (ai-router sits in front of
this for the phone's normal calls) isn't something to rely on. Instead
"start_deploy" kicks the work off in a background thread and returns
immediately; the caller polls "deploy_status" on its own timer until
it's no longer "running". A module-level lock makes a second
"start_deploy" while one's already running a clean 409 instead of two
builds racing over the same directory.

"wifi_status" is unrelated to the deploy work itself - the /invoke
call reaches this gateway over the internet regardless of Wi-Fi
(through ai-router / this gateway's Tailscale Funnel), same as every
other service. Only the final on-device install step needs the phone
and this Mac on the same LAN to find each other over Bonjour, so this
is purely informational: enough for the app to show which network the
Mac is currently on, so the user can switch their phone to match first.
"""

import subprocess
import threading

from .errors import ServiceError

SERVICE_ID = "mac_deploy"
INSTALL_SCRIPT = "/Users/ranjithcv/Documents/code/claude/yt-run/install_to_device.sh"
DEPLOY_TIMEOUT_SECONDS = 900

_lock = threading.Lock()
_state = {"status": "idle", "log_tail": None}


def _wifi_ssid():
    # The Wi-Fi device name (en0/en1/...) isn't guaranteed to stay put
    # across macOS/hardware changes, so it's looked up by hardware port
    # name rather than hardcoded.
    ports = subprocess.run(
        ["networksetup", "-listallhardwareports"], capture_output=True, text=True, timeout=10
    ).stdout
    lines = ports.splitlines()
    device = None
    for i, line in enumerate(lines):
        if line.strip() == "Hardware Port: Wi-Fi" and i + 1 < len(lines):
            device_line = lines[i + 1].strip()
            if device_line.startswith("Device:"):
                device = device_line.split(":", 1)[1].strip()
            break
    if not device:
        return None

    # `networksetup -getairportnetwork` needs Location Services
    # authorization that a headless process like this one doesn't have
    # - confirmed by hand, it reports "not associated" even while
    # genuinely connected. `ipconfig getsummary` reads the same
    # information without that restriction.
    summary = subprocess.run(
        ["ipconfig", "getsummary", device], capture_output=True, text=True, timeout=10
    ).stdout
    for line in summary.splitlines():
        if "SSID" in line and ":" in line:
            return line.split(":", 1)[1].strip()
    return None


def _run_deploy():
    try:
        result = subprocess.run(
            ["/bin/bash", INSTALL_SCRIPT],
            capture_output=True, text=True, timeout=DEPLOY_TIMEOUT_SECONDS,
        )
        log_tail = (result.stdout + result.stderr)[-4000:]
        _state["status"] = "success" if result.returncode == 0 else "failed"
        _state["log_tail"] = log_tail
    except subprocess.TimeoutExpired:
        _state["status"] = "failed"
        _state["log_tail"] = f"timed out after {DEPLOY_TIMEOUT_SECONDS}s"
    except Exception as e:
        _state["status"] = "failed"
        _state["log_tail"] = str(e)
    finally:
        _lock.release()


def _start_deploy():
    if not _lock.acquire(blocking=False):
        raise ServiceError("a deploy is already in progress", 409)
    _state["status"] = "running"
    _state["log_tail"] = None
    threading.Thread(target=_run_deploy, daemon=True).start()
    return {"status": "running"}


def handle(params):
    action = (params.get("action") or "").strip().lower()
    if action == "wifi_status":
        return {"ssid": _wifi_ssid()}
    if action == "start_deploy":
        return _start_deploy()
    if action == "deploy_status":
        return dict(_state)
    raise ServiceError("invalid 'action' - must be 'wifi_status', 'start_deploy', or 'deploy_status'", 400)
