# --- Build GoBGP from source (works for amd64 and arm64) --------------------
FROM golang:1.26 AS gobgp-build
ARG GOBGP_VERSION=4.11.0
ENV CGO_ENABLED=0 GOBIN=/out
RUN go install -trimpath -ldflags="-s -w" \
        github.com/osrg/gobgp/v4/cmd/gobgp@v${GOBGP_VERSION} \
        github.com/osrg/gobgp/v4/cmd/gobgpd@v${GOBGP_VERSION}

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
