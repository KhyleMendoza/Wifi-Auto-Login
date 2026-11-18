from __future__ import annotations

import argparse
import configparser
import ctypes
import getpass
import html
import ipaddress
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

requests = None
_requests_lock = threading.Lock()


def load_requests():
    global requests
    if requests is None:
        with _requests_lock:
            if requests is None:
                try:
                    import requests as _requests
                    import urllib3
                except ImportError:
                    print("The 'requests' package is missing. Install it with:  python -m pip install requests")
                    raise
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                requests = _requests
    return requests


HERE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
JS_REDIRECT = re.compile(
    r"""(?:window\.|document\.|top\.)?location(?:\.href)?\s*=\s*["']([^"']+)["']""", re.I
)
FGTAUTH_LINK = re.compile(r"""(https?://[^"'\s<>]*fgtauth[^"'\s<>]*)""", re.I)
META_REFRESH = re.compile(r"""http-equiv=["']?refresh["']?[^>]*url=([^"'>\s]+)""", re.I)
FAIL_WORDS = ("authentication failed", "failed", "invalid", "incorrect", "denied")

NO_DNS_PROBES = ["http://1.1.1.1"]
NCSI_URL = "http://www.msftconnecttest.com/connecttest.txt"
NCSI_TEXT = "Microsoft Connect Test"
PORTAL_LOGIN_PATHS = ["/login?", "/fgtauth?"]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_log_sink = lambda line: print(line, flush=True)
_status_sink = lambda state, detail="": None
STOP = threading.Event()


class Stopped(Exception):
    pass


def set_hooks(log_fn=None, status_fn=None) -> None:
    global _log_sink, _status_sink
    if log_fn:
        _log_sink = log_fn
    if status_fn:
        _status_sink = status_fn


LOG_DIR = HERE / "logs"
_log_lock = threading.Lock()


def log_file_path() -> Path:
    return LOG_DIR / time.strftime("wifi_login_%Y-%m-%d.txt")


def cleanup_old_logs(keep_days: int = 30) -> None:
    cutoff = time.time() - keep_days * 86400
    try:
        for f in LOG_DIR.glob("wifi_login_*.txt"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
    except OSError:
        pass


def log(msg: str) -> None:
    line = time.strftime("[%H:%M:%S] ") + msg
    _log_sink(line)
    with _log_lock:
        try:
            LOG_DIR.mkdir(exist_ok=True)
            with open(log_file_path(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def status(state: str, detail: str = "") -> None:
    _status_sink(state, detail)


def wait(seconds: float) -> None:
    if STOP.wait(seconds):
        raise Stopped()


def check_stop() -> None:
    if STOP.is_set():
        raise Stopped()


def is_yes(value: str) -> bool:
    return str(value).strip().lower() in ("yes", "true", "1", "on")


PROFILE_KEYS = ("username", "password", "portal_url", "probe_urls", "success_url", "success_text")
PROFILE_PREFIX = "profile "
DEFAULT_PROBES = "http://neverssl.com"


class Settings:

    def __init__(self, path: Path):
        cp = configparser.ConfigParser(interpolation=None)
        if not cp.read(path, encoding="utf-8"):
            log(f"Config file not found: {path} - using defaults.")
        g = lambda sec, key, default="": cp.get(sec, key, fallback=default).strip()

        default = {
            "username": os.environ.get("WIFI_USER") or g("login", "username"),
            "password": os.environ.get("WIFI_PASS") or g("login", "password"),
            "portal_url": g("portal", "portal_url", "https://192.168.201.254:1003"),
            "probe_urls": g("portal", "probe_urls", DEFAULT_PROBES),
            "success_url": g("portal", "success_url"),
            "success_text": g("portal", "success_text", "Start browsing, Welcome to, L&K Industries"),
        }
        self.profiles: dict[str, dict[str, str]] = {"": default}
        for sec in cp.sections():
            if sec.lower().startswith(PROFILE_PREFIX):
                name = sec[len(PROFILE_PREFIX):].strip()
                if name:
                    self.profiles[name] = {k: cp.get(sec, k, fallback=default[k]).strip() for k in PROFILE_KEYS}

        self.wifi_target = g("wifi", "target")
        self.wifi_keep = is_yes(g("wifi", "keep_connected", "no"))

        self.tries_per_site = max(1, int(g("behavior", "tries_per_site", "10") or 10))
        self.shuffle_sites = is_yes(g("behavior", "shuffle_sites", "yes"))
        self.attempts_before_reset = int(g("behavior", "attempts_before_reset", "3") or 3)
        self.login_retries = max(1, int(g("behavior", "login_retries", "20") or 20))
        self.delay = float(g("behavior", "delay_seconds", "2") or 2)
        self.max_attempts = int(g("behavior", "max_attempts", "0") or 0)
        self.reset_network = is_yes(g("behavior", "reset_network", "yes"))
        self.fix_dns = is_yes(g("behavior", "fix_dns", "yes"))
        self.adapter = g("behavior", "adapter_name")
        self.watch_interval = float(g("behavior", "watch_interval", "30") or 30)
        self.profile_name = ""
        self.use_profile("")

    def profile_for(self, ssid: str) -> str:
        return ssid if ssid and ssid in self.profiles else ""

    def use_profile(self, ssid: str) -> str:
        name = self.profile_for(ssid)
        p = self.profiles[name]
        self.profile_name = name
        self.username = p["username"]
        self.password = p["password"]
        url = p["portal_url"].strip()
        self.portal_url = url if not url or "://" in url else "https://" + url
        probes = [self._fix(u) for u in p["probe_urls"].replace("\n", ",").split(",") if u.strip()]
        self.probe_urls = probes or [DEFAULT_PROBES]
        self.success_url = self._fix(p["success_url"]) if p["success_url"].strip() else ""
        self.success_text = [t.strip() for t in p["success_text"].split(",") if t.strip()]
        return name

    @staticmethod
    def _fix(url: str) -> str:
        url = url.strip()
        return url if "://" in url else "http://" + url


def profile_label(name: str) -> str:
    return name or "Default"


class FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms: list[dict] = []
        self._current: dict | None = None
        self.loose_inputs: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "form":
            self._current = {"action": a.get("action", ""), "method": a.get("method", "get").lower(), "inputs": []}
            self.forms.append(self._current)
        elif tag in ("input", "button"):
            default_type = "text" if tag == "input" else "submit"
            item = {"name": a.get("name", ""), "type": a.get("type", default_type).lower(),
                    "value": a.get("value", ""), "tag": tag, "checked": "checked" in a}
            (self._current["inputs"] if self._current is not None else self.loose_inputs).append(item)

    def handle_endtag(self, tag):
        if tag == "form":
            self._current = None


def _parse_forms(page: str) -> list[dict]:
    p = FormParser()
    try:
        p.feed(page)
    except Exception:
        return []
    return p.forms


def find_login_form(page: str) -> dict | None:
    for form in _parse_forms(page):
        if any(i["type"] == "password" for i in form["inputs"]):
            return form
    return None


def find_portal_form(page: str) -> dict | None:
    forms = _parse_forms(page)
    for form in forms:
        if any(i["type"] == "password" for i in form["inputs"]):
            return form
    for form in forms:
        names = {i["name"].lower() for i in form["inputs"]}
        types = {i["type"] for i in form["inputs"]}
        if "magic" in names or "4tredir" in names or types & {"text", "email", "checkbox"}:
            return form
    for form in forms:
        if any(i["type"] in ("submit", "image") for i in form["inputs"]):
            return form
    return None


def page_title(body: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", body or "", re.I | re.S)
    return html.unescape(m.group(1)).strip()[:60] if m else "no title"


def new_session() -> requests.Session:
    s = load_requests().Session()
    s.verify = False
    s.headers["User-Agent"] = USER_AGENT
    return s


def why(e: Exception) -> str:
    text = str(e)
    low = text.lower()
    name = e.__class__.__name__
    if "nameresolution" in low or "getaddrinfo" in low or "name or service" in low or "11001" in text:
        return f"{name}: DNS failed - name could not be looked up"
    if "timeout" in name.lower() or "timed out" in low:
        return f"{name}: timed out"
    if "10061" in text or "refused" in low:
        return f"{name}: refused"
    if "10051" in text or "10065" in text or "unreachable" in low:
        return f"{name}: network unreachable"
    if "ssl" in low:
        return f"{name}: SSL error"
    return name


_portal_hosts: set[str] = set()
_found_portal: dict[str, str] = {}


def portal_host(settings: Settings) -> str:
    if not settings.portal_url:
        return ""
    return urlparse(settings.portal_url).hostname or ""


def _is_private_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private and not ip.is_loopback


def looks_like_portal(url: str, settings: Settings) -> bool:
    if not url:
        return False
    host = urlparse(url).hostname or ""
    if "fgtauth" in url.lower():
        return True
    if host and (host == portal_host(settings) or host in _portal_hosts):
        return True
    return _is_private_ip(host)


def portal_link_in_page(resp: requests.Response, settings: Settings) -> str | None:
    for hop in list(resp.history) + [resp]:
        if looks_like_portal(hop.url, settings):
            return hop.url
        loc = hop.headers.get("Location", "")
        if loc and looks_like_portal(urljoin(hop.url, loc), settings):
            return urljoin(hop.url, loc)

    body = resp.text[:200_000] if resp.text else ""
    m = FGTAUTH_LINK.search(body)
    if m:
        return m.group(1).replace("&amp;", "&")
    for rx in (JS_REDIRECT, META_REFRESH):
        m = rx.search(body)
        if m:
            target = urljoin(resp.url, m.group(1).replace("&amp;", "&"))
            if looks_like_portal(target, settings):
                return target
    if find_login_form(body):
        return resp.url
    return None


def _probe(s, url: str, settings: Settings) -> tuple[str, str | None]:
    check_stop()
    try:
        r = s.get(url, timeout=(4, 8), allow_redirects=True)
    except requests.RequestException as e:
        reason = why(e)
        log(f"   {url} -> no response ({reason})")
        return ("fail-dns" if "DNS failed" in reason else "fail"), None
    portal = portal_link_in_page(r, settings)
    if portal:
        log(f"   {url} -> redirected to portal")
        return "portal", portal
    log(f"   {url} -> loaded normally ({r.status_code})")
    return "online", url


def portal_candidates(settings: Settings, ssid: str) -> list[str]:
    bases: list[str] = []
    for base in (settings.portal_url, _found_portal.get(ssid, "")):
        if base:
            u = urlparse(base)
            bases.append(f"{u.scheme}://{u.netloc}")
    gw = default_route()[0]
    if gw:
        bases += [f"https://{gw}:1003", f"http://{gw}:1000"]
    urls: list[str] = []
    for base in dict.fromkeys(bases):
        urls += [base + path for path in PORTAL_LOGIN_PATHS]
    return urls


def find_portal_without_dns(settings: Settings, ssid: str) -> tuple[str, str | None]:
    log("   no site answered - trying without DNS (by IP address)...")
    s = new_session()
    state, found = ip_probe(settings)
    if state == "portal":
        return state, found
    if state == "online":
        return "online-ip", found
    for url in portal_candidates(settings, ssid):
        check_stop()
        host = urlparse(url).hostname or ""
        try:
            r = s.get(url, timeout=(4, 8), allow_redirects=True)
        except requests.RequestException as e:
            log(f"   {url} -> no response ({why(e)})")
            continue
        _portal_hosts.add(host)
        found = portal_link_in_page(r, settings)
        if not found and find_portal_form(r.text or ""):
            found = r.url
        if found:
            log(f"   {url} -> portal login page found")
            return "portal", found
        log(f"   {url} -> answered (HTTP {r.status_code}, page: {page_title(r.text)}) but no login form")
    return "offline", None


def check_state(settings: Settings, start: int = 0, ssid: str = "") -> tuple[str, str | None]:
    s = new_session()
    probes = settings.probe_urls
    start %= max(1, len(probes))
    tried_no_dns = False
    for url in probes[start:] + probes[:start]:
        state, found = _probe(s, url, settings)
        if state in ("portal", "online"):
            return state, found
        if state == "fail-dns" and not tried_no_dns:
            tried_no_dns = True
            state, found = find_portal_without_dns(settings, ssid)
            if state != "offline":
                return state, found
    if tried_no_dns:
        return "offline", None
    return find_portal_without_dns(settings, ssid)


def internet_check() -> bool:
    try:
        r = new_session().get(NCSI_URL, timeout=(4, 8), allow_redirects=False)
    except requests.RequestException:
        return False
    return r.status_code == 200 and (r.text or "").strip() == NCSI_TEXT


def confirm_connected(settings: Settings) -> bool:
    if not settings.success_url:
        if internet_check():
            log("CONNECTED - internet works (Windows connectivity test passed).")
            return True
        log("   Windows connectivity test did not pass")
        return ip_internet_check(settings)
    try:
        r = new_session().get(settings.success_url, timeout=(4, 10), allow_redirects=True)
    except requests.RequestException as e:
        reason = why(e)
        log(f"   {settings.success_url} -> no response ({reason})")
        if "DNS failed" in reason:
            return ip_internet_check(settings)
        return _fallback_internet_check(settings)
    if portal_link_in_page(r, settings):
        log(f"   {settings.success_url} -> still goes to the portal - not connected yet")
        return False
    if r.status_code >= 400:
        log(f"   {settings.success_url} -> HTTP {r.status_code}")
        return _fallback_internet_check(settings)
    body = html.unescape(r.text or "")
    page = page_title(r.text)
    if settings.success_text:
        found = [t for t in settings.success_text if t.lower() in body.lower()]
        if not found:
            log(f"   {settings.success_url} opened (HTTP {r.status_code}, page: {page}) but none of "
                f"{settings.success_text} is on it - not the real landing page yet")
            return False
        log(f"CONNECTED - {settings.success_url} is the real landing page (found '{found[0]}', page: {page})")
    else:
        log(f"CONNECTED - {settings.success_url} opened for real (HTTP {r.status_code}, page: {page})")
    return True


def _fallback_internet_check(settings: Settings) -> bool:
    if internet_check():
        log(f"CONNECTED - internet works (Windows connectivity test passed); "
            f"{settings.success_url} itself did not load on this network")
        return True
    return ip_internet_check(settings)


def ip_probe(settings: Settings) -> tuple[str, str | None]:
    url = NO_DNS_PROBES[0]
    check_stop()
    try:
        r = new_session().get(url, timeout=(4, 8), allow_redirects=False)
    except requests.RequestException as e:
        log(f"   {url} -> no response ({why(e)})")
        return "fail", None
    portal = portal_link_in_page(r, settings)
    if portal:
        log(f"   {url} -> redirected to portal")
        return "portal", portal
    where = r.headers.get("Location", "")
    log(f"   {url} -> answered by the internet, not the portal (HTTP {r.status_code}"
        f"{' -> ' + where if where else ''}) = logged in")
    return "online", url


def ip_internet_check(settings: Settings) -> bool:
    state, _ = ip_probe(settings)
    if state != "online":
        log("   not connected yet" if state == "portal" else "   no internet by IP either - not connected yet")
        return False
    if dns_works() is True:
        log("CONNECTED - logged in (internet by IP works and DNS answers)")
        return True
    log("   logged in, but this network's DNS does not work (sites can't be opened by name)")
    if settings.fix_dns:
        wifi = wifi_status()
        if wifi["connected"] and wifi["interface"]:
            set_public_dns(wifi["interface"], wifi["ssid"])
            wait(2)
            if internet_check():
                log(f"CONNECTED - logged in, and websites work using DNS {', '.join(PUBLIC_DNS)}")
                return True
    log("CONNECTED - logged in (internet by IP works), but this network's DNS is broken, so the browser "
        "may not open sites by name. Tick 'Fix broken Wi-Fi DNS' to repair that automatically.")
    return True


PUBLIC_DNS = ["8.8.8.8", "1.1.1.1"]


def _dns_state_file() -> Path:
    return HERE / "dns_changed.txt"


def set_public_dns(interface: str, ssid: str) -> None:
    status("checking", f"setting DNS {', '.join(PUBLIC_DNS)} on {interface}")
    log(f"   this network's DNS is broken - using {', '.join(PUBLIC_DNS)} on '{interface}' while on '{ssid}'")
    run_cmd(f'netsh interface ipv4 set dnsservers name="{interface}" static {PUBLIC_DNS[0]} primary validate=no')
    for i, extra in enumerate(PUBLIC_DNS[1:], start=2):
        run_cmd(f'netsh interface ipv4 add dnsservers name="{interface}" {extra} index={i} validate=no')
    run_cmd(["ipconfig", "/flushdns"], timeout=30)
    try:
        _dns_state_file().write_text(f"{interface}\n{ssid}\n", encoding="utf-8")
    except OSError:
        pass


def restore_dns(current_ssid: str) -> None:
    f = _dns_state_file()
    try:
        interface, ssid = (f.read_text(encoding="utf-8").splitlines() + ["", ""])[:2]
    except OSError:
        return
    if ssid and ssid == current_ssid:
        return
    log(f"Putting DNS on '{interface}' back to automatic (it was changed for '{ssid}')")
    out = _output(f'netsh interface ipv4 set dnsservers name="{interface}" source=dhcp').strip()
    log("   > netsh interface ipv4 set dnsservers source=dhcp" + (f"  ({out[:80]})" if out else "  (ok)"))
    _output(["ipconfig", "/flushdns"])
    try:
        f.unlink()
    except OSError:
        pass


def _forward_link(resp) -> str | None:
    body = resp.text[:200_000] if resp.text else ""
    m = FGTAUTH_LINK.search(body)
    if m:
        return m.group(1).replace("&amp;", "&")
    for rx in (JS_REDIRECT, META_REFRESH):
        m = rx.search(body)
        if m:
            return urljoin(resp.url, m.group(1).replace("&amp;", "&"))
    return None


def submit_login(portal_url: str, settings: Settings) -> bool:
    s = new_session()
    try:
        r = s.get(portal_url, timeout=10, allow_redirects=True)
    except requests.RequestException as e:
        log(f"   could not open portal page ({why(e)})")
        return False

    form = find_portal_form(r.text)
    for _ in range(3):
        if form:
            break
        nxt = _forward_link(r)
        if not nxt or nxt == r.url:
            break
        log(f"   portal page forwards to {nxt}")
        try:
            r = s.get(nxt, timeout=10, allow_redirects=True)
        except requests.RequestException as e:
            log(f"   could not open it ({why(e)})")
            return False
        form = find_portal_form(r.text)
    if not form:
        if "keepalive" in r.text.lower() or "keepalive" in r.url.lower():
            log("   portal says we are already authenticated")
            return True
        log(f"   portal page has no login form (HTTP {r.status_code}, page: {page_title(r.text)})")
        return False

    data: dict[str, str] = {}
    user_field = pass_field = None
    submit_done = False
    for inp in form["inputs"]:
        name, typ = inp["name"], inp["type"]
        if not name:
            continue
        if typ == "password" and pass_field is None:
            pass_field = name
        elif typ in ("text", "email", "tel") and user_field is None:
            user_field = name
        elif typ == "hidden":
            data[name] = inp["value"]
        elif typ == "checkbox":
            data[name] = inp["value"] or "on"
        elif typ == "radio" and (inp["checked"] or name not in data):
            data[name] = inp["value"]
        elif typ in ("submit", "image") and not submit_done:
            data[name] = inp["value"]
            submit_done = True

    if pass_field:
        if not settings.username or not settings.password:
            log(f"   this portal asks for a username/password but none is set for "
                f"'{profile_label(settings.profile_name)}'")
            return False
        if user_field:
            data[user_field] = settings.username
        data[pass_field] = settings.password
        kind = "login"
    else:
        if "answer" in data:
            data["answer"] = "1"
        if user_field:
            data[user_field] = settings.username
        log("   portal page has no password box - submitting it as an accept/continue page")
        kind = "accept"

    action = urljoin(r.url, form["action"] or r.url)
    try:
        if form["method"] == "post":
            resp = s.post(action, data=data, timeout=15, allow_redirects=False)
        else:
            resp = s.get(action, params=data, timeout=15, allow_redirects=False)
    except requests.RequestException as e:
        log(f"   submit raised {why(e)} - will verify connection")
        return True

    if 300 <= resp.status_code < 400:
        loc = urljoin(action, resp.headers.get("Location", ""))
        if "fgtauth" in loc.lower() and "keepalive" not in loc.lower():
            log(f"   portal sent us back to the login page ({loc}) - not accepted")
            return False
        log(f"   portal accepted the login (forwarding to {loc})")
        return True
    text = resp.text.lower()
    if "keepalive" in text or "keepalive" in resp.url.lower():
        log("   portal accepted the login (keepalive page)")
        return True
    if kind == "login" and find_login_form(resp.text):
        reason = next((w for w in FAIL_WORDS if w in text), "login form shown again")
        log(f"   portal rejected the login ({reason})")
        return False
    log("   login submitted - verifying connection")
    return True


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_cmd(args: list[str] | str, timeout: int = 90) -> None:
    check_stop()
    log("   > " + (args if isinstance(args, str) else " ".join(args)))
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        log("     (timed out)")
        return
    out = (p.stdout + p.stderr).strip()
    if p.returncode != 0:
        first = out.splitlines()[0] if out else ""
        log(f"     failed (code {p.returncode}) {first}")
        if "elevation" in out.lower() or "denied" in out.lower():
            log("     -> run this script as Administrator for release/renew to work")


def reset_network(settings: Settings) -> None:
    status("resetting", "flushdns / release / renew")
    log("Resetting network: flushdns / release / renew")
    if not is_admin():
        log("   (not running as Administrator - release/renew may be refused)")
    adapter = [settings.adapter] if settings.adapter else []
    run_cmd(["ipconfig", "/flushdns"], timeout=30)
    run_cmd(["ipconfig", "/release", *adapter])
    wait(2)
    run_cmd(["ipconfig", "/renew", *adapter], timeout=120)
    wait(3)
    log_network_info()


def _output(cmd, timeout: int = 10) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    except Exception:
        return ""
    raw = p.stdout + p.stderr
    for enc in ("utf-8", "oem", "mbcs"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", "replace")


def list_adapters() -> list[str]:
    names = []
    for line in _output(["netsh", "interface", "show", "interface"]).splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0] in ("Enabled", "Disabled"):
            names.append(parts[3].strip())
    return names


_KV = re.compile(r"^\s*([^:]+?)\s*:\s*(.*?)\s*$")


def wifi_status() -> dict:
    out = _output(["netsh", "wlan", "show", "interfaces"])
    info = {"present": False, "connected": False, "ssid": "", "radio_off": False, "interface": ""}
    cur: dict = {}
    blocks = []
    for line in out.splitlines():
        m = _KV.match(line)
        if m:
            key, val = m.group(1).lower(), m.group(2)
            if key == "name":
                cur = {"interface": val}
                blocks.append(cur)
            elif cur is not None:
                cur.setdefault(key, val)
        if "software off" in line.lower() or "hardware off" in line.lower():
            if cur is not None:
                cur["radio_off"] = True
    for b in blocks:
        b_connected = b.get("state", "").lower() == "connected"
        if not info["present"] or b_connected:
            info.update(present=True, connected=b_connected, ssid=b.get("ssid", "") if b_connected else "",
                        radio_off=bool(b.get("radio_off")), interface=b.get("interface", ""))
        if b_connected:
            break
    return info


def wifi_profiles() -> list[str]:
    names = []
    for line in _output(["netsh", "wlan", "show", "profiles"]).splitlines():
        m = re.match(r"^\s*[\w ]*Profile\s*:\s*(.+?)\s*$", line)
        if m and m.group(1) not in names:
            names.append(m.group(1))
    return names


def visible_networks() -> list[tuple[str, str]]:
    nets: list[tuple[str, str]] = []
    ssid = None
    for line in _output(["netsh", "wlan", "show", "networks"]).splitlines():
        m = re.match(r"^\s*SSID\s+\d+\s*:\s*(.*?)\s*$", line)
        if m:
            ssid = m.group(1)
            continue
        m = _KV.match(line)
        if m and ssid is not None and m.group(1).lower() == "authentication":
            nets.append((ssid, m.group(2)))
            ssid = None
    return nets


def _add_open_profile(name: str) -> bool:
    esc = html.escape(name)
    xml = f"""<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
<name>{esc}</name><SSIDConfig><SSID><name>{esc}</name></SSID></SSIDConfig>
<connectionType>ESS</connectionType><connectionMode>manual</connectionMode>
<MSM><security><authEncryption><authentication>open</authentication><encryption>none</encryption>
<useOneX>false</useOneX></authEncryption></security></MSM></WLANProfile>"""
    path = Path(tempfile.gettempdir()) / "wifi_auto_login_profile.xml"
    try:
        path.write_text(xml, encoding="utf-8")
        out = _output(f'netsh wlan add profile filename="{path}" user=current')
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    log(f"   saved open Wi-Fi '{name}' in Windows: {out.strip()[:80]}")
    return name in wifi_profiles()


def connect_wifi(name: str, timeout: float = 20) -> bool:
    name = name.replace('"', "").strip()
    if not name:
        return False
    st = wifi_status()
    if not st["present"]:
        log("   this PC has no Wi-Fi adapter")
        return False
    if st["radio_off"]:
        log("   Wi-Fi is turned OFF in Windows - turn it on (taskbar Wi-Fi button) and try again")
        return False
    if name not in wifi_profiles():
        auth = dict(visible_networks()).get(name, "")
        if auth.lower().startswith("open"):
            _add_open_profile(name)
        else:
            log(f"   Windows has no saved password for '{name}'. Connect to it once from the Windows "
                f"Wi-Fi menu (tick 'Connect automatically'), then this app can switch to it by itself.")
            return False
    log(f'   > netsh wlan connect name="{name}"')
    out = _output(f'netsh wlan connect name="{name}"').strip()
    if out:
        log("     " + out.splitlines()[0][:100])
    end = time.time() + timeout
    while time.time() < end:
        wait(1)
        st = wifi_status()
        if st["connected"] and st["ssid"] == name:
            log(f"   joined Wi-Fi '{name}'")
            wait(3)
            return True
    log(f"   could not join '{name}' within {timeout:g}s (now on: {st['ssid'] or 'nothing'})")
    return False


def default_route() -> tuple[str, str]:
    best = None
    for line in _output(["route", "print", "-4", "0.0.0.0"]).splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
            try:
                ipaddress.ip_address(parts[2])
                metric = int(parts[4])
            except ValueError:
                continue
            if best is None or metric < best[0]:
                best = (metric, parts[2], parts[3])
    return (best[1], best[2]) if best else ("", "")


def dns_works(host: str = "www.google.com", timeout: float = 5) -> bool | None:
    result: list = []

    def look():
        try:
            socket.getaddrinfo(host, 80)
            result.append(True)
        except OSError:
            result.append(False)

    t = threading.Thread(target=look, daemon=True)
    t.start()
    t.join(timeout)
    return result[0] if result else None


def log_network_info(wifi: dict | None = None) -> None:
    try:
        wifi = wifi or wifi_status()
        gw, ip = default_route()
        if not wifi["present"]:
            where = "no Wi-Fi adapter found right now (cable/LAN, or Wi-Fi restarting after release)"
        elif wifi["connected"]:
            where = f"Wi-Fi '{wifi['ssid']}'"
        elif wifi["radio_off"]:
            where = "Wi-Fi is turned OFF" + (" (using cable/LAN)" if gw else "")
        else:
            where = "Wi-Fi not connected to any network" + (" (using cable/LAN)" if gw else "")
        if gw:
            _portal_hosts.add(gw)
        dns = dns_works()
        dns_text = {True: "working", False: "NOT working (names like google.com can't be looked up)",
                    None: "no answer (timed out)"}[dns]
        log(f"Network: {where} | IP {ip or 'none'} | gateway {gw or 'none'} | DNS {dns_text}")
        if not gw:
            log("   no gateway = no usable network connection yet (not joined, or no IP from DHCP)")
    except Exception as e:
        log(f"Network: could not read network info ({e!r})")


def ensure_wifi(settings: Settings) -> dict:
    st = wifi_status()
    target = settings.wifi_target
    if target and settings.wifi_keep and st["present"] and not (st["connected"] and st["ssid"] == target):
        log(f"Wi-Fi is on '{st['ssid'] or 'nothing'}' but should be on '{target}' - switching...")
        status("checking", f"joining Wi-Fi {target}")
        connect_wifi(target)
        st = wifi_status()
    return st


def connect(settings: Settings) -> bool:
    attempts = 0
    fails_since_reset = 0
    order: list[int] = []
    pos = 0
    tries_on_site = 0
    round_no = 0
    prev_site: int | None = None
    current: tuple | None = None
    while True:
        check_stop()
        before = attempts
        portal_seen = False
        try:
            wifi = ensure_wifi(settings)
            ssid = wifi["ssid"]
            name = settings.use_profile(ssid)
            if current != (ssid, name):
                if current is not None:
                    log("Wi-Fi changed.")
                if ssid:
                    restore_dns(ssid)
                current = (ssid, name)
                order = []
                log(f"Using settings '{profile_label(name)}' for "
                    f"{('Wi-Fi ' + repr(ssid)) if ssid else 'this connection'}: user={settings.username or '(none)'}"
                    f"  portal={settings.portal_url or 'auto-detect'}  success={settings.success_url or 'internet test'}")
                log_network_info(wifi)

            n = max(1, len(settings.probe_urls))
            if pos >= len(order) or max(order, default=-1) >= n:
                order = list(range(n))
                if settings.shuffle_sites:
                    random.shuffle(order)
                    if n > 1 and order[0] == prev_site:
                        order.append(order.pop(0))
                pos, tries_on_site = 0, 0
                round_no += 1
                log(f"Round {round_no} site order: " + ", ".join(settings.probe_urls[i] for i in order))
            site_idx = prev_site = order[pos]
            site = settings.probe_urls[site_idx]
            try_no = tries_on_site + 1
            status("checking", f"{site}  (try {try_no}/{settings.tries_per_site})")
            log(f"Checking via {site} (try {try_no}/{settings.tries_per_site} on this site)...")
            state, url = check_state(settings, site_idx, ssid)

            if state in ("online", "online-ip"):
                if confirm_connected(settings):
                    status("connected", f"after {attempts} attempt(s)" if attempts else "")
                    return True
                if state == "online-ip":
                    log("   internet answers by IP address but not by name - DNS problem on this network")
                state = "offline"

            attempts += 1
            if state == "portal":
                portal_seen = True
                u = urlparse(url)
                base = f"{u.scheme}://{u.netloc}"
                if u.hostname and base != _found_portal.get(ssid):
                    _found_portal[ssid] = base
                    _portal_hosts.add(u.hostname)
                    log(f"   portal for this network: {base}")
                    status("portal_found", f"{ssid}|{base}")
                # The FortiGate often rejects the first several logins before one sticks, so
                # keep re-submitting it quickly while the portal is reachable (no network reset -
                # that would only disconnect us). Stops the moment we are really online.
                for i in range(1, settings.login_retries + 1):
                    check_stop()
                    status("logging in", f"attempt {attempts}  login {i}/{settings.login_retries}")
                    log(f"Attempt {attempts}: logging in at {url} (login {i}/{settings.login_retries})")
                    ok = submit_login(url, settings)
                    wait(1.5)
                    if ok and confirm_connected(settings):
                        status("connected", f"after {attempts} attempt(s)")
                        return True
                    log("   still not through after login" if ok else "   login not accepted")
                    if i < settings.login_retries:
                        wait(settings.delay)
            else:
                status("offline", f"attempt {attempts}")
                log(f"Attempt {attempts}: no site or portal reachable")
        except Stopped:
            raise
        except Exception as e:
            attempts = before + 1
            log(f"Attempt {attempts}: unexpected error {e!r} - continuing")

        tries_on_site += 1
        if tries_on_site >= settings.tries_per_site:
            pos, tries_on_site = pos + 1, 0

        if settings.max_attempts and attempts >= settings.max_attempts:
            log(f"Giving up after {attempts} attempts.")
            status("failed", f"gave up after {attempts} attempts")
            return False
        # The portal being reachable means a network reset would only cut us off, so only
        # count toward a reset when we couldn't reach the portal at all.
        if portal_seen:
            fails_since_reset = 0
            wait(settings.delay)
        elif settings.reset_network and (fails_since_reset := fails_since_reset + 1) >= settings.attempts_before_reset:
            try:
                reset_network(settings)
            except Stopped:
                raise
            except Exception as e:
                log(f"   network reset error {e!r} - continuing")
            fails_since_reset = 0
        else:
            wait(settings.delay)


def run(settings: Settings, watch: bool) -> bool:
    try:
        if not watch:
            return connect(settings)
        log(f"Watch mode: checking every {settings.watch_interval:g}s")
        while True:
            connect(settings)
            wait(settings.watch_interval)
    except Stopped:
        log("Stopped.")
        status("stopped")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="FortiGate captive portal auto login")
    ap.add_argument("--config", default=str(HERE / "config.ini"), help="settings file (default: config.ini)")
    ap.add_argument("--watch", action="store_true", help="stay running and re-login whenever disconnected")
    args = ap.parse_args()

    settings = Settings(Path(args.config))
    default = settings.profiles[""]
    if not default["username"]:
        default["username"] = input("Username: ").strip()
    if not default["password"]:
        default["password"] = getpass.getpass("Password: ")

    log(f"Profiles: {', '.join(profile_label(p) for p in settings.profiles)}")
    try:
        return 0 if run(settings, args.watch) else 1
    except KeyboardInterrupt:
        log("Stopped.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
