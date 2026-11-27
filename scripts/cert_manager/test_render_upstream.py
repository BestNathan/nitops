import tempfile
import unittest
from pathlib import Path

import yaml

from render_upstream import render_documents, transform


class RenderTests(unittest.TestCase):
    def test_crd_is_protected_and_images_are_pinned(self):
        crd = {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {"name": "certificates.cert-manager.io"},
            "spec": {},
        }
        transformed = transform(crd)
        annotations = transformed["metadata"]["annotations"]
        self.assertEqual(annotations["argocd.argoproj.io/sync-wave"], "-20")
        self.assertEqual(
            annotations["argocd.argoproj.io/sync-options"],
            "Prune=false,Delete=false",
        )

        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "cert-manager"},
            "spec": {"template": {"spec": {"containers": [{
                "name": "cert-manager-controller",
                "image": "quay.io/jetstack/cert-manager-controller:v1.21.3",
                "args": ["--acme-http01-solver-image=quay.io/jetstack/cert-manager-acmesolver:v1.21.3"],
                "env": [{"name": "POD_NAMESPACE"}],
            }]}}},
        }
        result = transform(deployment)
        pod = result["spec"]["template"]["spec"]
        self.assertEqual(pod["nodeSelector"]["kubernetes.io/arch"], "amd64")
        container = pod["containers"][0]
        self.assertTrue(container["image"].endswith(
            "@sha256:9f4f96a33c7774cef24466fcd85031997531a03a8d117f151126baa8d1290dcc"
        ))
        self.assertIn("--dns01-recursive-nameservers-only=true", container["args"])
        self.assertIn(
            "--acme-http01-solver-image=quay.io/jetstack/cert-manager-acmesolver@sha256:c8c1225bb2fe8ab37a330b77f0d8c2ee6a41210fcb54fc07ca606ab9a022b8b7",
            container["args"],
        )
        env = {item["name"]: item.get("value") for item in container["env"]}
        self.assertEqual(env["HTTPS_PROXY"], "http://192.168.2.98:7895")
        self.assertIn(".svc", env["NO_PROXY"])

    def test_one_resource_per_file(self):
        docs = [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "cert-manager"}},
            {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {
                "name": "cert-manager", "namespace": "cert-manager"}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            paths = render_documents(docs, Path(tmp))
            self.assertEqual(len(paths), 2)
            for path in paths:
                loaded = list(yaml.safe_load_all(path.read_text()))
                self.assertEqual(len(loaded), 1)


if __name__ == "__main__":
    unittest.main()
