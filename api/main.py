import os
from datetime import date, timedelta
from typing import List, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from pydantic import BaseModel, Field

OSRM_BASE_URL = os.getenv("OSRM_BASE_URL", "http://osrm-pap:5000")
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

