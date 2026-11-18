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
