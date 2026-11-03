#!/usr/bin/env python3
"""Build flat index tables from the manifest tree, status records, and catalog."""

import argparse
import collections
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from profile_catalog import load_candidates, load_status  # noqa: E402


def artifact_rows(ref, doc, source):
    """Return one flat row per artifact recorded in a manifest document."""
    entry = doc.get("entry", {})
    rep, plat = entry.get("reproduce", {}), entry.get("platform", {})
    base = {"ref": ref, "source": source, "digest": rep.get("digest", ""),
            "os_id": plat.get("os_id", ""), "os_version_id": plat.get("os_version_id", ""),
            "libc_flavor": plat.get("libc_flavor", ""), "libc_version": plat.get("libc_version", "")}
    rows = []
    for a in entry.get("artifacts", []):
        cap, prov = a.get("capability", {}), a.get("provenance", {})
        rows.append({**base,
                     "application": a.get("application", ""), "binary": a.get("binary", ""),
                     "arch": a.get("arch", ""), "accelerator": cap.get("accelerator", ""),
                     "gpu_libs": cap.get("gpu_libs", []), "mpi": cap.get("mpi", ""),
                     "fabric_libfabric": cap.get("fabric_libfabric", False),
                     "fabric_efa": cap.get("fabric_efa", False),
                     "fabric_verbs": cap.get("fabric_verbs", False),
                     "build_system": prov.get("build_system", ""), "compiler": prov.get("compiler", ""),
                     "compiler_version": prov.get("compiler_version", ""),
                     "variants": len(a.get("variants", [])), "confidence": a.get("confidence", "")})
    return rows


def main():
    """Write images.jsonl, artifacts.jsonl, and summary.json."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifests", default="manifests")
    p.add_argument("--status-dir", default="status")
    p.add_argument("--candidates", default="catalog/candidates.jsonl")
    p.add_argument("--out", default="db")
    args = p.parse_args()
    os.makedirs(args.out, exist_ok=True)

    candidates = load_candidates(args.candidates)
    status = load_status(args.status_dir)

    images, artifacts = [], []
    for ref, cand in sorted(candidates.items()):
        st = status.get(ref, {})
        if not st and cand.get("error"):
            st = {"status": "excluded", "reason": cand["error"]}
        images.append({"ref": ref, "source": cand.get("source", ""),
                       "candidate_digest": cand.get("digest"), "platforms": cand.get("platforms", []),
                       "status": st.get("status", "pending"), "reason": st.get("reason", ""),
                       "artifacts": st.get("artifacts"), "manifest": st.get("manifest", ""),
                       "backend": st.get("backend", ""), "model": st.get("model", ""),
                       "secretary_version": st.get("secretary_version", "")})
        if st.get("status") == "ok" and st.get("manifest") and os.path.exists(st["manifest"]):
            with open(st["manifest"]) as f:
                artifacts += artifact_rows(ref, json.load(f), cand.get("source", ""))

    known = {r["manifest"] for r in images if r["manifest"]}
    orphans = sorted(set(glob.glob(os.path.join(args.manifests, "**", "manifest.json"),
                                   recursive=True)) - known)

    for name, rows in (("images.jsonl", images), ("artifacts.jsonl", artifacts)):
        with open(os.path.join(args.out, name), "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    by_source = collections.defaultdict(collections.Counter)
    for r in images:
        by_source[r["source"]][r["status"]] += 1
    summary = {
        "images": len(images),
        "artifacts": len(artifacts),
        "status": dict(collections.Counter(r["status"] for r in images)),
        "by_source": {k: dict(v) for k, v in sorted(by_source.items())},
        "accelerator": dict(collections.Counter(a["accelerator"] for a in artifacts)),
        "mpi": dict(collections.Counter(a["mpi"] for a in artifacts)),
        "manifests_without_status": orphans,
    }
    with open(os.path.join(args.out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps({k: summary[k] for k in ("images", "artifacts", "status")}, indent=2))


if __name__ == "__main__":
    main()
