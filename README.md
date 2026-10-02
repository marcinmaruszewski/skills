# Skills

Agent skills for **Claude Code**, **Codex CLI** and **Antigravity CLI (agy)**.

## Install

```bash
npx skills add marcinmaruszewski/skills
```

The installer asks which skills to take and which agents to install them on. Add `--skill usage-guard` to pick one skill up front, `-g` to install globally, or `-a claude-code -a codex -a antigravity` to choose the agents.

## Available skills

| Skill | What it does |
|---|---|
| [`usage-guard`](#usage-guard) | Shows subscription usage limits and keeps long tasks from dying on them |

## usage-guard

Keeps long tasks from dying on subscription usage limits. Ask your agent to keep `/usage-guard` in mind while it works. The agent checks your 5-hour and 7-day usage at natural breaks in the work. When a limit reaches 95%, it pauses in the same session, sleeps until the window resets, and then carries on where it stopped. You don't need a new session, a handoff file or a manual restart.

### Usage

**Check what's left:**

```
/usage-guard
```

```
Claude Code
  5h                       [####................]  21.0%  reset Fri 02 Oct 16:00 (in 1h 58m)
  7d                       [#########...........]  47.0%  reset Mon 05 Oct 09:00 (in 2d 19h)
```

**Guard a task:**

```
Refactor the payment module and update the tests. Keep /usage-guard in mind while you work.
```

The agent checks usage before it starts and at natural breaks in its work (a finished plan item, a test or build run, a commit):

| Usage | What the agent does |
|---|---|
| below 95% | keeps working |
| 95% or more, reset within 6 h | pauses, sleeps in chunks of up to 9 minutes, re-checks, and resumes after the reset |
| 95% or more, reset more than 6 h away (or already waited 6 h) | stops and reports when the limit resets |
| check failed | keeps working, mentions once that usage isn't guarded, and retries at the next break |

The agent guards only the CLI it is running in. In Antigravity, limits apply per model group (Gemini, or Claude and GPT), and the agent watches the group it is using.

**Run the script directly** (no agent needed):

```bash
python3 skills/usage-guard/scripts/usage_guard.py            # all CLIs, human-readable
python3 skills/usage-guard/scripts/usage_guard.py codex --json
python3 skills/usage-guard/scripts/usage_guard.py claude --gate   # continue / pause / stop, for scripts
python3 skills/usage-guard/scripts/usage_guard.py --help
```

### Requirements

- Python 3.9+, standard library only
- macOS or Linux (Windows is untested)
- A subscription login (not an API key) in the CLI you want to guard

### Sandboxes and non-interactive runs

**Network access.** Agent sandboxes block network access by default. In an interactive session the agent asks to run the check outside the sandbox. For unattended runs, allow these domains up front:

- `api.anthropic.com` (Claude Code)
- `chatgpt.com` (Codex)
- `daily-cloudcode-pa.googleapis.com` and `cloudcode-pa.googleapis.com` (Antigravity)

**Long waits.** Waiting for a reset can take hours, and each 9-minute sleep uses one agent turn. Raise turn limits accordingly (for example `claude -p --max-turns`), and make sure CI job timeouts allow for the wait. Otherwise the run is cut off while it waits.

### Security

The script is read-only and short. Read it before you install.

**What it reads:**

| CLI | Reads |
|---|---|
| Claude Code | the OAuth token from the macOS Keychain (`Claude Code-credentials`) or from `~/.claude/.credentials.json` |
| Codex | `~/.codex/auth.json` |
| Antigravity | the OS keyring entry `gemini`/`antigravity`, or `~/.gemini/antigravity-cli/antigravity-oauth-token` |

**Where it connects:** only the usage endpoints of each vendor, which the CLIs use for their own `/usage` or `/status` screens:

- `api.anthropic.com/api/oauth/usage`
- `chatgpt.com/backend-api/wham/usage`
- `*cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary`

**What it never does:**

- print, log or write tokens
- refresh or modify credentials
- send data anywhere else

**What it stores:** a 5-minute cache in a private temp directory (`$TMPDIR/usage-guard-<uid>/`, mode 0700). The cache contains only percentages, reset times and the start time of a pause, with no tokens, account IDs or e-mail addresses.

**What the agent is told:** never to read credential files itself, so tokens never enter the model's context.

### Caveats

- The usage endpoints are undocumented and may change without notice.
- The Antigravity token is not refreshed by the script. If it has expired, the check returns `HTTP 401` until agy runs again and refreshes it.
- A 95% threshold leaves headroom for the waiting turns themselves. A single very large step can still overshoot it between checks.

## License

[MIT](LICENSE)
