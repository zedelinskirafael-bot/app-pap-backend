# app-pap-backend

Backend de otimizacao de rotas do P.A.P. (Porta a Porta Sebrae).

Dois servicos:

- **osrm/** — Container OSRM com extract do Parana (Geofabrik). Faz pre-processamento na primeira inicializacao e expoe a API de roteamento na porta 5000.
- **api/** — FastAPI + OR-Tools com 2 endpoints:
  - `POST /otimizar-rota` — gera plano de N dias para uma O.S.
  - `POST /otimizar-dia` — re-otimiza apenas um dia (TSP)

## Variaveis de ambiente

### osrm
- `OSRM_PBF_URL` (default `https://download.geofabrik.de/south-america/brazil/sul/parana-latest.osm.pbf`) — URL do extract OSM.
- `OSRM_PROFILE` (default `/opt/car.lua`) — perfil de roteamento.

### api
- `OSRM_BASE_URL` (default `http://osrm-pap:5000`) — URL interna do servico OSRM.

## Volume persistente

O servico `osrm` deve ter `/data` montado em volume persistente para nao re-processar a cada deploy.
