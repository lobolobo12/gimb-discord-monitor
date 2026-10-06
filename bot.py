"""A local, daily intranet monitor. No Discord gateway or privileged intents needed."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import logging
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from bs4 import BeautifulSoup, Comment
from dotenv import dotenv_values, load_dotenv, set_key
from filelock import FileLock, Timeout
from soupsieve import SelectorSyntaxError

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE = DATA / "state.json"
INTRANET = "https://www.gimb.org/intranet/"
HOSTS = {"www.gimb.org", "gimb.org"}
LOG = logging.getLogger("gimb")
AGENT_NAME = "org.gimb.discord-monitor"
DEFAULT_IGNORE = 'a[href*="/nadomescanja/"], a[href*="selitve" i], a[href*="jedilnik" i]'


class MonitorError(Exception):
    """A user-facing error, deliberately containing no credentials or HTTP bodies."""


def school_url(url: str) -> str:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in HOSTS
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        raise MonitorError("School URLs must use HTTPS on gimb.org.")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


@dataclass
class Config:
    email: str = ""
    password: str = ""
    webhook: str = ""
    token: str = ""
    channel: str = ""
    check_time: str = "18:00"
    timezone: str = "Europe/Ljubljana"
    urls: tuple[str, ...] = (INTRANET,)
    selector: str = ""
    ignore: str = DEFAULT_IGNORE
    follow_links: bool = True
    link_depth: int = 2
    max_pages: int = 25
    track_files: bool = True
    max_files: int = 50
    school_class: str = ""
    substitutions: bool = False
    request_delay: float = 0.5

    @classmethod
    def load(cls) -> Config:
        load_dotenv(ROOT / ".env")
        config = cls(
            email=os.getenv("GIMB_EMAIL", ""),
            password=os.getenv("GIMB_PASSWORD", ""),
            webhook=os.getenv("DISCORD_WEBHOOK_URL", ""),
            token=os.getenv("DISCORD_BOT_TOKEN", ""),
            channel=os.getenv("DISCORD_CHANNEL_ID", ""),
            check_time=os.getenv("CHECK_TIME", "18:00"),
            timezone=os.getenv("TIMEZONE", "Europe/Ljubljana"),
            urls=tuple(
                dict.fromkeys(
                    school_url(u.strip())
                    for u in os.getenv("WATCH_URLS", INTRANET).split(",")
                    if u.strip()
                )
            ),
            selector=os.getenv("CONTENT_SELECTOR", ""),
            ignore=os.getenv("IGNORE_SELECTORS", "") or DEFAULT_IGNORE,
            follow_links=os.getenv("FOLLOW_LINKS", "true").lower() == "true",
            track_files=os.getenv("TRACK_FILES", "true").lower() == "true",
            school_class=os.getenv("SCHOOL_CLASS", ""),
            substitutions=os.getenv("TRACK_SUBSTITUTIONS", "false").lower() == "true",
        )
        try:
            config.link_depth = int(os.getenv("LINK_DEPTH", "2"))
            config.max_pages = int(os.getenv("MAX_PAGES", "25"))
            config.max_files = int(os.getenv("MAX_FILES", "50"))
            if not (
                0 <= config.link_depth <= 3
                and len(config.urls) <= config.max_pages <= 100
                and 1 <= config.max_files <= 100
            ):
                raise ValueError()
        except ValueError:
            raise MonitorError("Use LINK_DEPTH=0..3, MAX_PAGES=1..100, MAX_FILES=1..100.")
        if not config.urls:
            raise MonitorError("WATCH_URLS must contain at least one school page.")
        if config.substitutions and not config.school_class.strip():
            raise MonitorError("Set SCHOOL_CLASS before enabling substitutions.")
        try:
            if not re.fullmatch(r"\d{2}:\d{2}", config.check_time):
                raise ValueError()
            clock_time.fromisoformat(config.check_time)
            ZoneInfo(config.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise MonitorError("Use CHECK_TIME=18:00 and a valid TIMEZONE, e.g. Europe/Ljubljana.")
        return config

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.timezone))


def private_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_state(path: Path = STATE) -> dict:
    if not path.exists():
        return {"version": 1, "pages": {}, "outbox": [], "last_success": None}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if (
            state["version"] != 1
            or not isinstance(state["pages"], dict)
            or not isinstance(state["outbox"], list)
        ):
            raise ValueError()
        for page in state["pages"].values():
            if not isinstance(page["lines"], list) or not all(
                isinstance(line, str) for line in page["lines"]
            ):
                raise ValueError()
        if state.get("last_success"):
            datetime.fromisoformat(state["last_success"])
        return state
    except (ValueError, KeyError, TypeError):
        raise MonitorError(
            "Saved state is invalid. Keep data/state.json for recovery; do not reset it blindly."
        )


def save_state(state: dict, path: Path = STATE) -> None:
    private_write(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def login_page(html: str, url: str) -> bool:
    return (
        urlsplit(url).path.rstrip("/") in {"/prijava", "/en/login", "/wp-login.php"}
        or BeautifulSoup(html, "html.parser").select_one(
            '.gb-login-form, form input[type="password"]'
        )
        is not None
    )


class School:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "GIMB-Intranet-Monitor/0.1 (personal daily checker)"

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        # Follow redirects ourselves: never send a school password/cookie to another origin.
        for _ in range(6):
            school_url(url)
            try:
                response = self.session.request(
                    method, url, timeout=(10, 35), allow_redirects=False, **kwargs
                )
            except requests.RequestException:
                raise MonitorError(
                    "Could not reach the school website. The saved snapshot is unchanged."
                )
            if response.is_redirect:
                response.close()
                url = school_url(urljoin(url, response.headers["Location"]))
                target = urlsplit(url)
                is_login = target.path.rstrip("/") in {"/prijava", "/en/login", "/wp-login.php"}
                if not is_login and not linked_url(url, url):
                    raise MonitorError(
                        "Refusing a redirect to an unrelated or account-action page."
                    )
                if is_login and any(k in {"action", "logout"} for k, _ in parse_qsl(target.query)):
                    raise MonitorError("Refusing an account-action redirect.")
                if response.status_code in {301, 302, 303}:
                    method, kwargs = "GET", {}
                continue
            if response.status_code != 200:
                response.close()
                raise MonitorError(f"School website returned HTTP {response.status_code}.")
            return response
        raise MonitorError("The school website redirected too many times.")

    def fetch(self, url: str) -> str:
        response = self.request("GET", url)
        if login_page(response.text, response.url):
            if not self.config.email or not self.config.password:
                raise MonitorError(
                    "School login required. Run ./Setup.command to enter it locally."
                )
            soup = BeautifulSoup(response.text, "html.parser")
            form = soup.select_one("form.gb-login-form")
            if not form:
                raise MonitorError("The school login form changed; its parser needs updating.")
            fields = {
                i["name"]: i.get("value", "") for i in form.select('input[type="hidden"][name]')
            }
            if "gimbezigrad_login_nonce" not in fields:
                raise MonitorError("School login nonce is missing. Try again later.")
            fields.update(
                log=self.config.email,
                pwd=self.config.password,
                redirect_to=url,
                gimbezigrad_login_submit="1",
                rememberme="forever",
            )
            action = school_url(urljoin(response.url, form.get("action", response.url)))
            self.request("POST", action, data=fields)
            response = self.request("GET", url)
            if login_page(response.text, response.url):
                raise MonitorError(
                    "School login failed or expired. Run ./Setup.command to check your credentials."
                )
        if urlsplit(response.url).path.rstrip("/") != urlsplit(url).path.rstrip("/"):
            raise MonitorError(
                "The school redirected away from the watched page. Snapshot preserved."
            )
        if "text/html" not in response.headers.get("Content-Type", ""):
            raise MonitorError("A watched URL did not return an HTML page.")
        return response.text

    def file_snapshot(self, url: str, label: str) -> dict:
        response = self.request("GET", url, stream=True)
        try:
            if "text/html" in response.headers.get("Content-Type", ""):
                raise MonitorError("A document returned an HTML/login page; snapshot preserved.")
            digest = hashlib.sha256()
            size = 0
            prefix = b""
            for chunk in response.iter_content(65536):
                prefix = (prefix + chunk)[:5]
                size += len(chunk)
                if size > 20 * 1024 * 1024:
                    raise MonitorError("Document exceeds the 20 MB download limit.")
                digest.update(chunk)
            if not size or (urlsplit(url).path.lower().endswith(".pdf") and prefix != b"%PDF-"):
                raise MonitorError("Document is empty or not a valid PDF response.")
            return {
                "title": label,
                "lines": [label],
                "kind": "file",
                "sha256": digest.hexdigest(),
                "bytes": size,
            }
        except requests.RequestException:
            raise MonitorError("Document download failed; snapshot preserved.")
        finally:
            response.close()


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).replace("\u200b", "").split())


def linked_url(href: str, base: str) -> tuple[str, str] | None:
    """Accept content pages/documents and the one known substitutions service."""
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return None
    try:
        parts = urlsplit(urljoin(base, href.strip()))
        if (
            parts.scheme != "https"
            or parts.username
            or parts.password
            or parts.port not in (None, 443)
        ):
            return None
        if parts.hostname == "urniki.easistent.com":
            if re.fullmatch(r"/nadomescanja/[a-f0-9]{40}/?", parts.path) and not parts.query:
                return urlunsplit(
                    ("https", parts.netloc, parts.path.rstrip("/"), "", "")
                ), "substitutions"
            return None
        if parts.hostname not in HOSTS:
            return None
        path = unquote(unquote(parts.path)).lower()
        if (
            re.search(
                r"(?:^|/)(?:wp-admin|wp-json|wp-includes|prijava|odjava|spremeni-geslo|pozabljeno-geslo|"
                r"login|logout|account|profile|profil|nastavitve|delete|remove|unsubscribe)(?:/|$)",
                path,
            )
            or path in {"/", "/obvestila/", "/novice/", "/dogodki/"}
            or any(x in path for x in ("wp-login.php", "politika-", "izjava-o-", "pogoji-uporabe"))
        ):
            return None
        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if key.startswith("utm_") or key in {"fbclid", "gclid"}:
                continue
            if key not in {"p", "page_id"} or not value.isdigit():
                return None
            query.append((key, value))
        suffix = Path(path).suffix
        if suffix in {".pdf", ".doc", ".docx", ".xls", ".xlsx"}:
            kind = "file"
        elif suffix not in {"", ".html", ".htm"} or "/wp-content/" in path:
            return None
        else:
            kind = "page"
        return urlunsplit(("https", parts.netloc, parts.path, urlencode(sorted(query)), "")), kind
    except ValueError:
        return None


def extract(html: str, url: str, config: Config) -> dict:
    if login_page(html, url):
        raise MonitorError("Refusing to save a login page as school news.")
    soup = BeautifulSoup(html, "html.parser")
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()
    # The site's H1 is often just "Obvestila"; the document title identifies the actual notice.
    title = normalize(
        soup.title.get_text(" ").split(" – Gimnazija Bežigrad")[0]
        if soup.title
        else soup.h1.get_text(" ")
        if soup.h1
        else "GIMB intranet"
    )
    remove = (
        "script, style, noscript, header, footer, nav, form, iframe, "
        '[hidden], [aria-hidden="true"], #wpadminbar, .screen-reader-text, '
        '[data-elementor-type="header"], [data-elementor-type="footer"], '
        ".cmplz-cookiebanner, #cmplz-manage-consent, #blz-menu, #blz-toggle"
        ", .gimb-account-navigation, .gimb-post-heading-menu"
    )
    for node in soup.select(remove):
        node.decompose()
    try:
        if config.ignore:
            for node in soup.select(config.ignore):
                node.decompose()
        if config.selector:
            roots = soup.select(config.selector)
            if not roots:
                raise MonitorError("CONTENT_SELECTOR matched nothing. Snapshot preserved.")
        else:
            # Prefer a semantic content region, but allow WordPress's bare page template.
            root = next(
                (
                    soup.select_one(s)
                    for s in (
                        ".elementor-widget-theme-post-content",
                        "main",
                        '[role="main"]',
                        ".entry-content",
                        "#content",
                    )
                    if soup.select_one(s)
                ),
                soup.body,
            )
            roots = [root] if root else []
    except MonitorError:
        raise
    except SelectorSyntaxError:
        raise MonitorError("Invalid CONTENT_SELECTOR or IGNORE_SELECTORS.")
    if not roots:
        raise MonitorError("Could not identify the page content. Snapshot preserved.")
    lines, links = [], {}
    for root in roots:
        for anchor in list(root.select("a[href]")):
            href = anchor.get("href", "")
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            link = urljoin(url, href)
            if urlsplit(link).scheme not in {"https", "http"}:
                continue
            if any(
                t in link.lower() for t in ("logout", "odjava", "wp-login.php", "spremeni-geslo")
            ):
                anchor.decompose()
                continue
            target = linked_url(href, url)
            if target:
                canonical, kind = target
                links[canonical] = {
                    "url": canonical,
                    "kind": kind,
                    "label": normalize(anchor.get_text(" ")) or Path(urlsplit(canonical).path).name,
                }
            anchor.append(f" <{link}>")
        for img in root.select("img[src]"):
            img.replace_with(
                f"[Slika: {normalize(img.get('alt', ''))}] <{urljoin(url, img['src'])}>"
            )
        # HTML source wrapping and inline tags must not appear as content edits.
        for node in list(root.find_all(string=True)):
            node.replace_with(re.sub(r"\s+", " ", str(node)))
        for br in root.select("br"):
            br.replace_with("\n")
        for block in root.select("p, h1, h2, h3, h4, h5, h6, div, section, article, li, tr"):
            block.insert_before("\n")
            block.insert_after("\n")
        lines.extend(
            line for value in root.get_text(" ").splitlines() if (line := normalize(value))
        )
    if len(" ".join(lines)) < 30:
        raise MonitorError("The page content looks empty; refusing to overwrite the snapshot.")
    return {"title": title, "lines": lines, "links": list(links.values())}


def escape(text: str) -> str:
    return re.sub(r"([\\`*_~|>])", r"\\\1", text).replace("@", "@\u200b")


def change_message(old: dict, new: dict, url: str, date: str) -> str | None:
    if new.get("kind") == "file":
        if old.get("sha256") == new["sha256"]:
            return None
        return (
            f"📄 **GIMB — posodobljen dokument** · {date}\n"
            f"**{escape(new['title'])[:250]}**\n"
            f"Datoteka na isti povezavi je bila spremenjena ({new['bytes']:,} bajtov).\n"
            f"Odpri dokument: <{url}>"
        )
    # Ignore harmless reordering and identical repeated elements.
    if set(old["lines"]) == set(new["lines"]):
        return None
    # Only report newly added content; removals alone stay silent.
    old_lines = set(old["lines"])
    added = list(dict.fromkeys(line for line in new["lines"] if line not in old_lines))
    if not added:
        return None
    header = f"🏫 **GIMB — novo na intranetu** · {date}\n**{escape(new['title'])[:180]}**\n"
    text = header
    for label, values in (("Novo", added),):
        text += f"\n**{label}:**\n"
        for value in values[:8]:
            piece = "• " + escape(value)[:220] + ("…" if len(escape(value)) > 220 else "") + "\n"
            if len(text) + len(piece) > 1450:
                text += "• Več sprememb je na izvorni strani.\n"
                break
            text += piece
        if len(values) > 8:
            text += f"• Še {len(values) - 8} spremenjenih vrstic.\n"
    return text[:1650] + f"\nOdpri stran: <{url}>"


def class_key(value: str) -> str:
    return re.sub(r"[\W_]", "", value, flags=re.UNICODE).upper()


def parse_substitutions(html: str, school_class: str, date: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    table = next(
        (
            t
            for t in soup.select("table")
            if [normalize(th.get_text()) for th in t.select("thead th")]
            == ["Razred", "Ura", "Nadomešča", "Učilnica", "Opombe"]
        ),
        None,
    )
    lines = []
    if table:
        current, remaining = "", 0
        for row in table.select("tr"):
            cells = row.find_all("td", recursive=False)
            if not cells:
                continue
            if len(cells) == 5:
                current = class_key(cells[0].get_text())
                try:
                    remaining = int(cells[0].get("rowspan", "1"))
                except ValueError:
                    raise MonitorError("The substitutions table has an invalid row span.")
                cells = cells[1:]
            elif len(cells) != 4 or remaining <= 0:
                raise MonitorError("The substitutions table layout changed; snapshot preserved.")
            if current == class_key(school_class):
                values = [normalize(c.get_text(" ")) for c in cells]
                lines.append(
                    f"{date} · {school_class} · ura {values[0]} · {values[1]} · "
                    f"učilnica {values[2]} · opombe: {values[3]}"
                )
            remaining -= 1
    else:
        text = normalize(soup.get_text(" ")).lower()
        if not any(
            marker in text
            for marker in (
                "ni nadomeščanj",
                "ni vnesenih nadomeščanj",
                "ni objavljenih nadomeščanj",
            )
        ):
            raise MonitorError("Could not verify the substitutions table or empty-day message.")
    return {
        "title": f"Nadomeščanja {school_class} — {date}",
        "lines": lines,
        "date": date,
        "kind": "substitutions",
        "notify_new": bool(lines),
    }


def fetch_substitutions(base: str, config: Config) -> dict[str, dict]:
    if not (target := linked_url(base, base)) or target[1] != "substitutions":
        raise MonitorError("Unrecognized substitutions source.")
    pages = {}
    day = config.now().date()
    # At 18:00 this includes tomorrow and several upcoming school days.
    while len(pages) < 5:
        if day.weekday() < 5:
            url = f"{base}/{day.year}-{day.month}-{day.day}/seznam"
            time.sleep(config.request_delay)
            try:
                # A separate unauthenticated request: never send school credentials to eAsistent.
                response = requests.get(url, timeout=(10, 35), allow_redirects=False)
                if response.status_code != 200:
                    raise MonitorError(
                        f"Substitutions service returned HTTP {response.status_code}."
                    )
                payload = response.json()
                html = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(html, str):
                    raise MonitorError("Unexpected substitutions response; snapshots preserved.")
                pages[url] = parse_substitutions(html, config.school_class, day.isoformat())
                pages[url]["source_url"] = base
            except (requests.RequestException, ValueError):
                raise MonitorError("Could not read the substitutions service; snapshots preserved.")
        day += timedelta(days=1)
    return pages


def scan(config: Config, school: School) -> tuple[dict, dict]:
    pages, errors = {}, {}
    queue = deque((u, 0) for u in config.urls)
    seen = set(config.urls)
    files, substitutions = {}, set()
    report = {"errors": errors, "page_limit_reached": False, "file_limit_reached": False}
    attempts = 0
    while queue:
        url, depth = queue.popleft()
        if attempts:
            time.sleep(config.request_delay)
        attempts += 1
        try:
            # The custom seed selector need not exist on a linked article's template.
            page_config = config if depth == 0 else replace(config, selector="")
            page = extract(school.fetch(url), url, page_config)
            pages[url] = page
            LOG.info("Read %s: %s", "seed" if depth == 0 else "linked page", page["title"])
        except MonitorError as error:
            if depth == 0:
                raise
            errors[url] = str(error)
            LOG.warning("Skipped linked page: %s", error)
            continue
        for link in page["links"]:
            kind, target = link["kind"], link["url"]
            # Track documents directly on the dashboard and its detail pages.
            # Deeper general forms/agreements remain visible as links, not downloads.
            if kind == "file" and config.track_files and depth <= 1:
                if target not in files and len(files) >= config.max_files:
                    report["file_limit_reached"] = True
                elif target not in files:
                    files[target] = link["label"]
            elif kind == "substitutions" and config.substitutions:
                substitutions.add(target)
            elif (
                kind == "page"
                and config.follow_links
                and depth < config.link_depth
                and target not in seen
            ):
                if len(seen) >= config.max_pages:
                    report["page_limit_reached"] = True
                    continue
                seen.add(target)
                queue.append((target, depth + 1))
    for url, label in files.items():
        time.sleep(config.request_delay)
        try:
            pages[url] = school.file_snapshot(url, label)
        except MonitorError as error:
            errors[url] = str(error)
            LOG.warning("Skipped document %s: %s", label, error)
    for base in sorted(substitutions):
        try:
            pages.update(fetch_substitutions(base, config))
        except MonitorError as error:
            errors[base] = str(error)
            LOG.warning("Skipped substitutions: %s", error)
    if report["page_limit_reached"] or report["file_limit_reached"]:
        LOG.warning("Scan limit reached; review MAX_PAGES/MAX_FILES in .env.")
    report["counts"] = {
        kind: sum(p.get("kind", "page") == kind for p in pages.values())
        for kind in ("page", "file", "substitutions")
    }
    return pages, report


class Discord:
    def __init__(self, config: Config):
        self.config = config
        if config.webhook:
            parsed = urlsplit(config.webhook)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in {"discord.com", "discordapp.com"}
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
                or not re.fullmatch(r"/api(?:/v\d+)?/webhooks/\d+/[\w.-]+", parsed.path)
            ):
                raise MonitorError("DISCORD_WEBHOOK_URL must be a Discord incoming webhook URL.")
            self.url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "wait=true", ""))
            self.headers = {}
        elif config.token and config.channel.isdigit():
            self.url = f"https://discord.com/api/v10/channels/{config.channel}/messages"
            self.headers = {"Authorization": f"Bot {config.token}"}
        else:
            raise MonitorError(
                "Configure a Discord webhook, or a bot token and channel ID, using ./Setup.command."
            )

    def destination(self) -> str:
        # Persist a fingerprint, never credentials, to avoid sending queued notices to a changed channel.
        identity = self.url if self.config.webhook else self.config.channel
        return hashlib.sha256(identity.encode()).hexdigest()

    def send(self, content: str, nonce: str = "") -> None:
        payload = {"content": content, "allowed_mentions": {"parse": []}, "flags": 4}
        if self.config.webhook:
            payload["username"] = "GIMB obvestila"
        elif nonce:
            payload.update(nonce=nonce, enforce_nonce=True)
        for _ in range(4):
            try:
                response = requests.post(
                    self.url,
                    headers=self.headers,
                    json=payload,
                    timeout=(10, 35),
                    allow_redirects=False,
                )
            except requests.RequestException:
                raise MonitorError("Discord could not confirm delivery. The notice remains queued.")
            if response.status_code in {200, 201}:
                return
            if response.status_code == 429:
                try:
                    delay = float(response.json()["retry_after"])
                    if not 0 <= delay <= 60:
                        break
                    time.sleep(delay + 0.1)
                except (ValueError, KeyError, TypeError):
                    break
                continue
            raise MonitorError(
                f"Discord returned HTTP {response.status_code}; notices remain queued. "
                "Check the token/webhook and channel permissions."
            )
        raise MonitorError(
            "Discord rate limit reached; notices remain queued for the next attempt."
        )


def flush(state: dict, sender: Discord, path: Path) -> None:
    while state["outbox"]:
        item = state["outbox"][0]
        if item["destination"] != sender.destination():
            raise MonitorError(
                "Discord destination changed while notices were queued. "
                "Restore the old destination or review data/state.json before retrying."
            )
        sender.send(item["content"], item["nonce"])
        state["outbox"].pop(0)
        save_state(state, path)


def check(
    config: Config, *, preview: bool = False, path: Path = STATE, school=None, sender=None
) -> None:
    state = load_state(path)
    sender = sender or (None if preview else Discord(config))
    if not preview:
        flush(state, sender, path)
    school = school or School(config)
    now = config.now()
    pages, report = scan(config, school)
    messages = []
    for url, page in pages.items():
        old = state["pages"].get(url)
        if old is None and state["pages"] and page.get("notify_new"):
            old = {"lines": []}
        if old is not None:
            message = change_message(
                old, page, page.get("source_url", url), now.strftime("%d. %m. %Y")
            )
            if message:
                messages.append(message)
    if preview:
        for url, page in pages.items():
            print(f"\n--- {page['title']} | {url} ---\n" + "\n".join(page["lines"][:60]))
        print(f"\nPreview: {len(messages)} change alert(s). No messages sent or state saved.")
        print("Sources:", report["counts"], "Skipped:", len(report["errors"]))
        if not state["pages"]:
            print("The first real check will save a baseline silently.")
        return
    for message in messages:
        state["outbox"].append(
            {
                "content": message,
                "destination": sender.destination(),
                "nonce": hashlib.sha256((now.isoformat() + message).encode()).hexdigest()[:24],
            }
        )
    # Save both new snapshots and an outbox atomically before trying delivery.
    state["pages"].update(pages)
    state["last_scan"] = report
    # Date-specific timetable snapshots do not need to grow forever.
    cutoff = (now.date() - timedelta(days=14)).isoformat()
    state["pages"] = {
        u: p
        for u, p in state["pages"].items()
        if p.get("kind") != "substitutions" or p.get("date", "9999") >= cutoff
    }
    state["last_success"] = now.isoformat()
    save_state(state, path)
    flush(state, sender, path)
    LOG.info("Check complete: %d change alert(s); snapshot saved.", len(messages))


def due(config: Config, state: dict, now: datetime | None = None) -> bool:
    now = now or config.now()
    if state["outbox"] or not state.get("last_success"):
        return True
    last_date = (
        datetime.fromisoformat(state["last_success"]).astimezone(ZoneInfo(config.timezone)).date()
    )
    return last_date < now.date() and now.time() >= clock_time.fromisoformat(config.check_time)


def scheduled(config: Config) -> None:
    state = load_state()
    if due(config, state):
        # Avoid hitting the site every five minutes during an outage or invalid login.
        last_attempt = state.get("last_attempt")
        if (
            last_attempt
            and (config.now() - datetime.fromisoformat(last_attempt)).total_seconds() < 900
        ):
            return
        state["last_attempt"] = config.now().isoformat()
        save_state(state)
        if state["outbox"]:
            flush(state, Discord(config), STATE)
            if not due(config, state):
                return
        check(config)


def install_agent() -> None:
    if sys.platform != "darwin":
        raise MonitorError(
            "Automatic background installation is currently for macOS. Use 'run' elsewhere."
        )
    if sys.prefix == sys.base_prefix:
        raise MonitorError(
            "Run this command with .venv/bin/python so dependencies remain available."
        )
    config = Config.load()
    if not config.email or not config.password:
        raise MonitorError("Run setup before installing the background job.")
    (ROOT / "logs").mkdir(exist_ok=True, mode=0o700)
    agent = Path.home() / "Library/LaunchAgents" / f"{AGENT_NAME}.plist"
    values = {
        "Label": AGENT_NAME,
        "ProgramArguments": [sys.executable, str(ROOT / "bot.py"), "scheduled"],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "StartInterval": 300,
        "StandardOutPath": str(ROOT / "logs/monitor.log"),
        "StandardErrorPath": str(ROOT / "logs/monitor.log"),
        "Umask": 0o077,
    }
    private_write(agent, plistlib.dumps(values).decode())
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", f"{domain}/{AGENT_NAME}"], capture_output=True, check=False
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(agent)], capture_output=True, check=False
    )
    if result.returncode:
        raise MonitorError(
            "macOS could not load the background job. Try again from a logged-in Terminal."
        )
    print("Installed background checks. Logs: " + str(ROOT / "logs/monitor.log"))


def setup() -> None:
    print(
        "GIMB → Discord setup\nCredentials are entered here, never in chat.\n"
        "They are stored in this project's private .env file.\n"
    )
    existing = dotenv_values(ROOT / ".env")
    email = input("School email [Enter keeps existing]: ").strip() or existing.get("GIMB_EMAIL", "")
    password = getpass.getpass("School password [hidden; Enter keeps existing]: ") or existing.get(
        "GIMB_PASSWORD", ""
    )
    choice = input("Discord: 1 = webhook (easiest), 2 = existing bot token [1]: ").strip() or "1"
    if choice not in {"1", "2"}:
        raise MonitorError("Choose 1 or 2.")
    values = {**existing, "GIMB_EMAIL": email, "GIMB_PASSWORD": password}
    if choice == "1":
        print("Discord channel → Edit Channel → Integrations → Webhooks → New Webhook → Copy URL.")
        values.update(
            DISCORD_WEBHOOK_URL=getpass.getpass("Webhook URL [hidden; Enter keeps existing]: ")
            or existing.get("DISCORD_WEBHOOK_URL", ""),
            DISCORD_BOT_TOKEN="",
            DISCORD_CHANNEL_ID="",
        )
    else:
        values.update(
            DISCORD_WEBHOOK_URL="",
            DISCORD_BOT_TOKEN=getpass.getpass("Bot token [hidden; Enter keeps existing]: ")
            or existing.get("DISCORD_BOT_TOKEN", ""),
            DISCORD_CHANNEL_ID=input("Channel ID [Enter keeps existing]: ").strip()
            or existing.get("DISCORD_CHANNEL_ID", ""),
        )
    values["CHECK_TIME"] = input("Daily check time in Ljubljana [18:00]: ").strip() or "18:00"
    values.setdefault("TIMEZONE", "Europe/Ljubljana")
    values.setdefault("WATCH_URLS", INTRANET)
    values.setdefault("FOLLOW_LINKS", "true")
    values.setdefault("TRACK_FILES", "true")
    values.setdefault("TRACK_SUBSTITUTIONS", "false")
    values.setdefault("IGNORE_SELECTORS", DEFAULT_IGNORE)
    values.setdefault("SCHOOL_CLASS", "")
    if not email or not password:
        raise MonitorError("School email and password are required.")
    env = ROOT / ".env"
    # Build a complete dotenv file privately; quotes and special characters are escaped by dotenv.
    fd, temporary = tempfile.mkstemp(dir=ROOT, prefix=".env-setup-")
    os.close(fd)
    try:
        for key, value in values.items():
            if value is not None:
                set_key(temporary, key, value, quote_mode="always")
        os.chmod(temporary, 0o600)
        os.replace(temporary, env)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    config = Config.load()
    sender = Discord(config)
    print("\nChecking school login and showing the content that will be watched…")
    check(config, preview=True)
    if input("Does the preview contain the notices you want? [y/N]: ").lower().strip() != "y":
        print("Saved settings. Adjust CONTENT_SELECTOR/WATCH_URLS in .env, then run preview again.")
        return
    if input("Send a setup test to the configured Discord channel? [y/N]: ").lower().strip() == "y":
        sender.send("🏫 GIMB obvestila: povezava deluje. Dnevno spremljanje je pripravljeno.")
        print("Discord confirmed delivery.")
    if input("Enable automatic daily checks on this Mac? [y/N]: ").lower().strip() == "y":
        install_agent()
    else:
        print("Saved settings. Start manually with: .venv/bin/python bot.py run")


def main() -> int:
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "setup",
            "preview",
            "once",
            "scheduled",
            "run",
            "status",
            "test-discord",
            "install",
        ],
    )
    args = parser.parse_args()
    DATA.mkdir(exist_ok=True, mode=0o700)
    try:
        if args.command == "setup":
            setup()
            return 0
        config = Config.load()
        if args.command == "install":
            Discord(config)
            install_agent()
            return 0
        if args.command == "test-discord":
            Discord(config).send("🏫 GIMB obvestila: testna povezava deluje.")
            print("Discord confirmed delivery.")
            return 0
        if args.command == "status":
            state = load_state()
            print(
                f"Daily check: {config.check_time} ({config.timezone})\n"
                f"Last successful fetch: {state.get('last_success') or 'never'}\n"
                f"Watched snapshots: {len(state['pages'])}\nQueued notices: {len(state['outbox'])}"
            )
            if state.get("last_scan"):
                print("Last scan sources:", state["last_scan"]["counts"])
                print("Skipped sources:", len(state["last_scan"]["errors"]))
            return 0
        if args.command == "run":
            Discord(config)
            LOG.info(
                "Running; daily check at %s %s. Ctrl+C stops it.",
                config.check_time,
                config.timezone,
            )
            while True:
                try:
                    with FileLock(str(DATA / "monitor.lock"), timeout=0):
                        scheduled(config)
                except Timeout:
                    LOG.info("Another check is already running.")
                except MonitorError as error:
                    LOG.error("%s", error)
                time.sleep(60)
        with FileLock(str(DATA / "monitor.lock"), timeout=0):
            if args.command == "scheduled":
                scheduled(config)
            else:
                check(config, preview=args.command == "preview")
    except Timeout:
        LOG.info("Another check is already running.")
    except MonitorError as error:
        LOG.error("%s", error)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
