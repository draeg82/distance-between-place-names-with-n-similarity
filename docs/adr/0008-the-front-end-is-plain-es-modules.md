# The front end is plain ES modules, served by the search server

The browser app is the third adapter over the seam (ADR 0002, ADR 0007), and it had three
plausible shapes: a framework and a bundler with a `dist/` built before the page can be
served, a static host beside the API, or four files handed out by the server that already
answers the search they ask for. It is the last: `distnsimilarity-serve` serves the page at
`/` and its three assets at `/static/{name}`, and the front end is `index.html`, `app.js`,
`results.js` and `styles.css` written as they are shipped. Nothing is transformed, bundled or
minified, nothing is installed with a package manager, and nothing is fetched from another
machine - no CDN for a map library, no webfont, no analytics.

The reason is the promise the whole tool is built on: one clone, `uv run`, and a search on
loopback. A build step would make the deployed page something other than the files in the
repository, which means a page that is right after a build and wrong in the repo, and a review
of the repo that proves nothing about what a person sees. It would also make the suite a lie:
`uv run pytest` is the whole suite today, and a front end that had to be compiled before it
could be served is a front end the suite cannot serve. The same reasoning made the server bind
`127.0.0.1` by default; serving the page from the same origin keeps that a single decision -
somebody who binds `0.0.0.0` publishes the page and the API together, in one act, rather than
discovering a second surface that was quietly open.

The front end is split in two on purpose. `results.js` is pure functions from an event to words
and table cells, and `app.js` is the adapter: it asks, reads the stream a line at a time, hands
each line to the presentation module, and puts what comes back into the page. That split is
what lets the words and the marking of a name be run outside a browser, which is how "the
differing characters are highlighted" and "a search that found nothing says so with the
parameters it used" became assertions about output rather than about hope. Node is a developer's
tool for that, not a runtime dependency: where it is not installed those tests skip and say so,
and everything else still runs.

## Consequences

The page holds no domain logic, and the suite refuses the alternative: no `Math.*` that could
be a distance, no spelling of the great-circle formula, no comparing two names in JavaScript,
no sorting or reversing the list the API ranked (`TestTheFrontEndAddsNoDomainLogic`). A map
(#8) and a from-a-place search (#9) inherit that rule: they get their geometry and their
candidates from the API too, and an arc is drawn from coordinates the server sent.

Every field the page reads is checked against a real estimate, dataset listing and search
stream - including the `error` event, which only happens when a stored row is not a place, so
the fixture breaks one. A page that starts reading a field the API stopped sending fails in the
suite rather than in front of a search.

Static files live inside the package, so an installed wheel carries them and one process needs
starting. The four names are the only ones the server will look up, so a request cannot walk out
of the folder, and an asset that is named but missing is a refusal naming the directory it
should have been in: a broken install says which half of it is broken. Assets are read per
request and served `no-store`, so editing the page is a reload on the machine that edited it.

The page is excluded from the OpenAPI schema, which describes the search: a machine contract has
parameters to check and a page has none.

What this forecloses is worth naming, because it will look like an omission: no component
model, no reactive state, no JSX, no code splitting, no minification. The app is a form, a
stream reader and a table, which is what the streaming contract needs and less than what a
framework would want. If a later round needs a map library or a build, that is a new decision
and should supersede this one rather than quietly drift.
