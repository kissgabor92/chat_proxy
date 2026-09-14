"""
A standalone OpenAI-compatible endpoint in front of an Open WebUI instance.

Point it at any Open WebUI with LLM_HOSTNAME and it serves three routes:

  GET  /v1/models            -> <LLM_HOSTNAME>/api/models
  POST /v1/chat/completions  -> <LLM_HOSTNAME>/api/chat/completions
  GET  /health               -> liveness, no credential

Any other path is 404. This serves no interface -- Open WebUI has its own.

LLM_HOSTNAME accepts whatever you have: a bare host, a host:port, or a full URL
with a base path. All of these resolve to the same upstream:

    webui.example.com
    webui.example.com:3000
    http://127.0.0.1:3000
    https://ai.example.com/openwebui/

An https upstream is verified against the image's root store. Two different
faults fail that with the same "unable to get local issuer certificate": a CA
this container does not have, or a server that omits the intermediate above its
own certificate. Running this file with --tls-check says which, and names the
certificate to put in UPSTREAM_CA_BUNDLE; --find-ca looks for that certificate on
the machine it is run from, and --autocert fetches it, keeps it and proves it
works. UPSTREAM_TLS_VERIFY=false stops checking altogether.

Standard library only. It exists to translate, not to decide:

1. Open WebUI answers on /api/... paths, not the /v1/... an OpenAI-compatible
   client expects, and its model list carries fields no such client reads.

2. Its replies deviate from the OpenAI schema in two ways that matter to VS
   Code -- a non-standard `reasoning_content` field, and `finish_reason` left as
   "stop" on a response that actually carries tool_calls, which an agent reads
   as "the model is done" and so never runs the tool.
"""

import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def env_flag(name, default):
    """A true/false setting, tolerant of what a .env line actually contains.

    An unstripped `UPSTREAM_TLS_VERIFY=false ` reads as true and silently keeps
    verifying, which is the kind of thing you debug for an hour.
    """
    return os.environ.get(name, default).strip().strip("\"'").lower() == "true"


LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8111"))

# Inference can take minutes on a cold model load; a short timeout here shows up
# as a truncated reply in the editor rather than as an error.
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "600"))

# The Open WebUI token requests travel upstream with when the caller supplies
# none, so a client that cannot hold a credential still works. A caller's own
# token always wins, so per-user model permissions still apply.
WEBUI_TOKEN = os.environ.get("WEBUI_TOKEN", "").strip()

# Some deployments put an SSO gateway in front of Open WebUI, and it authenticates
# by cookie rather than by bearer token -- a request carrying only a token is
# anonymous to it ("no session ID found" and friends). This is the raw Cookie
# header to send upstream, e.g. "session=abc123". A caller's own Cookie wins.
UPSTREAM_COOKIE = os.environ.get("UPSTREAM_COOKIE", "").strip()

# With a pinned token, a request carrying no credential is answered using it.
# Convenient, but it means anything that can reach this port can use the model.
# Set true to demand a bearer token anyway.
REQUIRE_CLIENT_TOKEN = env_flag("REQUIRE_CLIENT_TOKEN", "false")

# An https upstream is verified against the system roots. Certificates in this
# PEM file are trusted *in addition* to them. It must hold every certificate
# between the upstream's own and a trusted root that the server does not send
# itself -- a root alone does not bridge a missing intermediate. `--tls-check`
# says which ones those are.
UPSTREAM_CA_BUNDLE = os.environ.get("UPSTREAM_CA_BUNDLE", "").strip()

# Set false to accept any certificate. It removes the only protection against
# something answering in the upstream's place, so it is a last resort, not the
# fix for a chain the upstream should be serving properly.
UPSTREAM_TLS_VERIFY = env_flag("UPSTREAM_TLS_VERIFY", "true")


def parse_upstream(value):
    """Turn LLM_HOSTNAME into (use_tls, host, port, base_path).

    Accepts a bare host, a host:port, or a full URL with a base path, because
    "the hostname of my Open WebUI" means all three to different people and
    guessing wrong fails with a DNS error that explains nothing.
    """
    value = (value or "").strip()
    if not value:
        raise SystemExit(
            "LLM_HOSTNAME is not set -- point it at an Open WebUI instance, "
            "e.g. http://127.0.0.1:3000 or webui.example.com"
        )
    # urlsplit only finds a host when a scheme is present; add the default first.
    if "://" not in value:
        value = "http://" + value
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise SystemExit("LLM_HOSTNAME scheme must be http or https, got %r" % parts.scheme)
    if not parts.hostname:
        raise SystemExit("LLM_HOSTNAME has no host: %r" % value)
    tls = parts.scheme == "https"
    port = parts.port or (443 if tls else 80)
    # A base path lets Open WebUI live behind a reverse proxy on a subpath.
    base = parts.path.rstrip("/")
    return tls, parts.hostname, port, base


USE_TLS, UPSTREAM_HOST, UPSTREAM_PORT, UPSTREAM_BASE = parse_upstream(
    os.environ.get("LLM_HOSTNAME", "")
)


def upstream_label():
    scheme = "https" if USE_TLS else "http"
    return "%s://%s:%d%s" % (scheme, UPSTREAM_HOST, UPSTREAM_PORT, UPSTREAM_BASE)


def log(*parts):
    print(*parts, file=sys.stderr, flush=True)


def build_ssl_context(verify=None):
    """The TLS context every upstream request uses, built once at startup.

    A bad CA file is fatal here rather than a 502 on each request: it is a
    configuration mistake, and failing at startup names it while the operator is
    still looking. `verify=True` overrides the setting, so --tls-check can report
    what verification would find even when it is switched off.
    """
    if not USE_TLS:
        return None
    if not (UPSTREAM_TLS_VERIFY if verify is None else verify):
        if verify is None:
            log("warning: UPSTREAM_TLS_VERIFY=false -- the upstream certificate "
                "is not checked, so this connection is not authenticated")
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    context = ssl.create_default_context()
    if UPSTREAM_CA_BUNDLE:
        try:
            if os.path.isdir(UPSTREAM_CA_BUNDLE):
                context.load_verify_locations(capath=UPSTREAM_CA_BUNDLE)
            else:
                context.load_verify_locations(cafile=UPSTREAM_CA_BUNDLE)
        except OSError as exc:
            raise SystemExit("UPSTREAM_CA_BUNDLE %r is unusable: %s"
                             % (UPSTREAM_CA_BUNDLE, exc))
        if verify is None:
            log("trusting %s in addition to the system roots" % UPSTREAM_CA_BUNDLE)
    return context


SSL_CONTEXT = build_ssl_context()


def upstream_error(exc):
    """One 502 detail for every way the upstream can fail to answer.

    A certificate failure is singled out because the OpenSSL wording
    ("unable to get local issuer certificate") describes the symptom and none of
    the two things that cause it: a private CA, or a server that does not send
    its intermediates.
    """
    if isinstance(exc, ssl.SSLCertVerificationError):
        return ("Open WebUI certificate not trusted at %s: %s -- the chain it "
                "presents does not reach a CA this container trusts. Run "
                "`./init.sh --tls` to see what it sent and which certificate is "
                "missing; put that one in UPSTREAM_CA_FILE (the root alone will "
                "not do if the server omits an intermediate), or set "
                "UPSTREAM_TLS_VERIFY=false to stop checking (unauthenticated)."
                % (upstream_label(), exc))
    if isinstance(exc, ssl.SSLError):
        return "Open WebUI TLS handshake failed at %s: %s" % (upstream_label(), exc)
    return "Open WebUI unreachable at %s: %s" % (upstream_label(), exc)


def decode_cert(der):
    """One certificate's fields, out of the DER bytes the server sent.

    There is no public way to decode an arbitrary certificate. A store decodes
    what it holds, but keeps only CAs, so a server's own certificate comes back
    from it as nothing; the private decoder covers that case and its absence is
    not fatal -- the check then reports the chain's shape but not its names.
    """
    store = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    store.check_hostname = False
    store.verify_mode = ssl.CERT_NONE
    try:
        store.load_verify_locations(cadata=der)
        held = store.get_ca_certs()
        if held:
            return held[0]
    except (OSError, ValueError):
        pass
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".pem") as pem:
            pem.write(ssl.DER_cert_to_PEM_cert(der))
            pem.flush()
            return ssl._ssl._test_decode_cert(pem.name)
    except Exception:  # a private decoder: any failure means "no names", not a crash
        return None


def common_name(rdn_sequence):
    """The readable name out of getpeercert()'s nested RDN tuples."""
    fields = dict(pair for rdn in rdn_sequence or () for pair in rdn)
    return (fields.get("commonName") or fields.get("organizationName")
            or str(rdn_sequence))


# Where a machine keeps the CAs it has been told to trust. Read only by
# --find-ca, to answer "is the missing certificate already on this host?" -- the
# usual case for an internal CA, which the admin installed here and nowhere else.
CA_SEARCH_PATHS = (
    "/usr/local/share/ca-certificates",   # debian/ubuntu: what an operator adds
    "/etc/pki/ca-trust/source/anchors",   # rhel/fedora: the same
    "/etc/ssl/certs/ca-certificates.crt",  # debian/alpine: the compiled bundle
    "/etc/pki/tls/certs/ca-bundle.crt",   # rhel: the same
    "/etc/ssl/cert.pem",
    "/usr/share/ca-certificates",
    "/etc/ssl/certs",
)


def upstream_chain():
    """The certificates the upstream sends, decoded. Trusting none of them.

    Reading what a server presents needs no trust; that is the point -- it is
    the evidence for why the trusted path could not be built.
    """
    probe = ssl.create_default_context()
    probe.check_hostname = False
    probe.verify_mode = ssl.CERT_NONE
    with socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=10) as raw:
        with probe.wrap_socket(raw, server_hostname=UPSTREAM_HOST) as sock:
            return [decode_cert(der) for der in (sock.get_unverified_chain() or ())]


def cas_in(path):
    """(subject, DER) for every certificate in a PEM file. [] if it holds none.

    Only CAs: a store keeps nothing else, which is what makes it a trust store.
    """
    store = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    store.check_hostname = False
    store.verify_mode = ssl.CERT_NONE
    try:
        store.load_verify_locations(cafile=path)
    except (OSError, ValueError):
        return []
    held = []
    for der in store.get_ca_certs(binary_form=True):
        info = decode_cert(der)
        if info:
            held.append((common_name(info.get("subject")), der))
    return held


def certs_in(path):
    """Subject names of every certificate in a PEM file."""
    return [subject for subject, _ in cas_in(path)]


def find_ca(name, paths=CA_SEARCH_PATHS):
    """Files under `paths` holding a certificate whose subject matches `name`."""
    wanted = name.strip().casefold()
    found = []
    for path in paths:
        if os.path.isdir(path):
            files = [os.path.join(path, entry) for entry in sorted(os.listdir(path))
                     if entry.endswith((".crt", ".pem"))]
        else:
            files = [path]
        for candidate in files:
            if not os.path.isfile(candidate):
                continue
            for subject in certs_in(candidate):
                if subject.strip().casefold() == wanted:
                    found.append(candidate)
                    break
    return found


def completes_chain(path):
    """Whether trusting this file, plus the public roots, verifies the upstream.

    Holding the right name is not the same as fixing the problem: a file with
    the intermediate but not the private root above it still fails. Better to
    try each candidate than to recommend one and be wrong.
    """
    context = ssl.create_default_context()
    try:
        context.load_verify_locations(cafile=path)
        with socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=10) as raw:
            context.wrap_socket(raw, server_hostname=UPSTREAM_HOST).close()
    except (OSError, ValueError):
        return False
    return True


# A certificate that points at its issuer can be walked upward. Bounded, so a
# PKI that points at itself cannot spin this forever.
AIA_HOPS = 5


def fetch(url):
    """The bytes an AIA URL serves."""
    with urllib.request.urlopen(url, timeout=15) as answer:
        return answer.read()


def pkcs7_certs(blob):
    """PKCS#7 is what some PKIs publish, and what the standard library cannot read.

    openssl can, when the machine running this has it; a machine without it
    loses this one publication format, not the command.
    """
    for form in ("DER", "PEM"):
        try:
            done = subprocess.run(
                ["openssl", "pkcs7", "-inform", form, "-print_certs"],
                input=blob, capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return []
        if done.returncode == 0 and b"BEGIN CERTIFICATE" in done.stdout:
            return pems_in(done.stdout)
    return []


def pems_in(blob):
    """Every certificate in a payload, as PEM text, whatever form it arrived in."""
    text = blob.decode("ascii", "ignore")
    if "BEGIN CERTIFICATE" in text:
        head, tail = "-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----"
        return [head + part.split(tail)[0] + tail + "\n"
                for part in text.split(head)[1:] if tail in part]
    # DER_cert_to_PEM_cert re-encodes anything it is given, so ask whether the
    # bytes are a certificate before believing the result.
    if decode_cert(blob) is not None:
        return [ssl.DER_cert_to_PEM_cert(blob)]
    return pkcs7_certs(blob)


def climb_aia(start):
    """PEMs for the issuers above `start`, followed through caIssuers URLs."""
    collected = []
    current = start
    for _ in range(AIA_HOPS):
        pems = []
        for url in (current or {}).get("caIssuers", ()) or ():
            log("  fetching %s" % url)
            try:
                pems = pems_in(fetch(url))
            except (OSError, ValueError) as exc:
                log("    %s" % exc)
                continue
            if pems:
                break
        if not pems:
            break
        collected.extend(pems)
        current = decode_cert(ssl.PEM_cert_to_DER_cert(pems[0]))
        if current is None:
            break
        log("  got %s" % common_name(current.get("subject")))
        if common_name(current.get("subject")) == common_name(current.get("issuer")):
            break  # self-signed: the root, and the top of the climb
    return collected


def write_pem(path, pems):
    """One PEM file, duplicates dropped, replaced atomically."""
    unique = []
    for pem in pems:
        if pem not in unique:
            unique.append(pem)
    scratch = path + ".new"
    with open(scratch, "w") as out:
        out.write("".join(unique))
    os.replace(scratch, path)
    return len(unique)


def autocert_mode(directory):
    """Find the certificate this upstream needs, keep it, and prove it works.

    Two sources, cheapest first: a trust store on this machine, then the PKI the
    certificate itself points at. Whatever is collected is only kept if it
    actually verifies the upstream -- a file that does not fix anything is worse
    than no file, because it looks like the problem is solved.
    """
    if not USE_TLS:
        log("%s is plain http -- no certificate needed" % upstream_label())
        return 0
    try:
        sent = upstream_chain()
    except OSError as exc:
        log("cannot reach %s: %s" % (upstream_label(), exc))
        return 2
    if not sent or not sent[-1]:
        log("could not read the certificate chain from %s" % upstream_label())
        return 2

    verifying = build_ssl_context(verify=True)
    try:
        with socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=10) as raw:
            verifying.wrap_socket(raw, server_hostname=UPSTREAM_HOST).close()
        log("%s already verifies -- nothing to fetch." % upstream_label())
        return 0
    except ssl.SSLCertVerificationError:
        pass
    except OSError as exc:
        log("handshake failed: %s" % exc)
        return 2

    needed = common_name(sent[-1].get("issuer"))
    log("%s needs %r, which is not in the container's trust store."
        % (upstream_label(), needed))

    collected = []
    for path in find_ca(needed, CA_SEARCH_PATHS):
        for subject, der in cas_in(path):
            if subject.strip().casefold() == needed.strip().casefold():
                log("  found it in %s" % path)
                collected.append(ssl.DER_cert_to_PEM_cert(der))
                break
        if collected:
            break
    if not collected:
        log("not on this machine either; following the certificate's own AIA:")
        collected = climb_aia(sent[-1])
    if not collected:
        log("")
        log("could not obtain %r: it is not on this machine, and the "
            "certificate publishes no usable CA Issuers URL." % needed)
        log("ask whoever runs the CA for it, then set UPSTREAM_CA_FILE to it "
            "(see tls_101.md).")
        return 1

    os.makedirs(directory, exist_ok=True)
    # Two Open WebUIs on one host, different ports, must not share a file.
    label = UPSTREAM_HOST if UPSTREAM_PORT == 443 else "%s_%d" % (UPSTREAM_HOST, UPSTREAM_PORT)
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in label)
    target = os.path.join(directory, safe + ".pem")
    count = write_pem(target, collected)

    if not completes_chain(target):
        # Climbing further is the one thing left to try: what was found may be
        # an intermediate whose own issuer is missing too.
        more = climb_aia(decode_cert(ssl.PEM_cert_to_DER_cert(collected[-1])))
        if more:
            count = write_pem(target, collected + more)
    if not completes_chain(target):
        os.remove(target)
        log("")
        log("what could be collected does not verify %s -- something above %r "
            "is missing too." % (upstream_label(), needed))
        log("ask for the full chain: that certificate and every one up to the root.")
        return 1

    log("")
    log("verified: %d certificate(s) in %s complete the chain to %s."
        % (count, target, upstream_label()))
    log("UPSTREAM_CA_FILE=%s" % target)
    return 0


def find_ca_mode():
    """Look on this machine for the certificate the upstream chain is missing.

    An internal CA is usually installed on the hosts that need it and nowhere
    else, so the file that fixes the container is very often already here --
    and this is run on the host precisely because the container's store is the
    one that does not have it.
    """
    if not USE_TLS:
        log("%s is plain http -- no certificate involved" % upstream_label())
        return 0
    try:
        sent = upstream_chain()
    except OSError as exc:
        log("cannot reach %s: %s" % (upstream_label(), exc))
        return 2
    if not sent or not sent[-1]:
        log("could not read the certificate chain from %s" % upstream_label())
        return 2

    needed = common_name(sent[-1].get("issuer"))
    found = find_ca(needed, CA_SEARCH_PATHS)
    if not found:
        log("%r is not in this host's trust store either." % needed)
        log("get it from whoever runs your CA, or export it from a browser that "
            "trusts this site, then:")
        log("  UPSTREAM_CA_FILE=/path/to/that.pem   # in .env")
        log("check it is the right one before restarting anything:")
        log("  UPSTREAM_CA_BUNDLE=/path/to/that.pem LLM_HOSTNAME=%s \\"
            % os.environ.get("LLM_HOSTNAME", upstream_label()))
        log("      python3 proxy.py --tls-check")
        return 1

    log("this host already trusts %r. It is in:" % needed)
    for path in found:
        log("  %s" % path)

    works = [path for path in found if completes_chain(path)]
    log("")
    if not works:
        log("none of them verifies %s on its own -- something above %r is "
            "missing here too." % (upstream_label(), needed))
        log("ask for the full chain: that certificate and every one up to the root.")
        return 1
    log("verified: %d of them complete%s the chain to %s:"
        % (len(works), "" if len(works) > 1 else "s", upstream_label()))
    for path in works:
        log("  %s" % path)
    log("the container does not have it, so hand it one of those -- in .env:")
    log("  UPSTREAM_CA_FILE=%s" % works[0])
    log("then: ./init.sh --no-build")
    return 0


def tls_check():
    """Say why the upstream's certificate is or is not trusted, with evidence.

    "unable to get local issuer certificate" has two causes that need opposite
    fixes, and the message distinguishes neither: a CA nobody here has, or a
    server that sends its own certificate and none of the ones above it. Both
    show as one broken link, so this prints the chain the server actually sends
    and names the certificate that would complete it.
    """
    if not USE_TLS:
        log("%s is plain http -- no certificate involved" % upstream_label())
        return 0

    try:
        sent = upstream_chain()
    except OSError as exc:
        log("cannot reach %s: %s" % (upstream_label(), exc))
        return 2

    log("%s sends %d certificate(s):" % (upstream_label(), len(sent)))
    for depth, info in enumerate(sent):
        if info is None:
            log("  %d. (could not be decoded here)" % depth)
            continue
        log("  %d. %s" % (depth, common_name(info.get("subject"))))
        log("     issued by %s" % common_name(info.get("issuer")))

    if not UPSTREAM_TLS_VERIFY:
        log("")
        log("UPSTREAM_TLS_VERIFY=false: requests succeed whatever the chain says.")
        log("what follows is what verification would find if it were on.")
    verifying = build_ssl_context(verify=True)
    try:
        with socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=10) as raw:
            verifying.wrap_socket(raw, server_hostname=UPSTREAM_HOST).close()
    except ssl.SSLCertVerificationError as exc:
        log("")
        log("NOT trusted: %s" % (exc.verify_message or exc))
        if sent and sent[-1]:
            top = sent[-1]
            needed = common_name(top.get("issuer"))
            if len(sent) == 1:
                log("the server sent only its own certificate, so nothing links it "
                    "to a root.")
            log("the chain stops at %r, and its issuer %r is not in this trust store."
                % (common_name(top.get("subject")), needed))
            log("get the certificate for %r -- and any above it, up to the root --" % needed)
            for url in top.get("caIssuers", ()) or ():
                log("  it is usually published at %s" % url)
            log("put them all in one PEM file and set UPSTREAM_CA_FILE to it.")
            log("to see the same chain with openssl:")
            log("  openssl s_client -showcerts -connect %s:%d -servername %s </dev/null"
                % (UPSTREAM_HOST, UPSTREAM_PORT, UPSTREAM_HOST))
        return 1
    except OSError as exc:
        log("handshake failed: %s" % exc)
        return 2

    log("")
    log("trusted%s" % (" (via %s)" % UPSTREAM_CA_BUNDLE if UPSTREAM_CA_BUNDLE else ""))
    if not UPSTREAM_TLS_VERIFY:
        log("UPSTREAM_TLS_VERIFY=false is no longer buying you anything -- "
            "unset it and the proxy verifies this chain.")
    return 0


def normalise(choice):
    """Bring one Open WebUI choice back to the OpenAI schema.

    `reasoning_content` is not an OpenAI field. It is dropped, but promoted to
    `content` first if the model spent its whole budget thinking and left
    `content` empty -- otherwise the editor renders a blank reply.

    `finish_reason` comes back as "stop" even when the message carries
    tool_calls. An agent reads "stop" as "the model is done" and never runs the
    tool, so such a response is corrected to "tool_calls".
    """
    if not isinstance(choice, dict):
        return choice
    message = choice.get("message") or {}
    reasoning = message.pop("reasoning_content", None)
    if not message.get("content") and reasoning and not message.get("tool_calls"):
        message["content"] = reasoning
    if message.get("tool_calls") and choice.get("finish_reason") == "stop":
        choice["finish_reason"] = "tool_calls"
    return choice


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "webui-bridge/1.0"

    def log_message(self, fmt, *args):
        log("%s - %s" % (self.address_string(), fmt % args))

    # ---------- helpers ----------

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_detail(self, status, detail):
        """FastAPI's error shape, so clients written against Open WebUI parse it."""
        if status >= 500:
            # A client shows "Server error: 502" and little else; the reason
            # has to be findable here.
            log("%d: %s" % (status, detail))
        self.send_json(status, {"detail": detail})

    def relay_error(self, status, raw):
        """Pass an upstream refusal through in the shape it arrived in.

        Its body is nearly always JSON already. Wrapping that in a string gives
        the client {"detail": "{\"detail\": \"...\"}"} -- the real message,
        behind two layers of quoting, in the one response someone is reading
        closely.
        """
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = None
        if status in (401, 403) and not (self.headers.get("Cookie") or UPSTREAM_COOKIE):
            # Said here rather than in the body, which belongs to the upstream.
            log("upstream refused with %d and no cookie was sent -- if it sits "
                "behind an SSO gateway, that gateway wants a session cookie: "
                "set UPSTREAM_COOKIE" % status)
        if status >= 500:
            # An upstream 5xx is usually the gateway in front of Open WebUI
            # failing to reach it (nginx's own "502 Bad Gateway" page), not
            # Open WebUI itself. Say what came back, since the client will not.
            log("upstream answered %d: %s" % (status, raw.decode(errors="replace")[:200].replace("\n", " ")))
        if isinstance(parsed, dict):
            return self.send_json(status, parsed)
        return self.send_detail(status, raw.decode(errors="replace")[:500])

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def bearer(self):
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return self.headers.get("api-key", "").strip()

    def webui(self, method, path, body=None):
        token = self.bearer() or WEBUI_TOKEN
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        # Same precedence as the token: what the caller sent, else what is pinned.
        cookie = self.headers.get("Cookie", "").strip() or UPSTREAM_COOKIE
        if cookie:
            headers["Cookie"] = cookie
        if USE_TLS:
            conn = http.client.HTTPSConnection(UPSTREAM_HOST, UPSTREAM_PORT,
                                               timeout=UPSTREAM_TIMEOUT,
                                               context=SSL_CONTEXT)
        else:
            conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT,
                                              timeout=UPSTREAM_TIMEOUT)
        conn.request(method, UPSTREAM_BASE + path, body=body, headers=headers)
        return conn.getresponse()

    # ---------- routing ----------

    def route(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

        if path == "/health":
            return self.send_json(200, {
                "status": "ok",
                "upstream": upstream_label(),
                "pinned_token_configured": bool(WEBUI_TOKEN),
                "pinned_cookie_configured": bool(UPSTREAM_COOKIE),
                "client_token_required": REQUIRE_CLIENT_TOKEN,
                "upstream_tls_verified": bool(USE_TLS and UPSTREAM_TLS_VERIFY),
            })

        if path not in ("/v1/models", "/v1/chat/completions"):
            # Deliberately not a pass-through. This port is the model endpoint
            # only; the Open WebUI interface lives on its own port.
            return self.send_json(404, {
                "detail": "no such route: %s" % self.path,
                "routes": ["GET /v1/models", "POST /v1/chat/completions", "GET /health"],
            })

        if REQUIRE_CLIENT_TOKEN and not self.bearer():
            return self.send_detail(401, "Not authenticated")
        if not (self.bearer() or WEBUI_TOKEN):
            return self.send_detail(401, "Not authenticated")

        if path == "/v1/models":
            if self.command != "GET":
                return self.send_detail(405, "use GET on /v1/models")
            return self.handle_models()

        if self.command != "POST":
            return self.send_detail(405, "use POST on /v1/chat/completions")
        return self.handle_chat()

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = route

    # ---------- the two real routes ----------

    def handle_models(self):
        """Open WebUI's /api/models, reduced to the OpenAI list schema.

        Its entries carry extra keys (tags, actions, filters, connection_type)
        that no OpenAI client reads; passing them through invites a client to
        choke on a shape it did not expect.
        """
        try:
            resp = self.webui("GET", "/api/models")
            raw = resp.read()
        except OSError as exc:
            return self.send_detail(502, upstream_error(exc))
        if resp.status != 200:
            return self.relay_error(resp.status, raw)

        try:
            items = json.loads(raw).get("data", [])
        except (json.JSONDecodeError, AttributeError) as exc:
            return self.send_detail(502, "unreadable model list: %s" % exc)

        return self.send_json(200, {
            "object": "list",
            "data": [{
                "id": m.get("id"),
                "object": "model",
                "created": m.get("created", 0),
                "owned_by": m.get("owned_by", "open-webui"),
            } for m in items if m.get("id")],
        })

    def handle_chat(self):
        raw = self.read_body() or b"{}"
        try:
            # Parsed only to learn whether the caller asked for a stream; the
            # original bytes are forwarded, so nothing the client sent is
            # silently rewritten on the way through.
            wants_stream = bool(json.loads(raw).get("stream"))
        except (json.JSONDecodeError, AttributeError) as exc:
            return self.send_detail(400, "malformed JSON body: %s" % exc)

        try:
            resp = self.webui("POST", "/api/chat/completions", body=raw)
        except OSError as exc:
            return self.send_detail(502, upstream_error(exc))

        if resp.status >= 400:
            return self.relay_error(resp.status, resp.read())

        if wants_stream:
            return self.relay_stream(resp)

        try:
            data = json.loads(resp.read())
        except json.JSONDecodeError as exc:
            return self.send_detail(502, "unreadable completion: %s" % exc)
        for choice in data.get("choices", []):
            normalise(choice)
        return self.send_json(200, data)

    def relay_stream(self, response):
        """Re-emit Open WebUI's SSE as OpenAI-shaped chunks.

        `reasoning_content` deltas are accumulated rather than forwarded: they
        are not an OpenAI field, and a chunk carrying only reasoning has no
        content to render. If the stream ends having produced no content at all,
        the accumulation is flushed as one content chunk so the reply is never
        blank.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        saw_content = False
        reasoning_parts = []
        last_chunk = None

        def emit(blob):
            self.wfile.write(blob)
            self.wfile.flush()

        try:
            for line in response:
                if not line.strip():
                    continue
                if not line.startswith(b"data: "):
                    emit(line)
                    continue
                data = line[6:].strip()
                if data == b"[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                last_chunk = chunk
                keep = False
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta") or {}
                    reasoning = delta.pop("reasoning_content", None)
                    if reasoning:
                        reasoning_parts.append(reasoning)
                    if delta.get("content"):
                        saw_content = True
                    if delta.get("tool_calls") and choice.get("finish_reason") == "stop":
                        choice["finish_reason"] = "tool_calls"
                    # A delta emptied by stripping carries no information, and
                    # forwarding it makes a client count empty tokens.
                    if delta or choice.get("finish_reason"):
                        keep = True
                if keep:
                    emit(b"data: " + json.dumps(chunk).encode() + b"\n\n")

            if not saw_content and reasoning_parts:
                emit(b"data: " + json.dumps({
                    "id": (last_chunk or {}).get("id", "chatcmpl-fallback"),
                    "object": "chat.completion.chunk",
                    "created": (last_chunk or {}).get("created", 0),
                    "model": (last_chunk or {}).get("model", ""),
                    "choices": [{"index": 0,
                                 "delta": {"role": "assistant",
                                           "content": "".join(reasoning_parts)},
                                 "finish_reason": None}],
                }).encode() + b"\n\n")

            emit(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            log("client disconnected mid-stream")


def main():
    if "--tls-check" in sys.argv[1:]:
        raise SystemExit(tls_check())
    if "--find-ca" in sys.argv[1:]:
        raise SystemExit(find_ca_mode())
    if "--autocert" in sys.argv[1:]:
        where = sys.argv[sys.argv.index("--autocert") + 1:]
        if not where:
            raise SystemExit("--autocert needs the directory to keep the certificate in")
        raise SystemExit(autocert_mode(where[0]))
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    server.daemon_threads = True
    log("openai endpoint on %s:%d -> %s (pinned token: %s, pinned cookie: %s)"
        % (LISTEN_HOST, LISTEN_PORT, upstream_label(),
           "yes" if WEBUI_TOKEN else "no", "yes" if UPSTREAM_COOKIE else "no"))
    server.serve_forever()


if __name__ == "__main__":
    main()
