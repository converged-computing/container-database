# Environment for profiling the catalog with artifact-secretary.
# The host Docker daemon is used through a mounted socket (see environment/run.sh).
FROM docker:27-cli AS dockercli

FROM python:3.12-slim-bookworm
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY environment/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock && \
    artifact-secretary --help > /dev/null && \
    docker --version
ENV NO_COLOR=1
WORKDIR /db
