# --- Build GoBGP from source ---------------------------------------------------
# Runs on the build machine and cross-compiles for the target (amd64, arm64, ...)
FROM --platform=$BUILDPLATFORM golang:1.26 AS gobgp-build
ARG GOBGP_VERSION=4.11.0
ARG TARGETOS=linux
ARG TARGETARCH
ENV CGO_ENABLED=0
RUN GOOS=$TARGETOS GOARCH=${TARGETARCH:-$(go env GOARCH)} go install -trimpath -ldflags="-s -w" \
        github.com/osrg/gobgp/v4/cmd/gobgp@v${GOBGP_VERSION} \
        github.com/osrg/gobgp/v4/cmd/gobgpd@v${GOBGP_VERSION} && \
    mkdir -p /out && \
    if [ -d "/go/bin/${TARGETOS}_${TARGETARCH}" ]; then cp /go/bin/${TARGETOS}_${TARGETARCH}/* /out/; \
    else cp /go/bin/gobgp /go/bin/gobgpd /out/; fi

# --- Runtime ------------------------------------------------------------------
FROM python:3.15-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=gobgp-build /out/gobgp /out/gobgpd /usr/local/bin/
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY gobgp_to_mongo.py bgp_attributes.py neighbor_poller.py /app/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY gobgpd.conf /etc/gobgp/gobgpd.conf
RUN chmod +x /usr/local/bin/entrypoint.sh
EXPOSE 179
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
