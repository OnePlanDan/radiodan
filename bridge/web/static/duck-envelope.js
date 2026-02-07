/**
 * Duck Envelope Curve Editor
 *
 * SVG-based interactive editor for duck-in/out bezier curves.
 * Quadratic bezier: eased = 2*(1-t)*t*cy + t*t
 * Control point X is locked at section midpoint — visual matches audio exactly.
 *
 * Layout (viewBox 0 0 500 200):
 *   |--pre 60px--|--duck-in 130px--|--ducked 120px--|--duck-out 130px--|--post 60px--|
 *   Y: 20 (vol=1.0) to 180 (vol=0.0)
 */
(function () {
  const SVG_NS = "http://www.w3.org/2000/svg";
  const W = 500, H = 200;
  const Y_TOP = 20, Y_BOT = 180;
  const PRE = 60, IN_W = 130, DUCK_W = 120, OUT_W = 130, POST = 60;

  const X_IN_START = PRE;
  const X_IN_END = PRE + IN_W;
  const X_OUT_START = X_IN_END + DUCK_W;
  const X_OUT_END = X_OUT_START + OUT_W;

  const editor = document.getElementById("duck-envelope-editor");
  const svg = document.getElementById("duck-envelope-svg");
  if (!editor || !svg) return;

  // State — read initial values from data attributes
  let state = {
    duckAmount: parseFloat(editor.dataset.duckAmount) || 0.15,
    duckInDuration: parseFloat(editor.dataset.duckInDuration) || 0.8,
    duckOutDuration: parseFloat(editor.dataset.duckOutDuration) || 0.6,
    duckInCurve: parseFloat(editor.dataset.duckInCurve) || 0.7,
    duckOutCurve: parseFloat(editor.dataset.duckOutCurve) || 0.3,
  };

  function volToY(vol) {
    return Y_TOP + (1.0 - vol) * (Y_BOT - Y_TOP);
  }

  function yToVol(y) {
    return 1.0 - (y - Y_TOP) / (Y_BOT - Y_TOP);
  }

  function bezierY(from, to, cy, t) {
    var eased = 2.0 * (1.0 - t) * t * cy + t * t;
    return from + (to - from) * eased;
  }

  function el(tag, attrs) {
    var e = document.createElementNS(SVG_NS, tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }

  function renderEnvelope() {
    svg.innerHTML = "";

    var yFull = volToY(1.0);
    var yDuck = volToY(state.duckAmount);

    // Background grid lines
    for (var v = 0; v <= 1.0; v += 0.25) {
      var gy = volToY(v);
      svg.appendChild(el("line", {
        x1: 0, y1: gy, x2: W, y2: gy,
        stroke: "#2a2d3e", "stroke-width": 1
      }));
    }

    // Section dividers (vertical)
    [X_IN_START, X_IN_END, X_OUT_START, X_OUT_END].forEach(function (x) {
      svg.appendChild(el("line", {
        x1: x, y1: Y_TOP - 5, x2: x, y2: Y_BOT + 5,
        stroke: "#2a2d3e", "stroke-width": 1, "stroke-dasharray": "3,3"
      }));
    });

    // Duck level reference line (dashed red)
    svg.appendChild(el("line", {
      x1: X_IN_START, y1: yDuck, x2: X_OUT_END, y2: yDuck,
      stroke: "#e74c3c", "stroke-width": 1, "stroke-dasharray": "4,4", opacity: 0.6
    }));

    // Build envelope path using bezier segments
    var inCtrlX = X_IN_START + IN_W / 2;
    var outCtrlX = X_OUT_START + OUT_W / 2;

    // Compute control point Y positions (where the draggable circles go)
    var inCtrlVol = bezierY(1.0, state.duckAmount, state.duckInCurve, 0.5);
    var inCtrlY = volToY(inCtrlVol);
    var outCtrlVol = bezierY(state.duckAmount, 1.0, state.duckOutCurve, 0.5);
    var outCtrlY = volToY(outCtrlVol);

    // For the SVG quadratic bezier Q command, we need the actual control point
    // Q control point for quadratic bezier that passes through our desired midpoint:
    // The SVG Q control point is: cy_svg = 2*midpoint - 0.5*start - 0.5*end
    var inQY = 2 * inCtrlY - 0.5 * yFull - 0.5 * yDuck;
    var outQY = 2 * outCtrlY - 0.5 * yDuck - 0.5 * yFull;

    // Filled area under the envelope
    var pathD = "M 0," + yFull
      + " L " + X_IN_START + "," + yFull
      + " Q " + inCtrlX + "," + inQY + " " + X_IN_END + "," + yDuck
      + " L " + X_OUT_START + "," + yDuck
      + " Q " + outCtrlX + "," + outQY + " " + X_OUT_END + "," + yFull
      + " L " + W + "," + yFull
      + " L " + W + "," + Y_BOT
      + " L 0," + Y_BOT + " Z";

    svg.appendChild(el("path", {
      d: pathD, fill: "rgba(108, 92, 231, 0.12)", stroke: "none"
    }));

    // Envelope stroke
    var strokeD = "M 0," + yFull
      + " L " + X_IN_START + "," + yFull
      + " Q " + inCtrlX + "," + inQY + " " + X_IN_END + "," + yDuck
      + " L " + X_OUT_START + "," + yDuck
      + " Q " + outCtrlX + "," + outQY + " " + X_OUT_END + "," + yFull
      + " L " + W + "," + yFull;

    svg.appendChild(el("path", {
      d: strokeD, fill: "none", stroke: "#6c5ce7", "stroke-width": 2
    }));

    // Dashed handle lines from control points to envelope edges
    svg.appendChild(el("line", {
      x1: inCtrlX, y1: yFull, x2: inCtrlX, y2: yDuck,
      stroke: "#6c5ce7", "stroke-width": 1, "stroke-dasharray": "3,3", opacity: 0.5
    }));
    svg.appendChild(el("line", {
      x1: outCtrlX, y1: yDuck, x2: outCtrlX, y2: yFull,
      stroke: "#6c5ce7", "stroke-width": 1, "stroke-dasharray": "3,3", opacity: 0.5
    }));

    // Labels
    var labels = [
      { x: PRE / 2, text: "pre" },
      { x: X_IN_START + IN_W / 2, text: "duck in" },
      { x: X_IN_END + DUCK_W / 2, text: "ducked" },
      { x: X_OUT_START + OUT_W / 2, text: "duck out" },
      { x: X_OUT_END + POST / 2, text: "post" },
    ];
    labels.forEach(function (l) {
      svg.appendChild(el("text", {
        x: l.x, y: Y_BOT + 16, "text-anchor": "middle",
        fill: "#7a7d8e", "font-size": "10", "font-family": "sans-serif"
      })).textContent = l.text;
    });

    // Y-axis volume labels
    [1.0, 0.75, 0.5, 0.25, 0.0].forEach(function (v) {
      svg.appendChild(el("text", {
        x: W - 4, y: volToY(v) - 3, "text-anchor": "end",
        fill: "#7a7d8e", "font-size": "8", "font-family": "monospace"
      })).textContent = (v * 100).toFixed(0) + "%";
    });

    // Draggable control point circles
    var inCircle = el("circle", {
      cx: inCtrlX, cy: inCtrlY, r: 7,
      fill: "#6c5ce7", stroke: "#fff", "stroke-width": 2,
      "data-cp": "in", cursor: "ns-resize"
    });
    svg.appendChild(inCircle);

    var outCircle = el("circle", {
      cx: outCtrlX, cy: outCtrlY, r: 7,
      fill: "#6c5ce7", stroke: "#fff", "stroke-width": 2,
      "data-cp": "out", cursor: "ns-resize"
    });
    svg.appendChild(outCircle);

    // Set up drag for both
    setupDrag(inCircle, "in");
    setupDrag(outCircle, "out");
  }

  function setupDrag(circle, which) {
    var dragging = false;

    function toSVG(clientX, clientY) {
      var pt = svg.createSVGPoint();
      pt.x = clientX;
      pt.y = clientY;
      return pt.matrixTransform(svg.getScreenCTM().inverse());
    }

    function onStart(e) {
      e.preventDefault();
      dragging = true;
      circle.setAttribute("r", "9");
    }

    function onMove(e) {
      if (!dragging) return;
      e.preventDefault();
      var clientY = e.touches ? e.touches[0].clientY : e.clientY;
      var clientX = e.touches ? e.touches[0].clientX : e.clientX;
      var svgPt = toSVG(clientX, clientY);

      // Clamp Y within envelope range
      var y = Math.max(Y_TOP, Math.min(Y_BOT, svgPt.y));

      // Convert Y to a curve value (cy parameter 0–1)
      // At midpoint (t=0.5): eased = 2*0.5*0.5*cy + 0.25 = 0.5*cy + 0.25
      // So vol_at_mid = from + (to-from) * (0.5*cy + 0.25)
      // Solving for cy: cy = 2 * ((vol_at_mid - from)/(to - from) - 0.25)
      var vol = yToVol(y);
      var from, to;
      if (which === "in") {
        from = 1.0;
        to = state.duckAmount;
      } else {
        from = state.duckAmount;
        to = 1.0;
      }

      var range = to - from;
      var cy;
      if (Math.abs(range) < 0.001) {
        cy = 0.5;
      } else {
        cy = 2.0 * ((vol - from) / range - 0.25);
        cy = Math.max(0.0, Math.min(1.0, cy));
      }

      if (which === "in") {
        state.duckInCurve = cy;
      } else {
        state.duckOutCurve = cy;
      }

      renderEnvelope();
    }

    function onEnd() {
      if (!dragging) return;
      dragging = false;
      // Post value to server
      if (which === "in") {
        postValue("/audio/duck-in-curve", state.duckInCurve);
      } else {
        postValue("/audio/duck-out-curve", state.duckOutCurve);
      }
    }

    // Mouse events
    circle.addEventListener("mousedown", onStart);
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onEnd);

    // Touch events
    circle.addEventListener("touchstart", onStart, { passive: false });
    document.addEventListener("touchmove", onMove, { passive: false });
    document.addEventListener("touchend", onEnd);
  }

  function postValue(url, value) {
    var body = new FormData();
    body.append("value", value.toFixed(4));
    fetch(url, { method: "POST", body: body });
  }

  // External API for slider oninput handlers
  window.updateDuckEnvelope = function (changes) {
    if (changes.duckAmount !== undefined) state.duckAmount = parseFloat(changes.duckAmount);
    if (changes.duckInDuration !== undefined) state.duckInDuration = parseFloat(changes.duckInDuration);
    if (changes.duckOutDuration !== undefined) state.duckOutDuration = parseFloat(changes.duckOutDuration);
    if (changes.duckInCurve !== undefined) state.duckInCurve = parseFloat(changes.duckInCurve);
    if (changes.duckOutCurve !== undefined) state.duckOutCurve = parseFloat(changes.duckOutCurve);
    renderEnvelope();
  };

  // Initial render
  renderEnvelope();
})();
