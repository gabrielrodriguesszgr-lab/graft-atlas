# Auditoria Reversa: Graphify vs. CodeGraph e Arquitetura do Graft Atlas

## 1. Brechas e Deficiências Identificadas (Auditoria Reversa)

### Graphify (`Graphify-Labs/graphify`)
- **Pontos Fortes:**
  - Resolução de imports e chamadas em Python de alta precisão.
  - Separação explícita entre arestas `EXTRACTED` e `INFERRED`.
  - Comandos de grafo (`affected`, `path`, `explain`, `god-nodes`).
- **Deficiências (Retiradas no Graft):**
  1. **Mutação silenciosa de ambiente global:** Qualquer execução do CLI verifica e sobrescreve automaticamente skills instaladas em `~/.claude/skills/graphify` (a menos que `GRAPHIFY_NO_AUTO_REFRESH=1` esteja definido).
  2. **Poluição de grafo com nós de documentação/markdown:** Comandos de impacto (`affected`) misturam chamadas reais de código com dezenas de referências textuais em arquivos `.md` (`skill-*.md`), inflando o consumo de tokens.
  3. **Busca em linguagem natural (`query`) ruidosa e sem código-fonte:** Faz expansão BFS cega de 2 saltos sobre correspondências de palavras-chave, estourando o orçamento de 2.000 tokens com centenas de nós periféricos e **não entrega o código-fonte**, obrigando uma segunda chamada de leitura (`Read`).
  4. **Falha de desambiguação (`path`/`explain`):** Aborta com código de erro 3 quando dois símbolos têm o mesmo nome em arquivos diferentes, em vez de priorizar o nó mais central (PageRank) e listar alternativas.
  5. **Artefatos pesados no repositório:** Gera `graph.json` (~4,1 MB) e `graph.html` (~4,3 MB) dentro da árvore do projeto mesmo para consultas rápidas.

### CodeGraph (`colbymchenry/codegraph`)
- **Pontos Fortes:**
  - Persistência enxuta em SQLite com modo WAL e sincronização incremental.
  - Retorno de código-fonte real com numeração de linhas (`node`, `explore`) mais *blast radius* e detecção de testes na mesma resposta.
- **Deficiências (Retiradas no Graft):**
  1. **Telemetria *opt-out* e checagem de versão em background:** Coleta e envia estatísticas de uso por padrão (exige `DO_NOT_TRACK=1` ou `CODEGRAPH_TELEMETRY=0`) e consulta o GitHub periodicamente.
  2. **Instalador intrusivo:** `codegraph install` injeta blocos em `CLAUDE.md`, `AGENTS.md`, `GEMINI.md` e configurações globais de MCP.
  3. **Baixa precisão em perguntas abertas (`explore`):** Sem ponderação IDF/BM25 combinada a docstrings e assinaturas, perguntas conceituais retornam ~3.500–4.300 tokens de arquivos irrelevantes (1/4 de acerto na função-alvo no benchmark inicial).
  4. **Dependência de resolução estrita de caminho de pacote:** Quando um subdiretório é indexado fora da raiz do pacote Python, imports absolutos deixam de resolver chamadas entre arquivos.
  5. **Inadequado para microprojetos:** Sempre inicializa diretório `.codegraph/` e banco SQLite em disco, mesmo em projetos de poucos arquivos.

---

## 2. Como o Graft Atlas Combina o Melhor dos Dois (e Supera Ambos)

| Dimensão | Graphify | CodeGraph | **Graft Atlas** |
|---|---|---|---|
| **Privacidade & Efeitos Colaterais** | Auto-atualiza `~/.claude` | Telemetria opt-out + update check | **Zero rede, zero telemetria, zero escrita fora de `.graft/`** |
| **Dependências Externas** | Dezenas (tree-sitter, networkx, etc.) | Node.js + pacotes npm + WASM | **Zero (Python 3 stdlib puro: `ast`, `sqlite3`, `tokenize`)** |
| **Auto-Adaptação por Porte** | Fixo (`graphify-out/`) | Fixo (`.codegraph/codegraph.db`) | **3 Tiers Automáticos (`tiny` em RAM, `standard` SQLite WAL, `large` multiprocess)** |
| **Resolução de Chamadas Python** | Alta (mas polui com `.md`) | Quebra se raiz do pacote variar | **Import resolver por sufixo + AST em qualquer profundidade (inclui lazy imports)** |
| **Busca NL (`ask`)** | Só metadados (BFS truncado) | Código de arquivos errados (~4k tok) | **BM25/IDF sobre (nome + caminho + docstring + assinatura) + PageRank + cabeçalho do código + indicador de confiança (`strong`/`medium`/`weak`)** |
| **Orçamento de Tokens** | Fixo em `query` | Sem teto estrito em `explore` | **`--budget` estrito em todos os comandos + dica acionável de refinamento** |
| **Impacto & Testes** | Sem separação de testes | Mostra testes no `explore` | **`impact` e `changes` (git diff) separam código produtivo por profundidade e listam `tests to run`** |
