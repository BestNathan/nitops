import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cert_sync


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.versions = self.base / "versions"
        self.old = self.versions / "OLD"
        self.new = self.versions / "NEW"
        self.old.mkdir(parents=True)
        self.new.mkdir()
        self.current = self.base / "current"
        self.current.symlink_to(Path("versions/OLD"))
        cert_sync.BASE = self.base
        cert_sync.VERSIONS = self.versions
        cert_sync.CURRENT = self.current

    def tearDown(self):
        self.tmp.cleanup()

    @patch("cert_sync.nginx_reload")
    @patch("cert_sync.nginx_test", side_effect=RuntimeError("invalid candidate"))
    def test_invalid_candidate_restores_symlink_without_reload(self, test, reload):
        with self.assertRaises(RuntimeError):
            cert_sync.apply_candidate(self.new, "NEW")
        self.assertEqual(self.current.resolve(), self.old.resolve())
        reload.assert_not_called()

    @patch("cert_sync.verify_served", side_effect=RuntimeError("wrong serial"))
    @patch("cert_sync.nginx_reload")
    @patch("cert_sync.nginx_test")
    def test_served_mismatch_reloads_previous_version(self, test, reload, verify):
        with self.assertRaises(RuntimeError):
            cert_sync.apply_candidate(self.new, "NEW")
        self.assertEqual(self.current.resolve(), self.old.resolve())
        self.assertEqual(reload.call_count, 2)

    @patch("cert_sync.verify_served")
    @patch("cert_sync.nginx_reload")
    @patch("cert_sync.nginx_test")
    def test_success_keeps_new_version(self, test, reload, verify):
        cert_sync.apply_candidate(self.new, "NEW")
        self.assertEqual(self.current.resolve(), self.new.resolve())
        reload.assert_called_once_with()


class CertificateValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

        def openssl(*args):
            subprocess.run(["openssl", *map(str, args)], check=True,
                           capture_output=True, text=True)

        openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                "-subj", "/CN=Nitops Test Root", "-addext", "basicConstraints=critical,CA:TRUE",
                "-keyout", self.root / "root.key", "-out", self.root / "root.crt")
        openssl("req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=*.bestnathan.top",
                "-keyout", self.root / "leaf.key", "-out", self.root / "leaf.csr")
        (self.root / "leaf.ext").write_text(
            "subjectAltName=DNS:*.bestnathan.top\nbasicConstraints=critical,CA:FALSE\n")
        openssl("x509", "-req", "-in", self.root / "leaf.csr", "-CA", self.root / "root.crt",
                "-CAkey", self.root / "root.key", "-CAcreateserial", "-days", "2",
                "-extfile", self.root / "leaf.ext", "-out", self.root / "leaf.crt")
        self.chain = ((self.root / "leaf.crt").read_bytes() +
                      (self.root / "root.crt").read_bytes())
        self.key = (self.root / "leaf.key").read_bytes()
        cert_sync.CA_FILE = str(self.root / "root.crt")

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_fullchain_and_matching_key(self):
        serial = cert_sync.validate_certificate(self.chain, self.key, self.root)
        self.assertRegex(serial, r"^[0-9A-F]+$")

    def test_mismatched_key_is_rejected(self):
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt",
                        "rsa_keygen_bits:2048", "-out", str(self.root / "other.key")],
                       check=True, capture_output=True)
        other_key = (self.root / "other.key").read_bytes()
        with self.assertRaisesRegex(RuntimeError, "do not match"):
            cert_sync.validate_certificate(self.chain, other_key, self.root)


if __name__ == "__main__":
    unittest.main()
