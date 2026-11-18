"""Serving the browser adapter from the search server itself.

One process, one port, one thing to remember to start: the page, its scripts, its stylesheet and
the coastline it draws are handed out by the same server that answers the search they ask for.
That is also why there is no map library, no bundler and nothing fetched from another machine —
`tests/test_browser_app.py` refuses a page that loads anything over a URL, because a tool
whose answers come from one laptop's place database should not have its behaviour come from
somewhere else.

The names are listed here rather than served from a directory: the only names looked up are these,
so a request cannot walk out of the folder, and a missing asset is a refusal that says which one is
missing instead of a traceback from a file opener. The files are read per request so that editing
the page is a reload rather than a restart.

One of the names is data rather than code: `world-outline.json` is the coarse coastline the map puts
its arcs over. `scripts/make_world_outline.py` made it and the file records what from; no part of
the install needs the script, which is what keeps a map off the dependency list.

The front end is plain ES modules with no build step — see
`docs/adr/0008-the-front-end-is-plain-es-modules.md`.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

#: Where the page lives: inside the installed package, next to the API that serves it.
STATIC_DIRECTORY = Path(__file__).resolve().parent / "static"

#: The name of the page, and the media type of every file this server will hand out.
ASSETS: dict[str, str] = {
    "index.html": "text/html",
    "app.js": "text/javascript",
    "results.js": "text/javascript",
    "map.js": "text/javascript",
    "styles.css": "text/css",
    "world-outline.json": "application/json",
}

PAGE = "index.html"


def asset_response(name: str) -> Response | None:
    """The bytes of one named asset, or None if that is not something this server serves.

    An asset named but missing from the package is a `500`-free refusal: the page is part of
    the install, so a broken install should say so in words rather than half-load a page.
    """
    media_type = ASSETS.get(name)
    if media_type is None:
        return None
    path = STATIC_DIRECTORY / name
    if not path.is_file():
        return JSONResponse(
            {
                "error": "MissingAsset",
                "message": f"{name} is not in this installation's static directory ({STATIC_DIRECTORY})",
            },
            status_code=500,
        )
    # `text/...` gains an explicit `charset=utf-8` from Starlette: names arrive in UTF-8 and a
    # script or page served without a charset is a script or page whose accents a browser guesses.
    return Response(
        content=path.read_bytes(),
        media_type=media_type,
        # The page is read off disk on every request and never cached, so editing it is a
        # reload on the machine that edited it rather than a fight with a stale copy.
        headers={"Cache-Control": "no-store"},
    )


def mount_frontend(app: FastAPI) -> None:
    """Add the page and its assets to an API, so `distnsimilarity-serve` serves both.

    They are kept out of the OpenAPI schema: the schema describes the search, which is a
    machine contract with parameters to check, and a page has none of that.
    """

    @app.get("/", include_in_schema=False)
    async def page() -> Response:
        """Everywhere mode: choose a threshold, a dataset and a direction, and watch the list."""
        response = asset_response(PAGE)
        assert response is not None, f"{PAGE} is served by name and is not a directory entry"
        return response

    @app.get("/static/{name}", include_in_schema=False)
    async def asset(name: str) -> Response:
        response = asset_response(name)
        if response is not None:
            return response
        return JSONResponse(
            {
                "error": "UnknownAsset",
                "message": f"this server serves {sorted(ASSETS)}, not {name}",
            },
            status_code=404,
        )
