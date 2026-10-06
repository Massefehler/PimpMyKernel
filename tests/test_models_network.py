import io
import json
import tempfile
import threading
import unittest
from support import GermanTestCase
from pathlib import Path
from unittest.mock import Mock, patch

from pimpmykernel.models import (BuildOptions, Cancelled, KernelError, Release,
                                boot_configs, job_choices, parse_releases,
                                upstream_version, version_key)
from pimpmykernel.network import (ARCHIVE_ROOT, Network, OfficialRedirectHandler,
                                 parse_archive_releases, validate_url)


def release(version="7.2.9", channel="stable"):
    source = f"https://cdn.kernel.org/pub/linux/kernel/v{version.split('.')[0]}.x/linux-{version}.tar.xz"
    return Release(version, channel, "2026-10-03", source, source.removesuffix(".xz") + ".sign")


def archive_index(*versions):
    return "\n".join(f'<a href="linux-{version}.tar.{suffix}">linux-{version}.tar.{suffix}</a> '
                     "05-Oct-2026 12:00 100M"
                     for version in versions for suffix in ("sign", "xz"))


class ModelTests(GermanTestCase):
    def test_release_feed_prioritizes_stable_and_retains_rc_for_display(self):
        stable = release()
        data = {"releases": [
            {"moniker": "mainline", "version": "7.3-rc6", "source": "https://git.kernel.org/snapshot.tar.gz", "pgp": None},
            {"moniker": "longterm", "version": "6.18.55", "source": release("6.18.55").source,
             "pgp": release("6.18.55").signature},
            {"moniker": "stable", "version": stable.version, "source": stable.source,
             "pgp": stable.signature, "released": {"isodate": "2026-10-03"}},
            {"moniker": "linux-next", "version": "next-20261005", "source": None},
        ]}
        releases = parse_releases(data)
        self.assertEqual(releases[0].version, stable.version)
        self.assertEqual(len(releases), 3)
        self.assertFalse(next(r for r in releases if r.is_rc).buildable)

    def test_malformed_feed_and_unofficial_sources_are_rejected(self):
        for data in ([], {}, {"releases": []}, {"releases": ["bad"]}):
            with self.subTest(data=data), self.assertRaises(KernelError):
                parse_releases(data)
        good = release()
        for source in (good.source.replace("cdn.kernel.org", "evil.example"),
                       good.source.replace("https:", "http:"),
                       good.source + "?redirect=evil", good.source.replace("7.2.9.tar", "7.2.8.tar"),
                       good.source.replace("cdn.kernel.org", "cdn.kernel.org:broken")):
            with self.subTest(source=source), self.assertRaises(KernelError):
                Release(good.version, good.channel, good.date, source, good.signature).validate()

    def test_version_order_and_cpu_choices(self):
        self.assertGreater(version_key("7.2.10"), version_key("7.2.9"))
        self.assertGreater(version_key("7.3"), version_key("7.3-rc6"))
        self.assertGreater(version_key("7.2.9"), version_key("7.0.0-38-generic"))
        self.assertEqual(job_choices(6), [1, 2, 4, 6])
        self.assertEqual(job_choices(16), [1, 2, 4, 8, 10, 12, 16])
        release("10.0").validate()

    def test_archive_can_build_retired_releases_but_never_release_candidates(self):
        source = release("5.4.1")
        retired = Release(source.version, "longterm", source.date, source.source, source.signature, eol=True)
        self.assertFalse(retired.buildable)
        with self.assertRaises(KernelError):
            retired.validate()
        archived = Release(source.version, "archive", source.date, source.source, source.signature, eol=True)
        archived.validate()
        self.assertIn("Archiv", archived.label)
        rc = release("7.3-rc6", "archive")
        self.assertFalse(rc.buildable)
        with self.assertRaises(KernelError):
            rc.validate()

    def test_latest_boot_config_is_natural_version_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("config-7.2.8", "config-7.2.10", "config-7.2.9"):
                (root / name).write_text("CONFIG_TEST=y")
            self.assertEqual(boot_configs(root)[0].name, "config-7.2.10")

    def test_release_comparison_ignores_local_suffix_and_preserves_rc(self):
        for running in ("7.2.9-pmk", "7.2.9-38-generic", "7.2.9-custom.2", "7.2.9+deb13-amd64"):
            with self.subTest(running=running):
                self.assertEqual(version_key(upstream_version(running)), version_key("7.2.9"))
        self.assertLess(version_key(upstream_version("7.3-rc6-custom")), version_key("7.3"))
        self.assertLess(version_key(upstream_version("7.0.0-38-generic")), version_key("7.2.9"))

    def test_options_reject_cpu_oversubscription_and_relative_output(self):
        with tempfile.TemporaryDirectory() as tmp, patch("pimpmykernel.models.cpu_count", return_value=8):
            config = Path(tmp) / "config"
            config.write_text("CONFIG_TEST=y")
            for output, build, packaging in ((Path(tmp), 16, 8), (Path(tmp), 8, 10), (Path("relative"), 4, 4)):
                with self.subTest(output=output, build=build, packaging=packaging), self.assertRaises(KernelError):
                    BuildOptions(release(), config, output, build, packaging).validate()


class Response(io.BytesIO):
    def __init__(self, content, length=None, url="https://cdn.kernel.org/archive"):
        super().__init__(content)
        self.headers = {"Content-Length": str(length if length is not None else len(content))}
        self.url = url

    def geturl(self):
        return self.url


class NetworkTests(GermanTestCase):
    def test_archive_index_requires_matching_signed_archives_and_natural_order(self):
        html = archive_index("7.2.3", "7.2.10", "7.2.8", "6.1.1", "7.3-rc6") + "\n" + "\n".join([
            '<a href="linux-7.2.7.tar.xz">unsigned</a>',
            '<a href="linux-7.2.6.tar.sign">signature only</a>',
            '<a href="https://evil.example/linux-7.2.5.tar.xz">foreign</a>',
            '<a href="https://evil.example/linux-7.2.5.tar.sign">foreign</a>',
            '<a href="../linux-7.2.4.tar.xz">nested</a>',
            '<a href="../linux-7.2.4.tar.sign">nested</a>',
            '<a href="linux-7.2.8.tar.xz">duplicate</a>',
        ])
        releases = parse_archive_releases(html, 7)
        self.assertEqual([r.version for r in releases], ["7.2.10", "7.2.8", "7.2.3"])
        for item in releases:
            item.validate()
            self.assertEqual(item.channel, "archive")
            self.assertEqual(item.date, "2026-10-05")

    def test_empty_or_unsigned_archive_index_is_rejected(self):
        for html in ("", '<a href="linux-7.2.8.tar.xz">unsigned</a>', archive_index("6.1.1")):
            with self.subTest(html=html), self.assertRaises(KernelError):
                parse_archive_releases(html, 7)

    def test_archive_dates_can_be_missing_or_invalid(self):
        for text in (archive_index("7.2.8").replace("05-Oct-2026", "31-Feb-2026"),
                     archive_index("7.2.8").replace("05-Oct-2026", "")):
            with self.subTest(text=text):
                self.assertEqual(parse_archive_releases(text, 7)[0].date, "")

    def test_archive_discovers_all_supported_series_including_future_major(self):
        majors = (4, 5, 6, 7, 10)
        root = "\n".join(f'<a href="v{major}.x/">series</a>' for major in majors) + "\n" + "\n".join([
            '<a href="v3.x/">old</a>', '<a href="v07.x/">invalid</a>',
            '<a href="https://evil.example/v8.x/">foreign</a>',
            '<a href="../v9.x/">nested</a>', '<a href="v5.x/">duplicate</a>',
        ])
        pages = {ARCHIVE_ROOT: root.encode()}
        pages.update({f"{ARCHIVE_ROOT}v{major}.x/": archive_index(f"{major}.1.1", f"{major}.1.3").encode()
                      for major in majors})
        network = Network()
        network.read = Mock(side_effect=lambda url, **_: pages[url])
        releases = network.archive_releases()
        self.assertEqual([r.version for r in releases],
                         [f"{major}.1.{point}" for major in reversed(majors) for point in (3, 1)])
        self.assertEqual(network.read.call_count, len(majors) + 1)
        self.assertEqual(network.read.call_args_list[1].args, (ARCHIVE_ROOT + "v10.x/",))

    def test_archive_cancellation_stops_before_reading_series(self):
        event = threading.Event()
        network = Network(event)

        def root(_):
            event.set()
            return b'<a href="v7.x/">series</a>'

        network.read = Mock(side_effect=root)
        with self.assertRaises(Cancelled):
            network.archive_releases()
        network.read.assert_called_once_with(ARCHIVE_ROOT)

    def test_archive_rejects_invalid_root_and_does_not_return_partial_catalog(self):
        network = Network()
        for data in (b'<a href="v3.x/">old</a>', b"\xff"):
            with self.subTest(data=data):
                network.read = Mock(return_value=data)
                with self.assertRaises(KernelError):
                    network.archive_releases()
        network.read = Mock(side_effect=[b'<a href="v7.x/">7</a><a href="v6.x/">6</a>',
                                         archive_index("7.2.8").encode(), KernelError("network failed")])
        with self.assertRaisesRegex(KernelError, "network failed"):
            network.archive_releases()
        self.assertEqual(network.read.call_count, 3)

    def test_download_commits_only_complete_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            network = Network()
            network.opener = Mock()
            network.opener.open.return_value = Response(b"test-archive")
            target = Path(tmp) / "source.tar.xz"
            progress = []
            network.download("https://cdn.kernel.org/archive", target, lambda *p: progress.append(p))
            self.assertEqual(target.read_bytes(), b"test-archive")
            self.assertEqual(progress[-1], (12, 12))
            self.assertFalse(target.with_name(target.name + ".part").exists())

    def test_partial_download_and_size_limit_clean_temporary_files(self):
        for length, limit in ((20, 100), (1000, 100)):
            with self.subTest(length=length), tempfile.TemporaryDirectory() as tmp:
                network = Network()
                network.opener = Mock()
                network.opener.open.return_value = Response(b"short", length)
                target = Path(tmp) / "archive"
                with self.assertRaises(KernelError):
                    network.download("https://cdn.kernel.org/archive", target, lambda *_: None, limit=limit)
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_download_without_content_length_reports_a_committed_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            network = Network()
            network.opener = Mock()
            response = Response(b"test-archive")
            response.headers.clear()
            network.opener.open.return_value = response
            target = Path(tmp) / "archive"
            progress = []

            def report(current, total):
                progress.append((current, total, target.exists()))

            network.download("https://cdn.kernel.org/archive", target, report)
            self.assertEqual(progress[0], (12, 0, False))
            self.assertEqual(progress[-1], (12, 12, True))
            self.assertEqual(target.read_bytes(), b"test-archive")

    def test_cancel_prevents_connection(self):
        event = threading.Event()
        event.set()
        network = Network(event)
        network.opener = Mock()
        with self.assertRaises(Cancelled):
            network.read("https://www.kernel.org/releases.json")
        network.opener.open.assert_not_called()

    def test_redirect_and_credentials_cannot_leave_official_https_hosts(self):
        for url in ("http://cdn.kernel.org/foo", "https://evil.example/foo",
                    "https://www.kernel.org.evil.example/foo", "https://user@kernel.org/foo",
                    "https://kernel.org:8443/foo"):
            with self.subTest(url=url), self.assertRaises(KernelError):
                validate_url(url)
        with self.assertRaises(KernelError):
            OfficialRedirectHandler().redirect_request(None, None, 302, "", {}, "https://evil.example/archive")


if __name__ == "__main__":
    unittest.main()
