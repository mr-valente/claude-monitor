# Claude Code quota -> Homepage

A tiny authenticated bridge for displaying personal Claude Code quota usage in gethomepage.dev, including:

- 5-hour session limit
- all-models weekly limit
- Fable weekly limit (when your plan reports one)

## Data flow

```text
Claude Code statusLine ── 5h / 7d ───────────────┐
                                                  ├─> push-quota.py -> bridge -> Homepage
Claude Code experimental get_usage ── Fable ─────┘
```

The bridge never receives Claude credentials. The local sender only sends quota percentages and reset timestamps.

The 5-hour and all-models weekly windows come from Claude Code's documented `statusLine` JSON. Claude Code does **not** currently put the Fable model-scoped weekly window in that payload, so the sender periodically asks the local `claude` CLI for its experimental `get_usage` control response. Claude Code handles its own login/token refresh; no OAuth token is read by this script, and no model turn is invoked.

The `get_usage` control interface is explicitly experimental and may change in a future Claude Code release. If it breaks, the 5-hour and all-models weekly values will continue to work; only Fable will stop refreshing until the integration is updated.

## 1. Server

Copy this directory to the homelab server.

Generate a secret:

```bash
openssl rand -hex 32
```

Copy `.env.example` to `.env`, put the secret after `QUOTA_TOKEN=`, then start:

```bash
docker compose up -d --build
```

Health check:

```bash
curl http://SERVER-IP:8787/healthz
```

Do not expose port 8787 directly to the public Internet. LAN/VPN access or a TLS reverse proxy is recommended.

## 2. Claude Code machine

Copy `push-quota.py` to `~/.claude/push-quota.py`.

Copy `quota-bridge.json.example` to `~/.claude/quota-bridge.json`, then set:

- `url` to `http://SERVER-IP:8787/usage` (or your HTTPS reverse-proxy URL)
- `token` to the same secret from the server `.env`
- leave `scoped_usage_enabled` as `true` to include Fable
- `scoped_usage_refresh_seconds` defaults to 300 seconds (5 minutes)

On macOS/Linux, optionally protect the config:

```bash
chmod 600 ~/.claude/quota-bridge.json
```

Merge the appropriate statusLine snippet into `~/.claude/settings.json`.

For macOS/Linux/WSL use the POSIX snippet. On native Windows, use the Windows snippet and replace `YOUR_USERNAME`; if your Python command is `py -3` instead of `python`, change the command accordingly.

The sender prints a local status line such as:

```text
Claude · 5h 48% · 7d 20% · Fable 37%
```

The normal 5h/7d push is lightweight and runs with the status line. Fable is refreshed in a detached background probe at most once every five minutes, so the status line itself does not wait for the extra Claude CLI startup.

### Why the Fable fetch is separate

Claude Code currently exposes only the aggregate 5-hour and 7-day windows in `statusLine` stdin. Its experimental `get_usage` control request exposes model-scoped windows such as Fable. The sender runs this equivalent request locally:

```json
{"type":"control_request","request_id":"quota-bridge","request":{"subtype":"get_usage"}}
```

using a throwaway `claude -p --input-format stream-json --output-format stream-json` process. It disables session persistence and MCP loading for that probe. No prompt is sent and no model is invoked.

## 3. Verify the bridge has data

After Claude Code has been open for a few minutes and has completed at least one assistant response:

```bash
curl -H "X-API-Token: YOUR_SECRET" http://SERVER-IP:8787/usage | python3 -m json.tool
```

You should see `five_hour`, `seven_day`, `fable`, reset timestamps, summaries, and `updated_at`. If your account does not currently have a Fable-specific limit, `fable` will be `null`/absent until one is reported.

You can also force a local scoped-usage refresh for testing:

```bash
python3 ~/.claude/push-quota.py --refresh-usage
```

On native Windows:

```powershell
python $HOME/.claude/push-quota.py --refresh-usage
```

Then inspect:

```text
~/.claude/quota-bridge-usage-cache.json
```

## 4. Homepage

Merge `homepage-services.yaml.example` into Homepage's `services.yaml`.

The example uses Homepage's environment-secret substitution. Add this environment variable to the Homepage container:

```text
HOMEPAGE_VAR_CLAUDE_QUOTA_TOKEN=YOUR_SECRET
```

Then restart Homepage.

If Homepage and the bridge share a Docker network, you can replace the widget URL with:

```text
http://claude-quota-bridge:8787/usage
```

Otherwise use the homelab server's LAN IP or internal DNS name.

## Notes

- Claude Code can omit quota fields before the first API response, and occasionally a single quota window may be absent. The bridge preserves the last known value for a missing window.
- Once a stored reset timestamp passes, the bridge reports that window as 0% used until Claude Code sends a new observation. The original observed value remains available as `observed_used_percentage`.
- `refreshInterval: 60` causes Claude Code to rerun the status-line command once per minute while the session is open. Closing Claude Code stops status-line updates. The Fable background probe is only launched by those status-line invocations, so it also stops when Claude Code is closed.
- The Fable probe clears Claude's nested-session marker before launching the throwaway `claude -p` process, but preserves the user's normal Claude configuration/authentication context.
- The bridge stores no Claude OAuth token, API key, prompt, transcript, or code.
- If Anthropic later adds `rate_limits.model_scoped` directly to `statusLine`, the extra probe can be removed.
