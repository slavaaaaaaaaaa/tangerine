#!/usr/bin/python3
"""Install or update tangerine from config.yaml.

    sudo ./install.py [-c config.yaml]         install / update everything
    ./install.py --render-only DIR [-c ...]    just write the rendered files to DIR for review

Safe to re-run: files are only replaced (and services only restarted) when they change.
"""
import argparse
import grp
import hashlib
import os
import re
import shutil
import subprocess
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(HERE, "templates")
APP_BINARY = os.path.join(HERE, "build", "tangerine-server")
ETC = "/etc/tangerine"
ASK_VENV = "/opt/tangerine-ask/venv"
ASK_SHARE = "/usr/local/share/tangerine-ask"
ACME_ROOT = "/var/www/letsencrypt"

DEFAULTS = {
    "app": {"indexes": [], "site_name": "Tangerine", "slogan": "Freshly squeezed",
            "footer_phrase": "made with 100% pulp",
            "bundles": True, "logout_link": True, "memory_max": "12G", "listen": "127.0.0.1:8081"},
    "gate": {"listen": "127.0.0.1:8082", "geoip": True},
    "ask": {"enabled": False, "indexes": [], "listen": "127.0.0.1:8083", "crawl_listen": "127.0.0.1:8084",
            "index_max_gb": 30, "evict_at": 0.9},
    "nginx": {"catch_all": True, "requests_per_second": 40, "logins_per_minute": 10},
    "tls": {"certbot": True, "email": ""},
    "fail2ban": False,
    "users": {},
}

CATCH_ALL_HTTP = """
# Scanners hitting the bare IP / unknown names get nothing.
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    location /.well-known/acme-challenge/ { root %s; }
    location / { return 444; }
}
""" % ACME_ROOT

CATCH_ALL_HTTPS = """server {
    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;
    server_name _;
    ssl_reject_handshake on;
}
"""

PAGE_INJECT = """        # Uncompressed upstream so sub_filter can add to every page.
        proxy_set_header Accept-Encoding "";
        sub_filter '</body>' '%s</body>';
        sub_filter_once on;
"""
LOGOUT_LINK = ('<a href="/_gate/logout" style="position:fixed;right:10px;bottom:10px;font:12px system-ui,sans-serif;'
               'color:#e8741c;background:#fff9;padding:3px 8px;border-radius:8px;text-decoration:none">log out</a>')
ASK_SCRIPT = '<script src="/_ask/overlay.js" defer></script>'

ASK_UPSTREAM = "upstream tangerine_ask  { server %s; keepalive 8; }\n"
ASK_LOCATION = """    # Questions to an LLM about the document being viewed. Same sign-in as the archive;
    # the gate's answer tells tangerine-ask who is asking.
    location /_ask/ {
        auth_request /_gate/check;
        auth_request_set $tangerine_user $upstream_http_x_tangerine_user;
        error_page 401 = @ask_signed_out;

        proxy_pass http://tangerine_ask;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Tangerine-User $tangerine_user;
        # Answers stream in as they're written.
        proxy_buffering off;
        proxy_read_timeout 600s;
        client_max_body_size 256k;   # questions carry the conversation so far
    }

    # fetch() can't follow the login redirect, so the overlay gets a plain 401.
    location @ask_signed_out {
        default_type application/json;
        return 401 '{"error":"signed_out"}';
    }
"""


def say(msg):
    print("\n\033[1;33m== %s\033[0m" % msg, flush=True)


def die(msg):
    sys.exit("\033[1;31merror:\033[0m " + msg)


def run(*cmd, check=True, quiet=False):
    r = subprocess.run(cmd, text=True, capture_output=quiet)
    if check and r.returncode != 0:
        die("%s failed%s" % (" ".join(cmd), (":\n" + r.stdout + r.stderr) if quiet else ""))
    return r


# ---------------------------------------------------------------- config

def load(path):
    try:
        with open(path) as f:
            c = yaml.safe_load(f) or {}
    except FileNotFoundError:
        die("%s not found -- copy config.example.yaml to config.yaml and edit it" % path)
    for k, v in DEFAULTS.items():
        if isinstance(v, dict):
            c[k] = {**v, **(c.get(k) or {})}
        else:
            c.setdefault(k, v)
    c["users"] = c.get("users") or {}

    errors = []
    domain = c.get("domain") or ""
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain) or domain.endswith("example.com"):
        errors.append("domain: set it to your real hostname (got %r)" % domain)
    if not c["app"]["indexes"]:
        errors.append("app.indexes: list at least one index.json")
    for p in c["app"]["indexes"]:
        if not os.path.isfile(p):
            errors.append("app.indexes: %s does not exist" % p)
    for name in c["users"]:
        if not re.fullmatch(r"[a-z0-9_.-]{1,32}", str(name)):
            errors.append("users: %r -- names are 1-32 chars of a-z 0-9 _ . -" % name)
    if not c["tls"]["certbot"] and not (c["tls"].get("certificate") and c["tls"].get("certificate_key")):
        errors.append("tls: with certbot: false, set certificate and certificate_key")
    for key, field in (("app", "listen"), ("gate", "listen"), ("ask", "listen"), ("ask", "crawl_listen")):
        if not re.fullmatch(r"127\.0\.0\.1:\d+|\[::1\]:\d+|localhost:\d+", str(c[key][field])):
            errors.append("%s.%s: must be a localhost address, the gate is the only thing guarding it" % (key, field))
    if c["ask"]["enabled"]:
        if not c["ask"]["indexes"]:
            errors.append("ask.indexes: list the app.indexes entries tangerine-ask should read")
        for p in c["ask"]["indexes"]:
            if p not in c["app"]["indexes"]:
                errors.append("ask.indexes: %s is not one of app.indexes" % p)
        if not float(c["ask"]["index_max_gb"]) > 0:
            errors.append("ask.index_max_gb: must be more than 0")
        if not 0 < float(c["ask"]["evict_at"]) <= 1:
            errors.append("ask.evict_at: a fraction between 0 and 1, e.g. 0.9")
    if errors:
        die("config problems:\n  " + "\n  ".join(errors))
    return c


def ask_config(c):
    a = c["ask"]
    out = {
        "listen": a["listen"], "crawl_url": "http://%s" % a["crawl_listen"], "indexes": a["indexes"],
        "index_max_gb": float(a["index_max_gb"]), "evict_at": float(a["evict_at"]),
    }
    for key in ("noun", "examples", "system_prompt", "prices"):
        if a.get(key):
            out[key] = a[key]
    return yaml.safe_dump(out, sort_keys=False, allow_unicode=True).encode()


def systemd_exec(args):
    """systemd quoting: double quotes, with backslash, quote and $ / % escaped"""
    return " ".join('"%s"' % a.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$").replace("%", "%%")
                    for a in args)


def cert_paths(c):
    if c["tls"]["certbot"]:
        live = "/etc/letsencrypt/live/%s" % c["domain"]
        return live + "/fullchain.pem", live + "/privkey.pem"
    return c["tls"]["certificate"], c["tls"]["certificate_key"]


def render_all(c):
    """{destination path: (content bytes, mode)}"""
    app = c["app"]
    args = ["/usr/local/bin/tangerine-server", "--production", "--listen-address", app["listen"],
            "--site-name", app["site_name"], "--slogan", app["slogan"], "--latin-phrase", app["footer_phrase"]]
    if not app["bundles"]:
        args.append("--disable-bundles")
    args += app["indexes"]
    exec_line = systemd_exec(args)
    # The crawler's copy: no --production, so no rate limits (and it trusts no X-Real-IP).
    # Only tangerine-ask talks to it; nginx never routes there.
    crawl_exec = systemd_exec(["/usr/local/bin/tangerine-server", "--listen-address", c["ask"]["crawl_listen"],
                               "--site-name", app["site_name"], "--slogan", app["slogan"],
                               "--latin-phrase", app["footer_phrase"], "--disable-bundles"] + c["ask"]["indexes"])
    cert, key = cert_paths(c)
    ask = c["ask"]["enabled"]
    inject = (LOGOUT_LINK if app["logout_link"] else "") + (ASK_SCRIPT if ask else "")
    values = {
        "domain": c["domain"],
        "app_listen": app["listen"],
        "gate_listen": c["gate"]["listen"],
        "requests_per_second": str(int(c["nginx"]["requests_per_second"])),
        "logins_per_minute": str(int(c["nginx"]["logins_per_minute"])),
        "catch_all_http": CATCH_ALL_HTTP if c["nginx"]["catch_all"] else "",
        "catch_all_https": CATCH_ALL_HTTPS if c["nginx"]["catch_all"] else "",
        "ssl_certificate": cert,
        "ssl_certificate_key": key,
        "page_inject": PAGE_INJECT % inject if inject else "",
        "ask_upstream": ASK_UPSTREAM % c["ask"]["listen"] if ask else "",
        "ask_location": ASK_LOCATION if ask else "",
        "ask_data_dirs": " ".join(sorted({os.path.dirname(p) for p in c["ask"]["indexes"]})) if ask else "",
        "app_exec": exec_line,
        "crawl_exec": crawl_exec,
        "crawl_memory_max": "3G",
        "data_dirs": " ".join(sorted({os.path.dirname(p) for p in app["indexes"]})),
        "memory_max": str(app["memory_max"]),
    }

    def render(name):
        with open(os.path.join(TEMPLATES, name)) as f:
            text = f.read()
        out = re.sub(r"\{\{(\w+)\}\}", lambda m: values[m.group(1)], text)
        return out.encode()

    files = {
        "/etc/nginx/conf.d/tangerine-http.conf": (render("nginx-http.conf"), 0o644),
        "/etc/nginx/conf.d/tangerine.conf": (render("nginx-https.conf"), 0o644),
    }
    for unit in ("tangerine.service", "tangerine-gate.service", "tangerine-geoip.service", "tangerine-geoip.timer"):
        files["/etc/systemd/system/" + unit] = (render(unit), 0o644)
    if ask:
        files["/etc/systemd/system/tangerine-ask.service"] = (render("tangerine-ask.service"), 0o644)
        files["/etc/systemd/system/tangerine-crawl.service"] = (render("tangerine-crawl.service"), 0o644)
    if c["fail2ban"]:
        files["/etc/fail2ban/filter.d/tangerine-gate.conf"] = (render("fail2ban-filter.conf"), 0o644)
        files["/etc/fail2ban/jail.d/tangerine-gate.conf"] = (render("fail2ban-jail.conf"), 0o644)
    return files


# ---------------------------------------------------------------- install helpers

def put(dest, content, mode, group=None):
    """Write dest if its content/mode differ. Returns True when it changed."""
    try:
        with open(dest, "rb") as f:
            same = f.read() == content
        st = os.stat(dest)
        same = same and (st.st_mode & 0o777) == mode and (group is None or st.st_gid == grp.getgrnam(group).gr_gid)
    except FileNotFoundError:
        same = False
    if same:
        return False
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".tangerine-tmp"
    with open(tmp, "wb") as f:
        f.write(content)
    os.chmod(tmp, mode)
    if group:
        os.chown(tmp, 0, grp.getgrnam(group).gr_gid)
    os.replace(tmp, dest)
    if shutil.which("restorecon"):
        run("restorecon", dest, check=False, quiet=True)
    print("  wrote %s" % dest)
    return True


def read(path):
    with open(path, "rb") as f:
        return f.read()


def selinux_enforcing():
    return shutil.which("getenforce") and run("getenforce", quiet=True, check=False).stdout.strip() == "Enforcing"


def ensure_user(name, comment):
    if run("id", name, check=False, quiet=True).returncode != 0:
        nologin = shutil.which("nologin") or "/sbin/nologin"
        run("useradd", "--system", "--shell", nologin, "--home-dir", "/", "--comment", comment, name)
        print("  created user %s" % name)


def install_ask_venv():
    """Create /opt/tangerine-ask/venv and (re)install the SDKs when requirements.txt changes."""
    reqs = read(os.path.join(HERE, "ask", "requirements.txt"))
    stamp = os.path.join(ASK_VENV, ".requirements.sha256")
    want = hashlib.sha256(reqs).hexdigest()
    try:
        if read(stamp).decode().strip() == want:
            return False
    except FileNotFoundError:
        pass
    cfg = os.path.join(ASK_VENV, "pyvenv.cfg")
    if os.path.exists(cfg) and "include-system-site-packages = true" in read(cfg).decode():
        # Older installs shared the distro's packages, which pip can't keep consistent.
        shutil.rmtree(ASK_VENV)
    if not os.path.exists(os.path.join(ASK_VENV, "bin", "python")):
        run("/usr/bin/python3", "-m", "venv", ASK_VENV)
    run(os.path.join(ASK_VENV, "bin", "pip"), "install", "--quiet", "--upgrade", "-r",
        os.path.join(HERE, "ask", "requirements.txt"))
    with open(stamp, "w") as f:
        f.write(want + "\n")
    print("  installed tangerine-ask's Python packages into %s" % ASK_VENV)
    return True


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default=os.path.join(HERE, "config.yaml"))
    ap.add_argument("--render-only", metavar="DIR", help="write rendered files under DIR and stop")
    a = ap.parse_args()

    c = load(a.config)
    files = render_all(c)

    if a.render_only:
        for dest, (content, _) in files.items():
            out = os.path.join(a.render_only, dest.lstrip("/"))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(content)
            print(out)
        return

    if os.geteuid() != 0:
        die("run as root (or use --render-only DIR to preview)")

    say("checks")
    missing = [p for p in ("nginx", "systemctl") if not shutil.which(p)]
    if c["tls"]["certbot"] and not shutil.which("certbot"):
        missing.append("certbot")
    if c["fail2ban"] and not shutil.which("fail2ban-client"):
        missing.append("fail2ban")
    if missing:
        die("install first: %s  (Fedora: dnf install nginx certbot python3-pyyaml python3-maxminddb; "
            "Debian: apt install nginx certbot python3-yaml python3-maxminddb)" % " ".join(missing))
    if c["gate"]["geoip"] and run("/usr/bin/python3", "-c", "import maxminddb", check=False, quiet=True).returncode:
        print("  note: python3-maxminddb not installed; logins will be logged without locations")
    if not os.path.isfile(APP_BINARY):
        die("%s missing -- run ./build.sh first" % APP_BINARY)
    if c["ask"]["enabled"]:
        if run("/usr/bin/python3", "-c", "import venv, ensurepip", check=False, quiet=True).returncode:
            die("ask.enabled needs Python's venv module (Debian: apt install python3-venv)")

    say("users")
    ensure_user("tangerine", "tangerine archive server")
    ensure_user("tangerine-gate", "tangerine login gate")
    for p in c["app"]["indexes"]:
        if run("runuser", "-u", "tangerine", "--", "test", "-r", p, check=False).returncode:
            die("the tangerine user can't read %s (check permissions on it and its parent directories)" % p)
    if c["ask"]["enabled"]:
        ensure_user("tangerine-ask", "tangerine LLM questions")
        for p in c["ask"]["indexes"]:
            if run("runuser", "-u", "tangerine-ask", "--", "test", "-r", p, check=False).returncode:
                die("the tangerine-ask user can't read %s" % p)

    say("programs")
    app_changed = put("/usr/local/bin/tangerine-server", read(APP_BINARY), 0o755)
    gate_changed = put("/usr/local/bin/tangerine-gate", read(os.path.join(HERE, "gate", "tangerine-gate")), 0o755)
    put("/usr/local/bin/tangerine-geoip-update", read(os.path.join(HERE, "gate", "tangerine-geoip-update")), 0o755)
    ask_changed = False
    if c["ask"]["enabled"]:
        ask_changed |= install_ask_venv()
        ask_changed |= put("/usr/local/bin/tangerine-ask", read(os.path.join(HERE, "ask", "tangerine-ask")), 0o755)
        for name in ("overlay.js", "overlay.css"):
            ask_changed |= put(ASK_SHARE + "/" + name, read(os.path.join(HERE, "ask", name)), 0o644)

    say("config")
    os.makedirs(ETC, mode=0o755, exist_ok=True)
    gate_changed |= put(ETC + "/config.yaml", read(a.config), 0o640, group="tangerine-gate")
    if not os.path.exists(ETC + "/passwords"):
        put(ETC + "/passwords", b"", 0o640, group="tangerine-gate")
    if c["ask"]["enabled"]:
        # Its own small config: tangerine-ask has no business reading the gate's.
        ask_changed |= put(ETC + "/ask.yaml", ask_config(c), 0o640, group="tangerine-ask")

    say("services")
    units = {d: files.pop(d) for d in list(files) if d.startswith("/etc/systemd/")}
    crawl_changed = False
    for dest, (content, mode) in units.items():
        changed = put(dest, content, mode)
        app_changed |= changed and dest.endswith("/tangerine.service")
        gate_changed |= changed and dest.endswith("/tangerine-gate.service")
        ask_changed |= changed and dest.endswith("/tangerine-ask.service")
        crawl_changed |= changed and dest.endswith("/tangerine-crawl.service")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "tangerine.service", "tangerine-gate.service")
    if app_changed:
        run("systemctl", "restart", "tangerine.service")
    if gate_changed:
        run("systemctl", "restart", "tangerine-gate.service")
    if c["ask"]["enabled"]:
        run("systemctl", "enable", "--now", "tangerine-crawl.service", "tangerine-ask.service")
        if app_changed or crawl_changed:   # same binary as the archive server
            run("systemctl", "restart", "tangerine-crawl.service")
        if ask_changed:
            run("systemctl", "restart", "tangerine-ask.service")
    else:
        run("systemctl", "disable", "--now", "tangerine-ask.service", "tangerine-crawl.service", check=False, quiet=True)
    if c["gate"]["geoip"]:
        run("systemctl", "enable", "--now", "tangerine-geoip.timer")
        if not os.path.exists("/var/lib/tangerine-gate/dbip-city-lite.mmdb"):
            if run("systemctl", "start", "tangerine-geoip.service", check=False).returncode:
                print("  geoip download failed; locations stay blank until the monthly timer succeeds")
    else:
        run("systemctl", "disable", "--now", "tangerine-geoip.timer", check=False, quiet=True)

    say("nginx")
    if selinux_enforcing() and run("getsebool", "httpd_can_network_connect", quiet=True).stdout.strip().endswith("off"):
        run("setsebool", "-P", "httpd_can_network_connect", "1")
        print("  SELinux: allowed nginx to connect to the local backends")
    os.makedirs(ACME_ROOT, exist_ok=True)
    if c["nginx"]["catch_all"] and re.search(r"listen[^;]*\b80\b[^;]*default_server", read("/etc/nginx/nginx.conf").decode()):
        die("/etc/nginx/nginx.conf already has a default_server on port 80. Remove that flag (or its server "
            "block), or set nginx.catch_all: false, then re-run.")
    https_conf = files.pop("/etc/nginx/conf.d/tangerine.conf")
    put("/etc/nginx/conf.d/tangerine-http.conf", *files.pop("/etc/nginx/conf.d/tangerine-http.conf"))
    cert, key = cert_paths(c)
    if not os.path.exists(cert):
        # Until there's a certificate, only the port-80 site can be live.
        if os.path.exists("/etc/nginx/conf.d/tangerine.conf"):
            os.remove("/etc/nginx/conf.d/tangerine.conf")
    run("nginx", "-t", quiet=True)
    run("systemctl", "enable", "--now", "nginx")
    run("systemctl", "reload", "nginx")

    if c["tls"]["certbot"]:
        say("certificate")
        if not os.path.exists(cert):
            cmd = ["certbot", "certonly", "--non-interactive", "--agree-tos", "--webroot", "-w", ACME_ROOT,
                   "-d", c["domain"], "--deploy-hook", "systemctl reload nginx"]
            cmd += ["-m", c["tls"]["email"]] if c["tls"]["email"] else ["--register-unsafely-without-email"]
            if run(*cmd, check=False).returncode:
                die("certbot failed. Check that %s resolves to this machine and port 80 reaches it, "
                    "then re-run." % c["domain"])
        for timer in ("certbot-renew.timer", "certbot.timer"):
            if run("systemctl", "enable", "--now", timer, check=False, quiet=True).returncode == 0:
                break
    elif not os.path.exists(cert):
        die("tls.certificate %s does not exist" % cert)

    put("/etc/nginx/conf.d/tangerine.conf", *https_conf)
    run("nginx", "-t", quiet=True)
    run("systemctl", "reload", "nginx")

    if files:
        say("fail2ban")
        for dest, (content, mode) in files.items():
            put(dest, content, mode)
        run("systemctl", "enable", "--now", "fail2ban")
        run("fail2ban-client", "reload", check=False)

    say("done")
    run("systemctl", "--no-pager", "--lines=0", "status", "tangerine", "tangerine-gate", "nginx",
        *(["tangerine-ask", "tangerine-crawl"] if c["ask"]["enabled"] else []), check=False)
    have_pw = {line.split(":", 1)[0] for line in read(ETC + "/passwords").decode().splitlines()}
    no_pw = [u for u in c["users"] if u not in have_pw]
    print("\nSite: https://%s/   Door log: https://%s/_gate/admin" % (c["domain"], c["domain"]))
    if no_pw:
        print("Set passwords for: %s\n  sudo tangerine-gate passwd NAME [--random]" % ", ".join(no_pw))


if __name__ == "__main__":
    main()
