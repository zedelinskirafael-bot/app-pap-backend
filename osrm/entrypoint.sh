#!/bin/bash
set -e

PROFILE="${OSRM_PROFILE:-/opt/car.lua}"
DATA_DIR="/data"
BASENAME="map"

cd "$DATA_DIR"

if [ ! -f "${BASENAME}.osrm.mldgr" ]; then
  echo "[osrm] Pre-processamento nao encontrado. Iniciando..."

  # Sempre usa o PBF da imagem (mais recente apos cada build)
  rm -f "${BASENAME}.osm.pbf" "${BASENAME}.osrm"*
  echo "[osrm] Copiando PBF da imagem (/seed/map.osm.pbf)"
  cp /seed/map.osm.pbf "${BASENAME}.osm.pbf"

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
