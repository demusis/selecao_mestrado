# -*- coding: utf-8 -*-
import secrets
import sqlite3
import click
import os
from flask import Flask, request, render_template_string, redirect, url_for, flash, g, session
from flask.cli import with_appcontext
from collections import defaultdict
from datetime import datetime
from functools import wraps
from statistics import mean

# --- 1. CONFIGURAÇÃO DA APLICAÇÃO ---
app = Flask(__name__)
app.secret_key = secrets.token_hex(16)
app.config['ADMIN_PASSWORD'] = '42' # Senha para acesso administrativo. Em produção, use uma variável de ambiente.

DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sasac.db')

# --- 2. GESTÃO DO BANCO DE DADOS SQLITE ---
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()

# --- DECORADOR DE AUTENTICAÇÃO ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

SCHEMA_SQL = """
DROP TABLE IF EXISTS avaliacoes; DROP TABLE IF EXISTS preferencias_candidatos; DROP TABLE IF EXISTS orientadores; DROP TABLE IF EXISTS candidatos; DROP TABLE IF EXISTS configuracoes;
CREATE TABLE orientadores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL,
    vagas INTEGER NOT NULL DEFAULT 0,
    token TEXT NOT NULL UNIQUE,
    avalia_curriculo INTEGER NOT NULL DEFAULT 0,
    avalia_entrevista INTEGER NOT NULL DEFAULT 0,
    avalia_afinidade INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE candidatos ( id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL );
CREATE TABLE preferencias_candidatos (
    candidato_id INTEGER NOT NULL,
    orientador_id INTEGER NOT NULL,
    FOREIGN KEY (candidato_id) REFERENCES candidatos(id) ON DELETE CASCADE,
    FOREIGN KEY (orientador_id) REFERENCES orientadores(id) ON DELETE CASCADE,
    PRIMARY KEY (candidato_id, orientador_id)
);
CREATE TABLE avaliacoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, orientador_id INTEGER NOT NULL, candidato_id INTEGER NOT NULL,
    s2_1 INTEGER, s2_2 INTEGER, s3_1 INTEGER, s3_2 INTEGER, s4_1 INTEGER, s4_2 INTEGER,
    FOREIGN KEY (orientador_id) REFERENCES orientadores(id) ON DELETE CASCADE,
    FOREIGN KEY (candidato_id) REFERENCES candidatos(id) ON DELETE CASCADE,
    UNIQUE(orientador_id, candidato_id)
);
CREATE TABLE configuracoes ( chave TEXT PRIMARY KEY, valor TEXT NOT NULL );
"""

QUESTIONARIO_ESTRUTURA = {
    "II. Avaliação do Currículo": [{"id": "s2_1", "texto": "2.1. O desempenho acadêmico e a formação do candidato são adequados."}, {"id": "s2_2", "texto": "2.2. O candidato possui experiência prévia relevante em pesquisa."}],
    "III. Avaliação da Entrevista": [{"id": "s3_1", "texto": "3.1. O candidato comunicou-se com clareza e objetividade."}, {"id": "s3_2", "texto": "3.2. A motivação do candidato é evidente e bem fundamentada."}],
    "IV. Avaliação da Afinidade": [{"id": "s4_1", "texto": "4.1. Os interesses de pesquisa estão alinhados aos do orientador."}, {"id": "s4_2", "texto": "4.2. O candidato demonstra alto potencial de desenvolvimento."}]
}

def init_db_logic():
    db = get_db()
    db.executescript(SCHEMA_SQL)
    cursor = db.cursor()
    cursor.execute("INSERT INTO configuracoes (chave, valor) VALUES ('peso_preparo', '0.5'), ('peso_afinidade', '0.5'), ('peso_preferencia_candidato', '0.5')")
    for secao in QUESTIONARIO_ESTRUTURA.values():
        for questao in secao:
            cursor.execute("INSERT INTO configuracoes (chave, valor) VALUES (?, ?)", (questao['id'], '1.0'))
    db.commit()

@click.command('init-db')
@with_appcontext
def init_db_command():
    init_db_logic()
    click.echo('Base de dados inicializada (tabelas criadas e configurações padrão inseridas).')

app.cli.add_command(init_db_command)

# --- 3. LÓGICA DE NEGÓCIO ---
DADOS_SESSAO = { "alocacao_final": None, "nao_alocados": None, "todas_pontuacoes": None, "configs_usadas": None, "data_processamento": None }

# ALTERADO: Lógica de alocação para guardar o detalhe completo do cálculo da nota.
def executar_alocacao():
    db = get_db()
    orientadores_raw = db.execute("SELECT * FROM orientadores").fetchall()
    orientadores = {row['id']: dict(row) for row in orientadores_raw}
    orientadores_com_vagas = {k: v for k, v in orientadores.items() if v['vagas'] > 0}

    candidatos = {row['id']: dict(row) for row in db.execute("SELECT * FROM candidatos").fetchall()}
    avaliacoes = db.execute("SELECT * FROM avaliacoes").fetchall()
    configs = {row['chave']: float(row['valor']) for row in db.execute("SELECT * FROM configuracoes").fetchall()}
    peso_preparo_geral = configs.get('peso_preparo', 0.5)
    peso_afinidade_geral = configs.get('peso_afinidade', 0.5)
    bonus_preferencia_config = configs.get('peso_preferencia_candidato', 0.0)

    preferencias_raw = db.execute("SELECT * FROM preferencias_candidatos").fetchall()
    preferencias_candidatos = defaultdict(set)
    for pref in preferencias_raw:
        preferencias_candidatos[pref['candidato_id']].add(pref['orientador_id'])

    if not avaliacoes:
        flash("Nenhuma avaliação foi submetida.", "warning")
        return

    now = datetime.now().astimezone()
    offset_str = now.strftime('%z')
    formatted_offset = f"{offset_str[:3]}:{offset_str[3:]}"
    timestamp_str = now.strftime(f"%d/%m/%Y às %H:%M:%S (UTC{formatted_offset})")

    DADOS_SESSAO['data_processamento'] = timestamp_str
    DADOS_SESSAO['configs_usadas'] = configs

    notas_curriculo_por_candidato = defaultdict(lambda: defaultdict(list))
    for av in avaliacoes:
        oid = av['orientador_id']
        if orientadores.get(oid) and orientadores[oid]['avalia_curriculo']:
            cid = av['candidato_id']
            if av['s2_1'] is not None: notas_curriculo_por_candidato[cid]['s2_1'].append(av['s2_1'])
            if av['s2_2'] is not None: notas_curriculo_por_candidato[cid]['s2_2'].append(av['s2_2'])

    ipc_por_candidato = {}
    for cid, notas in notas_curriculo_por_candidato.items():
        soma_ponderada_preparo, soma_pesos_preparo = 0, 0
        questoes_curriculo = QUESTIONARIO_ESTRUTURA["II. Avaliação do Currículo"]
        for questao in questoes_curriculo:
            qid = questao['id']
            if notas[qid]:
                nota_media = mean(notas[qid])
                peso = configs.get(qid, 1.0)
                soma_ponderada_preparo += nota_media * peso
                soma_pesos_preparo += peso
        ipc_por_candidato[cid] = soma_ponderada_preparo / soma_pesos_preparo if soma_pesos_preparo > 0 else 0

    pontuacoes = []
    for avaliacao in avaliacoes:
        cid = avaliacao['candidato_id']
        oid = avaliacao['orientador_id']
        orientador_atual = orientadores.get(oid)

        if not orientador_atual or oid not in orientadores_com_vagas:
            continue

        if cid not in ipc_por_candidato:
            continue
        
        ip_c = ipc_por_candidato[cid]

        soma_ponderada_afinidade, soma_pesos_afinidade = 0, 0
        
        if orientador_atual['avalia_entrevista']:
            for questao in QUESTIONARIO_ESTRUTURA["III. Avaliação da Entrevista"]:
                qid = questao['id']
                if avaliacao[qid] is not None:
                    peso = configs.get(qid, 1.0)
                    soma_ponderada_afinidade += avaliacao[qid] * peso
                    soma_pesos_afinidade += peso

        if orientador_atual['avalia_afinidade']:
            for questao in QUESTIONARIO_ESTRUTURA["IV. Avaliação da Afinidade"]:
                qid = questao['id']
                if avaliacao[qid] is not None:
                    peso = configs.get(qid, 1.0)
                    soma_ponderada_afinidade += avaliacao[qid] * peso
                    soma_pesos_afinidade += peso

        ia_oc = soma_ponderada_afinidade / soma_pesos_afinidade if soma_pesos_afinidade > 0 else 0

        p_oc = (peso_preparo_geral * ip_c) + (peso_afinidade_geral * ia_oc)
        
        bonus_aplicado = 0
        if oid in preferencias_candidatos.get(cid, set()):
            p_oc += bonus_preferencia_config
            bonus_aplicado = bonus_preferencia_config

        pontuacoes.append({
            "id_candidato": cid,
            "id_orientador": oid,
            "pontuacao_final": p_oc,
            "detalhes": {
                "ipc": ip_c,
                "iaoc": ia_oc,
                "peso_preparo": peso_preparo_geral,
                "peso_afinidade": peso_afinidade_geral,
                "bonus": bonus_aplicado
            }
        })

    DADOS_SESSAO["todas_pontuacoes"] = pontuacoes

    pontuacoes.sort(key=lambda x: x["pontuacao_final"], reverse=True)
    candidatos_alocados_ids, vagas_preenchidas = set(), {o_id: 0 for o_id in orientadores_com_vagas}
    alocacao = {o_id: [] for o_id in orientadores_com_vagas}
    for par in pontuacoes:
        id_c, id_o = par["id_candidato"], par["id_orientador"]
        if id_o in orientadores_com_vagas and id_c not in candidatos_alocados_ids and vagas_preenchidas[id_o] < orientadores_com_vagas[id_o]["vagas"]:
            alocacao[id_o].append({
                "id": id_c,
                "nome": candidatos[id_c]["nome"],
                "pontuacao_alocacao": round(par["pontuacao_final"], 2),
                "preferencia_indicada": par["detalhes"]["bonus"] > 0
            })
            vagas_preenchidas[id_o] += 1
            candidatos_alocados_ids.add(id_c)

    candidatos_avaliados_ids = {av['candidato_id'] for av in avaliacoes}
    nao_alocados_ids = candidatos_avaliados_ids - candidatos_alocados_ids
    DADOS_SESSAO["alocacao_final"] = alocacao
    DADOS_SESSAO["nao_alocados"] = [candidatos[cid] for cid in nao_alocados_ids]
    flash("Processo de alocação executado com sucesso!", "success")

# --- 4. TEMPLATES HTML ---
TPL_BASE_HEAD = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, shrink-to-fit=no"><link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/css/bootstrap.min.css"><title>SASAC v5.3</title><script src="https://polyfill.io/v3/polyfill.min.js?features=es6"></script><script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script><style>
    @media print {
        .no-print { display: none !important; }
        .card { border: 1px solid #ccc !important; box-shadow: none !important; page-break-inside: avoid; }
        a { text-decoration: none !important; color: black !important; }
        .list-group-item { page-break-inside: avoid; }
        h3, h4 { page-break-after: avoid; }
    }
    .detalhe-nota {
        font-size: 0.8em;
        color: #6c757d;
        padding-left: 20px;
        border-left: 2px solid #eee;
        margin-top: 5px;
    }
</style></head><body><div class="container mt-4">"""
TPL_HEADER_ADMIN = TPL_BASE_HEAD + """<nav class="navbar navbar-expand-lg navbar-light bg-light mb-4 no-print"><a class="navbar-brand" href="/">SASAC v5.3</a><div class="collapse navbar-collapse"><ul class="navbar-nav mr-auto"><li class="nav-item"><a class="nav-link" href="/admin">Painel Administrativo</a></li><li class="nav-item"><a class="nav-link" href="/orientadores">Orientadores</a></li><li class="nav-item"><a class="nav-link" href="/candidatos">Candidatos</a></li><li class="nav-item"><a class="nav-link" href="/avaliar">Avaliar</a></li><li class="nav-item"><a class="nav-link" href="/ajuda">Ajuda</a></li></ul><ul class="navbar-nav"><li class="nav-item"><a class="nav-link" href="{{ url_for('logout') }}">Logout</a></li></ul></div></nav>{% with messages = get_flashed_messages(with_categories=true) %}<div class="no-print">{% if messages %}{% for category, message in messages %}<div class="alert alert-{{ category }}" role="alert">{{ message }}</div>{% endfor %}{% endif %}</div>{% endwith %}"""
TPL_HEADER_AVALIACAO = TPL_BASE_HEAD + """<nav class="navbar navbar-light bg-light mb-4 no-print"><span class="navbar-brand">SASAC v5.3 - Portal de Avaliação</span></nav>{% with messages = get_flashed_messages(with_categories=true) %}<div class="no-print">{% if messages %}{% for category, message in messages %}<div class="alert alert-{{ category }}" role="alert">{{ message }}</div>{% endfor %}{% endif %}</div>{% endwith %}"""
TPL_HEADER_LOGIN = TPL_BASE_HEAD + """<nav class="navbar navbar-light bg-light mb-4 no-print"><span class="navbar-brand">SASAC v5.3 - Acesso Administrativo</span></nav>{% with messages = get_flashed_messages(with_categories=true) %}<div class="no-print">{% if messages %}{% for category, message in messages %}<div class="alert alert-{{ category }}" role="alert">{{ message }}</div>{% endfor %}{% endif %}</div>{% endwith %}"""
TPL_FOOTER = "</div></body></html>"

TPL_LOGIN_CONTENT = """
<div class="row justify-content-center">
    <div class="col-md-6">
        <div class="card">
            <div class="card-header">Login</div>
            <div class="card-body">
                <form method="post">
                    <div class="form-group">
                        <label for="password">Senha de Administrador</label>
                        <input type="password" class="form-control" id="password" name="password" required>
                    </div>
                    <button type="submit" class="btn btn-primary">Entrar</button>
                </form>
            </div>
        </div>
    </div>
</div>
"""
TPL_LOGIN = TPL_HEADER_LOGIN + TPL_LOGIN_CONTENT + TPL_FOOTER

TPL_ADMIN_CONTENT = """
    <h2>Painel Administrativo</h2>
    <form action="{{ url_for('salvar_configuracoes') }}" method="post">
        <div class="card mb-4"><div class="card-header">Configurações de Avaliação</div>
        <div class="card-body">
            <div class="row">
                <div class="col-md-6">
                    <h5>Pesos Gerais</h5>
                    <div class="form-group">
                        <label for="peso_preparo">Peso do Preparo (CV): <b id="peso_preparo_label">{{ configs.get('peso_preparo', 0.5) * 100 }}%</b></label>
                        <input type="range" class="form-control-range" id="peso_preparo" name="peso_preparo" min="0" max="100" value="{{ configs.get('peso_preparo', 0.5) * 100 }}" oninput="updatePeso(this.value)">
                    </div>
                    <div class="form-group">
                        <label>Peso da Afinidade (Entrevista + Afinidade): <b id="peso_afinidade_label">{{ configs.get('peso_afinidade', 0.5) * 100 }}%</b></label>
                    </div>
                    <hr>
                    <h5>Bônus de Alocação</h5>
                     <div class="form-group">
                        <label for="peso_preferencia_candidato">Bônus por Preferência do Candidato</label>
                        <input type="number" step="0.1" class="form-control" name="peso_preferencia_candidato" id="peso_preferencia_candidato" value="{{ configs.get('peso_preferencia_candidato', 0.5) }}">
                        <small class="form-text text-muted">Valor somado à pontuação final se o orientador for um dos preferidos pelo candidato.</small>
                    </div>
                </div>
                <div class="col-md-6">
                    <h5>Pesos Individuais das Questões</h5>
                    {% for secao, questoes in questionario.items() %}
                        <strong>{{ secao }}</strong>
                        {% for questao in questoes %}
                        <div class="form-group row">
                            <label for="{{ questao.id }}" class="col-sm-8 col-form-label-sm">{{ questao.texto }}</label>
                            <div class="col-sm-4"><input type="number" step="0.1" class="form-control form-control-sm" name="{{ questao.id }}" id="{{ questao.id }}" value="{{ configs.get(questao.id, 1.0) }}"></div>
                        </div>
                        {% endfor %}
                    {% endfor %}
                </div>
            </div>
        </div>
        <div class="card-footer"><button type="submit" class="btn btn-success">Salvar Todas as Configurações</button></div>
        </div>
    </form>
    <div class="card mb-4"><div class="card-body">
        <h5 class="card-title">Ações do Sistema</h5>
        <form action="{{ url_for('processar') }}" method="post" class="d-inline mb-2"><button type="submit" class="btn btn-primary">Executar Alocação</button></form>
        <a href="/" class="btn btn-secondary">Ver Último Relatório</a>
    </div></div>
    <div class="card border-danger mb-4"><div class="card-header bg-danger text-white">Ações Destrutivas</div><div class="card-body">
        <p>A ação abaixo permite recomeçar a rodada de avaliações, apagando todas as notas já submetidas mas preservando os orientadores e candidatos.</p>
        <form action="{{ url_for('clear_evaluations') }}" method="post" class="d-inline">
            <button type="submit" class="btn btn-warning" onclick="return confirm('Tem a certeza que deseja apagar TODAS as avaliações? Esta ação não pode ser desfeita.');">Apagar Todas as Avaliações</button>
        </form>
        <hr>
        <p>A ação abaixo apaga <strong>TUDO</strong>: orientadores, candidatos, avaliações e configurações. A base de dados será recriada vazia.</p>
        <form action="{{ url_for('reset_database') }}" method="post" class="d-inline">
            <button type="submit" class="btn btn-danger" onclick="return confirm('ATENÇÃO! Tem a certeza que deseja apagar TODOS os dados e reinicializar a base de dados? Esta ação é IRREVERSÍVEL.');">Reinicializar Base de Dados</button>
        </form>
    </div></div>
    <script>
        function updatePeso(value) {
            document.getElementById('peso_preparo_label').innerText = value + '%';
            document.getElementById('peso_afinidade_label').innerText = 100 - value + '%';
        }
    </script>
"""
TPL_ADMIN = TPL_HEADER_ADMIN + TPL_ADMIN_CONTENT + TPL_FOOTER

TPL_ORIENTADOR_LIST = TPL_HEADER_ADMIN + """
<div class="d-flex justify-content-between align-items-center mb-3"><h2>Avaliadores e Orientadores</h2><a href="/orientadores/add" class="btn btn-success">Adicionar Novo</a></div>
<table class="table">
    <thead><tr><th>Nome</th><th>Atribuições</th><th>Vagas</th><th>Ações</th></tr></thead>
    <tbody>
    {% for o in orientadores %}
    <tr>
        <td>{{ o.nome }}</td>
        <td>
            {% set funcoes = [] %}
            {% if o.avalia_curriculo %}{% set _ = funcoes.append('Currículo') %}{% endif %}
            {% if o.avalia_entrevista %}{% set _ = funcoes.append('Entrevista') %}{% endif %}
            {% if o.avalia_afinidade %}{% set _ = funcoes.append('Afinidade') %}{% endif %}
            {{ funcoes|join(', ') if funcoes else 'Nenhuma' }}
        </td>
        <td>{{ o.vagas }}</td>
        <td>
            <a href="/orientadores/edit/{{ o.id }}" class="btn btn-sm btn-warning">Editar</a>
            <form action="/orientadores/delete/{{ o.id }}" method="post" class="d-inline">
                <button type="submit" class="btn btn-sm btn-danger" onclick="return confirm('Tem certeza?');">Apagar</button>
            </form>
        </td>
    </tr>
    {% endfor %}
    </tbody>
</table>""" + TPL_FOOTER

TPL_ORIENTADOR_FORM = TPL_HEADER_ADMIN + """
<h2>{{ titulo }}</h2>
<form method="post">
    <div class="form-group">
        <label for="nome">Nome</label>
        <input type="text" name="nome" id="nome" class="form-control" value="{{ orientador.nome if orientador else '' }}" required>
    </div>
    <div class="form-group">
        <label>Atribuições de Avaliação</label>
        <div class="form-check">
            <input class="form-check-input" type="checkbox" name="avalia_curriculo" id="avalia_curriculo" {% if orientador and orientador.avalia_curriculo %}checked{% endif %}>
            <label class="form-check-label" for="avalia_curriculo">Avaliador de Currículo</label>
        </div>
        <div class="form-check">
            <input class="form-check-input" type="checkbox" name="avalia_entrevista" id="avalia_entrevista" {% if orientador and orientador.avalia_entrevista %}checked{% endif %}>
            <label class="form-check-label" for="avalia_entrevista">Avaliador de Entrevista</label>
        </div>
        <div class="form-check">
            <input class="form-check-input" type="checkbox" name="avalia_afinidade" id="avalia_afinidade" {% if orientador and orientador.avalia_afinidade %}checked{% endif %}>
            <label class="form-check-label" for="avalia_afinidade">Avaliador de Afinidade</label>
        </div>
    </div>
    <div class="form-group" id="vagas_group">
        <label for="vagas">Vagas de Orientação</label>
        <input type="number" name="vagas" id="vagas" class="form-control" value="{{ orientador.vagas if orientador is not none else '0' }}" min="0" required>
        <small class="form-text text-muted">Defina como 0 se for apenas um avaliador sem vagas de orientação.</small>
    </div>
    <button type="submit" class="btn btn-primary">Salvar</button>
    <a href="/orientadores" class="btn btn-secondary">Cancelar</a>
</form>
""" + TPL_FOOTER

TPL_CANDIDATO_LIST = TPL_HEADER_ADMIN + """<div class="d-flex justify-content-between align-items-center mb-3"><h2>Candidatos</h2><a href="/candidatos/add" class="btn btn-success">Adicionar Novo</a></div><table class="table"><thead><tr><th>Nome</th><th>Ações</th></tr></thead><tbody>{% for c in candidatos %}<tr><td>{{ c.nome }}</td><td><a href="/candidatos/edit/{{ c.id }}" class="btn btn-sm btn-warning">Editar</a> <form action="/candidatos/delete/{{ c.id }}" method="post" class="d-inline"><button type="submit" class="btn btn-sm btn-danger" onclick="return confirm('Tem certeza?');">Apagar</button></form></td></tr>{% endfor %}</tbody></table>""" + TPL_FOOTER

TPL_CANDIDATO_FORM = TPL_HEADER_ADMIN + """
<h2>{{ titulo }}</h2>
<form method="post">
    <div class="form-group">
        <label for="nome">Nome</label>
        <input type="text" name="nome" id="nome" class="form-control" value="{{ candidato.nome if candidato else '' }}" required>
    </div>
    <div class="form-group">
        <label>Preferência de Orientadores (opcional)</label>
        {% for orientador in orientadores %}
        <div class="form-check">
            <input class="form-check-input" type="checkbox" name="preferencias" value="{{ orientador.id }}" id="pref_{{ orientador.id }}" {% if orientador.id in preferencias_atuais %}checked{% endif %}>
            <label class="form-check-label" for="pref_{{ orientador.id }}">{{ orientador.nome }}</label>
        </div>
        {% endfor %}
        {% if not orientadores %}
        <small class="form-text text-muted">Nenhum orientador registado.</small>
        {% endif %}
    </div>
    <button type="submit" class="btn btn-primary">Salvar</button> 
    <a href="/candidatos" class="btn btn-secondary">Cancelar</a>
</form>
""" + TPL_FOOTER

TPL_AVALIAR_INDEX_CONTENT = """
<h2>Portal de Avaliação de Orientadores</h2>
<p>Abaixo estão os links de acesso únicos para cada orientador. Partilhe o link correspondente com cada um para que possam submeter as suas avaliações.</p>
<table class="table table-bordered">
    <thead class="thead-light"><tr><th>Orientador/Avaliador</th><th>Link de Avaliação</th><th style="width: 1%;" class="text-center">Ação</th></tr></thead>
    <tbody>
        {% for o in orientadores %}
        <tr>
            <td class="align-middle">{{ o.nome }}</td>
            <td><input type="text" readonly class="form-control-plaintext bg-light p-2 border rounded" id="link-{{ o.id }}" value="{{ url_for('avaliar_home', token=o.token, _external=True) }}"></td>
            <td class="text-center align-middle"><button class="btn btn-secondary btn-sm" onclick="copiarLink('link-{{ o.id }}', this)">Copiar</button></td>
        </tr>
        {% endfor %}
    </tbody>
</table>
<script>
function copiarLink(elementId, button) {
    var copyText = document.getElementById(elementId);
    var textArea = document.createElement("textarea");
    textArea.value = copyText.value;
    textArea.style.top = "0"; textArea.style.left = "0"; textArea.style.position = "fixed";
    document.body.appendChild(textArea);
    textArea.focus(); textArea.select();
    try {
        var successful = document.execCommand('copy');
        if (successful) {
            var originalText = button.innerText;
            button.innerText = 'Copiado!';
            button.classList.remove('btn-secondary'); button.classList.add('btn-success');
            setTimeout(function() {
                button.innerText = originalText;
                button.classList.remove('btn-success'); button.classList.add('btn-secondary');
            }, 2000);
        }
    } catch (err) { console.error('Não foi possível copiar o link', err); }
    document.body.removeChild(textArea);
}
</script>
"""
TPL_AVALIAR_INDEX = TPL_HEADER_ADMIN + TPL_AVALIAR_INDEX_CONTENT + TPL_FOOTER
TPL_LISTA_CANDIDATOS = TPL_HEADER_AVALIACAO + """<h3>Página de Avaliação de {{ orientador.nome }}</h3><p>Selecione um candidato para avaliar ou para modificar uma avaliação existente.</p><table class="table"><thead><tr><th>Candidato</th><th>Ação</th></tr></thead><tbody>{% for c in candidatos %}<tr><td>{{ c.nome }}</td><td>{% if c.id in avaliados %}<a href="/avaliar/{{ orientador.token }}/{{ c.id }}" class="btn btn-sm btn-warning">Modificar Avaliação</a>{% else %}<a href="/avaliar/{{ orientador.token }}/{{ c.id }}" class="btn btn-sm btn-outline-primary">Avaliar</a>{% endif %}</td></tr>{% endfor %}</tbody></table>""" + TPL_FOOTER

TPL_FORM_AVALIACAO = TPL_HEADER_AVALIACAO + """
<h4>Avaliando: {{ candidato.nome }}</h4>
<p>Avaliador: {{ orientador.nome }}</p>
<form method="post">
    {% if not orientador.avalia_curriculo and not orientador.avalia_entrevista and not orientador.avalia_afinidade %}
        <div class="alert alert-warning">Este avaliador não possui nenhuma atribuição de avaliação configurada.</div>
    {% endif %}

    {% if orientador.avalia_curriculo %}
    <h5>{{ questionario.keys()|list|first }}</h5>
    {% for item in questionario["II. Avaliação do Currículo"] %}
    <div class="form-group">
        <label>{{ item.texto }}</label>
        <div>
        {% for val, label in [(-2, '-2: Discordo Totalmente'), (-1, '-1: Discordo'), (0, '0: Neutro'), (1, '+1: Concordo'), (2, '+2: Concordo Totalmente')] %}
            <div class="form-check form-check-inline">
                <input class="form-check-input" type="radio" name="{{ item.id }}" id="{{ item.id }}_{{ val }}" value="{{ val }}" {% if avaliacao_existente and avaliacao_existente[item.id] == val %}checked{% endif %} required>
                <label class="form-check-label" for="{{ item.id }}_{{ val }}">{{ label }}</label>
            </div>
        {% endfor %}
        </div>
    </div>
    {% endfor %}
    {% endif %}

    {% if orientador.avalia_entrevista %}
    <h5 class="mt-4">{{ (questionario.keys()|list)[1] }}</h5>
    {% for item in questionario["III. Avaliação da Entrevista"] %}
    <div class="form-group">
        <label>{{ item.texto }}</label>
        <div>
        {% for val, label in [(-2, '-2: Discordo Totalmente'), (-1, '-1: Discordo'), (0, '0: Neutro'), (1, '+1: Concordo'), (2, '+2: Concordo Totalmente')] %}
            <div class="form-check form-check-inline">
                <input class="form-check-input" type="radio" name="{{ item.id }}" id="{{ item.id }}_{{ val }}" value="{{ val }}" {% if avaliacao_existente and avaliacao_existente[item.id] == val %}checked{% endif %} required>
                <label class="form-check-label" for="{{ item.id }}_{{ val }}">{{ label }}</label>
            </div>
        {% endfor %}
        </div>
    </div>
    {% endfor %}
    {% endif %}

    {% if orientador.avalia_afinidade %}
    <h5 class="mt-4">{{ questionario.keys()|list|last }}</h5>
    {% for item in questionario["IV. Avaliação da Afinidade"] %}
    <div class="form-group">
        <label>{{ item.texto }}</label>
        <div>
        {% for val, label in [(-2, '-2: Discordo Totalmente'), (-1, '-1: Discordo'), (0, '0: Neutro'), (1, '+1: Concordo'), (2, '+2: Concordo Totalmente')] %}
            <div class="form-check form-check-inline">
                <input class="form-check-input" type="radio" name="{{ item.id }}" id="{{ item.id }}_{{ val }}" value="{{ val }}" {% if avaliacao_existente and avaliacao_existente[item.id] == val %}checked{% endif %} required>
                <label class="form-check-label" for="{{ item.id }}_{{ val }}">{{ label }}</label>
            </div>
        {% endfor %}
        </div>
    </div>
    {% endfor %}
    {% endif %}
    
    {% if orientador.avalia_curriculo or orientador.avalia_entrevista or orientador.avalia_afinidade %}
    <button type="submit" class="btn btn-success mt-3">{% if avaliacao_existente %}Atualizar Avaliação{% else %}Enviar Avaliação{% endif %}</button>
    {% endif %}
</form>""" + TPL_FOOTER

# ALTERADO: Template do relatório para exibir o detalhamento completo da pontuação.
TPL_RELATORIO = TPL_HEADER_ADMIN + """
<div class="d-flex justify-content-between align-items-center mb-3">
    <h3>Relatório de Alocação Final</h3>
    {% if alocacao %}
    <button onclick="window.print();" class="btn btn-info no-print">Imprimir Relatório</button>
    {% endif %}
</div>
{% if data_processamento %}<p class="text-muted mb-4">Data e hora do servidor: {{ data_processamento }}</p>{% endif %}

{% macro render_detalhes_candidato(c, pontuacoes_por_candidato, orientadores) %}
    {% if pontuacoes_por_candidato[c.id] %}
        <small class="form-text text-muted">
            <u>Avaliações recebidas (de orientadores com vagas):</u>
            <ul class="list-unstyled mb-0 mt-2">
            {% for p in pontuacoes_por_candidato[c.id] %}
                <li class="mb-2">
                    <strong>{{ p.orientador_nome }}: {{ "%.2f"|format(p.pontuacao) }}</strong>
                    <div class="detalhe-nota">
                        P = (Peso Preparo × IPc) + (Peso Afinidade × IAoc) + Bônus <br>
                        P = ({{ "%.2f"|format(p.detalhes.peso_preparo) }} × {{ "%.2f"|format(p.detalhes.ipc) }}) + ({{ "%.2f"|format(p.detalhes.peso_afinidade) }} × {{ "%.2f"|format(p.detalhes.iaoc) }}) + {{ "%.2f"|format(p.detalhes.bonus) }}
                    </div>
                </li>
            {% endfor %}
            </ul>
        </small>
    {% endif %}
{% endmacro %}

{% if alocacao %}
    {% for o_id, alocados in alocacao.items() %}
    <div class="card mb-3">
        <div class="card-header"><strong>{{ orientadores[o_id].nome }}</strong> (Vagas: {{ orientadores[o_id].vagas }})</div>
        <ul class="list-group list-group-flush">
            {% if alocados %}
                {% for c in alocados %}
                <li class="list-group-item">
                    {{ c.nome }} (<b>Pontuação de alocação: {{ c.pontuacao_alocacao }}</b>)
                    {% if c.preferencia_indicada %}<span class="badge badge-info ml-2">Preferência Indicada</span>{% endif %}
                    {{ render_detalhes_candidato(c, pontuacoes_por_candidato, orientadores) }}
                </li>
                {% endfor %}
            {% else %}
                <li class="list-group-item">Nenhum candidato alocado.</li>
            {% endif %}
        </ul>
    </div>
    {% endfor %}
    <h4 class="mt-4">Candidatos Não Alocados</h4>
    {% if nao_alocados %}
        <ul class="list-group">
        {% for c in nao_alocados %}
            <li class="list-group-item">
                {{ c.nome }}
                {{ render_detalhes_candidato(c, pontuacoes_por_candidato, orientadores) }}
            </li>
        {% endfor %}
        </ul>
    {% else %}
        <p>Todos os candidatos avaliados foram alocados.</p>
    {% endif %}
    <hr class="mt-4">
    <h4 class="mt-4">Candidatos não selecionados</h4>
    <p class="text-muted">Os candidatos abaixo não receberam nenhuma avaliação e, portanto, não participaram do processo de alocação.</p>
    {% if candidatos_nao_avaliados %}<ul class="list-group">{% for c in candidatos_nao_avaliados %}<li class="list-group-item">{{ c.nome }}</li>{% endfor %}</ul>
    {% else %}<p>Todos os candidatos registados foram avaliados.</p>{% endif %}
    <hr class="mt-4">
    <h4 class="mt-4">Pesos Utilizados na Alocação</h4>
    {% if configs_usadas %}
        <div class="row">
            <div class="col-md-6">
                <h5>Pesos Gerais</h5>
                <p><strong>Peso do Preparo (CV):</strong> {{ (configs_usadas.get('peso_preparo', 0.5) * 100)|round|int }}%</p>
                <p><strong>Peso da Afinidade (Entrevista + Afinidade):</strong> {{ (configs_usadas.get('peso_afinidade', 0.5) * 100)|round|int }}%</p>
                <p><strong>Bônus por Preferência do Candidato:</strong> +{{ configs_usadas.get('peso_preferencia_candidato', 0.0) }} pontos</p>
            </div>
            <div class="col-md-6">
                <h5>Pesos Individuais das Questões</h5>
                <table class="table table-sm table-bordered">
                {% for secao, questoes in questionario.items() %}
                    <thead class="thead-light"><tr><th colspan="2">{{ secao }}</th></tr></thead>
                    <tbody>
                    {% for questao in questoes %}
                    <tr><td>{{ questao.texto }}</td><td class="text-right" style="width: 20%;">{{ configs_usadas.get(questao.id, 1.0) }}</td></tr>
                    {% endfor %}
                    </tbody>
                {% endfor %}
                </table>
            </div>
        </div>
    {% else %}<p class="text-muted">Nenhuma alocação foi executada para exibir os pesos utilizados.</p>{% endif %}
{% else %}
    <div class="alert alert-info no-print">O processo de alocação ainda não foi executado.</div>
{% endif %}
""" + TPL_FOOTER

TPL_AJUDA_CONTENT = r"""
<div class="card"><div class="card-body">
<h2 class="card-title">Sistema de Apoio à Seleção e Alocação de Candidatos (SASAC)</h2><hr>
<h3>1. Visão Geral do Sistema</h3>
<p>O SASAC é uma aplicação web desenvolvida para sistematizar e apoiar o processo de seleção e alocação de candidatos a vagas de orientação em programas acadêmicos. O sistema permite o registo de orientadores (com o respetivo número de vagas) e de candidatos, e oferece uma plataforma para que cada orientador avalie os candidatos por meio de um questionário padronizado.</p>
<p>As funcionalidades principais são:</p>
<ul>
    <li><b>Painel Administrativo</b>: Interface centralizada para a gestão de entidades (orientadores e candidatos) e para a configuração dos parâmetros do algoritmo de alocação.</li>
    <li><b>Avaliação Individualizada</b>: Cada orientador acede a um portal restrito por token para submeter as suas avaliações, garantindo a confidencialidade do processo.</li>
    <li><b>Processamento Automatizado</b>: Um algoritmo executa a alocação dos candidatos às vagas com base nas avaliações submetidas e nos pesos configurados pelo administrador.</li>
    <li><b>Geração de Relatórios</b>: O sistema apresenta um relatório detalhado com o resultado final da alocação, incluindo a lista de candidatos alocados, não alocados e não avaliados.</li>
</ul>
<h3 class="mt-4">2. O Algoritmo de Alocação</h3>
<p>O núcleo do sistema é o seu algoritmo de alocação, que processa as avaliações para gerar uma correspondência ótima entre orientadores e candidatos. O processo é determinístico e pode ser decomposto nos seguintes passos:</p>
<h4 class="mt-3">2.1. Cálculo dos Índices de Avaliação</h4>
<p>Para cada avaliação submetida por um orientador <em>o</em> para um candidato <em>c</em>, o sistema calcula duas métricas principais:</p>
<ol>
    <li><b>Índice de Preparo do Candidato (IPc)</b>: Esta métrica quantifica a qualificação geral do candidato, independentemente do orientador. **É calculada como a média ponderada das notas atribuídas por todos os "Avaliadores de Currículo" às questões da seção "II. Avaliação do Currículo".**<br></li>
    <li class="mt-2"><b>Índice de Afinidade Orientador-Candidato (IAoc)</b>: Esta métrica avalia a compatibilidade específica entre o orientador e o candidato. **É calculada como a média ponderada das notas que o orientador atribui nas seções para as quais tem atribuição ("III. Avaliação da Entrevista" e/ou "IV. Avaliação da Afinidade").**<br></li>
</ol>
<h4 class="mt-3">2.2. Cálculo da Pontuação Final</h4>
<p>A pontuação final para o par (orientador, candidato) é uma média ponderada dos dois índices anteriores, utilizando pesos gerais também definidos pelo administrador. Esta pontuação só é calculada para orientadores que possuem vagas de orientação (vagas > 0).</p>
<h4 class="mt-3">2.3. Processo de Alocação</h4>
<p>A alocação é realizada através de um algoritmo do tipo greedy:</p>
<ol>
    <li>Todos os pares (o, c) de um orientador com vagas e um candidato avaliado por ele recebem uma pontuação final e são inseridos numa lista.</li>
    <li>A lista é ordenada de forma decrescente com base na pontuação final.</li>
    <li>O algoritmo itera sobre a lista ordenada. Para cada par (o, c):
        <ul>
            <li>Verifica se o candidato <em>c</em> já foi alocado.</li>
            <li>Verifica se o orientador <em>o</em> ainda possui vagas disponíveis.</li>
        </ul>
    </li>
    <li>Se ambas as condições forem satisfeitas (candidato livre e orientador com vagas), o candidato <em>c</em> é permanentemente alocado ao orientador <em>o</em>. O contador de vagas do orientador é decrementado e o candidato é marcado como alocado.</li>
    <li>O processo continua até que a lista seja percorrida por completo.</li>
</ol>
</div></div>
"""
TPL_AJUDA = TPL_HEADER_ADMIN + TPL_AJUDA_CONTENT + TPL_FOOTER

# --- 5. ROTAS DA APLICAÇÃO ---
# ALTERADO: Rota principal para processar e passar os dados detalhados para o template.
@app.route("/")
@login_required
def home():
    db = get_db()
    orientadores = {row['id']: dict(row) for row in db.execute("SELECT * FROM orientadores").fetchall()}
    candidatos_total = db.execute("SELECT * FROM candidatos").fetchall()

    alocacao = DADOS_SESSAO.get("alocacao_final")
    nao_alocados = DADOS_SESSAO.get("nao_alocados")
    todas_pontuacoes = DADOS_SESSAO.get("todas_pontuacoes")
    configs_usadas = DADOS_SESSAO.get("configs_usadas")
    data_processamento = DADOS_SESSAO.get("data_processamento")

    pontuacoes_por_candidato = defaultdict(list)
    if todas_pontuacoes:
        for p in todas_pontuacoes:
            cid = p['id_candidato']
            oid = p['id_orientador']
            if oid in orientadores:
                pontuacoes_por_candidato[cid].append({
                    'orientador_nome': orientadores[oid]['nome'],
                    'pontuacao': p['pontuacao_final'],
                    'detalhes': p['detalhes']
                })
    
    for cid in pontuacoes_por_candidato:
        pontuacoes_por_candidato[cid].sort(key=lambda x: x['pontuacao'], reverse=True)

    candidatos_avaliados_ids = set(row['candidato_id'] for row in db.execute("SELECT DISTINCT candidato_id FROM avaliacoes").fetchall())
    candidatos_nao_avaliados = [c for c in candidatos_total if c['id'] not in candidatos_avaliados_ids]

    return render_template_string(
        TPL_RELATORIO,
        alocacao=alocacao,
        nao_alocados=nao_alocados,
        orientadores=orientadores,
        pontuacoes_por_candidato=pontuacoes_por_candidato,
        candidatos_nao_avaliados=candidatos_nao_avaliados,
        configs_usadas=configs_usadas,
        questionario=QUESTIONARIO_ESTRUTURA,
        data_processamento=data_processamento
    )

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == app.config['ADMIN_PASSWORD']:
            session['logged_in'] = True
            flash('Login realizado com sucesso!', 'success')
            next_url = request.args.get('next')
            return redirect(next_url or url_for('home'))
        else:
            flash('Senha incorreta.', 'danger')
    return render_template_string(TPL_LOGIN)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    flash('Logout realizado com sucesso.', 'info')
    return redirect(url_for('login'))

@app.route("/ajuda")
@login_required
def ajuda():
    return render_template_string(TPL_AJUDA)

@app.route("/admin")
@login_required
def admin():
    db = get_db()
    configs = {row['chave']: float(row['valor']) for row in db.execute("SELECT * FROM configuracoes").fetchall()}
    return render_template_string(TPL_ADMIN, configs=configs, questionario=QUESTIONARIO_ESTRUTURA)

@app.route('/configuracoes', methods=['POST'])
@login_required
def salvar_configuracoes():
    db = get_db()
    
    peso_preparo_percent = int(request.form.get('peso_preparo', 50))
    peso_preparo = peso_preparo_percent / 100.0
    peso_afinidade = 1.0 - peso_preparo
    db.execute("UPDATE configuracoes SET valor = ? WHERE chave = ?", (str(peso_preparo), 'peso_preparo'))
    db.execute("UPDATE configuracoes SET valor = ? WHERE chave = ?", (str(peso_afinidade), 'peso_afinidade'))

    peso_preferencia = request.form.get('peso_preferencia_candidato', '0.5')
    db.execute("UPDATE configuracoes SET valor = ? WHERE chave = ?", (peso_preferencia, 'peso_preferencia_candidato'))

    for secao in QUESTIONARIO_ESTRUTURA.values():
        for questao in secao:
            peso_valor = request.form.get(questao['id'], '1.0')
            db.execute("UPDATE configuracoes SET valor = ? WHERE chave = ?", (peso_valor, questao['id']))
    
    db.commit()
    flash("Configurações de avaliação salvas com sucesso!", "success")
    return redirect(url_for('admin'))

@app.route("/processar", methods=['POST'])
@login_required
def processar():
    executar_alocacao()
    return redirect(url_for('home'))

@app.route("/avaliacoes/clear", methods=['POST'])
@login_required
def clear_evaluations():
    db = get_db()
    db.execute("DELETE FROM avaliacoes")
    db.commit()
    for key in DADOS_SESSAO: DADOS_SESSAO[key] = None
    flash('Todas as avaliações foram apagadas com sucesso. Pode iniciar uma nova rodada.', 'warning')
    return redirect(url_for('admin'))

@app.route("/admin/reset-db", methods=['POST'])
@login_required
def reset_database():
    init_db_logic()
    for key in DADOS_SESSAO: DADOS_SESSAO[key] = None
    flash('A base de dados foi completamente reinicializada com sucesso!', 'danger')
    return redirect(url_for('admin'))

@app.route("/orientadores")
@login_required
def orientadores_list():
    orientadores = get_db().execute("SELECT * FROM orientadores ORDER BY nome").fetchall()
    return render_template_string(TPL_ORIENTADOR_LIST, orientadores=orientadores)

@app.route("/orientadores/add", methods=['GET', 'POST'])
@login_required
def orientadores_add():
    if request.method == 'POST':
        nome = request.form['nome']
        vagas = int(request.form.get('vagas', 0))
        avalia_curriculo = 1 if 'avalia_curriculo' in request.form else 0
        avalia_entrevista = 1 if 'avalia_entrevista' in request.form else 0
        avalia_afinidade = 1 if 'avalia_afinidade' in request.form else 0
        token = secrets.token_urlsafe(16)
        db = get_db()
        db.execute(
            "INSERT INTO orientadores (nome, vagas, token, avalia_curriculo, avalia_entrevista, avalia_afinidade) VALUES (?, ?, ?, ?, ?, ?)",
            (nome, vagas, token, avalia_curriculo, avalia_entrevista, avalia_afinidade)
        )
        db.commit()
        flash("Registo adicionado com sucesso!", "success")
        return redirect(url_for('orientadores_list'))
    return render_template_string(TPL_ORIENTADOR_FORM, orientador=None, titulo="Adicionar Avaliador/Orientador")

@app.route("/orientadores/edit/<int:id>", methods=['GET', 'POST'])
@login_required
def orientadores_edit(id):
    db = get_db()
    if request.method == 'POST':
        nome = request.form['nome']
        vagas = int(request.form.get('vagas', 0))
        avalia_curriculo = 1 if 'avalia_curriculo' in request.form else 0
        avalia_entrevista = 1 if 'avalia_entrevista' in request.form else 0
        avalia_afinidade = 1 if 'avalia_afinidade' in request.form else 0
        db.execute(
            "UPDATE orientadores SET nome = ?, vagas = ?, avalia_curriculo = ?, avalia_entrevista = ?, avalia_afinidade = ? WHERE id = ?",
            (nome, vagas, avalia_curriculo, avalia_entrevista, avalia_afinidade, id)
        )
        db.commit()
        flash("Registo atualizado com sucesso!", "success")
        return redirect(url_for('orientadores_list'))
    orientador = db.execute("SELECT * FROM orientadores WHERE id = ?", (id,)).fetchone()
    return render_template_string(TPL_ORIENTADOR_FORM, orientador=orientador, titulo="Editar Avaliador/Orientador")

@app.route("/orientadores/delete/<int:id>", methods=['POST'])
@login_required
def orientadores_delete(id):
    db = get_db()
    db.execute("DELETE FROM orientadores WHERE id = ?", (id,))
    db.commit()
    flash("Registo apagado com sucesso!", "danger")
    return redirect(url_for('orientadores_list'))

@app.route("/candidatos")
@login_required
def candidatos_list():
    candidatos = get_db().execute("SELECT * FROM candidatos ORDER BY nome").fetchall()
    return render_template_string(TPL_CANDIDATO_LIST, candidatos=candidatos)

@app.route("/candidatos/add", methods=['GET', 'POST'])
@login_required
def candidatos_add():
    db = get_db()
    if request.method == 'POST':
        nome = request.form['nome']
        cursor = db.cursor()
        cursor.execute("INSERT INTO candidatos (nome) VALUES (?)", (nome,))
        new_candidato_id = cursor.lastrowid
        
        preferencias_ids = request.form.getlist('preferencias')
        for orientador_id in preferencias_ids:
            db.execute("INSERT INTO preferencias_candidatos (candidato_id, orientador_id) VALUES (?, ?)", (new_candidato_id, int(orientador_id)))

        db.commit()
        flash("Candidato adicionado com sucesso!", "success")
        return redirect(url_for('candidatos_list'))
    
    orientadores = db.execute("SELECT id, nome FROM orientadores ORDER BY nome").fetchall()
    return render_template_string(TPL_CANDIDATO_FORM, candidato=None, titulo="Adicionar Candidato", orientadores=orientadores, preferencias_atuais=[])

@app.route("/candidatos/edit/<int:id>", methods=['GET', 'POST'])
@login_required
def candidatos_edit(id):
    db = get_db()
    if request.method == 'POST':
        nome = request.form['nome']
        db.execute("UPDATE candidatos SET nome = ? WHERE id = ?", (nome, id))
        
        db.execute("DELETE FROM preferencias_candidatos WHERE candidato_id = ?", (id,))
        preferencias_ids = request.form.getlist('preferencias')
        for orientador_id in preferencias_ids:
            db.execute("INSERT INTO preferencias_candidatos (candidato_id, orientador_id) VALUES (?, ?)", (id, int(orientador_id)))
            
        db.commit()
        flash("Candidato atualizado com sucesso!", "success")
        return redirect(url_for('candidatos_list'))
    
    candidato = db.execute("SELECT * FROM candidatos WHERE id = ?", (id,)).fetchone()
    orientadores = db.execute("SELECT id, nome FROM orientadores ORDER BY nome").fetchall()
    preferencias_atuais = {row['orientador_id'] for row in db.execute("SELECT orientador_id FROM preferencias_candidatos WHERE candidato_id = ?", (id,)).fetchall()}
    return render_template_string(TPL_CANDIDATO_FORM, candidato=candidato, titulo="Editar Candidato", orientadores=orientadores, preferencias_atuais=preferencias_atuais)

@app.route("/candidatos/delete/<int:id>", methods=['POST'])
@login_required
def candidatos_delete(id):
    db = get_db()
    db.execute("DELETE FROM candidatos WHERE id = ?", (id,))
    db.commit()
    flash("Candidato apagado com sucesso!", "danger")
    return redirect(url_for('candidatos_list'))

@app.route("/avaliar")
@login_required
def avaliar_index():
    orientadores = get_db().execute("SELECT id, nome, token FROM orientadores ORDER BY nome").fetchall()
    return render_template_string(TPL_AVALIAR_INDEX, orientadores=orientadores)

@app.route("/avaliar/<token>")
def avaliar_home(token):
    db = get_db()
    orientador = db.execute("SELECT * FROM orientadores WHERE token = ?", (token,)).fetchone()
    if not orientador:
        return "Token de acesso inválido.", 404
    candidatos = db.execute("SELECT * FROM candidatos ORDER BY nome").fetchall()
    avaliados_ids = {row['candidato_id'] for row in db.execute("SELECT candidato_id FROM avaliacoes WHERE orientador_id = ?", (orientador['id'],)).fetchall()}
    return render_template_string(TPL_LISTA_CANDIDATOS, orientador=orientador, candidatos=candidatos, avaliados=avaliados_ids)

@app.route("/avaliar/<token>/<int:candidate_id>", methods=['GET', 'POST'])
def avaliar_candidato(token, candidate_id):
    db = get_db()
    orientador = db.execute("SELECT * FROM orientadores WHERE token = ?", (token,)).fetchone()
    candidato = db.execute("SELECT * FROM candidatos WHERE id = ?", (candidate_id,)).fetchone()
    if not orientador or not candidato:
        return "Acesso inválido.", 404

    avaliacao_existente = db.execute(
        "SELECT * FROM avaliacoes WHERE orientador_id = ? AND candidato_id = ?",
        (orientador['id'], candidate_id)
    ).fetchone()

    if request.method == 'POST':
        valores = {}
        if orientador['avalia_curriculo']:
            valores["s2_1"] = int(request.form["s2_1"])
            valores["s2_2"] = int(request.form["s2_2"])
        if orientador['avalia_entrevista']:
            valores["s3_1"] = int(request.form["s3_1"])
            valores["s3_2"] = int(request.form["s3_2"])
        if orientador['avalia_afinidade']:
            valores["s4_1"] = int(request.form["s4_1"])
            valores["s4_2"] = int(request.form["s4_2"])
        
        if not valores:
            flash("Este avaliador não possui atribuições para submeter uma avaliação.", "warning")
            return redirect(url_for('avaliar_home', token=token))

        if avaliacao_existente:
            set_clause = ', '.join([f"{col} = ?" for col in valores.keys()])
            query = f"UPDATE avaliacoes SET {set_clause} WHERE id = ?"
            db.execute(query, list(valores.values()) + [avaliacao_existente['id']])
            flash(f"Avaliação para {candidato['nome']} atualizada com sucesso!", "success")
        else:
            valores['orientador_id'] = orientador['id']
            valores['candidato_id'] = candidate_id
            cols = ', '.join(valores.keys())
            placeholders = ', '.join(['?'] * len(valores))
            query = f"INSERT INTO avaliacoes ({cols}) VALUES ({placeholders})"
            db.execute(query, list(valores.values()))
            flash(f"Avaliação para {candidato['nome']} enviada com sucesso!", "success")

        db.commit()
        return redirect(url_for('avaliar_home', token=token))

    return render_template_string(
        TPL_FORM_AVALIACAO,
        orientador=orientador,
        candidato=candidato,
        questionario=QUESTIONARIO_ESTRUTURA,
        avaliacao_existente=avaliacao_existente
    )

# --- 6. PONTO DE ENTRADA DA APLICAÇÃO ---
if __name__ == '__main__':
    # Nota: Antes de executar pela primeira vez, é necessário criar a base de dados.
    # Execute o comando 'flask init-db' no terminal, no diretório do projeto.
    # É preciso ter a variável de ambiente FLASK_APP="nome_do_arquivo.py" definida.
    app.run(debug=True)