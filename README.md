# Sistema de Apoio à Seleção e Alocação de Candidatos (SASAC)

Aplicação web desenvolvida em **Flask** para apoiar processos de **seleção e alocação de candidatos** a vagas de orientação em programas acadêmicos.
O sistema permite cadastrar orientadores e candidatos, realizar avaliações padronizadas e gerar relatórios detalhados com os resultados finais de alocação.

---

## Funcionalidades

* **Painel Administrativo**

  * Gestão de orientadores e candidatos
  * Configuração dos pesos das avaliações e bônus
  * Inicialização e reset da base de dados

* **Portal de Avaliação**

  * Cada orientador possui um link único protegido por *token*
  * Submissão de avaliações com base em questionário padronizado

* **Algoritmo de Alocação**

  * Cálculo de índices de preparo e afinidade
  * Consideração de preferências dos candidatos
  * Alocação automática dos candidatos às vagas disponíveis

* **Relatórios**

  * Relatório detalhado com candidatos alocados, não alocados e não avaliados
  * Transparência do cálculo de pontuação
  * Visualização dos pesos usados no processo

---

## Tecnologias Utilizadas

* [Python 3](https://www.python.org/)
* [Flask](https://flask.palletsprojects.com/)
* [SQLite3](https://www.sqlite.org/index.html)
* [Bootstrap 4](https://getbootstrap.com/)

---

## Instalação e Execução

### 1. Clonar o repositório

```bash
git clone https://github.com/seu-usuario/seu-repo.git
cd seu-repo
```

### 2. Criar e ativar ambiente virtual (opcional, mas recomendado)

```bash
python3 -m venv venv
source venv/bin/activate   # Linux/Mac
venv\Scripts\activate      # Windows
```

### 3. Instalar dependências

```bash
pip install flask click
```

### 4. Inicializar a base de dados

Antes de rodar a aplicação pela primeira vez:

```bash
export FLASK_APP=flask_app.py   # Linux/Mac
set FLASK_APP=flask_app.py      # Windows PowerShell

flask init-db
```

### 5. Executar a aplicação

```bash
flask run
```

Acesse em: [http://localhost:5000](http://localhost:5000)

---

## Acesso Administrativo

* O login administrativo utiliza a senha definida em:

  ```python
  app.config['ADMIN_PASSWORD'] = '42'
  ```

  > **Atenção:** Em produção, use variáveis de ambiente para armazenar a senha.

---

## Estrutura da Avaliação

O questionário é dividido em três seções:

1. **Avaliação do Currículo**

   * Desempenho acadêmico e formação
   * Experiência prévia em pesquisa

2. **Avaliação da Entrevista**

   * Clareza na comunicação
   * Motivação do candidato

3. **Avaliação da Afinidade**

   * Alinhamento de interesses
   * Potencial de desenvolvimento

---

## Comandos Úteis

* **Inicializar DB**

  ```bash
  flask init-db
  ```

* **Resetar DB**

  * Opção disponível no **Painel Administrativo**

---

## 📌 Observações

* Este projeto roda localmente com SQLite.
* Para ambientes de produção, recomenda-se:

  * Uso de servidor WSGI (ex.: Gunicorn)
  * Banco de dados mais robusto (ex.: PostgreSQL)
  * Configurações seguras para senhas e tokens
