"""``croqui design``: how to draw a diagram that argues, printed for the model drawing it.

The method is coleam00/excalidraw-diagram-skill's, adapted to croqui: its palette is
the one ``croqui draw --format excalidraw`` writes (and the map maps back on paste),
its evidence comes from the map instead of from the web, and its render-and-look
loop runs on ``croqui lint`` plus the UML tab. It lives here rather than in the
/croqui skill because the skill is read on every use and this only when a diagram
is drawn by hand — the way that skill keeps its own references in a side folder.
"""

from __future__ import annotations

GUIDE = """\
# croqui design: diagrams that argue, not display

A diagram is a visual argument: structure shows what words alone cannot. Two tests
before you keep anything: remove every word — does the structure still say it?
(isomorphism) Could a reader learn something concrete from it? (education)

## 1. Plan before drawing

- Depth. Conceptual (a mental model): abstract shapes, few words. Technical (a real
  system): real names and evidence. Anything about this codebase is technical — get
  the names from `croqui context --node "<name>"`, never placeholders.
- For each concept ask what it DOES, then give it the pattern that mirrors that:

  | the concept…              | pattern                                   |
  |---------------------------|-------------------------------------------|
  | spawns several outputs    | fan-out: arrows radiating from one shape  |
  | combines inputs           | convergence: arrows merging into one      |
  | is a hierarchy            | tree: lines + free text, no boxes         |
  | is a sequence of steps    | timeline: a line, 12px dots, labels       |
  | loops / retries           | cycle: an arrow back to the start         |
  | transforms input          | assembly line: before → process → after   |
  | compares two things       | side by side, contrast in colour          |
  | changes phase             | gap: white space between sections         |

- Different concepts get different patterns. A grid of equal cards is a list.
- Trace the eye's path (left→right for flows, top→bottom for hierarchies) first.

## 2. Three zoom levels

1. Summary: a title (28px) and the whole flow in one line under it.
2. Sections: one per responsibility or phase, named in free text (20px, in the
   section's colour). Box a section only when it truly is a container.
3. Detail: evidence inside each section — a table's real columns, a real payload,
   method names, `file.py:42`. Code and data go in a dark block (#1e293b) with
   monospaced green text (#22c55e).

## 3. Shapes, text, colour

- Shape is meaning: endpoint/trigger → ellipse in its verb's colour; step,
  service → rectangle; decision → diamond; table → rectangle + its columns block;
  external system → dashed ellipse; hierarchy/timeline → lines + free text.
- Containers only for what arrows join; under 30% of texts inside a shape. Titles,
  notes and details are free-floating text.
- Colour is meaning — stroke / fill pairs, darker stroke over lighter fill:
  GET, ok #2f9e44/#b2f2bb · POST #1971c2/#a5d8ff · PUT, PATCH, warn #e8590c/#ffd8a8
  · DELETE, error #e03131/#ffc9c9 · service, module #6741d9/#d0bfff · repository
  #0c8599/#96f2d7 · table, decision #f08c00/#ffec99 · external, muted
  #495057/#e9ecef (dashed). Text: title #1e1e1e, details #495057. Pasted on the
  map these become croqui's own colours; do not invent others.
- Hierarchy by size and room: hero ~300×150, primary 180×90, secondary 120×60. The
  most important thing gets the most white space around it.
- Every relation is an arrow — position alone relates nothing. Write its verb on it
  (grava, lê, envia, chama); reading dashed and thin, writing solid.

## 4. Excalidraw JSON

- File: {"type": "excalidraw", "version": 2, "elements": [...],
  "appState": {"viewBackgroundColor": "#ffffff", "gridSize": 20}, "files": {}}
- Every element: a descriptive unique `id` (api_post_leads, arrow_service_repo),
  type, x, y, width, height, strokeColor, backgroundColor, fillStyle "solid",
  strokeWidth 1 or 2, strokeStyle, roughness 0, opacity 100, angle 0, a unique
  integer seed, version 1, isDeleted false, groupIds [], boundElements [].
- Text: `text` = `originalText` = only the words; fontFamily 3, fontSize ≥ 13,
  lineHeight 1.25; width ≈ characters × 0.6 × fontSize, height = lines × 1.25 ×
  fontSize.
- A label in a shape: the text's `containerId` is the shape, AND the shape lists
  {"type": "text", "id": …} in boundElements.
- An arrow: `points` relative to its x, y; three or more points plus
  "roundness": {"type": 2} to curve round a block; startBinding / endBinding
  {"elementId": …, "focus": 0, "gap": 4}, AND each bound shape lists
  {"type": "arrow", "id": …} in boundElements.
- About this codebase, start from `croqui draw "<target>" --format excalidraw`: it
  already has the layers, colours, evidence and routed arrows. Change what the map
  cannot know, and delete the "croqui:draw" text of its "source" field once you do.
- A big diagram is written one section per edit, ids prefixed per section.

## 5. Check it, as many times as it takes

1. `croqui lint <file>`: fix every error and warning — repeated ids, broken
   bindings, writing that does not fit its shape, shapes or text on top of each
   other, arrows that stop short of what they point at.
2. Look at it: `croqui build .` — the UML tab draws the file (and "levar ao mapa"
   copies it onto the map). If you can render it to an image, open the image.
3. Against the plan: does the structure match the concepts? Does the eye follow
   the path you traced? Is the hero dominant? Is the evidence readable? Any empty
   corner next to a crowded one? Fix, check again — usually 2 to 4 rounds.
"""


def guide() -> str:
    return GUIDE
