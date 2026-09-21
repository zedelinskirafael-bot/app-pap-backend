# app-pap-backend

Backend de otimizacao de rotas do P.A.P. (Porta a Porta Sebrae).

Dois servicos:

- **osrm/** — Container OSRM com extract do Parana (Geofabrik). Faz pre-processamento na primeira inicializacao e expoe a API de roteamento na porta 5000.
- **api/** — FastAPI + OR-Tools com 3 endpoints:
  - `POST /otimizar-rota` — gera plano de N dias para uma O.S.
  - `POST /otimizar-dia` — re-otimiza apenas um dia (TSP)
  - `POST /trajeto` — caminho por rua entre 2 e 25 pontos, a pe ou de carro, na
    ordem em que vieram (nao reordena nada). Usado pelo corredor de visitas do
    mapa de Planejamento. Devolve `linha` em `[[lat, lng], ...]`, `distancia_m`
    e `duracao_s`.

## Dois containers OSRM (carro e pedestre)

A mesma imagem `osrm/` roda duas vezes, com `OSRM_PROFILE` e volume diferentes:

| Servico | Perfil | Volume | Porta no host (KVM8) | Quem usa |
|---|---|---|---|---|
| `osrm-pap` | `/opt/car.lua` | `osrm_data` | `127.0.0.1:5001` | `/otimizar-rota`, `/otimizar-dia` e `/trajeto?modo=carro` |
| `osrm-pap-foot` | `/opt/foot.lua` | `osrm_foot_data` | `127.0.0.1:5003` | `/trajeto?modo=pe` |

Cada um precisa do **seu** volume: os dois processam o mesmo PBF com perfis
diferentes e sobrescreveriam o pre-processamento um do outro.

⚠ O OSRM **ignora** o nome do perfil no caminho da URL (`/route/v1/foot/...`) —
quem decide e o que foi processado dentro do container. Apontar `OSRM_FOOT_URL`
para o container de carro devolveria rota de carro rotulada como "a pe", sem
erro nenhum. Para conferir, compare a duracao do mesmo trecho: a pe fica varias
vezes mais lento (medido em 21/09/26: 706 m em 8,6 min a pe contra 738 m em
1,6 min de carro).

O compose que roda isso vive na KVM8, em `/opt/ia-hub/docker-compose.yml` — nao
esta neste repo.

## Variaveis de ambiente

### osrm
- `OSRM_PBF_URL` (default `https://download.geofabrik.de/south-america/brazil/sul/parana-latest.osm.pbf`) — URL do extract OSM.
- `OSRM_PROFILE` (default `/opt/car.lua`) — perfil de roteamento.

### api
- `OSRM_BASE_URL` (default `http://osrm-pap:5000`) — URL interna do servico OSRM de carro.
- `OSRM_FOOT_URL` (default `http://osrm-pap-foot:5000`) — URL interna do OSRM de pedestre.

## Volume persistente

O servico `osrm` deve ter `/data` montado em volume persistente para nao re-processar a cada deploy.
