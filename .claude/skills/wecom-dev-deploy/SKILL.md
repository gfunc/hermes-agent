---
name: wecom-dev-deploy
description: Deploy WeCom gateway changes by pushing to the user's fork, updating the Hermes install, and verifying gateway health before asking the user to test via WeCom.
version: 1.0.0
---

# WeCom Dev Deploy — Push, Update, and Verify

Use this skill whenever you have modified WeCom gateway code (or any code that affects the running Hermes gateway) and need to roll it out so the user can test it live.

## Prerequisites

- The working directory is `/home/georgefu/Projects/hermes-agent` (dev repo).
- The installed Hermes agent lives in `~/.hermes/hermes-agent` and tracks `gfunc/hermes-agent` as `origin`.
- The dev repo has `atm` pointing to `gfunc/hermes-agent` and `origin` pointing to `NousResearch/hermes-agent`.

## Procedure

### 1. Commit any unstaged changes
If there are uncommitted changes, commit them with a descriptive message:
```bash
git add -A
git commit -m "fix(wecom): descriptive commit message"
```

### 2. Push to the user's fork
Push the current `main` branch to `atm/main` (the user's fork):
```bash
git push atm main
```

### 3. Update `~/.hermes/hermes-agent` from the fork
**IMPORTANT:** Do NOT run `hermes update` from the dev repo — its `origin` points to `NousResearch/hermes-agent` and will overwrite your work. **Always run `hermes` commands from the home directory** when the intention is to change the live agent:
```bash
cd ~ && hermes update
```
This ensures `hermes update` pulls from the user's fork (`gfunc/hermes-agent`) and not the upstream repo.

If you need a manual git sync instead:
```bash
cd ~/.hermes/hermes-agent && git pull --ff-only origin main
```

### 4. Restart the gateway
After the code is synced, restart the gateway service from the home directory:
```bash
cd ~ && hermes gateway restart
```

### 5. Wait for the gateway to become healthy
Poll gateway status until it shows `active (running)`:
```bash
hermes gateway status
```
If it is still `activating` or stopped, wait a few seconds and retry. If it fails to start, check recent logs:
```bash
journalctl --user -u hermes-gateway.service -n 30
```

### 6. Notify the user to test via WeCom
Once the gateway is confirmed healthy, tell the user:
- What fix or change was just deployed.
- Ask them to send a test message via WeCom to verify it works.

## Example notification
> The gateway is now running with the latest fix. Please test it by sending a message in WeCom (e.g. `杭州天气怎么样`) and confirm the typing indicator behaves correctly.

## Pitfalls
- Running `hermes update` from the dev repo resets `main` to `NousResearch/hermes-agent` and discards local commits. Always push to `atm` first, then run `cd ~ && hermes update`.
- Running `hermes gateway restart` from the dev repo can restart the service using the wrong codebase. Always run `cd ~ && hermes gateway restart`.
- The gateway may briefly show `activating (auto-restart)` after restart. Wait until `active (running)` before declaring success.
