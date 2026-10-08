# croqui

**English** · [Português](https://github.com/guimileib/croqui/blob/main/README.pt-BR.md)

Interactive, sketch-style maps of an API **and the database it touches** — generated from source, served on localhost, saved in the project. Drop a PRD and some mermaid next to it and `croqui build` gives you one HTML file that carries the map, the requirements and the diagrams together.

```
endpoints            services            repositories        database
┌──────────────┐     ┌────────────┐      ┌───────────────┐   ┌──────────────┐
│ ▸ v2 — Orders│────▶│OrderService│─────▶│OrderRepository│──▶│ crm.tb_orders│
│ 16 endpoints │     └────────────┘      └───────────────┘   │ tb_order_item│
│ GET 8 POS 5  │                                             └──────────────┘
└──────────────┘
```

Not a picture of your folder structure — a map of which endpoint reaches which table, with the SQL operation on every edge and a `file:line` for every claim.

## Why

Generated API docs stop at the HTTP boundary. `openapi.json` tells you `POST /orders` takes an `OrderDTO`; it does not tell you that call writes `tb_orders`, `tb_order_item`, and `tb_order_audit`. That second half is what you actually need when reviewing a migration, onboarding someone, or explaining a bug.

Excalidraw draws that beautifully and by hand, once, and then it's stale. croqui regenerates it from the code and stays hand-drawn.

## Install

Not on PyPI yet (the name is registered to nobody — see *[Publishing](RELEASING.md)*),
so install it from a checkout:

```sh
git clone https://github.com/guimileib/croqui
pipx install ./croqui          # the CLI on your PATH, in its own venv
```

`pipx` is the right tool here even though croqui is a library-shaped package: it is a
command you run *against* other projects, so it does not belong in any one project's
virtualenv. Working on croqui itself? `pipx install --editable ./croqui` — the viewer is
read off your checkout, so a change to `app.js` shows up on the next reload.

Without `pipx`, and without installing anything at all — croqui has **zero runtime
dependencies**, so any Python 3.10+ can run it straight from the checkout:

```sh
PYTHONPATH=/path/to/croqui python3 -m croqui.cli scan .
```

Pure standard library. Nothing to build, no CDN, no browser engine.

### Updating

```sh
croqui update          # from wherever you installed it
croqui update --check  # say what it would run, change nothing
```

The point is not having to remember. By the time there is a new version you are three
directories away from the checkout, and the right command depends on how you installed
it months ago. `croqui update` works that out: `pipx upgrade` for a pipx install, a
`git pull --ff-only` plus a reinstall for a checkout, `pip install --upgrade croqui`
for a plain pip one. It prints every command before running it, and never guesses when
it cannot tell.

## Use

```sh
croqui scan       # analyse the source  -> .croqui/graph.json
croqui serve      # open the editable map on localhost:7777
croqui build      # one self-contained .html — map + PRD + diagrams, no server
croqui prd        # write the PRD from the map, with whatever model you have
croqui context    # the same map as text, for an LLM's context window
croqui mcp        # run as an MCP server, so an agent can read it and write documents
croqui engines    # which models this machine can talk to
croqui update     # update croqui itself
```

Run them from the project root, or pass a path: `croqui scan ~/code/my-api`.

The first scan also creates `.croqui/prd/` and `.croqui/uml/`, so the map arrives with a document
tab on each side of it — see *[One file, three tabs](#one-file-three-tabs)*.
`croqui scan --no-docs` if you would rather it did not.

Re-scanning an already-mapped project keeps your work: `scan` re-points `layout.json` at anything the code renamed and reports whatever it could not match. See *[Surviving a code change](#surviving-a-code-change)*.

The spec is found automatically in the usual places (`contracts/openapi.json`, `openapi.json`, `docs/`, `static/`, `swagger.json`, …). Point at it explicitly — including a running app — when it lives elsewhere:

```sh
croqui scan --openapi api/spec/openapi.json
croqui scan --openapi http://localhost:8000/openapi.json
```

Without a spec the map still builds from the code; you just lose the documentation.

## What lands in your project

```
.croqui/
├── graph.json       # the map. commit this — it diffs cleanly in a PR
├── layout.json      # your arrangement, drawings, relations and view. commit this too
├── images/          # pictures you pasted onto the map. commit these too
├── prd/             # the PRD tab. prose — commit it
│   └── 01-visao-geral.md   # a blank PRD to fill in. yours; croqui never rewrites it
├── uml/             # the UML tab. prose — commit it
│   └── relacoes.mmd        # the map as a mermaid diagram, redrawn by every scan
├── croqui.html      # optional self-contained snapshot (croqui build)
├── graph.prev.json  # the graph your layout is in step with. gitignore this
└── cache.json       # LLM cache. gitignore this
```

Everything croqui writes lives under `.croqui/`, the two document folders included:
a tool that maps someone else's repo should not scatter folders through its root. But
they are only *born* there — a `prd/` at the project root, or a `docs/prd` the repo
already keeps, is read exactly the same and is never moved. Two files in there are
throwaway and two are not, so `.gitignore` the two named above rather than the whole
folder: a blanket `.croqui/` takes your PRD with it. See *[What the first scan
puts there](#what-the-first-scan-puts-there)*.

`graph.json` being a committed artifact is the point: a PR that moves an endpoint onto a new table shows up as a diff in the map, not just in the code.

### Editing is explicit

Moving a box, hiding a node, drawing a shape, writing a text box — all of it stays in memory until you press **salvar alterações** (or Ctrl+S). The button reads `salvo` when the map matches the file and `salvar alterações •` when it does not, and that state is a real comparison against what is on disk: undo your way back to the saved layout and the button goes quiet again. Closing the tab with pending edits warns first.

Saving writes down **every box on screen**, not only the ones you dragged. That is deliberate — see *[Surviving a code change](#surviving-a-code-change)* — and it is what makes the next `croqui scan` additive instead of a reshuffle.

`Ctrl+Z` walks back through moves, hides, drawings, relations and *limpar edições* alike (100 steps). Inside a text box it stays the browser's own text undo, as you'd expect.

## The viewer

Every stroke on the page — the generated map and the shapes you draw on it — comes out of one seeded-wobble function, not a drawing library. That is why the whole viewer is a few KB and renders 120 nodes instantly. Excalidraw's own runtime is far heavier than the diagram it draws.

| | |
|---|---|
| **click a group** | expand its endpoints (a 70-endpoint API is unreadable flat) |
| **click a node** | side panel: the endpoint's full documentation, parameters, responses, `file:line`, SQL ops, DDL origin |
| **click a table** | the same panel, plus **its columns** — name, type, `PK`, `not null`, and a foreign key that jumps to the table it points at. Read from the `CREATE TABLE` or the ORM model, never guessed |
| **⇤ / ⇥** | widen the panel for long documentation |
| **realce** | dim the rest of the map. **Off by default.** With it on, the pointer highlights whatever it is over — and **clicking a box pins the highlight there**, so it survives moving the cursor away to read the panel. Hovering something else previews that chain; leaving it returns to the pinned one; closing the panel lets go |
| **drag** a box or drawing | move it |
| **drag on the paper** | sweep a selection rectangle — boxes and drawings light up as it grows. Dragging any picked item moves them all in a single undo step. **Esc** or a click on the paper clears it |
| **shift+click** / **shift+drag** | add to the selection instead of starting over |
| **Ctrl+A** | select every box and drawing on the map |
| **Space + drag**, or the middle button | pan, with the grabbing hand — from anywhere, even starting on a box |
| **Ctrl+Z / Ctrl+Shift+Z** | undo / redo — up to 100 steps |
| **Ctrl+S** or **salvar alterações** | write `layout.json` — the arrangement you see, plus the view. Nothing is written before that |
| **órfãs** | appears only when the layout names nodes the code no longer has; lists them and can discard them |
| **double-click the paper** | a text box, right there — see *Drawing on the map* below |
| **Ctrl+V** | paste onto the map, where the cursor is: a picture, plain text, or an Excalidraw selection — shapes, arrows and all |
| **tabs** | `mapa` · `PRD` · `UML` — see *[One file, three tabs](#one-file-three-tabs)*. **Esc** or **m** goes back to the map |
| **isolate this chain** | show only one endpoint's path to the database |
| **search / verb chips** | dim everything that doesn't match |
| **roda / shift+roda** | zoom / pan |
| **URL** | `…/croqui.html#node=route:GET:/v2/crm/orders` deep-links straight to one endpoint |

Dark and light follow the OS. `✍` switches to a handwriting font where the OS has one.

## One file, three tabs

The generated map answers *what the code does*. Two questions it cannot answer are
*why* and *how we meant it to work* — and those live in a PRD and in diagrams
somebody drew on purpose. Put them next to the map and `croqui build` produces one
HTML file that holds all three:

```
.croqui/prd/
  01-vision.md            ->  PRD tab
  02-out-of-scope.md
.croqui/uml/
  order-flow.mmd          ->  UML tab
  checkout-sequence.mmd
```

Both directories are looked for inside `.croqui/` first and then at the project root
(`prd/`, `docs/prd` and `PRD` all work), so put them wherever your repo already keeps
prose. Nested folders are read. The tabs are not editors: apart from
the two files described in *[What the first scan puts there](#what-the-first-scan-puts-there)*,
croqui only ever **reads** this prose, and it never rewrites a PRD.

The tab strip only shows a tab that has something behind it: a project with no PRD
anywhere looks exactly as it did before. Every reload re-reads the directory, so
editing a document and pressing reload is the whole workflow — no re-scan needed.

### What the first scan puts there

You do not have to create the folders. `croqui scan` makes them if the project has
none, and drops one document in each so both tabs open with something in them:

```
.croqui/prd/01-visao-geral.md   # a blank PRD, with this project's own counts in it
.croqui/uml/relacoes.mmd        # the scanned map written as mermaid — the relation itself
```

They are created **inside `.croqui/`**, next to `graph.json` and `layout.json`, for
the same reason croqui writes nothing else at the root: it is a guest in someone
else's repo. Each folder is decided on its own, so a repo with a `docs/prd` and no
diagrams gets its seed in `docs/prd` and a new `.croqui/uml`.

Who owns which is the whole design:

| | |
|---|---|
| `01-visao-geral.md` | **written once.** It is yours the moment it lands — croqui never reads it back, diffs it or rewrites it. A project that already has a PRD anywhere croqui looks gets no seed at all |
| `relacoes.mmd` | **derived.** Every `croqui scan` redraws it from the code, so the diagram cannot drift from the map. Its second line says so |

```
%% croqui:gerado — reescrito a cada `croqui scan` enquanto esta linha existir.
```

Delete that line and the file becomes yours: croqui reports that it left it alone
and never writes it again. Rename it, and you keep both — your version and a fresh
`relacoes.mmd` next to it. `croqui scan --no-docs` skips all of this.

If the repo already keeps prose somewhere croqui looks — `docs/prd`, a `uml/` at the
root — the seed lands **there**, and nothing is migrated into `.croqui/`.

Past 150 nodes the diagram is a sample rather than the whole graph, taken evenly
across endpoints, services, repositories and tables. It says in a `%%` comment how
many it left out, and the map tab still has every one of them.

### The PRD tab

The tab opens on a **gallery of cards**, one per document — title, the opening lines,
the file, and whether it is Markdown or HTML. Click a card to read it;
**← todos os PRDs** goes back to the gallery.

Markdown, rendered by the same ~90-line renderer as the endpoint panel: headings,
lists, tables, block quotes, code, **bold**, `code`, and http(s) links. `.md`,
`.markdown` and `.txt`. The document's title comes from its first heading, falling
back to a tidied-up filename.

**`.html` / `.htm`** is shown as the page it is, in a sandboxed frame: its styles and
scripts run, but it cannot reach the viewer, your layout or the server. Its title is
its `<title>` (or first `<h1>`). Under `croqui serve` it is loaded by URL, so pictures,
stylesheets and scripts it links **relatively** — `<img src="assets/tela.png">` next to
the page — load like on any web server; only files inside the PRD folders are served.
`croqui build` folds those local pictures and stylesheets into the page as data URLs,
so the bundle carries them.

A ```` ```mermaid ```` block inside a PRD is **drawn**, not listed — so the diagram
that explains a requirement sits in the paragraph that states it. Any other fenced
block stays a code listing. An inline diagram fills the text column and may grow
past its natural size to do it, capped so a three-node picture does not become a
poster; a diagram you want to *read* belongs on the UML tab, which is a viewport.

### The UML tab

`.mmd`, `.mermaid` or `.md` holding mermaid source. The first scan already put
`relacoes.mmd` there; add your own beside it — ask an LLM for a diagram of your PRD,
drop the file in the same folder, reload.

**The tab is a viewport, not a page.** The diagram takes the whole panel and gets its
own pan and zoom: **wheel** to zoom about the cursor, **Space + drag** (or the middle
button) to pan, **drag** on the paper to select nodes — which lights up their relations
and fades the rest — **drag a node** to move the selection, **double-click**
or **f** to fit, and a readout in the corner that is also the fit button. It opens
fitted, so a wide graph arrives whole instead of squeezed into a text column with its
labels unreadable — which was the one thing this tab existed to avoid.

Pan and zoom move the `viewBox`, never a transform on the ink. The sketch wobble is
baked into the path data, so scaling the paths would scale the pencil stroke with
them; moving the window instead means 400% is drawn by the same hand as 100%.

**croqui draws mermaid itself — it does not load mermaid.js.** That is the point:
mermaid's runtime is an order of magnitude bigger than this entire viewer, and a
document whose three tabs are drawn in three different visual languages reads as
three documents. Your diagram comes out in the same wobbling pencil line as the
generated map, from the same `sketchRect`/`sketchArrow` that draw everything else.

What it draws today:

| | |
|---|---|
| `graph` / `flowchart` | `TB` `TD` `BT` `LR` `RL`; node shapes `[]` `()` `([])` `[[]]` `[()]` `(())` `{}` `{{}}` `[//]`; edges `-->` `---` `-.->` `-.-` `==>` `===` `<-->`; labels as `--\>\|text\|` or `-- text -->`; chains `A --> B --> C`; fan-out `A & B --> C & D`; `subgraph … end` with a framed, labelled group; quoted labels; `<br/>`; `%%` comments |
| `sequenceDiagram` | `participant` / `actor`, `as` aliases, `->>` `-->>` `->` `-->` `--x`, message labels, `Note over/left of/right of` |
| anything else | the diagram type is named and **your source is shown verbatim** — never a blank panel |

`classDef`, `style`, `linkStyle` and `click` are read and ignored: the sketch look
is croqui's own, and a diagram that half-honours a colour directive is worse than
one that plainly does not. `loop` / `alt` / `opt` / `par` in a sequence diagram are
not drawn as bands yet; croqui says so above the diagram and still draws every
message.

Layout is croqui's, not mermaid's: longest-path ranking, barycenter ordering inside
each rank, subgraph members kept together. Edges that close a cycle — a retry
pointing back at what it retries — are drawn but excluded from the ranking, because
a cycle in a longest-path pass does not terminate, and quietly stopping it at a
pass limit puts nodes in the wrong band instead of failing loudly.

## croqui for an AI

The viewer answers a person's questions by being a picture. A model cannot look at a
picture — it needs the same knowledge as text, short enough to sit in a context window
next to the actual work. Two commands do that, and they are the same facts twice:

```sh
croqui context                       # markdown, for pasting into any LLM
croqui context --json                # the same, structured
croqui context --node /v1/orders     # one endpoint or table, in full
croqui context --full                # include every endpoint doc and document body
```

The digest names the project, lists every endpoint with the chain behind it
(`GET /v1/orders → OrderService → OrderRepository -[grava: insert]-> tb_orders`), the
tables with their columns, the services and repositories, and an index of the PRD and
UML documents. It is deliberately lossy — descriptions are trimmed, chains are capped
— because a context that does not fit is a context nobody uses. A small API comes out
around 2 KB.

### The half that makes it a framework

Reading is only one direction. The last section of every digest tells the model
**where it may write**:

```
## como escrever aqui
- PRD → .croqui/prd/*.md   — markdown; the first heading names it in the PRD tab
- UML → .croqui/uml/*.mmd  — mermaid; flowchart and sequenceDiagram are drawn
- ⚠ .croqui/uml/relacoes.mmd is generated and rewritten by every scan — write a new file beside it
- ⚠ .croqui/graph.json is derived from the code — change the code and re-scan
```

So the loop closes: the model reads the map, writes a PRD or a diagram into the
folders the viewer already renders, and the next person opens `croqui serve` and sees
it in a tab. Nothing about that is Claude-specific — it is files in a folder.

### MCP

For Claude Code and Claude Desktop, the same thing without the copy-paste:

```sh
claude mcp add croqui -- croqui mcp
```

| tool | |
|---|---|
| `croqui_map` | the digest above, markdown or JSON |
| `croqui_node` | one endpoint or table in full — accepts an id, a bare path, or a table name |
| `croqui_docs` | list the PRD and UML documents, or read one |
| `croqui_write_doc` | write a document into the PRD or UML folder |
| `croqui_scan` | re-read the source and rebuild the map |

`croqui mcp` is a thin wrapper: every read goes through the same `context` code the CLI
uses, so there is one definition of what the map says. JSON-RPC over stdio, standard
library only — no server, no dependency, nothing leaves the machine.

Writing is fenced rather than trusted. A document name must be a plain file name (no
directories, no dotfiles), the extension has to match the section, and
`relacoes.mmd` is refused outright with an explanation — croqui regenerates that
file, so anything written there would be lost at the next scan. Every refusal comes
back as a tool **error**, not as text: a refusal that reads like success is a model
that believes it saved your work.

## Drawing on the map

A generated map answers *what the code does*. The things you need to write on top of it — this whole column is legacy, that queue is coming next quarter, these two services agree on a contract nothing in the source states — are not in the code, so no scanner will ever produce them. The tool rail on the left is for those.

| Tool | | |
|---|---|---|
| shapes | `r` `o` `d` `t` `h` `c` `n` | rectangle, ellipse, diamond, triangle, hexagon, cylinder, cloud |
| arrows | `a` `l` | arrow and plain line, with a head at one end, both, or neither — an end drawn into a box holds on to it |
| text box | `x`, or **double-click** the paper | resizable, borderless, in any colour and size |
| relation | `e` | a new edge between two existing nodes, with your own label |
| image | **Ctrl+V** | a pasted picture — a screenshot, a whiteboard photo, a Grafana panel |
| a drawing from Excalidraw | **Ctrl+V** | boxes, diamonds, ellipses, arrows, lines, text and images, in the arrangement they were copied in |

Drag to size it, or click once for a sensible default. Everything is drawn with the same seeded wobble as the map itself, so what you add does not look pasted on.

- **double-click** is the one gesture for writing: on bare paper it drops a new text box where you clicked, on a drawing it puts the caret inside that drawing. A text box grows downward as you fill it
- **drag the writing itself** to move a text box, even with the caret still in it — a click places the caret, a drag moves the label
- a text box with **preenchida** off is just the writing: no background, and no frame either once there is text in it (an empty one keeps a faint dashed outline, or you would never find it)
- **drag the handles** to resize; arrows get one handle per end. Let an end go **inside a box** — a node, or any drawing of yours — and it holds on to that box: the tip sits on its edge and follows it wherever it goes. A filled handle is an end that is holding on; **Ctrl** while letting go keeps it free; dragging the arrow itself lets go of both ends, and where it lands decides anew
- **Del** removes what is selected, **Esc** puts the pointer tool back
- **A− / A+** in the inspector sets the size of the letters, from 9 to 44px — on the selected drawing, or, with nothing selected, on the next one you draw
- the small panel next to the rail sets colour, fill, dashes, arrow heads, whether the shape sits **behind** the map (the default, so a rectangle works as a frame around a chain) or **à frente**, and which node it is **attached** to

### Pasted images

Paste a screenshot straight onto the map with **Ctrl+V** and it lands where your
cursor is, at its own aspect ratio, scaled down to fit if it is large. It behaves
like any other drawing: drag it, resize it by the handles, put it behind the map,
attach it to a node, **Del** to remove it, **Ctrl+Z** to take the paste back. It
arrives *in front* of the map rather than behind, because an opaque picture behind
the diagram is one that node boxes sit on top of — press **à frente** to push it
back if a backdrop is what you wanted. PNG, JPEG, GIF and WebP, up to 12 MB.

The bytes go to `.croqui/images/`, named after a hash of their own content, and
`layout.json` keeps only that name:

```json
{ "id": "sh-…", "type": "image", "x": 470, "y": -300, "w": 520, "h": 260,
  "src": "a1b2c3d4e5f60718.png", "front": true }
```

That split is the whole point. A screenshot inlined as base64 would be a megabyte
on one line of a file that is supposed to be reviewable in a PR, and it would put
`layout.json` near the size limit the viewer refuses to save past. Naming the file
instead keeps the layout diffable, makes pasting the same screenshot twice cost one
file, and means `croqui build` can still inline everything into a self-contained
HTML you can mail to someone.

Removing an image from the map leaves its file in `.croqui/images/` — croqui never
deletes one on its own, so an undo or a `Ctrl+Z` after a save always has the bytes
to come back to. Delete unused ones by hand if the directory grows.

### Pasted text and Excalidraw drawings

**Ctrl+V** with text on the clipboard drops a text box on the map, where your cursor
is, sized to fit what it holds. From there it is an ordinary text box: **A− / A+**
resizes the letters, the handles resize the box, **Del** removes it, **Ctrl+Z** takes
the paste back.

Copying elements in **Excalidraw** puts Excalidraw's own JSON on the clipboard, so a
plain paste would drop a page of JSON on your map. croqui reads that JSON instead and
redraws it with its own pencil:

| in Excalidraw | in croqui |
|---|---|
| rectangle, diamond, ellipse | the same shape, at the same size |
| arrow, line | arrow or line, keeping direction and which ends carry a head — and which box each end held on to, when that box came along in the same copy |
| text | a text box, at the font size it had, clamped to the 9–44px croqui draws in |
| a label inside a box | that box's own writing — not a second shape on top of it |
| a label on an arrow | a text box of its own, where Excalidraw had it (croqui's arrows hold no text) |
| stroke colour | the nearest of croqui's eight colours; a neutral stroke becomes `ink`, which follows the theme |
| background, dashes | **preenchida** and **tracejada** on the shape |
| image (PNG, JPEG, GIF, WebP) | an image, same place and size — stored in `.croqui/images/` like any pasted picture; arrows tied to it stay tied |

The arrangement is preserved — the whole copy moves as a group to your cursor, in the
order it was stacked — and the paste is a single undo step. Anything that lands on a
node attaches to it like any other drawing.

A few things do not survive the crossing, and the toast says which of them turned up
(*8 desenhos colados — fora: 1 rabisco*): **freehand ink**, **frames and embeds** and
**SVG images** — croqui has no counterpart for them — and an **elbowed arrow**, which
arrives as a straight run between its two ends. **Rotation** is lost too: a tilted
rectangle comes in square. Images need `croqui serve` (they are written to disk); in a
read-only `croqui.html` the rest of the copy still pastes and the toast says the
images stayed out.

Pasting with the caret inside a text box works the way you expect: plain text goes in
at the caret, and an Excalidraw copy goes in as its writing, not as JSON.

### Relations you draw yourself

The `relation` tool connects two nodes: click the source, then the target — or drag between them. The result is a labelled edge that behaves like the generated ones. It highlights with the node, it counts as part of *isolar esta cadeia*, and it follows a route into its group when the group collapses. What it never does is influence the automatic column layout, so drawing one does not reshuffle the map under you.

Relations are stored as node ids in `layout.json`, so they survive a re-scan and even a rename — see *[Surviving a code change](#surviving-a-code-change)*. One anchored to a *collapsed group* belongs to the group box and is hidden while the group is expanded; expand it first if you want the relation on the endpoint itself.

### Arrows that hold on to a box

A relation joins two *nodes*. An arrow is a drawing, and its ends can hold on to anything with a box: a node, a rectangle you drew around a chain, a text box, a pasted screenshot. The rule is one sentence — **an end resting inside a box holds on to it** — and it is applied whenever you let go: after drawing the arrow, after dragging one end, after moving the whole arrow. The tip stops just short of the edge, aimed at the other end (or at the other box's middle when both ends hold), so an arrow between two boxes stays an arrow between two boxes however you move them. When a box is inside another — a label on a node, a node inside a frame — the smallest one wins.

The inspector says what an arrow holds (*ligada: retângulo → OrderService*); pressing it lets go of both ends. Deleting a drawing frees the ends that held on to it; the arrow stays where it was. An Excalidraw arrow that was bound to a box arrives bound to the copy of that box.

### Drawings that belong to a node

A note about one service should travel with that service. Drop a drawing on a node and it is **attached to it**: drag the node and the drawing comes along, and it is still in the right place after the code moves underneath. The inspector says which node holds it — press it to let go, or to attach one that was drawn on bare paper and then moved over a node.

A drawing that spans **two or more** nodes is left on the paper instead, because that is a frame around a chain rather than a label on one box. Nothing about the ink changes either way; only whose coordinates it follows.

Drawings and relations live in `layout.json` next to your positions, so they diff in a PR like everything else:

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

`anchor` is what attaches a drawing to a node; `from`/`to` are what an arrow's ends hold on to, a node or another drawing. In both cases `x`/`y` (and `w`/`h`) stay in the file as the last resolved place, the fallback for when that box is hidden or gone. Layouts written before drawings existed carry a `notes` array; it is read back as text boxes in their original sticky-note yellow, and written out in the new form the next time you save.

## Surviving a code change

The map is only worth arranging if the arrangement outlives the code it describes. Three separate things used to reset it, and each is handled differently.

**One new endpoint used to move everything.** The automatic layout orders each column by the barycenter of its parents, so a single added route re-sorts and re-centres everything downstream — and a box you never dragged had nothing on disk holding it in place. So saving records every visible box. A node the code just grew starts where the automatic layout wanted it and slides down until it stops overlapping anything already saved: it appears next to its neighbours without pushing them.

**A rename used to orphan your work.** Node ids are semantic — `service:UserService`, `route:GET:/users` — which is exactly why a layout survives a re-scan at all, and exactly why renaming the class renames the id. Positions, hidden flags, drawing anchors, arrow ends and hand-made relations then pointed at a name nothing answered to. `croqui scan` now reads the previous graph, works out which vanished ids are the same thing under a new name, and re-points `layout.json`:

```
✓ wrote .croqui/graph.json
✓ layout.json follows the rename — 5 references re-pointed
  · route:GET:/users → route:GET:/api/users  (same handler, same file)
  · service:UserService → service:AccountService  (same class, same file)
! 1 layout reference with no matching node (kept in the file, invisible on the map)
  · repo:LegacyRepo — relation
```

A rule only fires when the answer is unambiguous on **both** sides — one vanished id, one new id — because a wrong guess moves your annotation onto the wrong node, which is worse than losing it. What it matches: a handler renamed or re-pathed in the same file, a handler whose file moved, a service or repository class renamed in place, a table whose schema changed, and an endpoint group that still holds most of its endpoints. Pass `--no-migrate` to get the report without the rewrite.

Whatever it cannot match is **left in the file**, never deleted, and surfaced both in that report and as an **órfãs** chip in the viewer — because on the map the loss is invisible: a relation to a node that no longer exists simply does not draw. The chip lists them and can discard them in one step; a drawing keeps its ink and its place, only its dead anchor goes.

**What a scan never touches.** The rewrite above is the only thing `croqui scan`
does to `layout.json`, and it only ever rewrites *node ids*. Your shapes, your
writing, the font sizes and colours you picked, pasted images and where they sit,
which boxes you hid, and the camera all pass through untouched — and the image
files in `.croqui/images/` are not on the scan's path at all. If the layout cannot
even be parsed, the scan reports it and writes nothing, on the same principle the
viewer uses when it opens read-only rather than overwrite work it cannot read.

**Reading state used to reset on every reload.** Which groups are open, where the camera sits, which filters are on and what is isolated now persist too. They are stored twice, on purpose: in `localStorage` on every change, so a reload right after `croqui scan` opens the map exactly as you left it with nothing to press — and in `layout.json` on an explicit save, so a teammate cloning the repo opens the view you committed. This is not an edit to the map, so it stays out of the `salvar alterações` comparison: panning never lights that button. Ctrl+S is how you commit a view-only change.

## The documentation panel

OpenAPI says `description` is CommonMark, and people use it — the rules, the edge cases, the "why" that no diagram conveys. croqui renders it in full rather than truncating it, from the standard spec fields, so this works with any OpenAPI 3 document and not just FastAPI's:

| Spec field | In the panel |
|---|---|
| `description` | rendered markdown: headings, lists, **bold**, `code`, tables, block quotes, links |
| `summary` | the one-liner under the title |
| `parameters[]` | name, location, required, type, default, and each one's own description |
| `requestBody` / `responses` | body schema, and every status code with its description |
| `operationId`, `tags`, `deprecated` | shown as metadata |

Markdown is rendered by a ~90-line function, not a library — everything is HTML-escaped *before* any transform runs, and only `http(s)` links are linkified, so a hostile spec cannot inject script into the page. When there is no spec entry, the handler's docstring is used instead and labelled as such.

## How it resolves the chain

Static analysis first, LLM only as a fallback. On a 69-endpoint FastAPI service this resolves the whole graph with **zero LLM calls**:

1. **`openapi.json`** — routes, methods, summaries, schemas at 100% fidelity.
2. **AST** — `@router.get` → `Depends(get_x_service)` → `XService` → `self.repo: XRepository` → `select(Model)` / `text("SELECT … FROM tb_x")` → table, with the operation.
3. **Ground truth for tables** — every table must exist in repo DDL (`CREATE TABLE`) or an ORM `__tablename__`. This is the anti-hallucination gate: anything not in that set is dropped, whether a regex or a model proposed it.

`croqui enrich` sends only the nodes step 2 could not resolve, as one code slice each, cached by content hash. Tables it returns go through the same gate and are drawn dashed with `confidence: "llm"` — never silently mixed with proven edges.

```sh
croqui enrich --dry-run      # worklist + token estimate, calls nothing
croqui enrich                # whichever model this machine has — see below
croqui enrich --engine http  # a specific one, from `croqui engines`
```

## Which model

croqui has no API key of its own and no opinion about whose model you use. It looks
for one you already have, in this order, and `croqui engines` prints what it found:

| | |
|---|---|
| **an agent CLI on PATH** | `claude`, `codex`, `gemini`, `cursor-agent`, `opencode`, `llm`, `ollama`. Already authenticated — nothing to configure, no key anywhere |
| **an OpenAI-compatible endpoint** | `CROQUI_LLM_BASE_URL` + `CROQUI_LLM_MODEL`, and `CROQUI_LLM_API_KEY` if it needs one. One shape covers OpenAI, Groq, Together, OpenRouter, Azure, vLLM, LM Studio and Ollama's `/v1` |
| **the Anthropic SDK** | if `anthropic` is installed and `ANTHROPIC_API_KEY` is set |
| **anything at all** | `CROQUI_LLM_CMD="my-cli --prompt"` — croqui appends the prompt as the last argument and reads stdout |

```sh
croqui engines                      # what this machine can do, best first
export CROQUI_LLM_BASE_URL=https://api.groq.com/openai/v1
export CROQUI_LLM_API_KEY=...
export CROQUI_LLM_MODEL=llama-3.3-70b-versatile
croqui prd                          # and now it is a Groq model writing the PRD
```

`--engine auto` is the default everywhere and picks the first row that works. The
HTTP engine is `urllib` and nothing else: croqui still installs with zero runtime
dependencies, and that is not going to change for this.

`CROQUI_LLM_CMD` is the whole extension story. A CLI croqui has never heard of is one
environment variable away, and the contract is small enough to satisfy with a
three-line wrapper: take the prompt as the last argument, print the answer.

## The PRD, written from the map

The seeded PRD is a blank form, and a blank form is the thing everyone leaves blank —
so the tab meant to hold *why* holds a template forever. `croqui prd` fills it: the
model gets the same digest `croqui context` hands an agent, and writes the document
back into the PRD folder the tab already reads.

```sh
croqui prd                                   # fill .croqui/prd/01-visao-geral.md
croqui prd --dry-run                         # which model, which file, calls nothing
croqui prd --about "foco no checkout"        # steer what it emphasises
croqui prd --name 02-billing.md              # a second document, beside the first
```

The ownership rule from the seeding is what makes this safe to run twice:

| | |
|---|---|
| a file still carrying `croqui:rascunho` | croqui's draft — the blank form, or one `croqui prd` wrote. It may be replaced, and the run says so |
| a file without that line | **prose somebody wrote.** Refused outright, with the two ways forward: `--name` for a new document, `--force` if you really mean it |

So a re-run after a code change costs nothing, and a re-run after you started writing
does not happen. A model that answers empty, or an engine that fails, writes nothing
at all — the file on disk is left exactly as it was.

The prompt tells the model, in as many words, that an endpoint or table not in the map
does not go in the document, and that anything the code cannot answer belongs under
*Perguntas em aberto* rather than being filled in with something plausible. It is a
draft by a thing that read your architecture and not your intent, and the header it
writes says that too.

## Stacks

| Stack | Status |
|---|---|
| Python / FastAPI + SQLAlchemy | supported |
| anything else | `croqui enrich` only, until an adapter lands |

Adapters live in `croqui/adapters/`, one module per stack, each turning source into graph nodes and edges. `detect_stack()` picks one; `--stack` overrides it.

## Known limits

- **Operations are aggregated per repository class, not per method.** If `ShipmentRepository` writes `tb_shipments` anywhere, a read-only endpoint that uses that repository still shows a write edge. The `file:line` evidence points at the real call site; method-level resolution is not implemented.
- **Schema names come from whatever the repo declares.** Where DDL and ORM disagree about a table's schema, the map follows DDL and lists every source under *DDL/model* in the panel — check it before trusting the prefix.
- **Dynamic SQL is invisible to the AST pass.** A table name assembled from an f-string won't be found; that's what `enrich` is for.
- **A rename is only followed when it is unambiguous.** Rename a class *and* move it to a new file in one commit, or rename two services in the same file at once, and `scan` refuses to guess: the references become orphans and the **órfãs** chip lists them for you to re-point by hand. This is the deliberate trade — a wrong match silently moves an annotation onto the wrong node.
- **Columns are read, not resolved.** The panel shows what the `CREATE TABLE` or the ORM model in *this repo* says, with the DDL winning when they disagree. A column added by a migration croqui never saw, a type behind an alias, a `Table()` built at runtime — none of those appear. It is the schema as written, which is the honest thing to show next to a map built the same way.
- **`croqui context` is a digest, not the whole graph.** Descriptions are trimmed, chains are capped at six hops, and a document's body only travels with `--full`. When a model needs everything about one thing, `--node` (or the `croqui_node` tool) is the way in; `--json` is the way out to another tool.
- **Reconciliation compares two graphs, so it needs a baseline.** `scan` keeps one at `.croqui/graph.prev.json` — the graph your layout is in step with — which is why re-scanning twice before you open the viewer, or with `--no-migrate`, still follows the rename afterwards. Delete that file and the next re-scan has nothing to diff against.

## License

MIT
