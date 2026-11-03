"""Exercise refresh, HTTP serving, and reversible user installation."""

import dataclasses
import fcntl
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock
from urllib import error, request

import yaml

import subscription_override as service


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "state"
        self.directory.mkdir()
        self.environment = mock.patch.dict(
            os.environ,
            {
                "XDG_STATE_HOME": str(self.root / "persistent"),
                "XDG_CONFIG_HOME": str(self.root / "configuration"),
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.configuration = service.Configuration(
            self.root / "config.toml",
            "127.0.0.1",
            8080,
            "*/30 * * * *",
            (service.Subscription("personal", "https://example.test/subscription"),),
        )

    def start_server(self, server: ThreadingHTTPServer) -> str:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def start_upstream(self, contents: bytes) -> tuple[str, list[str | None]]:
        agents: list[str | None] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                agents.append(self.headers.get("User-Agent"))
                self.send_response(200)
                self.send_header("Content-Length", str(len(contents)))
                self.end_headers()
                self.wfile.write(contents)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        return self.start_server(ThreadingHTTPServer(("127.0.0.1", 0), Handler)), agents

    def write_configuration(self, contents: str) -> service.Configuration:
        self.configuration.path.write_text(contents)
        return service.load_configuration(self.configuration.path)

    def test_configuration_resolves_override_and_accepts_empty_override(self) -> None:
        script = self.root / "custom.py"
        script.write_text("def override(configuration):\n    return configuration\n")
        configuration = self.write_configuration(
            'refresh_cron = "0 */2 * * *"\n'
            "[server]\nport = 8090\n"
            '[[subscriptions]]\nid = "custom"\nurl = "https://example.test/a"\n'
            'override = "custom.py"\n'
            '[[subscriptions]]\nid = "empty"\nurl = "https://example.test/b"\n'
            'override = ""\n'
            '[[subscriptions]]\nid = "omitted"\nurl = "https://example.test/c"\n'
        )
        self.assertEqual(configuration.port, 8090)
        self.assertEqual(configuration.subscriptions[0].override_path, script)
        self.assertIsNone(configuration.subscriptions[1].override_path)
        self.assertIsNone(configuration.subscriptions[2].override_path)

    def test_configuration_rejects_invalid_inputs(self) -> None:
        valid = '[[subscriptions]]\nid = "valid"\nurl = "https://example.test/a"\n'
        cases = [
            "",
            valid + valid,
            valid.replace('"valid"', '"../secret"'),
            valid.replace("https://example.test/a", "file:///etc/passwd"),
            "[server]\nport = true\n" + valid,
            "[server]\nport = 65536\n" + valid,
            'refresh_cron = "* * * * *\\nmalicious"\n' + valid,
            valid + 'override = "missing.py"\n',
        ]
        for contents in cases:
            with self.subTest(contents=contents), self.assertRaises(ValueError):
                self.write_configuration(contents)

    def test_fetch_bypasses_environment_proxy_and_sets_clash_agent(self) -> None:
        url, agents = self.start_upstream(b"proxies: []\n")
        with mock.patch.dict(
            os.environ,
            {
                "http_proxy": "http://127.0.0.1:1",
                "HTTP_PROXY": "http://127.0.0.1:1",
                "all_proxy": "http://127.0.0.1:1",
                "ALL_PROXY": "http://127.0.0.1:1",
                "no_proxy": "",
                "NO_PROXY": "",
            },
        ):
            result = service.download_subscription(url)
        self.assertEqual(yaml.safe_load(result), {"proxies": []})
        self.assertEqual(agents, ["clash.meta"])

    def test_override_receives_parsed_yaml_and_returns_serializable_mapping(
        self,
    ) -> None:
        source_yaml = "proxies: []\nname: 测试\n".encode()
        script = self.root / "custom.py"
        script.write_text(
            "from dataclasses import dataclass\n"
            "@dataclass\nclass Options:\n    port: int = 7890\n"
            "def override(configuration):\n"
            "    configuration['mixed-port'] = Options().port\n"
            "    return configuration\n"
        )
        result = service.transform(
            service.Subscription("custom", "https://example.test", script), source_yaml
        )
        self.assertEqual(
            yaml.safe_load(result), {"proxies": [], "name": "测试", "mixed-port": 7890}
        )
        self.assertIn("测试", result.decode())

    def test_untrusted_yaml_tags_are_not_executed(self) -> None:
        marker = self.root / "executed"
        source_yaml = f"!!python/object/apply:os.system ['touch {marker}']".encode()
        with self.assertRaises(yaml.YAMLError):
            service.transform(
                service.Subscription("unsafe", "https://example.test"), source_yaml
            )
        self.assertFalse(marker.exists())

    def test_invalid_override_does_not_replace_last_success(self) -> None:
        url, _ = self.start_upstream(b"proxies: []\n")
        script = self.root / "broken.py"
        previous = b"proxies: []\nmixed-port: 7890\n"
        cache_path = self.directory / "personal.yaml"
        cache_path.write_bytes(previous)
        configuration = dataclasses.replace(
            self.configuration,
            subscriptions=(service.Subscription("personal", url, script),),
        )
        for source in [
            "def override(configuration):\n    raise RuntimeError('broken')\n",
            "def override(configuration):\n    return None\n",
            "def override(configuration):\n    return {'bad': object()}\n",
            "not_override = True\n",
        ]:
            with self.subTest(source=source):
                script.write_text(source)
                with self.assertLogs(service.LOGGER, level="ERROR"):
                    self.assertEqual(service.refresh(configuration, self.directory), 1)
                self.assertEqual(cache_path.read_bytes(), previous)

    def test_failed_subscription_does_not_prevent_other_refreshes(self) -> None:
        configuration = dataclasses.replace(
            self.configuration,
            subscriptions=(
                service.Subscription("failed", "https://example.test/fail"),
                service.Subscription("success", "https://example.test/success"),
            ),
        )
        with (
            mock.patch.object(
                service,
                "download_subscription",
                side_effect=[
                    OSError("unavailable"),
                    b"proxies: []\n",
                ],
            ),
            self.assertLogs(service.LOGGER, level="ERROR"),
        ):
            self.assertEqual(service.refresh(configuration, self.directory), 1)
        self.assertEqual(
            (self.directory / "success.yaml").read_bytes(), b"proxies: []\n"
        )
        self.assertEqual(
            (self.directory / "success.yaml").stat().st_mode & 0o777, 0o600
        )

    def test_overlapping_refresh_is_skipped(self) -> None:
        with (self.directory / "refresh.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(service, "download_subscription") as download:
                self.assertEqual(service.refresh(self.configuration, self.directory), 0)
                download.assert_not_called()

    def test_shared_url_is_downloaded_once_with_isolated_overrides(self) -> None:
        url, agents = self.start_upstream(b"proxies:\n- name: Original\nrules: []\n")
        first_script = self.root / "first.py"
        first_script.write_text(
            "def override(configuration):\n"
            "    configuration['proxies'][0]['name'] = 'First'\n"
            "    configuration['rules'].append('FIRST')\n"
            "    return configuration\n"
        )
        second_script = self.root / "second.py"
        second_script.write_text(
            "def override(configuration):\n"
            "    configuration['rules'].append('SECOND')\n"
            "    return configuration\n"
        )
        configuration = dataclasses.replace(
            self.configuration,
            subscriptions=(
                service.Subscription("first", url, first_script),
                service.Subscription("second", url, second_script),
                service.Subscription("original", url),
            ),
        )
        self.assertEqual(service.refresh(configuration, self.directory), 0)
        self.assertEqual(agents, ["clash.meta"])
        outputs = {
            identifier: yaml.safe_load(
                (self.directory / f"{identifier}.yaml").read_bytes()
            )
            for identifier in ["first", "second", "original"]
        }
        self.assertEqual(outputs["first"]["proxies"], [{"name": "First"}])
        self.assertEqual(outputs["first"]["rules"], ["FIRST"])
        self.assertEqual(outputs["second"]["proxies"], [{"name": "Original"}])
        self.assertEqual(outputs["second"]["rules"], ["SECOND"])
        self.assertEqual(
            outputs["original"], {"proxies": [{"name": "Original"}], "rules": []}
        )
        self.assertEqual(service.refresh(configuration, self.directory), 0)
        self.assertEqual(agents, ["clash.meta", "clash.meta"])

    def test_failed_download_is_shared_and_preserves_each_previous_output(self) -> None:
        shared_url = "https://example.test/shared"
        other_url = "https://example.test/other"
        configuration = dataclasses.replace(
            self.configuration,
            subscriptions=(
                service.Subscription("first", shared_url),
                service.Subscription("second", shared_url),
                service.Subscription("other", other_url),
            ),
        )
        for identifier in ["first", "second"]:
            (self.directory / f"{identifier}.yaml").write_bytes(b"previous: true\n")
        with (
            mock.patch.object(
                service,
                "download_subscription",
                side_effect=[
                    error.URLError("unavailable"),
                    b"proxies: []\n",
                ],
            ) as download,
            self.assertLogs(service.LOGGER, level="ERROR"),
        ):
            self.assertEqual(service.refresh(configuration, self.directory), 1)
        self.assertEqual(
            download.call_args_list, [mock.call(shared_url), mock.call(other_url)]
        )
        for identifier in ["first", "second"]:
            self.assertEqual(
                (self.directory / f"{identifier}.yaml").read_bytes(),
                b"previous: true\n",
            )
        self.assertEqual((self.directory / "other.yaml").read_bytes(), b"proxies: []\n")

    def test_failed_override_does_not_change_the_shared_source(self) -> None:
        url, agents = self.start_upstream(b"proxies: []\n")
        script = self.root / "broken.py"
        script.write_text(
            "def override(configuration):\n"
            "    configuration['proxies'].append('broken')\n"
            "    raise RuntimeError('broken override')\n"
        )
        configuration = dataclasses.replace(
            self.configuration,
            subscriptions=(
                service.Subscription("broken", url, script),
                service.Subscription("original", url),
            ),
        )
        with self.assertLogs(service.LOGGER, level="ERROR"):
            self.assertEqual(service.refresh(configuration, self.directory), 1)
        self.assertEqual(agents, ["clash.meta"])
        self.assertEqual(
            (self.directory / "original.yaml").read_bytes(), b"proxies: []\n"
        )

    def test_cli_refresh_and_url_listing(self) -> None:
        url, _ = self.start_upstream(b"proxies: []\n")
        self.write_configuration(f'[[subscriptions]]\nid = "personal"\nurl = "{url}"\n')
        command = [
            sys.executable,
            str(Path(service.__file__).resolve()),
            "--config",
            str(self.configuration.path),
        ]
        refresh = subprocess.run(
            [*command, "refresh"], capture_output=True, text=True, timeout=10
        )
        self.assertEqual(refresh.returncode, 0, refresh.stderr)
        self.assertEqual(
            (service.state_directory() / "personal.yaml").read_bytes(),
            b"proxies: []\n",
        )
        urls = subprocess.run(
            [*command, "urls"], capture_output=True, text=True, timeout=10
        )
        self.assertEqual(urls.returncode, 0, urls.stderr)
        self.assertEqual(
            urls.stdout.strip(),
            "http://127.0.0.1:8080/subscriptions/personal.yaml",
        )

    def test_uninstall_does_not_require_configuration(self) -> None:
        with (
            mock.patch.object(
                sys, "argv", ["subscription_override.py", "uninstall-user"]
            ),
            mock.patch.object(service, "read_crontab", return_value=""),
            mock.patch.object(service, "load_configuration") as load,
        ):
            self.assertEqual(service.main(), 0)
            load.assert_not_called()

    def test_http_returns_cached_yaml_and_expected_errors(self) -> None:
        configuration = dataclasses.replace(self.configuration, port=0)
        url = self.start_server(service.create_server(configuration, self.directory))
        opener = request.build_opener(request.ProxyHandler({}))
        opener.addheaders = [("User-Agent", "clash.meta")]
        for path, status in [
            ("/subscriptions/personal.yaml", 503),
            ("/subscriptions/unknown.yaml", 404),
            ("/../config.toml", 404),
        ]:
            with self.subTest(path=path), self.assertRaises(error.HTTPError) as caught:
                opener.open(url + path, timeout=5)
            self.assertEqual(caught.exception.code, status)
            caught.exception.close()
        service.atomic_write(self.directory / "personal.yaml", b"proxies: []\n")
        with opener.open(url + "/subscriptions/personal.yaml", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"proxies: []\n")
            self.assertEqual(response.headers.get_content_type(), "application/yaml")

    def test_http_rejects_missing_different_and_duplicate_user_agents(self) -> None:
        configuration = dataclasses.replace(self.configuration, port=0)
        server = service.create_server(configuration, self.directory)
        url = self.start_server(server)
        service.atomic_write(self.directory / "personal.yaml", b"proxies: []\n")
        opener = request.build_opener(request.ProxyHandler({}))
        opener.addheaders = []
        for agent in [None, "", "curl/8", "Clash.Meta", "clash.meta/1.0"]:
            for path in ["/subscriptions/personal.yaml", "/unknown"]:
                headers = {} if agent is None else {"User-Agent": agent}
                with self.subTest(agent=agent, path=path):
                    with self.assertRaises(error.HTTPError) as caught:
                        opener.open(
                            request.Request(url + path, headers=headers), timeout=5
                        )
                    self.assertEqual(caught.exception.code, 403)
                    caught.exception.close()
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        self.addCleanup(connection.close)
        connection.putrequest("GET", "/subscriptions/personal.yaml")
        connection.putheader("User-Agent", "clash.meta")
        connection.putheader("User-Agent", "curl/8")
        connection.endheaders()
        with connection.getresponse() as response:
            self.assertEqual(response.status, 403)

    def test_crontab_changes_preserve_unrelated_entries(self) -> None:
        unrelated = "MAILTO=owner@example.test\n0 1 * * * /usr/bin/true\n"
        installed = service.managed_crontab(unrelated, "*/30 * * * * refresh")
        self.assertTrue(installed.startswith(unrelated))
        self.assertEqual(service.managed_crontab(installed, None), unrelated)
        self.assertEqual(
            service.managed_crontab(installed, "*/30 * * * * refresh"), installed
        )
        self.assertEqual(service.managed_crontab(unrelated, None), unrelated)
        for contents in [service.CRON_BEGIN + "\n", service.CRON_END + "\n"]:
            with self.assertRaises(ValueError):
                service.managed_crontab(contents, None)

    def test_install_and_repeated_uninstall_preserve_user_files(self) -> None:
        unrelated = "0 1 * * * /usr/bin/true\n"
        crontab = unrelated
        calls: list[list[str]] = []

        def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
            nonlocal crontab
            calls.append(command)
            if command == ["crontab", "-l"]:
                return subprocess.CompletedProcess(command, 0, crontab, "")
            if command == ["crontab", "-"]:
                crontab = kwargs["input"]
            return subprocess.CompletedProcess(command, 0, "", "")

        config_path = self.configuration.path
        config_path.write_text("preserve me")
        cache_path = self.directory / "personal.yaml"
        cache_path.write_text("proxies: []\n")
        with (
            mock.patch.object(service.subprocess, "run", side_effect=run),
            mock.patch.object(
                service.shutil, "which", return_value="/usr/bin/systemd-cat"
            ),
            mock.patch.object(service, "refresh", return_value=0),
        ):
            service.install_user(self.configuration, self.directory)
            first_crontab = crontab
            self.assertTrue(service.unit_path().exists())
            self.assertIn("--config", service.unit_path().read_text())
            self.assertIn(str(self.configuration.path), crontab)
            service.install_user(self.configuration, self.directory)
            self.assertEqual(crontab, first_crontab)
            service.uninstall_user()
            service.uninstall_user()
        self.assertEqual(crontab, unrelated)
        self.assertFalse(service.unit_path().exists())
        self.assertEqual(config_path.read_text(), "preserve me")
        self.assertEqual(cache_path.read_text(), "proxies: []\n")
        self.assertIn(
            ["systemctl", "--user", "disable", "--now", service.UNIT_NAME], calls
        )
        self.assertFalse(any("loginctl" in command for command in calls))

    def test_install_restores_unit_when_crontab_rejects_schedule(self) -> None:
        unit = service.unit_path()
        unit.parent.mkdir(parents=True)
        unit.write_bytes(b"previous unit")
        with (
            mock.patch.object(service, "read_crontab", return_value=""),
            mock.patch.object(service, "write_crontab", side_effect=RuntimeError),
            mock.patch.object(service.subprocess, "run"),
            mock.patch.object(
                service.shutil, "which", return_value="/usr/bin/systemd-cat"
            ),
        ):
            with self.assertRaises(RuntimeError):
                service.install_user(self.configuration, self.directory)
        self.assertEqual(unit.read_bytes(), b"previous unit")

    def test_read_crontab_distinguishes_missing_table_from_permission_error(
        self,
    ) -> None:
        with mock.patch.object(
            service.subprocess,
            "run",
            return_value=(
                subprocess.CompletedProcess([], 1, "", "no crontab for example")
            ),
        ):
            self.assertEqual(service.read_crontab(), "")
        with (
            mock.patch.object(
                service.subprocess,
                "run",
                return_value=(
                    subprocess.CompletedProcess([], 1, "", "permission denied")
                ),
            ),
            self.assertRaises(RuntimeError),
        ):
            service.read_crontab()

    def test_generated_commands_escape_systemd_and_cron_special_characters(
        self,
    ) -> None:
        configuration = dataclasses.replace(
            self.configuration, path=self.root / 'a $b%"c.toml'
        )
        unit, cron = service.installation_contents(
            configuration, self.directory, "/usr/bin/systemd-cat"
        )
        self.assertIn('$$b%%\\"c.toml', unit)
        self.assertIn(r"\%", cron)
        self.assertEqual(
            service.subscription_urls(self.configuration),
            ["http://127.0.0.1:8080/subscriptions/personal.yaml"],
        )
        with self.assertRaises(ValueError):
            service.systemd_quote("/tmp/invalid\npath")


if __name__ == "__main__":
    unittest.main()
