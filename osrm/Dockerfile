FROM debian:bookworm-slim AS downloader
ARG OSRM_PBF_URL=https://download.geofabrik.de/south-america/brazil/sul/parana-latest.osm.pbf
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates osmium-tool && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /pbf
RUN curl -L --fail --retry 5 --retry-delay 5 -o orig.osm.pbf "$OSRM_PBF_URL" && \
    SIZE=$(stat -c%s orig.osm.pbf) && \
    echo "Baixado $SIZE bytes" && \
    if [ "$SIZE" -lt 10000000 ]; then echo "PBF muito pequeno"; exit 1; fi && \
    osmium cat orig.osm.pbf -o map.osm.pbf --overwrite && \
    rm orig.osm.pbf && \
    ls -l map.osm.pbf

FROM osrm/osrm-backend:latest

WORKDIR /data

COPY --from=downloader /pbf/map.osm.pbf /seed/map.osm.pbf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 5000

ENTRYPOINT ["/entrypoint.sh"]
