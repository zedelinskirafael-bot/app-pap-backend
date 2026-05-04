FROM debian:bookworm-slim AS downloader
ARG OSRM_PBF_URL=https://download.geofabrik.de/south-america/brazil/sul-latest.osm.pbf
# Bounding box do Parana (W,S,E,N)
ARG OSRM_BBOX=-54.62,-26.72,-48.02,-22.51
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates osmium-tool && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /pbf
RUN curl -L --fail --retry 5 --retry-delay 5 -o orig.osm.pbf "$OSRM_PBF_URL" && \
    SIZE=$(stat -c%s orig.osm.pbf) && \
    echo "Baixado $SIZE bytes" && \
    if [ "$SIZE" -lt 10000000 ]; then echo "PBF muito pequeno"; head -c 500 orig.osm.pbf; exit 1; fi && \
    osmium extract --strategy simple -b "$OSRM_BBOX" orig.osm.pbf -o map.osm.pbf --overwrite && \
    rm orig.osm.pbf && \
    ls -lh map.osm.pbf

FROM osrm/osrm-backend:latest

WORKDIR /data

COPY --from=downloader /pbf/map.osm.pbf /seed/map.osm.pbf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 5000

ENTRYPOINT ["/entrypoint.sh"]
