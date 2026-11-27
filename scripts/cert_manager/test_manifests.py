import hashlib
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
        production = certificates[0]
        self.assertEqual(production["metadata"]["name"], "bestnathan-wildcard")
        self.assertEqual(production["metadata"]["namespace"], "slb")
        self.assertEqual(production["spec"]["secretName"], "bestnathan-top-tls")
        self.assertEqual(production["spec"]["issuerRef"]["name"], "letsencrypt-production")
        self.assertEqual(production["spec"]["dnsNames"], ["*.bestnathan.top"])
        self.assertEqual(production["spec"]["privateKey"]["rotationPolicy"], "Always")

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

    def test_cert_manager_monitoring_is_scoped_and_mounted(self):
        root = ROOT / "components/observability/prometheus"
        config_map = yaml.safe_load((root / "configmap.yaml").read_text())
        prometheus = yaml.safe_load(config_map["data"]["prometheus.yml"])
        self.assertEqual(
            prometheus.get("rule_files"),
            ["/etc/prometheus/rules/*.yml"],
        )
        jobs = {
            job["job_name"]: job
            for job in prometheus["scrape_configs"]
        }
        self.assertEqual(
            jobs["cert-manager"]["static_configs"][0]["targets"],
            ["cert-manager.cert-manager.svc:9402"],
        )

        deployment = yaml.safe_load((root / "deployment.yaml").read_text())
        pod = deployment["spec"]["template"]["spec"]
        container = next(item for item in pod["containers"] if item["name"] == "prometheus")
        mounts = {item["name"]: item for item in container["volumeMounts"]}
        self.assertEqual(mounts["rules"]["mountPath"], "/etc/prometheus/rules")
        volumes = {item["name"]: item for item in pod["volumes"]}
        self.assertEqual(volumes["rules"]["configMap"]["name"], "prometheus-rules")

        rules_path = root / "configmap-rules.yaml"
        self.assertTrue(rules_path.exists(), "cert-manager alert rules are missing")
        rules_map = yaml.safe_load(rules_path.read_text())
        rules_text = rules_map["data"]["cert-manager.yml"]
        groups = yaml.safe_load(rules_text)["groups"]
        alerts = {
            rule["alert"]
            for group in groups
            for rule in group["rules"]
        }
        self.assertEqual(alerts, {
            "CertManagerCertificateNotReady",
            "CertManagerCertificateExpiring",
            "CertManagerCertificateMetricsAbsent",
            "CertManagerScrapeDown",
            "CertManagerSyncErrors",
        })
        self.assertNotIn("blackbox", rules_text)
        expiry = next(
            rule
            for group in groups
            for rule in group["rules"]
            if rule["alert"] == "CertManagerCertificateExpiring"
        )
        self.assertIn("> 0", expiry["expr"])

    def test_prometheus_recreate_and_config_checksum(self):
        root = ROOT / "components/observability/prometheus"
        config_map = yaml.safe_load((root / "configmap.yaml").read_text())
        rules_map = yaml.safe_load((root / "configmap-rules.yaml").read_text())
        expected = hashlib.sha256(
            (
                config_map["data"]["prometheus.yml"]
                + "\n"
                + rules_map["data"]["cert-manager.yml"]
            ).encode()
        ).hexdigest()
        deployment = yaml.safe_load((root / "deployment.yaml").read_text())
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"].get("strategy", {}).get("type"), "Recreate")
        annotations = deployment["spec"]["template"]["metadata"].get("annotations", {})
        self.assertEqual(annotations.get("checksum/prometheus-config"), expected)

    def test_slb_application_bootstraps_before_cert_manager(self):
        cert_manager = yaml.safe_load((ROOT / "apps/cert-manager.yaml").read_text())
        slb = yaml.safe_load((ROOT / "apps/slb.yaml").read_text())
        cert_wave = int(
            cert_manager["metadata"].get("annotations", {}).get(
                "argocd.argoproj.io/sync-wave", "0"
            )
        )
        slb_wave = int(
            slb["metadata"].get("annotations", {}).get(
                "argocd.argoproj.io/sync-wave", "0"
            )
        )
        self.assertLess(slb_wave, cert_wave)

    def test_slb_secret_mount_and_reloader_preserve_legacy_certificates(self):
        root = ROOT / "components/slb"
        kustomization = yaml.safe_load((root / "kustomization.yaml").read_text())
        self.assertIn("nginx-proxy/configmap-reloader.yaml", kustomization["resources"])
        daemonset = yaml.safe_load((root / "nginx-proxy/daemonset.yaml").read_text())
        pod = daemonset["spec"]["template"]["spec"]
        self.assertTrue(pod["hostNetwork"])
        self.assertTrue(pod["shareProcessNamespace"])
        containers = {item["name"]: item for item in pod["containers"]}
        self.assertEqual(set(containers), {"nginx", "cert-reloader"})
        for container in containers.values():
            self.assertEqual(
                container["image"],
                "docker.io/library/nginx@sha256:2f4e10193b0c4f61472245e9d26cea78e7df7af266c7e7cf5e67353ee2731b51",
            )
            active = next(
                item for item in container["volumeMounts"]
                if item["name"] == "certs-active"
            )
            self.assertEqual(active["mountPath"], "/etc/nginx/certs-active")

        nginx_mounts = {item["name"] for item in containers["nginx"]["volumeMounts"]}
        self.assertNotIn("bestnathan-tls", nginx_mounts)
        self.assertIn("bootstrap-nfs", "\n".join(containers["nginx"]["command"]))

        reloader = containers["cert-reloader"]
        secret_mount = next(
            item for item in reloader["volumeMounts"] if item["name"] == "bestnathan-tls"
        )
        self.assertEqual(secret_mount["mountPath"], "/etc/nginx/certs-managed/bestnathan")
        self.assertTrue(secret_mount["readOnly"])
        self.assertNotIn("subPath", secret_mount)
        script_mount = next(
            item for item in reloader["volumeMounts"] if item["name"] == "reloader-script"
        )
        self.assertNotIn("subPath", script_mount)
        self.assertEqual(script_mount["mountPath"], "/usr/local/share/cert-reloader")
        self.assertIn("/usr/local/share/cert-reloader/cert-reloader.sh", reloader["command"])

        volumes = {item["name"]: item for item in pod["volumes"]}
        secret = volumes["bestnathan-tls"]["secret"]
        self.assertEqual(secret["secretName"], "bestnathan-top-tls")
        self.assertTrue(secret["optional"])
        self.assertEqual(secret["defaultMode"], 0o400)
        self.assertEqual(volumes["certs-active"]["emptyDir"]["medium"], "Memory")
        self.assertEqual(volumes["nginx-run"], {"name": "nginx-run", "emptyDir": {}})
        self.assertEqual(
            volumes["reloader-script"]["configMap"]["name"], "nginx-reloader-script"
        )

        script = yaml.safe_load(
            (root / "nginx-proxy/configmap-reloader.yaml").read_text()
        )["data"]["cert-reloader.sh"]
        expected_checksum = hashlib.sha256(script.encode()).hexdigest()
        annotations = daemonset["spec"]["template"]["metadata"]["annotations"]
        self.assertEqual(annotations["checksum/slb-reloader"], expected_checksum)

        sites = (root / "nginx-proxy/configmap-sites.yaml").read_text()
        self.assertIn("/etc/nginx/certs-active/current/tls.crt", sites)
        self.assertIn("/etc/nginx/certs-active/current/tls.key", sites)
        self.assertIn("/etc/nginx/certs/nhome.crt", sites)
        self.assertIn("/etc/nginx/certs/nhome.key", sites)
        self.assertIn("/etc/nginx/certs/default.crt", sites)
        self.assertIn("/etc/nginx/certs/default.key", sites)
        self.assertNotIn("/etc/nginx/certs-managed/bestnathan", sites)
        self.assertNotIn("/etc/nginx/certs/bestnathan.crt", sites)
        self.assertNotIn("/etc/nginx/certs/bestnathan.key", sites)

    def test_no_competing_secret_writer_or_private_ca(self):
        sealed = docs("components/cert-manager/secrets/*.yaml")
        for item in sealed:
            self.assertNotEqual(item["metadata"]["name"], "bestnathan-top-tls")
        competing = []
        for path in ROOT.glob("components/**/*.yaml"):
            for item in docs(str(path.relative_to(ROOT))):
                if (
                    item.get("kind") in {"Secret", "SealedSecret"}
                    and item.get("metadata", {}).get("name") == "bestnathan-top-tls"
                    and item.get("metadata", {}).get("namespace") == "slb"
                ):
                    competing.append(str(path))
        self.assertEqual(competing, [])
        all_text = "\n".join(
            path.read_text()
            for path in (ROOT / "components/cert-manager").rglob("*.yaml")
        )
        self.assertNotIn("BEGIN PRIVATE KEY", all_text)
        self.assertNotIn("apiToken:", all_text)
        self.assertNotIn("nhome-root-ca", all_text)


if __name__ == "__main__":
    unittest.main()
