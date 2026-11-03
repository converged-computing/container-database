# Container Manifest Database

This is a database of build and runtime manifests for pre-built HPC and scientific containers. We have approximately 16.7k image references, collected from public registry namespaces, and we profile them with the [artifact-secretary](https://github.com/converged-computing/artifact-secretary) to generate manifests. Each `manifest.json` describes the application binaries it contains, what they link against (GPU runtime, MPI, fabric), their provenance, and the image's OS and libc. The goal of this database and manifest set is oriented toward scheduling experiments.

## Catalog sources

The following sources were used as a starting set.

| Source | Refs | Notes |
|---|---|---|
| `quay/biocontainers` | 14,743 | Single-tool bioconda builds |
| `dockerhub/biocontainers` | 1,064 | |
| `dockerhub/intel` | 461 | Mixed HPC, AI and media images |
| `dockerhub/ecpe4s` | 235 | E4S |
| `dockerhub/rocm` | 96 | |
| `ngc/nvidia-hpc-label` | 49 | NVIDIA repos labelled HPC on NGC; includes non-HPC tooling |
| `dockerhub/amdih` | 36 | AMD Infinity Hub |
| `quay/pawsey` | 30 | |
| `ngc/hpc` | 22 | |

Each repository contributes one tag. On Docker Hub this is the newest tag that includes `linux/amd64` (the newest tag overall is kept as `newest_tag` in `candidates.jsonl`). On Quay it is the newest active tag, and on NGC it is the catalog's `latestTag`. The `digest` field records what the tag pointed to at discovery time.

## Environment

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r environment/requirements.lock
artifact-secretary --help
```

## Model backend

### Amazon Bedrock (`--backend aws`)

The `aws` backend is behalf's Strands runner, which calls Bedrock through boto3. Credentials come from the standard AWS chain (environment variables, profile, or role). A Bedrock API key also works: export it as `AWS_BEARER_TOKEN_BEDROCK` and botocore uses it for Bedrock calls.

Check access and pick a model:

```bash
aws sts get-caller-identity
aws bedrock list-inference-profiles --region us-east-1 \
  --query 'inferenceProfileSummaries[].inferenceProfileId' --output text
export SECRETARY_MODEL=us.anthropic.claude-opus-5   # or another inference profile id from that list
```

### Anthropic API (`--backend claude`)

```bash
export ANTHROPIC_API_KEY=...
export SECRETARY_MODEL=<model name>   # optional; omit to use the Claude Code default
```

## Pilot run

Start with a small source and a few images, then look at the results before scaling up:

```bash
python3 scripts/profile_catalog.py --backend aws --source dockerhub/amdih --limit 3
```

Use `--backend claude` for the Anthropic API. `--model` defaults to `$SECRETARY_MODEL`.

## Full extraction

End to end, on one amd64 host with Docker and Bedrock access:

```bash
# 1. environment and credentials (see Environment and Model backend above)
. .venv/bin/activate
export AWS_PROFILE=...            # or AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, AWS_BEARER_TOKEN_BEDROCK, or an instance role
export SECRETARY_MODEL=us.anthropic.claude-opus-5
docker login                      # Docker Hub, to avoid anonymous pull limits

# 2. preview, then run every source
python3 scripts/profile_catalog.py --backend aws --dry-run
python3 scripts/profile_catalog.py --backend aws

# 3. rerun anything that failed
python3 scripts/profile_catalog.py --backend aws --retry-failed

# 4. optional: the 17 NGC images that refuse anonymous pulls (after docker login nvcr.io)
python3 scripts/profile_catalog.py --backend aws --refs catalog/refs-by-source/_anonymous_pull_denied.txt

# 5. build the index
python3 scripts/build_index.py
```

The run is resumable (see below), so it can be stopped and restarted at any point. Commit `status/`, `manifests/` and `db/` when done; `logs/` is ignored.

Seventy-one E4S refs ship only arm64 or ppc64le images and are recorded as `skipped-arch` on an amd64 host. To profile the arm64 ones, repeat step 2 on an arm64 host with the same repository checkout; the arch check uses the host, so only the arm-only refs run. Refs already profiled on amd64 are skipped on resume, so multi-platform tags are profiled for amd64 only.

Preview what will run without running it:

```bash
python3 scripts/profile_catalog.py --backend aws --dry-run
```

A suggested order is to run the smaller HPC sources first, then the biocontainers:

```bash
for s in dockerhub/ecpe4s dockerhub/rocm dockerhub/amdih quay/pawsey ngc/hpc dockerhub/intel ngc/nvidia-hpc-label; do
  python3 scripts/profile_catalog.py --backend aws --source "$s"
done
python3 scripts/profile_catalog.py --backend aws --source dockerhub/biocontainers
python3 scripts/profile_catalog.py --backend aws --source quay/biocontainers
```

**Resuming.** Re-running the same command skips every image that already has a final status (`ok`, `skipped`, `skipped-arch`). Add `--retry-failed` to rerun `failed` and `timeout` images.

**Sharding across hosts.** Give each host its own slice with `--shard i/n`. The split uses a stable hash of the ref, and each shard appends to its own `status/profile.<i>-of-<n>.jsonl`, so results from all hosts can be committed together without conflicts:

```bash
python3 scripts/profile_catalog.py --backend aws --shard 0/4   # host 1
python3 scripts/profile_catalog.py --backend aws --shard 1/4   # host 2
```

**Options.**

| Flag | Meaning |
|---|---|
| `--refs FILE` | Reference list (default `catalog/repos.txt`), e.g. a file from `catalog/refs-by-source/` |
| `--source NAME` | Only refs from this catalog source (repeatable) |
| `--limit N` | Stop after N runs |
| `--timeout SECONDS` | Limit on profiling one image, not counting the pull (default none) |
| `--pull-timeout SECONDS` | Limit on one `docker pull` (default none) |
| `--stall SECONDS` | After this long with no output, print artifact-secretary's process tree and container state (default 300). A lingering `docker exec` means a tool call is stuck inside the container; none means it is waiting on the model |
| `--prefetch N` | Pull the next N images while the current one is profiled (default 1; 0 keeps only one image on disk) |
| `--keep-images` | Keep pulled images |
| `--allow-network` | Run the inspected container on a network (default is sealed) |
| `--any-arch` | Attempt images whose catalog platforms exclude the host |
| `--quiet` | Print one line per image instead of the live output (the log is written either way) |

**Status values.**

| Status | Meaning |
|---|---|
| `ok` | A manifest was written; `artifacts` is the number of binaries recorded |
| `skipped` | The pull failed, or artifact-secretary skipped the image (no usable Python, run failed); `reason` has the message |
| `skipped-arch` | The catalog lists platforms for the image and none match the host; not attempted |
| `failed` | The run exited without a manifest or a skip message; see the log |
| `timeout` | Profiling was killed after `--timeout`, when one is set; its container (and the image, if this run pulled it) was removed |

Each record also carries `pull_seconds`, so slow registries and slow models can be told apart.

### How the runner works

The runner calls `artifact-secretary profile --catalog <ref>` once per image rather than passing the whole list. artifact-secretary writes manifests only after it finishes its whole catalog, so one long batch could lose every result if it crashed, and separate runs also make a per-image timeout possible.

The runner pulls each image itself before calling artifact-secretary, which leaves images that already exist alone. This keeps the pull outside the profiling timeout (a killed pull discards the download, so a retry would start from zero) and allows the next image to be fetched while the current one is profiled. The runner removes images it pulled once the image is done, unless `--keep-images` is set.

Skipped images produce no manifest, so the status files are the record of what has been attempted. artifact-secretary's own architecture check only reads `ghcr.io` references. The runner therefore checks architecture itself using the `platforms` field from the catalog. Quay refs have no platform information, so they are always attempted.

## Running on an EC2 instance

The work is network and disk bound: pulling and unpacking images dominates, the ELF inspection is light, and the model cost dwarfs the instance cost. A good fit is **m6i.xlarge** (4 vCPU, 16 GB, up to 12.5 Gbps) in `us-east-1`, the Bedrock region behalf uses, with a **500 GB gp3** root volume. Two E4S images at once (the current one plus the prefetched next) fit comfortably, and the extra vCPUs unpack layers in parallel. Spot pricing works too: the runner resumes from the status files, so an interruption costs one image. For the arm64 pass use the same recipe with `m7g.xlarge` and the arm64 AMI.

**1. IAM role for Bedrock** (once, from your workstation):

```bash
cat > /tmp/trust.json <<'EOF2'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF2
cat > /tmp/bedrock.json <<'EOF2'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream","bedrock:ListInferenceProfiles","bedrock:ListFoundationModels"],"Resource":"*"}]}
EOF2
aws iam create-role --role-name manifest-db-bedrock --assume-role-policy-document file:///tmp/trust.json
aws iam put-role-policy --role-name manifest-db-bedrock --policy-name bedrock --policy-document file:///tmp/bedrock.json
aws iam create-instance-profile --instance-profile-name manifest-db-bedrock
aws iam add-role-to-instance-profile --instance-profile-name manifest-db-bedrock --role-name manifest-db-bedrock
```

**2. Launch** (needs an existing key pair and a security group that allows SSH from you):

```bash
export AWS_DEFAULT_REGION=us-east-1
AMI=$(aws ssm get-parameter --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id --query Parameter.Value --output text)
aws ec2 run-instances --image-id "$AMI" --instance-type m6i.xlarge \
  --key-name <your-key> --security-group-ids <sg-id> \
  --iam-instance-profile Name=manifest-db-bedrock \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":500,"VolumeType":"gp3","Throughput":500,"Iops":6000,"DeleteOnTermination":true}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=manifest-db}]' \
  --query 'Instances[0].InstanceId' --output text
```

**3. On the instance:**

```bash
sudo apt-get update && sudo apt-get install -y docker.io git python3-venv tmux
sudo usermod -aG docker "$USER" && newgrp docker

git clone <repo-url> container-database && cd container-database
python3 -m venv .venv && . .venv/bin/activate
pip install -r environment/requirements.lock

docker login            # Docker Hub, to avoid the anonymous pull limit
export SECRETARY_MODEL=<inference profile id>
python3 -c "import boto3; c=boto3.client('bedrock', region_name='us-east-1'); print([p['inferenceProfileId'] for p in c.list_inference_profiles()['inferenceProfileSummaries'] if 'anthropic' in p['inferenceProfileId']])"
```

The instance role supplies the credentials, so no keys are needed. Then run the pilot from the Pilot run section, and the full extraction inside `tmux` so it survives your SSH session:

```bash
tmux new -s profile
. .venv/bin/activate && export SECRETARY_MODEL=<inference profile id>
for s in dockerhub/amdih dockerhub/rocm quay/pawsey ngc/hpc dockerhub/ecpe4s dockerhub/intel ngc/nvidia-hpc-label dockerhub/biocontainers quay/biocontainers; do
  python3 scripts/profile_catalog.py --backend aws --source "$s"
done
```

Detach with `Ctrl-b d`, reattach with `tmux attach -t profile`. Commit `status/` and `manifests/` periodically from a second shell; they are only appended to.

## Build the index

```bash
python3 scripts/build_index.py
```

This writes three files:

- `db/images.jsonl`: one row per catalog ref with its source, status, reason, artifact count, manifest path, backend and model. Refs not yet run are `pending`; refs that refused anonymous pulls at discovery time are `excluded`.
- `db/artifacts.jsonl`: one row per recorded binary with application, binary path, arch, accelerator, GPU libs, MPI, fabric flags, build system, compiler, number of variants and confidence, plus the image's digest, OS and libc.
- `db/summary.json`: counts by status, source, accelerator and MPI, and any manifests that have no matching status record.

## Refresh or extend the catalog

```bash
cd catalog
rm -rf checkpoints
python3 discover_candidates.py
```

This needs no credentials and takes about 40 minutes, most of it Quay tag lookups and Docker Hub's 180 requests per minute. Without a login, Docker Hub only returns the first 100 repos of a namespace listing, so the script works around this by filtering on name substrings with several sort orders. It checks completeness against the count Docker Hub reports, recorded as `expected` and `enumerated` in `summary.json`.

Progress is saved per source in `checkpoints/`. Delete one source's checkpoint files to refresh only that source, or pass `--only dockerhub/rocm`. To add a namespace, edit `HUB_NAMESPACES`, `QUAY_NAMESPACES` or `NGC_ORGS` at the top of the script.

## Notes

- **Docker Hub pulls.** Anonymous pulls are rate-limited, so log in to Docker Hub before profiling the Docker Hub sources.
- **NGC.** Seventeen NGC images refused anonymous access at discovery time. After `docker login nvcr.io`, profile them with `--refs catalog/refs-by-source/_anonymous_pull_denied.txt`.
- **Mutable tags.** Some refs use `latest` or other tags that can move. Compare `candidate_digest` in the status and index with the digest artifact-secretary resolved, which is stored in the manifest.

## License

HPCIC DevTools is distributed under the terms of the MIT license.
All new contributions must be made under this license.

See [LICENSE](LICENSE), [COPYRIGHT](COPYRIGHT), and [NOTICE](NOTICE) for details.

SPDX-License-Identifier: (MIT)
