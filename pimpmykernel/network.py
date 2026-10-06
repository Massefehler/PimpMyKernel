"""Begrenzte HTTPS-Downloads mit Abbruch und geprüftem Redirect-Ziel."""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .i18n import tr
from . import __version__
from .models import Cancelled, KernelError, Release, VERSION_RE, parse_releases, version_key


ALLOWED_HOSTS = {"www.kernel.org", "cdn.kernel.org", "kernel.org"}
ARCHIVE_ROOT = "https://cdn.kernel.org/pub/linux/kernel/"


class _ArchiveIndex(HTMLParser):
    """Links und Veröffentlichungsdaten aus dem offiziellen Verzeichnisindex."""

    def __init__(self, html: str):
        super().__init__()
        self.entries: dict[str, str] = {}
        self.current_href: str | None = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.current_href = dict(attrs).get("href")
            if self.current_href:
                self.entries.setdefault(self.current_href, "")

    def handle_data(self, text):
        if not self.current_href or self.entries.get(self.current_href):
            return
        match = re.search(r"\b(\d{2})-([A-Z][a-z]{2})-(\d{4})\b", text)
        months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
        if match:
            try:
                released = date(int(match[3]), months.index(match[2]) + 1, int(match[1]))
            except ValueError:
                return
            self.entries[self.current_href] = released.isoformat()


def parse_archive_releases(html: str, major: int) -> list[Release]:
    """Nur vollständige Release-Archive mit zugehöriger Signatur anbieten."""
    index = _ArchiveIndex(html)
    releases = []
    for filename, released in index.entries.items():
        match = re.fullmatch(r"linux-(.+)\.tar\.xz", filename)
        if not match:
            continue
        version = match[1]
        signature = f"linux-{version}.tar.sign"
        if (not VERSION_RE.fullmatch(version) or "-rc" in version
                or int(version.split(".")[0]) != major or signature not in index.entries):
            continue
        source = f"{ARCHIVE_ROOT}v{major}.x/{filename}"
        release = Release(version, "archive", released, source, source.removesuffix(".xz") + ".sign")
        release.validate()
        releases.append(release)
    if not releases:
        raise KernelError(tr('Im Archiv für Linux {major}.x wurden keine signierten Releases gefunden.', major=major))
    return sorted(releases, key=lambda release: version_key(release.version), reverse=True)


def validate_url(url: str) -> None:
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise KernelError(tr("Ungültige HTTPS-Adresse.")) from exc
    if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
            or port not in (None, 443) or parsed.username or parsed.password):
        raise KernelError(tr("Download abgelehnt: Die Adresse ist keine erlaubte HTTPS-Quelle."))


class OfficialRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Network:
    def __init__(self, cancel: threading.Event | None = None):
        self.cancel = cancel or threading.Event()
        self.opener = urllib.request.build_opener(OfficialRedirectHandler())

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled(tr("Vorgang abgebrochen."))

    def _open(self, url: str):
        self.check_cancel()
        validate_url(url)
        request = urllib.request.Request(url, headers={"User-Agent": f"PimpMyKernel/{__version__}"})
        try:
            response = self.opener.open(request, timeout=20)
            validate_url(response.geturl())
            return response
        except (OSError, urllib.error.URLError) as exc:
            raise KernelError(tr('Die Quelle ist nicht erreichbar ({hostname}): {exc}', hostname=urlparse(url).hostname, exc=exc)) from exc

    def read(self, url: str, limit: int = 2 * 1024 * 1024) -> bytes:
        with self._open(url) as response:
            data = response.read(limit + 1)
        self.check_cancel()
        if len(data) > limit:
            raise KernelError(tr("Die Antwort des Servers ist unerwartet groß."))
        return data

    def releases(self) -> list[Release]:
        try:
            return parse_releases(json.loads(self.read("https://www.kernel.org/releases.json")))
        except (ValueError, UnicodeError) as exc:
            raise KernelError(tr("Die Release-Liste von kernel.org konnte nicht gelesen werden.")) from exc

    def archive_releases(self) -> list[Release]:
        try:
            root = _ArchiveIndex(self.read(ARCHIVE_ROOT).decode("utf-8"))
            majors = set()
            for href in root.entries:
                match = re.fullmatch(r"v([1-9]\d*)\.x/", href)
                if match and int(match[1]) >= 4:
                    majors.add(int(match[1]))
            if not majors:
                raise KernelError(tr("Das Kernel-Archiv enthält keine verfügbaren Kernelreihen."))
            releases = []
            for major in sorted(majors, reverse=True):
                self.check_cancel()
                html = self.read(f"{ARCHIVE_ROOT}v{major}.x/", limit=8 * 1024 * 1024).decode("utf-8")
                releases.extend(parse_archive_releases(html, major))
            self.check_cancel()
            return sorted(releases, key=lambda release: version_key(release.version), reverse=True)
        except UnicodeError as exc:
            raise KernelError(tr("Die Archivliste von kernel.org konnte nicht gelesen werden.")) from exc

    def download(self, url: str, path: Path, progress: Callable[[int, int], None],
                 limit: int = 512 * 1024 * 1024) -> None:
        partial = path.with_name(path.name + ".part")
        try:
            with self._open(url) as response, partial.open("xb") as target:
                try:
                    total = int(response.headers.get("Content-Length", 0))
                except ValueError:
                    total = 0
                if total > limit:
                    raise KernelError(tr("Das Archiv überschreitet die erlaubte Downloadgröße."))
                current = 0
                while True:
                    self.check_cancel()
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    current += len(chunk)
                    if current > limit:
                        raise KernelError(tr("Der Download ist unerwartet groß."))
                    target.write(chunk)
                    progress(current, total)
                if total and current != total:
                    raise KernelError(tr("Der Download ist unvollständig. Bitte erneut versuchen."))
                if not current:
                    raise KernelError(tr("Der Server hat eine leere Datei geliefert."))
            self.check_cancel()
            partial.replace(path)
            # A final known total also finishes displays for chunked responses.
            progress(current, total or current)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
