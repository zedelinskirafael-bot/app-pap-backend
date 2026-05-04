FROM debian:bookworm-slim AS downloader
ARG OSRM_PBF_URL=https://download.geofabrik.de/south-america/brazil/sul/parana-latest.osm.pbf
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /pbf
RUN curl -L --fail -o map.osm.pbf "$OSRM_PBF_URL"

FROM osrm/osrm-backend:latest

WORKDIR /data

COPY --from=downloader /pbf/map.osm.pbf /seed/map.osm.pbf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 5000

ENTRYPOINT ["/entrypoint.sh"]
