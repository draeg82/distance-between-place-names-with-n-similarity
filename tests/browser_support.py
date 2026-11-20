"""Running the front end's modules outside a browser, and the source rules that hold them to the API.

There is no browser in this suite. What there is: the four scripts and one data file the front end
is made of, Node where a developer has it, and a set of assertions about what a page is allowed to
compute for itself. All three live here because `tests/test_browser_app.py` and
`tests/test_browser_map.py` both need them, and a rule about what the page may contain is worth one
copy of.

The rules come in two sizes. `BANNED` is the wide one - trig, powers, a square root, an Earth
radius, a name comparison, a second `distance_km` - and it applies to everything the browser loads
except `map.js`. `map.js` gets `MAP_BANNED` instead: drawing an arc means interpolating between two
points on a sphere, which is trigonometry, and the thing that must stay impossible there is turning
coordinates into a *distance*. Hence no `6371` and no kilometres in that file, which is what leaves
the number a reader reads belonging to the API.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "distnsimilarity" / "api" / "static"
HTML = STATIC / "index.html"
APP_JS = STATIC / "app.js"
RESULTS_JS = STATIC / "results.js"
MAP_JS = STATIC / "map.js"
STYLES = STATIC / "styles.css"
OUTLINE = STATIC / "world-outline.json"

#: Everything a page is allowed to contain without the API's say-so. Trig, powers and a square root
#: together are a distance formula; `differing_positions:` and `character_difference:` written by
#: the page are a second opinion about what a pair looks like; `http` in a `src` or `href` is
#: somebody else's server in the middle of a tool that runs on one laptop.
BANNED = (
    "Math.sqrt",
    "Math.pow",
    "Math.hypot",
    "Math.sin",
    "Math.cos",
    "Math.tan",
    "Math.asin",
    "Math.acos",
    "Math.atan",
    "6371",
    "haversine",
    "great_circle",
    "levenshtein",
    "differing_positions:",
    "character_difference:",
    "distance_km:",
)

#: What `map.js` may not do, given that interpolating an arc is its whole reason to exist. No Earth
#: radius in any spelling, so no length can come out of it; no `distance_km` at all, so the
#: kilometres a reader reads can only have come from the wire. Re-ordering the finds is banned by
#: behaviour instead - `tests/test_browser_map.py` checks that the map draws the ranking it was
#: given - because the one `.sort` in that file orders the two pieces of an arc that crossed the
#: antimeridian, which is not a ranking of anything.
MAP_BANNED = (
    "6371",
    "6378",
    "6357",
    "40075",
    "111.32",
    "haversine",
    "great_circle",
    "levenshtein",
    "distance_km",
    "differing_positions:",
    "character_difference:",
    ".toSorted(",
    ".reverse()",
    "localeCompare",
)

#: The assets the wide ban applies to. `map.js` is not one of them, and says why in its own header.
MEASURED_ASSETS = ("index.html", "app.js", "results.js", "styles.css")

#: Everything the browser is handed, by the name the server serves it under.
ASSET_NAMES = ("index.html", "app.js", "results.js", "map.js", "styles.css", "world-outline.json")


def page_assets() -> dict[str, str]:
    """The text of every file the front end is made of, by name."""
    assert STATIC.is_dir(), f"the front end should live in {STATIC}"
    paths = {
        "index.html": HTML,
        "app.js": APP_JS,
        "results.js": RESULTS_JS,
        "map.js": MAP_JS,
        "styles.css": STYLES,
        "world-outline.json": OUTLINE,
    }
    missing = [name for name, path in paths.items() if not path.is_file()]
    assert not missing, f"the front end is missing {missing}"
    assert set(paths) == set(ASSET_NAMES), "the list of assets and the list of names have diverged"
    return {name: path.read_text(encoding="utf-8") for name, path in paths.items()}


#: Call one exported function of one module with each case in turn and print the results as JSON.
#: `process.argv` carries the module, the function and the cases, because a JSON payload
#: interpolated into a JavaScript literal is a quoting bug waiting to be found.
MODULE_SCRIPT = """
import { pathToFileURL } from "node:url";
const module = await import(pathToFileURL(process.argv[1]).href);
const underTest = module[process.argv[2]];
const cases = JSON.parse(process.argv[3]);
process.stdout.write(JSON.stringify(cases.map((sent) => underTest(...sent))));
"""


def run_in_node(script: str, *arguments: str) -> Any:
    """Run a snippet with Node and read the JSON it prints.

    Node is a developer's tool here, not a dependency: where it is not installed the tests that
    need it skip, and `uv run pytest` still means one command on a machine with only the packages
    in `pyproject.toml`.
    """
    if shutil.which("node") is None:
        pytest.skip("the front end is checked with Node, which is not installed here")
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, *arguments],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        # Node treats an unread standard input as a stream to wait for, and descriptor 0 belongs to
        # whatever is running the suite - a terminal, a CI job, another process capturing this
        # process's output. Asking for none of it is the difference between a test that runs and a
        # suite that appears to have hung.
        stdin=subprocess.DEVNULL,
    )
    assert completed.returncode == 0, (
        f"the front end's module did not run: {completed.stderr.strip()}"
    )
    return json.loads(completed.stdout.strip())


def in_module(module: Path, function: str, cases: list[list[object]]) -> list[object]:
    """Apply one exported function of one front-end module to each case, where a case is one call's arguments."""
    return run_in_node(MODULE_SCRIPT, str(module), function, json.dumps(cases))


def in_presentation(function: str, cases: list[list[object]]) -> list[object]:
    """`results.js`, which is where an event becomes words."""
    return in_module(RESULTS_JS, function, cases)


def in_map(function: str, cases: list[list[object]]) -> list[object]:
    """`map.js`, which is where coordinates become a picture."""
    return in_module(MAP_JS, function, cases)


#: `nameCell` turns data into elements, and Node has no page to make them from, so the check that
#: the highlight reaches the page brings its own stand-in document. It offers the three things the
#: marking uses - make an element, append to it, set its text - on purpose: an implementation that
#: needed more of a browser would be a sign the marking is no longer one small function's job.
ADAPTER_SCRIPT = """
import { pathToFileURL } from "node:url";
const adapter = await import(pathToFileURL(process.argv[1]).href);
const underTest = adapter[process.argv[2]];
const cases = JSON.parse(process.argv[3]);

function element(tag) {
  return {
    tagName: tag,
    children: [],
    className: "",
    title: "",
    textContent: "",
    dataset: {},
    append(...nodes) {
      this.children.push(...nodes);
    },
  };
}

function describe(node) {
  return {
    tag: node.tagName,
    class: node.className,
    title: node.title,
    text: node.textContent,
    children: node.children.map(describe),
  };
}

const page = { createElement: element };
const made = cases.map((sent) => underTest(...sent, page));
process.stdout.write(JSON.stringify(made.map(describe)));
"""


def in_the_adapter(function: str, cases: list[list[object]]) -> list[object]:
    """Apply one exported function of `app.js` to each case, with the stand-in document last."""
    return run_in_node(ADAPTER_SCRIPT, str(APP_JS), function, json.dumps(cases))


#: `paint` draws onto a canvas context, and Node has no canvas, so the check brings a context that
#: writes down what was asked of it instead of putting ink anywhere. Everything the painter does is
#: a method call or a style assignment, which is enough to say "two markers, one bold arc, and no
#: arc at all when the list is empty" without a browser - and enough to check a coordinate against
#: the numbers it was drawn at.
PAINT_SCRIPT = """
import { pathToFileURL } from "node:url";
const map = await import(pathToFileURL(process.argv[1]).href);
const cases = JSON.parse(process.argv[2]);

function recording(calls) {
  const styles = {};
  return new Proxy(styles, {
    get: (stored, name) => {
      if (name in stored) {
        return stored[name];
      }
      return (...args) => {
        calls.push([
          String(name),
          args.filter((one) => typeof one === "number" || typeof one === "string"),
        ]);
      };
    },
    set: (stored, name, value) => {
      stored[name] = value;
      calls.push([`=${String(name)}`, [String(value)]]);
      return true;
    },
  });
}

const painted = cases.map((sent) => {
  const calls = [];
  map.paint(recording(calls), {
    width: sent.width ?? 1000,
    height: sent.height ?? 500,
    paths: sent.paths ?? [],
    scene: map.sceneOf({
      rows: sent.rows ?? [],
      focus: sent.focus ?? 0,
      share: sent.share ?? 1,
    }),
  });
  return calls;
});
process.stdout.write(JSON.stringify(painted));
"""


def paint_scene(
    rows: list[dict[str, Any]],
    *,
    focus: int = 0,
    share: float = 1.0,
    paths: list[list[list[float]]] | None = None,
    width: int = 1000,
    height: int = 500,
) -> list[tuple[str, list[Any]]]:
    """Draw one scene with a recording context and hand back what it was asked to do."""
    case: dict[str, Any] = {"rows": rows, "focus": focus, "share": share, "width": width, "height": height}
    if paths is not None:
        case["paths"] = paths
    [calls] = run_in_node(PAINT_SCRIPT, str(MAP_JS), json.dumps([case]))
    return [(str(name), list(arguments)) for name, arguments in calls]


def asked(calls: list[tuple[str, list[Any]]], name: str) -> list[list[Any]]:
    """The arguments of every call to one method, in the order they happened."""
    return [arguments for called, arguments in calls if called == name]


#: A page to wire the adapter onto, and a server that says exactly what a test told it to.
#:
#: `app.js` is a thin layer over two things it gets from a browser: a document, and `fetch`. Hand
#: it a stand-in for both and the whole control surface - start, stop, restart, refuse - becomes
#: something a test can drive and then ask questions about, which is the only way "pressing Search
#: twice does not start two searches" and "stopping keeps the list" can be asserted at all. Nothing
#: is mocked inside the module: the fake ends at the same two boundaries a browser would provide,
#: and every answer it gives is a real `/v1/...` body from `tests/test_http_search.py`'s shape.
#:
#: A stream is a list of chunks; each chunk is a list of events to deliver on one read, or the
#: string ``"hold"``, which stops the stream there until the test releases it or hangs up on it.
#: That pause is the whole point: without it there is no such thing as "while a search is running".
PAGE_SCRIPT = """
import { pathToFileURL } from "node:url";

const modulePath = process.argv[1];
const plan = JSON.parse(process.argv[2]);

class Element {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.dataset = {};
    this.style = {};
    this.classes = new Set();
    this.listeners = new Map();
    this.textContent = "";
    this.className = "";
    this.title = "";
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.name = "";
    this.type = "";
    this.clientWidth = 1000;
    this.width = 1000;
    this.height = 500;
    this.tabIndex = -1;
    const self = this;
    this.classList = {
      add: (one) => self.classes.add(one),
      remove: (one) => self.classes.delete(one),
      contains: (one) => self.classes.has(one),
      toggle: (one, on) => {
        const wanted = on === undefined ? !self.classes.has(one) : Boolean(on);
        if (wanted) {
          self.classes.add(one);
        } else {
          self.classes.delete(one);
        }
        return wanted;
      },
    };
  }

  append(...nodes) {
    for (const node of nodes) {
      node.parentElement = this;
    }
    this.children.push(...nodes);
  }

  appendChild(node) {
    this.append(node);
    return node;
  }

  replaceChildren(...nodes) {
    this.children = [];
    this.append(...nodes);
  }

  addEventListener(kind, handler) {
    if (!this.listeners.has(kind)) {
      this.listeners.set(kind, []);
    }
    this.listeners.get(kind).push(handler);
  }

  dispatch(kind, event = {}) {
    for (const handler of this.listeners.get(kind) ?? []) {
      handler({ type: kind, target: this, preventDefault() {}, ...event });
    }
  }

  closest(selector) {
    let node = this;
    while (node) {
      if (node.tagName === selector.toUpperCase()) {
        return node;
      }
      node = node.parentElement;
    }
    return null;
  }

  contains(node) {
    let one = node;
    while (one) {
      if (one === this) {
        return true;
      }
      one = one.parentElement;
    }
    return false;
  }

  getContext() {
    return new Proxy(
      {},
      {
        get: (stored, name) => (name in stored ? stored[name] : () => undefined),
        set: (stored, name, value) => {
          stored[name] = value;
          return true;
        },
      },
    );
  }
}

// Every string each element was ever given, in order. A test that asks "did the page ever say
// it had finished?" is asking about the whole history of a line, not about whatever happens to be
// on screen when the test stops looking, and a stopped search's last line is exactly the case
// where the two differ.
const written = new Map();

const ids = new Map();
const id = (name) => {
  if (!ids.has(name)) {
    const tag = name.includes("button") ? "button" : name === "world-map" ? "canvas" : "p";
    const made = new Element(tag);
    made.id = name;
    const history = [];
    written.set(name, history);
    let text = "";
    Object.defineProperty(made, "textContent", {
      get: () => text,
      set: (value) => {
        text = String(value);
        history.push(text);
      },
    });
    ids.set(name, made);
  }
  return ids.get(name);
};

const form = id("search-form");
form.tagName = "FORM";
const fields = new Map();
for (const [name, value] of Object.entries(plan.parameters ?? {})) {
  const field = new Element("input");
  field.name = name;
  field.value = String(value);
  field.type = name === "dataset" || name === "direction" ? "select-one" : "number";
  fields.set(name, field);
}
Object.defineProperty(form, "elements", {
  get: () => [...fields.values(), id("start-button"), id("stop-button")],
});

/**
 * The browser's own form-to-pairs, over the stand-in form's fields.
 *
 * `app.js` forwards the form with `new FormData(form)`, which is a browser facility rather than
 * something the module owns, so it is the stand-in's job to provide it. Like the real thing it
 * yields every named field that holds text, including the ones left empty - the empty ones are how
 * "a population floor nobody typed" reaches the page's own filter rather than the query string -
 * and it skips the controls with no name, which are buttons and not parameters.
 */
globalThis.FormData = class FormData {
  constructor(theForm) {
    const pairs = Array.from(theForm?.elements ?? [])
      .filter((field) => String(field.name ?? "") !== "" && typeof field.value === "string")
      .map((field) => [String(field.name), field.value]);
    this[Symbol.iterator] = () => pairs[Symbol.iterator]();
  }
};

id("world-map").clientWidth = 1000;

const document = {
  body: new Element("body"),
  createElement: (tag) => new Element(tag),
  getElementById: id,
  addEventListener() {},
};

/** The text a person reading this node would read, however the markup is nested under it. */
function textOf(node) {
  return node.children.length === 0 ? node.textContent : node.children.map(textOf).join("");
}

/** One answer the adapter asked for, and whether the caller hung up on it. */
const calls = [];
const streams = [];
let streamIndex = 0;

const abortError = () => Object.assign(new Error("This stream was hung up on"), { name: "AbortError" });
const encoder = new TextEncoder();

function streamBody(chunks, signal) {
  const queue = [...chunks];
  let released = null;
  const stream = {
    release() {
      const resolve = released;
      released = null;
      if (resolve) {
        resolve();
        return true;
      }
      return false;
    },
  };
  async function read() {
    if (signal?.aborted) {
      throw abortError();
    }
    if (queue.length === 0) {
      return { done: true, value: undefined };
    }
    const [next] = queue;
    if (next === "hold") {
      queue.shift();
      await new Promise((resolve, reject) => {
        released = resolve;
        const onAbort = () => reject(abortError());
        if (signal?.aborted) {
          onAbort();
          return;
        }
        signal?.addEventListener("abort", onAbort, { once: true });
      });
      return read();
    }
    queue.shift();
    // A chunk is either a list of events, which is encoded the way the server encodes them, or text
    // exactly as this test wants it to arrive - which is how a test cuts a record in half.
    const text = typeof next === "string" ? next : next.map((event) => JSON.stringify(event)).join("\\n") + "\\n";
    return { done: false, value: encoder.encode(text) };
  }
  return { stream, reader: { read, cancel() {} } };
}

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      return structuredClone(body);
    },
  };
}

/**
 * A scripted answer that echoes what was asked, the way `/v1/estimate` really does.
 *
 * Any value written as `"@query"` is replaced by that parameter from the request, so a test can
 * say "this server answers with the parameters it was given" once and then watch the page change
 * the parameters. A parameter the page left out is left out of the answer too, which is how a
 * missing population floor stays missing rather than becoming a zero.
 */
function fillFromQuery(body, query) {
  const sent = Object.fromEntries(new URLSearchParams(query));
  const counted = new Set(["n", "top_k", "min_population"]);
  const filled = {};
  for (const [key, value] of Object.entries(body)) {
    if (value !== "@query") {
      filled[key] = value;
    } else if (key in sent) {
      filled[key] = counted.has(key) ? Number(sent[key]) : sent[key];
    }
  }
  return filled;
}

globalThis.document = document;
globalThis.window = { addEventListener() {} };
globalThis.matchMedia = () => ({ matches: plan.reduceMotion !== false });

/**
 * A page that has never reloaded, and would notice if it did.
 *
 * "Changing the parameters and searching again needs no page reload" is only an assertion if the
 * stand-in browser keeps score: a script that set `location.href` or called `reload()` would get a
 * fresh page in a real browser, wiping the list and the price with it, and here it gets recorded.
 */
let reloaded = false;
globalThis.location = {
  reload() {
    reloaded = true;
  },
  assign() {
    reloaded = true;
  },
  get href() {
    return "http://127.0.0.1:8000/";
  },
  set href(value) {
    reloaded = true;
  },
};

globalThis.fetch = async (url, options) => {
  const target = String(url);
  const signal = options?.signal ?? null;
  const asked = { url: target, path: target.split("?")[0], query: target.split("?")[1] ?? "", aborted: false };
  calls.push(asked);
  if (signal?.aborted) {
    // A browser never sends a request whose signal is already aborted, so neither does this one -
    // which is what makes "the page hung up on every stream it opened" mean something.
    asked.aborted = true;
    throw abortError();
  }
  signal?.addEventListener("abort", () => {
    asked.aborted = true;
  });
  if (asked.path === "/v1/search") {
    const chunks = plan.streams?.[streamIndex] ?? [];
    streamIndex += 1;
    const { stream, reader } = streamBody(chunks, signal);
    streams.push(stream);
    return { ok: true, status: 200, json: async () => ({}), body: { getReader: () => reader } };
  }
  const answer = (plan.json ?? {})[asked.path];
  if (answer === undefined) {
    return jsonResponse({ error: "NotServed", message: `this test server does not serve ${asked.path}` }, 404);
  }
  if (typeof answer?.status === "number" && answer.status >= 400) {
    return jsonResponse(fillFromQuery(answer.body ?? {}, asked.query), answer.status);
  }
  return jsonResponse(fillFromQuery(answer ?? {}, asked.query));
};

const adapter = await import(pathToFileURL(modulePath).href);
await new Promise((resolve) => setTimeout(resolve, 0));

const page = {
  calls,
  lines: () => ({
    database: id("database-line").textContent,
    estimate: id("estimate-line").textContent,
    status: id("status-text").textContent,
    message: id("message-line").textContent,
    progress: id("progress-line").textContent,
    caption: id("arc-caption").textContent,
  }),
  urls: () => calls.map((one) => one.url),
  askedFor: (path) => calls.filter((one) => one.path === path).map((one) => one.query),
  /**
   * The rows on screen, as a person reads them across.
   *
   * One string per row, the cells joined: what a page test has to be able to ask about a finding is
   * that both names, both countries, the kilometres and the difference are in the row a reader would
   * be looking at. A name cell holds its text in spans (that is what the highlighting is), so this
   * reads the whole run of text under each cell rather than only what sits directly on it.
   */
  rows: () => id("result-rows").children.map((row) => row.children.map(textOf).join(" | ")),
  wrote: (name) => written.get(name) ?? [],
  reloaded: () => reloaded,
  /** Every `/v1/search` this page asked for, and whether it hung up on that one. */
  searches: () =>
    calls
      .filter((one) => one.path === "/v1/search")
      .map((one) => ({ query: one.query, aborted: one.aborted })),
  tableHidden: () => id("results-table").hidden,
  controls: () => ({
    searching: document.body.classList.contains("searching"),
    startDisabled: id("start-button").disabled,
    stopHidden: id("stop-button").hidden,
    indicatorHidden: id("working-indicator").hidden,
    fieldsDisabled: [...fields.values()].every((field) => field.disabled),
  }),
  press: (name) => {
    // A real click on the search button is a form submit, and a real click on Stop is a click on
    // that button: this goes through the listeners the page wired up, not through the module.
    if (name === "start-button") {
      form.dispatch("submit", { preventDefault() {} });
      return;
    }
    id(name).dispatch("click", { preventDefault() {} });
  },
  parameters: (values) => {
    for (const [name, value] of Object.entries(values)) {
      if (!fields.has(name)) {
        const field = new Element("input");
        field.name = name;
        field.type = "number";
        fields.set(name, field);
      }
      fields.get(name).value = String(value);
    }
    form.dispatch("input");
    form.dispatch("change");
  },
  async settle(rounds = 8) {
    for (let round = 0; round < rounds; round += 1) {
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  },
  release(every = 4) {
    for (let one = 0; one < every; one += 1) {
      streams.some((stream) => stream.release());
    }
  },
  start() {
    if (typeof adapter.start !== "function") {
      throw new Error("app.js does not export a start control for the test to press");
    }
    return adapter.start({ preventDefault() {} });
  },
  stop() {
    if (typeof adapter.stop !== "function") {
      throw new Error("app.js does not export a stop control for the test to press");
    }
    return adapter.stop({ preventDefault() {} });
  },
};

let result;
try {
  result = await new Function("page", `"use strict"; return (async () => { ${process.argv[3]} })();`)(page);
} catch (error) {
  process.stderr.write(`${error && error.stack ? error.stack : error}\\n`);
  process.exitCode = 1;
  result = null;
}
process.stdout.write(JSON.stringify(result ?? null));
"""


DEFAULT_PAGE_JSON: dict[str, Any] = {
    "/v1/datasets": {
        "database": {"path": "/tmp/places.sqlite3", "present": True, "places": 4210, "imported": True},
        "datasets": [
            {
                "threshold": None,
                "label": "every place in the database",
                "places": 4210,
                "candidate_pairs": 8_860_000,
            },
            {
                "threshold": 5000,
                "label": "cities of 5,000 or more people",
                "places": 120,
                "candidate_pairs": 7_140,
            },
        ],
    },
    "/static/world-outline.json": {"rings": []},
}


def drive_page(
    body: str,
    *,
    json_bodies: dict[str, Any] | None = None,
    streams: list[list[Any]] | None = None,
    parameters: dict[str, Any] | None = None,
    reduce_motion: bool = True,
    module: Path = APP_JS,
) -> Any:
    """Press the page's own controls against a scripted server, and report what the page came to.

    `body` is the test, written in JavaScript against the `page` object: press, wait, look. It
    returns a plain object, which comes back here as a `dict` for the assertions to read.
    """
    plan = {
        "json": {**DEFAULT_PAGE_JSON, **(json_bodies or {})},
        "streams": streams or [],
        "parameters": parameters or {"n": 1, "dataset": "5000", "direction": "greatest", "top_k": 50},
        "reduceMotion": reduce_motion,
    }
    return run_in_node(PAGE_SCRIPT, str(module), json.dumps(plan), body)
