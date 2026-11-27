#!/usr/bin/env python3
import argparse
import re
from pathlib import Path

import yaml

IMAGES = {
    "quay.io/jetstack/cert-manager-controller:v1.21.3":
        "quay.io/jetstack/cert-manager-controller@sha256:9f4f96a33c7774cef24466fcd85031997531a03a8d117f151126baa8d1290dcc",
    "quay.io/jetstack/cert-manager-cainjector:v1.21.3":
        "quay.io/jetstack/cert-manager-cainjector@sha256:083d0e906f8eb44e255fe4ff817ae54e49f9ed82e22f1011bb4e9e4f4b8a641f",
    "quay.io/jetstack/cert-manager-webhook:v1.21.3":
        "quay.io/jetstack/cert-manager-webhook@sha256:055ac8e9de6a0ba6763794838fe38944acc650f68ebe1b01dd8be079e580f458",
    "quay.io/jetstack/cert-manager-acmesolver:v1.21.3":
        "quay.io/jetstack/cert-manager-acmesolver@sha256:c8c1225bb2fe8ab37a330b77f0d8c2ee6a41210fcb54fc07ca606ab9a022b8b7",
}
NO_PROXY = "localhost,127.0.0.1,.svc,.cluster.local,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"


def transform(document):
    metadata = document["metadata"]
    annotations = metadata.setdefault("annotations", {})
    annotations["argocd.argoproj.io/sync-wave"] = "0"
    if document["kind"] == "CustomResourceDefinition":
        annotations["argocd.argoproj.io/sync-wave"] = "-20"
        annotations["argocd.argoproj.io/sync-options"] = "Prune=false,Delete=false"
    elif document["kind"] == "Namespace":
        annotations["argocd.argoproj.io/sync-wave"] = "-10"
        annotations["argocd.argoproj.io/sync-options"] = "Prune=false,Delete=false"
    elif document["kind"] in {
        "ValidatingWebhookConfiguration",
        "MutatingWebhookConfiguration",
    }:
        annotations["argocd.argoproj.io/sync-wave"] = "30"

    if document["kind"] != "Deployment":
        return document

    pod = document["spec"]["template"]["spec"]
    pod["nodeSelector"] = {"kubernetes.io/arch": "amd64"}
    for container in pod["containers"]:
        container["image"] = IMAGES[container["image"]]
        if metadata["name"] != "cert-manager":
            continue
        args = container.setdefault("args", [])
        solver_flag = "--acme-http01-solver-image="
        args[:] = [
            solver_flag + IMAGES[arg[len(solver_flag):]] if arg.startswith(solver_flag) else arg
            for arg in args
        ]
        args.extend([
            "--dns01-recursive-nameservers=1.1.1.1:53,8.8.8.8:53",
            "--dns01-recursive-nameservers-only=true",
        ])
        env = container.setdefault("env", [])
        env.extend([
            {"name": "HTTP_PROXY", "value": "http://192.168.2.98:7895"},
            {"name": "HTTPS_PROXY", "value": "http://192.168.2.98:7895"},
            {"name": "NO_PROXY", "value": NO_PROXY},
        ])
    return document


def output_path(document, root):
    kind = document["kind"]
    name = document["metadata"]["name"]
    if kind == "CustomResourceDefinition":
        return root / "crds" / f"{name}.yaml"
    if kind == "Namespace":
        return root / "namespace" / "namespace.yaml"
    safe_name = re.sub(r"[^a-zA-Z0-9.-]+", "-", name)
    return root / "controller" / f"{kind.lower()}-{safe_name}.yaml"


def render_documents(documents, root):
    paths = []
    for original in documents:
        document = transform(original)
        path = output_path(document, root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(
            document, sort_keys=False, explicit_start=True, width=120))
        paths.append(path)
    return paths


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("upstream", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    with args.upstream.open(encoding="utf-8") as stream:
        documents = [doc for doc in yaml.safe_load_all(stream) if doc]
    if len(documents) != 47:
        raise SystemExit(f"expected 47 upstream resources, got {len(documents)}")
    paths = render_documents(documents, args.output)
    print(f"rendered {len(paths)} one-resource files")


if __name__ == "__main__":
    main()
