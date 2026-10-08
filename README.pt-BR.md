# croqui

**Português** · [English](README.md)

Mapas interativos, em traço de rascunho, de uma API **e do banco que ela toca** — gerados do código-fonte, servidos em localhost, guardados no projeto. Ponha um PRD e alguns diagramas mermaid ao lado e o `croqui build` te dá um único HTML que carrega o mapa, os requisitos e os desenhos juntos.

```
endpoints            services            repositories        database
┌──────────────┐     ┌────────────┐      ┌───────────────┐   ┌──────────────┐
│ ▸ v2 — Orders│────▶│OrderService│─────▶│OrderRepository│──▶│ crm.tb_orders│
│ 16 endpoints │     └────────────┘      └───────────────┘   │ tb_order_item│
│ GET 8 POS 5  │                                             └──────────────┘
└──────────────┘
```

Não é um retrato da sua árvore de pastas — é o mapa de qual endpoint chega em qual tabela, com a operação SQL em cada aresta e um `arquivo:linha` para cada afirmação.

## Por quê

Documentação de API gerada para na fronteira do HTTP. O `openapi.json` diz que `POST /orders` recebe um `OrderDTO`; ele não diz que essa chamada grava em `tb_orders`, `tb_order_item` e `tb_order_audit`. Essa segunda metade é justamente a que você precisa ao revisar uma migration, integrar alguém no time ou explicar um bug.

O Excalidraw desenha isso lindamente, à mão, uma vez — e depois envelhece. O croqui regenera do código e continua parecendo feito à mão.

## Instalação

Ainda não está no PyPI (o nome não é de ninguém — veja *[Publicando](RELEASING.md)*),
então instale a partir de um checkout:

```sh
git clone https://github.com/guimileib/croqui
pipx install ./croqui          # a CLI no seu PATH, em uma venv própria
```

O `pipx` é a ferramenta certa aqui mesmo o croqui tendo forma de biblioteca: é um
comando que você roda *contra* outros projetos, então ele não pertence à virtualenv de
nenhum deles. Mexendo no croqui em si? `pipx install --editable ./croqui` — o
visualizador é lido do seu checkout, então uma mudança no `app.js` aparece no próximo
reload.

Sem `pipx`, e sem instalar absolutamente nada — o croqui tem **zero dependências de
runtime**, então qualquer Python 3.10+ roda ele direto do checkout:

```sh
PYTHONPATH=/caminho/do/croqui python3 -m croqui.cli scan .
```

Biblioteca padrão pura. Nada para compilar, nenhum CDN, nenhum motor de navegador.

### Atualizar

```sh
croqui update          # de onde quer que você tenha instalado
croqui update --check  # diz o que rodaria, sem mudar nada
```

A ideia é não ter de lembrar. Quando sai uma versão nova você está três diretórios longe
do checkout, e o comando certo depende de como você instalou meses atrás. O
`croqui update` descobre isso: `pipx upgrade` para uma instalação com pipx, um
`git pull --ff-only` mais reinstalação para um checkout, `pip install --upgrade croqui`
para um pip comum. Ele imprime cada comando antes de rodar, e não chuta quando não
consegue saber.

## Uso

```sh
croqui scan       # analisa o código   -> .croqui/graph.json
croqui serve      # abre o mapa editável em localhost:7777
croqui build      # um .html autocontido — mapa + PRD + diagramas, sem servidor
croqui prd        # escreve o PRD a partir do mapa, com o modelo que você tiver
croqui context    # o mesmo mapa em texto, para o contexto de um LLM
croqui mcp        # roda como servidor MCP, para um agente ler o mapa e escrever documentos
croqui engines    # com quais modelos esta máquina consegue falar
croqui update     # atualiza o próprio croqui
```

Rode da raiz do projeto, ou passe um caminho: `croqui scan ~/code/minha-api`.

A primeira varredura também cria `.croqui/prd/` e `.croqui/uml/`, então o mapa já nasce com uma aba de
documento de cada lado — veja *[Um arquivo, três abas](#um-arquivo-três-abas)*.
`croqui scan --no-docs` se você preferir que não.

Re-escanear um projeto já mapeado preserva o seu trabalho: o `scan` re-aponta o `layout.json` para tudo que o código renomeou e relata o que não conseguiu casar. Veja *[Sobreviver a uma mudança no código](#sobreviver-a-uma-mudança-no-código)*.

A spec é encontrada sozinha nos lugares de sempre (`contracts/openapi.json`, `openapi.json`, `docs/`, `static/`, `swagger.json`, …). Aponte para ela explicitamente — inclusive para uma aplicação rodando — quando ela morar em outro lugar:

```sh
croqui scan --openapi api/spec/openapi.json
croqui scan --openapi http://localhost:8000/openapi.json
```

Sem spec o mapa ainda se monta a partir do código; você só perde a documentação.

## O que aparece no seu projeto

```
.croqui/
├── graph.json       # o mapa. versione — ele diffa limpo num PR
├── layout.json      # seu arranjo, desenhos, relações e vista. versione também
├── images/          # imagens que você colou no mapa. versione também
├── prd/             # a aba PRD. é prosa — versione
│   └── 01-visao-geral.md   # um PRD em branco para preencher. é seu; o croqui nunca reescreve
├── uml/             # a aba UML. é prosa — versione
│   └── relacoes.mmd        # o mapa como diagrama mermaid, redesenhado a cada scan
├── croqui.html      # snapshot autocontido opcional (croqui build)
├── graph.prev.json  # o grafo com que seu layout está em dia. gitignore
└── cache.json       # cache do LLM. gitignore
```

Tudo que o croqui escreve mora dentro do `.croqui/`, as duas pastas de documento
incluídas: uma ferramenta que mapeia o repo dos outros não deveria espalhar pasta pela
raiz dele. Mas elas só *nascem* lá — um `prd/` na raiz do projeto, ou um `docs/prd` que
o repo já mantém, é lido igualzinho e nunca é movido. Lá dentro dois arquivos são
descartáveis e o resto não é, então dê `.gitignore` nesses dois e não na pasta inteira:
um `.croqui/` no atacado leva o seu PRD junto. Veja *[O que a primeira varredura
põe lá](#o-que-a-primeira-varredura-põe-lá)*.

O `graph.json` ser um artefato versionado é o ponto: um PR que move um endpoint para uma tabela nova aparece como diff no mapa, não só no código.

### Editar é explícito

Mover uma caixa, ocultar um nó, desenhar uma forma, escrever um texto — tudo isso fica na memória até você apertar **salvar alterações** (ou Ctrl+S). O botão mostra `salvo` quando o mapa bate com o arquivo e `salvar alterações •` quando não bate, e esse estado é uma comparação real contra o que está em disco: desfaça até voltar ao layout salvo e o botão se cala de novo. Fechar a aba com edições pendentes avisa antes.

Salvar registra **todas as caixas na tela**, não só as que você arrastou. Isso é deliberado — veja *[Sobreviver a uma mudança no código](#sobreviver-a-uma-mudança-no-código)* — e é o que faz a próxima varredura ser aditiva em vez de um rearranjo.

`Ctrl+Z` volta por movimentos, ocultações, desenhos, relações e *limpar edições* igualmente (100 passos). Dentro de uma caixa de texto ele continua sendo o desfazer de texto do navegador, como se espera.

## O visualizador

Cada traço da página — o mapa gerado e as formas que você desenha em cima — sai de uma única função de trepidação com semente, não de uma biblioteca de desenho. É por isso que o visualizador inteiro tem alguns KB e renderiza 120 nós na hora. O runtime do próprio Excalidraw é muito mais pesado que o diagrama que ele desenha.

| | |
|---|---|
| **clique num grupo** | expande os endpoints dele (uma API de 70 endpoints é ilegível plana) |
| **clique num nó** | painel lateral: a documentação completa do endpoint, parâmetros, respostas, `arquivo:linha`, operações SQL, origem do DDL |
| **clique numa tabela** | o mesmo painel, mais **as colunas dela** — nome, tipo, `PK`, `not null`, e uma chave estrangeira que pula para a tabela apontada. Lidas do `CREATE TABLE` ou do modelo ORM, nunca chutadas |
| **⇤ / ⇥** | alarga o painel para documentação longa |
| **realce** | escurece o resto do mapa. **Desligado por padrão.** Com ele ligado, o ponteiro realça o que estiver embaixo — e **clicar num quadro fixa o realce nele**, então ele sobrevive a você levar o cursor embora para ler o painel. Passar por cima de outro pré-visualiza aquela cadeia; ao sair, volta para o fixado; fechar o painel solta |
| **arrastar** uma caixa ou desenho | move |
| **arrastar no papel** | desenha um retângulo de seleção — caixas e desenhos acendem enquanto ele cresce. Arrastar qualquer item selecionado move todos num único passo de desfazer. **Esc** ou um clique no papel limpa |
| **shift+clique** / **shift+arrastar** | soma à seleção em vez de recomeçar |
| **Ctrl+A** | seleciona todas as caixas e desenhos do mapa |
| **espaço + arrastar**, ou o botão do meio | navega, com a mão de grab — de qualquer lugar, até começando em cima de uma caixa |
| **Ctrl+Z / Ctrl+Shift+Z** | desfaz / refaz — até 100 passos |
| **Ctrl+S** ou **salvar alterações** | escreve o `layout.json` — o arranjo que você vê, mais a vista. Nada é escrito antes disso |
| **órfãs** | aparece só quando o layout nomeia nós que o código não tem mais; lista e sabe descartar |
| **duplo-clique no papel** | uma caixa de texto, ali mesmo — veja *Desenhar no mapa* abaixo |
| **Ctrl+V** | cola no mapa, onde o cursor está: uma imagem, um texto, ou uma seleção do Excalidraw — com formas e setas |
| **abas** | `mapa` · `PRD` · `UML` — veja *[Um arquivo, três abas](#um-arquivo-três-abas)*. **Esc** ou **m** volta ao mapa |
| **isolar esta cadeia** | mostra só o caminho de um endpoint até o banco |
| **busca / chips de verbo** | escurece tudo que não casa |
| **roda / shift+roda** | zoom / pan |
| **URL** | `…/croqui.html#node=route:GET:/v2/crm/orders` abre direto num endpoint |

Claro e escuro seguem o sistema. `✍` troca para uma fonte de escrita à mão onde o sistema tiver uma.

## Um arquivo, três abas

O mapa gerado responde *o que o código faz*. Duas perguntas que ele não responde são
*por quê* e *como a gente quis que funcionasse* — e essas moram num PRD e em diagramas
que alguém desenhou de propósito. Ponha os dois ao lado do mapa e o `croqui build`
produz um HTML só, que carrega os três:

```
.croqui/prd/
  01-visao.md             ->  aba PRD
  02-fora-de-escopo.md
.croqui/uml/
  fluxo-do-pedido.mmd     ->  aba UML
  sequencia-checkout.mmd
```

As duas pastas são procuradas primeiro dentro do `.croqui/` e depois na raiz do projeto
(`prd/`, `docs/prd` e `PRD` também valem), então ponha onde o seu repo já
guarda prosa. Subpastas são lidas. As abas não são editores: fora os dois arquivos
descritos em *[O que a primeira varredura põe lá](#o-que-a-primeira-varredura-põe-lá)*,
o croqui só **lê** essa prosa, e nunca reescreve um PRD.

A tira de abas só mostra aba que tem algo atrás: um projeto sem PRD nenhum continua
exatamente como era. Cada reload relê o diretório, então editar um documento e apertar
reload é o fluxo inteiro — não precisa re-escanear.

### O que a primeira varredura põe lá

Você não precisa criar as pastas. O `croqui scan` cria as que faltam e deixa um
documento em cada uma para que as duas abas abram com alguma coisa dentro:

```
.croqui/prd/01-visao-geral.md   # um PRD em branco, com os números deste projeto dentro
.croqui/uml/relacoes.mmd        # o mapa varrido escrito em mermaid — a relação em si
```

Elas nascem **dentro do `.croqui/`**, ao lado do `graph.json` e do `layout.json`, pelo
mesmo motivo que o croqui não escreve mais nada na raiz: ele é hóspede no repo dos
outros. Cada pasta é decidida por conta própria, então um repo com `docs/prd` e nenhum
diagrama recebe a semente em `docs/prd` e um `.croqui/uml` novo.

De quem é cada um é o desenho inteiro da coisa:

| | |
|---|---|
| `01-visao-geral.md` | **escrito uma vez.** É seu no instante em que nasce — o croqui nunca mais lê, compara ou reescreve. Um projeto que já tem um PRD em qualquer lugar onde o croqui procura não ganha semente nenhuma |
| `relacoes.mmd` | **derivado.** Cada `croqui scan` redesenha do código, então o diagrama não consegue divergir do mapa. A segunda linha dele diz isso |

```
%% croqui:gerado — reescrito a cada `croqui scan` enquanto esta linha existir.
```

Apague essa linha e o arquivo vira seu: o croqui relata que não tocou e nunca mais
escreve nele. Renomeie, e você fica com os dois — a sua versão e um `relacoes.mmd` novo
ao lado. `croqui scan --no-docs` pula tudo isso.

Se o repo já guarda prosa em algum lugar onde o croqui procura — `docs/prd`, um `uml/`
na raiz — a semente cai **lá**, e nada é migrado para dentro do `.croqui/`.

Acima de 150 nós o diagrama vira uma amostra em vez do grafo inteiro, distribuída entre
endpoints, serviços, repositórios e tabelas. Ele diz num comentário `%%` quantos ficaram
de fora, e a aba do mapa continua com todos.

### A aba PRD

A aba abre numa **galeria de cards**, um por documento — título, as primeiras linhas, o
arquivo e se é Markdown ou HTML. Clique num card para ler; **← todos os PRDs** volta
para a galeria.

Markdown, renderizado pelo mesmo renderizador de ~90 linhas do painel de endpoint:
títulos, listas, tabelas, citações, código, **negrito**, `código` e links http(s).
`.md`, `.markdown` e `.txt`. O título do documento vem do primeiro cabeçalho, caindo
para um nome de arquivo arrumado.

**`.html` / `.htm`** aparece como a página que é, num frame isolado: estilos e scripts
rodam, mas ela não alcança o visualizador, o seu layout nem o servidor. O título é o
`<title>` (ou o primeiro `<h1>`). Com `croqui serve` ela é carregada por URL, então
imagens, CSS e scripts ligados por caminho **relativo** — `<img src="assets/tela.png">`
ao lado da página — carregam como em qualquer servidor web; só arquivos dentro das pastas
de PRD são servidos. O `croqui build` embute essas imagens e folhas de estilo locais na
página como data URLs, para o bundle levá-las junto.

Um bloco ```` ```mermaid ```` dentro de um PRD é **desenhado**, não listado — então o
diagrama que explica um requisito fica no parágrafo que enuncia esse requisito. Qualquer
outro bloco cercado continua sendo listagem de código. Um diagrama embutido preenche a
coluna de texto e pode crescer além do tamanho natural para isso, com um teto para um
desenho de três nós não virar cartaz; diagrama que você quer **ler** é na aba UML, que
é um palco.

### A aba UML

`.mmd`, `.mermaid` ou `.md` com fonte mermaid dentro. A primeira varredura já deixou o
`relacoes.mmd` lá; ponha os seus ao lado — peça um diagrama do seu PRD a um LLM, jogue o
arquivo na mesma pasta, recarregue.

**A aba é um palco, não uma página.** O diagrama toma o painel inteiro e ganha zoom e
pan próprios: **roda** aproxima em torno do cursor, **espaço + arrastar** (ou o botão do
meio) navega, **arrastar** no papel seleciona nós — acendendo as relações deles e
esmaecendo o resto — **arrastar um nó** move a seleção, **duplo-clique**
ou **f** ajusta, e a leitura no canto também é o botão de ajustar. Ele abre ajustado,
então um grafo largo chega inteiro em vez de espremido numa coluna de texto com os
rótulos ilegíveis — que era justamente o que esta aba existia para evitar.

Zoom e pan movem o `viewBox`, nunca um transform na tinta. O tremido do traço está
assado dentro do path, então escalar os paths escalaria o lápis junto; mover a janela
faz com que 400% seja desenhado pela mesma mão que 100%.

**O croqui desenha mermaid ele mesmo — ele não carrega o mermaid.js.** Esse é o ponto: o
runtime do mermaid é uma ordem de grandeza maior que este visualizador inteiro, e um
documento cujas três abas são desenhadas em três linguagens visuais diferentes se lê como
três documentos. Seu diagrama sai no mesmo traço trêmulo de lápis do mapa gerado, das
mesmas `sketchRect`/`sketchArrow` que desenham todo o resto.

O que ele desenha hoje:

| | |
|---|---|
| `graph` / `flowchart` | `TB` `TD` `BT` `LR` `RL`; formas de nó `[]` `()` `([])` `[[]]` `[()]` `(())` `{}` `{{}}` `[//]`; arestas `-->` `---` `-.->` `-.-` `==>` `===` `<-->`; rótulos como `--\>\|texto\|` ou `-- texto -->`; cadeias `A --> B --> C`; leque `A & B --> C & D`; `subgraph … end` com moldura e rótulo; rótulos entre aspas; `<br/>`; comentários `%%` |
| `sequenceDiagram` | `participant` / `actor`, apelidos com `as`, `->>` `-->>` `->` `-->` `--x`, rótulos de mensagem, `Note over/left of/right of` |
| qualquer outro | o tipo do diagrama é nomeado e **sua fonte aparece na íntegra** — nunca um painel em branco |

`classDef`, `style`, `linkStyle` e `click` são lidos e ignorados: o traço de rascunho é
do croqui, e um diagrama que honra metade de uma diretiva de cor é pior que um que
claramente não honra nenhuma. `loop` / `alt` / `opt` / `par` num diagrama de sequência
ainda não viram faixas desenhadas; o croqui avisa acima do diagrama e desenha todas as
mensagens assim mesmo.

O layout é do croqui, não do mermaid: ranking por caminho mais longo, ordenação por
baricentro dentro de cada rank, membros de um subgraph mantidos juntos. Arestas que
fecham um ciclo — um retry apontando de volta para o que ele repete — são desenhadas mas
excluídas do ranking, porque um ciclo num passo de caminho mais longo não termina, e
pará-lo em silêncio num limite de passadas põe nós na faixa errada em vez de falhar alto.

## croqui para uma IA

O visualizador responde às perguntas de uma pessoa sendo um desenho. Um modelo não
consegue olhar para um desenho — ele precisa do mesmo conhecimento em texto, curto o
bastante para caber numa janela de contexto ao lado do trabalho de verdade. Dois comandos
fazem isso, e são os mesmos fatos duas vezes:

```sh
croqui context                       # markdown, para colar em qualquer LLM
croqui context --json                # o mesmo, estruturado
croqui context --node /v1/orders     # um endpoint ou tabela, inteiro
croqui context --full                # inclui a doc de cada endpoint e o corpo dos documentos
```

O digest nomeia o projeto, lista cada endpoint com a cadeia atrás dele
(`GET /v1/orders → OrderService → OrderRepository -[grava: insert]-> tb_orders`), as
tabelas com suas colunas, os serviços e repositórios, e um índice dos documentos PRD e
UML. Ele é intencionalmente incompleto — descrições são cortadas, cadeias têm teto —
porque um contexto que não cabe é um contexto que ninguém usa. Uma API pequena sai com
uns 2 KB.

### A metade que faz disso um framework

Ler é só uma direção. A última seção de todo digest diz ao modelo **onde ele pode
escrever**:

```
## como escrever aqui
- PRD → .croqui/prd/*.md   — markdown; o primeiro título nomeia o documento na aba PRD
- UML → .croqui/uml/*.mmd  — mermaid; flowchart e sequenceDiagram são desenhados
- ⚠ .croqui/uml/relacoes.mmd é gerado e reescrito a cada scan — escreva um arquivo novo ao lado
- ⚠ .croqui/graph.json é derivado do código — mude o código e re-escaneie
```

Assim o ciclo fecha: o modelo lê o mapa, escreve um PRD ou um diagrama nas pastas que o
visualizador já renderiza, e a próxima pessoa abre o `croqui serve` e vê aquilo numa aba.
Nada disso é específico do Claude — são arquivos numa pasta.

### MCP

Para o Claude Code e o Claude Desktop, a mesma coisa sem o copiar e colar:

```sh
claude mcp add croqui -- croqui mcp
```

| ferramenta | |
|---|---|
| `croqui_map` | o digest acima, em markdown ou JSON |
| `croqui_node` | um endpoint ou tabela por inteiro — aceita um id, um caminho cru ou um nome de tabela |
| `croqui_docs` | lista os documentos PRD e UML, ou lê um deles |
| `croqui_write_doc` | escreve um documento na pasta de PRD ou de UML |
| `croqui_scan` | relê o código e reconstrói o mapa |

O `croqui mcp` é uma casca fina: toda leitura passa pelo mesmo código de `context` que a
CLI usa, então existe uma definição só do que o mapa diz. JSON-RPC no stdio, biblioteca
padrão apenas — sem servidor, sem dependência, nada sai da máquina.

A escrita é cercada, não confiada. O nome do documento tem de ser um nome de arquivo
simples (sem diretórios, sem arquivos ocultos), a extensão tem de bater com a seção, e
`relacoes.mmd` é recusado na hora, com explicação — o croqui regenera aquele arquivo,
então o que fosse escrito ali se perderia na próxima varredura. Toda recusa volta como
**erro** de ferramenta, não como texto: uma recusa que parece sucesso é um modelo que
acredita ter salvo o seu trabalho.

## Desenhar no mapa

Um mapa gerado responde *o que o código faz*. As coisas que você precisa escrever por
cima — esta coluna inteira é legado, aquela fila entra no próximo trimestre, estes dois
serviços combinaram um contrato que nada no fonte declara — não estão no código, então
nenhum scanner vai produzir isso um dia. A régua de ferramentas da esquerda é para elas.

| Ferramenta | | |
|---|---|---|
| formas | `r` `o` `d` `t` `h` `c` `n` | retângulo, elipse, losango, triângulo, hexágono, cilindro, nuvem |
| setas | `a` `l` | seta e linha simples, com ponta numa extremidade, nas duas, ou em nenhuma — uma ponta desenhada dentro de uma caixa fica ligada a ela |
| caixa de texto | `x`, ou **duplo-clique** no papel | redimensionável, sem borda, em qualquer cor e tamanho |
| relação | `e` | uma aresta nova entre dois nós existentes, com o rótulo que você quiser |
| imagem | **Ctrl+V** | uma imagem colada — um print, uma foto de quadro branco, um painel do Grafana |
| um desenho do Excalidraw | **Ctrl+V** | caixas, losangos, elipses, setas, linhas, texto e imagens, no arranjo em que foram copiados |

Arraste para dimensionar, ou clique uma vez para um padrão razoável. Tudo é desenhado com
a mesma trepidação com semente do mapa, então o que você acrescenta não parece colado por cima.

- **duplo-clique** é o gesto único de escrever: no papel nu ele solta uma caixa de texto onde você clicou, num desenho ele põe o cursor dentro daquele desenho. Uma caixa de texto cresce para baixo conforme você escreve
- **arraste a própria escrita** para mover uma caixa de texto, mesmo com o cursor dentro dela — um clique posiciona o cursor, um arrasto move o rótulo
- uma caixa de texto com **preenchida** desligado é só a escrita: sem fundo, e sem moldura também assim que tem texto dentro (uma vazia mantém um contorno tracejado fraco, ou você nunca a encontraria)
- **arraste as alças** para redimensionar; setas ganham uma alça por ponta. Solte uma ponta **dentro de uma caixa** — um nó, ou qualquer desenho seu — e ela fica ligada àquela caixa: a ponta encosta na borda e a acompanha para onde ela for. Uma alça cheia é uma ponta ligada; **Ctrl** ao soltar deixa a ponta livre; arrastar a seta inteira solta as duas pontas, e onde ela cair decide de novo
- **Del** apaga o que estiver selecionado, **Esc** devolve a ferramenta de ponteiro
- **A− / A+** no inspetor define o tamanho das letras, de 9 a 44px — no desenho selecionado, ou, sem nada selecionado, no próximo que você desenhar
- o painelzinho ao lado da régua define cor, preenchimento, tracejado, pontas de seta, se a forma fica **atrás** do mapa (o padrão, para um retângulo servir de moldura em volta de uma cadeia) ou **à frente**, e a qual nó ela está **presa**

### Imagens coladas

Cole um print direto no mapa com **Ctrl+V** e ele cai onde o cursor está, na proporção
original, reduzido para caber se for grande. Ele se comporta como qualquer desenho:
arraste, redimensione pelas alças, mande para trás do mapa, prenda a um nó, **Del** para
remover, **Ctrl+Z** para desfazer a colagem. Ele chega *à frente* do mapa e não atrás,
porque uma imagem opaca atrás do diagrama é uma imagem em cima da qual as caixas dos nós
se sentam — aperte **à frente** para empurrar para trás se o que você queria era um
fundo. PNG, JPEG, GIF e WebP, até 12 MB.

Os bytes vão para `.croqui/images/`, nomeados pelo hash do próprio conteúdo, e o
`layout.json` guarda só esse nome:

```json
{ "id": "sh-…", "type": "image", "x": 470, "y": -300, "w": 520, "h": 260,
  "src": "a1b2c3d4e5f60718.png", "front": true }
```

Essa separação é o ponto inteiro. Um print embutido como base64 seria um megabyte numa
linha de um arquivo que deveria ser revisável num PR, e empurraria o `layout.json` para
perto do limite de tamanho que o visualizador se recusa a salvar. Nomear o arquivo mantém
o layout diffável, faz colar o mesmo print duas vezes custar um arquivo só, e permite que
o `croqui build` ainda embuta tudo num HTML autocontido que você manda por e-mail.

Tirar uma imagem do mapa deixa o arquivo em `.croqui/images/` — o croqui nunca apaga um
por conta própria, então um desfazer ou um `Ctrl+Z` depois de salvar sempre tem os bytes
para onde voltar. Apague os que sobraram à mão se o diretório crescer.

### Texto colado e desenhos do Excalidraw

**Ctrl+V** com texto no clipboard solta uma caixa de texto no mapa, onde o cursor está,
do tamanho do que ela guarda. Dali em diante é uma caixa de texto comum: **A− / A+**
muda o tamanho das letras, as alças redimensionam a caixa, **Del** apaga, **Ctrl+Z**
desfaz a colagem.

Copiar elementos no **Excalidraw** põe o JSON do próprio Excalidraw no clipboard, então
um Ctrl+V cru despejaria uma página de JSON no seu mapa. O croqui lê esse JSON e redesenha
tudo com o lápis dele:

| no Excalidraw | no croqui |
|---|---|
| retângulo, losango, elipse | a mesma forma, no mesmo tamanho |
| seta, linha | seta ou linha, guardando a direção e em que pontas há cabeça — e a que caixa cada ponta estava ligada, quando essa caixa veio na mesma cópia |
| texto | uma caixa de texto, no tamanho de fonte que tinha, limitado aos 9–44px em que o croqui desenha |
| rótulo dentro de uma caixa | a escrita da própria caixa — não uma segunda forma por cima dela |
| rótulo numa seta | uma caixa de texto própria, onde o Excalidraw a tinha (as setas do croqui não guardam texto) |
| cor do traço | a mais próxima das oito cores do croqui; um traço neutro vira `ink`, que acompanha o tema |
| fundo, tracejado | **preenchida** e **tracejada** na forma |
| imagem (PNG, JPEG, GIF, WebP) | uma imagem, no mesmo lugar e tamanho — gravada em `.croqui/images/` como qualquer imagem colada; setas presas a ela continuam presas |

O arranjo é preservado — a cópia inteira se desloca em grupo até o seu cursor, na ordem em
que estava empilhada — e a colagem é um passo único de desfazer. O que cai em cima de um nó
se prende a ele como qualquer outro desenho.

Algumas coisas não atravessam, e o aviso diz quais apareceram (*8 desenhos colados —
fora: 1 rabisco*): **rabiscos à mão livre**, **molduras e embeds** e **imagens SVG** — o
croqui não tem equivalente para eles — e a **seta com cotovelos**, que chega como um traço
reto entre as duas pontas. A **rotação** também se perde: um retângulo inclinado entra
reto. Imagens precisam do `croqui serve` (vão para o disco); num `croqui.html` só de
leitura o resto da cópia cola normalmente e o aviso diz que as imagens ficaram de fora.

Colar com o cursor dentro de uma caixa de texto funciona como você espera: texto puro
entra no cursor, e uma cópia do Excalidraw entra como o que está escrito nela, não como
JSON.

### Relações que você desenha

A ferramenta `relação` liga dois nós: clique na origem, depois no destino — ou arraste entre eles. O resultado é uma aresta rotulada que se comporta como as geradas. Ela realça junto com o nó, conta como parte do *isolar esta cadeia*, e segue uma rota para dentro do grupo quando o grupo se fecha. O que ela nunca faz é influenciar o layout automático de colunas, então desenhar uma não rearranja o mapa embaixo de você.

Relações são guardadas como ids de nó no `layout.json`, então sobrevivem a uma nova varredura e até a um rename — veja *[Sobreviver a uma mudança no código](#sobreviver-a-uma-mudança-no-código)*. Uma presa a um *grupo fechado* pertence à caixa do grupo e some enquanto o grupo está expandido; expanda primeiro se você quer a relação no endpoint em si.

### Setas ligadas a uma caixa

Uma relação une dois *nós*. Uma seta é um desenho, e as pontas dela podem se ligar a qualquer coisa que tenha caixa: um nó, um retângulo que você desenhou em volta de uma cadeia, uma caixa de texto, um print colado. A regra cabe numa frase — **uma ponta que descansa dentro de uma caixa fica ligada a ela** — e vale sempre que você solta: depois de desenhar a seta, depois de arrastar uma ponta, depois de mover a seta inteira. A ponta para um pouco antes da borda, apontando para a outra ponta (ou para o meio da outra caixa, quando as duas estão ligadas), então uma seta entre duas caixas continua sendo uma seta entre duas caixas por mais que você as mova. Quando uma caixa está dentro de outra — um rótulo sobre um nó, um nó dentro de uma moldura — a menor ganha.

O inspetor diz a que a seta está ligada (*ligada: retângulo → OrderService*); apertar ali solta as duas pontas. Apagar um desenho solta as pontas que estavam ligadas a ele; a seta fica onde estava. Uma seta do Excalidraw que estava presa a uma caixa chega presa à cópia dessa caixa.

### Desenhos que pertencem a um nó

Uma anotação sobre um serviço tem de viajar com aquele serviço. Solte um desenho em cima de um nó e ele fica **preso a ele**: arraste o nó e o desenho vai junto, e ele continua no lugar certo depois de o código se mexer embaixo. O inspetor diz qual nó o segura — aperte para soltar, ou para prender um que foi desenhado no papel nu e depois arrastado para cima de um nó.

Um desenho que abrange **dois ou mais** nós fica no papel, porque isso é uma moldura em volta de uma cadeia e não um rótulo numa caixa. Nada muda na tinta de um jeito ou de outro; só de quem são as coordenadas que ela segue.

Desenhos e relações moram no `layout.json` junto com as suas posições, então diffam num PR como todo o resto:

```json
{
  "croqui_layout": 2,
  "positions": { "svc:OrderService": { "x": 426, "y": 100 } },
  "shapes": [
    { "id": "sh-…", "type": "cloud", "x": 640, "y": -120, "w": 190, "h": 120,
      "text": "fila nova, Q3", "color": "orange", "fill": true, "dash": false, "size": 13,
      "anchor": { "node": "svc:OrderService", "dx": 214, "dy": -220 } },
    { "id": "sh-…", "type": "image", "x": 470, "y": -300, "w": 520, "h": 260,
      "src": "a1b2c3d4e5f60718.png", "front": true },
    { "id": "sh-…", "type": "arrow", "x": 835, "y": -60, "w": -204, "h": 118,
      "color": "ink", "dash": false, "heads": "end",
      "from": { "shape": "sh-…" }, "to": { "node": "svc:OrderService" } }
  ],
  "links": [
    { "id": "ln-…", "from": "svc:OrderService", "to": "tbl:crm.tb_orders",
      "label": "compensação", "color": "red", "heads": "both", "dash": true }
  ],
  "view": {
    "expanded": ["orders"], "focus": null,
    "camera": { "x": -120, "y": 44, "k": 0.82 },
    "filters": { "query": "", "methods": ["GET"], "internal": false, "hover": false }
  }
}
```

`anchor` é o que prende um desenho a um nó; `from`/`to` são a que as pontas de uma seta estão ligadas, um nó ou outro desenho. Nos dois casos `x`/`y` (e `w`/`h`) ficam no arquivo como o último lugar resolvido, o plano B para quando aquela caixa estiver oculta ou não existir mais. Layouts escritos antes de existirem desenhos carregam um array `notes`; ele é lido de volta como caixas de texto no amarelo de post-it original, e escrito na forma nova no próximo salvamento.

## Sobreviver a uma mudança no código

O mapa só vale a pena arrumar se o arranjo sobreviver ao código que ele descreve. Três coisas diferentes costumavam zerá-lo, e cada uma é tratada de um jeito.

**Um endpoint novo movia tudo.** O layout automático ordena cada coluna pelo baricentro dos pais, então uma única rota nova re-ordena e re-centra tudo rio abaixo — e uma caixa que você nunca arrastou não tinha nada em disco segurando o lugar dela. Por isso salvar registra toda caixa visível. Um nó que o código acabou de ganhar começa onde o layout automático queria e desliza para baixo até parar de sobrepor qualquer coisa já salva: ele aparece ao lado dos vizinhos sem empurrá-los.

**Um rename órfãozava o seu trabalho.** Os ids dos nós são semânticos — `service:UserService`, `route:GET:/users` — que é exatamente por que um layout sobrevive a uma nova varredura, e exatamente por que renomear a classe renomeia o id. Posições, marcas de oculto, âncoras de desenho, pontas de seta e relações feitas à mão passavam então a apontar para um nome que ninguém atendia. O `croqui scan` agora lê o grafo anterior, descobre quais ids que sumiram são a mesma coisa com nome novo, e re-aponta o `layout.json`:

```
✓ wrote .croqui/graph.json
✓ layout.json follows the rename — 5 references re-pointed
  · route:GET:/users → route:GET:/api/users  (same handler, same file)
  · service:UserService → service:AccountService  (same class, same file)
! 1 layout reference with no matching node (kept in the file, invisible on the map)
  · repo:LegacyRepo — relation
```

Uma regra só dispara quando a resposta é inequívoca dos **dois** lados — um id que sumiu, um id que apareceu — porque um palpite errado move a sua anotação para o nó errado, o que é pior que perdê-la. O que ele casa: um handler renomeado ou com caminho novo no mesmo arquivo, um handler cujo arquivo mudou de lugar, uma classe de serviço ou repositório renomeada no lugar, uma tabela cujo schema mudou, e um grupo de endpoints que ainda segura a maioria dos seus endpoints. Passe `--no-migrate` para ter o relatório sem a reescrita.

O que ele não consegue casar fica **no arquivo**, nunca é apagado, e aparece tanto naquele relatório quanto como um chip **órfãs** no visualizador — porque no mapa a perda é invisível: uma relação para um nó que não existe mais simplesmente não é desenhada. O chip lista essas referências e sabe descartá-las de uma vez; um desenho mantém a tinta e o lugar, só a âncora morta se vai.

**O que uma varredura nunca toca.** A reescrita acima é a única coisa que o `croqui scan`
faz com o `layout.json`, e ela só reescreve *ids de nó*. Suas formas, sua escrita, os
tamanhos de fonte e as cores que você escolheu, imagens coladas e onde elas estão, quais
caixas você ocultou e a câmera passam intactos — e os arquivos de imagem em
`.croqui/images/` não estão no caminho da varredura de jeito nenhum. Se o layout nem
puder ser lido, a varredura relata e não escreve nada, pelo mesmo princípio que faz o
visualizador abrir em modo leitura em vez de sobrescrever um trabalho que não conseguiu ler.

**O estado de leitura zerava a cada reload.** Quais grupos estão abertos, onde a câmera está, quais filtros estão ligados e o que está isolado também persistem agora. São guardados duas vezes, de propósito: no `localStorage` a cada mudança, para que um reload logo depois do `croqui scan` abra o mapa exatamente como você deixou, sem apertar nada — e no `layout.json` num salvamento explícito, para que um colega que clonar o repo abra a vista que você versionou. Isso não é uma edição do mapa, então fica fora da comparação do `salvar alterações`: dar pan nunca acende aquele botão. Ctrl+S é como você versiona uma mudança só de vista.

## O painel de documentação

O OpenAPI diz que `description` é CommonMark, e as pessoas usam — as regras, os casos de borda, o "por quê" que nenhum diagrama transmite. O croqui renderiza isso por inteiro em vez de truncar, a partir dos campos padrão da spec, então funciona com qualquer documento OpenAPI 3 e não só com o do FastAPI:

| Campo da spec | No painel |
|---|---|
| `description` | markdown renderizado: títulos, listas, **negrito**, `código`, tabelas, citações, links |
| `summary` | a linha única embaixo do título |
| `parameters[]` | nome, local, obrigatoriedade, tipo, padrão, e a descrição de cada um |
| `requestBody` / `responses` | schema do corpo, e cada código de status com sua descrição |
| `operationId`, `tags`, `deprecated` | mostrados como metadados |

O markdown é renderizado por uma função de ~90 linhas, não por uma biblioteca — tudo é escapado como HTML *antes* de qualquer transformação rodar, e só links `http(s)` viram links, então uma spec hostil não consegue injetar script na página. Quando não há entrada na spec, a docstring do handler é usada no lugar e rotulada como tal.

## Como ele resolve a cadeia

Análise estática primeiro, LLM só como plano B. Num serviço FastAPI de 69 endpoints isso resolve o grafo inteiro com **zero chamadas de LLM**:

1. **`openapi.json`** — rotas, métodos, resumos, schemas com 100% de fidelidade.
2. **AST** — `@router.get` → `Depends(get_x_service)` → `XService` → `self.repo: XRepository` → `select(Model)` / `text("SELECT … FROM tb_x")` → tabela, com a operação.
3. **Verdade de referência para tabelas** — toda tabela tem de existir no DDL do repo (`CREATE TABLE`) ou num `__tablename__` de ORM. Este é o portão anti-alucinação: o que não estiver nesse conjunto é descartado, tenha sido proposto por um regex ou por um modelo.

O `croqui enrich` manda só os nós que o passo 2 não resolveu, como uma fatia de código cada, com cache por hash de conteúdo. Tabelas que ele devolve passam pelo mesmo portão e são desenhadas tracejadas, com `confidence: "llm"` — nunca misturadas em silêncio com arestas provadas.

```sh
croqui enrich --dry-run      # lista de trabalho + estimativa de tokens, não chama nada
croqui enrich                # o modelo que esta máquina tiver — veja abaixo
croqui enrich --engine http  # um específico, dos que o `croqui engines` lista
```

## Qual modelo

O croqui não tem chave de API própria e não tem opinião sobre de quem é o seu modelo.
Ele procura um que você já tem, nesta ordem, e o `croqui engines` mostra o que achou:

| | |
|---|---|
| **uma CLI de agente no PATH** | `claude`, `codex`, `gemini`, `cursor-agent`, `opencode`, `llm`, `ollama`. Já autenticadas — nada para configurar, nenhuma chave em lugar nenhum |
| **um endpoint compatível com OpenAI** | `CROQUI_LLM_BASE_URL` + `CROQUI_LLM_MODEL`, e `CROQUI_LLM_API_KEY` se ele pedir. Um formato só cobre OpenAI, Groq, Together, OpenRouter, Azure, vLLM, LM Studio e o `/v1` do Ollama |
| **o SDK da Anthropic** | se o `anthropic` estiver instalado e o `ANTHROPIC_API_KEY` definido |
| **qualquer outra coisa** | `CROQUI_LLM_CMD="minha-cli --prompt"` — o croqui põe o prompt como último argumento e lê o stdout |

```sh
croqui engines                      # o que esta máquina consegue, do melhor pro pior
export CROQUI_LLM_BASE_URL=https://api.groq.com/openai/v1
export CROQUI_LLM_API_KEY=...
export CROQUI_LLM_MODEL=llama-3.3-70b-versatile
croqui prd                          # e agora é um modelo na Groq escrevendo o PRD
```

`--engine auto` é o padrão em todo lugar e pega a primeira linha que funciona. A engine
HTTP é `urllib` e mais nada: o croqui continua instalando com zero dependências de
runtime, e isso não vai mudar por causa disto.

O `CROQUI_LLM_CMD` é a história de extensão inteira. Uma CLI de que o croqui nunca ouviu
falar está a uma variável de ambiente de distância, e o contrato é pequeno o suficiente
para satisfazer com um wrapper de três linhas: receba o prompt como último argumento,
imprima a resposta.

## O PRD, escrito a partir do mapa

O PRD semeado é um formulário em branco, e formulário em branco é justamente a coisa que
todo mundo deixa em branco — então a aba que devia guardar o *por quê* guarda um template
para sempre. O `croqui prd` preenche: o modelo recebe o mesmo resumo que o `croqui context`
entrega a um agente, e escreve o documento de volta na pasta de PRD que a aba já lê.

```sh
croqui prd                                   # preenche .croqui/prd/01-visao-geral.md
croqui prd --dry-run                         # qual modelo, qual arquivo, não chama nada
croqui prd --about "foco no checkout"        # direciona o que ele enfatiza
croqui prd --name 02-billing.md              # um segundo documento, ao lado do primeiro
```

A regra de posse da semeadura é o que faz isto ser seguro rodar duas vezes:

| | |
|---|---|
| arquivo que ainda tem `croqui:rascunho` | é rascunho do croqui — o formulário em branco, ou um que o `croqui prd` escreveu. Pode ser substituído, e a execução avisa |
| arquivo sem essa linha | **é prosa que alguém escreveu.** Recusado na hora, com as duas saídas: `--name` para um documento novo, `--force` se você realmente quer |

Então re-rodar depois de uma mudança de código não custa nada, e re-rodar depois que você
começou a escrever não acontece. Modelo que responde vazio, ou engine que falha, não
escreve nada — o arquivo em disco fica exatamente como estava.

O prompt diz ao modelo, com essas palavras, que endpoint ou tabela que não está no mapa
não entra no documento, e que o que o código não responde vai em *Perguntas em aberto* em
vez de ser preenchido com algo plausível. É um rascunho feito por uma coisa que leu a sua
arquitetura e não a sua intenção, e o cabeçalho que ele escreve diz isso também.

## Stacks

| Stack | Situação |
|---|---|
| Python / FastAPI + SQLAlchemy | suportado |
| qualquer outra | só `croqui enrich`, até um adaptador chegar |

Adaptadores moram em `croqui/adapters/`, um módulo por stack, cada um transformando código-fonte em nós e arestas do grafo. `detect_stack()` escolhe um; `--stack` sobrepõe.

## Limites conhecidos

- **Operações são agregadas por classe de repositório, não por método.** Se o `ShipmentRepository` grava em `tb_shipments` em qualquer lugar, um endpoint só de leitura que usa esse repositório ainda mostra uma aresta de escrita. A evidência `arquivo:linha` aponta para o ponto de chamada real; resolução por método não está implementada.
- **Nomes de schema vêm do que o repo declarar.** Onde DDL e ORM discordam sobre o schema de uma tabela, o mapa segue o DDL e lista todas as origens em *DDL/model* no painel — confira antes de confiar no prefixo.
- **SQL dinâmico é invisível para a passada de AST.** Um nome de tabela montado com f-string não vai ser encontrado; é para isso que existe o `enrich`.
- **Um rename só é seguido quando é inequívoco.** Renomeie uma classe *e* mova para um arquivo novo no mesmo commit, ou renomeie dois serviços no mesmo arquivo de uma vez, e o `scan` se recusa a chutar: as referências viram órfãs e o chip **órfãs** lista para você re-apontar à mão. Essa é a troca deliberada — um casamento errado move uma anotação para o nó errado em silêncio.
- **Colunas são lidas, não resolvidas.** O painel mostra o que o `CREATE TABLE` ou o modelo ORM *deste repo* diz, com o DDL ganhando quando discordam. Uma coluna acrescentada por uma migration que o croqui nunca viu, um tipo atrás de um alias, uma `Table()` montada em runtime — nada disso aparece. É o schema como está escrito, que é o honesto a mostrar ao lado de um mapa construído do mesmo jeito.
- **`croqui context` é um digest, não o grafo inteiro.** Descrições são cortadas, cadeias têm teto de seis saltos, e o corpo de um documento só viaja com `--full`. Quando um modelo precisa de tudo sobre uma coisa só, `--node` (ou a ferramenta `croqui_node`) é a entrada; `--json` é a saída para outra ferramenta.
- **A reconciliação compara dois grafos, então precisa de uma linha de base.** O `scan` guarda uma em `.croqui/graph.prev.json` — o grafo com que o seu layout está em dia — que é por que re-escanear duas vezes antes de abrir o visualizador, ou com `--no-migrate`, ainda segue o rename depois. Apague esse arquivo e a próxima varredura não tem com o que comparar.

## Licença

MIT
