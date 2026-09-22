import os
from datetime import date, timedelta
from math import asin, cos, radians, sin, sqrt
from typing import List, Literal, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from pydantic import BaseModel, Field

OSRM_BASE_URL = os.getenv("OSRM_BASE_URL", "http://osrm-pap:5000")
# Segundo OSRM, perfil pedestre: o corredor de visitas anda pela calcada, onde
# mao de rua e conversao proibida nao valem. Container osrm-pap-foot, porta 5000
# dentro da rede docker (no host da KVM8 responde em 127.0.0.1:5003).
# ATENCAO: o OSRM IGNORA o nome do perfil no caminho da URL -- quem decide e o
# que foi processado no container. Apontar isto pro OSRM de carro devolveria
# rota de carro rotulada como "a pe", sem erro nenhum.
OSRM_FOOT_URL = os.getenv("OSRM_FOOT_URL", "http://osrm-pap-foot:5000")
# Teto por trecho no /trajeto. O endpoint e publico (sem login, CORS *): sem
# isto, dois cliques em pontas opostas do mapa poriam o OSRM pra varrer a base
# inteira a cada requisicao. Um corredor de visitas nao passa de alguns km.
TRAJETO_MAX_KM = 30
# Quanto o OSRM pode "grudar" um clique na rua mais proxima antes de virar erro.
# Ele gruda sempre e nao reclama: medido em 21/09/26, dois cliques no mar
# voltaram como rota de 0 m grudada num ponto a 40 km dali, com code "Ok" -- e a
# tela desenharia o corredor no lugar errado sem ninguem perceber. 500 m aceita
# clique no meio do quarteirao e recusa clique fora do mapa.
TRAJETO_SNAP_MAX_M = 500
DIAS_SEMANA = ["segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo"]

app = FastAPI(title="P.A.P. Rotas API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class Ponto(BaseModel):
    lat: float
    lng: float


class Cliente(BaseModel):
    id: str
    lat: float
    lng: float


class OtimizarRotaInput(BaseModel):
    origem: Ponto
    clientes: List[Cliente]
    total_visitas: int = Field(gt=0)
    dias: int = Field(gt=0, le=14)
    incluir_sabado: bool = False
    data_inicio: date


class OtimizarDiaInput(BaseModel):
    origem: Ponto
    clientes: List[Cliente]


class ClienteOrdenado(BaseModel):
    id: str
    ordem: int


class DiaPlanejado(BaseModel):
    dia_semana: str
    data: date
    clientes: List[ClienteOrdenado]


class OtimizarRotaOutput(BaseModel):
    dias: List[DiaPlanejado]


class OtimizarDiaOutput(BaseModel):
    clientes: List[ClienteOrdenado]


class PontoTrajeto(BaseModel):
    """Ponto do /trajeto, com a faixa conferida.

    Modelo proprio de proposito: o `Ponto` la de cima e usado pelo /otimizar-rota
    e pelo /otimizar-dia ha meses e fica como esta. Aqui o endpoint e novo e
    publico, entao nasce mais rigoroso -- sem o ge/le, um lat "NaN" atravessaria
    o teto dos 30 km (NaN > 30 e falso) e so estouraria la na frente, como 502
    sem explicacao nenhuma pro consultor.

    Limite conhecido, medido em 21/09/26: JSON com o literal `NaN` e barrado
    aqui, mas a resposta de erro sai como 500 vazio em vez de 422 -- o detalhe
    do pydantic carrega o valor recusado, e NaN nao cabe em JSON. Fica assim de
    proposito: o navegador nao produz esse corpo (`JSON.stringify(NaN)` vira
    `null`, que devolve 422 normal), e consertar exigiria trocar o tratador de
    erro de TODOS os endpoints. O dado ruim ja e recusado -- o que falta e so a
    mensagem bonita.
    """

    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class TrajetoInput(BaseModel):
    # 25 pontos ja e um corredor enorme; o limite e contra payload abusivo
    pontos: List[PontoTrajeto] = Field(min_length=2, max_length=25)
    modo: Literal["pe", "carro"] = "pe"


class TrajetoOutput(BaseModel):
    linha: List[List[float]]  # [[lat, lng], ...] — ordem que o Google Maps le
    distancia_m: float
    duracao_s: float
    # Nomes das vias percorridas, na ordem. E o que deixa o corredor pegar so
    # quem mora NA RUA do tracado: no centro a rua de tras fica a 30 m, entao
    # distancia sozinha nunca separa uma da outra.
    ruas: List[str] = []


@app.get("/health")
async def health():
    return {"status": "ok", "osrm_base_url": OSRM_BASE_URL}


async def osrm_table(coords: List[tuple], sources: Optional[List[int]] = None,
                     destinations: Optional[List[int]] = None) -> dict:
    """Chama OSRM /table e retorna durations (segundos) e distances (metros)."""
    coord_str = ";".join(f"{lng},{lat}" for lat, lng in coords)
    url = f"{OSRM_BASE_URL}/table/v1/driving/{coord_str}"
    params = {"annotations": "duration,distance"}
    if sources is not None:
        params["sources"] = ";".join(str(i) for i in sources)
    if destinations is not None:
        params["destinations"] = ";".join(str(i) for i in destinations)

    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.get(url, params=params)
        if r.status_code != 200:
            raise HTTPException(502, f"OSRM table falhou: {r.status_code} {r.text[:200]}")
        data = r.json()
        if data.get("code") != "Ok":
            raise HTTPException(502, f"OSRM retornou erro: {data.get('code')} {data.get('message')}")
        return data


def agrupar_por_coord(clientes: List[Cliente]) -> tuple:
    """Agrupa clientes com mesma lat/lng (arredondado a 6 casas) em super-nos.
    Retorna: (representantes, mapa[rep_id] -> [ids_do_grupo, na ordem original]).
    Assim clientes no mesmo endereco entram na matriz OSRM uma unica vez e
    saem na sequencia como consecutivos."""
    grupos: dict = {}
    ordem_chaves: List[tuple] = []
    for c in clientes:
        chave = (round(c.lat, 6), round(c.lng, 6))
        if chave not in grupos:
            grupos[chave] = []
            ordem_chaves.append(chave)
        grupos[chave].append(c)

    representantes: List[Cliente] = []
    mapa: dict = {}
    for chave in ordem_chaves:
        membros = grupos[chave]
        rep = membros[0]
        representantes.append(rep)
        mapa[rep.id] = [m.id for m in membros]
    return representantes, mapa


def datas_validas(inicio: date, qtd: int, incluir_sabado: bool) -> List[date]:
    """Gera N datas pulando domingo (sempre) e sabado (opcional)."""
    resultado: List[date] = []
    cursor = inicio
    while len(resultado) < qtd:
        wd = cursor.weekday()  # 0=seg ... 6=dom
        if wd == 6:
            cursor += timedelta(days=1)
            continue
        if wd == 5 and not incluir_sabado:
            cursor += timedelta(days=1)
            continue
        resultado.append(cursor)
        cursor += timedelta(days=1)
    return resultado


def resolver_tsp(matriz: List[List[int]], inicio: int = 0) -> List[int]:
    """TSP simples retornando ordem de visita (sem retornar ao deposito no final)."""
    n = len(matriz)
    if n <= 1:
        return list(range(n))

    manager = pywrapcp.RoutingIndexManager(n, 1, inicio)
    routing = pywrapcp.RoutingModel(manager)

    def cb(from_idx, to_idx):
        f = manager.IndexToNode(from_idx)
        t = manager.IndexToNode(to_idx)
        return matriz[f][t]

    transit = routing.RegisterTransitCallback(cb)
    routing.SetArcCostEvaluatorOfAllVehicles(transit)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.seconds = 30

    sol = routing.SolveWithParameters(params)
    if sol is None:
        return list(range(n))

    rota = []
    idx = routing.Start(0)
    while not routing.IsEnd(idx):
        rota.append(manager.IndexToNode(idx))
        idx = sol.Value(routing.NextVar(idx))
    return rota


def resolver_vrp_multi(matriz: List[List[int]], num_veiculos: int,
                       capacidade: int, inicio: int = 0) -> List[List[int]]:
    """VRP com K veiculos saindo do mesmo deposito. Retorna rotas (sem o deposito)."""
    n = len(matriz)
    if num_veiculos <= 1:
        rota = resolver_tsp(matriz, inicio)
        return [rota[1:]]  # remove deposito

    manager = pywrapcp.RoutingIndexManager(n, num_veiculos, inicio)
    routing = pywrapcp.RoutingModel(manager)

    def cb(from_idx, to_idx):
        f = manager.IndexToNode(from_idx)
        t = manager.IndexToNode(to_idx)
        return matriz[f][t]

    transit = routing.RegisterTransitCallback(cb)
    routing.SetArcCostEvaluatorOfAllVehicles(transit)

    # Demanda 1 por cliente, 0 no deposito
    demandas = [0] + [1] * (n - 1)

    def demanda_cb(from_idx):
        return demandas[manager.IndexToNode(from_idx)]

    demanda = routing.RegisterUnaryTransitCallback(demanda_cb)
    routing.AddDimensionWithVehicleCapacity(
        demanda, 0, [capacidade] * num_veiculos, True, "Capacidade"
    )

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.seconds = 30

    sol = routing.SolveWithParameters(params)
    if sol is None:
        raise HTTPException(500, "OR-Tools nao encontrou solucao")

    rotas: List[List[int]] = []
    for v in range(num_veiculos):
        idx = routing.Start(v)
        rota = []
        while not routing.IsEnd(idx):
            no = manager.IndexToNode(idx)
            if no != inicio:
                rota.append(no)
            idx = sol.Value(routing.NextVar(idx))
        rotas.append(rota)
    return rotas


@app.post("/otimizar-rota", response_model=OtimizarRotaOutput)
async def otimizar_rota(payload: OtimizarRotaInput):
    if payload.total_visitas > len(payload.clientes):
        raise HTTPException(400, "total_visitas maior que numero de clientes disponiveis")

    # 1) Pega tempos da origem para todos os clientes
    coords = [(payload.origem.lat, payload.origem.lng)] + [(c.lat, c.lng) for c in payload.clientes]
    tabela_origem = await osrm_table(coords, sources=[0], destinations=list(range(1, len(coords))))
    duracoes_origem = tabela_origem["durations"][0]

    # 2) Seleciona N clientes mais proximos da origem
    clientes_indexados = list(enumerate(payload.clientes))
    clientes_indexados.sort(key=lambda c: duracoes_origem[c[0]] if duracoes_origem[c[0]] is not None else 999999)
    selecionados = [c for _, c in clientes_indexados[:payload.total_visitas]]

    # 3) Constroi matriz NxN incluindo a origem
    coords_sel = [(payload.origem.lat, payload.origem.lng)] + [(c.lat, c.lng) for c in selecionados]
    tabela = await osrm_table(coords_sel)
    duracoes = tabela["durations"]
    matriz = [[int(round(d)) if d is not None else 999999 for d in linha] for linha in duracoes]

    # 4) VRP: K veiculos = K dias
    capacidade_por_dia = (payload.total_visitas + payload.dias - 1) // payload.dias
    rotas = resolver_vrp_multi(matriz, num_veiculos=payload.dias, capacidade=capacidade_por_dia)

    # 5) Monta saida com datas validas
    datas = datas_validas(payload.data_inicio, payload.dias, payload.incluir_sabado)
    dias_out: List[DiaPlanejado] = []
    for i, rota in enumerate(rotas):
        clientes_dia = [
            ClienteOrdenado(id=selecionados[idx_no_matriz - 1].id, ordem=ordem + 1)
            for ordem, idx_no_matriz in enumerate(rota)
        ]
        dias_out.append(DiaPlanejado(
            dia_semana=DIAS_SEMANA[datas[i].weekday()],
            data=datas[i],
            clientes=clientes_dia,
        ))

    return OtimizarRotaOutput(dias=dias_out)


@app.post("/otimizar-dia", response_model=OtimizarDiaOutput)
async def otimizar_dia(payload: OtimizarDiaInput):
    if not payload.clientes:
        return OtimizarDiaOutput(clientes=[])

    if len(payload.clientes) > 80:
        raise HTTPException(
            400,
            f"Muitos clientes ({len(payload.clientes)}). Limite: 80 por dia."
        )

    # Agrupa por coord: clientes no mesmo endereco entram na matriz uma vez so
    # e saem consecutivos no resultado.
    representantes, grupos = agrupar_por_coord(payload.clientes)

    coords = [(payload.origem.lat, payload.origem.lng)] + [(c.lat, c.lng) for c in representantes]
    tabela = await osrm_table(coords)
    duracoes = tabela["durations"]
    matriz = [[int(round(d)) if d is not None else 999999 for d in linha] for linha in duracoes]

    rota = resolver_tsp(matriz, inicio=0)
    sequencia = [idx for idx in rota if idx != 0]

    saida: List[ClienteOrdenado] = []
    ordem = 1
    for idx in sequencia:
        rep = representantes[idx - 1]
        for cid in grupos[rep.id]:
            saida.append(ClienteOrdenado(id=cid, ordem=ordem))
            ordem += 1
    return OtimizarDiaOutput(clientes=saida)


def linha_reta_km(a: PontoTrajeto, b: PontoTrajeto) -> float:
    """Distancia em km entre dois pontos, sobre a esfera (haversine)."""
    raio = 6371.0
    dlat = radians(b.lat - a.lat)
    dlng = radians(b.lng - a.lng)
    s = sin(dlat / 2) ** 2 + cos(radians(a.lat)) * cos(radians(b.lat)) * sin(dlng / 2) ** 2
    # min(1.0, ...) segura erro de arredondamento em pontos antipodais
    return 2 * raio * asin(min(1.0, sqrt(s)))


@app.post("/trajeto", response_model=TrajetoOutput)
async def trajeto(payload: TrajetoInput):
    """Caminho POR RUA entre os pontos clicados no mapa, a pe ou de carro.

    Nao otimiza nada e nao reordena: passa pelos pontos na ordem em que vieram.
    Quem decide o caminho aqui e o consultor -- o /otimizar-dia e que resolve
    ordem de visita. Serve pro corredor de visitas desenhar a linha na tela.
    """
    for i in range(len(payload.pontos) - 1):
        km = linha_reta_km(payload.pontos[i], payload.pontos[i + 1])
        if km > TRAJETO_MAX_KM:
            raise HTTPException(
                400,
                f"Trecho longo demais: {km:.0f} km entre o ponto {i + 1} e o {i + 2}. "
                f"O limite é {TRAJETO_MAX_KM} km por trecho.",
            )

    de_pe = payload.modo == "pe"
    base = OSRM_FOOT_URL if de_pe else OSRM_BASE_URL
    perfil = "foot" if de_pe else "driving"
    coord_str = ";".join(f"{p.lng},{p.lat}" for p in payload.pontos)
    params = {
        "overview": "full",
        "geometries": "geojson",
        # sem isto o OSRM se recusa a inverter o sentido num ponto do meio e da
        # a volta no quarteirao quando o consultor clica dos dois lados da rua
        "continue_straight": "false",
        # steps traz o nome de cada via percorrida (ver `ruas` na saida)
        "steps": "true",
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(f"{base}/route/v1/{perfil}/{coord_str}", params=params)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Servico de rotas fora do ar ({e.__class__.__name__})")

    # O OSRM responde 400 pra "nao consegui rotear" (NoRoute, NoSegment,
    # InvalidQuery) -- nao e servidor com problema, e clique em lugar ruim. Sem
    # separar os dois, o consultor veria "servico fora do ar" com JSON cru na
    # tela toda vez que clicasse numa ilha ou dentro de condominio fechado.
    if r.status_code == 400:
        print(f"[trajeto] OSRM recusou ({r.status_code}): {r.text[:200]}")
        raise HTTPException(422, "Não achei caminho por rua entre esses pontos.")
    if r.status_code != 200:
        # detalhe vai pro log: o endpoint e publico, nao devolve corpo interno
        print(f"[trajeto] OSRM {r.status_code}: {r.text[:200]}")
        raise HTTPException(502, "Servico de rotas respondeu com erro.")

    try:
        data = r.json()
    except ValueError:
        print(f"[trajeto] resposta nao-JSON do OSRM: {r.text[:200]}")
        raise HTTPException(502, "Servico de rotas respondeu algo inesperado.")

    rotas = data.get("routes")
    if data.get("code") != "Ok" or not isinstance(rotas, list) or not rotas:
        raise HTTPException(422, "Não achei caminho por rua entre esses pontos.")

    # O OSRM gruda cada ponto na rua mais proxima e devolve "Ok" mesmo quando a
    # rua esta a quilometros dali (ver TRAJETO_SNAP_MAX_M).
    for i, w in enumerate(data.get("waypoints") or []):
        gruda = w.get("distance")
        if gruda is not None and gruda > TRAJETO_SNAP_MAX_M:
            raise HTTPException(
                422,
                f"O ponto {i + 1} está a {gruda / 1000:.1f} km da rua mais próxima. "
                "Clique em cima de uma rua.",
            )

    rota = rotas[0] or {}
    coords = (rota.get("geometry") or {}).get("coordinates") or []
    if not coords:
        raise HTTPException(422, "Não achei caminho por rua entre esses pontos.")

    distancia = float(rota.get("distance") or 0.0)
    # O teto dos 30 km vale em LINHA RETA; o caminho por rua pode estourar muito
    # isso mesmo com cliques pertinho -- rio no meio, via expressa sem travessia
    # -- e voltar um desvio de dezenas de km. Aquilo nao e corredor de visita
    # nenhum: melhor recusar do que desenhar a volta na cidade como se fosse.
    teto_rota_m = (len(payload.pontos) - 1) * TRAJETO_MAX_KM * 1000
    if distancia > teto_rota_m:
        raise HTTPException(
            422,
            f"O caminho por rua deu {distancia / 1000:.1f} km — longe demais para "
            "um corredor de visitas. Tente pontos mais perto ou do mesmo lado.",
        )

    # Nome de cada via percorrida, sem repetir e na ordem em que aparecem.
    # Trecho sem nome (passagem de pedestre, praca, escadaria) simplesmente nao
    # entra na lista -- nao ha nome pra casar com o cadastro do cliente.
    ruas: List[str] = []
    for leg in rota.get("legs") or []:
        for passo in (leg or {}).get("steps") or []:
            nome = ((passo or {}).get("name") or "").strip()
            if nome and nome not in ruas:
                ruas.append(nome)

    return TrajetoOutput(
        # OSRM devolve [lng, lat]; o front le [lat, lng]
        linha=[[float(c[1]), float(c[0])] for c in coords],
        distancia_m=distancia,
        duracao_s=float(rota.get("duration") or 0.0),
        ruas=ruas,
    )

