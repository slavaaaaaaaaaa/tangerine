#!/usr/bin/python3
"""Install or update tangerine from config.yaml.

    sudo ./install.py [-c config.yaml]         install / update everything
    ./install.py --render-only DIR [-c ...]    just write the rendered files to DIR for review

Safe to re-run: files are only replaced (and services only restarted) when they change.
"""
import argparse
import grp
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
ACME_ROOT = "/var/www/letsencrypt"

DEFAULTS = {
    "app": {"indexes": [], "site_name": "Tangerine", "slogan": "Freshly squeezed",
            "footer_phrase": "made with 100% pulp",
            "bundles": True, "logout_link": True, "memory_max": "12G", "listen": "127.0.0.1:8081"},
    "gate": {"listen": "127.0.0.1:8082", "geoip": True},
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

LOGOUT_LINK = """        # Uncompressed upstream so sub_filter can add a logout link to every page.
        proxy_set_header Accept-Encoding "";
        sub_filter '</body>' '<a href="/_gate/logout" style="position:fixed;right:10px;bottom:10px;font:12px system-ui,sans-serif;color:#e8741c;background:#fff9;padding:3px 8px;border-radius:8px;text-decoration:none">log out</a></body>';
        sub_filter_once on;
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
    for key in ("app", "gate"):
        if not re.fullmatch(r"127\.0\.0\.1:\d+|\[::1\]:\d+|localhost:\d+", str(c[key]["listen"])):
            errors.append("%s.listen: must be a localhost address, the gate is the only thing guarding it" % key)
    if errors:
        die("config problems:\n  " + "\n  ".join(errors))
    return c


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
    # systemd quoting: double quotes, with backslash, quote and $ / % escaped
    exec_line = " ".join('"%s"' % a.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$").replace("%", "%%")
                         for a in args)
    cert, key = cert_paths(c)
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
        "logout_link": LOGOUT_LINK if app["logout_link"] else "",
        "app_exec": exec_line,
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

    say("users")
    ensure_user("tangerine", "tangerine archive server")
    ensure_user("tangerine-gate", "tangerine login gate")
    for p in c["app"]["indexes"]:
        if run("runuser", "-u", "tangerine", "--", "test", "-r", p, check=False).returncode:
            die("the tangerine user can't read %s (check permissions on it and its parent directories)" % p)

    say("programs")
    app_changed = put("/usr/local/bin/tangerine-server", read(APP_BINARY), 0o755)
    gate_changed = put("/usr/local/bin/tangerine-gate", read(os.path.join(HERE, "gate", "tangerine-gate")), 0o755)
    put("/usr/local/bin/tangerine-geoip-update", read(os.path.join(HERE, "gate", "tangerine-geoip-update")), 0o755)

    say("config")
    os.makedirs(ETC, mode=0o755, exist_ok=True)
    gate_changed |= put(ETC + "/config.yaml", read(a.config), 0o640, group="tangerine-gate")
    if not os.path.exists(ETC + "/passwords"):
        put(ETC + "/passwords", b"", 0o640, group="tangerine-gate")

    say("services")
    units = {d: files.pop(d) for d in list(files) if d.startswith("/etc/systemd/")}
    for dest, (content, mode) in units.items():
        changed = put(dest, content, mode)
        app_changed |= changed and dest.endswith("/tangerine.service")
        gate_changed |= changed and dest.endswith("/tangerine-gate.service")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "tangerine.service", "tangerine-gate.service")
    if app_changed:
        run("systemctl", "restart", "tangerine.service")
    if gate_changed:
        run("systemctl", "restart", "tangerine-gate.service")
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
    run("systemctl", "--no-pager", "--lines=0", "status", "tangerine", "tangerine-gate", "nginx", check=False)
    have_pw = {line.split(":", 1)[0] for line in read(ETC + "/passwords").decode().splitlines()}
    no_pw = [u for u in c["users"] if u not in have_pw]
    print("\nSite: https://%s/   Door log: https://%s/_gate/admin" % (c["domain"], c["domain"]))
    if no_pw:
        print("Set passwords for: %s\n  sudo tangerine-gate passwd NAME [--random]" % ", ".join(no_pw))


if __name__ == "__main__":
    main()
