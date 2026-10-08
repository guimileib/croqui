/* croqui viewer — hand-drawn SVG, no dependencies.
 *
 * The sketch look comes from the seeded wobble below instead of a drawing
 * library: that is the whole reason this stays a few KB and renders a 120-node
 * map instantly. The same two functions draw the generated map and everything
 * the user draws on top of it — shapes, arrows, text boxes, relations — so the
 * additions never look pasted onto the diagram.
 *
 * Endpoints are collapsed by tag into one box per group. A 70-endpoint API is
 * unreadable as one flat column, so the default view is ~12 groups you expand
 * on click.
 *
 * The map has to outlive the code it describes, and three things fight that. The
 * automatic layout re-sorts every column when one node is added, so saving pins
 * every visible box and a new node settles into free space instead of pushing the
 * others (`pinArrangement`, `settle`). A drawing about a node has to travel with
 * it, so it stores an offset from that node's box rather than a page coordinate
 * (`resolveAnchors`). And reading state — open groups, camera, filters, focus —
 * is not an edit, so it lives outside `state.layout` and outside the dirty
 * comparison (`currentView`). Renames are handled upstream, in `reconcile.py`.
 */
(function () {
  "use strict";

  var SVG = "http://www.w3.org/2000/svg";
  var COLS = ["route", "service", "repo", "table", "external"];
  var SIZE = {
    group: [304, 52],
    route: [304, 46],
    service: [208, 42],
    repo: [208, 42],
    table: [196, 44],
    external: [168, 36],
  };
  var COL_GAP = 122;
  var ROW_GAP = 13;
  var GROUP_GAP = 22;
  /* An open group is drawn as a frame around its endpoints — the collapsed box,
   * opened up. Its heading sits inside the top band, the endpoints are inset by
   * the padding, and the frame's corner is where the collapsed box's corner was. */
  var FRAME_PAD = 10;
  var FRAME_HEAD = 30;
  // Must match `background-size` on #canvas in style.css: paintPaper scales it.
  var GRID = 22;

  /* Drawing tools. `select` manipulates what is already there; every other tool
   * creates one thing and hands control back to `select`. */
  var TOOLS = [
    "select", "rect", "ellipse", "diamond", "triangle", "hexagon",
    "cylinder", "cloud", "arrow", "line", "text", "link",
  ];
  var TOOL_KEYS = {
    v: "select", 1: "select", r: "rect", 2: "rect", o: "ellipse", 3: "ellipse",
    d: "diamond", 4: "diamond", t: "triangle", 5: "triangle", h: "hexagon", 6: "hexagon",
    c: "cylinder", 7: "cylinder", n: "cloud", 8: "cloud", a: "arrow", 9: "arrow",
    l: "line", 0: "line", x: "text", e: "link",
  };
  var LINEAR = { arrow: true, line: true };
  /* A pasted image is a shape like any other — it moves, resizes, sits behind or in
   * front, and anchors to a node. What it does not have is ink or a text box, so
   * `isTextual` is what the drawing code branches on rather than the type. */
  var MAX_PASTE = 520;       // longest edge of a freshly pasted image, in scene units
  var MAX_PASTE_MB = 12;     // must match MAX_IMAGE_BYTES in serve.py
  // Mirrors IMAGE_TYPES in model.py — refusing here means a clear message instead
  // of a 415 from the server.
  var IMAGE_MIME = { "image/png": 1, "image/jpeg": 1, "image/gif": 1, "image/webp": 1 };
  /* Copying in Excalidraw puts its own JSON on the clipboard as text/plain — the
   * same payload a .excalidraw file holds. These are the two wrappers it uses. */
  var EXCALIDRAW_KINDS = { "excalidraw/clipboard": 1, excalidraw: 1 };
  /* Its element types, onto the shapes croqui draws. What is missing here has no
   * counterpart — freehand ink, images, frames, embeds — and is counted and left
   * behind rather than faked with the nearest box. */
  var EXCALIDRAW_SHAPES = {
    rectangle: "rect", diamond: "diamond", ellipse: "ellipse",
    arrow: "arrow", line: "line",
  };
  // Singular and plural, for saying what stayed behind.
  var EXCALIDRAW_NAMES = {
    freedraw: ["rabisco", "rabiscos"],
    image: ["imagem", "imagens"],
    frame: ["moldura", "molduras"],
    magicframe: ["moldura", "molduras"],
    embeddable: ["embed", "embeds"],
    iframe: ["iframe", "iframes"],
  };
  var PASTE_TEXT_W = 320;      // where pasted writing wraps, in scene units
  var PASTE_TEXT_MAX_W = 640;  // ceiling on that, so wide writing is not a ribbon
  var MAX_PASTE_TEXT = 8000;   // longer than this is a document, not a label
  var DEFAULT_SIZE = {
    text: [220, 68], arrow: [170, 0], line: [170, 0], cylinder: [150, 110],
    triangle: [150, 120], cloud: [190, 120],
  };
  var SHAPE_MIN = 22;
  // How far an arrow tip stops short of the box it holds on to, in scene units.
  var END_GAP = 5;
  // How close to a box, in screen pixels, a let-go end has to be to hold on to it.
  var BIND_MARGIN = 14;
  // What a drawing is called when an arrow's end is described as holding on to it.
  var SHAPE_WORD = {
    rect: "retângulo", ellipse: "elipse", diamond: "losango", triangle: "triângulo",
    hexagon: "hexágono", cylinder: "cilindro", cloud: "nuvem", text: "texto", image: "imagem",
  };
  // Font size of a drawing's text, in scene units. Bounded because past either
  // end the text stops being readable at the zoom you drew it at.
  var TEXT_SIZE = 13;
  var TEXT_RANGE = [9, 44];
  var TEXT_STEP = 2;
  var HANDLES = [
    ["nw", 0, 0], ["n", 0.5, 0], ["ne", 1, 0], ["e", 1, 0.5],
    ["se", 1, 1], ["s", 0.5, 1], ["sw", 0, 1], ["w", 0, 0.5],
  ];

  // Named so layout.json stays readable, and so a shape follows the theme.
  var PALETTE = [
    ["ink", "var(--ink)"],
    ["blue", "var(--post)"],
    ["green", "var(--get)"],
    ["amber", "var(--table)"],
    ["orange", "var(--patch)"],
    ["red", "var(--delete)"],
    ["purple", "var(--service)"],
    ["teal", "var(--repo)"],
  ];

  var state = {
    graph: null,
    layout: { positions: {}, hidden: [], shapes: [], links: [] },
    canSave: false,
    expanded: {},
    focus: null,
    query: "",
    methods: new Set(),
    showInternal: false,
    selected: null,
    // Nodes ganged up for a group move, as an id set. Transient on purpose: a
    // selection is not an edit and not a view, so it is neither saved nor undone.
    marked: {},
    // Drawings ganged up with them (Ctrl+A takes everything), by shape id.
    markedShapes: {},
    hover: null,
    // Dimming the rest of the map on hover is useful but intrusive — opt-in.
    hoverHighlight: false,
    view: { x: 0, y: 0, k: 1 },
    boxes: {},
    tool: "select",
    style: { color: "ink", fill: false, dash: false, heads: "end", size: TEXT_SIZE },
    sel: null,       // {kind:"shape"|"link", id} — the drawing you are editing
    editing: null,   // shape id whose text box has the caret
    linkFrom: null,  // source node while a relation is being drawn
    seq: 1,
    renders: 0,
    // name -> data URI, for a bundle. Empty while a server is serving the files.
    images: {},
    pasting: 0,
    // The PRD and UML tabs. Read-only: these are the user's files on disk, and
    // croqui never writes to them — so none of this enters a snapshot or a save.
    docs: { prd: [], uml: [] },
    tab: "map",
    // -1 on the PRD tab is the card gallery: with several PRDs, the tab opens on
    // all of them side by side rather than on whichever sorts first.
    picked: { prd: -1, uml: 0 },
  };

  var el = {};

  /* ------------------------------------------------------------- hand-drawn */

  function seeded(seed) {
    var s = (seed * 2654435761) >>> 0 || 1;
    return function () {
      s = (s * 1664525 + 1013904223) >>> 0;
      return s / 4294967296;
    };
  }

  function wobble(x1, y1, x2, y2, rand, amp) {
    var dx = x2 - x1, dy = y2 - y1;
    var len = Math.hypot(dx, dy) || 1;
    var nx = -dy / len, ny = dx / len;
    var off = (rand() - 0.5) * 2 * amp;
    var j = amp * 0.55;
    return (
      "M" + (x1 + (rand() - 0.5) * j) + "," + (y1 + (rand() - 0.5) * j) +
      "Q" + ((x1 + x2) / 2 + nx * off) + "," + ((y1 + y2) / 2 + ny * off) +
      " " + (x2 + (rand() - 0.5) * j) + "," + (y2 + (rand() - 0.5) * j)
    );
  }

  /** Two overlapping passes of wobbly segments. Corners stay sharp. */
  function sketchPath(pts, seed, amp, close) {
    amp = amp == null ? 1.5 : amp;
    var d = "";
    for (var p = 0; p < 2; p++) {
      var rand = seeded(seed + p * 977);
      for (var i = 0; i + 1 < pts.length; i++) {
        d += wobble(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], rand, amp);
      }
      if (close && pts.length > 2) {
        var last = pts[pts.length - 1];
        d += wobble(last[0], last[1], pts[0][0], pts[0][1], rand, amp);
      }
    }
    return d;
  }

  /** The same two passes, drawn smooth: quadratics through jittered points. */
  function sketchCurve(pts, seed, amp, close) {
    amp = amp == null ? 1.5 : amp;
    var d = "";
    for (var p = 0; p < 2; p++) {
      var rand = seeded(seed + p * 977);
      var q = pts.map(function (pt) {
        return [pt[0] + (rand() - 0.5) * 2 * amp, pt[1] + (rand() - 0.5) * 2 * amp];
      });
      var n = q.length;
      if (n < 2) continue;
      if (close) {
        d += "M" + (q[n - 1][0] + q[0][0]) / 2 + "," + (q[n - 1][1] + q[0][1]) / 2;
        for (var i = 0; i < n; i++) {
          var a = q[i], b = q[(i + 1) % n];
          d += "Q" + a[0] + "," + a[1] + " " + (a[0] + b[0]) / 2 + "," + (a[1] + b[1]) / 2;
        }
      } else {
        d += "M" + q[0][0] + "," + q[0][1];
        for (var j = 1; j < n - 2; j++) {
          d += "Q" + q[j][0] + "," + q[j][1] +
            " " + (q[j][0] + q[j + 1][0]) / 2 + "," + (q[j][1] + q[j + 1][1]) / 2;
        }
        d += "Q" + q[n - 2][0] + "," + q[n - 2][1] + " " + q[n - 1][0] + "," + q[n - 1][1];
      }
    }
    return d;
  }

  /** Points around an ellipse. `bumps` makes a cloud, `flatten` sits it down. */
  function ringPoints(cx, cy, rx, ry, n, bumps, flatten) {
    var pts = [];
    for (var i = 0; i < n; i++) {
      var a = (i / n) * Math.PI * 2;
      var r = bumps ? 1 + 0.13 * Math.cos(a * bumps) : 1;
      var f = flatten && Math.sin(a) > 0 ? 0.84 : 1;
      pts.push([cx + Math.cos(a) * rx * r, cy + Math.sin(a) * ry * r * f]);
    }
    return pts;
  }

  function sketchRect(x, y, w, h, seed, amp) {
    return sketchPath([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], seed, amp, true);
  }

  /** A slightly shaky S-curve between two columns, plus an arrow head. */
  function sketchEdge(x1, y1, x2, y2, seed) {
    var rand = seeded(seed);
    var mid = (x1 + x2) / 2;
    var sag = (rand() - 0.5) * 9;
    var d =
      "M" + x1 + "," + y1 +
      "C" + mid + "," + (y1 + sag) +
      " " + mid + "," + (y2 - sag) +
      " " + (x2 - 7) + "," + y2;
    var a = 6.5;
    d += "M" + (x2 - a - 2) + "," + (y2 - a * 0.72) + "L" + x2 + "," + y2;
    d += "M" + (x2 - a - 2) + "," + (y2 + a * 0.72) + "L" + x2 + "," + y2;
    return d;
  }

  function hashCode(str) {
    var h = 0;
    for (var i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) | 0;
    return Math.abs(h) + 7;
  }

  /* ---------------------------------------------------- shapes you can draw */

  /** Ink for a user shape, in local coordinates: 0,0 → w,h. */
  function shapeOutline(type, w, h, seed, amp) {
    var cx = w / 2, cy = h / 2;
    if (type === "ellipse") {
      return sketchCurve(ringPoints(cx, cy, cx, cy, 18, 0, false), seed, amp, true);
    }
    if (type === "cloud") {
      return sketchCurve(ringPoints(cx, cy, cx * 0.94, cy * 0.9, 20, 5, true), seed, amp, true);
    }
    if (type === "diamond") {
      return sketchPath([[cx, 0], [w, cy], [cx, h], [0, cy]], seed, amp, true);
    }
    if (type === "triangle") {
      return sketchPath([[cx, 0], [w, h], [0, h]], seed, amp, true);
    }
    if (type === "hexagon") {
      var k = Math.min(w * 0.26, 30);
      return sketchPath([[k, 0], [w - k, 0], [w, cy], [w - k, h], [k, h], [0, cy]], seed, amp, true);
    }
    if (type === "cylinder") {
      var ry = Math.max(5, Math.min(h * 0.16, 18));
      var arc = [];
      for (var i = 0; i <= 12; i++) {
        var a = Math.PI * (i / 12);
        arc.push([cx - Math.cos(a) * cx, h - ry + Math.sin(a) * ry]);
      }
      return sketchCurve(ringPoints(cx, ry, cx, ry, 16, 0, false), seed, amp, true) +
        sketchPath([[0, ry], [0, h - ry]], seed + 31, amp) +
        sketchPath([[w, ry], [w, h - ry]], seed + 61, amp) +
        sketchCurve(arc, seed + 97, amp, false);
    }
    return sketchRect(0, 0, w, h, seed, amp);
  }

  /* The tint needs its own clean geometry: the ink above is a pile of separate
   * wobble sub-paths, and filling that would produce slivers, not a shape. */
  function shapeFillEl(type, w, h) {
    var cx = w / 2, cy = h / 2;
    if (type === "ellipse") {
      return svgEl("ellipse", { cx: cx, cy: cy, rx: Math.max(1, cx), ry: Math.max(1, cy) });
    }
    if (type === "diamond") {
      return svgEl("polygon", { points: cx + ",0 " + w + "," + cy + " " + cx + "," + h + " 0," + cy });
    }
    if (type === "triangle") {
      return svgEl("polygon", { points: cx + ",0 " + w + "," + h + " 0," + h });
    }
    if (type === "hexagon") {
      var k = Math.min(w * 0.26, 30);
      return svgEl("polygon", {
        points: k + ",0 " + (w - k) + ",0 " + w + "," + cy + " " + (w - k) + "," + h + " " + k + "," + h + " 0," + cy,
      });
    }
    if (type === "cloud") {
      return svgEl("polygon", {
        points: ringPoints(cx, cy, cx * 0.94, cy * 0.9, 24, 5, true)
          .map(function (p) { return p[0] + "," + p[1]; }).join(" "),
      });
    }
    if (type === "cylinder") {
      var ry = Math.max(5, Math.min(h * 0.16, 18));
      return svgEl("path", {
        d: "M0," + ry + "A" + cx + "," + ry + " 0 0 1 " + w + "," + ry +
          "L" + w + "," + (h - ry) + "A" + cx + "," + ry + " 0 0 1 0," + (h - ry) + "Z",
      });
    }
    return svgEl("rect", { x: 0, y: 0, width: Math.max(1, w), height: Math.max(1, h), rx: 4 });
  }

  function arrowHead(x, y, angle, size) {
    var a = angle + Math.PI;
    return "M" + (x + Math.cos(a - 0.42) * size) + "," + (y + Math.sin(a - 0.42) * size) + "L" + x + "," + y +
      "M" + (x + Math.cos(a + 0.42) * size) + "," + (y + Math.sin(a + 0.42) * size) + "L" + x + "," + y;
  }

  /** A drawn-by-hand stroke between two points, with heads at neither/one/both. */
  function sketchArrow(x1, y1, x2, y2, seed, heads) {
    var dx = x2 - x1, dy = y2 - y1;
    var len = Math.hypot(dx, dy) || 1;
    var nx = -dy / len, ny = dx / len;
    var bow = (seeded(seed)() - 0.5) * Math.min(15, len * 0.09);
    var mx = (x1 + x2) / 2 + nx * bow, my = (y1 + y2) / 2 + ny * bow;
    var d = "";
    for (var p = 0; p < 2; p++) {
      var rand = seeded(seed + p * 977);
      d += "M" + (x1 + (rand() - 0.5)) + "," + (y1 + (rand() - 0.5)) +
        "Q" + mx + "," + my + " " + (x2 + (rand() - 0.5)) + "," + (y2 + (rand() - 0.5));
    }
    var size = Math.min(10, Math.max(5, len * 0.14));
    if (heads === "end" || heads === "both") d += arrowHead(x2, y2, Math.atan2(y2 - my, x2 - mx), size);
    if (heads === "start" || heads === "both") d += arrowHead(x1, y1, Math.atan2(y1 - my, x1 - mx), size);
    return d;
  }

  /** Which sides of two boxes face each other — a relation is not always L→R. */
  function anchorPair(a, b) {
    var acx = a.x + a.w / 2, acy = a.y + a.h / 2;
    var bcx = b.x + b.w / 2, bcy = b.y + b.h / 2;
    var dx = bcx - acx, dy = bcy - acy;
    if (Math.abs(dx) >= Math.abs(dy)) {
      return dx >= 0
        ? { ax: a.x + a.w, ay: acy, bx: b.x, by: bcy, axis: "h", dir: 1 }
        : { ax: a.x, ay: acy, bx: b.x + b.w, by: bcy, axis: "h", dir: -1 };
    }
    return dy >= 0
      ? { ax: acx, ay: a.y + a.h, bx: bcx, by: b.y, axis: "v", dir: 1 }
      : { ax: acx, ay: a.y, bx: bcx, by: b.y + b.h, axis: "v", dir: -1 };
  }

  /** Curve for a hand-made relation: heads, plus where its label goes. */
  function sketchLink(p, seed, heads) {
    var rand = seeded(seed);
    var horiz = p.axis === "h";
    var span = Math.abs(horiz ? p.bx - p.ax : p.by - p.ay);
    var pull = Math.max(30, Math.min(130, span * 0.5));
    var sag = (rand() - 0.5) * 10;
    var c1 = horiz ? [p.ax + pull * p.dir, p.ay + sag] : [p.ax + sag, p.ay + pull * p.dir];
    var c2 = horiz ? [p.bx - pull * p.dir, p.by - sag] : [p.bx - sag, p.by - pull * p.dir];
    var d = "M" + p.ax + "," + p.ay + "C" + c1[0] + "," + c1[1] +
      " " + c2[0] + "," + c2[1] + " " + p.bx + "," + p.by;
    if (heads !== "none") d += arrowHead(p.bx, p.by, Math.atan2(p.by - c2[1], p.bx - c2[0]), 8);
    if (heads === "both") d += arrowHead(p.ax, p.ay, Math.atan2(p.ay - c1[1], p.ax - c1[0]), 8);
    return {
      d: d,
      // The exact middle of a cubic, so the label never drifts off the curve.
      mid: [
        (p.ax + 3 * c1[0] + 3 * c2[0] + p.bx) / 8,
        (p.ay + 3 * c1[1] + 3 * c2[1] + p.by) / 8,
      ],
    };
  }

  function paintOf(key) {
    for (var i = 0; i < PALETTE.length; i++) if (PALETTE[i][0] === key) return PALETTE[i][1];
    return PALETTE[0][1];
  }

  function isLinear(shape) {
    return !!LINEAR[shape.type];
  }

  function isImage(shape) {
    return shape.type === "image";
  }

  /** Can this shape hold writing? Everything that is not a line or an image. */
  function isTextual(shape) {
    return !isLinear(shape) && !isImage(shape);
  }

  /**
   * Where the bytes of a pasted image come from.
   *
   * Served from `.croqui/images/` while you are editing, and inlined as a data URI
   * in a bundle, which has no server behind it. Content-addressed either way, so
   * the name is enough to identify the picture.
   */
  function imageHref(src) {
    if (!src) return null;
    if (state.images && state.images[src]) return state.images[src];
    return "images/" + encodeURIComponent(src);
  }

  /** Arrows keep their direction in the sign of w/h; boxes are normalised. */
  function shapeBox(s) {
    if (!isLinear(s)) return { x: s.x, y: s.y, w: s.w, h: s.h };
    return {
      x: Math.min(s.x, s.x + s.w), y: Math.min(s.y, s.y + s.h),
      w: Math.abs(s.w), h: Math.abs(s.h),
    };
  }

  function shapes() {
    if (!state.layout.shapes) state.layout.shapes = [];
    return state.layout.shapes;
  }

  function links() {
    if (!state.layout.links) state.layout.links = [];
    return state.layout.links;
  }

  function findBy(list, id) {
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i];
    return null;
  }

  function selectedShape() {
    return state.sel && state.sel.kind === "shape" ? findBy(shapes(), state.sel.id) : null;
  }

  function selectedLink() {
    return state.sel && state.sel.kind === "link" ? findBy(links(), state.sel.id) : null;
  }

  /* ------------------------------------------------------- drawings on a node */

  /* A drawing can belong to a node instead of to the paper. `anchor` holds the
   * node id plus the offset from its box; `x`/`y` are then only the last resolved
   * value, kept in the file as the fallback for when that node is gone. Every
   * consumer — render, hit test, drag, fit — keeps reading `x`/`y` and knows
   * nothing about anchors, because they are resolved before anything runs. */

  /** The node a drawing is *about*: the one under its middle.
   *
   *  A drawing that spans two or more nodes is a frame around them rather than a
   *  label on one of them, and belongs to the paper. Counting the node middles it
   *  covers is what tells the two apart — comparing sizes does not, because the
   *  default text box is already larger than a service box. */
  function hostFor(box) {
    var cx = box.x + box.w / 2, cy = box.y + box.h / 2;
    var under = null;
    var spans = 0;
    Object.keys(state.boxes).forEach(function (id) {
      var b = state.boxes[id];
      if (cx >= b.x && cx <= b.x + b.w && cy >= b.y && cy <= b.y + b.h) under = id;
      var bx = b.x + b.w / 2, by = b.y + b.h / 2;
      if (bx >= box.x && bx <= box.x + box.w && by >= box.y && by <= box.y + box.h) spans++;
    });
    return spans > 1 ? null : under;
  }

  /** Where a shape would attach: its current anchor, or the node under it. */
  function anchorTarget(s) {
    if (!s) return null;
    // An arrow held by its ends is placed by those boxes, not by a host.
    if (isLinear(s) && hasEnds(s)) return null;
    if (s.anchor && s.anchor.node) return s.anchor.node;
    return hostFor(shapeBox(s));
  }

  function anchorTo(s, id) {
    var box = state.boxes[id];
    if (!box) return;
    s.anchor = { node: id, dx: Math.round(s.x - box.x), dy: Math.round(s.y - box.y) };
  }

  /** Keep the offset in step after the drawing itself was moved or resized. */
  function reanchor(s) {
    if (!s || !s.anchor || !s.anchor.node) return;
    anchorTo(s, s.anchor.node);
  }

  /** Resolve every anchor against the final boxes. Idempotent by construction:
   *  same positions in, same x/y out, so it never invents an unsaved edit. */
  function resolveAnchors(boxes) {
    shapes().forEach(function (s) {
      if (!s.anchor || !s.anchor.node) return;
      var box = boxes[s.anchor.node];
      // No box means the node is hidden, collapsed into a group or gone from the
      // code. The drawing stays where it last was rather than jumping to 0,0.
      if (!box) return;
      s.x = Math.round(box.x + (s.anchor.dx || 0));
      s.y = Math.round(box.y + (s.anchor.dy || 0));
    });
  }

  /** Drawings on a node follow it live, without paying for a full re-render. */
  function moveAnchored(id) {
    var box = state.boxes[id];
    if (!box) return;
    var moved = {};
    moved[refKey({ node: id })] = true;
    shapes().forEach(function (s) {
      if (!s.anchor || s.anchor.node !== id) return;
      s.x = Math.round(box.x + (s.anchor.dx || 0));
      s.y = Math.round(box.y + (s.anchor.dy || 0));
      applyShapeGeometry(s);
      moved[refKey({ shape: s.id })] = true;
    });
    // Arrows holding on to the node, or to a drawing that rode along with it.
    followEnds(moved);
  }

  /* -------------------------------------------------------- arrow ends on a box */

  /* An arrow or line can hold on to a box by either end — a node, or a drawing of
   * yours — the way Excalidraw's do. `from`/`to` name the box (`{node: id}` or
   * `{shape: id}`); `x`/`y`/`w`/`h` are then only the last resolved run, kept in
   * the file so a box that is hidden or gone leaves the arrow where it was rather
   * than nowhere. The rule is one sentence: **an end resting inside a box, or on
   * its edge, holds on to it.** It is applied when an arrow is drawn, when an end is dragged and when
   * the whole arrow is moved; Ctrl while letting go skips it. */

  function hasEnds(s) {
    return !!(s && (s.from || s.to));
  }

  /** One key per box an end can hold, so a node and a drawing never collide. */
  function refKey(ref) {
    return ref.node ? "n|" + ref.node : "s|" + ref.shape;
  }

  function sameRef(a, b) {
    return !!a && !!b && a.node === b.node && a.shape === b.shape;
  }

  /** The box an end reference points at right now, or null while it is hidden. */
  function endBox(ref) {
    if (!ref) return null;
    if (ref.node) {
      var b = state.boxes[ref.node];
      return b ? { x: b.x, y: b.y, w: b.w, h: b.h, type: "rect" } : null;
    }
    var s = ref.shape ? findBy(shapes(), ref.shape) : null;
    if (!s || isLinear(s)) return null;
    var box = shapeBox(s);
    box.type = s.type;
    return box;
  }

  /** What an end let go at (x, y) would hold on to: the *smallest* box under it,
   *  so a label sitting on a node wins over the node, and a node wins over a frame
   *  drawn around it. Arrows and lines are never targets — an arrow tied to an
   *  arrow has nothing to point at. */
  function endTargetAt(x, y, skipId) {
    /* Inside a box wins, smallest first. Failing that, a box whose edge is within
     * BIND_MARGIN screen pixels: people draw an arrow *to* a card and let go on its
     * border or just short of it, and an arrow that stays loose there is an arrow
     * that falls off the card the first time the card moves. Nearest edge wins. */
    var best = null, area = Infinity, near = null, gap = BIND_MARGIN / (state.view.k || 1);
    function consider(ref, b) {
      if (!b) return;
      var ox = Math.max(b.x - x, 0, x - (b.x + b.w));
      var oy = Math.max(b.y - y, 0, y - (b.y + b.h));
      if (ox === 0 && oy === 0) {
        var a = b.w * b.h;
        if (a < area) { area = a; best = ref; }
        return;
      }
      var d = Math.sqrt(ox * ox + oy * oy);
      if (d <= gap) { gap = d; near = ref; }
    }
    Object.keys(state.boxes).forEach(function (id) { consider({ node: id }, state.boxes[id]); });
    shapes().forEach(function (s) {
      if (s.id === skipId || isLinear(s)) return;
      consider({ shape: s.id }, shapeBox(s));
    });
    return best || near;
  }

  /** Where a straight run from the middle of `box` towards (tx, ty) leaves the
   *  shape, pushed out by END_GAP so the head does not sit on the ink. Ellipses
   *  and diamonds get their own outline; everything else is close enough to its
   *  rectangle. */
  function boundaryPoint(box, tx, ty) {
    var cx = box.x + box.w / 2, cy = box.y + box.h / 2;
    var dx = tx - cx, dy = ty - cy;
    var len = Math.sqrt(dx * dx + dy * dy);
    if (len < 1e-6) return { x: cx, y: cy };
    var hw = box.w / 2, hh = box.h / 2, t;
    if (box.type === "ellipse") {
      t = 1 / Math.sqrt((dx * dx) / (hw * hw) + (dy * dy) / (hh * hh));
    } else if (box.type === "diamond") {
      t = 1 / (Math.abs(dx) / hw + Math.abs(dy) / hh);
    } else {
      t = Math.min(dx ? hw / Math.abs(dx) : Infinity, dy ? hh / Math.abs(dy) : Infinity);
    }
    t += END_GAP / len;
    return { x: cx + dx * t, y: cy + dy * t };
  }

  /** Put a tied arrow's ends on their boxes. Idempotent: the same boxes in give
   *  the same integers out, so a render never invents an unsaved edit. A free end
   *  stays exactly where it was left. */
  function resolveEnds(s) {
    if (!isLinear(s) || !hasEnds(s)) return;
    var A = endBox(s.from), B = endBox(s.to);
    if (!A && !B) return;
    var ax = s.x, ay = s.y, bx = s.x + s.w, by = s.y + s.h;
    // Each tied end aims at the other end: that box's middle if it is tied too,
    // otherwise the point where it was left.
    var aimA = B ? { x: B.x + B.w / 2, y: B.y + B.h / 2 } : { x: bx, y: by };
    var aimB = A ? { x: A.x + A.w / 2, y: A.y + A.h / 2 } : { x: ax, y: ay };
    var a = A ? boundaryPoint(A, aimA.x, aimA.y) : { x: ax, y: ay };
    var b = B ? boundaryPoint(B, aimB.x, aimB.y) : { x: bx, y: by };
    s.x = Math.round(a.x);
    s.y = Math.round(a.y);
    s.w = Math.round(b.x) - s.x;
    s.h = Math.round(b.y) - s.y;
  }

  function resolveAllEnds() {
    shapes().forEach(function (s) { resolveEnds(s); });
  }

  /** Arrows holding on to any of `keys` (see refKey) follow live, mid-drag. */
  function followEnds(keys) {
    shapes().forEach(function (s) {
      if (!hasEnds(s)) return;
      if (!(s.from && keys[refKey(s.from)]) && !(s.to && keys[refKey(s.to)])) return;
      resolveEnds(s);
      applyShapeGeometry(s);
    });
  }

  function keyed(ref) {
    var keys = {};
    keys[refKey(ref)] = true;
    return keys;
  }

  /** Tie `end` ("a" is `from`, "b" is `to`) to whatever box it rests in, if any. */
  function snapEnd(s, end, x, y) {
    var prop = end === "a" ? "from" : "to";
    var ref = endTargetAt(x, y, s.id);
    // Both ends on one box is a loop with nowhere to go.
    if (ref && sameRef(ref, end === "a" ? s.to : s.from)) ref = null;
    if (ref) s[prop] = ref;
    else delete s[prop];
    return ref;
  }

  /** After a gesture on an arrow: tie the ends that landed in a box, and say so.
   *  `which` is "a", "b" or "ab"; `skip` (Ctrl) leaves every end free. */
  function settleEnds(s, which, skip, before) {
    state.hint = null;
    if (!skip) {
      if (which !== "b") snapEnd(s, "a", s.x, s.y);
      if (which !== "a") snapEnd(s, "b", s.x + s.w, s.y + s.h);
    }
    if (hasEnds(s)) {
      // A tied arrow is placed by its boxes; a whole-shape anchor would fight it.
      delete s.anchor;
      resolveEnds(s);
      toast("ligada: " + endsSummary(s) + " — segue as caixas");
    } else if (before) {
      toast("solta — a seta fica onde está");
    }
    return hasEnds(s);
  }

  function releaseEnds(s) {
    delete s.from;
    delete s.to;
  }

  /** Every arrow holding on to a drawing that is going away lets go of it. */
  function releaseEndsOn(shapeId) {
    shapes().forEach(function (s) {
      if (s.from && s.from.shape === shapeId) delete s.from;
      if (s.to && s.to.shape === shapeId) delete s.to;
    });
  }

  /** How a box an end holds is named in the inspector and in toasts. */
  function refName(ref) {
    if (!ref) return "";
    if (ref.node) return nodeName(ref.node);
    var s = findBy(shapes(), ref.shape);
    if (!s) return "desenho";
    var text = s.text ? truncate(s.text.split("\n")[0], 18) : "";
    return text || SHAPE_WORD[s.type] || s.type;
  }

  function endsSummary(s) {
    var f = refName(s.from), t = refName(s.to);
    if (f && t) return f + " → " + t;
    return (f ? "início em " : "fim em ") + (f || t);
  }

  /* ------------------------------------------------------------------ colors */

  function nodeColor(node) {
    if (node.kind === "group") return "var(--group)";
    if (node.kind === "route") {
      var m = (node.meta.method || "GET").toLowerCase();
      return "var(--" + (["get", "post", "patch", "put", "delete"].indexOf(m) >= 0 ? m : "external") + ")";
    }
    return "var(--" + node.kind + ")";
  }

  function columnOf(node) {
    return node.kind === "group" ? "route" : node.kind;
  }

  /* -------------------------------------------------------------- view graph */

  function groupKey(node) {
    return node.group || "—";
  }

  function routePasses(node) {
    if (!state.showInternal && node.meta.internal) return false;
    if (state.methods.size && !state.methods.has(node.meta.method)) return false;
    return true;
  }

  /**
   * Derive the graph actually drawn: routes in collapsed groups are replaced by
   * a single group node, and their edges are remapped onto it.
   */
  function buildView() {
    var hidden = {};
    state.layout.hidden.forEach(function (id) { hidden[id] = true; });

    var nodes = [];
    var proxy = {};   // real route id -> group node id
    var members = {}; // group node id -> [route nodes]

    state.graph.nodes.forEach(function (n) {
      if (hidden[n.id]) return;
      if (n.kind !== "route") {
        nodes.push(n);
        return;
      }
      if (!routePasses(n)) return;
      var key = groupKey(n);
      if (state.expanded[key]) {
        nodes.push(n);
        return;
      }
      var gid = "group:" + key;
      proxy[n.id] = gid;
      (members[gid] = members[gid] || []).push(n);
    });

    Object.keys(members).forEach(function (gid) {
      var list = members[gid];
      var verbs = {};
      list.forEach(function (r) { verbs[r.meta.method] = (verbs[r.meta.method] || 0) + 1; });
      nodes.push({
        id: gid,
        kind: "group",
        label: gid.slice(6),
        group: gid.slice(6),
        meta: { count: list.length, verbs: verbs, routes: list.map(function (r) { return r.id; }) },
      });
    });

    var index = {};
    nodes.forEach(function (n) { index[n.id] = n; });

    // Remap and dedupe edges onto whatever node now stands in for each endpoint.
    var seen = {};
    var edges = [];
    state.graph.edges.forEach(function (e) {
      var from = proxy[e.from] || e.from;
      var to = proxy[e.to] || e.to;
      if (from === to || !index[from] || !index[to]) return;
      var key = from + "\u0000" + to + "\u0000" + e.kind;
      var merged = seen[key];
      if (!merged) {
        merged = {
          from: from, to: to, kind: e.kind,
          confidence: e.confidence,
          ops: (e.ops || []).slice(),
          evidence: (e.evidence || []).slice(0, 1),
        };
        seen[key] = merged;
        edges.push(merged);
      } else {
        (e.ops || []).forEach(function (op) {
          if (merged.ops.indexOf(op) < 0) merged.ops.push(op);
        });
        if (e.confidence === "static") merged.confidence = "static";
      }
    });

    // Relations drawn by hand ride along the same rails: they highlight, they
    // count as part of a chain, they follow a route into its collapsed group.
    var drawn = {};
    links().forEach(function (l) {
      var from = proxy[l.from] || l.from;
      var to = proxy[l.to] || l.to;
      if (from === to || !index[from] || !index[to]) return;
      var key = from + "\u0000" + to + "\u0000" + (l.label || "");
      if (drawn[key]) return;
      drawn[key] = true;
      edges.push({
        from: from, to: to, kind: "link", manual: true, id: l.id,
        label: l.label, color: l.color, dash: l.dash, heads: l.heads, ops: [],
      });
    });

    state.vnodes = nodes;
    state.vindex = index;
    state.vout = {};
    state.vin = {};
    edges.forEach(function (e) {
      (state.vout[e.from] = state.vout[e.from] || []).push(e);
      (state.vin[e.to] = state.vin[e.to] || []).push(e);
    });
    state.vedges = edges;
  }

  function byId(id) {
    return state.vindex[id] || state.rawIndex[id];
  }

  /** Everything reachable from a node, following edges both ways. */
  function chainOf(id) {
    var keep = new Set([id]);
    function walk(cur, map, key) {
      (map[cur] || []).forEach(function (e) {
        var next = e[key];
        if (!keep.has(next)) {
          keep.add(next);
          walk(next, map, key);
        }
      });
    }
    walk(id, state.vout, "to");
    walk(id, state.vin, "from");
    return keep;
  }

  function visibleNodes() {
    if (!state.focus) return state.vnodes;
    var chain = chainOf(state.focus);
    return state.vnodes.filter(function (n) { return chain.has(n.id); });
  }

  function matchesQuery(node) {
    if (!state.query) return true;
    var q = state.query.toLowerCase();
    var parts = [node.label, node.group, node.meta.summary, node.meta.path, node.meta.source, node.meta.schema];
    if (node.kind === "group") {
      (node.meta.routes || []).forEach(function (id) {
        var r = state.rawIndex[id];
        if (r) parts.push(r.meta.path, r.meta.summary);
      });
    }
    return parts.filter(Boolean).join(" ").toLowerCase().indexOf(q) >= 0;
  }

  /* ------------------------------------------------------------------ layout */

  function computeLayout() {
    buildView();
    var nodes = visibleNodes();
    var visible = new Set(nodes.map(function (n) { return n.id; }));
    var buckets = {};
    COLS.forEach(function (k) { buckets[k] = []; });
    nodes.forEach(function (n) { buckets[columnOf(n)].push(n); });

    // Endpoint column: grouped, then alphabetical — a stable reading order.
    buckets.route.sort(function (a, b) {
      var ga = groupKey(a), gb = groupKey(b);
      if (ga !== gb) return ga.localeCompare(gb);
      if (a.kind !== b.kind) return a.kind === "group" ? -1 : 1;
      return (a.meta.path || a.label).localeCompare(b.meta.path || b.label);
    });

    // Later columns follow the barycenter of their parents to cut crossings.
    var indexIn = {};
    buckets.route.forEach(function (n, i) { indexIn[n.id] = i; });
    COLS.slice(1).forEach(function (kind) {
      buckets[kind].sort(function (a, b) {
        var pa = parentBary(a.id, indexIn, visible);
        var pb = parentBary(b.id, indexIn, visible);
        if (pa !== pb) return pa - pb;
        return a.label.localeCompare(b.label);
      });
      buckets[kind].forEach(function (n, i) { indexIn[n.id] = i; });
    });

    var boxes = {};
    var x = 0;
    COLS.forEach(function (kind) {
      var list = buckets[kind];
      if (!list.length) return;
      var w = SIZE[kind][0];
      var y = 0;
      var open = null;   // the open group whose frame we are inside, if any
      list.forEach(function (n, i) {
        // An open group's frame needs its heading band above the first endpoint
        // and its padding below the last — whether the neighbour is another open
        // group or a collapsed box, or the heading ends up under the box above.
        var key = kind === "route" && n.kind === "route" ? groupKey(n) : null;
        if (kind === "route" && key !== open) {
          if (open !== null) y += FRAME_PAD + GROUP_GAP - ROW_GAP;
          if (key !== null) y += (i && open === null ? GROUP_GAP - ROW_GAP : 0) + FRAME_HEAD;
          open = key;
        }
        var bx = key !== null ? x + FRAME_PAD : x;
        boxes[n.id] = { x: bx, y: y, w: w, h: SIZE[n.kind][1], node: n };
        y += SIZE[n.kind][1] + ROW_GAP;
      });
      x += w + COL_GAP + (kind === "route" ? FRAME_PAD * 2 : 0);
    });

    // Vertically centre the short columns against the tallest one.
    var heights = {};
    COLS.forEach(function (kind) {
      var list = buckets[kind];
      if (!list.length) return;
      var last = boxes[list[list.length - 1].id];
      heights[kind] = last.y + last.h;
    });
    var tallest = Math.max.apply(null, [0].concat(Object.keys(heights).map(function (k) { return heights[k]; })));
    COLS.forEach(function (kind) {
      if (heights[kind] == null) return;
      // Rounded: a saved position is an integer, so a half-pixel here would show
      // up as every box in the column nudging on the first save.
      var shift = Math.round((tallest - heights[kind]) / 2);
      if (!shift) return;
      buckets[kind].forEach(function (n) { boxes[n.id].y += shift; });
    });

    // Saved positions always win. Everything above was only needed to decide
    // where a node with *no* saved position goes — and to give `settle` a
    // starting guess that reads well.
    var loose = [];
    Object.keys(boxes).forEach(function (id) {
      var p = state.layout.positions[id];
      if (p && typeof p.x === "number" && typeof p.y === "number") {
        boxes[id].x = p.x;
        boxes[id].y = p.y;
        boxes[id].pinned = true;
      } else {
        loose.push(id);
      }
    });
    var summoned = summonMembers(boxes);
    loose = loose.filter(function (id) { return !summoned[id]; });
    if (loose.length) settle(boxes, loose);
    makeRoomForFrames(boxes);

    state.boxes = boxes;
    // Column titles and group labels are derived from where the boxes actually
    // ended up. Reading them off the automatic pass instead would leave them
    // floating in mid-air the moment a single box was moved by hand.
    var chrome = deriveChrome(boxes);
    state.headers = chrome.headers;
    resolveAnchors(boxes);
    // After the anchors: an arrow may hold on to a drawing that just moved.
    resolveAllEnds();
  }

  /** The endpoints of each open group on the map, by group key. */
  function openMembers(boxes) {
    var out = {};
    Object.keys(boxes).forEach(function (id) {
      var node = boxes[id].node;
      if (node.kind !== "route") return;
      (out[groupKey(node)] = out[groupKey(node)] || []).push(id);
    });
    return out;
  }

  /** The frame around an open group: its endpoints' bounds, plus heading and padding. */
  function frameAround(ids, boxes) {
    var x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
    ids.forEach(function (id) {
      var b = boxes[id];
      if (!b) return;
      x1 = Math.min(x1, b.x); y1 = Math.min(y1, b.y);
      x2 = Math.max(x2, b.x + b.w); y2 = Math.max(y2, b.y + b.h);
    });
    if (x1 === Infinity) return null;
    return {
      x: x1 - FRAME_PAD, y: y1 - FRAME_HEAD,
      w: x2 - x1 + FRAME_PAD * 2, h: y2 - y1 + FRAME_HEAD + FRAME_PAD,
    };
  }

  /**
   * An open group's endpoints come to where the group is.
   *
   * The collapsed box has a place of its own — dragged there, or pinned by a save
   * — and opening it must open it *there*, not scatter its endpoints wherever the
   * automatic column or an old arrangement happens to put them. So the group's
   * saved position is the frame's corner: endpoints that were arranged by hand
   * keep that arrangement and move with the corner; endpoints never placed are
   * stacked inside it in reading order. A group never placed by hand needs none of
   * this — the automatic column already opened it where the box stood.
   *
   * Returns the ids it placed, so `settle` leaves them alone.
   */
  function summonMembers(boxes) {
    var placed = {};
    var groups = openMembers(boxes);
    Object.keys(groups).forEach(function (key) {
      var at = state.layout.positions["group:" + key];
      if (!at || typeof at.x !== "number" || typeof at.y !== "number") return;
      var ids = groups[key].slice().sort(function (a, b) {
        var na = boxes[a].node, nb = boxes[b].node;
        return (na.meta.path || na.label).localeCompare(nb.meta.path || nb.label);
      });
      var saved = ids.filter(function (id) { return boxes[id].pinned; });
      if (saved.length === ids.length) {
        var f = frameAround(ids, boxes);
        var dx = at.x - f.x, dy = at.y - f.y;
        ids.forEach(function (id) { boxes[id].x += dx; boxes[id].y += dy; placed[id] = true; });
        return;
      }
      var y = at.y + FRAME_HEAD;
      ids.forEach(function (id) {
        boxes[id].x = at.x + FRAME_PAD;
        boxes[id].y = y;
        boxes[id].pinned = true;
        y += boxes[id].h + ROW_GAP;
        placed[id] = true;
      });
    });
    return placed;
  }

  /**
   * Open groups push what is below them down, like an accordion.
   *
   * Opening a group asks for room, and a frame drawn over the boxes under it
   * would hide them. Anything in the endpoint column that a frame overlaps — or a
   * frame that overlaps another — slides down until it clears. Boxes that only
   * overlap each other are left exactly where they were put: that is somebody's
   * arrangement, not a side effect of opening a group.
   */
  function makeRoomForFrames(boxes) {
    var groups = openMembers(boxes);
    var units = [];
    Object.keys(groups).forEach(function (key) {
      units.push({ ids: groups[key], frame: true });
    });
    Object.keys(boxes).forEach(function (id) {
      if (boxes[id].node.kind === "group") units.push({ ids: [id], frame: false });
    });
    if (!units.some(function (u) { return u.frame; })) return;
    function extent(u) {
      if (u.frame) return frameAround(u.ids, boxes);
      var b = boxes[u.ids[0]];
      return { x: b.x, y: b.y, w: b.w, h: b.h };
    }
    units.forEach(function (u) { u.box = extent(u); });
    units.sort(function (a, b) { return a.box.y - b.box.y || a.box.x - b.box.x; });
    var done = [];
    units.forEach(function (u) {
      for (var guard = 0; guard < 400; guard++) {
        var hit = null;
        for (var i = 0; i < done.length; i++) {
          var o = done[i];
          if ((u.frame || o.frame) && overlaps(u.box, o.box)) { hit = o; break; }
        }
        if (!hit) break;
        var by = hit.box.y + hit.box.h + GROUP_GAP - u.box.y;
        u.ids.forEach(function (id) { boxes[id].y += by; });
        u.box.y += by;
      }
      done.push(u);
    });
  }

  /** Would `makeRoomForFrames` move anything right now? Tried on a copy. */
  function framesCollide() {
    var copy = {};
    Object.keys(state.boxes).forEach(function (id) {
      var b = state.boxes[id];
      copy[id] = { x: b.x, y: b.y, w: b.w, h: b.h, node: b.node };
    });
    makeRoomForFrames(copy);
    return Object.keys(copy).some(function (id) { return copy[id].y !== state.boxes[id].y; });
  }

  function overlaps(a, b) {
    return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
  }

  /**
   * Find room for the nodes that have no saved position — the ones the code just
   * grew. They start where the automatic layout wanted them and slide down until
   * they stop overlapping anything already placed, so a new endpoint appears next
   * to its neighbours instead of on top of the arrangement you saved.
   *
   * Columns need no special handling: boxes in different columns never overlap in
   * x, so the same test that separates rows separates columns.
   */
  function settle(boxes, loose) {
    var placed = Object.keys(boxes).filter(function (id) { return boxes[id].pinned; });
    loose.sort(function (a, b) { return boxes[a].y - boxes[b].y || boxes[a].x - boxes[b].x; });
    loose.forEach(function (id) {
      var box = boxes[id];
      for (var guard = 0; guard < 500; guard++) {
        var hit = null;
        for (var i = 0; i < placed.length; i++) {
          if (overlaps(box, boxes[placed[i]])) { hit = boxes[placed[i]]; break; }
        }
        if (!hit) break;
        box.y = hit.y + hit.h + ROW_GAP;
      }
      placed.push(id);
    });
  }

  /** Open-group frames and their headings, positioned off the final boxes. */
  function deriveChrome(boxes) {
    var groups = openMembers(boxes);
    // Only an open group needs a heading; a collapsed box says its own name.
    var headers = Object.keys(groups).sort().map(function (key) {
      var f = frameAround(groups[key], boxes);
      return { x: f.x + 12, y: f.y + 19, text: key, group: key, frame: f, count: groups[key].length };
    });
    return { headers: headers };
  }

  /* What each colour means, and how many of each there are — the key to the map,
   * in the corner rather than written over the columns. */
  var LEGEND = [
    ["route", "endpoints", "linear-gradient(90deg, var(--get) 0 20%, var(--post) 20% 40%, var(--patch) 40% 60%, var(--put) 60% 80%, var(--delete) 80%)"],
    ["service", "serviços", "var(--service)"],
    ["repo", "repositórios", "var(--repo)"],
    ["table", "tabelas", "var(--table)"],
    ["external", "externos", "var(--external)"],
  ];

  function renderLegend() {
    if (!el.legend || !state.graph) return;
    var count = {};
    (state.graph.nodes || []).forEach(function (n) { count[n.kind] = (count[n.kind] || 0) + 1; });
    var groups = {};
    (state.graph.nodes || []).forEach(function (n) { if (n.kind === "route") groups[groupKey(n)] = true; });
    el.legend.textContent = "";
    LEGEND.forEach(function (row) {
      var n = count[row[0]] || 0;
      if (!n) return;
      var line = document.createElement("span");
      line.className = "legend-row";
      var sw = document.createElement("i");
      sw.style.background = row[2];
      var name = document.createElement("b");
      name.textContent = row[1];
      var num = document.createElement("em");
      num.textContent = n;
      line.appendChild(sw);
      line.appendChild(name);
      line.appendChild(num);
      if (row[0] === "route") {
        var g = Object.keys(groups).length;
        line.title = "GET · POST · PATCH · PUT · DELETE — " + g + (g === 1 ? " grupo" : " grupos");
      }
      el.legend.appendChild(line);
    });
  }

  /**
   * Re-aim the column titles and group labels at where the boxes are *now*.
   *
   * A drag moves boxes by writing `transform` straight onto the elements, with no
   * full render — that is what keeps twenty boxes smooth under the cursor. The
   * chrome is not one of those elements, so without this it stays behind, and a
   * group heading parked over empty paper names nothing. Same maths as the render
   * path, only the existing text nodes are moved instead of rebuilt.
   */
  function positionChrome() {
    if (!state.chromeEls) return;
    var chrome = deriveChrome(state.boxes);
    state.headers = chrome.headers;
    chrome.headers.forEach(function (h) {
      var t = state.chromeEls.groups[h.group];
      if (t) {
        t.setAttribute("x", h.x);
        t.setAttribute("y", h.y);
      }
      var fr = state.chromeEls.frames && state.chromeEls.frames[h.group];
      if (fr) shapeFrame(fr, h);
    });
  }

  /** Put an open group's frame elements on `h.frame`. */
  function shapeFrame(parts, h) {
    var f = h.frame;
    parts.fill.setAttribute("x", f.x);
    parts.fill.setAttribute("y", f.y);
    parts.fill.setAttribute("width", f.w);
    parts.fill.setAttribute("height", f.h);
    parts.ink.setAttribute("d", sketchRect(f.x, f.y, f.w, f.h, hashCode("frame:" + h.group), 1.3));
    parts.count.setAttribute("x", f.x + f.w - 12);
    parts.count.setAttribute("y", f.y + 19);
  }

  function parentBary(id, indexIn, visible) {
    // Hand-made relations are deliberately ignored here: drawing one should not
    // reshuffle the columns under you.
    var parents = (state.vin[id] || []).filter(function (e) {
      return !e.manual && visible.has(e.from) && indexIn[e.from] != null;
    });
    if (!parents.length) return 1e6;
    return parents.reduce(function (a, e) { return a + indexIn[e.from]; }, 0) / parents.length;
  }

  /* ------------------------------------------------------------------ render */

  function svgEl(name, attrs) {
    var node = document.createElementNS(SVG, name);
    for (var k in attrs) if (attrs[k] != null) node.setAttribute(k, attrs[k]);
    return node;
  }

  function truncate(text, maxChars) {
    if (text.length <= maxChars) return text;
    if (maxChars < 6) return text.slice(0, maxChars);
    // Keep the tail: the specific part of a path matters more than the prefix.
    var tail = Math.ceil((maxChars - 1) * 0.68);
    return text.slice(0, maxChars - 1 - tail) + "…" + text.slice(-tail);
  }

  function render() {
    state.renders++;
    computeLayout();
    var root = el.scene;
    root.textContent = "";

    // Drawings sit under the map by default — a box you draw around a chain is a
    // frame, not a lid. `à frente` moves one on top.
    var gDeco = svgEl("g", {});
    var gBack = svgEl("g", {});
    var gEdges = svgEl("g", {});
    var gNodes = svgEl("g", {});
    var gFront = svgEl("g", {});
    el.overlay = svgEl("g", { class: "overlay" });
    root.appendChild(gDeco);
    root.appendChild(gBack);
    root.appendChild(gEdges);
    root.appendChild(gNodes);
    root.appendChild(gFront);
    root.appendChild(el.overlay);
    state.after = [];

    // No column titles on the paper: the colours already say what each column is,
    // and the legend in the corner is the key — name, colour and how many.
    state.chromeEls = { groups: {}, frames: {} };
    renderLegend();
    // An open group is its collapsed box opened up: the same fill, now framing
    // the endpoints. Grab the frame and the whole group comes along.
    state.headers.forEach(function (h) {
      var g = svgEl("g", { class: "gframe", "data-frame": h.group });
      var parts = {
        fill: svgEl("rect", {
          class: "fill", rx: 8, fill: "var(--group)", "fill-opacity": 0.07,
        }),
        ink: svgEl("path", {
          class: "ink", fill: "none", stroke: "var(--group)", "stroke-width": 1.5,
          "stroke-dasharray": "7 5", opacity: 0.8,
        }),
        count: svgEl("text", { class: "gcount", "text-anchor": "end" }),
      };
      parts.count.textContent = h.count + (h.count === 1 ? " endpoint" : " endpoints");
      g.appendChild(parts.fill);
      g.appendChild(parts.ink);
      g.appendChild(parts.count);
      var t = svgEl("text", { class: "grouplabel collapse", x: h.x, y: h.y, "data-collapse": h.group });
      t.textContent = "▾ " + h.text;
      g.appendChild(t);
      shapeFrame(parts, h);
      gDeco.appendChild(g);
      state.chromeEls.groups[h.group] = t;
      state.chromeEls.frames[h.group] = parts;
    });

    state.edgeEls = [];
    state.linkEls = {};
    state.vedges.forEach(function (e) {
      var a = state.boxes[e.from], b = state.boxes[e.to];
      if (!a || !b) return;
      if (e.manual) { renderLink(e, gEdges); return; }
      var write = e.kind === "write";
      var path = svgEl("path", {
        class: "edge",
        d: sketchEdge(a.x + a.w, a.y + a.h / 2, b.x, b.y + b.h / 2, hashCode(e.from + e.to)),
        stroke: nodeColor(byId(e.from)),
        "stroke-width": write ? 1.9 : 1.25,
        "stroke-dasharray": e.confidence !== "static" ? "5 4" : null,
        opacity: write ? 0.72 : 0.5,
      });
      gEdges.appendChild(path);
      state.edgeEls.push({ edge: e, path: path, el: path });
    });

    state.nodeEls = {};
    Object.keys(state.boxes).forEach(function (id) {
      var box = state.boxes[id];
      var node = box.node;
      var color = nodeColor(node);
      var g = svgEl("g", { class: "node", "data-id": id, transform: "translate(" + box.x + "," + box.y + ")" });

      g.appendChild(svgEl("rect", {
        class: "fill", x: 0, y: 0, width: box.w, height: box.h, rx: 4,
        fill: color, "fill-opacity": node.kind === "group" ? 0.14 : 0.1,
      }));
      g.appendChild(svgEl("path", {
        // Classed so the selection ring can thicken this outline instead of
        // drawing a second, straighter rectangle beside the sketched one.
        class: "ink",
        d: sketchRect(0, 0, box.w, box.h, hashCode(id), node.meta.verified === false ? 2.6 : 1.5),
        fill: "none", stroke: color, "stroke-width": node.kind === "group" ? 2 : 1.7,
        "stroke-dasharray": node.meta.verified === false ? "6 4" : null,
      }));

      if (node.kind === "group") {
        var gname = svgEl("text", { class: "title", x: 26, y: 22, "font-size": 13.5, fill: "var(--ink)" });
        gname.textContent = truncate(node.label, 26);
        g.appendChild(gname);
        var chev = svgEl("text", { x: 11, y: 22, "font-size": 12, fill: color });
        chev.textContent = "▸";
        g.appendChild(chev);
        var count = svgEl("text", { x: 26, y: 39, "font-size": 10.5, fill: "var(--ink-soft)" });
        count.textContent = node.meta.count + (node.meta.count === 1 ? " endpoint" : " endpoints");
        g.appendChild(count);
        // Verb tally, so a collapsed group still shows its read/write mix.
        var vx = box.w - 11;
        ["DELETE", "PUT", "PATCH", "POST", "GET"].forEach(function (verb) {
          var n = node.meta.verbs[verb];
          if (!n) return;
          var tag = svgEl("text", {
            x: vx, y: 39, "font-size": 9.5, "text-anchor": "end",
            fill: "var(--" + verb.toLowerCase() + ")", "font-weight": 700,
          });
          tag.textContent = verb.slice(0, 3) + " " + n;
          g.appendChild(tag);
          vx -= 42;
        });
      } else if (node.kind === "route") {
        var badge = svgEl("text", { class: "title", x: 11, y: 19, "font-size": 11.5, fill: color, "letter-spacing": "0.04em" });
        badge.textContent = node.meta.method;
        g.appendChild(badge);
        var p = svgEl("text", { x: 11, y: 35, "font-size": 12.5, fill: "var(--ink)", "font-family": "var(--font-mono)" });
        p.textContent = truncate(node.meta.path || node.label, Math.floor((box.w - 22) / 7.5));
        g.appendChild(p);
      } else if (node.kind === "table") {
        var sch = svgEl("text", { x: 11, y: 17, "font-size": 10, fill: "var(--ink-faint)", "letter-spacing": "0.05em" });
        sch.textContent = (node.meta.schema || "public") + (node.meta.object_kind === "matview" ? " · matview" : "");
        g.appendChild(sch);
        var nm = svgEl("text", { class: "title", x: 11, y: 33, "font-size": 12.5, fill: "var(--ink)", "font-family": "var(--font-mono)" });
        nm.textContent = truncate(node.label, Math.floor((box.w - 22) / 7.5));
        g.appendChild(nm);
      } else {
        var lbl = svgEl("text", { class: "title", x: 11, y: box.h / 2 + 4.5, "font-size": 12.5, fill: "var(--ink)" });
        lbl.textContent = truncate(node.label, Math.floor((box.w - 22) / 6.9));
        g.appendChild(lbl);
      }

      if (node.meta.unresolved) {
        var mark = svgEl("text", { x: box.w - 15, y: 17, "font-size": 13, fill: "var(--patch)" });
        mark.textContent = "?";
        g.appendChild(mark);
      }

      gNodes.appendChild(g);
      state.nodeEls[id] = g;
    });

    state.shapeEls = {};
    shapes().forEach(function (s) {
      renderShape(s, s.front ? gFront : gBack);
    });

    renderOverlay();
    applyEmphasis();
    // The ring lives on elements this pass just replaced.
    refreshMarks();
    state.after.forEach(function (fn) { fn(); });
    state.after = [];
  }

  /* ----------------------------------------------------------- user drawings */

  function renderShape(s, layer) {
    var paint = paintOf(s.color);
    var box = shapeBox(s);
    var picked = state.sel && state.sel.kind === "shape" && state.sel.id === s.id;
    var g = svgEl("g", {
      class: "shape" + (picked ? " picked" : ""),
      "data-shape": s.id,
      transform: "translate(" + s.x + "," + s.y + ")",
    });
    var refs = { g: g, shape: s };

    if (isImage(s)) {
      // `meet` rather than `none`: an image you resize should letterbox inside its
      // box, never stretch. The box starts at the picture's own aspect ratio, so
      // there is nothing to letterbox until you deliberately reshape it.
      refs.img = svgEl("image", {
        class: "img", x: 0, y: 0, width: Math.max(1, s.w), height: Math.max(1, s.h),
        preserveAspectRatio: "xMidYMid meet",
      });
      var href = imageHref(s.src);
      if (href) {
        refs.img.setAttributeNS("http://www.w3.org/1999/xlink", "href", href);
        refs.img.setAttribute("href", href);
      }
      g.appendChild(refs.img);
      // A missing file must say so instead of leaving an invisible hole you can
      // still select and drag around.
      refs.ink = svgEl("path", {
        class: "ink frame", d: sketchRect(0, 0, s.w, s.h, hashCode(s.id), 1.2),
        fill: "none", stroke: "var(--ink-faint)", "stroke-width": 1.2,
      });
      g.appendChild(refs.ink);
      refs.img.addEventListener("error", function () {
        g.classList.add("missing");
        if (s.alt) refs.img.setAttribute("aria-label", s.alt);
      });
      layer.appendChild(g);
      state.shapeEls[s.id] = refs;
      return;
    }

    if (isLinear(s)) {
      var d = sketchArrow(0, 0, s.w, s.h, hashCode(s.id), s.type === "arrow" ? (s.heads || "end") : "none");
      // A 2px stroke is impossible to grab; a fat invisible twin is not.
      refs.hit = svgEl("path", { class: "hit", d: d, fill: "none", stroke: "transparent", "stroke-width": 16 });
      refs.ink = svgEl("path", {
        class: "ink", d: d, fill: "none", stroke: paint, "stroke-width": 1.9,
        "stroke-linecap": "round", "stroke-dasharray": s.dash ? "7 5" : null,
      });
      g.appendChild(refs.hit);
      g.appendChild(refs.ink);
    } else {
      var outline = shapeOutline(s.type, s.w, s.h, hashCode(s.id), 1.6);
      refs.fill = shapeFillEl(s.type, s.w, s.h);
      refs.fill.setAttribute("class", "fill");
      refs.fill.setAttribute("fill", s.fill ? paint : "transparent");
      refs.fill.setAttribute("fill-opacity", s.fill ? 0.13 : 1);
      // An unfilled shape is grabbed by its ink, like a real pencil drawing;
      // a filled one (or a text box) is grabbed anywhere inside. This is an
      // attribute rather than a style so the drawing-mode CSS can still win.
      refs.fill.setAttribute("pointer-events", s.fill || s.type === "text" ? "all" : "none");
      g.appendChild(refs.fill);
      refs.hit = svgEl("path", { class: "hit", d: outline, fill: "none", stroke: "transparent", "stroke-width": 14 });
      g.appendChild(refs.hit);
      refs.ink = svgEl("path", {
        class: "ink", d: outline, fill: "none", stroke: paint,
        "stroke-width": s.type === "text" ? 1.4 : 1.8,
        "stroke-dasharray": s.dash ? "7 5" : null,
      });
      // A bare text box shows no frame — until it is empty or selected, when a
      // faint one is the only thing telling you it is there. `quieten` keeps that
      // live while you type; see it in textEditor's input handler.
      if (s.type === "text" && !s.fill) {
        refs.ink.setAttribute("class", "ink ghost");
        quieten(s, refs);
      }
      g.appendChild(refs.ink);
      refs.editor = textEditor(s, box, paint, refs);
      g.appendChild(refs.editor.fo);
    }

    layer.appendChild(g);
    state.shapeEls[s.id] = refs;
  }

  /** The locating frame of a bare text box, hidden the moment there is text to
   *  see instead. Called on every keystroke, not just on render: waiting for the
   *  next full pass meant typing inside a dashed box until you clicked away. */
  function quieten(s, refs) {
    if (!refs || !refs.ink || s.type !== "text" || s.fill) return;
    refs.ink.classList.toggle("quiet", !!s.text);
  }

  /**
   * Move a shape by dragging its own text, while the caret is still in it.
   *
   * The textarea has to swallow the gesture — otherwise the canvas would treat a
   * click meant for the caret as the start of a drag — so the move is driven from
   * here instead. Past a few pixels it stops being a caret placement and becomes a
   * drag, which is what makes a bare label movable without first clicking off it.
   */
  function dragFromText(s, ta, ev) {
    var startX = ev.clientX, startY = ev.clientY;
    var originX = s.x, originY = s.y;
    var pre = snapshot();
    var moving = false;

    function onMove(e) {
      if (!moving) {
        if (Math.abs(e.clientX - startX) + Math.abs(e.clientY - startY) < 4) return;
        moving = true;
        // Leave edit mode *without* rendering: the blur handler bails out once
        // `editing` no longer names this shape, and a render here would detach
        // the very elements this gesture is holding.
        state.editing = null;
        ta.blur();
        el.canvas.classList.add("dragging");
      }
      s.x = Math.round(originX + (e.clientX - startX) / state.view.k);
      s.y = Math.round(originY + (e.clientY - startY) / state.view.k);
      reanchor(s);
      applyShapeGeometry(s);
      renderOverlay();
    }

    function onUp() {
      window.removeEventListener("pointermove", onMove, true);
      window.removeEventListener("pointerup", onUp, true);
      window.removeEventListener("pointercancel", onUp, true);
      // Never moved: it was a click, and the caret is already where it landed.
      if (!moving) return;
      el.canvas.classList.remove("dragging");
      pushHistory(pre);
      markDirty();
      render();
      refreshInspector();
    }

    // Capture keeps the gesture alive once the pointer leaves the few pixels of
    // text it started on, which it does immediately. Window listeners cover the
    // rest either way, so a browser without it still works.
    if (ta.setPointerCapture) {
      try {
        ta.setPointerCapture(ev.pointerId);
      } catch (err) { /* nothing to fall back to; the listeners below suffice */ }
    }
    window.addEventListener("pointermove", onMove, true);
    window.addEventListener("pointerup", onUp, true);
    window.addEventListener("pointercancel", onUp, true);
  }

  /* The text of a shape is a real <textarea> at all times — read-only and
   * click-through until you double-click it. Swapping elements on every edit
   * would mean fighting focus; this way the caret lands and stays. */
  function textEditor(s, box, paint, refs) {
    var pad = s.type === "text" ? 8 : 11;
    var editing = state.editing === s.id;
    var centred = s.type !== "text";
    var fo = svgEl("foreignObject", {
      x: pad, y: pad,
      width: Math.max(6, box.w - pad * 2), height: Math.max(6, box.h - pad * 2),
    });
    var ta = document.createElement("textarea");
    ta.value = s.text || "";
    ta.readOnly = !editing;
    ta.spellcheck = false;
    if (editing) ta.placeholder = "texto…";
    ta.style.cssText =
      "width:100%;height:100%;border:0;background-color:transparent;resize:none;outline:none;overflow:hidden;" +
      "font:" + (s.size || TEXT_SIZE) + "px/1.35 var(--font);padding:0;margin:0;" +
      "text-align:" + (centred ? "center" : "left") + ";" +
      "color:" + paint + ";pointer-events:" + (editing ? "auto" : "none") + ";" +
      "cursor:text;user-select:" + (editing ? "text" : "none");

    function centre() {
      if (!centred) return;
      ta.style.paddingTop = "0px";
      var free = ta.clientHeight - ta.scrollHeight;
      if (free > 0) ta.style.paddingTop = free / 2 + "px";
    }

    // Typing past the bottom of a text box grows it rather than hiding the text.
    function grow() {
      if (s.type !== "text") return;
      var need = ta.scrollHeight + pad * 2 + 2;
      if (need <= s.h) return;
      s.h = Math.ceil(need);
      applyShapeGeometry(s);
      renderOverlay();
    }

    var preEdit = null;
    ta.addEventListener("focus", function () { preEdit = snapshot(); });
    ta.addEventListener("blur", function () {
      preEdit = null;
      if (state.editing !== s.id) return;
      state.editing = null;
      render();
      refreshInspector();
    });
    ta.addEventListener("input", function () {
      // One history entry per editing session, not per keystroke.
      if (preEdit !== null) { pushHistory(preEdit); preEdit = null; }
      s.text = ta.value;
      grow();
      centre();
      quieten(s, refs);
      markDirty();
    });
    ta.addEventListener("pointerdown", function (ev) {
      // The canvas must not see this: it would read a click meant for the caret
      // as the start of a drag. Moving is handled from here instead.
      ev.stopPropagation();
      if (ev.button || state.editing !== s.id) return;
      dragFromText(s, ta, ev);
    });
    ta.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") { ev.stopPropagation(); ta.blur(); }
    });
    fo.appendChild(ta);
    refs.ta = ta;

    state.after.push(function () {
      centre();
      if (!editing) return;
      ta.focus();
      var end = ta.value.length;
      try { ta.setSelectionRange(end, end); } catch (err) { /* not focusable yet */ }
    });
    return { fo: fo, ta: ta, centre: centre, grow: grow };
  }

  /** Repaint one shape in place — used while dragging, resizing and typing. */
  function applyShapeGeometry(s) {
    var refs = state.shapeEls[s.id];
    if (!refs) return;
    refs.g.setAttribute("transform", "translate(" + s.x + "," + s.y + ")");
    if (isImage(s)) {
      if (refs.img) {
        refs.img.setAttribute("width", Math.max(1, s.w));
        refs.img.setAttribute("height", Math.max(1, s.h));
      }
      if (refs.ink) refs.ink.setAttribute("d", sketchRect(0, 0, s.w, s.h, hashCode(s.id), 1.2));
      return;
    }
    if (isLinear(s)) {
      var d = sketchArrow(0, 0, s.w, s.h, hashCode(s.id), s.type === "arrow" ? (s.heads || "end") : "none");
      refs.ink.setAttribute("d", d);
      refs.hit.setAttribute("d", d);
      return;
    }
    var outline = shapeOutline(s.type, s.w, s.h, hashCode(s.id), 1.6);
    refs.ink.setAttribute("d", outline);
    refs.hit.setAttribute("d", outline);
    var fresh = shapeFillEl(s.type, s.w, s.h);
    for (var i = 0; i < fresh.attributes.length; i++) {
      var attr = fresh.attributes[i];
      if (attr.name !== "class") refs.fill.setAttribute(attr.name, attr.value);
    }
    if (refs.editor) {
      var pad = s.type === "text" ? 8 : 11;
      refs.editor.fo.setAttribute("width", Math.max(6, s.w - pad * 2));
      refs.editor.fo.setAttribute("height", Math.max(6, s.h - pad * 2));
      refs.editor.centre();
    }
  }

  function renderLink(e, layer) {
    var a = state.boxes[e.from], b = state.boxes[e.to];
    var picked = state.sel && state.sel.kind === "link" && state.sel.id === e.id;
    var paint = e.color ? paintOf(e.color) : "var(--ink-soft)";
    var geo = sketchLink(anchorPair(a, b), hashCode(e.id || e.from + e.to), e.heads || "end");
    var g = svgEl("g", { class: "link" + (picked ? " picked" : ""), "data-link": e.id });
    var hit = svgEl("path", { class: "hit", d: geo.d, fill: "none", stroke: "transparent", "stroke-width": 16 });
    var path = svgEl("path", {
      class: "ink", d: geo.d, fill: "none", stroke: paint, "stroke-width": 1.7,
      "stroke-linecap": "round", "stroke-dasharray": e.dash ? "7 5" : null,
    });
    g.appendChild(hit);
    g.appendChild(path);

    var bg = null, label = null;
    if (e.label) {
      bg = svgEl("rect", { class: "linklabel-bg", x: 0, y: 0, rx: 3, height: 16 });
      label = svgEl("text", { class: "linklabel", x: 0, y: 0, "text-anchor": "middle", fill: paint });
      label.textContent = e.label;
      g.appendChild(bg);
      g.appendChild(label);
    }
    layer.appendChild(g);
    var refs = { g: g, path: path, hit: hit, label: label, bg: bg, edge: e };
    state.linkEls[e.id] = refs;
    positionLinkLabel(refs, geo);
    state.edgeEls.push({ edge: e, path: path, el: g, link: refs });
  }

  function positionLinkLabel(refs, geo) {
    if (!refs.label) return;
    var text = refs.edge.label || "";
    refs.label.style.display = text ? "" : "none";
    refs.bg.style.display = text ? "" : "none";
    var w = text.length * 6.1 + 12;
    refs.label.setAttribute("x", geo.mid[0]);
    refs.label.setAttribute("y", geo.mid[1] + 4);
    refs.bg.setAttribute("x", geo.mid[0] - w / 2);
    refs.bg.setAttribute("y", geo.mid[1] - 8);
    refs.bg.setAttribute("width", w);
  }

  /** Selection frame and drag handles, sized in screen pixels at any zoom. */
  function renderOverlay() {
    if (!el.overlay) return;
    el.overlay.textContent = "";
    var k = state.view.k || 1;
    if (state.draft) el.overlay.appendChild(state.draft);
    // The box an arrow end would hold on to if let go here.
    var hint = state.hint ? endBox(state.hint) : null;
    if (hint) {
      el.overlay.appendChild(svgEl("rect", {
        class: "bindhint", x: hint.x - 3 / k, y: hint.y - 3 / k,
        width: hint.w + 6 / k, height: hint.h + 6 / k, rx: 4 / k, "stroke-width": 1.2 / k,
      }));
    }
    var s = state.tool === "select" ? selectedShape() : null;
    if (!s) return;
    var r = 4.5 / k;
    if (isLinear(s)) {
      // A filled handle is an end that is holding on to something.
      [[s.x, s.y, "a", s.from], [s.x + s.w, s.y + s.h, "b", s.to]].forEach(function (p) {
        el.overlay.appendChild(svgEl("circle", {
          class: "handle" + (p[3] ? " tied" : ""), "data-handle": p[2],
          cx: p[0], cy: p[1], r: r + 0.6, "stroke-width": 1.4 / k,
        }));
      });
      return;
    }
    var b = shapeBox(s);
    el.overlay.appendChild(svgEl("rect", {
      class: "selbox", x: b.x - 4 / k, y: b.y - 4 / k,
      width: b.w + 8 / k, height: b.h + 8 / k, "stroke-width": 1 / k,
    }));
    HANDLES.forEach(function (h) {
      el.overlay.appendChild(svgEl("rect", {
        class: "handle", "data-handle": h[0],
        x: b.x + b.w * h[1] - r, y: b.y + b.h * h[2] - r,
        width: r * 2, height: r * 2, "stroke-width": 1.3 / k,
      }));
    });
  }

  function addShape(type, x, y, w, h, opts) {
    opts = opts || {};
    // A batched call is one of many inside a single paste: the caller owns the
    // history entry and the one render at the end of it.
    if (!opts.batch) pushHistory();
    var s = {
      id: "sh-" + Date.now().toString(36) + "-" + state.seq++,
      type: type, x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h),
      text: "",
      // The pencil in the rail is the default; a paste brings its own styling and
      // says so element by element.
      color: opts.color || state.style.color,
      fill: opts.fill == null ? state.style.fill : !!opts.fill,
      dash: opts.dash == null ? state.style.dash : !!opts.dash,
    };
    if (isLinear(s)) {
      s.heads = opts.heads || (type === "arrow" ? state.style.heads : "none");
      delete s.fill;
      delete s.text;
    } else if (isImage(s)) {
      // An image has no ink and no writing, so none of the pencil styling applies.
      delete s.text;
      delete s.fill;
      delete s.color;
      delete s.dash;
      s.src = opts.src;
      if (opts.alt) s.alt = opts.alt;
    } else {
      s.size = opts.size || state.style.size;
      if (opts.text) s.text = opts.text;
    }
    if (opts.front) s.front = true;
    // A drawing dropped on a node is about that node, so it travels with it —
    // through a drag, through a re-layout, and through the next re-scan.
    // An arrow drawn from one box to another holds on to them by its ends
    // instead; `snap` is the drawing tool saying it was drawn by hand.
    var tied = isLinear(s) && !!opts.snap && settleEnds(s, "ab", false, false);
    var host = tied ? null : hostFor(shapeBox(s));
    if (host) anchorTo(s, host);
    shapes().push(s);
    if (opts.batch) return s;
    state.sel = { kind: "shape", id: s.id };
    state.editing = opts.edit ? s.id : null;
    markDirty();
    render();
    refreshInspector();
    if (host) toast("preso a " + nodeName(host) + " — segue o nó");
    return s;
  }

  /** How a node should be named in a message: its path if it has one. */
  function nodeName(id) {
    var node = byId(id);
    if (!node) return id;
    return truncate(node.kind === "route" ? (node.meta.path || node.label) : node.label, 24);
  }

  function toggleAnchor() {
    var s = selectedShape();
    if (!s) return;
    if (isLinear(s) && hasEnds(s)) {
      pushHistory();
      releaseEnds(s);
      toast("solta — a seta fica onde está");
      markDirty();
      render();
      refreshInspector();
      return;
    }
    var host = anchorTarget(s);
    if (!host) return;
    pushHistory();
    if (s.anchor && s.anchor.node) {
      delete s.anchor;
      toast("solto — o desenho fica onde está");
    } else {
      anchorTo(s, host);
      toast("preso a " + nodeName(host) + " — segue o nó");
    }
    markDirty();
    render();
    refreshInspector();
  }

  function addLink(from, to) {
    if (from === to) return;
    var exists = links().some(function (l) { return l.from === from && l.to === to; });
    if (exists) { toast("essa relação já existe"); return; }
    pushHistory();
    var l = {
      id: "ln-" + Date.now().toString(36) + "-" + state.seq++,
      from: from, to: to, label: "",
      color: state.style.color, dash: state.style.dash, heads: state.style.heads,
    };
    links().push(l);
    state.sel = { kind: "link", id: l.id };
    markDirty();
    render();
    refreshInspector();
    // A relation anchored to a collapsed group belongs to the group box, and
    // there is no box to hang it on once the group is opened.
    var onGroup = from.indexOf("group:") === 0 || to.indexOf("group:") === 0;
    toast(onGroup ? "relação presa ao grupo — some se você expandir" : "relação criada — dê um rótulo a ela");
    if (el.insLabel) el.insLabel.focus();
  }

  function deleteSelection() {
    if (!state.sel) return;
    var list = state.sel.kind === "shape" ? shapes() : links();
    var i = list.findIndex(function (item) { return item.id === state.sel.id; });
    if (i < 0) return;
    pushHistory();
    // Arrows that held on to this drawing stay where they are, free.
    if (state.sel.kind === "shape") releaseEndsOn(state.sel.id);
    list.splice(i, 1);
    state.sel = null;
    state.editing = null;
    markDirty();
    render();
    refreshInspector();
  }

  function pick(kind, id) {
    state.sel = id ? { kind: kind, id: id } : null;
    render();
    refreshInspector();
  }

  /* --------------------------------------------------------------- emphasis */

  function applyEmphasis() {
    var focusIds = null;
    // The pointer wins while it is over a node; the selected node holds the realce
    // when it is not. Without the second half the highlight dies the moment you
    // move the cursor to read the panel about the very chain you are highlighting.
    var lit = state.hoverHighlight ? (state.hover || state.selected) : null;
    if (lit && (state.vout[lit] || state.vin[lit] || state.nodeEls[lit])) {
      focusIds = new Set([lit]);
      (state.vout[lit] || []).forEach(function (e) { focusIds.add(e.to); });
      (state.vin[lit] || []).forEach(function (e) { focusIds.add(e.from); });
    }
    var queried = state.query
      ? new Set(state.vnodes.filter(matchesQuery).map(function (n) { return n.id; }))
      : null;

    Object.keys(state.nodeEls).forEach(function (id) {
      var dim = false;
      if (focusIds && !focusIds.has(id)) dim = true;
      if (queried && !queried.has(id)) dim = true;
      if (state.selected === id) dim = false;
      state.nodeEls[id].classList.toggle("dimmed", dim);
    });
    state.edgeEls.forEach(function (item) {
      var e = item.edge;
      var hot = focusIds ? focusIds.has(e.from) && focusIds.has(e.to) : false;
      var dim = false;
      if (focusIds && !hot) dim = true;
      if (queried && !(queried.has(e.from) || queried.has(e.to))) dim = true;
      // For a relation the label has to fade with its curve, so the whole group
      // carries the class.
      item.el.classList.toggle("dimmed", dim);
      item.path.classList.toggle("hot", hot);
    });
  }

  /* ------------------------------------------------------------------- panel */

  /** The id of the table node with this name, or null when it is off the map. */
  function tableNamed(name) {
    var want = String(name || "").toLowerCase();
    var hit = (state.graph.nodes || []).filter(function (n) {
      return n.kind === "table" && String(n.label).toLowerCase() === want;
    })[0];
    return hit ? hit.id : null;
  }

  function tagList(ops) {
    return (ops || []).map(function (op) { return '<span class="tag ' + op + '">' + op + "</span>"; }).join("");
  }

  function esc(str) {
    return String(str == null ? "" : str).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  /* ---------------------------------------------------------------- markdown */

  /* OpenAPI says `description` is CommonMark, and authors use it — bold, inline
   * code, lists, headings, sometimes tables. Rendering it as plain text throws
   * away the structure the rules were written in.
   *
   * Everything is HTML-escaped BEFORE any transform runs, so the only tags that
   * reach the DOM are the ones generated below. */

  function mdInline(text) {
    var codes = [];
    // Pull code spans out first so no emphasis rule can reach inside them.
    text = text.replace(/`([^`]+)`/g, function (_, c) {
      codes.push(c);
      return "\u0000" + (codes.length - 1) + "\u0000";
    });
    text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    text = text.replace(/(^|[\s(])_([^_\n]+)_/g, "$1<em>$2</em>");
    text = text.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    // Only http(s) — never javascript: or data:.
    text = text.replace(
      /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
    );
    return text.replace(/\u0000(\d+)\u0000/g, function (_, i) {
      return "<code>" + codes[+i] + "</code>";
    });
  }

  function isTableSeparator(line) {
    return /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(line) && line.indexOf("-") >= 0 && line.indexOf("|") >= 0;
  }

  function tableCells(line) {
    return line.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map(function (c) { return c.trim(); });
  }

  function mdToHtml(src) {
    var lines = esc(src).replace(/\r\n?/g, "\n").split("\n");
    var out = [];
    var para = [];
    var i = 0;

    function flushPara() {
      if (!para.length) return;
      out.push("<p>" + mdInline(para.join(" ")) + "</p>");
      para = [];
    }

    function listBlock(re, tag) {
      var items = [];
      while (i < lines.length && re.test(lines[i])) {
        var item = lines[i].replace(re, "");
        i++;
        // Continuation lines belong to the item they are indented under.
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !re.test(lines[i])) {
          item += " " + lines[i].trim();
          i++;
        }
        items.push("<li>" + mdInline(item) + "</li>");
      }
      out.push("<" + tag + ">" + items.join("") + "</" + tag + ">");
    }

    while (i < lines.length) {
      var line = lines[i];

      var fence = /^\s*```\s*([A-Za-z0-9_+-]*)/.exec(line);
      if (fence) {
        flushPara();
        i++;
        var code = [];
        while (i < lines.length && !/^\s*```/.test(lines[i])) { code.push(lines[i]); i++; }
        i++;
        // A mermaid fence inside a PRD is a diagram, not a listing. It stays escaped
        // text in the HTML and is swapped for drawn ink afterwards, by `drawFences`,
        // which reads it back through textContent — so nothing is ever unescaped.
        var cls = /^mermaid$/i.test(fence[1] || "") ? ' class="mermaid-block"' : "";
        out.push("<pre" + cls + "><code>" + code.join("\n") + "</code></pre>");
        continue;
      }

      var heading = /^(#{1,6})\s+(.*)$/.exec(line);
      if (heading) {
        flushPara();
        var level = Math.min(6, heading[1].length + 3); // h1 in a panel would shout
        out.push("<h" + level + ">" + mdInline(heading[2]) + "</h" + level + ">");
        i++;
        continue;
      }

      if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        flushPara();
        out.push("<hr>");
        i++;
        continue;
      }

      if (line.indexOf("|") >= 0 && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
        flushPara();
        var head = tableCells(line);
        i += 2;
        var rows = [];
        while (i < lines.length && lines[i].indexOf("|") >= 0 && lines[i].trim()) {
          rows.push(tableCells(lines[i]));
          i++;
        }
        out.push(
          '<div class="doc-table"><table><thead><tr>' +
          head.map(function (c) { return "<th>" + mdInline(c) + "</th>"; }).join("") +
          "</tr></thead><tbody>" +
          rows.map(function (r) {
            return "<tr>" + r.map(function (c) { return "<td>" + mdInline(c) + "</td>"; }).join("") + "</tr>";
          }).join("") +
          "</tbody></table></div>"
        );
        continue;
      }

      if (/^\s*[-*+]\s+/.test(line)) {
        flushPara();
        listBlock(/^\s*[-*+]\s+/, "ul");
        continue;
      }
      if (/^\s*\d+[.)]\s+/.test(line)) {
        flushPara();
        listBlock(/^\s*\d+[.)]\s+/, "ol");
        continue;
      }
      // `&gt;`, not `>`: the whole source was HTML-escaped before any of these
      // rules ran, which is why matching the raw character never fired.
      if (/^\s*&gt;\s?/.test(line)) {
        flushPara();
        var quote = [];
        while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])) {
          quote.push(lines[i].replace(/^\s*&gt;\s?/, ""));
          i++;
        }
        out.push("<blockquote>" + mdInline(quote.join(" ")) + "</blockquote>");
        continue;
      }

      if (!line.trim()) {
        flushPara();
        i++;
        continue;
      }
      para.push(line.trim());
      i++;
    }
    flushPara();
    return out.join("");
  }

  function openPanel(id) {
    var node = byId(id);
    if (!node) return;
    state.selected = id;
    var m = node.meta;
    var html = "";

    if (node.kind === "group") {
      html += '<div class="kind">grupo de endpoints</div><h2>' + esc(node.label) + "</h2>";
      html += '<div class="sub">' + m.count + " endpoints</div>";
      html += '<h3>ações</h3><ul><li><button class="jump" data-expand="' + esc(node.label) + '">expandir no mapa</button></li>';
      html += '<li><button class="jump" data-focus="' + esc(id) + '">isolar esta cadeia</button></li></ul>';
      html += "<h3>endpoints</h3><ul>" + (m.routes || []).map(function (rid) {
        var r = state.rawIndex[rid];
        if (!r) return "";
        return '<li><span class="tag ' + (r.meta.method === "GET" ? "SELECT" : "UPDATE") + '">' + esc(r.meta.method) +
          '</span><button class="jump" data-detail="' + esc(rid) + '">' + esc(r.meta.path) + "</button>" +
          (r.meta.summary ? '<br><span class="sub">' + esc(r.meta.summary) + "</span>" : "") + "</li>";
      }).join("") + "</ul>";
      el.panelBody.innerHTML = html;
      el.panel.classList.add("open");
      applyEmphasis();
      return;
    }

    html += '<div class="kind">' + esc(node.kind) + (node.group ? " · " + esc(node.group) : "") + "</div>";
    html += "<h2>" + esc(node.kind === "route" ? m.method + " " + m.path : node.label) + "</h2>";
    if (m.source) html += '<div class="sub">' + esc(m.source) + "</div>";
    if (m.summary) html += '<p class="desc">' + esc(m.summary) + "</p>";
    if (m.deprecated) html += '<div class="warn"><b>deprecated</b> — evite novos usos.</div>';

    if (m.unresolved) {
      html += '<div class="warn"><b>não resolvido estaticamente</b><br>' + esc(m.hint || "") +
        "<br><br>Rode <code>croqui enrich</code> para o LLM completar só este trecho.</div>";
    }

    var rows = [];
    if (m.handler) rows.push(["handler", m.handler]);
    if (m.operation_id) rows.push(["operationId", m.operation_id]);
    if (m.request_body) rows.push(["request", m.request_body]);
    if (m.request_schema && m.request_schema.schema) rows.push(["body", m.request_schema.schema]);
    if (m.response_model) rows.push(["response", m.response_model]);
    if (m.schema) rows.push(["schema", m.schema]);
    if (m.object_kind) rows.push(["tipo", m.object_kind]);
    if (m.standalone) rows.push(["service", "nenhum (lógica inline)"]);
    if (m.internal) rows.push(["visibilidade", "interna"]);
    if (m.defined_in) rows.push(["DDL/model", m.defined_in.join("<br>")]);
    if (m.methods && m.methods.length) rows.push(["métodos", m.methods.join(", ")]);
    if (rows.length) {
      html += "<dl>" + rows.map(function (r) {
        return "<dt>" + esc(r[0]) + "</dt><dd>" + (r[0] === "DDL/model" ? r[1] : esc(r[1])) + "</dd>";
      }).join("") + "</dl>";
    }

    // What the table holds. Clicking a table used to answer "who touches it" and
    // not "what is in it", which is the question you actually have in front of a
    // schema you did not write. A `references` jumps to the table it points at,
    // when that table is on the map.
    if (m.columns && m.columns.length) {
      html += "<h3>colunas <span class=\"sub\">(" + m.columns.length + ")</span></h3>";
      html += '<div class="doc-table cols"><table><thead><tr><th>coluna</th><th>tipo</th><th></th></tr></thead><tbody>';
      html += m.columns.map(function (c) {
        var flags = "";
        if (c.pk) flags += '<span class="tag pk">PK</span>';
        if (c.required) flags += '<span class="tag req">not null</span>';
        if (c.indexed) flags += '<span class="tag idx">index</span>';
        if (c.references) {
          var target = tableNamed(c.references);
          flags += target
            ? '<button class="jump fk" data-detail="' + esc(target) + '">→ ' + esc(c.references) + "</button>"
            : '<span class="tag fk">→ ' + esc(c.references) + "</span>";
        }
        return "<tr><td><code>" + esc(c.name) + "</code></td><td class=\"ctype\">" +
          esc(c.type || "") + "</td><td>" + flags + "</td></tr>";
      }).join("") + "</tbody></table></div>";
    }

    // The endpoint's own documentation: rules, semantics, caveats. Rendered in
    // full — this is the part a flat diagram normally throws away.
    if (m.description) {
      html += '<h3>documentação' + (m.doc_source === "docstring" ? " <span class=\"sub\">(docstring)</span>" : "") + "</h3>";
      html += '<div class="doc">' + mdToHtml(m.description) + "</div>";
    }

    if (m.params && m.params.length) {
      html += "<h3>parâmetros</h3><ul class=\"params\">" + m.params.map(function (p) {
        var badges = '<span class="loc">' + esc(p["in"] || "") + "</span>";
        if (p.required) badges += '<span class="req">obrigatório</span>';
        return "<li><code>" + esc(p.name) + "</code>" + badges +
          (p.type ? '<div class="ptype">' + esc(p.type) + "</div>" : "") +
          (p["default"] !== undefined ? '<div class="ptype">default: ' + esc(String(p["default"])) + "</div>" : "") +
          (p.description ? '<div class="pdesc">' + mdToHtml(p.description) + "</div>" : "") + "</li>";
      }).join("") + "</ul>";
    }

    if (m.responses && m.responses.length) {
      html += "<h3>respostas</h3><ul class=\"responses\">" + m.responses.map(function (r) {
        var cls = r.code.charAt(0) === "2" ? "ok" : (r.code.charAt(0) === "4" ? "warnc" : "errc");
        return '<li><span class="code ' + cls + '">' + esc(r.code) + "</span>" +
          (r.description ? esc(r.description) : "") +
          (r.schema ? '<div class="ptype">' + esc(r.schema) + "</div>" : "") + "</li>";
      }).join("") + "</ul>";
    }

    var out = (state.vout[id] || []).concat(state.rawOut[id] && !state.vout[id] ? state.rawOut[id] : []);
    if (out.length) {
      html += "<h3>usa</h3><ul>" + out.map(function (e) {
        var t = byId(e.to);
        return "<li>" + tagList(e.ops) + '<button class="jump" data-detail="' + esc(e.to) + '">' +
          esc(t ? t.label : e.to) + "</button>" +
          (e.evidence && e.evidence.length ? '<br><span class="sub">' + esc(e.evidence[0]) + "</span>" : "") + "</li>";
      }).join("") + "</ul>";
    }
    var inc = state.vin[id] || state.rawIn[id] || [];
    if (inc.length) {
      html += "<h3>usado por</h3><ul>" + inc.map(function (e) {
        var f = byId(e.from);
        return '<li><button class="jump" data-detail="' + esc(e.from) + '">' + esc(f ? f.label : e.from) + "</button></li>";
      }).join("") + "</ul>";
    }

    html += "<h3>ações</h3><ul>";
    html += '<li><button class="jump" data-focus="' + esc(id) + '">isolar esta cadeia</button></li>';
    html += '<li><button class="jump" data-hide="' + esc(id) + '">ocultar do mapa</button></li></ul>';

    el.panelBody.innerHTML = html;
    el.panel.classList.add("open");
    el.panelBody.scrollTop = 0;
    setHash(id);
    applyEmphasis();
  }

  function closePanel() {
    el.panel.classList.remove("open");
    state.selected = null;
    setHash(null);
    applyEmphasis();
  }

  /** Deep link, so a specific endpoint can be shared as a URL. */
  function setHash(id) {
    if (!window.history || !window.history.replaceState) return;
    var target = id ? "#node=" + encodeURIComponent(id) : location.pathname + location.search;
    try {
      history.replaceState(null, "", target);
    } catch (e) { /* file:// in some browsers */ }
  }

  function openFromHash() {
    var match = /^#node=(.+)$/.exec(location.hash || "");
    if (!match) return;
    var id = decodeURIComponent(match[1]);
    var node = state.rawIndex[id];
    if (!node) return;
    el.panel.classList.add("instant");
    setTimeout(function () { el.panel.classList.remove("instant"); }, 60);
    // A route inside a collapsed group has no box yet — expand its group first.
    if (node.kind === "route" && !state.expanded[groupKey(node)]) {
      state.expanded[groupKey(node)] = true;
      render();
    }
    openPanel(id);
  }

  /* ------------------------------------------------------------- view / pan */

  function applyView() {
    el.scene.setAttribute(
      "transform",
      "translate(" + state.view.x + "," + state.view.y + ") scale(" + state.view.k + ")"
    );
    paintPaper();
    // Handles are drawn in scene units but must stay one size on screen.
    renderOverlay();
    rememberView();
  }

  /**
   * Pan and zoom the dotted paper with the map.
   *
   * The grid is a CSS background on `#canvas`, which is `position: fixed` — so
   * without this it never moves, and dragging a box slides it over a paper that
   * stays glued to the screen. That is what makes the map feel detached from its
   * own background. Cheaper than putting the dots in the SVG, and it keeps the
   * grid out of `croqui build`'s node count.
   */
  function paintPaper() {
    if (!el.canvas) return;
    var step = GRID * state.view.k;
    // Zoomed far out the dots would collapse into a grey wash, and far in they
    // would drift apart; step through octaves so the paper reads the same at any
    // zoom. Nothing snaps to it, so which octave wins is purely visual.
    while (step > 0 && step < 11) step *= 2;
    while (step > 60) step /= 2;
    // Trimmed: these land in an inline style on every frame of a pan, and the raw
    // floats would read as `21.119999999999997px`.
    var px = function (n) { return Math.round(n * 100) / 100 + "px"; };
    el.canvas.style.backgroundSize = px(step) + " " + px(step);
    el.canvas.style.backgroundPosition = px(state.view.x) + " " + px(state.view.y);
  }

  function fitToView() {
    var ids = Object.keys(state.boxes);
    var drawings = shapes();
    if (!ids.length && !drawings.length) return;
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    ids.forEach(function (id) {
      var b = state.boxes[id];
      minX = Math.min(minX, b.x); minY = Math.min(minY, b.y - 52);
      maxX = Math.max(maxX, b.x + b.w); maxY = Math.max(maxY, b.y + b.h);
    });
    drawings.forEach(function (s) {
      var b = shapeBox(s);
      minX = Math.min(minX, b.x); minY = Math.min(minY, b.y);
      maxX = Math.max(maxX, b.x + b.w); maxY = Math.max(maxY, b.y + b.h);
    });
    var pad = 56;
    // Keep the map clear of the tool rail — but only while the rail is the tall
    // one on the left; on a short window it lies flat along the bottom.
    var rail = 0;
    if (el.tools) {
      var box = el.tools.getBoundingClientRect();
      if (box.width && box.width < box.height) rail = box.right + 8;
    }
    var vw = el.canvas.clientWidth - rail - (el.panel.classList.contains("open") ? 380 : 0);
    var vh = el.canvas.clientHeight - 74;
    var raw = Math.min(vw / (maxX - minX + pad * 2), vh / (maxY - minY + pad * 2));
    // Never shrink past readability — pan instead of squinting.
    var k = Math.min(1.15, Math.max(0.4, raw));
    state.view.k = k;
    state.view.x = rail + Math.max(pad, (vw - (maxX - minX) * k) / 2) - minX * k;
    state.view.y = 74 + Math.max(0, (vh - (maxY - minY) * k) / 2) - minY * k;
    applyView();
  }

  function screenToScene(cx, cy) {
    var rect = el.canvas.getBoundingClientRect();
    return {
      x: (cx - rect.left - state.view.x) / state.view.k,
      y: (cy - rect.top - state.view.y) / state.view.k,
    };
  }

  /* --------------------------------------------------------- undo / redo */

  /* Snapshot-based history. The layout is a few KB of JSON, so cloning it whole
   * is simpler and harder to get wrong than replaying inverse operations —
   * and it makes "dirty" an exact comparison against what is on disk rather
   * than a flag that can drift. */

  var MAX_HISTORY = 100;
  var past = [];
  var future = [];

  function snapshot() {
    return JSON.stringify(state.layout);
  }

  /** Call BEFORE mutating state.layout, or pass a snapshot captured earlier. */
  function pushHistory(json) {
    past.push(json == null ? snapshot() : json);
    if (past.length > MAX_HISTORY) past.shift();
    future.length = 0;
  }

  function isDirty() {
    return snapshot() !== state.savedJson;
  }

  function restore(json) {
    state.layout = normalizeLayout(JSON.parse(json));
    state.editing = null;
    if (state.sel && !findBy(state.sel.kind === "shape" ? shapes() : links(), state.sel.id)) state.sel = null;
    closePanel();
    render();
    // Undoing a discard brings the orphans back; the chip has to agree.
    refreshOrphans();
    refreshChrome();
    refreshInspector();
  }

  /** Old layouts carried `notes`; they come back as text boxes, sticky colour
   *  and all, so nothing written before this version is lost. */
  function normalizeLayout(layout) {
    layout = layout || {};
    if (!layout.positions) layout.positions = {};
    if (!layout.hidden) layout.hidden = [];
    if (!layout.shapes) layout.shapes = [];
    if (!layout.links) layout.links = [];
    (layout.notes || []).forEach(function (note) {
      layout.shapes.push({
        id: note.id || "sh-note-" + state.seq++,
        type: "text", x: note.x || 0, y: note.y || 0,
        w: note.w || 190, h: note.h || 92, text: note.text || "",
        color: "amber", fill: true,
      });
    });
    delete layout.notes;
    return layout;
  }

  function undo() {
    if (!past.length) { toast("nada para desfazer"); return; }
    future.push(snapshot());
    restore(past.pop());
    toast("desfeito" + (past.length ? " · " + past.length + " restantes" : ""));
  }

  function redo() {
    if (!future.length) { toast("nada para refazer"); return; }
    past.push(snapshot());
    restore(future.pop());
    toast("refeito");
  }

  /* ------------------------------------------------------ selecting many nodes */

  /* Excalidraw's hands. Holding the button down on the paper sweeps up everything
   * under the rectangle — boxes and drawings — and Space + drag (or the middle
   * button) pans. Shift is "and also": shift-click a box to add or drop it,
   * shift-sweep to add to what is already picked. */

  /**
   * Add the marked drawings to a gang drag.
   *
   * A drawing anchored to a box that is moving already rides along with it, and an
   * arrow whose ends hold on to moving boxes is re-aimed by them — moving either of
   * those as well would move it twice. Everything else in the gang is carried by
   * the same offset as the boxes. Returns the drag, for chaining.
   */
  function gangDrag(drag, withShapes) {
    if (!withShapes) return drag;
    var moving = {};
    drag.ids.forEach(function (id) { moving[id] = true; });
    var list = [];
    markedShapeIds().forEach(function (id) {
      var s = findBy(shapes(), id);
      if (!s) return;
      if (s.anchor && s.anchor.node && moving[s.anchor.node]) return;
      list.push({ ref: s, x: s.x, y: s.y });
    });
    drag.shapes = list;
    return drag;
  }

  function moveGangShapes(drag, byX, byY) {
    var keys = {};
    drag.shapes.forEach(function (it) {
      var s = it.ref;
      s.x = it.x + byX;
      s.y = it.y + byY;
      if (s.anchor) reanchor(s);
      if (!isLinear(s)) keys[refKey({ shape: s.id })] = true;
    });
    // Tied arrows settle last, once every box they hold has moved.
    drag.shapes.forEach(function (it) {
      if (isLinear(it.ref) && hasEnds(it.ref)) resolveEnds(it.ref);
      applyShapeGeometry(it.ref);
    });
    followEnds(keys);
    renderOverlay();
  }

  /** Marked ids that still have a box. A rescan or a filter can retire one. */
  function marks() {
    return Object.keys(state.marked).filter(function (id) { return !!state.boxes[id]; });
  }

  function markNodes(ids, on) {
    ids.forEach(function (id) {
      if (on) state.marked[id] = true;
      else delete state.marked[id];
    });
    refreshMarks();
  }

  function clearMarks() {
    if (!Object.keys(state.marked).length && !Object.keys(state.markedShapes).length) return false;
    state.marked = {};
    state.markedShapes = {};
    refreshMarks();
    return true;
  }

  /** Marked drawings that still exist. */
  function markedShapeIds() {
    return shapes().filter(function (s) { return state.markedShapes[s.id]; })
      .map(function (s) { return s.id; });
  }

  /** Ctrl+A: every box and every drawing on the map, ready to be moved together. */
  function markEverything() {
    if (state.sel) pick(null, null);
    state.marked = {};
    Object.keys(state.boxes).forEach(function (id) { state.marked[id] = true; });
    state.markedShapes = {};
    shapes().forEach(function (s) { state.markedShapes[s.id] = true; });
    refreshMarks();
  }

  /** Every node whose box meets the swept rectangle. */
  function nodesIn(rect) {
    var area = {
      x: Math.min(rect.sx, rect.ex), y: Math.min(rect.sy, rect.ey),
      w: Math.abs(rect.ex - rect.sx), h: Math.abs(rect.ey - rect.sy),
    };
    return Object.keys(state.boxes).filter(function (id) {
      return overlaps(area, state.boxes[id]);
    });
  }

  /** Paint the ring, and keep the toolbar counter honest. */
  function refreshMarks() {
    var picked = state.marked;
    Object.keys(state.nodeEls || {}).forEach(function (id) {
      state.nodeEls[id].classList.toggle("marked", !!picked[id]);
    });
    Object.keys(state.shapeEls || {}).forEach(function (id) {
      var refs = state.shapeEls[id];
      if (refs && refs.g) refs.g.classList.toggle("marked", !!state.markedShapes[id]);
    });
    var n = marks().length, d = markedShapeIds().length;
    if (!el.marked) return;
    el.marked.hidden = !n && !d;
    var parts = [];
    if (n) parts.push(n + (n === 1 ? " nó" : " nós"));
    if (d) parts.push(d + (d === 1 ? " desenho" : " desenhos"));
    el.marked.textContent = parts.join(" + ") + (n + d === 1 ? " selecionado ✕" : " selecionados ✕");
    el.marked.title = "arraste qualquer um deles para mover todos · Esc ou clique aqui para limpar";
  }

  /* -------------------------------------------------------- orphan references */

  /* A layout outlives the code it describes. When a node goes for good — an
   * endpoint deleted, a service merged away — `croqui scan` says so and leaves
   * the reference in the file instead of throwing the work away. The viewer has
   * to say so as well, because on the map the loss is invisible: a relation to a
   * node that no longer exists simply does not draw. */

  function orphanRefs() {
    var out = {};
    function add(id, what) {
      if (typeof id !== "string" || !id) return;
      // `group:<name>` is the viewer's own collapsed box, not a graph node.
      if (state.rawIndex[id] || id.indexOf("group:") === 0) return;
      out[id] = out[id] || {};
      out[id][what] = (out[id][what] || 0) + 1;
    }
    Object.keys(state.layout.positions).forEach(function (id) { add(id, "posição"); });
    state.layout.hidden.forEach(function (id) { add(id, "oculto"); });
    links().forEach(function (l) { add(l.from, "relação"); add(l.to, "relação"); });
    shapes().forEach(function (s) {
      if (s.anchor) add(s.anchor.node, "desenho");
      if (s.from && s.from.node) add(s.from.node, "seta");
      if (s.to && s.to.node) add(s.to.node, "seta");
    });
    return out;
  }

  function refreshOrphans() {
    state.orphans = orphanRefs();
    var n = Object.keys(state.orphans).length;
    if (!el.orphans) return;
    el.orphans.hidden = !n;
    el.orphans.textContent = n + (n === 1 ? " órfã" : " órfãs");
    el.orphans.title = "referências do layout a nós que não existem mais no código";
  }

  function openOrphanPanel() {
    var ids = Object.keys(state.orphans || {}).sort();
    if (!ids.length) return;
    var html = '<div class="kind">layout</div><h2>referências órfãs</h2>';
    html += '<p class="desc">' + ids.length + " entrada(s) do <code>layout.json</code> apontam para nós " +
      "que o código não tem mais. Nada foi apagado — elas só não aparecem no mapa. Se foi um " +
      "<b>rename</b>, <code>croqui scan</code> reaponta sozinho o que conseguir identificar.</p>";
    html += "<ul>" + ids.map(function (id) {
      var counts = state.orphans[id];
      var what = Object.keys(counts).map(function (k) {
        return counts[k] > 1 ? counts[k] + " " + k + "s" : k;
      }).join(" · ");
      return "<li><code>" + esc(id) + '</code><br><span class="sub">' + esc(what) + "</span></li>";
    }).join("") + "</ul>";
    if (state.canSave) {
      html += '<h3>ações</h3><ul><li><button class="jump" data-drop-orphans="1">' +
        "descartar estas referências</button></li></ul>";
    }
    el.panelBody.innerHTML = html;
    el.panel.classList.add("open");
    state.selected = null;
    setHash(null);
    applyEmphasis();
  }

  function dropOrphans() {
    var ids = Object.keys(state.orphans || {});
    if (!ids.length) return;
    var gone = {};
    ids.forEach(function (id) { gone[id] = true; });
    var relations = links().filter(function (l) { return gone[l.from] || gone[l.to]; }).length;
    if (!confirm(
      "Descartar " + ids.length + " referência(s) órfã(s)" +
      (relations ? ", incluindo " + relations + " relação(ões) que você desenhou" : "") +
      "? (Ctrl+Z desfaz)"
    )) return;
    pushHistory();
    Object.keys(state.layout.positions).forEach(function (id) {
      if (gone[id]) delete state.layout.positions[id];
    });
    state.layout.hidden = state.layout.hidden.filter(function (id) { return !gone[id]; });
    state.layout.links = links().filter(function (l) { return !gone[l.from] && !gone[l.to]; });
    // A drawing keeps its ink and its place; only the dead anchor goes.
    shapes().forEach(function (s) {
      if (s.anchor && gone[s.anchor.node]) delete s.anchor;
      if (s.from && gone[s.from.node]) delete s.from;
      if (s.to && gone[s.to.node]) delete s.to;
    });
    state.sel = null;
    state.editing = null;
    markDirty();
    refreshOrphans();
    closePanel();
    render();
    refreshInspector();
    toast(ids.length + " referência(s) descartada(s)");
  }

  /* -------------------------------------------------------------- reading state */

  /* Which groups are open, where the camera sits, which filters are on and what
   * is isolated. This is not an edit to the map, so it is deliberately kept out
   * of `state.layout` and out of the dirty comparison — otherwise panning would
   * light up "salvar alterações" and the button would stop meaning anything.
   *
   * It is stored twice, on purpose. localStorage is written on every change so a
   * reload after `croqui scan` opens the map exactly as you left it, with nothing
   * to press. layout.json gets it on an explicit save, so a teammate cloning the
   * repo opens the view you committed. */

  function viewKey() {
    var project = (state.graph && state.graph.project) || {};
    return "croqui:view:" + (project.root || project.name || "-");
  }

  function currentView() {
    return {
      expanded: Object.keys(state.expanded).sort(),
      focus: state.focus || null,
      camera: {
        x: Math.round(state.view.x),
        y: Math.round(state.view.y),
        k: +(state.view.k || 1).toFixed(4),
      },
      filters: {
        query: state.query || "",
        methods: Array.from(state.methods).sort(),
        internal: !!state.showInternal,
        hover: !!state.hoverHighlight,
      },
    };
  }

  /** Returns true when a camera came with it, so boot knows not to re-fit over it. */
  function adoptView(view) {
    if (!view || typeof view !== "object") return false;
    state.expanded = {};
    (view.expanded || []).forEach(function (name) {
      if (typeof name === "string") state.expanded[name] = true;
    });
    // A focus on a node the code no longer has would render an empty map, which
    // reads exactly like the tool broke.
    state.focus = typeof view.focus === "string" && state.rawIndex[view.focus] ? view.focus : null;
    var f = view.filters || {};
    state.query = typeof f.query === "string" ? f.query : "";
    state.methods = new Set(Array.isArray(f.methods) ? f.methods : []);
    state.showInternal = !!f.internal;
    state.hoverHighlight = !!f.hover;
    var c = view.camera;
    if (!c || typeof c.x !== "number" || typeof c.y !== "number" || typeof c.k !== "number") return false;
    state.view = { x: c.x, y: c.y, k: Math.min(3, Math.max(0.12, c.k)) };
    return true;
  }

  var viewTimer = null;

  /** Called from every handler that changes the view. Debounced: a pan fires this
   *  on every frame and localStorage is synchronous. */
  function rememberView() {
    if (!state.graph) return;
    clearTimeout(viewTimer);
    viewTimer = setTimeout(function () {
      try {
        window.localStorage.setItem(viewKey(), JSON.stringify(currentView()));
      } catch (err) {
        // Private mode, file://, quota. layout.json is the real store.
      }
    }, 250);
  }

  function storedView() {
    try {
      return JSON.parse(window.localStorage.getItem(viewKey()) || "null");
    } catch (err) {
      return null;
    }
  }

  function viewDirty() {
    return JSON.stringify(currentView()) !== state.savedView;
  }

  /** Push the restored view back onto the toolbar, which is otherwise hardcoded. */
  function syncChrome() {
    if (el.search) el.search.value = state.query;
    if (el.methodChips) {
      Array.prototype.forEach.call(el.methodChips.querySelectorAll("[data-method]"), function (chip) {
        press(chip, state.methods.has(chip.getAttribute("data-method")));
      });
    }
    press(el.toggleInternal, state.showInternal);
    press(el.toggleHover, state.hoverHighlight);
    if (el.clearFocus) {
      el.clearFocus.hidden = !state.focus;
      el.clearFocus.textContent = state.focus ? "◂ todo o mapa" : "";
    }
    if (el.expandAll) {
      var groups = allGroups();
      var open = groups.length && groups.every(function (g) { return state.expanded[g]; });
      el.expandAll.textContent = open ? "recolher tudo" : "expandir tudo";
      press(el.expandAll, open);
    }
  }

  /* ------------------------------------------------------------------ saving */

  /** Edits live in memory until you save — nothing is written behind your back. */
  function markDirty() {
    refreshChrome();
  }

  function refreshChrome() {
    var dirty = isDirty();
    if (el.save) {
      if (state.saving) {
        el.save.disabled = true;
        el.save.textContent = "salvando…";
      } else {
        el.save.disabled = !dirty;
        el.save.textContent = dirty ? "salvar alterações •" : "salvo";
      }
    }
    if (el.undo) el.undo.disabled = !past.length;
    if (el.redo) el.redo.disabled = !future.length;
  }

  /**
   * Write down where every visible box actually is, not just the ones you dragged.
   *
   * This is the whole difference between a map that survives a code change and one
   * that reflows. The automatic layout orders each column by the barycenter of its
   * parents, so *one* new endpoint re-sorts and re-centres everything downstream —
   * and a box you never touched had nothing on disk to hold it in place. Saving the
   * arrangement you are looking at is what makes the next `croqui scan` additive.
   *
   * Positions of nodes that are not on screen right now (hidden, filtered out,
   * inside a collapsed group) are carried over untouched rather than dropped.
   */
  function pinArrangement() {
    var out = {};
    Object.keys(state.layout.positions).forEach(function (id) {
      out[id] = state.layout.positions[id];
    });
    Object.keys(state.boxes).forEach(function (id) {
      out[id] = { x: Math.round(state.boxes[id].x), y: Math.round(state.boxes[id].y) };
    });
    // An open group has no box of its own; its corner is its frame's, so it folds
    // up where it was open.
    var open = openMembers(state.boxes);
    Object.keys(open).forEach(function (key) {
      var f = frameAround(open[key], state.boxes);
      if (f) out["group:" + key] = { x: Math.round(f.x), y: Math.round(f.y) };
    });
    return out;
  }

  function saveLayout() {
    // The in-flight guard matters: without it a slow server invites a double POST.
    if (!state.canSave || state.saving) return;
    // The button only tracks edits, so Ctrl+S is also how a view-only change
    // (a pan, an opened group) gets committed to the file.
    if (!isDirty() && !viewDirty()) { toast("nada para salvar"); return; }

    var fresh = pinArrangement();
    var pinned = Object.keys(fresh).length - Object.keys(state.layout.positions).length;
    state.layout.positions = fresh;
    // Deliberately *not* a history entry. Pinning is what saving means, not
    // something you did, and spending an undo step on it would put "un-pin
    // sixteen boxes you never touched" between Ctrl+Z and your actual last edit.
    // It is still reversible: undo past it, or `limpar edições`.

    var sending = snapshot();
    var payload = JSON.parse(sending);
    payload.view = currentView();
    var view = JSON.stringify(payload.view);
    state.saving = true;
    refreshChrome();
    fetch("api/layout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(function (r) {
        if (!r.ok) throw new Error("http " + r.status);
        state.savedJson = sending;
        state.savedView = view;
        toast(
          "gravado em .croqui/layout.json" +
          (pinned > 0 ? " · " + pinned + " caixa(s) fixada(s)" : "")
        );
      })
      .catch(function (err) { toast("falha ao salvar: " + err.message); })
      .then(function () {
        state.saving = false;
        refreshChrome();
      });
  }

  function toast(msg) {
    el.toast.textContent = msg;
    el.toast.classList.add("show");
    clearTimeout(el.toast._t);
    el.toast._t = setTimeout(function () { el.toast.classList.remove("show"); }, 2200);
  }

  /* ------------------------------------------------------------ interactions */

  function toggleGroup(name) {
    if (state.expanded[name]) delete state.expanded[name];
    else state.expanded[name] = true;
    render();
    syncChrome();
    rememberView();
  }

  /** What is under the cursor, even mid-drag when the canvas has the capture. */
  function hitAt(cx, cy, selector) {
    var target = document.elementFromPoint(cx, cy);
    return target && target.closest ? target.closest(selector) : null;
  }

  function wireCanvas() {
    var drag = null;

    function onPointerDown(ev) {
      var pos = screenToScene(ev.clientX, ev.clientY);
      var attr = ev.target.getAttribute ? ev.target.getAttribute.bind(ev.target) : function () { return null; };

      // 0. Space held (or the middle button) is the hand: it pans from anywhere,
      // even starting on a box, the way Excalidraw's does.
      if (state.spaceDown || ev.button === 1) {
        ev.preventDefault();
        drag = { kind: "pan", sx: ev.clientX, sy: ev.clientY, ox: state.view.x, oy: state.view.y, moved: false };
        el.canvas.classList.add("panning");
        return;
      }

      // 1. A handle of the current selection wins over everything under it.
      var handle = attr("data-handle");
      var sel = selectedShape();
      if (handle && sel) {
        drag = {
          kind: "resize", handle: handle, ref: sel, pre: snapshot(), moved: false,
          start: { x: sel.x, y: sel.y, w: sel.w, h: sel.h },
        };
        el.canvas.classList.add("dragging");
        return;
      }

      // 2. A drawing tool takes the canvas over completely.
      if (state.tool === "link") { drag = startLink(ev); return; }
      if (state.tool !== "select") {
        drag = { kind: "draw", type: state.tool, sx: pos.x, sy: pos.y, ex: pos.x, ey: pos.y, moved: false };
        return;
      }

      var collapse = attr("data-collapse");
      if (collapse) { toggleGroup(collapse); return; }

      // 3. Your own drawings.
      var sg = ev.target.closest ? ev.target.closest(".shape") : null;
      if (sg) {
        var shape = findBy(shapes(), sg.getAttribute("data-shape"));
        if (shape && state.markedShapes[shape.id] && !ev.shiftKey) {
          // One of a gang: the whole selection moves, drawings and boxes alike.
          var gang = marks(), from0 = {};
          gang.forEach(function (n) { from0[n] = { x: state.boxes[n].x, y: state.boxes[n].y }; });
          drag = gangDrag({
            kind: "node", id: null, ids: gang, origin: from0,
            lead: { x: shape.x, y: shape.y },
            dx: pos.x - shape.x, dy: pos.y - shape.y, moved: false, pre: snapshot(),
          }, true);
          el.canvas.classList.add("dragging");
          return;
        }
        if (shape) {
          if (!state.sel || state.sel.id !== shape.id) pick("shape", shape.id);
          drag = {
            kind: "shape", id: shape.id, ref: shape, moved: false, pre: snapshot(),
            dx: pos.x - shape.x, dy: pos.y - shape.y,
            hadEnds: isLinear(shape) && hasEnds(shape),
          };
          el.canvas.classList.add("dragging");
          return;
        }
      }
      var lg = ev.target.closest ? ev.target.closest("[data-link]") : null;
      if (lg) { pick("link", lg.getAttribute("data-link")); return; }

      // 4. An open group's frame: the group moves as one, endpoints and all.
      var fg = ev.target.closest ? ev.target.closest(".gframe") : null;
      if (fg && !ev.shiftKey) {
        var gkey = fg.getAttribute("data-frame");
        var ids = openMembers(state.boxes)[gkey] || [];
        if (ids.length) {
          if (state.sel) pick(null, null);
          var lead = state.boxes[ids[0]];
          var from = {};
          ids.forEach(function (n) { from[n] = { x: state.boxes[n].x, y: state.boxes[n].y }; });
          drag = {
            kind: "node", id: ids[0], ids: ids, origin: from, frame: gkey,
            lead: { x: lead.x, y: lead.y },
            dx: pos.x - lead.x, dy: pos.y - lead.y, moved: false, pre: snapshot(),
          };
          el.canvas.classList.add("dragging");
          return;
        }
      }

      // 5. The map itself.
      var g = ev.target.closest ? ev.target.closest(".node") : null;
      if (g) {
        var id = g.getAttribute("data-id");
        if (ev.shiftKey) {
          // One selection at a time: a drawing's handles staying live while you
          // gang up boxes leaves the inspector talking about the wrong thing.
          if (state.sel) pick(null, null);
          markNodes([id], !state.marked[id]);
          return;
        }
        // Dropping the selection redraws everything, so read the box after it:
        // the old one is detached and the edges would trail behind the node.
        if (state.sel) pick(null, null);
        var box = state.boxes[id];
        if (!box) return;
        // Grabbing one of a gang moves the gang; grabbing anything else moves it
        // alone. Origins are copied now so every box shifts by the same amount
        // however far the pointer wanders.
        var moving = state.marked[id] ? marks() : [id];
        var origin = {};
        moving.forEach(function (n) { origin[n] = { x: state.boxes[n].x, y: state.boxes[n].y }; });
        drag = gangDrag({
          kind: "node", id: id, ids: moving, origin: origin,
          lead: { x: box.x, y: box.y },
          dx: pos.x - box.x, dy: pos.y - box.y, moved: false,
          // Captured before the move so undo returns to where it started.
          pre: snapshot(),
        }, !!state.marked[id]);
        el.canvas.classList.add("dragging");
        return;
      }

      if (state.sel) pick(null, null);
      // Bare paper: holding the button down sweeps a selection. Shift adds to the
      // one you have; without it the sweep starts over.
      drag = { kind: "marquee", sx: pos.x, sy: pos.y, ex: pos.x, ey: pos.y, moved: false, add: ev.shiftKey };
      el.canvas.classList.add("sweeping");
    }

    el.canvas.addEventListener("pointerdown", function (ev) {
      // The middle button pans too; any other button is not ours.
      if (ev.button && ev.button !== 1) return;
      // Leaving a text box must not swallow the click that left it.
      var wasEditing = state.editing;
      state.editing = null;
      var before = state.renders;
      onPointerDown(ev);
      if (wasEditing && state.renders === before) {
        render();
        // A node drag survives this on its own: it holds only ids and numbers and
        // looks the boxes and elements up on every move. A shape drag still holds
        // the shape object itself, which render() does not replace.
      }
      el.canvas.setPointerCapture(ev.pointerId);
    });

    el.canvas.addEventListener("pointermove", function (ev) {
      // Remembered for paste, which has no coordinates of its own.
      state.point = { x: ev.clientX, y: ev.clientY };
      if (!drag) {
        if (!state.hoverHighlight) return;
        var g = ev.target.closest ? ev.target.closest(".node") : null;
        var id = g && g.getAttribute("data-id");
        if (id !== state.hover) { state.hover = id; applyEmphasis(); }
        return;
      }
      var pos = screenToScene(ev.clientX, ev.clientY);

      if (drag.kind === "pan") {
        state.view.x = drag.ox + (ev.clientX - drag.sx);
        state.view.y = drag.oy + (ev.clientY - drag.sy);
        if (Math.abs(ev.clientX - drag.sx) + Math.abs(ev.clientY - drag.sy) > 3) drag.moved = true;
        applyView();
        return;
      }
      if (drag.kind === "draw") {
        drag.ex = pos.x;
        drag.ey = pos.y;
        drag.moved = Math.abs(pos.x - drag.sx) + Math.abs(pos.y - drag.sy) > 5;
        drawPreview(drag);
        return;
      }
      if (drag.kind === "linking") {
        drag.to = pos;
        drawLinkPreview(drag);
        return;
      }
      if (drag.kind === "resize") { drag.ctrl = ev.ctrlKey; resizeTo(drag, pos); return; }
      if (drag.kind === "marquee") {
        drag.ex = pos.x;
        drag.ey = pos.y;
        drag.moved = Math.abs(pos.x - drag.sx) + Math.abs(pos.y - drag.sy) > 4;
        drawMarquee(drag);
        previewMarquee(drag);
        return;
      }

      var nx = Math.round(pos.x - drag.dx), ny = Math.round(pos.y - drag.dy);
      drag.moved = true;
      if (drag.kind === "shape") {
        // Picking a tied arrow up lets go of its boxes; where it lands decides anew.
        if (drag.hadEnds && !drag.released) { releaseEnds(drag.ref); drag.released = true; }
        drag.ref.x = nx;
        drag.ref.y = ny;
        // Dragging an anchored drawing re-aims the offset, it does not break the
        // anchor: you are saying "sit here relative to that node", not "let go".
        reanchor(drag.ref);
        applyShapeGeometry(drag.ref);
        // Arrows holding on to this drawing come along.
        if (!isLinear(drag.ref)) followEnds(keyed({ shape: drag.ref.id }));
        renderOverlay();
        return;
      }
      // One node or twenty, the same path: every box in the gang shifts by what
      // the grabbed one did. Boxes and elements are looked up live rather than
      // captured, because a render in between would have detached them.
      var byX = nx - drag.lead.x;
      var byY = ny - drag.lead.y;
      drag.ids.forEach(function (id) {
        var box = state.boxes[id];
        if (!box) return;
        box.x = drag.origin[id].x + byX;
        box.y = drag.origin[id].y + byY;
        state.layout.positions[id] = { x: box.x, y: box.y };
        var node = state.nodeEls[id];
        if (node) node.setAttribute("transform", "translate(" + box.x + "," + box.y + ")");
        moveAnchored(id);
        redrawEdgesFor(id);
      });
      if (drag.shapes) moveGangShapes(drag, byX, byY);
      positionChrome();
    });

    el.canvas.addEventListener("pointerup", function (ev) {
      el.canvas.classList.remove("panning", "dragging", "sweeping");
      if (!drag) return;
      var done = drag;
      drag = null;

      if (done.kind === "draw") { commitDraw(done, ev); return; }
      if (done.kind === "linking") { finishLink(done, ev); return; }
      if (done.kind === "marquee") { commitMarquee(done); return; }
      if (done.kind === "node" && !done.moved && done.frame) {
        // A click on the frame is a click on the group: it folds back up.
        toggleGroup(done.frame);
        return;
      }
      if (done.kind === "node" && done.moved) {
        // Whatever moved, an open group's corner is where its frame now is — so
        // folding it up leaves the box there, and the next render summons the
        // endpoints back to the same spot instead of snapping them home.
        var open = openMembers(state.boxes);
        Object.keys(open).forEach(function (key) {
          var touched = open[key].some(function (id) { return done.ids.indexOf(id) >= 0; });
          if (!touched) return;
          var f = frameAround(open[key], state.boxes);
          if (f) state.layout.positions["group:" + key] = { x: Math.round(f.x), y: Math.round(f.y) };
        });
      }
      if (done.kind === "node" && !done.moved && done.id == null) return;
      if (done.kind === "node" && !done.moved) {
        // A plain click is a fresh start: it drops the gang and speaks about one
        // node. Shift-click is the way to keep building a selection.
        clearMarks();
        var node = byId(done.id);
        // A collapsed group's primary affordance is expanding it.
        if (node && node.kind === "group") toggleGroup(node.label);
        else openPanel(done.id);
      }
      if (done.kind === "pan" && !done.moved) {
        clearMarks();
        closePanel();
      }
      if (done.moved && done.kind !== "pan") {
        // An arrow let go of: its ends hold on to whatever box they landed in.
        var arrow = done.ref && isLinear(done.ref) && (done.kind === "resize" || done.kind === "shape");
        if (arrow) settleEnds(done.ref, done.kind === "resize" ? done.handle : "ab", ev.ctrlKey, done.hadEnds);
        pushHistory(done.pre);
        markDirty();
        // Text re-wraps to the new width only on a full pass — and a box or frame
        // dropped onto an open group is pushed clear by one.
        if (done.kind === "resize" || arrow) render();
        else if (done.kind === "node" && framesCollide()) render();
      }
    });

    /* Double-click is how you write: inside a drawing it drops the caret into
     * that drawing's text, on bare paper it makes a new text box right where you
     * clicked. Nothing else on the canvas answers to a double-click, which is why
     * it can carry the gesture without a modifier. */
    el.canvas.addEventListener("dblclick", function (ev) {
      var sg = ev.target.closest ? ev.target.closest(".shape") : null;
      if (sg) {
        var id = sg.getAttribute("data-shape");
        var shape = findBy(shapes(), id);
        if (shape && !isLinear(shape)) {
          state.sel = { kind: "shape", id: id };
          state.editing = id;
          render();
          refreshInspector();
        }
        return;
      }
      var lg = ev.target.closest ? ev.target.closest("[data-link]") : null;
      if (lg) {
        pick("link", lg.getAttribute("data-link"));
        if (el.insLabel) el.insLabel.focus();
        return;
      }
      // A node and the collapse caret are not bare paper — a text box dropped on
      // top of the box you were double-clicking is never what you meant.
      if (ev.target.closest && ev.target.closest(".node")) return;
      if (ev.target.getAttribute && ev.target.getAttribute("data-collapse")) return;
      // With a shape tool armed, a drag is a drawing and a click already made one;
      // only the two tools that mean "write" answer here.
      if (state.tool !== "select" && state.tool !== "text") return;
      var pos = screenToScene(ev.clientX, ev.clientY);
      var size = DEFAULT_SIZE.text;
      addShape("text", pos.x - size[0] / 2, pos.y - size[1] / 2, size[0], size[1], { edit: true });
    });

    el.canvas.addEventListener("wheel", function (ev) {
      ev.preventDefault();
      if (ev.shiftKey) { state.view.x -= ev.deltaY; applyView(); return; }
      var rect = el.canvas.getBoundingClientRect();
      var mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
      var k = Math.min(3, Math.max(0.12, state.view.k * Math.exp(-ev.deltaY * 0.0015)));
      state.view.x = mx - (mx - state.view.x) * (k / state.view.k);
      state.view.y = my - (my - state.view.y) * (k / state.view.k);
      state.view.k = k;
      applyView();
    }, { passive: false });
  }

  /* ------------------------------------------------------------ pasted images */

  /** Where a paste lands: under the pointer if it has been over the map, else the
   *  middle of what you are looking at. */
  function pastePoint() {
    var rect = el.canvas.getBoundingClientRect();
    var p = state.point;
    var inside = p && p.x >= rect.left && p.x <= rect.right && p.y >= rect.top && p.y <= rect.bottom;
    if (inside) return screenToScene(p.x, p.y);
    return screenToScene(rect.left + rect.width / 2, rect.top + rect.height / 2);
  }

  /** The image on the clipboard, if there is one. */
  function clipboardImage(data) {
    if (!data) return null;
    var files = data.files;
    for (var i = 0; files && i < files.length; i++) {
      if (files[i].type && files[i].type.indexOf("image/") === 0) return files[i];
    }
    // Safari and some Linux clipboards expose the picture through `items` only.
    var items = data.items;
    for (var j = 0; items && j < items.length; j++) {
      if (items[j].kind === "file" && items[j].type.indexOf("image/") === 0) {
        var file = items[j].getAsFile();
        if (file) return file;
      }
    }
    return null;
  }

  /** Natural size of a blob, so a paste lands at the picture's own aspect ratio. */
  function measureImage(blob) {
    return new Promise(function (resolve) {
      var url = URL.createObjectURL(blob);
      var probe = new Image();
      probe.onload = function () {
        var w = probe.naturalWidth || MAX_PASTE;
        var h = probe.naturalHeight || MAX_PASTE;
        URL.revokeObjectURL(url);
        resolve({ w: w, h: h });
      };
      probe.onerror = function () {
        URL.revokeObjectURL(url);
        resolve(null);
      };
      probe.src = url;
    });
  }

  /**
   * Paste a picture onto the map.
   *
   * The bytes go to `.croqui/images/` and the shape only keeps the name it came
   * back as. That is what keeps `layout.json` reviewable — a screenshot inlined as
   * base64 would be a megabyte on one line — and it is why a pasted image survives
   * `croqui scan` for free: the scan rewrites node ids inside the layout and never
   * touches the image files at all.
   */
  function pasteImage(blob, at) {
    if (!state.canSave) {
      toast("este é um croqui.html só de leitura — abra com croqui serve para colar imagens");
      return;
    }
    if (!IMAGE_MIME[blob.type]) {
      toast("não sei colar " + (blob.type || "esse formato") + " — use PNG, JPEG, GIF ou WebP");
      return;
    }
    if (blob.size > MAX_PASTE_MB * 1024 * 1024) {
      toast("imagem acima de " + MAX_PASTE_MB + " MB");
      return;
    }
    state.pasting++;
    toast("colando imagem…");
    measureImage(blob).then(function (size) {
      if (!size) throw new Error("não consegui ler a imagem");
      return fetch("api/image", {
        method: "POST",
        headers: { "Content-Type": blob.type },
        body: blob,
      }).then(function (r) {
        return r.json().then(function (body) {
          if (!r.ok) throw new Error(body && body.error ? body.error : "http " + r.status);
          return { name: body.name, size: size };
        });
      });
    }).then(function (res) {
      // Scaled down to something you can see whole, never scaled up: a 40px icon
      // pasted at 520px would just be a blurry square.
      var scale = Math.min(1, MAX_PASTE / Math.max(res.size.w, res.size.h));
      var w = Math.max(SHAPE_MIN, Math.round(res.size.w * scale));
      var h = Math.max(SHAPE_MIN, Math.round(res.size.h * scale));
      addShape("image", at.x - w / 2, at.y - h / 2, w, h, {
        src: res.name,
        alt: blob.name || "imagem colada",
        // In front, unlike a drawn shape. An unfilled outline behind the map is a
        // frame around a chain; an opaque picture behind it is just a picture with
        // node boxes sitting on top of it, unclickable where they overlap. Push it
        // back with `à frente` if that is what you actually wanted.
        front: true,
      });
      toast("imagem colada — Ctrl+S grava");
    }).catch(function (err) {
      toast("não deu para colar: " + (err && err.message ? err.message : err));
    }).then(function () {
      state.pasting--;
    });
  }

  /* --------------------------------------------------------- pasted drawings */

  /** The plain text on the clipboard, if there is any. */
  function clipboardText(data) {
    if (!data || !data.getData) return "";
    try { return data.getData("text/plain") || ""; } catch (err) { return ""; }
  }

  function finiteNum(v) {
    return typeof v === "number" && isFinite(v) ? v : 0;
  }

  function savedHint() {
    return state.canSave ? " — Ctrl+S grava" : "";
  }

  /* -------------------------------------------------- colours from elsewhere */

  var paletteCache = null;

  /** croqui's palette, resolved to RGB in the theme that is on screen. */
  function paletteRGB() {
    var root = getComputedStyle(document.documentElement);
    var ink = root.getPropertyValue("--ink").trim();
    // Keyed on ink, so switching to dark mode rebuilds the list by itself.
    if (paletteCache && paletteCache.ink === ink) return paletteCache.list;
    var list = [];
    PALETTE.forEach(function (entry) {
      var named = /^var\((--[\w-]+)\)$/.exec(entry[1]);
      var rgb = parseColor(named ? root.getPropertyValue(named[1]).trim() : entry[1]);
      if (rgb) list.push({ name: entry[0], rgb: rgb });
    });
    paletteCache = { ink: ink, list: list };
    return list;
  }

  function parseColor(value) {
    if (!value) return null;
    var hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(value.trim());
    if (hex) {
      var h = hex[1];
      if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
      return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
    }
    var fn = /rgba?\(([^)]+)\)/i.exec(value);
    if (fn) {
      var parts = fn[1].split(/[,\s/]+/).map(Number).slice(0, 3);
      if (parts.length === 3 && parts.every(function (n) { return isFinite(n); })) return parts;
    }
    return null;
  }

  /**
   * The croqui colour closest to a colour from outside.
   *
   * Nearest in plain RGB, which is coarse but right where it matters: the eight
   * croqui colours are far apart and Excalidraw's defaults sit almost on top of
   * them. A *neutral* stroke is the exception, and it is not a rounding error: it
   * means no colour was chosen, so it becomes `ink` — the theme's own foreground.
   * Matching Excalidraw's near-black against a dark theme's palette would pick
   * whichever colour happens to be darkest, which is how a black arrow arrives
   * green.
   */
  function nearestColor(value) {
    var want = parseColor(value);
    if (!want) return null;
    var hi = Math.max(want[0], want[1], want[2]);
    var lo = Math.min(want[0], want[1], want[2]);
    if (hi - lo <= 24) return "ink";
    var best = null, bestDist = Infinity;
    paletteRGB().forEach(function (p) {
      var d = 0;
      for (var i = 0; i < 3; i++) d += (p.rgb[i] - want[i]) * (p.rgb[i] - want[i]);
      if (d < bestDist) { bestDist = d; best = p.name; }
    });
    return best;
  }

  /* ------------------------------------------------------ an Excalidraw copy */

  /** A straight run from the first point to the last: croqui's arrows and lines are
   *  one segment, and an elbowed Excalidraw arrow has to lose its elbows. The sign
   *  of w/h is the direction, which is how croqui stores it too. */
  function linearSpan(elm) {
    var pts = Array.isArray(elm.points) && elm.points.length >= 2 ? elm.points : null;
    if (!pts) return { x: finiteNum(elm.x), y: finiteNum(elm.y), w: finiteNum(elm.width), h: finiteNum(elm.height) };
    var a = pts[0], b = pts[pts.length - 1];
    return {
      x: finiteNum(elm.x) + finiteNum(a[0]), y: finiteNum(elm.y) + finiteNum(a[1]),
      w: finiteNum(b[0]) - finiteNum(a[0]), h: finiteNum(b[1]) - finiteNum(a[1]),
    };
  }

  /** Which ends carry a head. An arrow drawn without touching the arrowhead picker
   *  has no `endArrowhead` at all, and it does have a head. */
  function headsFor(elm) {
    var start = !!elm.startArrowhead;
    var end = elm.endArrowhead === undefined ? elm.type === "arrow" : !!elm.endArrowhead;
    if (start && end) return "both";
    if (start) return "start";
    return end ? "end" : "none";
  }

  /** The writing on an element: what was typed, without the breaks Excalidraw
   *  inserts to fit it to a box. */
  function elementText(elm) {
    var body = elm.originalText || elm.text;
    return typeof body === "string" && body.trim()
      ? body.replace(/\r\n?/g, "\n").replace(/\s+$/, "") : "";
  }

  /**
   * An Excalidraw copy, as croqui drawings.
   *
   * Excalidraw copies its own JSON to the clipboard as text/plain, so a bare Ctrl+V
   * would drop a page of JSON on the map. This reads that JSON instead: boxes,
   * diamonds, ellipses, arrows and lines become the croqui shapes they match, with
   * their colour, fill, dashes and arrowheads; text becomes text boxes; and a label
   * bound to a box becomes that box's own writing rather than a second shape on top
   * of it. Everything croqui cannot draw is counted in `skipped`, by kind.
   *
   * `null` means this is not an Excalidraw copy, and the plain-text path takes it.
   */
  function excalidrawDrawing(text) {
    if (!text || text.charAt(0) !== "{") return null;
    var doc;
    try { doc = JSON.parse(text); } catch (err) { return null; }
    if (!doc || !EXCALIDRAW_KINDS[doc.type] || !Array.isArray(doc.elements)) return null;

    var live = doc.elements.filter(function (elm) {
      return elm && typeof elm === "object" && !elm.isDeleted && typeof elm.type === "string";
    });
    var index = {};
    live.forEach(function (elm) { if (elm.id) index[elm.id] = elm; });

    /* A label inside a box is a text element of its own, pointing at the box through
     * `containerId`. It belongs *on* the shape — but only on a shape that can hold
     * writing: croqui's arrows and lines cannot, so a labelled arrow keeps its label
     * as a separate text box, where Excalidraw already put it. */
    var labels = {};
    live.forEach(function (elm) {
      if (elm.type !== "text" || !elm.containerId) return;
      var host = index[elm.containerId];
      var kind = host && EXCALIDRAW_SHAPES[host.type];
      if (!kind || LINEAR[kind]) return;
      labels[elm.containerId] = elm;
    });

    var found = { items: [], skipped: {} };
    function skip(kind) { found.skipped[kind] = (found.skipped[kind] || 0) + 1; }

    live.forEach(function (elm) {
      if (elm.type === "text") {
        // Already carried by the box it labels.
        if (elm.containerId && labels[elm.containerId] === elm) return;
        // Writing with nothing in it is not something croqui refused to draw, so it
        // is dropped rather than counted as left behind.
        var body = elementText(elm);
        if (!body) return;
        found.items.push({
          type: "text", text: body,
          x: finiteNum(elm.x), y: finiteNum(elm.y),
          w: finiteNum(elm.width), h: finiteNum(elm.height),
          size: finiteNum(elm.fontSize),
          color: nearestColor(elm.strokeColor),
        });
        return;
      }
      if (elm.type === "image") {
        // The picture itself travels in the copy: `files` maps the element's
        // fileId to a data URL. Kept as bytes here; `pasteDrawing` stores them.
        var file = doc.files && elm.fileId ? doc.files[elm.fileId] : null;
        var url = file && typeof file.dataURL === "string" ? file.dataURL : "";
        var mime = (file && file.mimeType) || (/^data:([^;,]+)/.exec(url) || [])[1] || "";
        if (!url || !IMAGE_MIME[mime]) { skip("image"); return; }
        found.items.push({
          type: "image", src: elm.id, data: url, mime: mime,
          x: finiteNum(elm.x), y: finiteNum(elm.y),
          w: Math.max(SHAPE_MIN, Math.abs(finiteNum(elm.width))),
          h: Math.max(SHAPE_MIN, Math.abs(finiteNum(elm.height))),
        });
        return;
      }
      var type = EXCALIDRAW_SHAPES[elm.type];
      if (!type) { skip(elm.type); return; }
      var span = LINEAR[type]
        ? linearSpan(elm)
        : { x: finiteNum(elm.x), y: finiteNum(elm.y),
            w: Math.max(SHAPE_MIN, Math.abs(finiteNum(elm.width))),
            h: Math.max(SHAPE_MIN, Math.abs(finiteNum(elm.height))) };
      var label = labels[elm.id];
      // An arrow that held on to a box there holds on to the copy of it here.
      var tie = LINEAR[type] ? {
        from: elm.startBinding && elm.startBinding.elementId,
        to: elm.endBinding && elm.endBinding.elementId,
      } : null;
      found.items.push({
        type: type, x: span.x, y: span.y, w: span.w, h: span.h, src: elm.id, tie: tie,
        text: label ? elementText(label) : "",
        size: label ? finiteNum(label.fontSize) : 0,
        color: nearestColor(elm.strokeColor),
        // `fillStyle` says hachure, cross-hatch or solid; croqui has one fill, so
        // what matters is only whether there is a background at all.
        fill: !!(elm.backgroundColor && elm.backgroundColor !== "transparent"),
        dash: elm.strokeStyle === "dashed" || elm.strokeStyle === "dotted",
        heads: LINEAR[type] ? headsFor(elm) : null,
      });
    });
    return found;
  }

  /** Everything written in a copy, in reading order — what a caret wants from it. */
  function copyWriting(copy) {
    return copy.items.map(function (it) { return it.text; })
      .filter(function (t) { return !!t; }).join("\n");
  }

  /** How many of each kind stayed behind, said out loud. */
  function skippedSummary(skipped) {
    return Object.keys(skipped).map(function (kind) {
      var n = skipped[kind];
      var name = EXCALIDRAW_NAMES[kind] || [kind, kind];
      return n + " " + (n > 1 ? name[1] : name[0]);
    }).join(", ");
  }

  var measureCtx = null;

  /**
   * How big a box has to be to hold `text` at `size`, wrapping at `maxW`.
   *
   * Measured with the map's own font in a canvas, because a box born two lines short
   * hides the tail of the writing: the textarea only grows while you type in it.
   */
  function textBox(text, size, maxW) {
    var pad = 18;             // the 8px inset of a text box, on both sides
    var lineH = size * 1.35;  // the textarea's own line-height
    if (!measureCtx) {
      var probe = document.createElement("canvas");
      measureCtx = probe.getContext ? probe.getContext("2d") : null;
    }
    var family = "sans-serif";
    try { family = getComputedStyle(document.body).fontFamily || family; } catch (err) { /* jsdom */ }
    if (measureCtx) measureCtx.font = size + "px " + family;
    function widthOf(s) {
      if (measureCtx && measureCtx.measureText) return measureCtx.measureText(s).width;
      return s.length * size * 0.55;   // no canvas to measure in: estimate
    }
    var room = Math.max(size * 4, maxW - pad);
    var widest = 0, lines = 0;
    text.split("\n").forEach(function (line) {
      var words = line.split(" ");
      var run = "";
      lines++;
      for (var i = 0; i < words.length; i++) {
        var next = run ? run + " " + words[i] : words[i];
        if (run && widthOf(next) > room) {
          widest = Math.max(widest, widthOf(run));
          run = words[i];
          lines++;
        } else {
          run = next;
        }
      }
      widest = Math.max(widest, widthOf(run));
    });
    return {
      w: Math.max(SHAPE_MIN, Math.min(maxW, Math.ceil(widest + pad))),
      h: Math.max(SHAPE_MIN, Math.ceil(lines * lineH + pad)),
    };
  }

  /** A font size from outside, brought into the range croqui draws in. */
  function pastedTextSize(px) {
    if (!px) return state.style.size || TEXT_SIZE;
    return Math.max(TEXT_RANGE[0], Math.min(TEXT_RANGE[1], Math.round(px)));
  }

  /**
   * Drop a pasted drawing on the map.
   *
   * The batch keeps the arrangement it was copied in: the whole group is moved so
   * its middle lands where the cursor is — the same place a pasted picture lands —
   * and it goes down in the order it came in, so what was in front stays in front.
   * A text box is measured (writing from outside carries no croqui box); a shape
   * brings its own size. Anything that lands on a node anchors to it, like any
   * other drawing.
   */
  function pasteDrawing(items, at) {
    if (!items.length) return 0;
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    var placed = items.map(function (it) {
      var out = {
        type: it.type, text: it.text || "", x: it.x, y: it.y, w: it.w, h: it.h,
        color: it.color, fill: it.fill, dash: it.dash, heads: it.heads,
        size: pastedTextSize(it.size), src: it.src, tie: it.tie,
        data: it.data, mime: it.mime,
      };
      if (it.type === "text") {
        var wrap = Math.min(PASTE_TEXT_MAX_W, Math.max(PASTE_TEXT_W, it.w || 0));
        var fit = textBox(it.text, out.size, wrap);
        out.w = fit.w;
        out.h = fit.h;
      }
      // Linear shapes keep their direction in the sign of w/h, so the corners of
      // the run are not simply x,y and x+w,y+h.
      minX = Math.min(minX, out.x, out.x + out.w);
      minY = Math.min(minY, out.y, out.y + out.h);
      maxX = Math.max(maxX, out.x, out.x + out.w);
      maxY = Math.max(maxY, out.y, out.y + out.h);
      return out;
    });
    var dx = at.x - (minX + maxX) / 2;
    var dy = at.y - (minY + maxY) / 2;
    pushHistory();
    // Pictures need a round trip to the server first; everything else goes down now.
    var made = placed.map(function (p) {
      if (p.type === "image") return null;
      return addShape(p.type, p.x + dx, p.y + dy, p.w, p.h, {
        batch: true, text: p.text, size: p.size,
        color: p.color, fill: p.fill, dash: p.dash, heads: p.heads,
      });
    });
    // Excalidraw arrows that held on to a box hold on to the copy of that box —
    // only a box that came along in the same copy, and never another arrow. Run
    // again once the pictures land, so an arrow into a picture holds on to it too.
    function tieArrows() {
      var copyOf = {};
      made.forEach(function (s, i) { if (s && placed[i].src) copyOf[placed[i].src] = s; });
      made.forEach(function (s, i) {
        var tie = placed[i].tie;
        if (!s || !tie || !isLinear(s)) return;
        var a = tie.from ? copyOf[tie.from] : null, b = tie.to ? copyOf[tie.to] : null;
        if (a && !isLinear(a) && !s.from) s.from = { shape: a.id };
        if (b && !isLinear(b) && b !== a && !s.to) s.to = { shape: b.id };
        if (hasEnds(s)) delete s.anchor;
      });
    }
    tieArrows();
    var drawn = made.filter(Boolean);
    // A single drawing comes selected, so it can be moved or typed into straight
    // away. A batch does not: the selection holds one thing, and picking the last of
    // them would be arbitrary.
    state.sel = drawn.length === 1 && drawn.length === placed.length ? { kind: "shape", id: drawn[0].id } : null;
    state.editing = null;
    markDirty();
    render();
    refreshInspector();

    var pictures = placed.map(function (p, i) { return p.type === "image" ? i : -1; })
      .filter(function (i) { return i >= 0; });
    if (pictures.length) placeCopiedImages(pictures, placed, made, dx, dy, tieArrows);
    return drawn.length + pictures.length;
  }

  /** `data:image/png;base64,…` -> Blob, without a fetch the page may not be allowed. */
  function dataUrlBlob(url, mime) {
    var m = /^data:([^;,]*)(;base64)?,([\s\S]*)$/.exec(url || "");
    if (!m) return null;
    try {
      var raw = m[2] ? atob(m[3]) : decodeURIComponent(m[3]);
      var bytes = new Uint8Array(raw.length);
      for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
      return new Blob([bytes], { type: mime || m[1] });
    } catch (err) {
      return null;
    }
  }

  /**
   * The pictures of an Excalidraw copy, stored and placed where they were drawn.
   *
   * Same road as a pasted screenshot: the bytes go to `.croqui/images/` and the
   * shape keeps only the name, so the layout stays small and reviewable. They go
   * in the slot the copy gave them, at the size they had there, and join the
   * history entry the rest of the paste opened — one Ctrl+Z takes the lot.
   */
  function placeCopiedImages(indexes, placed, made, dx, dy, tieArrows) {
    if (!state.canSave) {
      toast("as imagens do Excalidraw ficaram de fora — este croqui.html é só de leitura; use croqui serve");
      return;
    }
    state.pasting++;
    var failed = 0;
    Promise.all(indexes.map(function (i) {
      var p = placed[i];
      var blob = dataUrlBlob(p.data, p.mime);
      if (!blob || blob.size > MAX_PASTE_MB * 1024 * 1024) { failed++; return null; }
      return fetch("api/image", {
        method: "POST",
        headers: { "Content-Type": blob.type },
        body: blob,
      }).then(function (r) {
        return r.json().then(function (body) {
          if (!r.ok) throw new Error(body && body.error ? body.error : "http " + r.status);
          made[i] = addShape("image", p.x + dx, p.y + dy, p.w, p.h, {
            batch: true, src: body.name, alt: "imagem do Excalidraw", front: true,
          });
        });
      }).catch(function () { failed++; });
    })).then(function () {
      state.pasting--;
      tieArrows();
      markDirty();
      render();
      refreshInspector();
      var ok = indexes.length - failed;
      if (failed) toast(ok + " de " + indexes.length + " imagens do Excalidraw coladas — as outras falharam");
      else toast((ok > 1 ? ok + " imagens do Excalidraw coladas" : "imagem do Excalidraw colada") + savedHint());
    });
  }

  /** Put text where the caret is, keeping the browser's own undo for the box. */
  function insertAtCaret(field, text) {
    if (!text || field.readOnly || field.disabled) return;
    var inserted = false;
    try { inserted = document.execCommand("insertText", false, text); } catch (err) { inserted = false; }
    if (inserted) return;
    var start = field.selectionStart == null ? field.value.length : field.selectionStart;
    var end = field.selectionEnd == null ? start : field.selectionEnd;
    field.value = field.value.slice(0, start) + text + field.value.slice(end);
    var caret = start + text.length;
    try { field.setSelectionRange(caret, caret); } catch (err) { /* not selectable */ }
    // The shape reads its text off the box's own input event.
    field.dispatchEvent(new Event("input", { bubbles: true }));
  }

  /* ----------------------------------------------------------- drawing tools */

  function draftBox(d) {
    if (LINEAR[d.type]) return { x: d.sx, y: d.sy, w: d.ex - d.sx, h: d.ey - d.sy };
    return {
      x: Math.min(d.sx, d.ex), y: Math.min(d.sy, d.ey),
      w: Math.abs(d.ex - d.sx), h: Math.abs(d.ey - d.sy),
    };
  }

  /** The swept rectangle, in the overlay so it stays crisp at any zoom. */
  function drawMarquee(d) {
    var k = state.view.k || 1;
    var g = svgEl("g", { class: "draft" });
    g.appendChild(svgEl("rect", {
      class: "marquee",
      x: Math.min(d.sx, d.ex), y: Math.min(d.sy, d.ey),
      width: Math.abs(d.ex - d.sx), height: Math.abs(d.ey - d.sy),
      "stroke-width": 1.2 / k, "stroke-dasharray": 5 / k + " " + 4 / k,
    }));
    state.draft = g;
    renderOverlay();
  }

  function commitMarquee(d) {
    state.draft = null;
    renderOverlay();
    // A click on bare paper, without a sweep, is how you start over.
    if (!d.moved) {
      if (!d.add) { clearMarks(); closePanel(); }
      return;
    }
    // Shift means "and also", so a sweep adds rather than replaces.
    if (!d.add) { state.marked = {}; state.markedShapes = {}; }
    nodesIn(d).forEach(function (id) { state.marked[id] = true; });
    shapesIn(d).forEach(function (id) { state.markedShapes[id] = true; });
    refreshMarks();
    var n = marks().length + markedShapeIds().length;
    toast(n ? n + (n === 1 ? " item selecionado" : " itens selecionados") + " — arraste um deles" : "nada sob o retângulo");
  }

  /** Light up what the sweep would pick, while it is still growing. */
  function previewMarquee(d) {
    var nodes = {}, drawn = {};
    if (d.add) {
      Object.keys(state.marked).forEach(function (id) { nodes[id] = true; });
      Object.keys(state.markedShapes).forEach(function (id) { drawn[id] = true; });
    }
    if (d.moved) {
      nodesIn(d).forEach(function (id) { nodes[id] = true; });
      shapesIn(d).forEach(function (id) { drawn[id] = true; });
    }
    Object.keys(state.nodeEls || {}).forEach(function (id) {
      state.nodeEls[id].classList.toggle("marked", !!nodes[id]);
    });
    Object.keys(state.shapeEls || {}).forEach(function (id) {
      var refs = state.shapeEls[id];
      if (refs && refs.g) refs.g.classList.toggle("marked", !!drawn[id]);
    });
  }

  /** Every drawing whose box meets the swept rectangle. */
  function shapesIn(rect) {
    var area = {
      x: Math.min(rect.sx, rect.ex), y: Math.min(rect.sy, rect.ey),
      w: Math.abs(rect.ex - rect.sx), h: Math.abs(rect.ey - rect.sy),
    };
    return shapes().filter(function (s) {
      var b = shapeBox(s);
      // A line's box may have negative width or height: normalise it first.
      var n = { x: Math.min(b.x, b.x + b.w), y: Math.min(b.y, b.y + b.h), w: Math.abs(b.w), h: Math.abs(b.h) };
      n.w = Math.max(n.w, 1); n.h = Math.max(n.h, 1);
      return overlaps(area, n);
    }).map(function (s) { return s.id; });
  }

  function drawPreview(d) {
    var b = draftBox(d);
    var g = svgEl("g", { class: "draft", transform: "translate(" + b.x + "," + b.y + ")" });
    g.appendChild(svgEl("path", {
      d: LINEAR[d.type]
        ? sketchArrow(0, 0, b.w, b.h, 7, d.type === "arrow" ? state.style.heads : "none")
        : shapeOutline(d.type, Math.max(1, b.w), Math.max(1, b.h), 7, 1.6),
      fill: "none", stroke: paintOf(state.style.color), "stroke-width": 1.7, opacity: 0.85,
    }));
    state.draft = g;
    // Show which box the tip would hold on to.
    state.hint = LINEAR[d.type] ? endTargetAt(d.ex, d.ey) : null;
    renderOverlay();
  }

  /** A drag sets the size; a plain click drops one at a comfortable default. */
  function commitDraw(d, ev) {
    state.draft = null;
    state.hint = null;
    var b = draftBox(d);
    if (!d.moved) {
      var def = DEFAULT_SIZE[d.type] || [170, 110];
      b = LINEAR[d.type]
        ? { x: d.sx - def[0] / 2, y: d.sy, w: def[0], h: def[1] }
        : { x: d.sx - def[0] / 2, y: d.sy - def[1] / 2, w: def[0], h: def[1] };
    } else if (!LINEAR[d.type]) {
      b.w = Math.max(SHAPE_MIN, b.w);
      b.h = Math.max(SHAPE_MIN, b.h);
    }
    setTool("select");
    addShape(d.type, b.x, b.y, b.w, b.h, {
      edit: d.type === "text",
      // Ctrl while letting go keeps the arrow free of whatever it was drawn over.
      snap: !!LINEAR[d.type] && !(ev && ev.ctrlKey),
    });
  }

  function resizeTo(d, pos) {
    var s = d.ref, st = d.start;
    d.moved = true;
    if (isLinear(s)) {
      // The end in your hand is free until you let go; the other keeps aiming.
      var other = d.handle === "a" ? s.to : s.from;
      if (d.handle === "a") {
        delete s.from;
        s.x = Math.round(pos.x);
        s.y = Math.round(pos.y);
        s.w = Math.round(st.x + st.w - pos.x);
        s.h = Math.round(st.y + st.h - pos.y);
      } else {
        delete s.to;
        s.w = Math.round(pos.x - s.x);
        s.h = Math.round(pos.y - s.y);
      }
      resolveEnds(s);
      var cand = d.ctrl ? null : endTargetAt(pos.x, pos.y, s.id);
      state.hint = cand && !sameRef(cand, other) ? cand : null;
    } else {
      var left = st.x, top = st.y, right = st.x + st.w, bottom = st.y + st.h;
      if (d.handle.indexOf("w") >= 0) left = Math.min(pos.x, right - SHAPE_MIN);
      if (d.handle.indexOf("e") >= 0) right = Math.max(pos.x, left + SHAPE_MIN);
      if (d.handle.indexOf("n") >= 0) top = Math.min(pos.y, bottom - SHAPE_MIN);
      if (d.handle.indexOf("s") >= 0) bottom = Math.max(pos.y, top + SHAPE_MIN);
      s.x = Math.round(left);
      s.y = Math.round(top);
      s.w = Math.round(right - left);
      s.h = Math.round(bottom - top);
    }
    // A north or west handle moves the origin, so the offset has to follow.
    reanchor(s);
    applyShapeGeometry(s);
    if (!isLinear(s)) followEnds(keyed({ shape: s.id }));
    renderOverlay();
  }

  /* A relation is drawn either by dragging from one node to another or by
   * clicking each in turn — far apart on a big map, dragging is not an option. */
  function startLink(ev) {
    var g = ev.target.closest ? ev.target.closest(".node") : null;
    var id = g && g.getAttribute("data-id");
    if (!id) {
      if (state.linkFrom) { state.linkFrom = null; toast("relação cancelada"); }
      else setTool("select");
      return null;
    }
    if (state.linkFrom && state.linkFrom !== id) {
      var from = state.linkFrom;
      state.linkFrom = null;
      setTool("select");
      addLink(from, id);
      return null;
    }
    state.linkFrom = id;
    return { kind: "linking", from: id, to: null };
  }

  function drawLinkPreview(d) {
    var a = state.boxes[d.from];
    if (!a || !d.to) return;
    var g = svgEl("g", { class: "draft" });
    g.appendChild(svgEl("path", {
      d: sketchArrow(a.x + a.w / 2, a.y + a.h / 2, d.to.x, d.to.y, 11, "end"),
      fill: "none", stroke: paintOf(state.style.color), "stroke-width": 1.6,
      "stroke-dasharray": "6 5", opacity: 0.8,
    }));
    state.draft = g;
    renderOverlay();
  }

  function finishLink(d, ev) {
    state.draft = null;
    renderOverlay();
    var g = hitAt(ev.clientX, ev.clientY, ".node");
    var id = g && g.getAttribute("data-id");
    if (!id || id === d.from) { toast("agora clique no nó de destino"); return; }
    state.linkFrom = null;
    setTool("select");
    addLink(d.from, id);
  }

  function setTool(tool) {
    tool = TOOLS.indexOf(tool) >= 0 ? tool : "select";
    state.tool = tool;
    if (tool !== "link") state.linkFrom = null;
    state.draft = null;
    // Arming a tool drops the selection: otherwise its handles stay live under
    // the crosshair and the first drag resizes the old shape.
    var hadSel = !!state.sel;
    if (tool !== "select") state.sel = null;
    if (el.tools) {
      Array.prototype.forEach.call(el.tools.querySelectorAll("[data-tool]"), function (b) {
        b.setAttribute("aria-pressed", String(b.getAttribute("data-tool") === tool));
      });
    }
    el.canvas.classList.toggle("drawing", tool !== "select" && tool !== "link");
    el.canvas.classList.toggle("linking", tool === "link");
    if (hadSel && !state.sel) render();
    else renderOverlay();
    refreshInspector();
    if (tool === "link") toast("clique na origem, depois no destino");
  }

  /* ------------------------------------------------------------- inspector */

  function show(node, visible) {
    if (node) node.hidden = !visible;
  }

  function press(node, on) {
    if (node) node.setAttribute("aria-pressed", String(!!on));
  }

  function refreshInspector() {
    if (!el.inspector) return;
    var s = selectedShape();
    var l = selectedLink();
    var target = s || l;
    if (!target && state.tool === "select") {
      el.inspector.hidden = true;
      return;
    }
    el.inspector.hidden = false;

    var kind = l ? "link" : (s ? s.type : state.tool);
    var linear = kind === "arrow" || kind === "line";
    var picture = !!s && isImage(s);
    var style = target || state.style;

    // A picture has no stroke, no fill and no letters — offering the palette would
    // be offering a control that does nothing.
    show(el.insColors, !picture);
    Array.prototype.forEach.call(el.insColors.children, function (b) {
      press(b, b.getAttribute("data-color") === (style.color || "ink"));
    });
    var writable = !linear && kind !== "link" && !picture;
    show(el.insSize, writable);
    if (writable && el.insSizeVal) {
      el.insSizeVal.textContent = (s ? (s.size || TEXT_SIZE) : state.style.size) + "px";
    }
    show(el.insFill, writable);
    press(el.insFill && el.insFill.firstElementChild, style.fill);
    show(el.insDashRow, !picture);
    press(el.insDash, style.dash);
    show(el.insHeads, !picture && (kind === "arrow" || kind === "link"));
    Array.prototype.forEach.call(el.insHeads ? el.insHeads.children : [], function (b) {
      if (b.hasAttribute("data-heads")) press(b, b.getAttribute("data-heads") === (style.heads || "end"));
    });
    show(el.insLabelRow, !!l);
    if (l && document.activeElement !== el.insLabel) el.insLabel.value = l.label || "";
    show(el.insLayer, !!s);
    press(el.insLayer && el.insLayer.firstElementChild, s && s.front);

    // Only offered when there is a node to attach to — an anchor to nothing is
    // worse than no anchor, because it looks like it is holding.
    var host = anchorTarget(s);
    // An arrow holding on by its ends says what it holds; pressing lets go.
    var tied = !!s && isLinear(s) && hasEnds(s);
    show(el.insAnchor, !!host || tied);
    if (tied && el.insAnchor) {
      var release = el.insAnchor.firstElementChild;
      release.textContent = "ligada: " + endsSummary(s);
      release.title = "soltar as pontas — a seta para de seguir as caixas";
      press(release, true);
    } else if (host && el.insAnchor) {
      var button = el.insAnchor.firstElementChild;
      var held = !!(s.anchor && s.anchor.node);
      button.textContent = (held ? "preso a " : "prender a ") + nodeName(host);
      button.title = held
        ? "soltar — o desenho para de seguir o nó"
        : "o desenho passa a seguir este nó, inclusive depois de um croqui scan";
      press(button, held);
    }

    show(el.insDelete, !!target);
    show(el.insHint, !target);
  }

  /** Styling with something selected restyles it; with nothing, sets the default. */
  function applyStyle(patch) {
    var target = selectedShape() || selectedLink();
    Object.keys(patch).forEach(function (k) {
      if (k in state.style) state.style[k] = patch[k];
    });
    if (target) {
      pushHistory();
      Object.keys(patch).forEach(function (k) { target[k] = patch[k]; });
      markDirty();
      render();
    }
    refreshInspector();
  }

  /**
   * Font size of the selected drawing's text — its own control, not part of
   * `applyStyle`, because it is the one style that changes geometry: a bigger
   * font can outgrow the box it is in, so the box has to be refitted after.
   */
  function applyTextSize(delta) {
    var s = selectedShape();
    var base = s && !isLinear(s) ? (s.size || TEXT_SIZE) : state.style.size;
    var next = Math.max(TEXT_RANGE[0], Math.min(TEXT_RANGE[1], base + delta));
    if (next === base) return;
    state.style.size = next;
    if (s && !isLinear(s)) {
      pushHistory();
      s.size = next;
      markDirty();
      render();
      var refs = state.shapeEls[s.id];
      if (refs && refs.editor) refs.editor.grow();
    }
    refreshInspector();
  }

  function redrawEdgesFor(id) {
    state.edgeEls.forEach(function (item) {
      var e = item.edge;
      if (e.from !== id && e.to !== id) return;
      var a = state.boxes[e.from], b = state.boxes[e.to];
      if (!a || !b) return;
      if (item.link) {
        var geo = sketchLink(anchorPair(a, b), hashCode(e.id || e.from + e.to), e.heads || "end");
        item.link.path.setAttribute("d", geo.d);
        item.link.hit.setAttribute("d", geo.d);
        positionLinkLabel(item.link, geo);
        return;
      }
      item.path.setAttribute("d", sketchEdge(a.x + a.w, a.y + a.h / 2, b.x, b.y + b.h / 2, hashCode(e.from + e.to)));
    });
  }

  function allGroups() {
    var names = {};
    state.graph.nodes.forEach(function (n) {
      if (n.kind === "route" && routePasses(n)) names[groupKey(n)] = true;
    });
    return Object.keys(names);
  }

  /** A missing toolbar element should not blank the whole map. */
  function on(target, event, handler) {
    if (target) target.addEventListener(event, handler);
    else if (window.console) console.warn("croqui: elemento ausente para " + event);
  }

  function wireChrome() {
    on(el.panel, "click", function (ev) {
      var t = ev.target;
      if (!t.getAttribute) return;
      var detail = t.getAttribute("data-detail");
      if (detail) return openPanel(detail);
      var expand = t.getAttribute("data-expand");
      if (expand) {
        state.expanded[expand] = true;
        closePanel();
        render();
        syncChrome();
        fitToView();
        rememberView();
        return;
      }
      var focus = t.getAttribute("data-focus");
      if (focus) return setFocus(focus);
      if (t.getAttribute("data-drop-orphans")) return dropOrphans();
      var hide = t.getAttribute("data-hide");
      if (hide) {
        pushHistory();
        state.layout.hidden.push(hide);
        markDirty();
        closePanel();
        render();
      }
    });
    on(el.panelClose, "click", closePanel);

    on(el.orphans, "click", openOrphanPanel);
    on(el.marked, "click", clearMarks);

    on(el.search, "input", function () {
      state.query = el.search.value.trim();
      applyEmphasis();
      rememberView();
    });

    on(el.methodChips, "click", function (ev) {
      var chip = ev.target.closest("[data-method]");
      if (!chip) return;
      var m = chip.getAttribute("data-method");
      if (state.methods.has(m)) state.methods.delete(m);
      else state.methods.add(m);
      chip.setAttribute("aria-pressed", state.methods.has(m));
      render();
      rememberView();
    });

    on(el.toggleInternal, "click", function () {
      state.showInternal = !state.showInternal;
      el.toggleInternal.setAttribute("aria-pressed", state.showInternal);
      rememberView();
      render();
    });

    on(el.toggleHover, "click", function () {
      state.hoverHighlight = !state.hoverHighlight;
      el.toggleHover.setAttribute("aria-pressed", state.hoverHighlight);
      state.hover = null;
      applyEmphasis();
      rememberView();
      toast(state.hoverHighlight
        ? "realce ligado — clique num quadro para fixá-lo"
        : "realce desligado");
    });

    on(el.panelWide, "click", function () {
      var wide = el.panel.classList.toggle("wide");
      el.panelWide.textContent = wide ? "⇥" : "⇤";
      el.panelWide.title = wide ? "estreitar painel" : "alargar painel";
    });

    on(el.expandAll, "click", function () {
      var groups = allGroups();
      var anyCollapsed = groups.some(function (g) { return !state.expanded[g]; });
      state.expanded = {};
      if (anyCollapsed) groups.forEach(function (g) { state.expanded[g] = true; });
      render();
      syncChrome();
      fitToView();
      rememberView();
    });

    on(el.tabs, "click", function (ev) {
      var b = ev.target.closest ? ev.target.closest("[data-tab]") : null;
      if (b) setTab(b.getAttribute("data-tab"));
    });
    on(el.docList, "click", function (ev) {
      var b = ev.target.closest ? ev.target.closest("[data-doc]") : null;
      if (b) openDoc(state.tab, +b.getAttribute("data-doc"));
    });
    on(el.docBody, "click", function (ev) {
      var b = ev.target.closest ? ev.target.closest(".prd-card[data-doc]") : null;
      if (b) openDoc("prd", +b.getAttribute("data-doc"));
    });
    on(el.docBack, "click", openGallery);

    on(el.fit, "click", fitToView);
    on(el.reset, "click", function () {
      if (!confirm("Descartar posições, ocultos, desenhos e relações? (Ctrl+Z desfaz)")) return;
      pushHistory();
      state.layout.positions = {};
      state.layout.hidden = [];
      state.layout.shapes = [];
      state.layout.links = [];
      state.sel = null;
      state.editing = null;
      markDirty();
      render();
      refreshOrphans();
      refreshInspector();
      fitToView();
    });
    on(el.undo, "click", undo);
    on(el.redo, "click", redo);
    on(el.clearFocus, "click", function () { setFocus(null); });
    on(el.save, "click", saveLayout);
    on(el.hand, "click", function () {
      document.body.classList.toggle("hand");
      el.hand.setAttribute("aria-pressed", document.body.classList.contains("hand"));
      render();
    });

    /* Ctrl+V anywhere on the page drops what is on the clipboard on the map: a
     * picture becomes an image, writing becomes text boxes, and an Excalidraw copy
     * becomes the shapes and arrows it holds. Bound to the
     * document rather than the canvas because the canvas is not focusable, so a
     * paste never reaches it. An Excalidraw copy arrives as JSON; what goes onto
     * the map is the writing in it, not the JSON. */
    document.addEventListener("paste", function (ev) {
      var data = ev.clipboardData;
      var typing = !!ev.target && (ev.target.tagName === "INPUT" || ev.target.tagName === "TEXTAREA");

      if (!typing) {
        var blob = clipboardImage(data);
        if (blob) { ev.preventDefault(); pasteImage(blob, pastePoint()); return; }
      }

      var text = clipboardText(data);
      if (!text) return;
      var copy = excalidrawDrawing(text);
      // A clipboard holding nothing but blank space has nothing to write down.
      if (!copy && !text.trim()) return;

      // Inside a text box the clipboard belongs to the caret. An Excalidraw copy is
      // the exception: what the caret wants from it is the writing, not the JSON.
      if (typing) {
        if (!copy) return;
        ev.preventDefault();
        insertAtCaret(ev.target, copyWriting(copy));
        return;
      }
      // Off the map there is nowhere for a drawing to land.
      if (state.tab !== "map") return;
      ev.preventDefault();

      if (copy) {
        var left = skippedSummary(copy.skipped);
        if (!copy.items.length) {
          toast(left ? "no que você copiou só tem " + left + " — o croqui não desenha isso"
                     : "nada que o croqui saiba desenhar no que você copiou");
          return;
        }
        var n = pasteDrawing(copy.items, pastePoint());
        toast(n + (n > 1 ? " desenhos colados" : " desenho colado") +
              (left ? " — fora: " + left : "") + savedHint());
        return;
      }
      if (text.length > MAX_PASTE_TEXT) {
        toast("texto acima de " + MAX_PASTE_TEXT + " caracteres — cole em partes");
        return;
      }
      pasteDrawing([{
        type: "text",
        text: text.replace(/\r\n?/g, "\n").replace(/\s+$/, ""),
        x: 0, y: 0, w: 0, h: 0, size: 0,
      }], pastePoint());
      toast("texto colado" + savedHint());
    });

    // Space held is the hand on the UML stage, as in Excalidraw: Space + drag pans.
    document.addEventListener("keyup", function (ev) {
      if (ev.key !== " ") return;
      state.spaceDown = false;
      document.body.classList.remove("space-pan");
    });
    window.addEventListener("blur", function () {
      state.spaceDown = false;
      document.body.classList.remove("space-pan");
    });

    document.addEventListener("keydown", function (ev) {
      var typing = ev.target.tagName === "INPUT" || ev.target.tagName === "TEXTAREA";
      var mod = ev.ctrlKey || ev.metaKey;
      if (!typing && ev.key === " " && (state.tab === "map" || state.stage)) {
        // Otherwise the page scrolls and a focused button gets pressed.
        ev.preventDefault();
        if (!state.spaceDown) {
          state.spaceDown = true;
          document.body.classList.add("space-pan");
        }
        return;
      }
      if (!typing && mod && ev.key.toLowerCase() === "a") {
        if (state.tab === "map") { ev.preventDefault(); markEverything(); return; }
        if (state.stage && state.stage.__selectAll) { ev.preventDefault(); state.stage.__selectAll(); return; }
      }

      // Inside a text box, Ctrl+Z is the browser's text undo — don't hijack it.
      if (mod && !typing) {
        var key = ev.key.toLowerCase();
        if (key === "z" && !ev.shiftKey) { ev.preventDefault(); undo(); return; }
        if ((key === "z" && ev.shiftKey) || key === "y") { ev.preventDefault(); redo(); return; }
      }
      if (mod && ev.key.toLowerCase() === "s") { ev.preventDefault(); saveLayout(); return; }

      if (typing) {
        if (ev.key === "Escape") ev.target.blur();
        return;
      }
      if (ev.key === "Escape") {
        // On the stage, Escape first lets go of the selection, like any canvas.
        if (state.stage && state.stage.__hasPick && state.stage.__hasPick()) {
          state.stage.__clear();
          return;
        }
        if (state.tab !== "map") { setTab("map"); return; }
        if (state.tool !== "select") setTool("select");
        else if (state.sel) pick(null, null);
        else if (clearMarks()) return;
        else if (state.focus) setFocus(null);
        else closePanel();
        return;
      }
      if (ev.key === "Delete" || ev.key === "Backspace") {
        if (state.sel) { ev.preventDefault(); deleteSelection(); }
        return;
      }
      if (mod || ev.altKey) return;
      // The reading tabs have no drawing tools, but a diagram you can pan is a
      // diagram you can lose, so `f` means the same thing here as on the map.
      if (state.tab !== "map") {
        if (ev.key === "m") setTab("map");
        else if (state.stage && ev.key === "f") state.stage.__fit();
        else if (state.stage && (ev.key === "+" || ev.key === "=")) zoomStage(0.8);
        else if (state.stage && (ev.key === "-" || ev.key === "_")) zoomStage(1.25);
        return;
      }
      var tool = TOOL_KEYS[ev.key.toLowerCase()];
      if (tool) { setTool(tool); return; }
      if (ev.key === "f") fitToView();
      else if (ev.key === "/") { ev.preventDefault(); el.search.focus(); }
    });
  }

  /** Zoom the open UML stage about its own centre, for the keyboard. */
  function zoomStage(factor) {
    var v = state.stage && state.stage.__view;
    if (!v) return;
    state.stage.__zoom(factor, v.x + v.w / 2, v.y + v.h / 2);
  }

  function wireTools() {
    PALETTE.forEach(function (entry) {
      var b = document.createElement("button");
      b.className = "swatch";
      b.setAttribute("data-color", entry[0]);
      b.setAttribute("aria-pressed", "false");
      b.title = entry[0];
      b.style.setProperty("--swatch", entry[1]);
      el.insColors.appendChild(b);
    });

    on(el.tools, "click", function (ev) {
      var b = ev.target.closest("[data-tool]");
      if (b) setTool(b.getAttribute("data-tool"));
    });

    on(el.inspector, "click", function (ev) {
      var b = ev.target.closest("button");
      if (!b) return;
      if (b.hasAttribute("data-color")) return applyStyle({ color: b.getAttribute("data-color") });
      if (b.hasAttribute("data-heads")) return applyStyle({ heads: b.getAttribute("data-heads") });
      if (b.hasAttribute("data-size")) return applyTextSize(+b.getAttribute("data-size") * TEXT_STEP);
      var flag = b.getAttribute("data-flag");
      if (flag === "fill" || flag === "dash") {
        var cur = selectedShape() || selectedLink() || state.style;
        return applyStyle(flag === "fill" ? { fill: !cur.fill } : { dash: !cur.dash });
      }
      if (flag === "front") {
        var s = selectedShape();
        if (s) applyStyle({ front: !s.front });
        return;
      }
      if (flag === "anchor") return toggleAnchor();
      if (b.hasAttribute("data-del")) deleteSelection();
    });

    // One history entry per editing session, as with the text boxes.
    var labelPre = null;
    on(el.insLabel, "focus", function () { labelPre = snapshot(); });
    on(el.insLabel, "blur", function () { labelPre = null; });
    on(el.insLabel, "input", function () {
      var l = selectedLink();
      if (!l) return;
      if (labelPre !== null) { pushHistory(labelPre); labelPre = null; }
      l.label = el.insLabel.value;
      markDirty();
      var refs = state.linkEls[l.id];
      if (!refs || !refs.label) { render(); return; }
      refs.label.textContent = l.label;
      // Keep the curve untouched and just re-centre the plate on the same point.
      positionLinkLabel(refs, { mid: [+refs.label.getAttribute("x"), +refs.label.getAttribute("y") - 4] });
    });
  }

  function setFocus(id) {
    state.focus = id;
    render();
    syncChrome();
    fitToView();
    rememberView();
  }


  /* ============================================================ documentation
   *
   * Two more tabs on the same page: the PRD you wrote and the mermaid diagrams you
   * (or an LLM) wrote next to it. Both are read straight off disk and never written
   * back, so nothing here touches the layout, the history or the save button.
   *
   * The diagrams are drawn by croqui rather than by mermaid.js. Parsing and layout
   * live in diagram.js; this is the ink, and it goes through the same seeded-wobble
   * primitives as the map — a document whose three tabs are drawn in three
   * different hands reads as three documents.
   */

  var DIAGRAM_PAD = 8;
  // An inline diagram lives in the flow of a PRD, so it may grow to the column but
  // not beyond this much of its natural size — past that the wobble reads as
  // sloppiness rather than as pencil.
  var INLINE_UPSCALE = 1.9;
  // The UML tab's own viewport, as a multiple of the fit: how far in you can go
  // to read one label, how far out before the picture is a smudge.
  var STAGE_MIN = 0.25, STAGE_MAX = 8;

  function docSections() {
    return ["prd", "uml"].filter(function (k) { return (state.docs[k] || []).length; });
  }

  /** Build the tab strip: only tabs that have something behind them. */
  function syncTabs() {
    if (!el.tabs) return;
    var wanted = ["map"].concat(docSections());
    el.tabs.textContent = "";
    wanted.forEach(function (key) {
      var b = document.createElement("button");
      b.className = "chip tab";
      b.setAttribute("data-tab", key);
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", String(state.tab === key));
      var n = key === "map" ? 0 : state.docs[key].length;
      b.textContent = key === "map" ? "mapa" : key.toUpperCase();
      if (n > 1) {
        var tally = document.createElement("small");
        tally.textContent = n;
        b.appendChild(tally);
      }
      el.tabs.appendChild(b);
    });
    // One tab is no choice at all.
    el.tabs.hidden = wanted.length < 2;
  }

  /** Chrome that only means anything over the map. */
  function syncTabChrome() {
    var onMap = state.tab === "map";
    document.body.classList.toggle("reading", !onMap);
    if (el.canvas) el.canvas.hidden = !onMap;
    if (el.doc) el.doc.hidden = onMap;
    if (!onMap) {
      closePanel();
      if (el.inspector) el.inspector.hidden = true;
    } else {
      refreshInspector();
    }
  }

  function setTab(tab) {
    if (tab !== "map" && !(state.docs[tab] || []).length) tab = "map";
    state.tab = tab;
    syncTabs();
    syncTabChrome();
    if (tab === "prd" && state.picked.prd < 0) openGallery();
    else if (tab !== "map") openDoc(tab, state.picked[tab] || 0);
  }

  /** The list of documents down the left of a reading tab. */
  function renderDocList(section) {
    if (!el.docList) return;
    var list = state.docs[section] || [];
    el.docList.textContent = "";
    // A single document needs no picker; the title above it already names it.
    el.docList.hidden = list.length < 2;
    list.forEach(function (doc, i) {
      var b = document.createElement("button");
      b.className = "doc-item" + (i === state.picked[section] ? " on" : "");
      b.setAttribute("data-doc", i);
      var t = document.createElement("strong");
      t.textContent = doc.title;
      var f = document.createElement("span");
      f.textContent = doc.name;
      b.appendChild(t);
      b.appendChild(f);
      el.docList.appendChild(b);
    });
  }

  /** Plain text of a document, for the excerpt on its card. */
  function docExcerpt(doc, max) {
    var text = String(doc.text || "");
    if (doc.format === "html") {
      text = text.replace(/<(script|style|head|title)[\s\S]*?<\/\1>/gi, " ").replace(/<[^>]+>/g, " ")
        .replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">");
    } else {
      text = text.replace(/```[\s\S]*?```/g, " ")
        .replace(/^\s{0,3}#{1,6}\s+.*$/m, " ")              // the H1 is already the card title
        .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
        .replace(/^\s*([-*+]|\d+\.|>|\|)\s*/gm, "")
        .replace(/[*_`#|]/g, "");
    }
    text = text.replace(/\s+/g, " ").trim();
    return text.length > max ? text.slice(0, max - 1).replace(/\s+\S*$/, "") + "…" : text;
  }

  /**
   * The PRD tab's front page: one card per document, newest thinking side by side.
   *
   * A PRD folder grows one file per feature, and a picker of file names down the
   * side says nothing about which one you want. A card says what each is about
   * before you open it; clicking opens it, and the back link returns here.
   */
  function openGallery() {
    var list = state.docs.prd || [];
    if (!list.length) { setTab("map"); return; }
    state.picked.prd = -1;
    state.stage = null;
    if (el.docList) { el.docList.textContent = ""; el.docList.hidden = true; }
    if (el.docBack) el.docBack.hidden = true;
    if (el.docTitle) el.docTitle.textContent = "PRDs";
    if (el.docFile) el.docFile.textContent = list.length + (list.length === 1 ? " documento" : " documentos");
    if (!el.docBody) return;
    el.docBody.textContent = "";
    el.docBody.scrollTop = 0;
    el.docBody.className = "prd-gallery";
    var grid = document.createElement("div");
    grid.className = "prd-cards";
    list.forEach(function (doc, i) {
      var card = document.createElement("button");
      card.type = "button";
      card.className = "prd-card";
      card.setAttribute("data-doc", i);
      var badge = document.createElement("span");
      badge.className = "prd-card-kind " + (doc.format === "html" ? "html" : "md");
      badge.textContent = doc.format === "html" ? "HTML" : "MD";
      var title = document.createElement("strong");
      title.textContent = doc.title;
      var body = document.createElement("p");
      body.textContent = docExcerpt(doc, 220) || "(sem texto)";
      var file = document.createElement("span");
      file.className = "prd-card-file";
      file.textContent = doc.name;
      card.appendChild(badge);
      card.appendChild(title);
      card.appendChild(body);
      card.appendChild(file);
      grid.appendChild(card);
    });
    el.docBody.appendChild(grid);
  }

  function openDoc(section, index) {
    var list = state.docs[section] || [];
    if (!list.length) { setTab("map"); return; }
    index = Math.max(0, Math.min(list.length - 1, index | 0));
    state.picked[section] = index;
    var doc = list[index];
    renderDocList(section);
    if (el.docBack) el.docBack.hidden = section !== "prd";
    if (el.docTitle) el.docTitle.textContent = doc.title;
    if (el.docFile) el.docFile.textContent = doc.name;
    if (!el.docBody) return;
    el.docBody.textContent = "";
    el.docBody.scrollTop = 0;
    el.docBody.className = section === "uml" ? "uml" : "prd";
    state.stage = null;

    if (section === "prd" && doc.format === "html") {
      // Someone else's page, scripts and all: it runs in a frame of its own with
      // no access to the viewer, the layout it is editing, or the server.
      var frame = document.createElement("iframe");
      frame.className = "prd-frame";
      frame.setAttribute("sandbox", "allow-scripts allow-popups allow-popups-to-escape-sandbox");
      frame.setAttribute("referrerpolicy", "no-referrer");
      frame.title = doc.title;
      // Served, the page loads by URL so its relative pictures and stylesheets
      // resolve; in a bundle there is no server, and build already inlined them.
      if (window.__CROQUI__) frame.srcdoc = doc.text;
      else frame.src = "prd/" + doc.name.split("/").map(encodeURIComponent).join("/");
      el.docBody.classList.add("framed");
      el.docBody.appendChild(frame);
      return;
    }

    if (section === "uml") {
      // The whole panel, with its own viewport: this tab has one job, and a
      // diagram you cannot zoom into is a diagram whose relations you cannot read.
      var card = diagramCard(doc.text, doc.name, true);
      el.docBody.appendChild(card);
      state.stage = card.querySelector(".dg-stage");
      // A diagram croqui cannot draw comes back as a source listing, which wants
      // the padding and the scrollbar the stage gives up.
      if (state.stage) el.docBody.classList.add("staged");
      return;
    }
    // Markdown is escaped inside mdToHtml before any transform runs; drawFences
    // then reads each mermaid block back out of the DOM as text.
    el.docBody.innerHTML = mdToHtml(doc.text);
    drawFences(el.docBody);
  }

  /** Swap every ```mermaid block in a rendered PRD for a drawn diagram. */
  function drawFences(host) {
    Array.prototype.slice.call(host.querySelectorAll("pre.mermaid-block")).forEach(function (pre) {
      var card = diagramCard(pre.textContent, null);
      pre.parentNode.replaceChild(card, pre);
    });
  }

  /** One diagram, plus whatever croqui could not make sense of. */
  function diagramCard(source, name, stage) {
    var wrap = document.createElement("figure");
    wrap.className = "diagram";
    var model = null;
    try {
      model = window.croquiMermaid ? window.croquiMermaid.parse(source) : null;
    } catch (err) {
      // A parser bug must not take the tab down with it.
      model = { kind: "broken", message: String(err && err.message || err), source: source };
    }
    if (!model) {
      wrap.appendChild(note("diagram.js não carregou — o diagrama fica como texto"));
      wrap.appendChild(sourceBlock(source));
      return wrap;
    }
    if (model.kind === "flowchart" || model.kind === "sequence") {
      var svg = model.kind === "flowchart" ? drawFlow(model, stage) : drawSequence(model, stage);
      if (stage) wrap.classList.add("full");
      wrap.appendChild(stage ? diagramStage(svg, model) : svg);
      (model.warnings || []).forEach(function (w) { wrap.appendChild(note(w)); });
      return wrap;
    }
    if (model.kind === "empty") {
      wrap.appendChild(note("diagrama vazio" + (name ? " em " + name : "")));
      return wrap;
    }
    if (model.kind === "broken") {
      wrap.appendChild(note("não consegui ler este diagrama — " + model.message));
      wrap.appendChild(sourceBlock(model.source));
      return wrap;
    }
    // Unsupported: never silently blank. The source is the document.
    wrap.appendChild(note(
      "croqui ainda não desenha " + (model.diagram || "este tipo de diagrama") +
      " — abaixo está o que você escreveu, sem alterações"
    ));
    wrap.appendChild(sourceBlock(model.source));
    return wrap;
  }

  function note(text) {
    var d = document.createElement("p");
    d.className = "diagram-note";
    d.textContent = text;
    return d;
  }

  function sourceBlock(text) {
    var pre = document.createElement("pre");
    var code = document.createElement("code");
    code.textContent = String(text == null ? "" : text);
    pre.appendChild(code);
    return pre;
  }

  /**
   * An <svg> for one diagram.
   *
   * Inline (in a PRD) it flows with the prose: it fills the column and scales down
   * on a narrow screen, but is capped so a three-node diagram does not become a
   * poster. On the UML tab it is a stage instead — it takes the whole panel and
   * `stageOn` drives its viewBox, because a diagram squeezed into a text column is
   * a diagram whose relations you cannot follow, which is the only reason the tab
   * exists.
   */
  function diagramSvg(model, stage) {
    var world = {
      x: -DIAGRAM_PAD, y: -DIAGRAM_PAD,
      w: model.width + DIAGRAM_PAD * 2, h: model.height + DIAGRAM_PAD * 2,
    };
    var svg = svgEl("svg", {
      class: "diagram-svg" + (stage ? " staged" : ""),
      viewBox: world.x + " " + world.y + " " + world.w + " " + world.h,
      preserveAspectRatio: stage ? "xMidYMid meet" : "xMinYMin meet",
    });
    svg.__world = world;
    if (stage) return svg;
    svg.setAttribute("width", model.width);
    svg.setAttribute("height", model.height);
    svg.style.width = "100%";
    svg.style.maxWidth = Math.round(model.width * INLINE_UPSCALE) + "px";
    svg.style.height = "auto";
    return svg;
  }

  /**
   * Wrap a drawn diagram in the UML tab's viewport: fit on open, wheel to zoom
   * about the cursor, drag to pan, and a readout that doubles as the way back.
   *
   * Pan and zoom are the viewBox, never a transform on the contents. The ink is
   * already positioned in diagram coordinates and the sketch wobble is baked into
   * the path data, so scaling the paths would scale the pencil stroke with them —
   * moving the window instead keeps a 400% zoom drawn by the same hand as a 100%
   * one.
   */
  function diagramStage(svg, model) {
    var stage = document.createElement("div");
    stage.className = "dg-stage";
    var world = svg.__world;
    var view = { x: world.x, y: world.y, w: world.w, h: world.h };

    function apply() {
      svg.setAttribute("viewBox", view.x + " " + view.y + " " + view.w + " " + view.h);
      if (out) out.textContent = Math.round((world.w / view.w) * 100) + "%";
    }

    function fit() {
      view.x = world.x; view.y = world.y; view.w = world.w; view.h = world.h;
      apply();
    }

    /** Zoom about a point held still, in world coordinates. */
    function zoomAt(factor, wx, wy) {
      var want = view.w * factor;
      var lo = world.w / STAGE_MAX, hi = world.w / STAGE_MIN;
      want = Math.max(lo, Math.min(hi, want));
      factor = want / view.w;
      view.x = wx - (wx - view.x) * factor;
      view.y = wy - (wy - view.y) * factor;
      view.w *= factor;
      view.h *= factor;
      apply();
    }

    /** Client pixels -> diagram coordinates, for zooming under the cursor. */
    function at(ev) {
      var box = svg.getBoundingClientRect();
      if (!box.width || !box.height) return { x: view.x + view.w / 2, y: view.y + view.h / 2 };
      // `meet` letterboxes: the drawn scale is the smaller of the two, and the
      // slack is split evenly. Ignoring it makes the cursor drift while zooming.
      var scale = Math.min(box.width / view.w, box.height / view.h);
      return {
        x: view.x + (ev.clientX - box.left - (box.width - view.w * scale) / 2) / scale,
        y: view.y + (ev.clientY - box.top - (box.height - view.h * scale) / 2) / scale,
      };
    }

    svg.addEventListener("wheel", function (ev) {
      ev.preventDefault();
      var here = at(ev);
      zoomAt(Math.exp(ev.deltaY * 0.0016), here.x, here.y);
    }, { passive: false });

    /* The gestures are Excalidraw's, because that is the hand people draw with:
     * dragging on paper sweeps a selection, dragging a node moves it (with the
     * rest of the selection), and navigating is Space + drag — or the middle
     * button — with the grabbing hand. A selection lights up its relations and
     * fades everything else, which is how you read one chain out of a dense
     * diagram. Moves are for reading: the diagram is the .mmd file, and croqui
     * does not rewrite it. */
    var flow = svg.__flow || null;
    var picked = {};
    var hoverId = null;
    var mode = null;
    var marquee = null;

    function pickedIds() { return Object.keys(picked); }

    function lightUp() {
      if (!flow) return;
      var ids = pickedIds();
      var focus = ids.length ? ids : (hoverId ? [hoverId] : []);
      var lit = {}, near = {};
      focus.forEach(function (id) { lit[id] = true; });
      flow.edges.forEach(function (item) {
        var hot = lit[item.e.from] || lit[item.e.to];
        item.g.classList.toggle("hot", !!hot);
        if (hot) { near[item.e.from] = true; near[item.e.to] = true; }
      });
      Object.keys(flow.nodes).forEach(function (id) {
        flow.nodes[id].classList.toggle("sel", !!picked[id]);
        flow.nodes[id].classList.toggle("near", !!near[id] && !lit[id]);
        flow.nodes[id].classList.toggle("lit", !!lit[id]);
      });
      svg.classList.toggle("has-focus", focus.length > 0);
    }

    function setPicked(ids, add) {
      if (!add) picked = {};
      ids.forEach(function (id) { picked[id] = true; });
      lightUp();
    }

    function nodeAt(ev) {
      var g = ev.target.closest ? ev.target.closest(".dg-node") : null;
      return g && flow ? g.getAttribute("data-id") : null;
    }

    function moveNodes(ids, dx, dy) {
      var dirty = {};
      ids.forEach(function (id) {
        var n = flow.byId[id];
        n.x = mode.origin[id].x + dx;
        n.y = mode.origin[id].y + dy;
        flow.nodes[id].setAttribute("transform", "translate(" + n.x + "," + n.y + ")");
        (flow.touching[id] || []).forEach(function (item) { dirty[item.i] = item; });
      });
      Object.keys(dirty).forEach(function (k) { paintEdge(dirty[k], flow.byId); });
      refitFrames(flow);
    }

    /** Draw the sweep rectangle; returns the ids of the nodes it touches. */
    function drawMarquee() {
      var x = Math.min(marquee.x1, marquee.x2), y = Math.min(marquee.y1, marquee.y2);
      var w = Math.abs(marquee.x2 - marquee.x1), h = Math.abs(marquee.y2 - marquee.y1);
      if (!marquee.el) {
        marquee.el = svgEl("rect", { class: "dg-marquee" });
        svg.appendChild(marquee.el);
      }
      marquee.el.setAttribute("x", x); marquee.el.setAttribute("y", y);
      marquee.el.setAttribute("width", w); marquee.el.setAttribute("height", h);
      marquee.el.setAttribute("stroke-width", 1.2 * view.w / Math.max(1, svg.getBoundingClientRect().width));
      if (!flow) return [];
      return flow.model.nodes.filter(function (n) {
        return n.x < x + w && x < n.x + n.w && n.y < y + h && y < n.y + n.h;
      }).map(function (n) { return n.id; });
    }

    svg.addEventListener("pointerdown", function (ev) {
      var pan = ev.button === 1 || (ev.button === 0 && state.spaceDown);
      if (!pan && ev.button !== 0) return;
      ev.preventDefault();
      try { svg.setPointerCapture(ev.pointerId); } catch (err) { /* no live pointer */ }
      if (pan) {
        mode = { kind: "pan", x: ev.clientX, y: ev.clientY, vx: view.x, vy: view.y };
        stage.classList.add("panning");
        return;
      }
      var p = at(ev);
      var id = nodeAt(ev);
      if (id) {
        if (ev.shiftKey) {
          if (picked[id]) delete picked[id]; else picked[id] = true;
          lightUp();
          if (!picked[id]) return;
        } else if (!picked[id]) {
          setPicked([id], false);
        }
        var ids = pickedIds(), origin = {};
        ids.forEach(function (k) { origin[k] = { x: flow.byId[k].x, y: flow.byId[k].y }; });
        mode = { kind: "move", ids: ids, origin: origin, sx: p.x, sy: p.y };
        stage.classList.add("moving");
        return;
      }
      // Shift keeps what was already picked and adds what the sweep touches.
      marquee = { x1: p.x, y1: p.y, x2: p.x, y2: p.y, base: marquee_base(ev.shiftKey), el: null, moved: false };
      mode = { kind: "sweep" };
    });

    function marquee_base(keep) {
      var out = {};
      if (keep) Object.keys(picked).forEach(function (k) { out[k] = true; });
      return out;
    }

    svg.addEventListener("pointermove", function (ev) {
      if (!mode) {
        if (!flow || pickedIds().length) return;
        var over = nodeAt(ev);
        if (over !== hoverId) { hoverId = over; lightUp(); }
        return;
      }
      if (mode.kind === "pan") {
        var box = svg.getBoundingClientRect();
        var scale = Math.min(box.width / view.w, box.height / view.h) || 1;
        view.x = mode.vx - (ev.clientX - mode.x) / scale;
        view.y = mode.vy - (ev.clientY - mode.y) / scale;
        apply();
        return;
      }
      var p = at(ev);
      if (mode.kind === "move") {
        moveNodes(mode.ids, Math.round(p.x - mode.sx), Math.round(p.y - mode.sy));
        return;
      }
      marquee.x2 = p.x; marquee.y2 = p.y; marquee.moved = true;
      var inside = drawMarquee();
      picked = {};
      Object.keys(marquee.base).forEach(function (k) { picked[k] = true; });
      inside.forEach(function (k) { picked[k] = true; });
      hoverId = null;
      lightUp();
    });

    function release(ev) {
      if (!mode) return;
      var done = mode;
      mode = null;
      stage.classList.remove("panning", "moving");
      try { svg.releasePointerCapture(ev.pointerId); } catch (err) { /* already gone */ }
      if (done.kind === "sweep") {
        if (marquee && marquee.el) marquee.el.remove();
        // A click on bare paper drops the selection, as on every canvas.
        if (marquee && !marquee.moved && !Object.keys(marquee.base).length) setPicked([], false);
        marquee = null;
      }
    }
    svg.addEventListener("pointerup", release);
    svg.addEventListener("pointercancel", release);
    svg.addEventListener("pointerleave", function () {
      if (!mode && hoverId) { hoverId = null; lightUp(); }
    });
    stage.__clear = function () { setPicked([], false); };
    stage.__hasPick = function () { return pickedIds().length > 0; };
    stage.__selectAll = function () {
      if (flow) setPicked(flow.model.nodes.map(function (n) { return n.id; }), false);
    };
    svg.addEventListener("dblclick", fit);

    var bar = document.createElement("div");
    bar.className = "dg-zoom";
    function button(text, title, run) {
      var b = document.createElement("button");
      b.className = "dg-zoom-btn";
      b.type = "button";
      b.textContent = text;
      b.title = title;
      b.addEventListener("click", run);
      bar.appendChild(b);
      return b;
    }
    button("−", "afastar", function () { zoomAt(1.25, view.x + view.w / 2, view.y + view.h / 2); });
    var out = button("100%", "ajustar à tela (duplo-clique no diagrama, ou f)", fit);
    out.classList.add("dg-zoom-read");
    button("+", "aproximar", function () { zoomAt(0.8, view.x + view.w / 2, view.y + view.h / 2); });

    var tip = document.createElement("div");
    tip.className = "dg-tip";
    tip.textContent = flow
      ? "arrastar: selecionar · arrastar nó: mover · espaço+arrastar: navegar · Esc: soltar"
      : "espaço+arrastar: navegar · roda: zoom";
    stage.appendChild(svg);
    stage.appendChild(bar);
    stage.appendChild(tip);
    stage.__fit = fit;
    stage.__zoom = zoomAt;
    stage.__view = view;
    fit();
    return stage;
  }

  /** Centred label lines inside a box. */
  function boxLabel(g, lines, cx, cy, size, cls) {
    var lead = size + 4;
    var top = cy - ((lines.length - 1) * lead) / 2;
    lines.forEach(function (line, i) {
      var t = svgEl("text", {
        class: cls || "dg-label", x: cx, y: top + i * lead + size * 0.34,
        "text-anchor": "middle", "font-size": size,
      });
      t.textContent = line;
      g.appendChild(t);
    });
  }

  // Must match SUB_PAD / SUB_TITLE in diagram.js: a frame re-wrapped after a
  // node is dragged has to land exactly where the layout would have put it.
  var DG_SUB_PAD = 24, DG_SUB_TITLE = 22;

  function drawFlow(model, stage) {
    var svg = diagramSvg(model, stage);
    var frames = svgEl("g", { class: "dg-frames" });
    var links = svgEl("g", { class: "dg-edges" });
    var boxes = svgEl("g", { class: "dg-nodes" });
    svg.appendChild(frames);
    svg.appendChild(links);
    svg.appendChild(boxes);

    // Everything the stage needs to select, move and re-draw, kept by id.
    var flow = { model: model, nodes: {}, byId: {}, edges: [], frames: [], touching: {} };
    svg.__flow = flow;

    model.frames.forEach(function (f) {
      var g = svgEl("g", { class: "dg-frame" });
      var ink = svgEl("path", { class: "dg-frame-ink" });
      g.appendChild(ink);
      var t = null;
      if (f.label) {
        t = svgEl("text", { class: "dg-frame-label", "font-size": 12 });
        t.textContent = f.label;
        g.appendChild(t);
      }
      frames.appendChild(g);
      var item = { f: f, ink: ink, label: t };
      paintFrame(item);
      flow.frames.push(item);
    });

    model.nodes.forEach(function (n) { flow.byId[n.id] = n; });

    model.edges.forEach(function (e, i) {
      if (e.invisible) return;
      var a = flow.byId[e.from], b = flow.byId[e.to];
      if (!a || !b) return;
      var g = svgEl("g", { class: "dg-edge" });
      var path = svgEl("path", {
        class: "dg-ink", fill: "none",
        "stroke-dasharray": e.dashed ? "6 4" : null,
        "stroke-width": e.thick ? 2.6 : 1.7,
      });
      g.appendChild(path);
      var item = { e: e, i: i, g: g, path: path, plate: null, text: null };
      if (e.label && a !== b) {
        item.plate = svgEl("rect", { class: "dg-edge-plate", height: 18, rx: 4 });
        item.text = svgEl("text", { class: "dg-edge-label", "text-anchor": "middle", "font-size": 11 });
        item.text.textContent = e.label;
        g.appendChild(item.plate);
        g.appendChild(item.text);
      }
      paintEdge(item, flow.byId);
      links.appendChild(g);
      flow.edges.push(item);
      (flow.touching[e.from] = flow.touching[e.from] || []).push(item);
      if (e.to !== e.from) (flow.touching[e.to] = flow.touching[e.to] || []).push(item);
    });

    model.nodes.forEach(function (n) {
      var g = svgEl("g", { class: "dg-node", "data-id": n.id, transform: "translate(" + n.x + "," + n.y + ")" });
      var fill = shapeFillEl(n.shape, n.w, n.h);
      fill.setAttribute("class", "dg-fill");
      g.appendChild(fill);
      g.appendChild(svgEl("path", {
        class: "dg-ink", d: shapeOutline(n.shape, n.w, n.h, hashCode(n.id), 1.5), fill: "none",
      }));
      boxLabel(g, n.lines, n.w / 2, n.h / 2, 13);
      boxes.appendChild(g);
      flow.nodes[n.id] = g;
    });
    return svg;
  }

  function paintEdge(item, byId) {
    var e = item.e, a = byId[e.from], b = byId[e.to];
    if (a === b) {
      // A self-loop has no facing sides; a small ear on the right reads clearly.
      var x = a.x + a.w, y = a.y + a.h / 2;
      item.path.setAttribute("d", "M" + x + "," + (y - 8) + "C" + (x + 46) + "," + (y - 34) +
        " " + (x + 46) + "," + (y + 34) + " " + x + "," + (y + 8));
      return;
    }
    var geo = sketchLink(anchorPair(a, b), hashCode(e.from + ">" + e.to + item.i), e.heads || "end");
    item.path.setAttribute("d", geo.d);
    if (item.plate) {
      var w = e.label.length * 6.2 + 12;
      item.plate.setAttribute("x", geo.mid[0] - w / 2);
      item.plate.setAttribute("y", geo.mid[1] - 9);
      item.plate.setAttribute("width", w);
      item.text.setAttribute("x", geo.mid[0]);
      item.text.setAttribute("y", geo.mid[1] + 4);
    }
  }

  function paintFrame(item) {
    var f = item.f;
    item.ink.setAttribute("d", sketchRect(f.x, f.y, f.w, f.h, hashCode("frame" + f.id), 1.4));
    if (item.label) {
      item.label.setAttribute("x", f.x + 14);
      item.label.setAttribute("y", f.y + 18);
    }
  }

  /** Re-wrap each subgraph frame around wherever its nodes are now. */
  function refitFrames(flow) {
    flow.frames.forEach(function (item) {
      var f = item.f;
      var x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
      flow.model.nodes.forEach(function (n) {
        if (n.sub !== f.id) return;
        x1 = Math.min(x1, n.x); y1 = Math.min(y1, n.y);
        x2 = Math.max(x2, n.x + n.w); y2 = Math.max(y2, n.y + n.h);
      });
      if (x1 === Infinity) return;
      f.x = x1 - DG_SUB_PAD; f.y = y1 - DG_SUB_PAD - DG_SUB_TITLE;
      f.w = x2 - x1 + DG_SUB_PAD * 2; f.h = y2 - y1 + DG_SUB_PAD * 2 + DG_SUB_TITLE;
      paintFrame(item);
    });
  }

  function drawSequence(model, stage) {
    var svg = diagramSvg(model, stage);
    var lifelines = svgEl("g", { class: "dg-lifelines" });
    var body = svgEl("g", { class: "dg-messages" });
    var heads = svgEl("g", { class: "dg-actors" });
    svg.appendChild(lifelines);
    svg.appendChild(body);
    svg.appendChild(heads);

    model.actors.forEach(function (a) {
      lifelines.appendChild(svgEl("path", {
        class: "dg-lifeline",
        d: sketchPath([[a.cx, model.lifelineTop], [a.cx, model.lifelineBottom]], hashCode("ll" + a.id), 1.1),
        fill: "none",
      }));
      var g = svgEl("g", { class: "dg-node", transform: "translate(" + a.x + "," + a.y + ")" });
      var fill = shapeFillEl("rect", a.w, a.h);
      fill.setAttribute("class", "dg-fill");
      g.appendChild(fill);
      g.appendChild(svgEl("path", {
        class: "dg-ink", d: shapeOutline("rect", a.w, a.h, hashCode(a.id), 1.4), fill: "none",
      }));
      boxLabel(g, a.lines, a.w / 2, a.h / 2, 12.5);
      heads.appendChild(g);
    });

    model.notes.forEach(function (n) {
      if (n.x == null) return;
      var g = svgEl("g", { class: "dg-note", transform: "translate(" + n.x + "," + n.y + ")" });
      var fill = shapeFillEl("rect", n.w, n.h);
      fill.setAttribute("class", "dg-note-fill");
      g.appendChild(fill);
      g.appendChild(svgEl("path", {
        class: "dg-ink", d: shapeOutline("rect", n.w, n.h, hashCode("note" + n.y), 1.3), fill: "none",
      }));
      boxLabel(g, n.lines, n.w / 2, n.h / 2, 11.5, "dg-note-label");
      body.appendChild(g);
    });

    var byId = {};
    model.actors.forEach(function (a) { byId[a.id] = a; });
    model.messages.forEach(function (m, i) {
      var a = byId[m.from], b = byId[m.to];
      if (!a || !b) return;
      var g = svgEl("g", { class: "dg-message" });
      if (a === b) {
        var x = a.cx;
        g.appendChild(svgEl("path", {
          class: "dg-ink", fill: "none", "stroke-dasharray": m.dashed ? "6 4" : null,
          d: "M" + x + "," + m.y + "C" + (x + 52) + "," + m.y +
            " " + (x + 52) + "," + (m.y + 26) + " " + (x + 6) + "," + (m.y + 26),
        }));
        var st = svgEl("text", { class: "dg-msg-label", x: x + 60, y: m.y + 16, "font-size": 11.5 });
        st.textContent = m.label;
        g.appendChild(st);
        body.appendChild(g);
        return;
      }
      g.appendChild(svgEl("path", {
        class: "dg-ink", fill: "none", "stroke-dasharray": m.dashed ? "6 4" : null,
        d: sketchArrow(a.cx, m.y, b.cx, m.y, hashCode(m.from + m.to + i), m.lost ? "none" : "end"),
      }));
      if (m.label) {
        var mid = (a.cx + b.cx) / 2;
        var t = svgEl("text", {
          class: "dg-msg-label", x: mid, y: m.y - 8, "text-anchor": "middle", "font-size": 11.5,
        });
        t.textContent = m.label;
        g.appendChild(t);
      }
      body.appendChild(g);
    });
    return svg;
  }

  /* -------------------------------------------------------------------- boot */

  function boot(graph, layout, canSave, images, docs) {
    state.graph = graph;
    // Only a bundle carries these; with a server behind it the files are fetched.
    state.images = images || {};
    state.docs = {
      prd: (docs && docs.prd) || [],
      uml: (docs && docs.uml) || [],
    };
    // A real copy, not a shallow one: `positions`, `shapes` and `links` are
    // mutated in place from here on, and the caller's object must not be them.
    var incoming = normalizeLayout(layout ? JSON.parse(JSON.stringify(layout)) : {});
    // Reading state travels in the same file but is not an edit to the map, so it
    // is lifted straight out of the layout and never enters a snapshot.
    var savedViewBlock = incoming.view;
    delete incoming.view;
    state.layout = incoming;
    state.canSave = canSave;

    state.rawIndex = {};
    graph.nodes.forEach(function (n) { state.rawIndex[n.id] = n; });
    state.rawOut = {};
    state.rawIn = {};
    graph.edges.forEach(function (e) {
      (state.rawOut[e.from] = state.rawOut[e.from] || []).push(e);
      (state.rawIn[e.to] = state.rawIn[e.to] || []).push(e);
    });

    el.canvas = document.getElementById("canvas");
    el.scene = document.getElementById("scene");
    el.panel = document.getElementById("panel");
    el.panelBody = document.getElementById("panel-body");
    el.panelClose = document.querySelector("#panel .close");
    el.search = document.getElementById("search");
    el.methodChips = document.getElementById("method-chips");
    el.toggleInternal = document.getElementById("toggle-internal");
    el.toggleHover = document.getElementById("toggle-hover");
    el.panelWide = document.querySelector("#panel .widen");
    el.expandAll = document.getElementById("expand-all");
    el.fit = document.getElementById("fit");
    el.reset = document.getElementById("reset");
    el.undo = document.getElementById("undo");
    el.redo = document.getElementById("redo");
    el.clearFocus = document.getElementById("clear-focus");
    el.orphans = document.getElementById("orphans");
    el.marked = document.getElementById("marked");
    el.save = document.getElementById("save");
    el.hand = document.getElementById("hand");
    el.toast = document.getElementById("toast");
    el.tools = document.getElementById("tools");
    el.inspector = document.getElementById("inspector");
    el.tabs = document.getElementById("tabs");
    el.doc = document.getElementById("doc");
    el.docList = document.getElementById("doc-list");
    el.toolbar = document.getElementById("toolbar");
    el.legend = document.getElementById("legend");
    el.docBack = document.getElementById("doc-back");
    el.docTitle = document.getElementById("doc-title");
    el.docFile = document.getElementById("doc-file");
    el.docBody = document.getElementById("doc-body");
    el.insColors = document.getElementById("ins-colors");
    el.insSize = document.getElementById("ins-size");
    el.insSizeVal = document.getElementById("ins-size-val");
    el.insFill = document.getElementById("ins-fill");
    el.insDash = document.getElementById("ins-dash");
    el.insDashRow = document.getElementById("ins-dash-row");
    el.insHeads = document.getElementById("ins-heads");
    el.insLabelRow = document.getElementById("ins-label-row");
    el.insLabel = document.getElementById("ins-label");
    el.insLayer = document.getElementById("ins-layer");
    el.insAnchor = document.getElementById("ins-anchor");
    el.insDelete = document.getElementById("ins-delete");
    el.insHint = document.getElementById("ins-hint");

    var counts = graph.stats || {};
    document.getElementById("meta").textContent =
      graph.project.name + " · " + (counts.route || 0) + " endpoints · " + (counts.table || 0) + " tabelas";
    document.title = "croqui — " + graph.project.name;

    // The committed view first, so `savedView` is what the file says; then this
    // machine's live one, which may win without making the file look dirty.
    var camera = adoptView(savedViewBlock);
    state.savedView = JSON.stringify(currentView());
    var local = storedView();
    if (local) camera = adoptView(local) || camera;

    if (!canSave) {
      if (el.save) el.save.hidden = true;
      el.reset.hidden = true;
    }

    // Nothing is written automatically, so leaving with pending edits must warn.
    window.addEventListener("beforeunload", function (ev) {
      if (!state.canSave || !isDirty()) return;
      ev.preventDefault();
      ev.returnValue = "";
      return "";
    });

    wireCanvas();
    wireChrome();
    wireTools();
    syncTabs();
    setTool("select");
    render();
    // The baseline for "dirty" is what came off disk — captured after the first
    // render, because that is when drawings anchored to a node resolve onto it.
    state.savedJson = snapshot();
    refreshOrphans();
    syncChrome();
    // A restored camera is a deliberate choice; re-fitting over it would undo it.
    // It still has to be pushed onto the scene, which only applyView does.
    if (camera) applyView();
    else fitToView();
    refreshChrome();
    openFromHash();

    var orphans = Object.keys(state.orphans).length;
    if (orphans) toast(orphans + " referência(s) do layout sem nó — veja em “órfãs”");
  }

  if (window.__CROQUI__) {
    boot(window.__CROQUI__.graph, window.__CROQUI__.layout, false,
         window.__CROQUI__.images, window.__CROQUI__.docs);
  } else {
    Promise.all([
      fetch("api/graph").then(function (r) { return r.json(); }),
      // A layout that will not parse is not the same as no layout. Booting with a
      // blank map and saving still switched on would let the next Ctrl+S overwrite
      // whatever is in that file, so read it as text and tell them instead.
      fetch("api/layout")
        .then(function (r) { return r.text(); })
        .then(function (body) {
          if (!body || !body.trim()) return {};
          try {
            return JSON.parse(body);
          } catch (err) {
            return { broken: err.message };
          }
        })
        .catch(function () { return {}; }),
      // Documents are a bonus, never a reason the map fails to open.
      fetch("api/docs")
        .then(function (r) { return r.json(); })
        .catch(function () { return { prd: [], uml: [] }; }),
    ]).then(function (res) {
      var broken = res[1] && res[1].broken;
      boot(res[0], broken ? {} : res[1], !broken, null, res[2]);
      if (broken) toast("layout.json ilegível — mapa somente leitura para não sobrescrevê-lo");
    }).catch(function (err) {
      document.body.innerHTML = '<pre style="padding:24px">falha ao carregar o grafo: ' + err.message + "</pre>";
    });
  }
})();
