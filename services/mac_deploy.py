"""service_id: "mac_deploy"

params:
    action (str, required) - "wifi_status" | "start_deploy" | "deploy_status"

result:
    wifi_status   -> { "ssid": "..." | null, "ip": "192.168.x.x" | null, "proceed_ok": true | false }
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
it's no longer "running" - while running, "log_tail" is updated live
(the last ~60 lines seen so far), not just filled in once at the end,
so a polling UI can show real progress rather than a silent spinner
for however many minutes the build takes. A module-level lock makes a
second "start_deploy" while one's already running a clean 409 instead
of two builds racing over the same directory.

"wifi_status" is unrelated to the deploy work itself - the /invoke call
reaches this gateway over the internet regardless of Wi-Fi (through
ai-router / this gateway's Tailscale Funnel), same as every other
service. `proceed_ok` is the real, definitive answer to "will a deploy
actually be able to reach a device right now": `pairingState ==
"paired"` alone is NOT enough to answer that - confirmed by hand, a
device stays "paired" even when its actual trusted-connectivity
session (the thing any real build/install needs) is currently down,
e.g. because it's locked or has just been idle a while. `devicectl
device info apps` fails fast (well under a second) and precisely in
exactly that situation, so it's used as a cheap, real readiness probe
on top of the pairing check rather than trusting the pairing record
alone. `ssid`/`ip` stay purely informational, for telling the user
which network to switch to when `proceed_ok` is false for that reason
specifically (as opposed to the device just being asleep).
"""

import collections
import json
import subprocess
import tempfile
import threading
import time

from .errors import ServiceError

SERVICE_ID = "mac_deploy"
INSTALL_SCRIPT = "/Users/ranjithcv/Documents/code/claude/yt-run/install_to_device.sh"
DEPLOY_TIMEOUT_SECONDS = 900
MAX_LOG_LINES = 60

_lock = threading.Lock()
_state = {"status": "idle", "log_tail": None}


def _wifi_device():
    # The Wi-Fi device name (en0/en1/...) isn't guaranteed to stay put
    # across macOS/hardware changes, so it's looked up by hardware port
    # name rather than hardcoded.
    ports = subprocess.run(
        ["networksetup", "-listallhardwareports"], capture_output=True, text=True, timeout=10
    ).stdout
    lines = ports.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "Hardware Port: Wi-Fi" and i + 1 < len(lines):
            device_line = lines[i + 1].strip()
            if device_line.startswith("Device:"):
                return device_line.split(":", 1)[1].strip()
    return None


def _wifi_status():
    device = _wifi_device()
    ssid = None
    ip = None
    if device:
        # `networksetup -getairportnetwork` needs Location Services
        # authorization that a headless process like this one doesn't
        # have - confirmed by hand, it reports "not associated" even
        # while genuinely connected. `ipconfig getsummary` reads the
        # same information without that restriction.
        summary = subprocess.run(
            ["ipconfig", "getsummary", device], capture_output=True, text=True, timeout=10
        ).stdout
        for line in summary.splitlines():
            if "SSID" in line and ":" in line:
                ssid = line.split(":", 1)[1].strip()
                break

        ip = subprocess.run(
            ["ipconfig", "getifaddr", device], capture_output=True, text=True, timeout=10
        ).stdout.strip() or None

    return {"ssid": ssid, "ip": ip, "proceed_ok": _paired_device_available()}


def _first_paired_device_udid():
    # The exact same lookup install_to_device.sh itself does to find a
    # device to build for.
    try:
        with tempfile.NamedTemporaryFile(suffix=".json") as tmp:
            subprocess.run(
                ["xcrun", "devicectl", "list", "devices", "--json-output", tmp.name,
                 "--omit-deprecated-fields-in-json"],
                capture_output=True, timeout=30, check=True,
            )
            with open(tmp.name) as f:
                data = json.load(f)
        for d in data.get("result", {}).get("devices", []):
            if d.get("properties", {}).get("connection", {}).get("pairingState") == "paired":
                return d.get("properties", {}).get("hardware", {}).get("udid")
    except Exception:
        pass
    return None


def _paired_device_available():
    udid = _first_paired_device_udid()
    if not udid:
        return False
    # A real, fast readiness probe on top of the pairing check - see
    # the module docstring for why pairingState alone isn't enough.
    # Any successful devicectl call against the device implies the
    # trusted-connectivity session a real build/install also needs is
    # currently up.
    try:
        result = subprocess.run(
            ["xcrun", "devicectl", "device", "info", "apps", "--device", udid, "--timeout", "8"],
            capture_output=True, timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False


def _run_deploy():
    lines = collections.deque(maxlen=MAX_LOG_LINES)
    try:
        proc = subprocess.Popen(
            ["/bin/bash", INSTALL_SCRIPT],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        deadline = time.time() + DEPLOY_TIMEOUT_SECONDS
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))
            _state["log_tail"] = "\n".join(lines)
            if time.time() > deadline:
                proc.kill()
                lines.append(f"[timed out after {DEPLOY_TIMEOUT_SECONDS}s]")
                _state["status"] = "failed"
                _state["log_tail"] = "\n".join(lines)
                return
        returncode = proc.wait()
        _state["status"] = "success" if returncode == 0 else "failed"
        _state["log_tail"] = "\n".join(lines)
    except Exception as e:
        lines.append(f"[error: {e}]")
        _state["status"] = "failed"
        _state["log_tail"] = "\n".join(lines)
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
        return _wifi_status()
    if action == "start_deploy":
        return _start_deploy()
    if action == "deploy_status":
        return dict(_state)
    raise ServiceError("invalid 'action' - must be 'wifi_status', 'start_deploy', or 'deploy_status'", 400)
