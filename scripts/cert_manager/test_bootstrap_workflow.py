import base64
import copy
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/seal-cert-manager-cloudflare.yml"
PUBLIC_CERT = ROOT / "components/sealed-secrets/public-cert.pem"
CHECKOUT_SHA = "11bd71901bbe5b1630ceea73d27597364c9af683"
UPLOAD_SHA = "ea165f8d65b6e75b540449e92b4886f43607fa02"
KUBESEAL_SHA256 = "01069a5e79ed7702ba10bed42cd8132d94746ca288add06836d082b4202c2789"
APPROVED_SECRET_STEPS = {
    "Seal Cloudflare credential",
    "Write sanitized DNS inventory and validate artifacts",
}
DUMMY_TOKEN = "dummy-token-for-offline-seal-test-only"


def contains_cloudflare_secret_reference(value):
    if isinstance(value, dict):
        return any(
            contains_cloudflare_secret_reference(key)
            or contains_cloudflare_secret_reference(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(contains_cloudflare_secret_reference(item) for item in value)
    return (
        isinstance(value, str)
        and "CLOUDFLARE_API_TOKEN" in value
        and re.search(r"\bsecrets\b", value) is not None
    )


def secret_referencing_steps(document):
    return {
        step["name"]
        for step in document["jobs"]["seal"]["steps"]
        if contains_cloudflare_secret_reference(step)
    }


def inline_python_blocks(run):
    marker = "<<'PY'"
    lines = run.splitlines()
    blocks = []
    index = 0
    while index < len(lines):
        if marker not in lines[index]:
            index += 1
            continue
        index += 1
        while lines[index - 1].rstrip().endswith("\\"):
            index += 1
        body = []
        while index < len(lines) and lines[index] != "PY":
            body.append(lines[index])
            index += 1
        if index == len(lines):
            raise AssertionError("unterminated Python heredoc")
        blocks.append(textwrap.dedent("\n".join(body)))
        index += 1
    return blocks


def load_workflow():
    if not WORKFLOW.exists():
        raise AssertionError("PR-0 sealing workflow is missing")
    return yaml.safe_load(WORKFLOW.read_text())


class BootstrapWorkflowTests(unittest.TestCase):
    def execute_seal_step(self, workspace, executable, real_kubeseal=None):
        workspace = Path(workspace)
        runner_temp = workspace / "runner"
        binary_dir = runner_temp / "bin"
        binary_dir.mkdir(parents=True)
        binary = binary_dir / "kubeseal"
        binary.write_text(executable)
        binary.chmod(0o700)
        certificate = workspace / "components/sealed-secrets/public-cert.pem"
        certificate.parent.mkdir(parents=True)
        shutil.copy(PUBLIC_CERT, certificate)
        document = load_workflow()
        seal = next(
            step["run"]
            for step in document["jobs"]["seal"]["steps"]
            if step["name"] == "Seal Cloudflare credential"
        )
        environment = os.environ.copy()
        environment.update({
            "CLOUDFLARE_API_TOKEN": DUMMY_TOKEN,
            "RUNNER_TEMP": str(runner_temp),
            "REAL_KUBESEAL": str(real_kubeseal or ""),
            "ARGV_FILE": str(workspace / "kubeseal-argv.txt"),
        })
        result = subprocess.run(
            ["bash", "-c", seal],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        artifact = workspace / "dist/sealedsecret-cloudflare-api-token.yaml"
        return result, artifact, environment["ARGV_FILE"]

    def test_manual_main_only_protected_execution(self):
        document = load_workflow()
        self.assertEqual(set(document["on"]), {"workflow_dispatch"})
        self.assertEqual(document["permissions"], {"contents": "read"})
        job = document["jobs"]["seal"]
        self.assertEqual(job["if"], "github.ref == 'refs/heads/main'")
        self.assertEqual(job["environment"], "cert-manager-bootstrap")
        self.assertEqual(job["timeout-minutes"], 5)
        checkout = next(step for step in job["steps"] if "uses" in step and step["uses"].startswith("actions/checkout@"))
        self.assertEqual(checkout["uses"], f"actions/checkout@{CHECKOUT_SHA}")
        self.assertFalse(checkout["with"]["persist-credentials"])
        upload = next(step for step in job["steps"] if "uses" in step and step["uses"].startswith("actions/upload-artifact@"))
        self.assertEqual(upload["uses"], f"actions/upload-artifact@{UPLOAD_SHA}")
        self.assertEqual(upload["with"]["retention-days"], 1)

    def test_secret_is_scoped_to_only_seal_and_inventory_steps(self):
        document = load_workflow()
        self.assertNotIn("env", document)
        job = document["jobs"]["seal"]
        self.assertNotIn("env", job)
        secret_steps = secret_referencing_steps(document)
        self.assertEqual(secret_steps, APPROVED_SECRET_STEPS)
        for step in job["steps"]:
            run = step.get("run", "")
            self.assertNotIn("set -x", run)
            self.assertNotIn("printenv", run)
            if "uses" in step:
                self.assertNotIn("CLOUDFLARE_API_TOKEN", step.get("env", {}))

    def test_secret_scanner_detects_alternate_injection_spellings(self):
        mutations = {
            "aliased env": {
                "env": {"CF_TOKEN": "${{ secrets.CLOUDFLARE_API_TOKEN }}"}
            },
            "inline run": {
                "run": "curl -H 'Authorization: Bearer ${{ secrets.CLOUDFLARE_API_TOKEN }}'"
            },
            "bracket expression": {
                "env": {"CF_TOKEN": "${{ secrets['CLOUDFLARE_API_TOKEN'] }}"}
            },
        }
        for label, mutation in mutations.items():
            with self.subTest(injection=label):
                document = copy.deepcopy(load_workflow())
                install = next(
                    step
                    for step in document["jobs"]["seal"]["steps"]
                    if step["name"] == "Install verified kubeseal"
                )
                install.update(mutation)
                self.assertIn(
                    "Install verified kubeseal",
                    secret_referencing_steps(document),
                )

    def test_seal_step_rejects_wrong_secret_identity(self):
        fake_kubeseal = """#!/bin/sh
cat >/dev/null
cat <<'YAML'
---
apiVersion: bitnami.com/v1alpha1
kind: SealedSecret
metadata:
  name: wrong-secret
  namespace: cert-manager
spec:
  encryptedData:
    token: ciphertext
  template:
    metadata:
      name: wrong-secret
      namespace: cert-manager
    type: Opaque
YAML
"""
        with tempfile.TemporaryDirectory() as workspace:
            result, _, _ = self.execute_seal_step(workspace, fake_kubeseal)
        self.assertNotEqual(
            result.returncode,
            0,
            "Seal step accepted a SealedSecret with the wrong Secret name",
        )

    def test_real_kubeseal_generates_expected_strict_encrypted_artifact(self):
        real_kubeseal = os.environ.get("KUBESEAL_BIN")
        if not real_kubeseal:
            self.skipTest("set KUBESEAL_BIN to a verified kubeseal v0.27.1 binary")
        version = subprocess.run(
            [real_kubeseal, "--version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(version, "kubeseal version: 0.27.1")
        wrapper = """#!/bin/sh
printf '%s\\n' "$@" > "$ARGV_FILE"
exec "$REAL_KUBESEAL" "$@"
"""
        with tempfile.TemporaryDirectory() as workspace:
            result, artifact, argv_file = self.execute_seal_step(
                workspace,
                wrapper,
                real_kubeseal=real_kubeseal,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            raw = artifact.read_text()
            document = yaml.safe_load(raw)
            argv = Path(argv_file).read_text().splitlines()
        self.assertEqual(argv, [
            "--cert",
            "components/sealed-secrets/public-cert.pem",
            "--format",
            "yaml",
            "--scope",
            "strict",
        ])
        self.assertEqual(document["kind"], "SealedSecret")
        self.assertEqual(document["metadata"]["name"], "cloudflare-api-token")
        self.assertEqual(document["metadata"]["namespace"], "cert-manager")
        self.assertEqual(set(document["spec"]["encryptedData"]), {"token"})
        self.assertNotIn("data", document)
        self.assertNotIn(DUMMY_TOKEN, raw)
        encoded_token = base64.b64encode(DUMMY_TOKEN.encode()).decode()
        self.assertNotIn(encoded_token, raw)

    def test_offline_tools_artifacts_and_sanitization(self):
        document = load_workflow()
        job = document["jobs"]["seal"]
        steps = {step["name"]: step for step in job["steps"]}
        install = steps["Install verified kubeseal"]["run"]
        self.assertIn(KUBESEAL_SHA256, install)
        self.assertIn("kubeseal-0.27.1-linux-amd64.tar.gz", install)
        self.assertIn("$RUNNER_TEMP/downloads", install)
        self.assertIn("$RUNNER_TEMP/bin", install)

        seal = steps["Seal Cloudflare credential"]["run"]
        self.assertIn("--cert components/sealed-secrets/public-cert.pem", seal)
        self.assertIn("--scope strict", seal)
        self.assertIn("base64.b64encode", seal)
        self.assertIn("dist/sealedsecret-cloudflare-api-token.yaml", seal)

        inventory = steps["Write sanitized DNS inventory and validate artifacts"]["run"]
        self.assertIn("/user/tokens/verify", inventory)
        self.assertIn("result_info", inventory)
        self.assertIn('"name": record["name"], "type": record["type"]', inventory)
        self.assertNotIn('record["content"]', inventory)
        self.assertIn("base64.b64encode(token.encode()).decode()", inventory)

        upload = steps["Upload encrypted bootstrap artifact"]["with"]
        self.assertEqual(upload["path"], (
            "dist/sealedsecret-cloudflare-api-token.yaml\n"
            "dist/cloudflare-records.json"
        ))
        self.assertEqual(upload["if-no-files-found"], "error")

        for step in job["steps"]:
            if "uses" in step:
                self.assertRegex(step["uses"].split("@", 1)[1], re.compile(r"^[0-9a-f]{40}$"))

    def test_inline_python_heredocs_are_well_formed(self):
        document = load_workflow()
        steps = {
            step["name"]: step
            for step in document["jobs"]["seal"]["steps"]
        }
        expected_counts = {
            "Seal Cloudflare credential": 2,
            "Write sanitized DNS inventory and validate artifacts": 1,
        }
        for name, expected_count in expected_counts.items():
            with self.subTest(step=name):
                blocks = inline_python_blocks(steps[name]["run"])
                self.assertEqual(len(blocks), expected_count)
                for index, block in enumerate(blocks):
                    compile(block, f"workflow:{name}:{index}", "exec")

    def test_public_sealing_certificate_matches_live_key(self):
        self.assertTrue(PUBLIC_CERT.exists(), "public sealing certificate is missing")
        pem = PUBLIC_CERT.read_bytes()
        self.assertIn(b"BEGIN CERTIFICATE", pem)
        self.assertNotIn(b"PRIVATE KEY", pem)
        fingerprint = subprocess.run(
            ["openssl", "x509", "-in", str(PUBLIC_CERT), "-noout", "-fingerprint", "-sha256"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip().split("=", 1)[1]
        self.assertEqual(
            fingerprint,
            "25:01:16:0D:24:A7:A6:21:A8:C1:CA:16:C5:B5:DD:6A:0D:F3:AF:6B:14:C2:6A:1F:0D:D4:F5:5D:F8:65:B3:1A",
        )


if __name__ == "__main__":
    unittest.main()
