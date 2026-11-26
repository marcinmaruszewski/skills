# Prepare mode

The fix is announced but not yet published. The goal is a project that can take the fix in minutes when it ships. Three parts, each offered in the plan (gate 1), each optional for the user.

## Catch up to the latest release

Compare the project's installed version with the latest release on the same branch (`latest` in the inspection report). When the project is behind, plan a regular update to that latest version: steps 4 to 6 of SKILL.md, with the same gates. A small jump today keeps release day to a single patch step.

## Pre-stage the fix

Prepare the change for the version that does not exist yet: the version the advisory names, or else the latest release with the patch number plus one. State that guess in the plan, e.g. "latest is 8.5.22, expecting 8.5.23".

Change only what can be written without the release: `Dockerfile` `FROM` tags and `ARG`s, compose and CI image tags, manifest constraints and pins. A lock file needs the release to resolve, so it waits. Commit locally on the update branch, with a message body line saying the commit waits for the release, and keep it unpushed. When the release appears, resolve the lock, build or install, verify as in step 4, amend the commit, and continue from the commit gate.

## Watch for the release

When the advisory gives a release time, offer to watch for it. One check, with the path relative to this file's directory:

```
python3 scripts/check_release.py <ecosystem> <package> <version>
```

Exit 0 means published, 1 means not yet, 2 means the check failed. Repeat it every 5 minutes by default, with your agent's own scheduling or loop tool when it has one, else with `sleep 300` between checks. Use the system crontab only when the user asks for it.

When the check exits 0, tell the user, switch to fix mode, re-run the inspection (the lock may have moved since), and continue from gate 1.
