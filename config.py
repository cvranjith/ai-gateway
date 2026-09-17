"""Runtime configuration for ai-gateway, backed by config.properties.

Format is flat "key=value" lines (# for comments), with dotted keys
namespaced as either:
    <service_id>.<param>   e.g. youtube_summarizer.model_id=gpt-5-luna
    gateway.<param>        a default shared by all services

get_param() checks the per-service key first, falling back to the
gateway-wide key, then the caller's own default. Nothing is cached —
the file is small and rarely read more than once per request, so a
fresh read means config.properties edits (by hand or via the /ui web
UI) take effect immediately, with no gateway restart needed.
"""

from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.properties"


def load_config():
    """Returns the full config as a flat {"service_id.param": "value"} dict."""
    config = {}
    if not CONFIG_PATH.exists():
        return config
    with open(CONFIG_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            config[key.strip()] = value.strip()
    return config


def save_config(config):
    """Overwrites config.properties with the given flat dict, sorted by key."""
    lines = [
        "# ai-gateway configuration - edit here directly, or via the /ui web UI.",
        "# Keys are \"<service_id>.<param>\" for per-service settings, or",
        "# \"gateway.<param>\" for a default shared by all services.",
        "",
    ]
    for key in sorted(config):
        lines.append(f"{key}={config[key]}")
    CONFIG_PATH.write_text("\n".join(lines) + "\n")


def get_param(service_id, param, default=None):
    config = load_config()
    if f"{service_id}.{param}" in config:
        return config[f"{service_id}.{param}"]
    if f"gateway.{param}" in config:
        return config[f"gateway.{param}"]
    return default
