#!/usr/bin/env python3
"""Show subscription usage limits (5-hour and 7-day windows) for AI coding CLIs.

Supports Claude Code, Codex CLI and Antigravity CLI (agy). Runs non-interactively.

Exit status: 0 on success, 1 if any provider failed.
With --gate: 0 continue, 1 error, 3 pause, 4 stop.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOME = Path.home()
IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"
HTTP_TIMEOUT = 15
CMD_TIMEOUT = 10
CACHE_TTL = 300  # seconds; the usage endpoints rate-limit more frequent polling
WINDOW_ORDER = ("5h", "7d")
BAR_WIDTH = 20

# --gate policy
THRESHOLD = 95.0  # percent; headroom for the waiting turns themselves
MAX_WAIT_MINUTES = 360  # longer waits stop the task instead of pausing it
MAX_SLEEP = 540  # seconds; fits a 10-minute shell timeout
PAUSE_GAP = 1800  # seconds between pause checks after which a new pause begins
EXIT_CONTINUE, EXIT_ERROR, EXIT_PAUSE, EXIT_STOP = 0, 1, 3, 4


class ProviderError(Exception):
    """Short, user-facing failure code such as 'HTTP 403' or 'no credentials'."""


@dataclass(frozen=True)
class Window:
    window: str  # one of WINDOW_ORDER
    used_percent: float
    resets_at: datetime | None
    scope: str = ""  # model or model group; empty = whole account

    @property
    def label(self) -> str:
        return f"{self.window} {self.scope}".strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "used_percent": self.used_percent,
            "resets_at": self.resets_at.isoformat() if self.resets_at else None,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Window:
        return cls(data["window"], float(data["used_percent"]), parse_iso(data["resets_at"]), data["scope"])


@dataclass(frozen=True)
class Result:
    windows: list[Window]
    error: str | None = None
    stale: bool = False  # served from cache because the API rate-limited us


# ---------- helpers ----------


def http_json(url: str, headers: dict[str, str], body: dict | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        raise ProviderError("network error") from None
    except json.JSONDecodeError:
        raise ProviderError("invalid response") from None


def run(cmd: list[str]) -> str | None:
    """Return stripped stdout of a successful command, otherwise None."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=CMD_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def read_text(path: Path | str | None) -> str | None:
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def cli_version(cmd: str, default: str) -> str:
    output = run([cmd, "--version"]) or ""
    return next((word for word in output.split() if word[:1].isdigit()), default)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    # fromisoformat() before 3.11 rejects 'Z' and more than 6 fractional digits.
    value = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
    return datetime.fromisoformat(value)


def parse_epoch(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    seconds = float(value)
    if seconds >= 1e12:  # milliseconds
        seconds /= 1000
    return datetime.fromtimestamp(seconds, timezone.utc)


def to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def clamp_percent(value: float) -> float:
    return max(0.0, min(100.0, value))


# ---------- Claude Code ----------


def claude() -> list[Window]:
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not token:
        raw = (
            run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"])
            if IS_MACOS
            else None
        )
        config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude"))
        raw = raw or read_text(config_dir / ".credentials.json")
        if raw:
            creds = json.loads(raw)
            token = creds.get("claudeAiOauth", creds).get("accessToken")
    if not token:
        raise ProviderError("no credentials")

    data = http_json(
        "https://api.anthropic.com/api/oauth/usage",
        {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": f"claude-code/{cli_version('claude', '2.0.0')}",
            "Content-Type": "application/json",
        },
    )
    fields = {
        "five_hour": ("5h", ""),
        "seven_day": ("7d", ""),
        "seven_day_opus": ("7d", "Opus"),
        "seven_day_sonnet": ("7d", "Sonnet"),
    }
    windows = []
    for key, (window, scope) in fields.items():
        period = data.get(key)
        if period:
            windows.append(
                Window(window, period.get("utilization") or 0.0, parse_iso(period.get("resets_at")), scope)
            )
    return windows


# ---------- Codex ----------


def _codex_windows(rate_limit: dict | None, scope: str = "") -> list[Window]:
    windows = []
    for period in ((rate_limit or {}).get("primary_window"), (rate_limit or {}).get("secondary_window")):
        if not period:
            continue
        length = period.get("limit_window_seconds") or 0
        # Classify by duration, not position: the API reorders windows over time.
        window = "5h" if 0 < length <= 6 * 3600 else "7d"
        reset = parse_epoch(period.get("reset_at"))
        if reset is None and period.get("reset_after_seconds") is not None:
            reset = parse_epoch(time.time() + period["reset_after_seconds"])
        windows.append(Window(window, period.get("used_percent") or 0.0, reset, scope))
    return windows


def codex() -> list[Window]:
    raw = read_text(Path(os.environ.get("CODEX_HOME", HOME / ".codex")) / "auth.json")
    tokens = (json.loads(raw).get("tokens") or {}) if raw else {}
    if not tokens.get("access_token"):
        raise ProviderError("no credentials")

    headers = {
        "Authorization": f"Bearer {tokens['access_token']}",
        "User-Agent": f"codex_cli_rs/{cli_version('codex', '0.0.0')}",
        "Accept": "application/json",
    }
    if tokens.get("account_id"):
        headers["ChatGPT-Account-Id"] = tokens["account_id"]
    data = http_json("https://chatgpt.com/backend-api/wham/usage", headers)

    windows = _codex_windows(data.get("rate_limit"))
    for extra in data.get("additional_rate_limits") or []:
        scope = extra.get("limit_name") or extra.get("metered_feature") or "?"
        windows += _codex_windows(extra.get("rate_limit"), scope)
    return windows


# ---------- Antigravity (agy) ----------

AGY_HOSTS = ("daily-cloudcode-pa.googleapis.com", "cloudcode-pa.googleapis.com")
AGY_KEYRING_PREFIX = "go-keyring-base64:"


def _agy_token() -> str:
    """Read agy's stored access token. It is never refreshed here; agy refreshes it on startup."""
    raw = None
    if IS_MACOS:
        raw = run(["security", "find-generic-password", "-s", "gemini", "-a", "antigravity", "-w"])
    elif IS_LINUX:
        raw = run(["secret-tool", "lookup", "service", "gemini", "account", "antigravity"])
    for path in (
        os.environ.get("AGY_OAUTH_TOKEN_FILE"),
        HOME / ".gemini/antigravity-cli/antigravity-oauth-token",
        HOME / ".gemini/jetski-standalone-oauth-token",
    ):
        raw = raw or read_text(path)
    if not raw:
        raise ProviderError("no credentials")
    if raw.startswith(AGY_KEYRING_PREFIX):
        raw = base64.b64decode(raw[len(AGY_KEYRING_PREFIX) :]).decode()

    creds = json.loads(raw)
    token = creds.get("token", creds).get("access_token")
    if not token:
        raise ProviderError("no credentials")
    return token


def _agy_quota(headers: dict[str, str]) -> dict[str, Any]:
    last_error = ProviderError("no data")
    for host in AGY_HOSTS:
        try:
            load = http_json(
                f"https://{host}/v1internal:loadCodeAssist", headers, {"metadata": {"ideType": "ANTIGRAVITY"}}
            )
            project = load.get("cloudaicompanionProject")
            if not project:
                last_error = ProviderError("no data")
                continue
            return http_json(
                f"https://{host}/v1internal:retrieveUserQuotaSummary", headers, {"project": project}
            )
        except ProviderError as exc:
            if str(exc) in ("HTTP 401", "HTTP 403"):  # account-level, same on every host
                raise
            last_error = exc
    raise last_error


def agy() -> list[Window]:
    headers = {
        "Authorization": f"Bearer {_agy_token()}",
        "Content-Type": "application/json",
        # The API identifies the product by User-Agent; it must contain "antigravity".
        "User-Agent": f"antigravity-cli/{cli_version('agy', '0.0.0')} usage-guard",
    }
    data = _agy_quota(headers)

    windows = []
    for group in data.get("groups") or []:
        scope = group.get("displayName") or "Models"
        for bucket in group.get("buckets") or []:
            kind = (bucket.get("window") or "").lower()
            name = (bucket.get("displayName") or "").lower()
            window = {"5h": "5h", "weekly": "7d"}.get(kind) or (
                "5h" if "five" in name or "5" in name else "7d" if "week" in name else None
            )
            if window is None:
                continue
            remaining = to_float(bucket.get("remainingFraction"))
            if remaining is None:
                # proto3 JSON omits 0.0, so a missing fraction with a reset time means exhausted.
                remaining = 0.0 if bucket.get("resetTime") else 1.0
            used = clamp_percent((1 - remaining) * 100)
            windows.append(Window(window, used, parse_iso(bucket.get("resetTime")), scope))
    return windows


# ---------- cache ----------
# Holds only normalized windows (percentages and reset times), never tokens or account data.
# Lives in the temp dir because agent sandboxes allow writes there; failures are ignored.


def _cache_dir() -> Path | None:
    uid = str(os.getuid()) if hasattr(os, "getuid") else getpass.getuser()
    path = Path(tempfile.gettempdir()) / f"usage-guard-{uid}"
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        info = os.lstat(path)
    except OSError:
        return None
    # Refuse a directory that another user could have planted or can write to.
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        return None
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        return None
    return path


def cache_load(key: str) -> tuple[float, list[Window]] | None:
    directory = _cache_dir()
    raw = read_text(directory / f"{key}.json") if directory else None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        windows = [Window.from_dict(w) for w in data["windows"]]
        return float(data["fetched_at"]), windows
    except (KeyError, TypeError, ValueError):
        return None


def _cache_write(name: str, data: dict[str, Any]) -> None:
    directory = _cache_dir()
    if not directory:
        return
    try:
        with tempfile.NamedTemporaryFile("w", dir=directory, delete=False, encoding="utf-8") as tmp:
            tmp.write(json.dumps(data))
        os.replace(tmp.name, directory / name)
    except OSError:
        pass


def cache_save(key: str, windows: list[Window]) -> None:
    _cache_write(f"{key}.json", {"fetched_at": time.time(), "windows": [w.to_dict() for w in windows]})


def pause_minutes(key: str) -> float | None:
    """Record a pause check and return the minutes since the pause began (None without a cache)."""
    directory = _cache_dir()
    if not directory:
        return None
    now = time.time()
    started = now
    raw = read_text(directory / f"{key}-pause.json")
    try:
        data = json.loads(raw) if raw else {}
        if now - float(data["last"]) < PAUSE_GAP:
            started = float(data["started"])
    except (KeyError, TypeError, ValueError):
        pass
    _cache_write(f"{key}-pause.json", {"started": started, "last": now})
    return (now - started) / 60


def pause_clear(key: str) -> None:
    directory = _cache_dir()
    if directory:
        try:
            (directory / f"{key}-pause.json").unlink()
        except OSError:
            pass


# ---------- providers ----------


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    fetch: Callable[[], list[Window]]
    grouped: bool = False  # limits exist per model group only, never account-wide


PROVIDERS = (
    Provider("claude", "Claude Code", claude),
    Provider("codex", "Codex", codex),
    Provider("agy", "Antigravity", agy, grouped=True),
)


def _sorted(windows: list[Window]) -> list[Window]:
    return sorted(windows, key=lambda w: (WINDOW_ORDER.index(w.window), w.scope))


def collect(provider: Provider) -> Result:
    now = datetime.now(timezone.utc)
    cached = cache_load(provider.key)
    if cached:
        fetched_at, windows = cached
        # A window that has already reset makes the cached numbers wrong, so refetch.
        reset_passed = any(w.resets_at and w.resets_at <= now for w in windows)
        if time.time() - fetched_at < CACHE_TTL and not reset_passed:
            return Result(_sorted(windows))

    try:
        windows = provider.fetch()
    except ProviderError as exc:
        if str(exc) == "HTTP 429" and cached:
            return Result(_sorted(cached[1]), stale=True)
        return Result([], error=str(exc))
    except (KeyError, TypeError, ValueError, AttributeError):
        return Result([], error="invalid response")
    if not windows:
        return Result([], error="no data")
    cache_save(provider.key, windows)
    return Result(_sorted(windows))


# ---------- output ----------


def time_left(moment: datetime) -> str:
    seconds = int((moment - datetime.now(timezone.utc)).total_seconds())
    if seconds <= 0:
        return "now"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def render_text(name: str, result: Result) -> None:
    print(f"{name} (cached)" if result.stale else name)
    if result.error:
        print(f"  error: {result.error}")
        return
    for w in result.windows:
        filled = round(clamp_percent(w.used_percent) / (100 / BAR_WIDTH))
        bar = "#" * filled + "." * (BAR_WIDTH - filled)
        reset = ""
        if w.resets_at:
            reset = f"reset {w.resets_at.astimezone():%a %d %b %H:%M} (in {time_left(w.resets_at)})"
        print(f"  {w.label:<24} [{bar}] {w.used_percent:5.1f}%  {reset}".rstrip())


def worst_per_window(windows: list[Window]) -> list[Window]:
    """One window per length, keeping the most exhausted limit."""
    worst: dict[str, Window] = {}
    for w in windows:
        if w.window not in worst or w.used_percent > worst[w.window].used_percent:
            worst[w.window] = w
    return [worst[key] for key in WINDOW_ORDER if key in worst]


def minutes_until(moment: datetime | None, now: datetime) -> int | None:
    return max(0, int((moment - now).total_seconds() // 60)) if moment else None


def short_iso(moment: datetime | None) -> str | None:
    return moment.astimezone().isoformat(timespec="minutes") if moment else None


def windows_json(windows: list[Window], now: datetime) -> dict[str, Any]:
    out = {}
    for w in worst_per_window(windows):
        used = round(float(w.used_percent), 1)
        out[w.window] = {
            "used_percent": used,
            "remaining_percent": round(100 - used, 1),
            "resets_at": short_iso(w.resets_at),
            "resets_in_minutes": minutes_until(w.resets_at, now),
        }
    return out


def scope_key(scope: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", scope.lower()).strip("_") or "models"


def provider_json(provider: Provider, result: Result, now: datetime) -> dict[str, Any]:
    # Only normalized windows: no account IDs, e-mails or tokens.
    if result.error:
        return {"error": result.error}
    out: dict[str, Any] = {"stale": True} if result.stale else {}
    if not provider.grouped:
        out.update(windows_json(result.windows, now))
        return out
    by_scope: dict[str, list[Window]] = {}
    for w in result.windows:
        by_scope.setdefault(scope_key(w.scope), []).append(w)
    out.update({scope: windows_json(ws, now) for scope, ws in by_scope.items()})
    return out


def gate(provider: Provider, result: Result, group: str | None, now: datetime) -> tuple[int, str]:
    """Turn one provider's usage into a single decision line for an agent guarding a task."""
    if result.error:
        return EXIT_ERROR, f"error: {result.error}"
    windows = result.windows
    if group:
        windows = [w for w in windows if scope_key(w.scope) == group]
        if not windows:
            known = ", ".join(sorted({scope_key(w.scope) for w in result.windows}))
            return EXIT_ERROR, f"error: unknown group {group} (known: {known})"

    exhausted = [w for w in windows if w.used_percent >= THRESHOLD]
    if not exhausted:
        pause_clear(provider.key)
        summary = ", ".join(f"{w.label} {w.used_percent:.1f}% used" for w in worst_per_window(windows))
        return EXIT_CONTINUE, f"continue: {summary}"

    # The window that resets last decides how long the pause lasts; unknown reset times count as latest.
    last = max(exhausted, key=lambda w: (w.resets_at is None, w.resets_at or now))
    wait = minutes_until(last.resets_at, now)
    reset = "reset time unknown"
    if last.resets_at:
        reset = f"resets {short_iso(last.resets_at)} (in {time_left(last.resets_at)})"
    detail = f"{last.label} {last.used_percent:.1f}% used, {reset}"

    if wait is not None and wait > MAX_WAIT_MINUTES:
        pause_clear(provider.key)
        return EXIT_STOP, f"stop: {detail}"
    waited = pause_minutes(provider.key)
    if waited is not None and waited > MAX_WAIT_MINUTES:
        pause_clear(provider.key)
        return EXIT_STOP, f"stop: {detail}; already waited {MAX_WAIT_MINUTES // 60} h"
    sleep = MAX_SLEEP if wait is None else min(MAX_SLEEP, wait * 60 + 60)
    return EXIT_PAUSE, f"pause: {detail}; sleep {sleep}"


HELP_EPILOG = """\
how data is fetched (read-only, tokens are never written or printed):

  claude  Reads the OAuth token Claude Code saved at login (macOS Keychain or
          ~/.claude/.credentials.json). Calls api.anthropic.com/api/oauth/usage,
          the same endpoint behind Claude Code's /usage command.

  codex   Reads the ChatGPT token from ~/.codex/auth.json. Calls
          chatgpt.com/backend-api/wham/usage, which Codex uses for /status.

  agy     Reads the token agy stored in the OS keyring (or ~/.gemini/... on
          headless Linux). Calls Google's Cloud Code quota API, the same source
          as agy's /usage panel. The token is not refreshed; agy does that itself.

--gate (one provider) prints one line for an agent guarding a task:

  continue  every window is below 95%                           exit 0
  pause     a window is at 95%+ and resets within 6 h;          exit 3
            the line ends with "sleep N": sleep N seconds, then check again
  stop      the reset is more than 6 h away, or the pause       exit 4
            has already lasted 6 h
  error     the check failed                                    exit 1

Results are cached for 5 minutes in a private temp directory (percentages and
reset times only, plus pause timestamps for --gate). On HTTP 429 the last
cached result is returned as stale. All endpoints are undocumented and may
change without notice."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "provider",
        nargs="?",
        default="all",
        choices=[p.key for p in PROVIDERS] + ["all"],
        help="which CLI to check (default: all)",
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="machine-readable output")
    output.add_argument("--gate", action="store_true", help="one decision line for a guarded task")
    parser.add_argument("--group", help="with --gate on agy: the model group to guard, e.g. gemini_models")
    args = parser.parse_args(argv)
    if args.gate and args.provider == "all":
        parser.error("--gate needs a single provider")
    grouped = {p.key for p in PROVIDERS if p.grouped}
    if args.group and not (args.gate and args.provider in grouped):
        parser.error(f"--group needs --gate and a provider with model groups ({', '.join(sorted(grouped))})")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    selected = [p for p in PROVIDERS if args.provider in ("all", p.key)]
    results = [(p, collect(p)) for p in selected]

    if args.gate:
        provider, result = results[0]
        code, line = gate(provider, result, args.group, datetime.now(timezone.utc))
        print(line)
        return code
    if args.json:
        now = datetime.now(timezone.utc)
        out: dict[str, Any] = {"checked_at": now.astimezone().isoformat(timespec="seconds")}
        out.update({p.key: provider_json(p, r, now) for p, r in results})
        print(json.dumps(out, indent=1, ensure_ascii=False))
    else:
        for index, (p, r) in enumerate(results):
            if index:
                print()
            render_text(p.name, r)

    return 1 if any(r.error for _, r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
