#!/usr/bin/env python3
import argparse
import base64
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

BASE = Path("/root/docker-nginx/certs/le")
VERSIONS = BASE / "versions"
CURRENT = BASE / "current"
SECRET_URL = "/api/v1/namespaces/slb/secrets/bestnathan-top-tls"
PROBE_HOST = "mcp-nession.bestnathan.top"
CA_FILE = os.environ.get("CERT_SYNC_CA_FILE", "/etc/ssl/certs/ca-certificates.crt")


def run(args, input_text=None):
    return subprocess.run(args, input=input_text, text=True, capture_output=True,
                          check=True, timeout=20).stdout


def fetch_secret():
    endpoint = os.environ["KUBERNETES_API_URL"].rstrip("/") + SECRET_URL
    token = Path(os.environ["KUBERNETES_TOKEN_FILE"]).read_text().strip()
    context = ssl.create_default_context(cafile=os.environ["KUBERNETES_CA_FILE"])
    request = urllib.request.Request(endpoint, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urllib.request.urlopen(request, context=context, timeout=20) as response:
        document = json.load(response)
    if document["metadata"]["name"] != "bestnathan-top-tls":
        raise RuntimeError("unexpected Secret identity")
    return (base64.b64decode(document["data"]["tls.crt"]),
            base64.b64decode(document["data"]["tls.key"]))


def validate_certificate(cert, key, directory):
    chain_path = directory / "chain.pem"
    key_path = directory / "tls.key"
    chain_path.write_bytes(cert)
    key_path.write_bytes(key)
    key_path.chmod(0o600)
    blocks = re.findall(
        b"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----\\n?",
        cert, flags=re.DOTALL)
    if len(blocks) < 2:
        raise RuntimeError("tls.crt does not contain a full chain")
    leaf = directory / "leaf.pem"
    intermediates = directory / "intermediates.pem"
    leaf.write_bytes(blocks[0])
    intermediates.write_bytes(b"".join(blocks[1:]))
    run(["openssl", "verify", "-purpose", "sslserver", "-CAfile", CA_FILE,
         "-untrusted", str(intermediates), str(leaf)])
    run(["openssl", "x509", "-in", str(leaf), "-noout", "-checkend", "86400"])
    run(["openssl", "x509", "-in", str(leaf), "-noout", "-checkhost", PROBE_HOST])
    cert_pub = run(["openssl", "x509", "-in", str(leaf), "-pubkey", "-noout"])
    key_pub = run(["openssl", "pkey", "-in", str(key_path), "-pubout"])
    if cert_pub != key_pub:
        raise RuntimeError("certificate and private key do not match")
    output = run(["openssl", "x509", "-in", str(leaf), "-noout", "-serial"])
    serial = output.strip().split("=", 1)[1].upper()
    if not re.fullmatch(r"[0-9A-F]+", serial):
        raise RuntimeError("invalid serial")
    return serial


def candidate():
    VERSIONS.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".candidate-", dir=VERSIONS))
    try:
        cert, key = fetch_secret()
        serial = validate_certificate(cert, key, temp)
        destination = VERSIONS / serial
        if destination.exists():
            shutil.rmtree(temp)
        else:
            temp.rename(destination)
        return destination, serial
    except Exception:
        if temp.exists():
            shutil.rmtree(temp)
        raise


def switch_symlink(target):
    temporary = BASE / ".current.tmp"
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(Path("versions") / target.name)
    os.replace(temporary, CURRENT)


def nginx_test():
    run(["docker", "exec", "nginx-proxy", "nginx", "-t"])


def nginx_reload():
    run(["docker", "exec", "nginx-proxy", "nginx", "-s", "reload"])


def verify_served(serial):
    for attempt in range(6):
        process = subprocess.run([
            "openssl", "s_client", "-connect", "127.0.0.1:443",
            "-servername", PROBE_HOST, "-verify_hostname", PROBE_HOST,
            "-verify_return_error", "-CAfile", "/etc/ssl/certs/ca-certificates.crt",
        ], input="", text=True, capture_output=True, timeout=20)
        if process.returncode == 0:
            output = run(["openssl", "x509", "-noout", "-serial"], process.stdout)
            if output.strip().split("=", 1)[1].upper() == serial:
                return
        time.sleep(5)
    raise RuntimeError("Router did not serve the candidate serial")


def apply_candidate(destination, serial):
    previous = CURRENT.resolve(strict=True)
    if previous == destination:
        verify_served(serial)
        return
    switch_symlink(destination)
    reloaded = False
    try:
        nginx_test()
        nginx_reload()
        reloaded = True
        verify_served(serial)
    except Exception:
        switch_symlink(previous)
        if reloaded:
            nginx_test()
            nginx_reload()
        raise
    for path in VERSIONS.iterdir():
        if path.is_dir() and not path.name.startswith(".") and path not in {previous, destination}:
            shutil.rmtree(path)
    print(f"verified router serial={serial}")


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    destination, serial = candidate()
    if args.check:
        print(f"validated candidate serial={serial}; nginx unchanged")
        return
    apply_candidate(destination, serial)


if __name__ == "__main__":
    main()
