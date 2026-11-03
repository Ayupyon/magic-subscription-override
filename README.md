# Magic Subscription Override

A small Python service that periodically downloads Clash YAML subscriptions,
applies optional trusted Python overrides, and serves the latest successful results.
Python 3.11+, uv, Linux cron, and a systemd user session are required.

## Setup

```sh
uv sync --locked
cp config/config.example.toml config/config.toml
```

Edit `config/config.toml` with real subscription URLs. The example domains are
placeholders. The local configuration is ignored by Git because subscription URLs
often contain credentials. PyYAML is the only runtime dependency; uv manages its
environment and lockfile.

```toml
refresh_cron = "*/30 * * * *"

[server]
host = "127.0.0.1"
port = 8080

[[subscriptions]]
id = "personal"
url = "https://provider.example/subscription"
override = "overrides/personal.py"

[[subscriptions]]
id = "original"
url = "https://provider.example/subscription"
override = ""
```

IDs must be unique and contain letters, digits, underscores, or hyphens. An empty
or omitted override leaves the parsed YAML unchanged. Override paths are relative
to the configuration file; absolute paths also work. Without `--config`, commands
use `config/config.toml` beside the Python module, regardless of the working directory.

Entries with the exact same URL share one download per refresh. Each entry parses
its own copy before applying its override, so changes to nested mappings or lists
cannot affect other entries. The example above serves overridden and original
versions of one upstream subscription. For different overrides, give each entry a
unique ID and its own override file.

The shared download cache lasts for one refresh only; the next refresh fetches each
URL again. Download failures are also shared within that refresh, while failures
in one override do not prevent other entries from using the successful download.
Each entry still keeps its own last successful output on disk.

## Overrides

```python
def override(configuration):
    configuration["mixed-port"] = 7890
    configuration["allow-lan"] = False
    return configuration
```

The function receives the parsed YAML mapping and must return a YAML-serializable
mapping. Override scripts are fully trusted Python code and run as your user.
Upstream YAML is parsed with `safe_load`. Comments and original YAML formatting
are not preserved.

## Run manually

```sh
uv run python subscription_override.py refresh
uv run python subscription_override.py urls
uv run python subscription_override.py serve
```

Then, from another terminal:

```sh
curl -A "clash.meta" http://127.0.0.1:8080/subscriptions/personal.yaml
```

GET requests must contain exactly one `User-Agent` header with the value
`clash.meta` (case-sensitive). Missing, different, or duplicate headers return 403
before any subscription lookup. Clients can spoof this header; it provides no
authentication. HTTP requests only read cached results. Unknown IDs
return 404; subscriptions without a successful refresh return 503. After a refresh
failure, the previous successful YAML remains available. Failures are independent
and make `refresh` exit with status 1; overlapping refreshes are skipped.

Downloads use `User-Agent: clash.meta`, a 30-second network timeout, and explicitly
disable environment-configured HTTP/HTTPS proxies. OS-level transparent routing
is outside the application's control.

To select another configuration, put `--config` before the command:

```sh
uv run python subscription_override.py --config /absolute/path/config.toml refresh
```

## Install as a user service

Ensure the system's cron daemon is installed and running, then run without sudo:

```sh
uv run python subscription_override.py install-user
```

Installation creates and enables `magic-subscription-override.service` under
`~/.config/systemd/user/` (or `$XDG_CONFIG_HOME/systemd/user/`), adds one marked block
to your crontab, starts the HTTP server, and performs an initial refresh. Failed
downloads are logged without undoing installation. Existing unrelated cron entries
are preserved. The five-field cron expression uses the cron daemon's time zone,
subject to any existing `CRON_TZ` setting. The system's `crontab` command validates
the schedule during installation.

The generated commands use absolute paths to the current Python environment,
module, and configuration; they do not run uv or install packages in the background.
Keep the project and `.venv` at their installed paths. Re-run `install-user` after
moving them or changing the TOML configuration; this updates the schedule and
restarts the HTTP server. Override scripts are loaded on each refresh.

```sh
systemctl --user status magic-subscription-override.service
journalctl --user -u magic-subscription-override.service
journalctl -t magic-subscription-override
```

The last command includes cron refresh logs, subject to the system's journal access
policy. To keep the service running after logout and start it at boot, enable
lingering if permitted by your system's policy:

```sh
loginctl enable-linger "$USER"
```

## Uninstall

```sh
uv run python subscription_override.py uninstall-user
```

This stops and disables the user service, removes its unit, reloads the user service
manager, and removes only the marked cron block. Repeated uninstalls are harmless,
and uninstall does not require a working configuration file.

Configuration, override scripts, the Python environment, and cached files are
preserved. Lingering is left unchanged because other services may depend on it.
Cache and lock files live in `$XDG_STATE_HOME/magic-subscription-override`, defaulting
to `~/.local/state/magic-subscription-override`. The cache directory is private and
new YAML files have mode 0600. Remove preserved files manually if no longer needed.

One installation and one cache namespace are supported per user. All subscription
endpoints use plain HTTP without authentication. The default loopback listener is
intended for personal local use; use an authenticated TLS reverse proxy for remote
access rather than exposing Python's HTTP server directly.

## Checks

```sh
uv run python -m unittest discover -s tests -v
ruff check .
basedpyright
```

Ruff and basedpyright are optional developer tools installed separately. Tests use
local HTTP servers and temporary files, and mock systemd/crontab operations; they
do not change your actual user service or cron schedule.
