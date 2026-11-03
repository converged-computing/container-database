#!/usr/bin/env python3
"""Profile catalog image references with artifact-secretary, one image per run, with resume."""

import argparse
import glob
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from importlib.metadata import PackageNotFoundError, version

from secretary.catalog import host_arch
from secretary.model.manifest import parse_reference

TERMINAL = {"ok", "skipped", "skipped-arch"}
SKIP_LINE = re.compile(r"skipped:\s*(.+)")


def load_candidates(path):
    """Map image reference to its candidate row."""
    rows = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                r = json.loads(line)
                if r.get("ref"):
                    rows[r["ref"]] = r
    return rows


def load_status(status_dir):
    """Map image reference to its most recent status record across all status files."""
    latest = {}
    for path in sorted(glob.glob(os.path.join(status_dir, "*.jsonl"))):
        with open(path) as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    prev = latest.get(r["ref"])
                    if prev is None or r["finished"] >= prev["finished"]:
                        latest[r["ref"]] = r
    return latest


def manifest_path(out_dir, ref):
    """Return the manifest.json path artifact-secretary writes for a reference."""
    return os.path.join(out_dir, *parse_reference(ref), "manifest.json")


def arch_supported(platforms, host_platform):
    """True if platforms are unknown or include the host platform (any variant)."""
    if not platforms:
        return True
    return any(p == host_platform or p.startswith(host_platform + "/") for p in platforms)


def in_shard(ref, shard):
    """True if ref belongs to shard (index, count) by a stable hash."""
    index, count = shard
    return int(hashlib.sha1(ref.encode()).hexdigest(), 16) % count == index


def log_name(ref):
    """Return a filesystem-safe log file name for a reference."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", ref) + ".log"


def docker_quiet(args):
    """Run a docker command, returning stdout or None if docker is unavailable or fails."""
    try:
        r = subprocess.run(["docker", *args], capture_output=True, text=True)
    except FileNotFoundError:
        return None
    return r.stdout if r.returncode == 0 else None


def cleanup_after_kill(ref, image_existed, keep_images):
    """Remove containers (and an image we pulled) left behind by a killed run."""
    ids = (docker_quiet(["ps", "-q", "--filter", f"ancestor={ref}"]) or "").split()
    if ids:
        docker_quiet(["rm", "-f", *ids])
    if not image_existed and not keep_images:
        docker_quiet(["rmi", ref])


def stamp():
    """Wall-clock prefix for streamed lines, so pull and model time can be told apart."""
    return time.strftime("%H:%M:%S ")


def stream(proc, log, echo, last=None):
    """Copy a process's output to the log and, if echo, the terminal, line by line.
    `last`, if given, is a one-element list updated with the time of the latest line."""
    for line in proc.stdout:
        line = stamp() + line
        log.write(line)
        log.flush()
        if echo:
            sys.stdout.write(line)
            sys.stdout.flush()
        if last is not None:
            last[0] = time.time()


def stall_report(ref, proc, quiet_for, log):
    """Describe what artifact-secretary is doing while it produces no output: its
    process tree (a lingering `docker exec` means a tool call is stuck in the
    container; none means it is waiting on the model) and the container state."""
    tree = subprocess.run(["ps", "-s", str(proc.pid), "-o", "pid,etime,args"],
                          capture_output=True, text=True).stdout.strip()
    containers = docker_quiet(["ps", "--filter", f"ancestor={ref}",
                               "--format", "{{.ID}} {{.Status}}"]) or ""
    lines = [f"no output for {int(quiet_for // 60)}m{int(quiet_for % 60):02d}s; processes:"]
    lines += ["    " + l for l in tree.splitlines()]
    lines.append("  containers: " + (containers.strip() or "none"))
    text = "".join(stamp() + l + "\n" for l in lines)
    log.write(text)
    log.flush()
    sys.stdout.write(text)
    sys.stdout.flush()


class Pull(threading.Thread):
    """`docker pull` for one reference in a background thread.

    artifact-secretary leaves images that already exist alone, so pulling here
    keeps the pull outside the profiling timeout and lets the next image download
    while the current one is profiled. Output goes to the image's log, and to the
    terminal when `echo` is set.
    """

    def __init__(self, ref, log_path, echo):
        super().__init__(daemon=True)
        self.ref, self.log_path, self.echo = ref, log_path, echo
        self.existed = docker_quiet(["image", "inspect", ref]) is not None
        self.ok, self.seconds, self.proc, self.timed_out = self.existed, 0.0, None, False

    def run(self):
        started = time.time()
        with open(self.log_path, "w") as log:
            if self.existed:
                log.write(stamp() + f"image {self.ref} already present\n")
                return
            log.write(stamp() + f"$ docker pull {self.ref}\n")
            log.flush()
            self.proc = subprocess.Popen(["docker", "pull", self.ref], stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, errors="replace",
                                         bufsize=1, start_new_session=True)
            stream(self.proc, log, self.echo)
            self.ok = self.proc.wait() == 0
            self.seconds = round(time.time() - started, 1)
            log.write(stamp() + f"pull {'done' if self.ok else 'failed'} in {self.seconds}s\n")

    def wait(self, timeout):
        """Block until the pull ends; kill it after `timeout` seconds (0 = no limit)."""
        self.join(timeout or None)
        if self.is_alive():
            if self.proc:
                os.killpg(self.proc.pid, signal.SIGKILL)
            self.join()
            self.ok, self.timed_out = False, True


def run_one(ref, args, log_path):
    """Run artifact-secretary on one reference; return (exit_code, timed_out)."""
    cmd = ["artifact-secretary", "profile", "--backend", args.backend,
           "--catalog", ref, "--out-dir", args.out_dir]
    if args.model:
        cmd += ["--model", args.model]
    for flag in ("keep_images", "allow_network", "any_arch"):
        if getattr(args, flag):
            cmd.append("--" + flag.replace("_", "-"))
    env = dict(os.environ, NO_COLOR="1", PYTHONUNBUFFERED="1")
    with open(log_path, "a") as log:
        log.write(stamp() + "$ " + " ".join(cmd) + "\n")
        log.flush()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                start_new_session=True, text=True, errors="replace", bufsize=1)
        last = [time.time()]
        reader = threading.Thread(target=stream, args=(proc, log, not args.quiet, last), daemon=True)
        reader.start()
        started, reported = time.time(), 0
        timed_out = False
        while True:
            try:
                code = proc.wait(timeout=30)
                break
            except subprocess.TimeoutExpired:
                pass
            if args.timeout and time.time() - started > args.timeout:
                os.killpg(proc.pid, signal.SIGKILL)
                code, timed_out = proc.wait(), True
                break
            quiet_for = time.time() - last[0]
            if args.stall and quiet_for > args.stall * (reported + 1):
                stall_report(ref, proc, quiet_for, log)
                reported += 1
        reader.join(timeout=10)
        return code, timed_out


def classify(ref, args, log_path, started, exit_code, timed_out):
    """Return (status, reason, artifact_count) for a finished run."""
    if timed_out:
        return "timeout", f"exceeded {args.timeout}s", None
    path = manifest_path(args.out_dir, ref)
    if os.path.exists(path) and os.path.getmtime(path) >= started:
        with open(path) as f:
            doc = json.load(f)
        return "ok", "", len(doc.get("entry", {}).get("artifacts", []))
    with open(log_path, errors="replace") as f:
        for line in f:
            m = SKIP_LINE.search(line)
            if m:
                return "skipped", m.group(1).strip(), None
    return "failed", f"exit code {exit_code}, no manifest written", None


def parse_shard(text):
    """Parse 'i/n' into (i, n)."""
    i, n = (int(x) for x in text.split("/"))
    if not 0 <= i < n:
        raise argparse.ArgumentTypeError("shard must be i/n with 0 <= i < n")
    return i, n


def main():
    """Select references, run the ones not yet done, and append status records."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--backend", required=True, choices=["claude", "aws", "gemini"])
    p.add_argument("--model", default=os.environ.get("SECRETARY_MODEL"))
    p.add_argument("--refs", default="catalog/repos.txt")
    p.add_argument("--candidates", default="catalog/candidates.jsonl")
    p.add_argument("--source", action="append", help="only refs from this candidate source")
    p.add_argument("--limit", type=int, default=0, help="stop after this many runs")
    p.add_argument("--shard", type=parse_shard, help="i/n: run only this slice of the refs")
    p.add_argument("--out-dir", default="manifests")
    p.add_argument("--status-dir", default="status")
    p.add_argument("--log-dir", default="logs")
    p.add_argument("--timeout", type=int, default=0, metavar="SECONDS",
                   help="limit on profiling one image, not counting the pull (default 0 = none)")
    p.add_argument("--retry-failed", action="store_true", help="rerun failed and timed-out refs")
    p.add_argument("--dry-run", action="store_true", help="print the plan without running")
    p.add_argument("--keep-images", action="store_true")
    p.add_argument("--allow-network", action="store_true")
    p.add_argument("--any-arch", action="store_true")
    p.add_argument("--quiet", action="store_true",
                   help="only print one line per image; full output still goes to logs/")
    p.add_argument("--prefetch", type=int, default=1, metavar="N",
                   help="pull the next N images while the current one is profiled (default 1; "
                        "0 disables, which also bounds disk use to one image)")
    p.add_argument("--stall", type=float, default=300, metavar="SECONDS",
                   help="after this long without output, report what artifact-secretary is "
                        "doing, and again at each further multiple (default 300; 0 disables)")
    p.add_argument("--pull-timeout", type=float, default=0, metavar="SECONDS",
                   help="limit on each docker pull (default 0 = none); --timeout covers only "
                        "the profiling step")
    args = p.parse_args()

    for d in (args.out_dir, args.status_dir, args.log_dir):
        os.makedirs(d, exist_ok=True)
    try:
        secretary_version = version("artifact-secretary")
    except PackageNotFoundError:
        secretary_version = "unknown"

    candidates = load_candidates(args.candidates)
    done = load_status(args.status_dir)
    host_platform = f"linux/{host_arch()}"
    shard_tag = f"{args.shard[0]}-of-{args.shard[1]}" if args.shard else "all"
    status_file = os.path.join(args.status_dir, f"profile.{shard_tag}.jsonl")

    with open(args.refs) as f:
        refs = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    plan, counts = [], {"done": 0, "other_shard": 0, "other_source": 0}
    for ref in refs:
        row = candidates.get(ref, {})
        if args.source and row.get("source") not in args.source:
            counts["other_source"] += 1
            continue
        if args.shard and not in_shard(ref, args.shard):
            counts["other_shard"] += 1
            continue
        prev = done.get(ref)
        if prev and (prev["status"] in TERMINAL or not args.retry_failed):
            counts["done"] += 1
            continue
        plan.append(ref)
    if args.limit:
        plan = plan[: args.limit]

    print(f"host {host_platform}; {len(refs)} refs; {len(plan)} to run; "
          f"{counts['done']} already recorded; status -> {status_file}", flush=True)
    if args.dry_run:
        for ref in plan:
            print(ref)
        return

    def attempted(ref):
        row = candidates.get(ref, {})
        return args.any_arch or arch_supported(row.get("platforms"), host_platform)

    runnable = [ref for ref in plan if attempted(ref)]
    pulls = {}

    def pull_for(ref, echo):
        if ref not in pulls:
            pulls[ref] = Pull(ref, os.path.join(args.log_dir, log_name(ref)), echo)
            pulls[ref].start()
        return pulls[ref]

    for i, ref in enumerate(plan, 1):
        row = candidates.get(ref, {})
        record = {"ref": ref, "source": row.get("source", ""), "candidate_digest": row.get("digest"),
                  "backend": args.backend, "model": args.model or "",
                  "secretary_version": secretary_version, "host_platform": host_platform}
        started = time.time()
        pull_seconds = None
        if not attempted(ref):
            status, reason, n, exit_code, log_rel = (
                "skipped-arch", f"image provides {', '.join(row['platforms'])}", None, None, "")
        else:
            log_path = os.path.join(args.log_dir, log_name(ref))
            pull = pull_for(ref, echo=not args.quiet)
            pos = runnable.index(ref)
            for nxt in runnable[pos + 1: pos + 1 + max(args.prefetch, 0)]:
                pull_for(nxt, echo=False)
            if pull.is_alive() and not pull.echo:
                print(f"[{i}/{len(plan)}] waiting      {ref} (prefetched pull in progress, "
                      f"log: {log_path})", flush=True)
            pull.wait(args.pull_timeout)
            pull_seconds = pull.seconds
            if not pull.ok:
                reason = (f"pull exceeded {args.pull_timeout}s" if pull.timed_out
                          else "docker pull failed (see log)")
                status, n, exit_code, log_rel = "skipped", None, None, log_path
                if not pull.existed and not args.keep_images:
                    docker_quiet(["rmi", ref])
            else:
                print(f"[{i}/{len(plan)}] profiling    {ref} (pulled in {pull.seconds}s, "
                      f"log: {log_path})", flush=True)
                profiling_started = time.time()
                exit_code, timed_out = run_one(ref, args, log_path)
                if timed_out:
                    cleanup_after_kill(ref, pull.existed, args.keep_images)
                elif not pull.existed and not args.keep_images:
                    docker_quiet(["rmi", ref])
                status, reason, n = classify(ref, args, log_path, profiling_started,
                                             exit_code, timed_out)
                log_rel = log_path
        record.update(status=status, reason=reason, artifacts=n, exit_code=exit_code, log=log_rel,
                      manifest=manifest_path(args.out_dir, ref) if status == "ok" else "",
                      started=round(started, 3), finished=round(time.time(), 3),
                      seconds=round(time.time() - started, 1), pull_seconds=pull_seconds)
        with open(status_file, "a") as f:
            f.write(json.dumps(record) + "\n")
        print(f"[{i}/{len(plan)}] {status:<12} {ref} {reason}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
