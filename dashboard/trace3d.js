/* trace3d.js - the execution web of a farm cycle, in three dimensions.
 *
 * WHY THIS EXISTS
 * The pipeline tab used to be fourteen rows with a duration bar each. Bars are
 * honest and completely flat: they show that `feed` took 36s and hide that the
 * step reaches fifteen functions across four modules to make two server calls.
 * The interesting property of this loop is its *shape* - a deep, narrow spine of
 * steps with a wide fan of deterministic Python underneath and a thin shell of
 * MCP tools at the boundary - and shape is the one thing a bar chart cannot draw.
 *
 * So: the graph from farm/topology.py, laid out in 3D, grown in step order as the
 * run actually executes, with real recorded server calls travelling the edges.
 *
 * CONSTRAINTS THAT SHAPED IT
 * - No dependencies, no build step, no network. The dashboard is a single Python
 *   file serving one page on localhost; three.js is not an option and would be
 *   500KB to draw 80 nodes. This is ~900 lines of Canvas2D and arithmetic.
 * - No WebGL. Canvas2D with pre-rendered radial-gradient sprites, painter's-
 *   algorithm depth sorting and an additive composite pass gets the glow and the
 *   depth cueing for a fraction of the complexity, and it degrades on machines
 *   where WebGL context creation fails.
 * - Testable without a browser. There is no node and no headless Chrome here, so
 *   every non-drawing function (layout, projection, growth gating, routing,
 *   picking, fallback markup) is pure and exported for JavaScriptCore. Only
 *   `Renderer` touches a canvas.
 * - It must never be load-bearing. Every entry point is guarded, the animation
 *   loop stops when the tab is hidden, and `fallbackHtml()` renders the same
 *   facts as accessible markup when there is no 2D context at all.
 */
(function (root) {
  "use strict";

  var TAU = Math.PI * 2;

  // Module hue table. Colour carries the module, so a node's origin is legible
  // without reading a label - the fan under `plan` being almost entirely one hue
  // is the point being made (planning is arithmetic in rules.py, not I/O).
  var HUES = {
    cycle: 150,     // green: the orchestrator
    rules: 46,      // amber: pure arithmetic
    growth: 96,     // lime: the evidence gate
    parse: 196,     // cyan: server text -> typed state
    mcp: 288,       // violet: the transport boundary
    progress: 20,
    tokens: 260,
    heal: 12,
    journal: 320,
    report: 220,
    watch: 0,
    scheduler: 240,
    evidence: 176,
    release: 60
  };
  var DEFAULT_HUE = 210;

  /* Visual tuning in one place.
   *
   * These are the numbers that decide whether the picture reads or not, and they
   * were chosen by sweeping them headlessly (deploy/preview_trace.py measures ink
   * coverage, blob crowding, label area and edge crossings for a given set) rather
   * than by taste. Keeping them in one exported object is what makes the sweep
   * possible.
   */
  var TUNING = {
    // Bending edges toward their step hub (classic hierarchical bundling) was
    // tried and measured worse: it drags every call into the one corridor that is
    // already crowded - the spine - and raised crossings from 447 to 540. Straight
    // is both cleaner and cheaper. The knob stays because the sweep is repeatable.
    bundle: 0,
    edgeAlpha: 0.36,     // base opacity of a fully live edge
    // Long connectors are what turn a graph into a hairball: they cross everything
    // and are impossible to follow anyway. Past fadeStart pixels of screen length
    // an edge fades toward fadeFloor, so local structure stays crisp and the
    // long-distance web is still there, whispered rather than shouted.
    fadeStart: 110,
    fadeSpan: 340,
    fadeFloor: 0.12,
    secondary: 0.34,     // opacity multiplier for a call that is not a node's primary caller
    softEdge: 0.7,       // context edges under ambient focus (hover is far harsher)
    fog: 0.7,            // share of node opacity governed by depth
    stepSpread: 1.55,    // halo size per node kind, in radii
    funcSpread: 2.15,
    toolSpread: 2.5,
    softDim: 0.6,        // opacity of context when focused on the live step
    hardDim: 0.22        // opacity of context when the pointer picks a node
  };

  function hue(module) {
    return HUES[module] == null ? DEFAULT_HUE : HUES[module];
  }

  // ---------------------------------------------------------------- utilities

  function clamp(value, low, high) { return value < low ? low : (value > high ? high : value); }
  function lerp(a, b, t) { return a + (b - a) * t; }

  /* Deterministic 32-bit string hash. Node start positions are seeded from the
   * node id so a reload lays the web out identically - a graph that reshuffles
   * every refresh cannot be reasoned about or compared with a screenshot. */
  function hash32(text) {
    var h = 2166136261, i;
    text = String(text);
    for (i = 0; i < text.length; i++) {
      h ^= text.charCodeAt(i);
      h = (h * 16777619) >>> 0;
    }
    return h >>> 0;
  }

  function seeded(seed) {
    var state = (seed >>> 0) || 1;
    return function () {
      state ^= state << 13; state >>>= 0;
      state ^= state >>> 17;
      state ^= state << 5; state >>>= 0;
      return state / 4294967296;
    };
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c];
    });
  }

  // ------------------------------------------------------------------- camera

  function makeCamera(overrides) {
    var cam = {
      yaw: 0.72, pitch: -0.26, dist: 640, fov: 780,
      target: { x: 0, y: 0, z: 0 },
      spin: 0.022          // radians/second of idle drift: slow enough to read
    };
    if (overrides) { for (var k in overrides) if (overrides.hasOwnProperty(k)) cam[k] = overrides[k]; }
    return cam;
  }

  /* World -> screen. Yaw/pitch orbit around a target, then a single perspective
   * divide. Right-handed: the camera sits on +z looking at the target, so a
   * larger rotated z is *nearer* and projects larger. `scale` is handed back so
   * sprites and line widths shrink with distance, and `depth` is the sort key for
   * the painter's algorithm. */
  function project(point, cam, width, height) {
    var dx = point.x - cam.target.x;
    var dy = point.y - cam.target.y;
    var dz = point.z - cam.target.z;

    var cy = Math.cos(cam.yaw), sy = Math.sin(cam.yaw);
    var x1 = dx * cy - dz * sy;
    var z1 = dx * sy + dz * cy;

    var cp = Math.cos(cam.pitch), sp = Math.sin(cam.pitch);
    var y1 = dy * cp - z1 * sp;
    var z2 = dy * sp + z1 * cp;

    // Distance from the eye. A point past the eye plane clamps to a tiny positive
    // depth and is reported invisible: projecting it would mirror the geometry
    // through the origin, which looks like corruption rather than perspective.
    var depth = cam.dist - z2;
    var behind = depth < 1;
    if (behind) depth = 1;
    var scale = cam.fov / depth;
    return {
      x: width / 2 + x1 * scale,
      y: height / 2 - y1 * scale,
      depth: depth,
      scale: scale,
      visible: !behind && depth > 40
    };
  }

  function orbit(cam, dyaw, dpitch) {
    cam.yaw += dyaw;
    // Stop just short of the poles: at +-90 degrees the spine collapses to a
    // point and the view becomes unreadable rather than interesting.
    cam.pitch = clamp(cam.pitch + dpitch, -1.35, 1.35);
    return cam;
  }

  function zoom(cam, factor) {
    // The upper bound is generous because the fit for a wide graph in a small
    // panel legitimately needs a lot of distance; the lower bound stops the
    // camera from ending up inside the cloud.
    cam.dist = clamp(cam.dist * factor, 200, 6000);
    return cam;
  }

  // -------------------------------------------------------------- scene build

  var RADIUS = { step: 34, func: 172, tool: 344 };
  var SPINE_GAP = 44;

  /* Circular mean of a set of angles. Used to give a function a home direction
   * from the steps that reach it: a helper called by `sell` and `buy_feed` sits
   * between them rather than at some average that points nowhere. */
  function meanAngle(angles) {
    if (!angles.length) return null;
    var sx = 0, sy = 0;
    for (var i = 0; i < angles.length; i++) { sx += Math.cos(angles[i]); sy += Math.sin(angles[i]); }
    if (Math.abs(sx) < 1e-9 && Math.abs(sy) < 1e-9) return angles[0];
    return Math.atan2(sy, sx);
  }

  /* Build the simulation from a topology payload.
   *
   * The layout is deliberately half-constrained: steps are *anchored* on a helix
   * in execution order (so the vertical axis always reads as time and the shape
   * is comparable between runs), while functions and tools are free and settle
   * under a force model. A fully free layout is prettier for one frame and
   * useless for orientation; a fully fixed layout cannot show clustering.
   */
  function buildScene(topology, options) {
    options = options || {};
    var nodes = [], byId = {}, edges = [];
    var raw = (topology && topology.nodes) || [];
    var rawEdges = (topology && topology.edges) || [];
    var steps = raw.filter(function (n) { return n.kind === "step"; })
      .sort(function (a, b) { return (a.order || 0) - (b.order || 0); });
    var stepCount = Math.max(1, steps.length);

    raw.forEach(function (spec) {
      var rnd = seeded(hash32(spec.id));
      var node = {
        id: spec.id,
        kind: spec.kind,
        label: spec.label,
        module: spec.module,
        qual: spec.qual,
        line: spec.line || 0,
        loc: spec.loc || 0,
        doc: spec.doc || "",
        steps: spec.steps || [],
        depth: spec.depth || 0,
        fan: spec.fan || 0,
        order: spec.order,
        hue: hue(spec.module),
        // simulation state
        x: 0, y: 0, z: 0, vx: 0, vy: 0, vz: 0,
        fixed: false,
        // presentation state, animated toward targets by advance()
        reveal: 0, revealTarget: 0, heat: 0, status: "pending", seconds: null
      };

      if (spec.kind === "step") {
        var index = steps.indexOf(spec);
        // Two different angles, which is the point.
        //
        // fanAngle is the direction this step's subtree spreads: one full turn
        // across the pipeline, so each step owns its own sector and consecutive
        // steps are angular neighbours.
        //
        // The step's own position uses only a shallow lean of that turn. Placing
        // the spine on the full helix wrapped step 1 back around next to step 14
        // and projected the timeline as a wandering tangle - the axis everything
        // else is read against has to look like an axis.
        var fanAngle = index / stepCount * TAU;
        var lean = (index / Math.max(1, stepCount - 1) - 0.5) * 1.3;
        node.x = Math.sin(lean) * RADIUS.step;
        node.z = Math.cos(lean) * RADIUS.step * 0.5;
        node.y = (stepCount - 1) / 2 * SPINE_GAP - index * SPINE_GAP;
        node.fixed = true;                       // the spine is the frame of reference
        node.spineIndex = index;
        node.fanAngle = fanAngle;
        node.homeAngle = fanAngle;
      } else {
        // Seed near where it will end up: reduces the visible settling flail on
        // first paint from ~200 frames to ~30.
        var ring = spec.kind === "tool" ? RADIUS.tool : RADIUS.func + (spec.depth || 1) * 26;
        var a = rnd() * TAU, b = (rnd() - 0.5) * 1.1;
        node.x = Math.cos(a) * ring * Math.cos(b);
        node.z = Math.sin(a) * ring * Math.cos(b);
        node.y = (rnd() - 0.5) * SPINE_GAP * stepCount * 0.55;
        node.ring = ring;
        node.jitter = (rnd() - 0.5) * 0.5;       // keeps a wedge from collapsing to a line
      }
      nodes.push(node);
      byId[node.id] = node;
    });

    rawEdges.forEach(function (spec) {
      var source = byId[spec.source], target = byId[spec.target];
      if (!source || !target) return;
      edges.push({
        source: source, target: target, kind: spec.kind,
        // Springs get shorter as they get deeper: the fan under a step should
        // read as one cluster, not as a uniform cloud.
        rest: spec.kind === "tool" ? 150 : (spec.kind === "step" ? 120 : 92),
        // The step this edge belongs under, used to bundle it with its siblings
        // when drawing. Straight lines between 78 free-floating nodes cross each
        // other ~320 times in a 620x380 panel; bundled to their hub they read as
        // a handful of fans.
        hubId: source.kind === "step" ? source.id
          : (source.steps && source.steps.length ? "step:" + source.steps[0] : null),
        flow: 0
      });
      source.out = (source.out || 0) + 1;
      target.in = (target.in || 0) + 1;
    });

    // Adjacency, used by hover highlighting and packet routing.
    var adjacency = {};
    edges.forEach(function (edge) {
      (adjacency[edge.source.id] = adjacency[edge.source.id] || []).push(edge);
    });

    /* Spanning-tree emphasis.
     *
     * Every function gets one *primary* edge - the call from its shallowest
     * caller, a step where possible - drawn at full strength, and its remaining
     * callers drawn faintly. Trees read as structure and graphs read as tangle,
     * and this is a graph: with all 101 edges at equal weight the eye has no
     * route through the picture. The extra callers are still visible, and hovering
     * a node brings its full set back to full strength - so nothing is hidden,
     * but there is a shape to follow.
     */
    var best = {};
    edges.forEach(function (edge) {
      var target = edge.target;
      if (target.kind === "step") return;
      var score = edge.source.kind === "step" ? -1 : (edge.source.depth || 9);
      if (!best[target.id] || score < best[target.id].score) {
        best[target.id] = { edge: edge, score: score };
      }
    });
    Object.keys(best).forEach(function (id) { best[id].edge.primary = true; });

    // Home direction per node: the wedge of the run it belongs to. Without this
    // the force model produces a pretty, uniform, unreadable ball; with it each
    // step's fan-out occupies its own sector at its own height, which is what
    // makes "this is what `feed` does" a thing you can point at.
    var stepAngle = {};
    nodes.forEach(function (node) {
      if (node.kind === "step") stepAngle[node.label] = node.fanAngle;
    });
    nodes.forEach(function (node) {
      if (node.kind === "step") return;
      var angles = (node.steps || []).map(function (name) { return stepAngle[name]; })
        .filter(function (a) { return a != null; });
      var home = meanAngle(angles);
      node.homeAngle = home == null ? null : home + (node.jitter || 0);
    });

    return {
      nodes: nodes, edges: edges, byId: byId, adjacency: adjacency,
      steps: nodes.filter(function (n) { return n.kind === "step"; })
        .sort(function (a, b) { return a.spineIndex - b.spineIndex; }),
      alpha: 1, packets: [], rings: [],
      stats: (topology && topology.stats) || {},
      modules: (topology && topology.modules) || [],
      fingerprint: topology && topology.fingerprint
    };
  }

  // ------------------------------------------------------------- force layout

  /* One integration step of the layout.
   *
   * O(n^2) repulsion is fine and stays fine: the graph is bounded at 600 nodes by
   * farm/topology.py, and at the real size (~80) a full pass is about 3k pair
   * comparisons - tens of microseconds. Barnes-Hut would be more code than the
   * whole renderer for no measurable gain.
   */
  function relax(scene, alpha) {
    var nodes = scene.nodes, edges = scene.edges;
    var i, j, a, b, dx, dy, dz, dist, force;
    alpha = alpha == null ? scene.alpha : alpha;

    for (i = 0; i < nodes.length; i++) {
      a = nodes[i];
      for (j = i + 1; j < nodes.length; j++) {
        b = nodes[j];
        dx = b.x - a.x; dy = b.y - a.y; dz = b.z - a.z;
        dist = Math.sqrt(dx * dx + dy * dy + dz * dz) || 0.01;
        if (dist > 460) continue;                         // far pairs contribute noise
        force = 26000 / (dist * dist);
        if (force > 9) force = 9;                         // no explosive kicks on overlap
        dx /= dist; dy /= dist; dz /= dist;
        if (!a.fixed) { a.vx -= dx * force; a.vy -= dy * force; a.vz -= dz * force; }
        if (!b.fixed) { b.vx += dx * force; b.vy += dy * force; b.vz += dz * force; }
      }
    }

    for (i = 0; i < edges.length; i++) {
      var edge = edges[i];
      a = edge.source; b = edge.target;
      dx = b.x - a.x; dy = b.y - a.y; dz = b.z - a.z;
      dist = Math.sqrt(dx * dx + dy * dy + dz * dz) || 0.01;
      force = (dist - edge.rest) * 0.012;
      dx = dx / dist * force; dy = dy / dist * force; dz = dz / dist * force;
      if (!a.fixed) { a.vx += dx; a.vy += dy; a.vz += dz; }
      if (!b.fixed) { b.vx -= dx; b.vy -= dy; b.vz -= dz; }
    }

    for (i = 0; i < nodes.length; i++) {
      a = nodes[i];
      if (a.fixed) { a.vx = a.vy = a.vz = 0; continue; }
      // Shell constraint: tools belong on the outside, functions in the middle.
      // Without it the force model happily buries a server call in the core,
      // which destroys the one structural claim the picture makes.
      var planar = Math.sqrt(a.x * a.x + a.z * a.z) || 0.01;
      var want = a.ring || RADIUS.func;
      var pull = (want - planar) * 0.02;
      a.vx += a.x / planar * pull;
      a.vz += a.z / planar * pull;
      // Angular pull toward the node's home wedge. Applied tangentially so it
      // rotates a node around the spine without fighting the radial constraint.
      if (a.homeAngle != null) {
        var theta = Math.atan2(a.z, a.x);
        var delta = a.homeAngle - theta;
        while (delta > Math.PI) delta -= TAU;
        while (delta < -Math.PI) delta += TAU;
        var force2 = delta * planar * 0.02;
        a.vx += -Math.sin(theta) * force2;
        a.vz += Math.cos(theta) * force2;
      }
      // Vertical pull toward the mean height of the steps that reach it, so a
      // node sits beside the part of the run that uses it.
      if (a.steps && a.steps.length) {
        var sum = 0, seen = 0;
        for (j = 0; j < a.steps.length; j++) {
          var step = scene.byId["step:" + a.steps[j]];
          if (step) { sum += step.y; seen++; }
        }
        if (seen) a.vy += (sum / seen - a.y) * 0.05;
      }
      a.vx *= 0.84; a.vy *= 0.84; a.vz *= 0.84;
      a.x += a.vx * alpha; a.y += a.vy * alpha; a.z += a.vz * alpha;
    }

    scene.alpha = Math.max(0.08, alpha * 0.988);
    return scene;
  }

  function settle(scene, iterations) {
    var alpha = 1;
    for (var i = 0; i < (iterations || 120); i++) {
      relax(scene, alpha);
      alpha = Math.max(0.12, alpha * 0.97);
    }
    scene.alpha = 0.12;
    return scene;
  }

  /* Radius of the settled cloud, used to frame the camera on first paint. */
  function extent(scene) {
    var max = 1;
    scene.nodes.forEach(function (n) {
      var d = Math.sqrt(n.x * n.x + n.y * n.y + n.z * n.z);
      if (d > max) max = d;
    });
    return max;
  }

  /* Point the camera at the content and pull back just far enough to hold all of
   * it. Solved exactly rather than guessed from a radius: with a fixed multiple of
   * the cloud radius, a graph whose fan-out is lopsided (which this one is - the
   * `plan` wedge is all of rules.py) sits off to one side with dead space beside
   * it, and every window size needs a different fudge factor.
   *
   *   screen offset = coord * fov / (dist - z)  <=  margin * viewport
   *   =>  dist >= z + coord * fov / (margin * viewport)
   */
  /* Move the camera target so the graph's projected bounding box lands where we
   * want it on screen.
   *
   * Framing on the 3D centroid is not the same thing as filling the frame: a
   * lopsided fan (this graph's `plan` wedge is all of rules.py) puts the centroid
   * well away from the middle of what you actually see, which left a third of the
   * panel empty. The two probe vectors give the exact target shift for a wanted
   * screen shift, so the sign conventions of the projection never have to be
   * rederived here.
   */
  function recenter(scene, cam, width, height, live, wantX, wantY) {
    var probes = [
      { x: Math.cos(cam.yaw), y: 0, z: -Math.sin(cam.yaw) },   // camera right-ish
      { x: 0, y: 1, z: 0 }                                     // world up
    ];
    for (var pass = 0; pass < 3; pass++) {
      var box = screenBox(live, cam, width, height);
      if (!box) return cam;
      var dx = wantX - (box.x0 + box.x1) / 2;
      var dy = wantY - (box.y0 + box.y1) / 2;
      if (Math.abs(dx) < 1.5 && Math.abs(dy) < 1.5) return cam;
      var base = { x: (box.x0 + box.x1) / 2, y: (box.y0 + box.y1) / 2 };
      var eps = Math.max(4, cam.dist * 0.01), cols = [];
      for (var i = 0; i < 2; i++) {
        var saved = cam.target;
        cam.target = {
          x: saved.x + probes[i].x * eps,
          y: saved.y + probes[i].y * eps,
          z: saved.z + probes[i].z * eps
        };
        var moved = screenBox(live, cam, width, height);
        cam.target = saved;
        if (!moved) return cam;
        cols.push({
          x: ((moved.x0 + moved.x1) / 2 - base.x) / eps,
          y: ((moved.y0 + moved.y1) / 2 - base.y) / eps
        });
      }
      // Solve [cols] * t = (dx, dy) for the two probe amounts.
      var det = cols[0].x * cols[1].y - cols[1].x * cols[0].y;
      if (Math.abs(det) < 1e-9) return cam;
      var t0 = (dx * cols[1].y - cols[1].x * dy) / det;
      var t1 = (cols[0].x * dy - dx * cols[0].y) / det;
      cam.target = {
        x: cam.target.x + probes[0].x * t0 + probes[1].x * t1,
        y: cam.target.y + probes[0].y * t0 + probes[1].y * t1,
        z: cam.target.z + probes[0].z * t0 + probes[1].z * t1
      };
    }
    return cam;
  }

  function screenBox(nodes, cam, width, height) {
    var x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity, seen = 0;
    for (var i = 0; i < nodes.length; i++) {
      var p = project(nodes[i], cam, width, height);
      if (!p.visible) continue;
      seen++;
      if (p.x < x0) x0 = p.x;
      if (p.x > x1) x1 = p.x;
      if (p.y < y0) y0 = p.y;
      if (p.y > y1) y1 = p.y;
    }
    return seen ? { x0: x0, y0: y0, x1: x1, y1: y1 } : null;
  }

  function frame(scene, cam, width, height, margin) {
    if (!scene.nodes.length || !width || !height) return cam;
    margin = margin || 0.47;
    // Frame what is *lit*. Including the parts of the web this run never reached
    // pulled the camera back far enough to leave the live half of the graph in a
    // small clump in the middle of a large empty panel.
    var live = scene.nodes.filter(function (n) {
      return n.revealTarget == null || n.revealTarget > 0.05;
    });
    if (live.length < 4) live = scene.nodes;
    var cx = 0, cy = 0, cz = 0, count = live.length;
    live.forEach(function (n) { cx += n.x; cy += n.y; cz += n.z; });
    if (!count) return cam;
    cam.target = { x: cx / count, y: cy / count, z: cz / count };

    function fitDistance() {
      var cyaw = Math.cos(cam.yaw), syaw = Math.sin(cam.yaw);
      var cpit = Math.cos(cam.pitch), spit = Math.sin(cam.pitch);
      var limitX = margin * width, limitY = margin * height;
      var needs = [];
      live.forEach(function (n) {
        var dx = n.x - cam.target.x, dy = n.y - cam.target.y, dz = n.z - cam.target.z;
        var x1 = dx * cyaw - dz * syaw;
        var z1 = dx * syaw + dz * cyaw;
        var y1 = dy * cpit - z1 * spit;
        var z2 = dy * spit + z1 * cpit;
        needs.push(Math.max(z2 + Math.abs(x1) * cam.fov / limitX,
                            z2 + Math.abs(y1) * cam.fov / limitY));
      });
      needs.sort(function (a, b) { return a - b; });
      // 94th percentile, not the maximum, once there is a crowd. One helper parked
      // outside its wedge is worth clipping at the edge of the frame; letting it
      // halve the scale of everything else is how a legible graph turns into a
      // distant smudge. Below ~24 nodes every node is a meaningful share of the
      // picture, so nothing is sacrificed.
      var index = live.length >= 24 ? Math.floor(needs.length * 0.94) : needs.length - 1;
      cam.dist = clamp(needs[Math.min(needs.length - 1, Math.max(0, index))], 240, 6000);
    }

    // Distance and centring interact: moving the target changes which node is the
    // binding one. Two rounds converge to within a pixel or two, and the final fit
    // is what guarantees the content is inside the frame it was centred in.
    var bias = Math.min(34, width * 0.055);
    fitDistance();
    recenter(scene, cam, width, height, live, width * 0.5 + bias, height * 0.5);
    fitDistance();
    recenter(scene, cam, width, height, live, width * 0.5 + bias, height * 0.5);
    fitDistance();
    return cam;
  }
  // ---------------------------------------------------------- live run growth

  /* Map the live pipeline onto the web.
   *
   * This is the whole idea: a node is only lit once the run has actually reached
   * the step that reaches it. Watching a cycle, the web assembles itself from the
   * spine outward in execution order; between runs it stands complete; when a
   * step is skipped its subtree stays a ghost, which makes "we did not harvest"
   * a visible absence instead of a word in a table.
   */
  function applyRun(scene, pipeline, options) {
    options = options || {};
    var steps = (pipeline && pipeline.steps) || [];
    var status = {}, seconds = {}, baseline = (pipeline && pipeline.baseline) || {};
    var running = (pipeline && pipeline.status) === "running";
    steps.forEach(function (step) {
      status[step.name] = step.status;
      seconds[step.name] = step.seconds;
    });

    // With no run data at all, show the complete web rather than a black panel:
    // the topology is true whether or not the loop is mid-cycle.
    var known = Object.keys(status).length > 0;

    scene.nodes.forEach(function (node) {
      var state = "pending", reveal = 0.1;
      if (node.kind === "step") {
        state = known ? (status[node.label] || "pending") : "done";
        node.seconds = seconds[node.label];
        node.baseline = baseline[node.label];
      } else {
        // A function inherits the furthest-along state of the steps that reach it.
        var best = null;
        (node.steps || []).forEach(function (name) {
          var s = known ? status[name] : "done";
          if (s === "active") best = "active";
          else if (s === "done" && best !== "active") best = "done";
          else if (s === "failed") best = best === "active" ? best : "failed";
          else if (s === "skipped" && !best) best = "skipped";
        });
        state = best || "pending";
      }
      if (state === "done") reveal = 1;
      else if (state === "active") reveal = 1;
      else if (state === "failed") reveal = 1;
      else if (state === "skipped") reveal = 0.22;
      else reveal = running ? 0.12 : 0.34;   // not reached yet vs. simply idle

      if (node.status !== state) {
        // A step completing is the moment worth seeing; mark it for a ring.
        if (node.kind === "step" && state === "done" && node.status === "active") {
          scene.rings.push({ node: node, t: 0 });
        }
        node.status = state;
      }
      node.revealTarget = reveal;
      node.heatTarget = state === "active" ? 1 : (state === "failed" ? 0.8 : 0);
    });
    scene.runStatus = (pipeline && pipeline.status) || "idle";
    scene.activeStep = (pipeline && pipeline.active) || null;
    return scene;
  }

  /* Ease presentation state toward its target. Kept separate from applyRun so a
   * 2s poll sets targets and a 60fps loop animates them. */
  function advance(scene, dt) {
    var k = 1 - Math.exp(-dt * 3.4);
    scene.nodes.forEach(function (node) {
      node.reveal = lerp(node.reveal, node.revealTarget, k);
      node.heat = lerp(node.heat, node.heatTarget || 0, k);
    });
    scene.edges.forEach(function (edge) {
      var target = Math.min(edge.source.reveal, edge.target.reveal);
      edge.flow = lerp(edge.flow, target, k);
    });
    scene.rings = scene.rings.filter(function (ring) {
      ring.t += dt * 1.6;
      return ring.t < 1;
    });
    return scene;
  }

  // -------------------------------------------------------------- packets

  /* Shortest path from a step to a tool, so a recorded server call travels the
   * route the code actually takes rather than a straight line through the middle
   * of the picture. */
  function route(scene, fromId, toId) {
    if (!scene.byId[fromId] || !scene.byId[toId]) return null;
    var queue = [fromId], prev = {}, seen = {};
    seen[fromId] = true;
    while (queue.length) {
      var current = queue.shift();
      if (current === toId) {
        var path = [current];
        while (prev[current]) { current = prev[current]; path.unshift(current); }
        return path;
      }
      var out = scene.adjacency[current] || [];
      for (var i = 0; i < out.length; i++) {
        var next = out[i].target.id;
        if (seen[next]) continue;
        seen[next] = true; prev[next] = current;
        queue.push(next);
      }
    }
    return null;
  }

  /* Turn recorded intents into packets. `activity` rows are real lines from
   * state/intents.ndjson, already matched to tool node ids by monitor.py, so
   * every dot on the screen is a call the loop actually made. */
  function spawnPackets(scene, activity, options) {
    options = options || {};
    // A cap of 40 in flight was a swarm. Three at a time reads as traffic; a
    // swarm reads as static, and the packets are the only thing in the picture
    // that is genuinely moving.
    var limit = options.limit || 3;
    var random = options.random || Math.random;
    var seen = scene.seenActivity || (scene.seenActivity = {});
    (activity || []).forEach(function (row) {
      var key = row.key || (row.ts + "|" + row.tool);
      if (seen[key]) return;
      seen[key] = true;
      if (scene.packets.length >= limit) return;
      var target = "tool:" + row.tool;
      var from = row.step ? "step:" + row.step : null;
      var path = (from && route(scene, from, target)) || route(scene, "step:collect", target);
      if (!path) {
        if (!scene.byId[target]) return;
        path = [target];
      }
      scene.packets.push({
        path: path.map(function (id) { return scene.byId[id]; }),
        t: 0,
        speed: 0.5 + random() * 0.25,
        label: row.label || row.tool,
        trail: []
      });
    });
    return scene;
  }

  function movePackets(scene, dt) {
    scene.packets = scene.packets.filter(function (packet) {
      packet.t += dt * packet.speed;
      var legs = Math.max(1, packet.path.length - 1);
      if (packet.t >= legs) return false;
      var index = Math.floor(packet.t);
      var frac = packet.t - index;
      var a = packet.path[Math.min(index, packet.path.length - 1)];
      var b = packet.path[Math.min(index + 1, packet.path.length - 1)];
      // Ease within each leg so a packet visibly "lands" on each function it
      // passes through instead of gliding at constant speed.
      var e = frac < 0.5 ? 2 * frac * frac : 1 - Math.pow(-2 * frac + 2, 2) / 2;
      packet.x = lerp(a.x, b.x, e);
      packet.y = lerp(a.y, b.y, e);
      packet.z = lerp(a.z, b.z, e);
      packet.trail.unshift({ x: packet.x, y: packet.y, z: packet.z });
      if (packet.trail.length > 7) packet.trail.pop();
      return true;
    });
    return scene;
  }

  // -------------------------------------------------------------- picking

  /* Nearest node to a screen point, in screen space, nearest-to-camera first.
   * Pure so it can be tested without a pointer event. */
  function pick(scene, cam, width, height, sx, sy, tolerance) {
    tolerance = tolerance || 16;
    var best = null, bestScore = Infinity;
    scene.nodes.forEach(function (node) {
      if (node.reveal < 0.08) return;
      var p = project(node, cam, width, height);
      if (!p.visible) return;
      var dx = p.x - sx, dy = p.y - sy;
      var distance = Math.sqrt(dx * dx + dy * dy);
      var radius = Math.max(tolerance, nodeRadius(node) * p.scale * 1.6);
      if (distance > radius) return;
      var score = distance + p.depth * 0.01;   // ties go to the closer node
      if (score < bestScore) { bestScore = score; best = node; }
    });
    return best;
  }

  function nodeRadius(node) {
    if (node.kind === "step") return 20;      // the spine has to dominate
    if (node.kind === "tool") return 10;
    // Size carries lines of code: a 90-line function is a bigger thing to reason
    // about than a 4-line helper, and that is worth seeing. The range is narrow
    // on purpose - functions are the crowd, and a crowd of varied blobs is
    // noisier than a crowd of similar ones.
    return clamp(3.4 + Math.sqrt(node.loc || 4) * 0.78, 3.8, 9);
  }

  function neighbourhood(scene, node) {
    var set = {};
    if (!node) return set;
    set[node.id] = true;
    scene.edges.forEach(function (edge) {
      if (edge.source.id === node.id) set[edge.target.id] = true;
      if (edge.target.id === node.id) set[edge.source.id] = true;
    });
    return set;
  }

  // -------------------------------------------------------------- renderer

  function sprite(document, size, h, saturation, light) {
    var canvas = document.createElement("canvas");
    canvas.width = canvas.height = size;
    var ctx = canvas.getContext("2d");
    var gradient = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    gradient.addColorStop(0, "hsla(" + h + "," + saturation + "%," + Math.min(98, light + 26) + "%,1)");
    gradient.addColorStop(0.28, "hsla(" + h + "," + saturation + "%," + light + "%,.92)");
    gradient.addColorStop(0.62, "hsla(" + h + "," + saturation + "%," + Math.max(12, light - 22) + "%,.28)");
    gradient.addColorStop(1, "hsla(" + h + "," + saturation + "%," + Math.max(8, light - 30) + "%,0)");
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, size, size);
    return canvas;
  }

  /* The only part of this file that needs a DOM. Sprites are pre-rendered once
   * per (hue, brightness) pair: drawing 80 radial gradients per frame costs more
   * than the rest of the frame put together, drawing 80 cached images costs
   * nothing, and the visual result is identical. */
  function Renderer(canvas, options) {
    options = options || {};
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.document = options.document || (canvas.ownerDocument || null);
    this.dpr = options.dpr || 1;
    this.sprites = {};
    this.width = 0; this.height = 0;
    this.quality = 1;         // dropped automatically if frames get expensive
    this.frameCost = 16;
  }

  Renderer.prototype.spriteFor = function (h, bright) {
    var key = h + "|" + bright;
    if (!this.sprites[key] && this.document) {
      this.sprites[key] = sprite(this.document, 64, h, bright ? 92 : 66, bright ? 66 : 48);
    }
    return this.sprites[key];
  };

  Renderer.prototype.resize = function (width, height) {
    this.width = Math.max(1, Math.floor(width));
    this.height = Math.max(1, Math.floor(height));
    this.canvas.width = Math.floor(this.width * this.dpr);
    this.canvas.height = Math.floor(this.height * this.dpr);
    if (this.canvas.style) {
      this.canvas.style.width = this.width + "px";
      this.canvas.style.height = this.height + "px";
    }
  };

  Renderer.prototype.draw = function (scene, cam, state) {
    state = state || {};
    var ctx = this.ctx, w = this.width, h = this.height, started = Date.now();
    if (!ctx || !w || !h) return;
    var highlight = state.highlight || null;
    var near = highlight ? neighbourhood(scene, highlight) : null;

    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    // Backdrop: a vignette plus a faint horizon ellipse. It reads as depth and
    // costs one gradient fill.
    var sky = ctx.createRadialGradient(w * 0.5, h * 0.42, 20, w * 0.5, h * 0.5, Math.max(w, h) * 0.78);
    sky.addColorStop(0, "#13251d");
    sky.addColorStop(1, "#080d0b");
    ctx.fillStyle = sky;
    ctx.fillRect(0, 0, w, h);

    this.drawSpine(scene, cam);
    this.drawEdges(scene, cam, near, state);
    this.drawRings(scene, cam);
    this.drawNodes(scene, cam, near, highlight, state);
    this.drawPackets(scene, cam);
    this.drawLabels(scene, cam, near, highlight, state);

    // Adaptive quality: a slow machine or a very large graph loses the additive
    // bloom pass before it loses its frame rate.
    var cost = Date.now() - started;
    this.frameCost = this.frameCost * 0.9 + cost * 0.1;
    this.quality = this.frameCost > 26 ? 0 : 1;
  };

  /* The step spine, drawn as a ribbon so execution order is readable from any
   * camera angle even when the fan is dense. */
  Renderer.prototype.drawSpine = function (scene, cam) {
    var ctx = this.ctx, w = this.width, h = this.height, first = true;
    if (scene.steps.length < 2) return;
    ctx.save();
    ctx.strokeStyle = "rgba(140,220,180,.20)";
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    scene.steps.forEach(function (step) {
      var p = project(step, cam, w, h);
      if (!p.visible) return;
      if (first) { ctx.moveTo(p.x, p.y); first = false; } else ctx.lineTo(p.x, p.y);
    });
    ctx.stroke();
    ctx.restore();
  };

  Renderer.prototype.drawEdges = function (scene, cam, near, state) {
    var ctx = this.ctx, w = this.width, h = this.height;
    var hubs = {};                            // one projection per hub per frame
    function hubPoint(id) {
      if (hubs[id] === undefined) {
        var node = scene.byId[id];
        hubs[id] = node ? project(node, cam, w, h) : null;
      }
      return hubs[id];
    }
    ctx.save();
    ctx.lineCap = "round";
    scene.edges.forEach(function (edge) {
      if (edge.flow < 0.04) return;
      var a = project(edge.source, cam, w, h);
      var b = project(edge.target, cam, w, h);
      if (!a.visible || !b.visible) return;
      var dimmed = near && !(near[edge.source.id] && near[edge.target.id]);
      var alpha = edge.flow * (dimmed ? (state && state.soft ? TUNING.edgeAlpha * TUNING.softEdge : 0.05) : TUNING.edgeAlpha)
        * clamp(a.scale * 0.9, 0.25, 1.1);
      var span = Math.sqrt((b.x - a.x) * (b.x - a.x) + (b.y - a.y) * (b.y - a.y));
      if (span > TUNING.fadeStart) {
        alpha *= clamp(1 - (span - TUNING.fadeStart) / TUNING.fadeSpan, TUNING.fadeFloor, 1);
      }
      if (!edge.primary && !(near && near[edge.source.id] && near[edge.target.id])) {
        alpha *= TUNING.secondary;
      }
      if (alpha < 0.012) return;
      var h1 = edge.kind === "tool" ? 288 : edge.source.hue;
      var active = edge.source.heat > 0.2 || edge.target.heat > 0.2;
      ctx.strokeStyle = "hsla(" + h1 + "," + (active ? 90 : 55) + "%," + (active ? 68 : 56) + "%," + alpha.toFixed(3) + ")";
      ctx.lineWidth = Math.max(0.5, (edge.kind === "step" ? 1.5 : 1) * clamp(a.scale, 0.4, 1.6));
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      var mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      var cx, cy;
      var hub = edge.hubId ? hubPoint(edge.hubId) : null;
      if (hub && hub.visible) {
        // Bundle toward the step the call belongs to: siblings share a corridor,
        // so a fan reads as a fan instead of as N independent lines through the
        // middle of everything else.
        cx = mx + (hub.x - mx) * TUNING.bundle;
        cy = my + (hub.y - my) * TUNING.bundle;
      } else {
        // No hub (a tool reached from several steps): bow away from the centre so
        // the line does not disappear into the spine's glow.
        cx = mx + (mx - w / 2) * 0.12;
        cy = my + (my - h / 2) * 0.12;
      }
      ctx.quadraticCurveTo(cx, cy, b.x, b.y);
      ctx.stroke();
    });
    ctx.restore();
  };

  Renderer.prototype.drawRings = function (scene, cam) {
    var ctx = this.ctx, w = this.width, h = this.height;
    if (!scene.rings.length) return;
    ctx.save();
    ctx.globalCompositeOperation = "lighter";
    scene.rings.forEach(function (ring) {
      var p = project(ring.node, cam, w, h);
      if (!p.visible) return;
      var radius = 8 + ring.t * 70 * p.scale;
      ctx.strokeStyle = "hsla(150,90%,70%," + ((1 - ring.t) * 0.5).toFixed(3) + ")";
      ctx.lineWidth = 2 * (1 - ring.t) + 0.3;
      ctx.beginPath();
      ctx.arc(p.x, p.y, radius, 0, TAU);
      ctx.stroke();
    });
    ctx.restore();
  };

  Renderer.prototype.drawNodes = function (scene, cam, near, highlight, state) {
    var ctx = this.ctx, w = this.width, h = this.height, self = this;
    // `now` is injectable so a headless regression frame is byte-for-byte
    // reproducible; the live panel omits it and gets ordinary wall-clock pulse.
    var now = state.now == null ? Date.now() : state.now;
    var order = scene.nodes.map(function (node) {
      return { node: node, p: project(node, cam, w, h) };
    }).filter(function (item) {
      return item.p.visible && item.node.reveal > 0.03;
    }).sort(function (a, b) { return b.p.depth - a.p.depth; });   // far to near

    // Aerial perspective. The camera sits far enough back that perspective scale
    // varies by only a few percent across the whole graph, so distance carried no
    // visual signal at all and 78 nodes read as one flat sheet of dots. Fading
    // with depth is what turns the sheet back into a volume.
    var dFar = order.length ? order[0].p.depth : 0;
    var dNear = order.length ? order[order.length - 1].p.depth : 0;
    var dSpan = (dFar - dNear) || 1;

    ctx.save();
    ctx.globalCompositeOperation = "lighter";
    order.forEach(function (item) {
      var node = item.node, p = item.p;
      var fog = clamp((dFar - p.depth) / dSpan, 0, 1);
      var dimmed = near && !near[node.id];
      var reveal = node.reveal * (dimmed ? (state.soft ? TUNING.softDim : TUNING.hardDim) : 1);
      var radius = nodeRadius(node) * p.scale;
      var pulse = node.heat > 0.02
        ? 1 + 0.18 * Math.sin(now / 190 + (node.spineIndex || 0))
        : 1;
      var tint = node.status === "failed" ? 2
        : (node.status === "skipped" ? node.hue : node.hue);
      var img = self.spriteFor(Math.round(tint), node.heat > 0.3 || node.kind === "step");
      // Halo scale by kind. A uniform multiple made every function's glow overlap
      // its neighbours' into a haze, which is most of what "cluttered" meant here.
      var spread = node.status === "skipped" ? 1.7
        : (node.kind === "step" ? TUNING.stepSpread
          : (node.kind === "tool" ? TUNING.toolSpread : TUNING.funcSpread));
      var size = radius * pulse * spread;
      var alpha = clamp(reveal * (1 - TUNING.fog + TUNING.fog * fog)
        * (0.5 + 0.5 * clamp(p.scale, 0.2, 1.4)), 0, 1);
      if (!img) {                             // no offscreen canvas: still draw something
        ctx.globalAlpha = alpha;
        ctx.fillStyle = "hsl(" + tint + ",70%,60%)";
        ctx.beginPath(); ctx.arc(p.x, p.y, radius, 0, TAU); ctx.fill();
        return;
      }
      if (self.quality && node.heat > 0.1) {  // bloom for the live step only
        ctx.globalAlpha = alpha * 0.5 * node.heat;
        ctx.drawImage(img, p.x - size * 1.9, p.y - size * 1.9, size * 3.8, size * 3.8);
      }
      ctx.globalAlpha = alpha;
      ctx.drawImage(img, p.x - size, p.y - size, size * 2, size * 2);
      // Core dot: the sprite alone is a soft blob, and a crisp centre is what
      // makes it read as an object at small scale.
      ctx.globalAlpha = clamp(alpha * 1.1, 0, 1);
      ctx.fillStyle = node.status === "skipped"
        ? "hsla(" + tint + ",30%,62%,.5)"
        : "hsla(" + tint + ",95%," + (node.heat > 0.3 ? 88 : 74) + "%,.95)";
      ctx.beginPath();
      ctx.arc(p.x, p.y, Math.max(0.7, radius * 0.42), 0, TAU);
      ctx.fill();
      if (highlight && highlight.id === node.id && !state.soft) {
        // Pointer affordance only. Under ambient focus the live step already
        // pulses and rings; a selection ring there reads as "you clicked this",
        // which is a lie.
        ctx.globalAlpha = 1;
        ctx.strokeStyle = "rgba(237,247,239,.9)";
        ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.arc(p.x, p.y, radius * 1.9 + 3, 0, TAU); ctx.stroke();
      }
      if (state.selected && state.selected === node.id) {
        ctx.globalAlpha = 1;
        ctx.strokeStyle = "hsla(" + tint + ",95%,75%,.95)";
        ctx.lineWidth = 1.6;
        ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.arc(p.x, p.y, radius * 2.3 + 5, 0, TAU); ctx.stroke();
        ctx.setLineDash([]);
      }
    });
    ctx.restore();
    this.projected = order;
  };

  Renderer.prototype.drawPackets = function (scene, cam) {
    var ctx = this.ctx, w = this.width, h = this.height;
    if (!scene.packets.length) return;
    ctx.save();
    ctx.globalCompositeOperation = "lighter";
    scene.packets.forEach(function (packet) {
      var p = project(packet, cam, w, h);
      if (!p.visible) return;
      packet.trail.forEach(function (point, index) {
        var q = project(point, cam, w, h);
        if (!q.visible) return;
        var fade = (1 - index / packet.trail.length) * 0.5;
        ctx.fillStyle = "hsla(46,100%,78%," + fade.toFixed(3) + ")";
        ctx.beginPath();
        ctx.arc(q.x, q.y, Math.max(0.5, 2.6 * p.scale * (1 - index / packet.trail.length)), 0, TAU);
        ctx.fill();
      });
      ctx.fillStyle = "rgba(255,246,214,.95)";
      ctx.beginPath();
      ctx.arc(p.x, p.y, Math.max(1.2, 3.4 * p.scale), 0, TAU);
      ctx.fill();
    });
    ctx.restore();
  };

  Renderer.prototype.drawLabels = function (scene, cam, near, highlight, state) {
    var ctx = this.ctx, w = this.width, h = this.height;
    ctx.save();
    ctx.font = "600 11px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif";
    ctx.textBaseline = "middle";

    /* Step names go in a gutter down the left edge rather than beside their node.
     *
     * Fourteen label plates floating next to a spine that projects through the
     * middle of the frame covered the exact structure they were annotating - in
     * the headless captures the labels were the single largest visual mass in the
     * picture. Read as an axis instead, they cost the same pixels, never move
     * over the graph, and give the vertical direction an explicit meaning.
     */
    var GUTTER = 9;
    var axis = [];
    scene.steps.forEach(function (step) {
      if (step.reveal < 0.1) return;
      var p = project(step, cam, w, h);
      if (p.visible) axis.push({ node: step, p: p });
    });
    axis.sort(function (a, b) { return a.p.y - b.p.y; });
    var lastY = -99;
    axis.forEach(function (row) {
      var node = row.node, p = row.p;
      var y = clamp(p.y, 11, h - 9);
      if (y - lastY < 13) return;         // no vertical room: skip the name, keep the bead
      lastY = y;
      var active = node.status === "active";
      var text = node.label;
      if (node.seconds != null) text += "  " + Number(node.seconds).toFixed(1) + "s";
      var width = ctx.measureText(text).width;
      if (active) {
        ctx.globalAlpha = 1;
        ctx.fillStyle = "rgba(6,10,8,.62)";
        ctx.fillRect(GUTTER - 4, y - 7.5, width + 8, 15);
      }
      // A leader line only for the step being executed or pointed at: one line is
      // an explanation, fourteen would be the hairball this replaced.
      if (active || (highlight && highlight.id === node.id)) {
        ctx.globalAlpha = 0.5;
        ctx.strokeStyle = active ? "rgba(255,230,163,.5)" : "rgba(237,247,239,.35)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(GUTTER + width + 6, y);
        ctx.lineTo(p.x - nodeRadius(node) * p.scale - 4, p.y);
        ctx.stroke();
      }
      ctx.globalAlpha = clamp(node.reveal, 0, 1);
      ctx.fillStyle = active ? "#ffe6a3"
        : node.status === "skipped" ? "rgba(237,247,239,.34)"
        : node.status === "done" ? "rgba(237,247,239,.72)"
        : "rgba(237,247,239,.4)";
      ctx.fillText(text, GUTTER, y);
      // Status pip, so the axis carries the run's shape on its own.
      ctx.globalAlpha = 0.9;
      ctx.fillStyle = active ? "#ffe6a3"
        : node.status === "done" ? "hsla(150,80%,62%,.8)"
        : node.status === "failed" ? "hsla(2,85%,64%,.9)"
        : "rgba(237,247,239,.18)";
      ctx.beginPath();
      ctx.arc(GUTTER - 6, y, 2, 0, TAU);
      ctx.fill();
    });

    // Everything else is labelled only on demand: hover, selection, or a camera
    // close enough that there is room for the outer shell's names.
    var rows = [];
    scene.nodes.forEach(function (node) {
      if (node.kind === "step" || node.reveal < 0.12) return;
      var show = (highlight && !state.soft
            && (highlight.id === node.id || (near && near[node.id])))
        || (state.selected === node.id)
        || (node.kind === "tool" && cam.dist < 640);
      if (!show) return;
      var p = project(node, cam, w, h);
      if (!p.visible || p.x < -60 || p.x > w + 60) return;
      rows.push({ node: node, p: p });
    });
    rows.sort(function (a, b) { return a.p.depth - b.p.depth; });   // nearest first
    var placed = [];
    function fits(x, y, width) {
      if (x < GUTTER + 74 && y > 6 && y < h - 6) return false;       // keep off the axis
      for (var i = 0; i < placed.length; i++) {
        var box = placed[i];
        if (x < box.x + box.w + 5 && x + width + 5 > box.x
            && y < box.y + 15 && y + 15 > box.y) return false;
      }
      return true;
    }
    rows.slice(0, 14).forEach(function (row) {
      var node = row.node, p = row.p;
      var text = node.label;
      var width = ctx.measureText(text).width;
      var offset = nodeRadius(node) * p.scale + 7;
      var x = p.x + offset;
      var y = p.y - 7.5;
      if (x + width > w - 6) x = p.x - offset - width;               // flip inward at the edge
      if (!fits(x, y, width)) return;
      placed.push({ x: x, y: y, w: width });
      ctx.globalAlpha = clamp(node.reveal * 0.85, 0, 1);
      ctx.fillStyle = "rgba(6,10,8,.66)";
      ctx.fillRect(x - 3, y, width + 6, 15);
      ctx.fillStyle = node.status === "skipped" ? "rgba(237,247,239,.45)" : "#edf7ef";
      ctx.fillText(text, x, y + 7.5);
    });
    ctx.restore();
  };

  // -------------------------------------------------------------- fallback

  /* No 2D context (or no canvas at all): the same facts as a table. The panel is
   * allowed to be less beautiful; it is not allowed to be absent. */
  function fallbackHtml(topology, pipeline) {
    var steps = (topology && topology.steps) || [];
    var stats = (topology && topology.stats) || {};
    var status = {};
    (((pipeline || {}).steps) || []).forEach(function (step) { status[step.name] = step.status; });
    if (!steps.length) return '<div class="empty">No topology available</div>';
    var rows = steps.map(function (step) {
      return '<tr><td>' + escapeHtml(step.name) + '</td>'
        + '<td>' + escapeHtml(status[step.name] || "pending") + '</td>'
        + '<td>' + escapeHtml(step.functions) + '</td>'
        + '<td>' + escapeHtml((step.modules || []).join(" ")) + '</td>'
        + '<td>' + escapeHtml((step.tools || []).join(" ") || "none") + '</td></tr>';
    }).join("");
    return '<table class="trace-fallback"><caption>'
      + escapeHtml(stats.functions || 0) + ' functions, '
      + escapeHtml(stats.tools || 0) + ' server tools, '
      + escapeHtml(stats.edges || 0) + ' call edges</caption>'
      + '<thead><tr><th>Step</th><th>State</th><th>Functions</th><th>Modules</th><th>Server tools</th></tr></thead>'
      + '<tbody>' + rows + '</tbody></table>';
  }

  var api = {
    HUES: HUES,
    TUNING: TUNING,
    hash32: hash32,
    seeded: seeded,
    hue: hue,
    clamp: clamp,
    lerp: lerp,
    meanAngle: meanAngle,
    escapeHtml: escapeHtml,
    makeCamera: makeCamera,
    project: project,
    orbit: orbit,
    zoom: zoom,
    buildScene: buildScene,
    relax: relax,
    settle: settle,
    extent: extent,
    frame: frame,
    recenter: recenter,
    screenBox: screenBox,
    applyRun: applyRun,
    advance: advance,
    route: route,
    spawnPackets: spawnPackets,
    movePackets: movePackets,
    pick: pick,
    nodeRadius: nodeRadius,
    neighbourhood: neighbourhood,
    Renderer: Renderer,
    fallbackHtml: fallbackHtml
  };

  root.Trace3D = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
