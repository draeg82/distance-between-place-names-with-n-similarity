# The map is drawn from a committed coastline

A world map with animated great-circle arcs has an obvious implementation: put Leaflet or MapLibre
in the page, point it at tile or vector-tile URLs, and let a library draw the world. That is what
the front end does not do. ADR 0008 already refused to load anything from another machine, and a
map library would have made the promise the tool is built on - one clone, `uv run`, a search on
loopback - quietly false: the page would look right and be empty of context the moment the network
was not there, which is exactly when a person is most likely to be running this on a machine with
all three dumps on it. A bundler was the other route to the same place, and ADR 0008 dealt with
that.

So the map is a `<canvas>` and 32 KB of JSON. `map.js` samples each pair's great circle, projects
it onto a plate carree world, and strokes it; `index.html` carries one canvas and a caption; and the
land is `world-outline.json`, Natural Earth 110m outer rings, Douglas-Peuckered to a quarter of a
degree, rounded to one decimal, with rings smaller than 1.2 degrees left out - 2,419 vertices in
113 rings. `scripts/make_world_outline.py` builds it from a downloaded source and writes its own
provenance into the output; the result is committed, so nothing fetches it, nothing builds it during
install, and the file explains itself when opened.

The resolution is the decision most likely to be criticised, so it is made out loud. At 1:110m a
coastline says "continent", and the map is about a thousand pixels wide: a city's shore is not in
this data, and a coastal town can therefore be drawn in the water. That is accepted because of what
the map is for. It exists to answer "which side of the world are these two places on, and what does
the route between them look like" - the pair that crosses the antimeridian, the pair that goes over
the pole, the pair that turns out to be two towns with the same name ninety metres apart. Not "is
this place on the coast". Making the coastline fine enough for the second question would mean a
mapping library, tiles, and a network, in exchange for a question nobody asked.

Drawing it in the browser rather than in Python is the other half. The server already has the
coordinates - every `results` event carries both places' latitudes and longitudes - and an image
rendered server-side would be a second rendering pipeline, on the machine with the least screen real
estate, for something a browser draws for free. Nothing map-related is added on the Python side: no
shapely, no geopandas, no pyproj, no tile server. The generator script uses the standard library
only, which keeps it out of the package's dependency list along with the map itself.

## Consequences

The front end has one exemption from its own rule, and it is narrow and named. ADR 0008's ban on
`Math.*` still covers `app.js` and `results.js`; `map.js` may use trigonometry, because a great
circle is not drawable without it, and may not have an Earth radius, a `distance_km`, or any other
way of turning two coordinates into a length - so the kilometres a reader sees can only have come
from the API. `tests/browser_support.py` holds the two lists (`BANNED` and `MAP_BANNED`) and
`tests/test_browser_map.py` says which file each applies to. The one thing `map.js` is allowed to
sort is the pieces of one arc, which a test pins by insisting `lines` is the only sort target in the
file.

Plate carree with one world window means an arc that crosses the antimeridian is cut, and the cut is
drawn as two pieces that meet at the edges at the same height. That is also where the interesting
bugs are, so they are tested rather than trusted: a pair whose route leaves the right edge, a pair
almost exactly antipodal - where the usual formula is undefined and the route is decided by naming a
pole - and a pair that is one place twice.

The map is checked without a browser. Its arcs are sampled and asserted against arithmetic a reader
can repeat, and its painter is handed a canvas context that writes down the instructions it was
given, so marker positions, which arc is bold, what order the faint arcs are laid down in, and the
fact that an empty list draws no arc are all assertions about recorded numbers. Where Node is not
installed those tests skip and say so, as they do for the rest of the front end.

Coastline geometry is checked as geometry: known inland cities on land, a patch of open ocean in the
water, rings closed, coordinates inside the world, and a cap on bytes and vertices so the asset
cannot quietly grow into something a person has to wait for. Coastal cities are deliberately not
asserted either way, and the test says so instead of pretending the data is finer than it is.

The palette is in `map.js`, not in the stylesheet, and the map is light in dark mode. This is the
first asset with a colour of its own; if a second one appears, the colours belong in one place that
both can read.

What this forecloses: no pan, no zoom, no labels, no basemap styles, no satellite imagery, no
country borders, no projection choice. The map is a picture of one world at the width of the page,
whose whole job is to make an arc legible. If a later round wants interaction, tiles or a library,
that supersedes this decision rather than drifting into it - and should say what it does about the
machine with no network.
