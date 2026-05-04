#!/bin/bash
set -e

REGION_URL="${OSRM_PBF_URL:-https://download.geofabrik.de/south-america/brazil/sul/parana-latest.osm.pbf}"
PROFILE="${OSRM_PROFILE:-/opt/car.lua}"
DATA_DIR="/data"
BASENAME="map"

cd "$DATA_DIR"

if [ ! -f "${BASENAME}.osrm.mldgr" ]; then
  echo "[osrm] Pre-processamento nao encontrado. Iniciando..."

  if [ ! -f "${BASENAME}.osm.pbf" ]; then
    echo "[osrm] Baixando PBF de $REGION_URL"
    curl -L --fail -o "${BASENAME}.osm.pbf" "$REGION_URL"
  fi

  echo "[osrm] osrm-extract..."
  osrm-extract -p "$PROFILE" "${BASENAME}.osm.pbf"

  echo "[osrm] osrm-partition..."
  osrm-partition "${BASENAME}.osrm"

  echo "[osrm] osrm-customize..."
  osrm-customize "${BASENAME}.osrm"

  rm -f "${BASENAME}.osm.pbf"
  echo "[osrm] Pre-processamento concluido."
else
  echo "[osrm] Dados ja processados, pulando build."
fi

echo "[osrm] Iniciando osrm-routed na porta 5000..."
exec osrm-routed --algorithm mld --port 5000 "${DATA_DIR}/${BASENAME}.osrm"
