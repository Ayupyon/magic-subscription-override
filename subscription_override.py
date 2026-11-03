"""Fetch, transform, and serve Clash subscriptions for a single Linux user."""

import argparse
import fcntl
import importlib.util
import logging
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import request
from urllib.parse import urlsplit

import yaml

APPLICATION = "magic-subscription-override"
UNIT_NAME = f"{APPLICATION}.service"
CRON_BEGIN = f"# BEGIN {APPLICATION}"
CRON_END = f"# END {APPLICATION}"
DEFAULT_CONFIG = Path(__file__).resolve().parent / "config/config.toml"
LOGGER = logging.getLogger(APPLICATION)


@dataclass(frozen=True)
class Subscription:
    identifier: str
    url: str
    override_path: Path | None = None


@dataclass(frozen=True)
class Configuration:
    path: Path
    host: str
    port: int
    refresh_cron: str
    subscriptions: tuple[Subscription, ...]


def load_configuration(path: Path) -> Configuration:
    """Load and validate settings, resolving script paths beside the TOML file."""
    path = path.expanduser().absolute()
    with path.open("rb") as configuration_file:
        settings = tomllib.load(configuration_file)
    server = settings.get("server", {})
    if not isinstance(server, dict):
        raise ValueError("server must be a TOML table")
    host = server.get("host", "127.0.0.1")
    port = server.get("port", 8080)
    if not isinstance(host, str) or not host or any(c.isspace() for c in host):
        raise ValueError("server.host must be a nonempty hostname or IP address")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("server.port must be an integer between 1 and 65535")
    schedule = settings.get("refresh_cron", "*/30 * * * *")
    if not isinstance(schedule, str) or not re.fullmatch(
        r"[\w*/,\-]+(?:[ \t]+[\w*/,\-]+){4}", schedule, flags=re.ASCII
    ):
        raise ValueError("refresh_cron must contain five standard cron fields")
    entries = settings.get("subscriptions", [])
    if not isinstance(entries, list) or not entries:
        raise ValueError("at least one [[subscriptions]] entry is required")
    subscriptions = []
    identifiers = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each subscription must be a TOML table")
        identifier = entry.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]*", identifier
        ):
            raise ValueError("subscription IDs must use letters, digits, '-' or '_'")
        if identifier in identifiers:
            raise ValueError(f"duplicate subscription ID: {identifier}")
        identifiers.add(identifier)
        url = entry.get("url")
        if not isinstance(url, str):
            raise ValueError(f"{identifier}: url must be an HTTP/HTTPS URL")
        parsed_url = urlsplit(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            raise ValueError(f"{identifier}: url must be an HTTP/HTTPS URL")
        override = entry.get("override", "")
        if not isinstance(override, str):
            raise ValueError(
                f"{identifier}: override must be a file path or empty string"
            )
        override_path = path.parent / Path(override).expanduser() if override else None
        if override_path is not None and not override_path.is_file():
            raise ValueError(f"{identifier}: override file not found: {override_path}")
        subscriptions.append(Subscription(identifier, url, override_path))
    return Configuration(path, host, port, schedule, tuple(subscriptions))


def state_directory() -> Path:
    """Return a private persistent directory shared by HTTP and cron processes."""
    root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    if not root.is_absolute():
        raise ValueError("XDG_STATE_HOME must be an absolute path")
    directory = root / APPLICATION
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory


def atomic_write(path: Path, contents: bytes) -> None:
    """Replace a file only after the complete new contents have been written."""
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(contents)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def download_subscription(url: str) -> bytes:
    """Download upstream YAML directly without environment-configured proxies."""
    opener = request.build_opener(request.ProxyHandler({}))
    upstream_request = request.Request(url, headers={"User-Agent": "clash.meta"})
    with opener.open(upstream_request, timeout=30) as response:
        return response.read()


def transform(subscription: Subscription, source_yaml: bytes) -> bytes:
    """Parse an independent configuration and run its trusted override function."""
    configuration: Any = yaml.safe_load(source_yaml)
    if not isinstance(configuration, dict):
        raise ValueError("upstream Clash YAML must contain a mapping")
    if subscription.override_path is not None:
        module_name = f"_subscription_override_{subscription.identifier}"
        specification = importlib.util.spec_from_file_location(
            module_name, subscription.override_path
        )
        if specification is None or specification.loader is None:
            raise ValueError("override must be an importable Python file")
        module = importlib.util.module_from_spec(specification)
        sys.modules[module_name] = module
        try:
            specification.loader.exec_module(module)
            override = getattr(module, "override", None)
            if not callable(override):
                raise ValueError("override file must define a callable override")
            configuration = override(configuration)
        finally:
            sys.modules.pop(module_name, None)
    if not isinstance(configuration, dict):
        raise ValueError("override must return a Clash configuration mapping")
    return yaml.safe_dump(configuration, allow_unicode=True, sort_keys=False).encode(
        "utf-8"
    )


def refresh(configuration: Configuration, directory: Path) -> int:
    """Download each URL once per refresh, preserving previous files on failure."""
    with (directory / "refresh.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOGGER.info("Another refresh is already running")
            return 0
        failures = 0
        downloads: dict[str, bytes | Exception] = {}
        for subscription in configuration.subscriptions:
            try:
                if subscription.url not in downloads:
                    try:
                        downloads[subscription.url] = download_subscription(
                            subscription.url
                        )
                    except Exception as error:
                        downloads[subscription.url] = error
                source_yaml = downloads[subscription.url]
                if isinstance(source_yaml, Exception):
                    raise source_yaml
                contents = transform(subscription, source_yaml)
                atomic_write(directory / f"{subscription.identifier}.yaml", contents)
                LOGGER.info("Refreshed %s", subscription.identifier)
            except Exception as error:
                failures += 1
                LOGGER.error("%s: refresh failed: %s", subscription.identifier, error)
        return int(failures > 0)


def create_server(configuration: Configuration, directory: Path) -> ThreadingHTTPServer:
    """Build an HTTP server that exposes only configured cached subscriptions."""
    routes = {
        f"/subscriptions/{subscription.identifier}.yaml": (
            directory / f"{subscription.identifier}.yaml"
        )
        for subscription in configuration.subscriptions
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.headers.get_all("User-Agent") != ["clash.meta"]:
                self.send_error(403, "User-Agent must be clash.meta")
                return
            cache_path = routes.get(urlsplit(self.path).path)
            if cache_path is None:
                self.send_error(404, "Unknown subscription")
                return
            try:
                contents = cache_path.read_bytes()
            except OSError:
                self.send_error(503, "No successful refresh available")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/yaml; charset=utf-8")
            self.send_header("Content-Length", str(len(contents)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(contents)

        def log_message(self, format: str, *args: Any) -> None:
            LOGGER.info("%s %s", self.client_address[0], format % args)

    class Server(ThreadingHTTPServer):
        address_family = (
            socket.AF_INET6 if ":" in configuration.host else socket.AF_INET
        )

    return Server((configuration.host, configuration.port), Handler)


def subscription_urls(configuration: Configuration) -> list[str]:
    host = configuration.host
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1" if host == "0.0.0.0" else "::1"
    if ":" in host:
        host = f"[{host}]"
    return [
        f"http://{host}:{configuration.port}/subscriptions/{entry.identifier}.yaml"
        for entry in configuration.subscriptions
    ]


def managed_crontab(existing: str, entry: str | None) -> str:
    """Replace only the marked application block, keeping other entries intact."""
    lines = existing.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == CRON_BEGIN]
    ends = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == CRON_END]
    if starts or ends:
        if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
            raise ValueError("Malformed managed cron block; refusing to edit crontab")
        del lines[starts[0] : ends[0] + 1]
    result = "".join(lines)
    if entry is not None:
        if result and not result.endswith("\n"):
            result += "\n"
        result += f"{CRON_BEGIN}\n{entry}\n{CRON_END}\n"
    return result


def read_crontab() -> str:
    result = subprocess.run(
        ["crontab", "-l"],
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
        check=False,
    )
    if result.returncode == 0:
        return result.stdout
    if result.returncode == 1 and "no crontab for" in result.stderr.lower():
        return ""
    raise RuntimeError(f"Could not read crontab: {result.stderr.strip()}")


def write_crontab(contents: str) -> None:
    subprocess.run(["crontab", "-"], input=contents, text=True, check=True)


def unit_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if not root.is_absolute():
        raise ValueError("XDG_CONFIG_HOME must be an absolute path")
    return root / "systemd/user" / UNIT_NAME


def systemd_quote(value: str, *, command: bool = False) -> str:
    if any(character in value for character in "\r\n\x00"):
        raise ValueError("Service paths must not contain newlines or NUL bytes")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        value = value.replace("$", "$$")
    return f'"{value}"'


def installation_contents(
    configuration: Configuration, directory: Path, journal_command: str
) -> tuple[str, str]:
    arguments = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--config",
        str(configuration.path),
    ]
    environment = f"XDG_STATE_HOME={directory.parent}"
    service_command = " ".join(systemd_quote(arg, command=True) for arg in arguments)
    unit = (
        "[Unit]\nDescription=Clash subscription override service\n\n"
        "[Service]\nType=exec\n"
        f"Environment={systemd_quote(environment)}\n"
        f"ExecStart={service_command} serve\n"
        "Restart=on-failure\nRestartSec=5\nUMask=0077\n"
        f"SyslogIdentifier={APPLICATION}\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    cron_command = shlex.join(
        [
            journal_command,
            f"--identifier={APPLICATION}",
            "/usr/bin/env",
            environment,
            *arguments,
            "refresh",
        ]
    )
    # Cron treats percent signs specially even inside shell quotes.
    cron_command = cron_command.replace("%", r"\%")
    return unit, f"{configuration.refresh_cron} {cron_command}"


def install_user(configuration: Configuration, directory: Path) -> None:
    """Install one user service and update only this application's cron block."""
    journal_command = shutil.which("systemd-cat")
    if journal_command is None:
        raise RuntimeError("systemd-cat is required for scheduled refresh logging")
    unit, cron_entry = installation_contents(configuration, directory, journal_command)
    previous_crontab = read_crontab()
    updated_crontab = managed_crontab(previous_crontab, cron_entry)
    subprocess.run(
        ["systemctl", "--user", "show-environment"],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    destination = unit_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    previous_unit = destination.read_bytes() if destination.exists() else None
    atomic_write(destination, unit.encode())
    try:
        write_crontab(updated_crontab)
    except Exception:
        if previous_unit is None:
            destination.unlink()
        else:
            atomic_write(destination, previous_unit)
        raise
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", UNIT_NAME], check=True)
    subprocess.run(["systemctl", "--user", "restart", UNIT_NAME], check=True)
    LOGGER.info("Installed service and cron entry")
    if refresh(configuration, directory):
        LOGGER.warning("Installed; some subscriptions have no new data yet")


def uninstall_user() -> None:
    """Remove the service and cron block, preserving user files and lingering."""
    previous_crontab = read_crontab()
    updated_crontab = managed_crontab(previous_crontab, None)
    destination = unit_path()
    if destination.exists():
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", UNIT_NAME], check=True
        )
        destination.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    if updated_crontab != previous_crontab:
        write_crontab(updated_crontab)
    LOGGER.info(
        "Uninstalled; configuration, overrides, cache and environment preserved"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "command",
        choices=["serve", "refresh", "urls", "install-user", "uninstall-user"],
    )
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        if arguments.command == "uninstall-user":
            uninstall_user()
            return 0
        configuration = load_configuration(arguments.config)
        if arguments.command == "urls":
            print("\n".join(subscription_urls(configuration)))
            return 0
        directory = state_directory()
        if arguments.command == "refresh":
            return refresh(configuration, directory)
        if arguments.command == "install-user":
            install_user(configuration, directory)
            return 0
        with create_server(configuration, directory) as server:
            LOGGER.info("Listening at %s:%s", configuration.host, configuration.port)
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        LOGGER.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
