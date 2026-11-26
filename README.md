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
| [`critical-update`](#critical-update) | Patches a known vulnerability in one project or a group, from the advisory to a smoke-tested deploy |
| [`smoke-test`](#smoke-test) | Checks a deployed site's home page and random sitemap URLs for HTTP 200 |

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

## critical-update

Patches a known vulnerability in your projects. You give it the advisory and the project, and it works out how to update it as quickly as possible. It makes the change, then helps you commit, open the merge request, deploy and check the site. Nothing is pushed, merged or deployed without your approval.

### Usage

**Patch one project:**

```
/critical-update https://nvd.nist.gov/vuln/detail/CVE-2026-12345 ~/projects/shop
```

**Patch a group of projects** (a directory of separate repositories):

```
/critical-update CVE-2026-12345 ~/clients
```

The agent reads the advisory once and then opens one conversation per repository. It uses the agent's own thread tools when it has them, and otherwise gives you ready-to-paste prompts. Each conversation works on one project only, so parallel updates don't get in each other's way.

**What to give it:**

| | |
|---|---|
| required | the vulnerability: an advisory URL, an id (CVE, GHSA, a vendor's own advisory id, …) or pasted text |
| required | the project path, or the directory that holds the projects |
| optional | upgrade instructions (URL or text), the announced release date of the fix, a skill that writes or opens merge requests (e.g. `/pr`) |

When the advisory or the project is missing, the agent says what it needs and stops. It doesn't guess.

### What the agent does

| Step | What happens | Asks first |
|---|---|---|
| Inspect | A read-only subagent checks whether the project is affected and finds the fastest update command, the git and commit conventions, the post-update steps (migrations, config export, overwritten files) and the checks to run | |
| Plan | One short plan: versions, branch, command, extra steps | ✔ |
| Update | Creates a new branch, or a worktree if your checkout has work in progress. Runs one command in the project's own container (`docker compose run --rm --no-deps …`) without starting the whole stack. Runs an audit before and after the update to show the advisory is fixed | |
| Tests | Suggests the project's test command | ✔ |
| Commit | Uses the repository's commit convention and passes its hooks | ✔ |
| Push, MR | Opens the MR with `gh` or `glab`, or with the skill you named | ✔ each |
| Deploy | A subagent reads the CI/CD setup and recent runs to find how the team actually deploys. It warns if the target branch has other work not yet on production | ✔ every command |
| Smoke test | Offers [`smoke-test`](#smoke-test) on the deployed site | ✔ |

### Prepare mode

When a fix is announced for a later date, the agent prepares the project so the update takes minutes on release day:

- **Catch up.** If the project is behind the latest release, it plans an update to that release now (for example 8.4.22 → 8.4.31).
- **Pre-stage.** It writes the change for the expected version before that version exists. The expected version is the one the advisory names, or else the latest patch plus one: with 8.5.22 out, it writes `php:8.5.23` into the Dockerfile. It commits locally and doesn't push until the release appears.
- **Watch.** It checks the registry every 5 minutes. When the release appears, it switches to a normal update and starts again at the plan.

**Check a release yourself:**

```bash
python3 skills/critical-update/scripts/check_release.py packagist laravel/framework 12.74.0
python3 skills/critical-update/scripts/check_release.py docker php --latest 8.5
python3 skills/critical-update/scripts/check_release.py --help
```

```
available packagist laravel/framework 12.74.0
latest docker php 8.5.13
```

It supports Packagist, npm, PyPI, Docker Hub and GitHub. Exit 0 means published, 1 not yet, 2 error.

### Requirements

- git, and Python 3.9+ (standard library only)
- Docker, `gh` or `glab`, for projects that use them
- Optional: a subagent tool (without one, the agent does the inspection itself) and a tool that starts new conversations (without one, a group run gives you prompts instead)

### Sandboxes and non-interactive runs

**Network access.** The agent needs to fetch the advisory and query the registries: `repo.packagist.org`, `registry.npmjs.org`, `pypi.org`, `hub.docker.com` and `api.github.com`. The update itself also needs the project's package sources and the Docker socket.

**Approval steps.** The skill stops for your approval before every outward step, so it is built for interactive sessions. In an unattended run it gets only as far as the plan.

### Security

**What the agent does without asking:** reads files, runs read-only commands (`git log`, `git fetch`, `gh run list`, `glab ci list`, GET calls with `gh api` or `glab api`), and changes files only on its own branch or worktree.

**What it does only after your approval:** commit, push, open an MR, merge, run a pipeline job or workflow, tag, skip commit hooks (`--no-verify`). Each deploy command is approved on its own, even when you approved a similar command earlier.

**What `check_release.py` connects to:** only the registry you name. It sends `GITHUB_TOKEN`, when set, only to `api.github.com`. It stores nothing.

### Caveats

- A pre-staged version is a guess until the release appears. A fix published under another number needs the change edited.
- Without `GITHUB_TOKEN`, the GitHub API allows 60 requests per hour. Watching a GitHub release every 5 minutes uses about 24 of them.
- On Docker Hub, `--latest` reads up to 1000 tags matching the prefix and considers only plain version tags (`8.5.13`, not `8.5.13-fpm`). To check one variant, pass its full tag.

## smoke-test

Checks that a deployed site answers HTTP 200 on its home page and on 20 random URLs from its sitemap. When the site is behind a cache (CloudFront, Varnish, Fastly, Cloudflare), a second pass repeats the check on a fresh sample with a random `?cachebuster=` value, so those requests reach the origin. The agent runs it only when you ask, or after you approve its offer (for example at the end of `critical-update`).

### Usage

**Ask the agent:**

```
Smoke test https://example.com and https://example.com/pl.
```

**Run the script directly** (no agent needed):

```bash
python3 skills/smoke-test/scripts/smoke_test.py https://example.com https://example.com/pl
python3 skills/smoke-test/scripts/smoke_test.py https://example.com --sitemap https://example.com/pl/sitemap.xml --count 50
python3 skills/smoke-test/scripts/smoke_test.py --help
```

```
https://example.com
  pass 1: 21/21 OK, slowest 412 ms
  pass 2, cachebuster=9f3a1c2e (cache headers: age, via, x-cache): 20/21 OK, slowest 1380 ms
    500  1210 ms  https://example.com/products/42?cachebuster=9f3a1c2e
```

| Option | Default | |
|---|---|---|
| `--count` | 20 | random sitemap URLs per pass, plus the home page |
| `--sitemap` | found automatically | sitemap URL. By default the script looks for `sitemap.xml` under the site's path, then at the root, then in `robots.txt` |
| `--cachebuster` | `auto` | `auto` runs the second pass when the home page has cache headers. `always` and `never` force it on or off |
| `--parallel` | 8 | concurrent requests |
| `--timeout` | 30 | seconds per request |
| `--seed` | random | repeats the same sample |

Exit 0 means every URL answered 200, 1 means some did not, and 2 means a site could not be tested (for example, no sitemap was found).

### Requirements

- Python 3.9+, standard library only
- Network access to the tested sites

### Security

**What it sends:** GET requests with the user agent `smoke-test/1.0`. That is the sitemap files (at most 11 per site: the main one and up to 10 it links to), plus the home page and `--count` URLs per pass. It follows redirects. It sends no cookies or credentials.

**What it stores:** nothing.

### Caveats

- The test passes on the final status after redirects, so a page that redirects to a working page counts as OK.
- A page that answers 200 with an error message (a soft 404) also counts as OK.
- The cachebuster reaches the origin only if the cache includes the query string in its key. If the second pass is no slower than the first, the cache may ignore it.
- Python from python.org on macOS can fail with `CERTIFICATE_VERIFY_FAILED`. Run its `Install Certificates.command`, or point `SSL_CERT_FILE` at a CA bundle.

## License

[MIT](LICENSE)
