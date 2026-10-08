/* Mermaid → croqui, the parsing and layout half.
 *
 * croqui draws a mermaid diagram itself instead of loading mermaid.js. That is not
 * stubbornness: mermaid's runtime is an order of magnitude larger than this whole
 * viewer, and the point of the UML tab is that a diagram written next to the code
 * comes out in the same hand-drawn line as the generated map. A picture in two
 * visual languages on two tabs of one document reads as two documents.
 *
 * This file is deliberately pure — text in, positioned model out, no DOM. The
 * drawing lives in app.js next to the other sketch code, and keeping the seam here
 * means the parser and the layout can be tested on their own, in node.
 *
 * The supported subset is stated in README under "The UML tab". Anything outside it
 * comes back as {kind:"unsupported"} carrying its own source, so an unrecognised
 * diagram shows up as something to read rather than as an empty box.
 */
(function (root) {
  "use strict";

  var FLOW_HEAD = /^\s*(graph|flowchart)\b\s*([A-Za-z]{2})?\s*[:;]?\s*$/i;
  var DIRS = { TB: "TB", TD: "TB", BT: "BT", LR: "LR", RL: "RL" };

  /* Node shapes, longest delimiter first — `[[x]]` must win over `[x]`. The third
   * entry is which croqui outline draws it; mermaid has more shapes than croqui has
   * outlines, so several map onto the same one rather than inventing lookalikes. */
  var SHAPES = [
    ["[[", "]]", "rect"],
    ["[(", ")]", "cylinder"],
    ["((", "))", "ellipse"],
    ["([", "])", "ellipse"],
    ["{{", "}}", "hexagon"],
    ["[/", "/]", "rect"],
    ["[\\", "\\]", "rect"],
    ["[", "]", "rect"],
    ["(", ")", "rect"],
    ["{", "}", "diamond"],
    [">", "]", "rect"],
  ];

  var NODE_ID = /^[A-Za-z0-9_.\-#]+/;

  // Geometry. Kept here rather than in the renderer so the layout is a complete
  // answer: the drawing code only has to put ink where this says.
  var CHAR_W = 6.9;          // average advance of the 13px UI font
  var WRAP_AT = 24;          // characters per line before wrapping a label
  var PAD_X = 20, PAD_Y = 16;
  var MIN_W = 96, MIN_H = 44;
  var RANK_GAP = 96;
  var NODE_GAP = 36;
  // Labelled edges put a plate on their midpoint; between two dense columns the
  // plates need room of their own or they sit on top of each other.
  var LABEL_GAP = 48;
  var SUB_PAD = 24, SUB_TITLE = 22;
  // Clear paper between two subgraph frames, on top of their own padding.
  var SUB_GAP = 28;
  // A subgraph with no inner chain that is this many times the next biggest one
  // is folded into several columns; never folded below FOLD_MIN per column.
  var FOLD_RATIO = 1.6, FOLD_MIN = 10;
  var MARGIN = 40;

  function esc(s) { return String(s == null ? "" : s); }

  /** Split a label into rendered lines: explicit <br> first, then soft wrapping. */
  function wrap(label) {
    var hard = esc(label).split(/<br\s*\/?>|\\n/i);
    var lines = [];
    hard.forEach(function (chunk) {
      var words = chunk.trim().split(/\s+/).filter(Boolean);
      if (!words.length) { lines.push(""); return; }
      var line = "";
      words.forEach(function (word) {
        if (!line) { line = word; return; }
        if ((line + " " + word).length <= WRAP_AT) { line += " " + word; return; }
        lines.push(line);
        line = word;
      });
      lines.push(line);
    });
    return lines.length ? lines : [""];
  }

  function measure(label, shape) {
    var lines = wrap(label);
    var widest = 0;
    lines.forEach(function (l) { widest = Math.max(widest, l.length); });
    var w = Math.max(MIN_W, Math.round(widest * CHAR_W) + PAD_X * 2);
    var h = Math.max(MIN_H, lines.length * 18 + PAD_Y * 2 - 8);
    // A diamond only has half its box to write in, and a circle rather less.
    if (shape === "diamond") { w = Math.round(w * 1.45); h = Math.round(h * 1.5); }
    if (shape === "ellipse") { w = Math.round(w * 1.22); h = Math.round(h * 1.3); }
    if (shape === "hexagon") { w = Math.round(w * 1.2); }
    if (shape === "cylinder") { h = Math.round(h * 1.25); }
    return { w: w, h: h, lines: lines };
  }

  /* ------------------------------------------------------------------- lexing */

  /** Read `A`, `A[Label]`, `A{{Label}}`… at `i`. Returns null if there is no id. */
  function readNode(text, i) {
    var rest = text.slice(i);
    var m = NODE_ID.exec(rest);
    if (!m) return null;
    var id = m[0];
    var j = i + id.length;
    for (var s = 0; s < SHAPES.length; s++) {
      var open = SHAPES[s][0], close = SHAPES[s][1];
      if (text.substr(j, open.length) !== open) continue;
      var from = j + open.length;
      var label = null, end = -1;
      // A quoted label may contain the closing delimiter.
      if (text.charAt(from) === '"') {
        var q = text.indexOf('"', from + 1);
        if (q > 0 && text.substr(q + 1, close.length) === close) {
          label = text.slice(from + 1, q);
          end = q + 1 + close.length;
        }
      }
      if (end < 0) {
        var k = text.indexOf(close, from);
        if (k < 0) continue;
        label = text.slice(from, k);
        end = k + close.length;
      }
      return { id: id, label: label, shape: SHAPES[s][2], next: end };
    }
    return { id: id, label: null, shape: null, next: j };
  }

  // `-->` and friends. Longest first, again: `-->` before `--`.
  var EDGE_RE = /^(<?)(-\.-+>|-\.-+|-{2,}>|-{2,}|={2,}>|={2,}|~{3})(?:\s*\|([^|]*)\|)?/;

  /** `A -- texto --> B` is the same edge as `A -->|texto| B`; say it one way. */
  function normalizeLabels(line) {
    return line
      .replace(/--\s*([^->|][^>|]*?)\s*-{2,}>/g, "-->|$1|")
      .replace(/--\s*([^->|][^>|]*?)\s*-{2,}(?![>-])/g, "---|$1|")
      .replace(/==\s*([^=>|][^>|]*?)\s*={2,}>/g, "==>|$1|")
      .replace(/==\s*([^=>|][^>|]*?)\s*={2,}(?![>=])/g, "===|$1|")
      .replace(/-\.\s*([^.>|][^>|]*?)\s*\.-+>/g, "-.->|$1|")
      .replace(/-\.\s*([^.>|][^>|]*?)\s*\.-+(?!>)/g, "-.-|$1|");
  }

  function edgeStyle(op, back) {
    return {
      dashed: op.indexOf(".") >= 0 || op.indexOf("~") >= 0,
      thick: op.indexOf("=") >= 0,
      invisible: op.indexOf("~") >= 0,
      heads: back ? "both" : (op.indexOf(">") >= 0 ? "end" : "none"),
    };
  }

  /* ------------------------------------------------------------- flowchart */

  function parseFlow(lines, dir, warnings) {
    var nodes = {}, order = [], edges = [];
    var subs = [], stack = [];
    var subSeq = 0;

    function touch(ref) {
      var node = nodes[ref.id];
      if (!node) {
        node = nodes[ref.id] = { id: ref.id, label: ref.id, shape: "rect", sub: null };
        order.push(ref.id);
      }
      // A later declaration with a label wins: mermaid lets you name a node once
      // and refer to it bare everywhere else.
      if (ref.label != null) node.label = ref.label;
      if (ref.shape) node.shape = ref.shape;
      if (node.sub == null && stack.length) node.sub = stack[stack.length - 1].id;
      return node;
    }

    lines.forEach(function (raw) {
      var line = raw.replace(/%%.*$/, "").trim().replace(/;+$/, "");
      if (!line) return;

      var sub = /^subgraph\s+(.*)$/i.exec(line);
      if (sub) {
        var spec = sub[1].trim();
        var id = "sub" + (++subSeq), label = spec;
        var bracket = /^([A-Za-z0-9_.\-]+)\s*[\[(]\s*"?(.*?)"?\s*[\])]$/.exec(spec);
        if (bracket) { id = bracket[1]; label = bracket[2]; }
        else if (/^[A-Za-z0-9_.\-]+$/.test(spec)) { id = spec; label = spec; }
        var frame = { id: id, label: label, parent: stack.length ? stack[stack.length - 1].id : null };
        subs.push(frame);
        stack.push(frame);
        return;
      }
      if (/^end$/i.test(line)) {
        if (stack.length) stack.pop();
        else warnings.push("`end` sem `subgraph` aberto");
        return;
      }
      // Styling directives carry no structure; the sketch look is croqui's own.
      if (/^(classDef|class|style|linkStyle|click|direction)\b/i.test(line)) return;

      line = normalizeLabels(line);

      /* A statement is a chain: group, edge, group, edge, group…  `A & B --> C`
       * is one group of two feeding one group of one, and `A --> B --> C` keeps
       * going from whatever the last group was. */
      function readGroup(at) {
        var group = [];
        for (;;) {
          while (at < line.length && /\s/.test(line.charAt(at))) at++;
          var ref = readNode(line, at);
          if (!ref) break;
          group.push(touch(ref));
          at = ref.next;
          while (at < line.length && /\s/.test(line.charAt(at))) at++;
          if (line.charAt(at) !== "&") break;
          at++;
        }
        return group.length ? { nodes: group, next: at } : null;
      }

      function readEdge(at) {
        while (at < line.length && /\s/.test(line.charAt(at))) at++;
        var m = EDGE_RE.exec(line.slice(at));
        if (!m) return null;
        var style = edgeStyle(m[2], !!m[1]);
        style.label = m[3] != null ? m[3].trim() : "";
        style.next = at + m[0].length;
        return style;
      }

      var head = readGroup(0);
      if (!head) return;
      var prev = head.nodes;
      var i = head.next;
      var guard = 0;
      while (guard++ < 200) {
        var edge = readEdge(i);
        if (!edge) break;
        var tail = readGroup(edge.next);
        if (!tail) { warnings.push("aresta sem destino: " + raw.trim()); break; }
        prev.forEach(function (a) {
          tail.nodes.forEach(function (b) {
            edges.push({
              from: a.id, to: b.id, label: edge.label,
              dashed: edge.dashed, thick: edge.thick,
              invisible: edge.invisible, heads: edge.heads,
            });
          });
        });
        prev = tail.nodes;
        i = tail.next;
      }
    });

    if (stack.length) warnings.push(stack.length + " `subgraph` sem `end`");

    var list = order.map(function (id) {
      var n = nodes[id];
      var m = measure(n.label, n.shape);
      return { id: id, label: n.label, shape: n.shape, sub: n.sub, lines: m.lines, w: m.w, h: m.h };
    });
    return { nodes: list, edges: edges, subs: subs };
  }

  /* ---------------------------------------------------------------- layout */

  /**
   * Which edges close a cycle, found by depth-first search.
   *
   * Ranking has to run on an acyclic graph or it does not terminate: `A --> B`
   * with a `B --> A` beside it pushes both ranks up forever, and cutting the loop
   * off at a pass limit does not fail loudly — it quietly returns inflated ranks,
   * which puts nodes in the wrong band and makes a subgraph frame swallow whatever
   * drifted inside its bounding box. A retry arrow pointing back at the thing it
   * retries is completely ordinary in a real diagram, so this is the common case,
   * not an edge case. The edge is still drawn; it just does not get a say in the
   * ordering.
   */
  function feedbackEdges(nodes, edges) {
    var out = {}, colour = {};
    nodes.forEach(function (n) { out[n.id] = []; colour[n.id] = 0; });
    edges.forEach(function (e, i) {
      if (out[e.from] && colour[e.to] != null) out[e.from].push({ to: e.to, i: i });
    });
    var back = {};
    // Iterative DFS: a deep chain must not blow the JS stack.
    nodes.forEach(function (start) {
      if (colour[start.id] !== 0) return;
      var stack = [{ id: start.id, at: 0 }];
      colour[start.id] = 1;
      while (stack.length) {
        var top = stack[stack.length - 1];
        var kids = out[top.id];
        if (top.at >= kids.length) {
          colour[top.id] = 2;
          stack.pop();
          continue;
        }
        var edge = kids[top.at++];
        if (colour[edge.to] === 1) { back[edge.i] = true; continue; }   // on the stack: a cycle
        if (colour[edge.to] === 2) continue;                            // already settled
        colour[edge.to] = 1;
        stack.push({ id: edge.to, at: 0 });
      }
    });
    return back;
  }

  /** Longest-path ranking over the graph minus its back edges. */
  function rankNodes(nodes, edges) {
    var rank = {};
    nodes.forEach(function (n) { rank[n.id] = 0; });
    var back = feedbackEdges(nodes, edges);
    var forward = edges.filter(function (e, i) {
      return !back[i] && e.from !== e.to && rank[e.from] != null && rank[e.to] != null;
    });
    // Without cycles this settles in at most one pass per node.
    for (var pass = 0; pass <= nodes.length; pass++) {
      var moved = false;
      forward.forEach(function (e) {
        if (rank[e.to] < rank[e.from] + 1) { rank[e.to] = rank[e.from] + 1; moved = true; }
      });
      if (!moved) break;
    }
    return rank;
  }

  /** The outermost subgraph a node sits in, or null for a node on bare paper. */
  function topSubOf(node, subsById) {
    var sub = node.sub != null ? subsById[node.sub] : null;
    var guard = 0;
    while (sub && sub.parent != null && subsById[sub.parent] && guard++ < 64) sub = subsById[sub.parent];
    return sub ? sub.id : null;
  }

  /**
   * Ranks that keep every top-level subgraph in a band of its own.
   *
   * Plain longest-path ranking puts a node wherever its longest chain ends, so a
   * table a service reads directly lands one rank early — in the repositories'
   * band — and the "tabelas" frame, which wraps all its members, grows across the
   * "repositórios" frame. Two frames over each other read as one smudge. So each
   * subgraph is ranked as a single unit first (as deep as its own inner chain),
   * then its members are ranked inside it: frames can only sit side by side.
   */
  function rankByUnit(model) {
    var subsById = {};
    model.subs.forEach(function (s) { subsById[s.id] = s; });
    var unitOf = {}, members = {}, units = [];
    model.nodes.forEach(function (n) {
      var top = topSubOf(n, subsById);
      var key = top != null ? "s:" + top : "n:" + n.id;
      unitOf[n.id] = key;
      if (!members[key]) { members[key] = []; units.push({ id: key }); }
      members[key].push(n);
    });

    // Inside a unit: its own edges only.
    var inner = {}, depth = {};
    units.forEach(function (u) {
      var list = members[u.id];
      var own = model.edges.filter(function (e) {
        return unitOf[e.from] === u.id && unitOf[e.to] === u.id;
      });
      var r = rankNodes(list, own);
      var deep = 0;
      list.forEach(function (n) { inner[n.id] = r[n.id]; deep = Math.max(deep, r[n.id]); });
      depth[u.id] = deep;
    });

    /* A subgraph far bigger than every other one — 78 endpoints beside 17
     * services — is a single column eight screens tall, and fitting the picture
     * to the panel shrinks everything else to specks. When its members have no
     * chain among themselves, fold it into a few columns inside its own frame,
     * about as tall as the next biggest subgraph. */
    var sizes = units.filter(function (u) { return u.id.charAt(0) === "s"; })
      .map(function (u) { return { id: u.id, n: members[u.id].length }; })
      .sort(function (a, b) { return b.n - a.n; });
    if (sizes.length > 1) {
      var tall = Math.max(FOLD_MIN, sizes[1].n);
      sizes.forEach(function (s) {
        if (depth[s.id] !== 0 || s.n <= tall * FOLD_RATIO) return;
        var cols = Math.ceil(s.n / tall);
        var per = Math.ceil(s.n / cols);
        members[s.id].forEach(function (n, i) { inner[n.id] = Math.floor(i / per); });
        depth[s.id] = cols - 1;
      });
    }

    // Between units: a unit starts after the whole depth of the one feeding it.
    var seen = {}, unitEdges = [];
    model.edges.forEach(function (e) {
      var a = unitOf[e.from], b = unitOf[e.to];
      if (!a || !b || a === b || seen[a + ">" + b]) return;
      seen[a + ">" + b] = true;
      unitEdges.push({ from: a, to: b });
    });
    var back = feedbackEdges(units, unitEdges);
    var forward = unitEdges.filter(function (e, i) { return !back[i]; });
    var base = {};
    units.forEach(function (u) { base[u.id] = 0; });
    for (var pass = 0; pass <= units.length; pass++) {
      var moved = false;
      forward.forEach(function (e) {
        var want = base[e.from] + depth[e.from] + 1;
        if (base[e.to] < want) { base[e.to] = want; moved = true; }
      });
      if (!moved) break;
    }

    var rank = {};
    model.nodes.forEach(function (n) { rank[n.id] = base[unitOf[n.id]] + inner[n.id]; });
    return { rank: rank, unitOf: unitOf };
  }

  /** Barycenter ordering inside each rank — the same idea the map's columns use. */
  function orderRanks(nodes, edges, rank, unitOf) {
    var rows = {};
    nodes.forEach(function (n) {
      (rows[rank[n.id]] = rows[rank[n.id]] || []).push(n.id);
    });
    var ins = {}, outs = {};
    edges.forEach(function (e) {
      (outs[e.from] = outs[e.from] || []).push(e.to);
      (ins[e.to] = ins[e.to] || []).push(e.from);
    });
    var levels = Object.keys(rows).map(Number).sort(function (a, b) { return a - b; });
    var pos = {};
    levels.forEach(function (r) { rows[r].forEach(function (id, i) { pos[id] = i; }); });

    for (var sweep = 0; sweep < 4; sweep++) {
      levels.forEach(function (r) {
        var refs = sweep % 2 ? outs : ins;
        rows[r] = rows[r].slice().sort(function (a, b) {
          return bary(a, refs, pos) - bary(b, refs, pos);
        });
        rows[r].forEach(function (id, i) { pos[id] = i; });
      });
    }
    // Nodes of one subgraph sit together, or its frame would swallow strangers —
    // outermost subgraph first, so a nested frame stays inside its parent's run.
    function frameKey(id) {
      var sub = subOf(nodes, id);
      if (sub == null) return "";
      var unit = unitOf && unitOf[id];
      return (unit && unit.charAt(0) === "s" ? unit : "") + "\u0000" + sub;
    }
    levels.forEach(function (r) {
      rows[r] = rows[r].slice().sort(function (a, b) {
        var sa = frameKey(a), sb = frameKey(b);
        if (sa === sb) return pos[a] - pos[b];
        return sa < sb ? -1 : 1;
      });
      rows[r].forEach(function (id, i) { pos[id] = i; });
    });
    return { rows: rows, levels: levels };
  }

  function subOf(nodes, id) {
    for (var i = 0; i < nodes.length; i++) if (nodes[i].id === id) return nodes[i].sub;
    return null;
  }

  function bary(id, refs, pos) {
    var list = refs[id];
    if (!list || !list.length) return pos[id] == null ? 0 : pos[id];
    var sum = 0, n = 0;
    list.forEach(function (other) { if (pos[other] != null) { sum += pos[other]; n++; } });
    return n ? sum / n : (pos[id] == null ? 0 : pos[id]);
  }

  function place(model, dir) {
    var vertical = dir === "TB" || dir === "BT";
    var ranked = rankByUnit(model);
    var rank = ranked.rank, unitOf = ranked.unitOf;
    var ordered = orderRanks(model.nodes, model.edges, rank, unitOf);
    var byId = {};
    model.nodes.forEach(function (n) { byId[n.id] = n; });
    var labelled = model.edges.some(function (e) { return !!e.label; });

    /* Which frames a band belongs to. Two neighbouring bands of different
     * subgraphs each carry a frame edge (and, top to bottom, a title) into the
     * gap between them, so that gap has to hold both plus clear paper. */
    function framesIn(r) {
      var out = {};
      ordered.rows[r].forEach(function (id) {
        if (unitOf[id].charAt(0) === "s") out[unitOf[id]] = true;
      });
      return out;
    }
    function gapAfter(prev, next) {
      var gap = RANK_GAP + (labelled ? LABEL_GAP : 0);
      var a = framesIn(prev), b = framesIn(next);
      var change = Object.keys(a).some(function (k) { return !b[k]; }) ||
        Object.keys(b).some(function (k) { return !a[k]; });
      if (change) gap += SUB_PAD * 2 + SUB_GAP + (vertical ? SUB_TITLE : 0);
      return gap;
    }

    // Along the rank axis: one band per rank, as deep as its deepest node.
    var bandSize = {}, bandStart = {}, cursor = MARGIN;
    ordered.levels.forEach(function (r, i) {
      var deep = 0;
      ordered.rows[r].forEach(function (id) {
        deep = Math.max(deep, vertical ? byId[id].h : byId[id].w);
      });
      if (i) cursor += gapAfter(ordered.levels[i - 1], r);
      bandSize[r] = deep;
      bandStart[r] = cursor;
      cursor += deep;
    });
    var along = cursor + MARGIN;

    /* Across: pack each rank, centred on the widest rank. Stepping from one
     * subgraph's run into the next leaves room for both frames' padding — and,
     * left to right, for the title the lower frame writes above its first node. */
    function gapBetween(a, b) {
      if (unitOf[a] === unitOf[b] && byId[a].sub === byId[b].sub) return NODE_GAP;
      var framed = byId[a].sub != null || byId[b].sub != null;
      return framed ? NODE_GAP + SUB_PAD * 2 + SUB_GAP + (vertical ? 0 : SUB_TITLE) : NODE_GAP;
    }
    var acrossOf = {}, widest = 0;
    ordered.levels.forEach(function (r) {
      var total = 0;
      ordered.rows[r].forEach(function (id, i) {
        total += (vertical ? byId[id].w : byId[id].h) + (i ? gapBetween(ordered.rows[r][i - 1], id) : 0);
      });
      acrossOf[r] = total;
      widest = Math.max(widest, total);
    });
    var across = widest + MARGIN * 2;

    ordered.levels.forEach(function (r) {
      var at = MARGIN + (widest - acrossOf[r]) / 2;
      ordered.rows[r].forEach(function (id, i) {
        var n = byId[id];
        var deep = bandSize[r];
        if (i) at += gapBetween(ordered.rows[r][i - 1], id);
        if (vertical) {
          n.x = Math.round(at);
          n.y = Math.round(bandStart[r] + (deep - n.h) / 2);
          at += n.w;
        } else {
          n.y = Math.round(at);
          n.x = Math.round(bandStart[r] + (deep - n.w) / 2);
          at += n.h;
        }
      });
    });

    var width = vertical ? across : along;
    var height = vertical ? along : across;

    // BT and RL are the same layout read from the other end.
    if (dir === "BT") {
      model.nodes.forEach(function (n) { n.y = height - n.y - n.h; });
    } else if (dir === "RL") {
      model.nodes.forEach(function (n) { n.x = width - n.x - n.w; });
    }

    // Subgraph frames wrap whatever ended up inside them.
    var frames = [];
    model.subs.forEach(function (sub) {
      var members = model.nodes.filter(function (n) { return n.sub === sub.id; });
      if (!members.length) return;
      var x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
      members.forEach(function (n) {
        x1 = Math.min(x1, n.x); y1 = Math.min(y1, n.y);
        x2 = Math.max(x2, n.x + n.w); y2 = Math.max(y2, n.y + n.h);
      });
      frames.push({
        id: sub.id, label: sub.label,
        x: Math.round(x1 - SUB_PAD), y: Math.round(y1 - SUB_PAD - SUB_TITLE),
        w: Math.round(x2 - x1 + SUB_PAD * 2), h: Math.round(y2 - y1 + SUB_PAD * 2 + SUB_TITLE),
      });
    });
    // A frame may stick out past the content it wraps.
    var minX = 0, minY = 0;
    frames.forEach(function (f) { minX = Math.min(minX, f.x); minY = Math.min(minY, f.y); });
    if (minX < MARGIN || minY < MARGIN) {
      var dx = minX < MARGIN ? MARGIN - minX : 0;
      var dy = minY < MARGIN ? MARGIN - minY : 0;
      model.nodes.forEach(function (n) { n.x += dx; n.y += dy; });
      frames.forEach(function (f) { f.x += dx; f.y += dy; });
      width += dx; height += dy;
    }
    frames.forEach(function (f) {
      width = Math.max(width, f.x + f.w + MARGIN);
      height = Math.max(height, f.y + f.h + MARGIN);
    });

    return { width: Math.round(width), height: Math.round(height), frames: frames, rank: rank };
  }

  /* ----------------------------------------------------------- sequence */

  /* The arrow has to be found before the names, not after: a participant id may
   * contain a hyphen, so anchoring on the id first made `API-->>C` read as an
   * actor called `API-`. Lazy names either side of an explicit arrow, longest
   * arrow alternative first. */
  var SEQ_ARROW = "(?:<<-{1,2}>>|-{1,2}>>|-{1,2}x|-{1,2}\\)|-{1,2}>)";
  var SEQ_MSG = new RegExp("^([^\\s:]+?)\\s*(" + SEQ_ARROW + ")\\s*([^\\s:]+?)\\s*:\\s*([\\s\\S]*)$");
  var ACTOR_GAP = 46, ACTOR_TOP = 30, MSG_GAP = 46, SEQ_HEAD_H = 42;

  function parseSequence(lines, warnings) {
    var actors = [], byId = {}, messages = [], notes = [];

    function actor(id, label) {
      if (byId[id]) {
        if (label) byId[id].label = label;
        return byId[id];
      }
      var a = { id: id, label: label || id };
      byId[id] = a;
      actors.push(a);
      return a;
    }

    lines.forEach(function (raw) {
      var line = raw.replace(/%%.*$/, "").trim();
      if (!line) return;
      var p = /^(participant|actor)\s+([A-Za-z0-9_.\-]+)(?:\s+as\s+(.*))?$/i.exec(line);
      if (p) { actor(p[2], p[3] ? p[3].trim() : null); return; }
      var note = /^note\s+(over|left of|right of)\s+([^:]+):\s*(.*)$/i.exec(line);
      if (note) {
        var who = note[2].split(",").map(function (s) { return s.trim(); }).filter(Boolean);
        who.forEach(function (id) { actor(id, null); });
        notes.push({ over: who, text: note[3].trim(), after: messages.length });
        return;
      }
      var m = SEQ_MSG.exec(line);
      if (m) {
        actor(m[1], null);
        actor(m[3], null);
        messages.push({
          from: m[1], to: m[3], label: m[4].trim(),
          dashed: /^-{2}/.test(m[2]),
          heads: "end",
          lost: /x$/.test(m[2]),
        });
        return;
      }
      if (/^(loop|alt|else|opt|par|and|critical|rect|break|end|activate|deactivate|autonumber|box)\b/i.test(line)) {
        // Blocks change the reading, not the message flow. Dropping them silently
        // would misrepresent the diagram, so say so once.
        if (!warnings.some(function (w) { return w.indexOf("blocos") === 0; })) {
          warnings.push("blocos (loop/alt/opt/par) ainda não são desenhados — as mensagens aparecem em sequência");
        }
        return;
      }
      warnings.push("linha não reconhecida: " + line);
    });

    // Actors across the top, messages down the page.
    var x = MARGIN;
    actors.forEach(function (a) {
      var m = measure(a.label, "rect");
      a.w = Math.max(110, m.w);
      a.h = SEQ_HEAD_H;
      a.lines = m.lines;
      a.x = x;
      a.y = ACTOR_TOP;
      a.cx = x + a.w / 2;
      x += a.w + ACTOR_GAP;
    });
    var y = ACTOR_TOP + SEQ_HEAD_H + 44;
    var noteAt = {};
    notes.forEach(function (n) { (noteAt[n.after] = noteAt[n.after] || []).push(n); });

    function flushNotes(k) {
      (noteAt[k] || []).forEach(function (n) {
        var xs = n.over.map(function (id) { return byId[id]; }).filter(Boolean);
        if (!xs.length) return;
        var left = Math.min.apply(null, xs.map(function (a) { return a.x; }));
        var right = Math.max.apply(null, xs.map(function (a) { return a.x + a.w; }));
        var m = measure(n.text, "rect");
        n.x = Math.round((left + right) / 2 - Math.max(m.w, right - left) / 2);
        n.w = Math.round(Math.max(m.w, right - left));
        n.lines = m.lines;
        n.h = Math.max(34, m.lines.length * 18 + 16);
        n.y = y;
        y += n.h + 22;
      });
    }

    flushNotes(0);
    messages.forEach(function (msg, k) {
      msg.y = y;
      y += MSG_GAP;
      flushNotes(k + 1);
    });

    var width = Math.max(x - ACTOR_GAP + MARGIN, 320);
    var height = y + 30;
    return {
      kind: "sequence", actors: actors, messages: messages, notes: notes,
      width: Math.round(width), height: Math.round(height),
      lifelineTop: ACTOR_TOP + SEQ_HEAD_H, lifelineBottom: Math.round(height - 18),
      warnings: warnings,
    };
  }

  /* -------------------------------------------------------------- entrypoint */

  var KINDS = {
    classdiagram: "classDiagram", erdiagram: "erDiagram", statediagram: "stateDiagram",
    "statediagram-v2": "stateDiagram-v2", gantt: "gantt", pie: "pie", journey: "journey",
    mindmap: "mindmap", timeline: "timeline", quadrantchart: "quadrantChart",
    gitgraph: "gitGraph", c4context: "C4Context", requirementdiagram: "requirementDiagram",
    sankey: "sankey", block: "block", xychart: "xychart",
  };

  /**
   * Parse mermaid source into a positioned model.
   *
   * Never throws: a diagram croqui cannot draw comes back as `kind:"unsupported"`
   * with its source attached, because the tab exists to show what somebody wrote.
   */
  function parse(source) {
    var text = String(source == null ? "" : source).replace(/\r\n?/g, "\n");
    // A ```mermaid fence, front-matter and an init directive are all noise here.
    text = text.replace(/^\s*```[a-zA-Z]*\s*\n?/, "").replace(/\n?```\s*$/, "");
    text = text.replace(/^---\n[\s\S]*?\n---\n/, "");
    text = text.replace(/^\s*%%\{[\s\S]*?\}%%\s*$/gm, "");

    var lines = text.split("\n");
    var head = -1;
    for (var i = 0; i < lines.length; i++) {
      if (lines[i].replace(/%%.*$/, "").trim()) { head = i; break; }
    }
    if (head < 0) return { kind: "empty", warnings: [], source: text };

    var first = lines[head].replace(/%%.*$/, "").trim();
    var warnings = [];

    var flow = FLOW_HEAD.exec(first);
    if (flow) {
      var dir = DIRS[(flow[2] || "TB").toUpperCase()] || "TB";
      var model = parseFlow(lines.slice(head + 1), dir, warnings);
      if (!model.nodes.length) {
        return { kind: "empty", warnings: warnings, source: text };
      }
      var box = place(model, dir);
      return {
        kind: "flowchart", dir: dir,
        nodes: model.nodes, edges: model.edges, frames: box.frames,
        width: box.width, height: box.height, warnings: warnings, source: text,
      };
    }

    if (/^sequenceDiagram\b/i.test(first)) {
      var seq = parseSequence(lines.slice(head + 1), warnings);
      if (!seq.actors.length) return { kind: "empty", warnings: warnings, source: text };
      seq.source = text;
      return seq;
    }

    var word = first.split(/[\s{]/)[0].toLowerCase();
    return {
      kind: "unsupported",
      diagram: KINDS[word] || first.split("\n")[0].slice(0, 40),
      warnings: warnings, source: text,
    };
  }

  root.croquiMermaid = { parse: parse, wrap: wrap, measure: measure };
})(typeof window !== "undefined" ? window : (typeof globalThis !== "undefined" ? globalThis : this));
