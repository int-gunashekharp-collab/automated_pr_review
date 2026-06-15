# Keep the loop running

The loop only runs on **your Mac** (it needs Vertex auth, `gh`, and read access
to `maestro-core`). It stopped on 2026-06-15 ~05:57 UTC for two reasons:

1. The process died (heartbeat went stale).
2. Before that, every iteration since ~#121 errored because your **Vertex /
   gcloud ADC credentials expired** (`_perform_refresh_token` failure). The loop
   kept spinning but learned nothing — 73 errored iterations, 0 promotions.

This folder fixes both: re-auth once, then run a supervisor that restarts the
loop automatically and refuses to spin on dead auth.

## Step 1 — re-auth (one time, opens a browser)

```bash
gcloud auth application-default login
```

## Step 2 — keep it running (pick ONE)

**A. Always-on (recommended — survives crashes, logout, reboot):**

```bash
cp "/Users/gunashekharp/testing workflow/src/data/loop_pr_reviewer/keepalive/com.snabbit.pr-review-loop.plist" ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.snabbit.pr-review-loop.plist
```

Stop it:

```bash
launchctl unload -w ~/Library/LaunchAgents/com.snabbit.pr-review-loop.plist
```

**B. Foreground supervisor (survives crashes + terminal close, not reboot):**

```bash
cd "/Users/gunashekharp/testing workflow/src/data/loop_pr_reviewer"
nohup bash keepalive/keep_running.sh >/dev/null 2>&1 &
```

## Check it's alive

```bash
tail -f "/Users/gunashekharp/testing workflow/src/data/loop_pr_reviewer/workspace/loop_supervisor.log"
python3 loop.py --status          # champion metrics + last decisions
python3 dashboard_server.py       # live dashboard at http://127.0.0.1:8123/
```

## The durable fix (stop re-auth from expiring)

User ADC tokens expire and need re-login. For a true unattended runner, use a
**service account** with the `Vertex AI User` role instead:

```bash
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/sa-key.json"   # add to .env
```

Then auth never expires and Step 1 is no longer needed. (Requires someone with
GCP admin to mint the key.)
