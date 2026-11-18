/**
 * The map: land from the outline asset, arcs from the coordinates the API already sends.
 *
 * Two things are decided here rather than in `app.js`, because they are the whole job:
 *
 * **The projection is plate carrée** — `x = (longitude + 180) / 360`, `y = (90 - latitude) / 180`.
 * It is the one a reader can check with arithmetic: a place on the equator and the prime meridian
 * lands at the middle of the map, and a place at 45° north is a quarter of the way up from there.
 * A prettier projection would put a table of constants between the coordinates and the dot.
 *
 * **The map does not zoom or pan.** The scale that this page exists to show — two names one letter
 * apart, nineteen thousand kilometres apart — is only visible with the whole world on screen, and a
 * window onto a piece of it is the opposite of that point. "Focus" therefore means emphasis: the
 * leading find, or whichever row was picked, is drawn boldly and the rest faintly behind it.
 *
 * An arc is a great circle, sampled by interpolating between the two unit vectors — which needs
 * trigonometry, and is the only reason this file has any. Note what it never does: multiply by the
 * radius of the Earth, and no length in any unit ever comes out of it, so the distance a reader
 * reads can only have come from the API. `tests/test_browser_map.py` holds that line.
 *
 * Antimeridian: an arc from New Zealand to New York leaves the right-hand edge and arrives at the
 * left. Longitudes are unwrapped first (a step of more than 180° means the seam was crossed, so the
 * rest of the arc moves by 360°), and the continuous polyline is then copied back into the world
 * window, which turns it into the two pieces a canvas has to stroke.
 *
 * Antipodes: when the two places are very nearly opposite each other, the great circle between them
 * is not determined — every azimuth is the same length. Rather than let `asin` of 1/0 decide, the
 * route is drawn over the nearer pole, which is what a reader tracing it with a finger expects.
 */

const RADIANS = Math.PI / 180;

/** Where the coastline lives: a path on this same server, never another machine's. */
export const OUTLINE_URL = "/static/world-outline.json";

/** Vertices per arc. Ninety-six is smooth at a thousand pixels wide and cheap at sixty frames. */
export const ARC_STEPS = 96;

const SEA = "#e8f0f6";
const GRATICULE = "#d3e0ea";
const LAND = "#d8d0c0";
const COAST = "#b3a892";
const TRAIL = "rgba(47, 111, 137, 0.34)";
const FOCUS = "#c0392b";
const MARKER = "#1d3440";

/**
 * The pair of a row, in the words this file uses.
 *
 * Reading the wire is `app.js`'s job; this is the smallest bridge that lets `map.js` draw a row
 * without knowing the names on it.
 */
export function pairOf(row) {
  const place = (one) => ({
    name: one.display_name ?? one.asciiname ?? "",
    longitude: Number(one.longitude),
    latitude: Number(one.latitude),
  });
  return { from: place(row.first), to: place(row.second) };
}

/** A point on the map, in fractions of it: 0 is the west edge or the north pole, 1 the east edge or the south pole. */
export function project(point) {
  return { x: (point.longitude + 180) / 360, y: (90 - point.latitude) / 180 };
}

function unit(point) {
  const latitude = point.latitude * RADIANS;
  const longitude = point.longitude * RADIANS;
  return [
    Math.cos(latitude) * Math.cos(longitude),
    Math.cos(latitude) * Math.sin(longitude),
    Math.sin(latitude),
  ];
}

function toDegrees(vector) {
  return {
    longitude: Math.atan2(vector[1], vector[0]) / RADIANS,
    latitude: Math.atan2(vector[2], Math.hypot(vector[0], vector[1])) / RADIANS,
  };
}

/**
 * The route over the nearer pole, for a pair so close to antipodal that the great circle is not
 * determined. Two legs: up (or down) the meridian of the first place, then down the meridian of the
 * second, with the pole itself shared so the polyline has no gap in it.
 */
function overAPole(from, to, steps) {
  const pole = from.latitude + to.latitude >= 0 ? 90 : -90;
  const half = Math.max(1, Math.round(steps / 2));
  const points = [];
  for (let index = 0; index <= half; index += 1) {
    const share = index / half;
    points.push({
      longitude: from.longitude,
      latitude: from.latitude + share * (pole - from.latitude),
    });
  }
  for (let index = 1; index <= half; index += 1) {
    const share = index / half;
    points.push({
      longitude: from.longitude + share * (to.longitude - from.longitude),
      latitude: pole + share * (to.latitude - pole),
    });
  }
  return points;
}

/**
 * The great circle through two places, sampled. Interpolating along the arc rather than along the
 * map: a straight line on a plate carrée map is a curve on the globe, and the curve is the finding.
 */
export function arcPoints(from, to, steps = ARC_STEPS) {
  const start = unit(from);
  const end = unit(to);
  const dot = Math.max(-1, Math.min(1, start[0] * end[0] + start[1] * end[1] + start[2] * end[2]));
  const central = Math.acos(dot);
  if (central < 1e-9) {
    return [
      { ...from },
      { ...to },
    ];
  }
  if (Math.PI - central < 1e-6) {
    return overAPole(from, to, steps);
  }
  const sine = Math.sin(central);
  const points = [];
  for (let index = 0; index <= steps; index += 1) {
    const share = index / steps;
    const a = Math.sin((1 - share) * central) / sine;
    const b = Math.sin(share * central) / sine;
    points.push(
      toDegrees([
        a * start[0] + b * end[0],
        a * start[1] + b * end[1],
        a * start[2] + b * end[2],
      ]),
    );
  }
  return points;
}

/**
 * Longitudes that keep walking in one direction instead of jumping at the seam. A step of more than
 * 180° is a crossing, so everything after it moves by a whole world; the shape of the arc is
 * unchanged and its `x` goes on growing past 1 (or below 0), which is what makes it strokeable.
 */
export function unwrapped(points) {
  let offset = 0;
  let previous = points.length ? points[0].longitude : 0;
  return points.map((point) => {
    if (previous - point.longitude > 180) {
      offset += 360;
    } else if (point.longitude - previous > 180) {
      offset -= 360;
    }
    previous = point.longitude;
    return { ...point, longitude: point.longitude + offset };
  });
}

/** Project a list of points, of which the longitudes may have been unwrapped past the edges. */
export function projected(points) {
  return points.map((point) => ({
    x: (point.longitude + 180) / 360,
    y: (90 - point.latitude) / 180,
  }));
}

/** How far along a polyline a share of its length is: the drawing-in of an arc. */
export function partial(points, share) {
  if (points.length < 2 || !(share > 0)) {
    return [];
  }
  if (share >= 1) {
    return points.slice();
  }
  let total = 0;
  const steps = [];
  for (let index = 1; index < points.length; index += 1) {
    const length = Math.hypot(points[index].x - points[index - 1].x, points[index].y - points[index - 1].y);
    steps.push(length);
    total += length;
  }
  const target = total * share;
  const kept = [points[0]];
  let walked = 0;
  for (let index = 0; index < steps.length; index += 1) {
    if (walked + steps[index] >= target) {
      const within = steps[index] === 0 ? 0 : (target - walked) / steps[index];
      const from = points[index];
      const to = points[index + 1];
      kept.push({ x: from.x + within * (to.x - from.x), y: from.y + within * (to.y - from.y) });
      return kept;
    }
    walked += steps[index];
    kept.push(points[index + 1]);
  }
  return kept;
}

/** How far along the step `from` to `to` a map edge is passed, or null when it is not passed. */
function share_of(from, to, edge) {
  if (to.x === from.x) {
    return null;
  }
  const share = (edge - from.x) / (to.x - from.x);
  return share > 0 && share < 1 ? share : null;
}

function crossing(from, to, share, edge) {
  return { x: edge, y: from.y + share * (to.y - from.y) };
}

/**
 * The edges one step crosses, in the order the step meets them. Worked out by hand rather than
 * sorted: a step can leave the world and come back into it, and the two crossings have to be drawn
 * in the order the arc arrives at them or a westward arc doubles back on itself.
 */
function crossingsOf(from, to) {
  const atZero = share_of(from, to, 0);
  const atOne = share_of(from, to, 1);
  if (atZero === null && atOne === null) {
    return [];
  }
  const zero = atZero === null ? null : crossing(from, to, atZero, 0);
  const one = atOne === null ? null : crossing(from, to, atOne, 1);
  const first = one === null || (zero !== null && atZero < atOne) ? zero : one;
  const second = one === null || (zero !== null && atZero < atOne) ? one : zero;
  return second === null ? [first] : [first, second];
}

function withinTheWorld(point) {
  return point.x >= 0 && point.x <= 1;
}

/**
 * Back into the world window: the pieces of a continuous polyline that fall inside it, one copy per
 * world it touches. An arc that crosses the antimeridian comes out as two polylines - the one that
 * leaves the right edge and the one that arrives at the left - and nothing anywhere is outside 0..1,
 * so the canvas cannot stroke a line across the whole map.
 *
 * Where a step leaves the window it is drawn all the way to the edge and picked up at the far edge
 * at the same height, because that is what a map that wraps looks like. Sampling longitudes into
 * the next world is not enough on its own: Wellington to New York would stop a vertex short of the
 * edge and start again a vertex inside it, and the gap would read as a place where the world ends.
 */
export function worldCopies(points) {
  let run = [];
  const lines = [];
  const add = (point) => {
    const last = run[run.length - 1];
    if (!last || Math.abs(last.x - point.x) > 1e-12 || Math.abs(last.y - point.y) > 1e-12) {
      run.push(point);
    }
  };
  const flush = () => {
    if (run.length > 1) {
      lines.push(run);
    }
    run = [];
  };

  for (const shift of [-1, 0, 1]) {
    const shifted = points.map((point) => ({ x: point.x + shift, y: point.y }));
    for (let index = 1; index < shifted.length; index += 1) {
      const from = shifted[index - 1];
      const to = shifted[index];
      const crossings = crossingsOf(from, to);
      if (withinTheWorld(from) && withinTheWorld(to)) {
        add(from);
        add(to);
      } else if (withinTheWorld(from)) {
        add(from);
        crossings.forEach(add);
        flush();
      } else if (withinTheWorld(to)) {
        flush();
        crossings.forEach(add);
        add(to);
      } else if (crossings.length === 2) {
        flush();
        crossings.forEach(add);
        flush();
      }
    }
    flush();
  }
  return lines.sort((left, right) => left[0].x - right[0].x);
}

/** An arc's polyline pieces, from places to strokeable coordinates. */
export function arcLines(from, to, share = 1) {
  return worldCopies(partial(projected(unwrapped(arcPoints(from, to))), share));
}

/**
 * What to draw: the focused pair bold, every other find faint behind it. `focus` defaults to 0,
 * which is the leading find — the list arrives ranked, and the map draws it in that order without
 * looking at the distances that put it there.
 */
export function sceneOf({ rows = [], focus = 0, share = 1 } = {}) {
  const arcs = rows.map((row, index) => {
    const pair = pairOf(row);
    return {
      role: index === focus ? "focus" : "trail",
      lines: arcLines(pair.from, pair.to, index === focus ? share : 1),
    };
  });
  const leads = rows[focus];
  const markers = leads
    ? [pairOf(leads).from, pairOf(leads).to].map((point) => ({ ...project(point), name: point.name }))
    : [];
  return { arcs, markers, empty: rows.length === 0 };
}

/** The outline asset, in strokeable rings. */
export function landPaths(rings) {
  return rings.map((ring) =>
    ring.map(([longitude, latitude]) => project({ longitude, latitude })),
  );
}

/** Read the coastline off this server. `fetcher` is a parameter so a test can hand it a file. */
export async function loadOutline(fetcher = fetch, url = OUTLINE_URL) {
  const response = await fetcher(url);
  if (!response.ok) {
    throw new Error(`${url} is not here (${response.status})`);
  }
  const outline = await response.json();
  return { rings: outline.rings, source: outline.source, paths: landPaths(outline.rings) };
}

/**
 * Draw the whole thing. `width` and `height` are CSS pixels; the caller has already scaled the
 * context for the device, so nothing here knows about pixels per inch.
 */
export function paint(context, { width, height, paths = [], scene, animate = false }) {
  context.clearRect(0, 0, width, height);
  context.fillStyle = SEA;
  context.fillRect(0, 0, width, height);

  context.strokeStyle = GRATICULE;
  context.lineWidth = 1;
  context.beginPath();
  for (let degree = -150; degree <= 150; degree += 30) {
    const x = ((degree + 180) / 360) * width;
    context.moveTo(x, 0);
    context.lineTo(x, height);
  }
  for (let degree = -60; degree <= 60; degree += 30) {
    const y = ((90 - degree) / 180) * height;
    context.moveTo(0, y);
    context.lineTo(width, y);
  }
  context.stroke();

  if (paths.length) {
    context.beginPath();
    for (const ring of paths) {
      ring.forEach((point, index) => {
        if (index === 0) {
          context.moveTo(point.x * width, point.y * height);
        } else {
          context.lineTo(point.x * width, point.y * height);
        }
      });
      context.closePath();
    }
    context.fillStyle = LAND;
    context.fill("evenodd");
    context.strokeStyle = COAST;
    context.lineWidth = 0.75;
    context.stroke();
  }

  const focused = scene ? scene.arcs.filter((arc) => arc.role === "focus") : [];
  const trailing = scene ? scene.arcs.filter((arc) => arc.role !== "focus") : [];

  context.lineJoin = "round";
  context.lineCap = "round";
  context.strokeStyle = TRAIL;
  context.lineWidth = 1.4;
  for (const arc of trailing) {
    for (const line of arc.lines) {
      context.beginPath();
      line.forEach((point, index) => {
        if (index === 0) {
          context.moveTo(point.x * width, point.y * height);
        } else {
          context.lineTo(point.x * width, point.y * height);
        }
      });
      context.stroke();
    }
  }

  for (const arc of focused) {
    // A pale wide line under the bold one keeps an arc readable where it crosses land.
    context.strokeStyle = "rgba(255, 255, 255, 0.75)";
    context.lineWidth = 5;
    strokeAll(context, arc.lines, width, height);
    context.strokeStyle = FOCUS;
    context.lineWidth = 2.4;
    strokeAll(context, arc.lines, width, height);
  }

  if (scene && !scene.empty && scene.markers.length) {
    for (const marker of scene.markers) {
      context.beginPath();
      context.arc(marker.x * width, marker.y * height, animate ? 3.5 : 4, 0, Math.PI * 2);
      context.fillStyle = MARKER;
      context.fill();
      context.lineWidth = 1.5;
      context.strokeStyle = "#ffffff";
      context.stroke();
    }
  }
}

function strokeAll(context, lines, width, height) {
  for (const line of lines) {
    context.beginPath();
    line.forEach((point, index) => {
      if (index === 0) {
        context.moveTo(point.x * width, point.y * height);
      } else {
        context.lineTo(point.x * width, point.y * height);
      }
    });
    context.stroke();
  }
}
