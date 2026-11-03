#!/usr/bin/env python3
"""Enumerate pre-built HPC/scientific container namespaces into a candidate list."""

import argparse
import concurrent.futures
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HUB = "https://hub.docker.com/v2"
QUAY = "https://quay.io/api/v1"
NGC_SEARCH = "https://api.ngc.nvidia.com/v2/search/catalog/resources/CONTAINER"

HUB_NAMESPACES = ["ecpe4s", "intel", "rocm", "amdih", "biocontainers"]
QUAY_NAMESPACES = ["biocontainers", "pawsey"]
NGC_ORGS = ["hpc", "nvidia-hpcvis"]
NGC_HPC_LABELS = {"High Performance Computing", "HPC / Supercomputing", "HPC", "Supercomputing"}

# Characters allowed in Docker Hub repository names.
HUB_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789._-"
HUB_ANON_WINDOW = 100


class RateLimiter:
    """Space requests to at most `per_minute` per minute across threads."""

    def __init__(self, per_minute):
        self.interval = 60.0 / per_minute
        self.lock = threading.Lock()
        self.next_time = 0.0

    def wait(self):
        """Block until the next request slot."""
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_time - now)
            self.next_time = max(now, self.next_time) + self.interval
        if delay:
            time.sleep(delay)


hub_limiter = RateLimiter(170)
quay_limiter = RateLimiter(600)


def http_json(url, headers=None, limiter=None, retries=6):
    """GET a URL and parse JSON, retrying on 429 and 5xx."""
    for attempt in range(retries):
        if limiter:
            limiter.wait()
        req = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read(), strict=False)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                reset = e.headers.get("x-ratelimit-reset")
                retry_after = e.headers.get("Retry-After")
                if reset:
                    sleep = max(1.0, float(reset) - time.time() + 1)
                elif retry_after:
                    sleep = float(retry_after)
                else:
                    sleep = 2 ** attempt
                print(f"  HTTP {e.code}, sleeping {min(sleep, 120):.0f}s: {url[:100]}",
                      file=sys.stderr, flush=True)
                time.sleep(min(sleep, 120))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2 ** attempt)
    raise RuntimeError(f"giving up on {url}")


# ---------------------------------------------------------------- Docker Hub


HUB_ORDERINGS = ["name", "-name", "last_updated", "-last_updated", "pull_count", "-pull_count"]
hub_calls = [0]


def hub_query(ns, name, ordering):
    """Return (count, results) for one filtered, ordered repository page."""
    params = {"page_size": HUB_ANON_WINDOW, "ordering": ordering}
    if name:
        params["name"] = name
    hub_calls[0] += 1
    if hub_calls[0] % 100 == 0:
        print(f"    hub listing calls: {hub_calls[0]}", flush=True)
    d = http_json(f"{HUB}/namespaces/{ns}/repositories?{urllib.parse.urlencode(params)}", limiter=hub_limiter)
    return d["count"], d["results"]


def hub_cover(ns, s, found):
    """Fetch repos matching substring `s` under each ordering; return True if all were seen."""
    seen = set()
    for ordering in HUB_ORDERINGS:
        count, results = hub_query(ns, s, ordering)
        for r in results:
            found[r["name"]] = r
            seen.add(r["name"])
        if len(seen) >= count:
            return True
    return False


def hub_partition(ns, s, found, visited, expected):
    """Collect every repo whose name contains substring `s`."""
    if s in visited or len(found) >= expected:
        return
    visited.add(s)
    if hub_cover(ns, s, found):
        return
    for c in HUB_ALPHABET:
        hub_partition(ns, s + c, found, visited, expected)
        hub_partition(ns, c + s, found, visited, expected)


def hub_list_namespace(ns):
    """List all repos in a Docker Hub namespace without authentication."""
    hub_calls[0] = 0
    found, visited = {}, {""}
    expected, _ = hub_query(ns, "", "name")
    if not hub_cover(ns, "", found):
        for c in HUB_ALPHABET:
            hub_partition(ns, c, found, visited, expected)
    return list(found.values()), expected, hub_calls[0]


def hub_latest_tag(ns, repo, prefer="linux/amd64"):
    """Return (tag_count, newest tag, newest tag with `prefer` platform) for a Docker Hub repo."""
    d = http_json(
        f"{HUB}/repositories/{ns}/{repo}/tags/?page_size=25&ordering=last_updated",
        limiter=hub_limiter,
    )
    tags = d.get("results") or []
    newest = tags[0] if tags else None
    preferred = next((t for t in tags if prefer in hub_platforms(t)), newest)
    return d.get("count", 0), newest, preferred


def hub_platforms(tag):
    """Return sorted os/arch[/variant] strings for a Docker Hub tag record."""
    return sorted(
        {f"{i.get('os')}/{i.get('architecture')}" + (f"/{i['variant']}" if i.get("variant") else "")
         for i in tag.get("images") or [] if i.get("architecture") not in (None, "unknown")}
    )


def hub_candidate(ns, repo):
    """Build a candidate row for a Docker Hub repo."""
    count, newest, tag = hub_latest_tag(ns, repo["name"])
    row = {
        "source": f"dockerhub/{ns}",
        "registry": "docker.io",
        "repository": f"{ns}/{repo['name']}",
        "description": repo.get("description") or "",
        "pull_count": repo.get("pull_count"),
        "repo_last_updated": repo.get("last_updated"),
        "tag_count": count,
        "tag": None,
        "ref": None,
        "digest": None,
        "platforms": [],
        "tag_last_updated": None,
        "newest_tag": newest["name"] if newest else None,
    }
    if tag:
        images = tag.get("images") or []
        row.update(
            tag=tag["name"],
            ref=f"docker.io/{ns}/{repo['name']}:{tag['name']}",
            digest=tag.get("digest") or (images[0].get("digest") if len(images) == 1 else None),
            platforms=hub_platforms(tag),
            tag_last_updated=tag.get("last_updated"),
        )
    return row


# ---------------------------------------------------------------------- Quay


def quay_list_namespace(ns):
    """List all public repos in a Quay namespace."""
    repos, url = [], f"{QUAY}/repository?namespace={ns}&public=true&last_modified=true&popularity=true"
    while url:
        d = http_json(url, limiter=quay_limiter)
        repos += d["repositories"]
        nxt = d.get("next_page")
        url = (f"{QUAY}/repository?namespace={ns}&public=true&last_modified=true"
               f"&popularity=true&next_page={nxt}") if nxt else None
    return repos


def quay_candidate(ns, repo):
    """Build a candidate row for a Quay repo using its newest active tag."""
    d = http_json(
        f"{QUAY}/repository/{ns}/{repo['name']}/tag/?limit=10&onlyActiveTags=true",
        limiter=quay_limiter,
    )
    tags = d.get("tags") or []
    tag = max(tags, key=lambda t: t.get("start_ts") or 0) if tags else None
    row = {
        "source": f"quay/{ns}",
        "registry": "quay.io",
        "repository": f"{ns}/{repo['name']}",
        "description": (repo.get("description") or "")[:500],
        "pull_count": None,
        "popularity": repo.get("popularity"),
        "repo_last_updated": repo.get("last_modified"),
        "tag_count": None,
        "tag": None,
        "ref": None,
        "digest": None,
        "is_manifest_list": None,
        "platforms": [],
        "tag_last_updated": None,
    }
    if tag:
        row.update(
            tag=tag["name"],
            ref=f"quay.io/{ns}/{repo['name']}:{tag['name']}",
            digest=tag.get("manifest_digest"),
            is_manifest_list=tag.get("is_manifest_list"),
            tag_last_updated=tag.get("last_modified"),
        )
    return row


# ----------------------------------------------------------------------- NGC


def ngc_list_all():
    """Return every container resource in the public NGC catalog."""
    out, page, pages = {}, 0, 1
    while page < pages:
        q = urllib.parse.quote(json.dumps({"query": "*", "page": page, "pageSize": 1000}))
        d = http_json(f"{NGC_SEARCH}?q={q}")
        pages = d.get("resultPageTotal", 0)
        for group in d.get("results", []):
            for r in group.get("resources", []):
                out[r["resourceId"]] = r
        page += 1
    return list(out.values())


def ngc_general_labels(r):
    """Return the values of an NGC resource's 'general' labels."""
    return {v for lab in r.get("labels", []) if lab["key"] == "general" for v in lab["values"]}


def nvcr_digest(repo, tag):
    """Return (digest, platforms) for an nvcr.io image using an anonymous token."""
    tok = http_json(f"https://nvcr.io/proxy_auth?scope=repository:{repo}:pull")["token"]
    accept = ", ".join([
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ])
    req = urllib.request.Request(
        f"https://nvcr.io/v2/{repo}/manifests/{tag}",
        headers={"Authorization": f"Bearer {tok}", "Accept": accept},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        digest = resp.headers.get("Docker-Content-Digest")
        body = json.loads(resp.read())
    platforms = sorted(
        {f"{m['platform']['os']}/{m['platform']['architecture']}" for m in body.get("manifests", [])
         if m.get("platform", {}).get("architecture") not in (None, "unknown")}
    )
    return digest, platforms


def ngc_candidate(r, source):
    """Build a candidate row for an NGC container resource."""
    attrs = {a["key"]: a["value"] for a in r.get("attributes", [])}
    tag = attrs.get("latestTag")
    row = {
        "source": source,
        "registry": "nvcr.io",
        "repository": r["resourceId"],
        "description": (r.get("description") or "")[:500],
        "pull_count": None,
        "repo_last_updated": r.get("dateModified"),
        "tag_count": None,
        "tag": tag,
        "ref": f"nvcr.io/{r['resourceId']}:{tag}" if tag else None,
        "digest": None,
        "platforms": [],
        "tag_last_updated": attrs.get("latestTagPushedDate"),
        "labels": sorted(ngc_general_labels(r)),
        "error": None,
    }
    if tag:
        try:
            row["digest"], row["platforms"] = nvcr_digest(r["resourceId"], tag)
        except urllib.error.HTTPError as e:
            row["error"] = f"manifest: HTTP {e.code}"
    return row


# ---------------------------------------------------------------------- main


def run_pool(fn, items, workers):
    """Map fn over items in a thread pool, keeping failures as rows."""
    rows = []
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(fn, *it): it for it in items}
        for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            try:
                rows.append(fut.result())
            except Exception as e:
                ns, repo = futures[fut][0], futures[fut][1]
                rows.append({"source": str(ns), "repository": str(repo.get("name", repo)), "error": str(e)})
            if i % 500 == 0:
                print(f"    {i}/{len(items)}", flush=True)
    return rows


def load_jsonl(path):
    """Read a JSONL file into a list."""
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path, rows):
    """Write rows to a JSONL file atomically."""
    with open(path + ".tmp", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    os.replace(path + ".tmp", path)


def checkpointed(workdir, name, build):
    """Return cached (rows, meta) for `name`, or build and cache them."""
    rows_path = os.path.join(workdir, f"{name.replace('/', '__')}.jsonl")
    meta_path = rows_path + ".meta.json"
    if os.path.exists(rows_path) and os.path.exists(meta_path):
        print(f"{name}: cached", flush=True)
        return load_jsonl(rows_path), json.load(open(meta_path))
    t = time.time()
    rows, meta = build()
    meta["seconds"] = round(time.time() - t)
    meta["with_ref"] = sum(1 for r in rows if r.get("ref"))
    meta["errors"] = sum(1 for r in rows if r.get("error"))
    write_jsonl(rows_path, rows)
    json.dump(meta, open(meta_path, "w"), indent=2)
    print(f"{name}: {json.dumps(meta)}", flush=True)
    return rows, meta


def build_hub(ns):
    """Enumerate one Docker Hub namespace into candidate rows."""
    repos, expected, ncalls = hub_list_namespace(ns)
    print(f"dockerhub/{ns}: listed {len(repos)}/{expected} via {ncalls} calls", flush=True)
    rows = run_pool(hub_candidate, [(ns, r) for r in repos], 4)
    return rows, {"expected": expected, "enumerated": len(repos), "listing_calls": ncalls}


def build_quay(ns):
    """Enumerate one Quay namespace into candidate rows."""
    repos = quay_list_namespace(ns)
    print(f"quay/{ns}: listed {len(repos)}", flush=True)
    rows = run_pool(quay_candidate, [(ns, r) for r in repos], 8)
    return rows, {"enumerated": len(repos)}


def build_ngc():
    """Enumerate selected NGC containers into candidate rows."""
    ngc = ngc_list_all()
    picked = [(r, f"ngc/{r['orgName']}") for r in ngc if r["orgName"] in NGC_ORGS]
    picked += [(r, "ngc/nvidia-hpc-label") for r in ngc
               if r["orgName"] == "nvidia" and ngc_general_labels(r) & NGC_HPC_LABELS]
    rows = [ngc_candidate(r, src) for r, src in picked]
    by_source = {}
    for r in rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    return rows, {"catalog_total": len(ngc), "enumerated": len(rows), "by_source": by_source,
                  "with_digest": sum(1 for r in rows if r.get("digest"))}


def main():
    """Enumerate all configured namespaces and write candidate files."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workdir", default="checkpoints")
    p.add_argument("--out", default="candidates.jsonl")
    p.add_argument("--refs", default="repos.txt")
    p.add_argument("--summary", default="summary.json")
    p.add_argument("--only", nargs="*", help="limit to these source names")
    args = p.parse_args()
    os.makedirs(args.workdir, exist_ok=True)

    jobs = [(f"dockerhub/{ns}", lambda ns=ns: build_hub(ns)) for ns in HUB_NAMESPACES]
    jobs += [(f"quay/{ns}", lambda ns=ns: build_quay(ns)) for ns in QUAY_NAMESPACES]
    jobs += [("ngc", build_ngc)]
    if args.only:
        jobs = [j for j in jobs if j[0] in args.only]

    rows, summary = [], {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sources": {}}
    for name, build in jobs:
        got, meta = checkpointed(args.workdir, name, build)
        rows += got
        summary["sources"][name] = meta

    write_jsonl(args.out, rows)
    ok = [r for r in rows if r.get("ref") and not r.get("error")]
    refs = sorted({r["ref"] for r in ok})
    with open(args.refs, "w") as f:
        f.write("\n".join(refs) + "\n")
    by_source_dir = os.path.join(os.path.dirname(os.path.abspath(args.refs)), "refs-by-source")
    os.makedirs(by_source_dir, exist_ok=True)
    for src in sorted({r["source"] for r in ok}):
        with open(os.path.join(by_source_dir, src.replace("/", "__") + ".txt"), "w") as f:
            f.write("\n".join(sorted({r["ref"] for r in ok if r["source"] == src})) + "\n")
    unpullable = sorted({r["ref"] for r in rows if r.get("ref") and r.get("error")})
    with open(os.path.join(by_source_dir, "_anonymous_pull_denied.txt"), "w") as f:
        f.write("\n".join(unpullable) + "\n")
    summary["total_rows"] = len(rows)
    summary["total_refs"] = len(refs)
    summary["rows_without_tags"] = sum(1 for r in rows if not r.get("ref"))
    summary["anonymous_pull_denied"] = len(unpullable)
    summary["errors"] = sum(1 for r in rows if r.get("error"))
    summary["unique_digests"] = len({r["digest"] for r in rows if r.get("digest")})
    with open(args.summary, "w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
