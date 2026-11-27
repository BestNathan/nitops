import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def docs(pattern):
    result = []
    for path in ROOT.glob(pattern):
        result.extend(doc for doc in yaml.safe_load_all(path.read_text()) if doc)
    return result


class ManifestContracts(unittest.TestCase):
    def test_single_recursive_argocd_application(self):
        path = ROOT / "apps/cert-manager.yaml"
        self.assertTrue(path.exists(), "cert-manager ArgoCD Application is missing")
        app = yaml.safe_load(path.read_text())
        self.assertEqual(app["spec"]["source"]["path"], "components/cert-manager")
        self.assertEqual(app["spec"]["source"]["targetRevision"], "main")
        self.assertTrue(app["spec"]["source"]["directory"]["recurse"])
        self.assertIn("ServerSideApply=true", app["spec"]["syncPolicy"]["syncOptions"])
        ignored = {
            item["kind"]: item
            for item in app["spec"].get("ignoreDifferences", [])
        }
        for kind in ["ValidatingWebhookConfiguration", "MutatingWebhookConfiguration"]:
            self.assertIn(kind, ignored)
            self.assertEqual(ignored[kind]["group"], "admissionregistration.k8s.io")
            self.assertEqual(ignored[kind]["name"], "cert-manager-webhook")
            self.assertIn(
                "/webhooks/*/clientConfig/caBundle",
                ignored[kind]["jsonPointers"],
            )

    def test_bootstrap_resources_precede_fail_closed_admission(self):
        webhooks = docs("components/cert-manager/controller/*webhookconfiguration*.yaml")
        self.assertEqual(len(webhooks), 2)
        for webhook in webhooks:
            self.assertEqual(
                webhook["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"],
                "30",
            )
            self.assertTrue(webhook["webhooks"])
            for rule in webhook["webhooks"]:
                self.assertEqual(rule["failurePolicy"], "Fail")
        bootstrap = (
            docs("components/cert-manager/issuers/*.yaml")
            + docs("components/cert-manager/secrets/*.yaml")
            + docs("components/cert-manager/certificates/*.yaml")
        )
        self.assertTrue(bootstrap)
        for resource in bootstrap:
            wave = int(resource["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"])
            self.assertLess(wave, 30)

    def test_issuers_and_certificate(self):
        issuers = docs("components/cert-manager/issuers/*.yaml")
        self.assertEqual(
            {d["metadata"]["name"] for d in issuers},
            {"letsencrypt-staging", "letsencrypt-production"},
        )
        emails = set()
        for issuer in issuers:
            acme = issuer["spec"]["acme"]
            email = acme["email"]
            self.assertTrue(email and "@" in email and "\n" not in email)
            emails.add(email)
            self.assertEqual(
                acme["privateKeySecretRef"]["name"],
                f"{issuer['metadata']['name']}-account-key",
            )
            self.assertNotIn("skipTLSVerify", acme)
            self.assertNotIn("dns01RecursiveNameservers", acme)
            self.assertEqual(len(acme["solvers"]), 1)
            solver = acme["solvers"][0]
            self.assertEqual(solver["selector"]["dnsZones"], ["bestnathan.top"])
            self.assertEqual(solver["dns01"]["cloudflare"]["apiTokenSecretRef"], {
                "name": "cloudflare-api-token", "key": "token"})
        self.assertEqual(len(emails), 1)

        certificates = docs("components/cert-manager/certificates/*.yaml")
        self.assertEqual(len(certificates), 1)
        staging = certificates[0]
        self.assertEqual(staging["metadata"]["name"], "bestnathan-staging-test")
        self.assertEqual(staging["spec"]["issuerRef"]["name"], "letsencrypt-staging")
        self.assertEqual(staging["spec"]["dnsNames"], ["*.bestnathan.top"])
        self.assertEqual(staging["spec"]["privateKey"]["rotationPolicy"], "Always")

    def test_cloudflare_secret_is_strict_sealed_token(self):
        sealed = docs("components/cert-manager/secrets/*.yaml")
        self.assertEqual(len(sealed), 1)
        item = sealed[0]
        self.assertEqual(item["kind"], "SealedSecret")
        self.assertEqual(item["metadata"]["name"], "cloudflare-api-token")
        self.assertEqual(item["metadata"]["namespace"], "cert-manager")
        self.assertEqual(item["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"], "10")
        self.assertEqual(set(item["spec"]["encryptedData"]), {"token"})
        self.assertNotIn("data", item)
        self.assertNotIn("data", item["spec"].get("template", {}))

    def test_no_competing_secret_writer_or_private_ca(self):
        sealed = docs("components/cert-manager/secrets/*.yaml")
        for item in sealed:
            self.assertNotEqual(item["metadata"]["name"], "bestnathan-top-tls")
        all_text = "\n".join(
            path.read_text()
            for path in (ROOT / "components/cert-manager").rglob("*.yaml")
        )
        self.assertNotIn("BEGIN PRIVATE KEY", all_text)
        self.assertNotIn("apiToken:", all_text)
        self.assertNotIn("nhome-root-ca", all_text)


if __name__ == "__main__":
    unittest.main()
