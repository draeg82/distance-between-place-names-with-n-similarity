/**
 * The browser adapter: the DOM half, which does nothing but ask for a search and draw it.
 *
 * Three requests, all to this server: `/v1/datasets` for the dataset choices, `/v1/estimate`
 * for the price, `/v1/search` for the search itself. The estimate is asked for first and shown
 * before the stream opens, because a search over every place in the database is a decision
 * rather than a surprise. The stream is then read as it arrives - `started`, `progress`,
 * `results`, `done`, `error` - and every line is handed to the presentation module, which is
 * where the words come from.
 *
 * Nothing is sorted, filtered, measured or compared here, and the page is never reloaded to
 * start, stop or search again: hanging up the connection is how this API is told to stop, so
 * `AbortController` is the stop control. One search runs at a time, so pressing Search while one is
 * running is refused in words - what is running, how far it has got, and that Stop is the way out -
 * rather than quietly cancelling the search the reader was watching.
 *
 * The map is drawn from the same `results` events as the table, on requestAnimationFrame, and it
 * is given the finds in the order they arrived: which pair is bold is the API's ranking, and which
 * row is picked is the reader's. Drawing happens between reads rather than during one, so a busy
 * scan cannot make the list slow, and a person who has asked for their pages not to move gets the
 * arcs already drawn instead of drawing themselves in.
 */

import {
  costWords,
  databaseWords,
  datasetChoices,
  emptyWords,
  finishedWords,
  focusWords,
  listWords,
  parseStream,
  progressWords,
  refusalWords,
  rowCells,
  runningWords,
  searchingWords,
  stoppedWords,
} from "./results.js";
import { landPaths, loadOutline, paint, sceneOf } from "./map.js";

const DATASETS_ENDPOINT = "/v1/datasets";
const ESTIMATE_ENDPOINT = "/v1/estimate";
const SEARCH_ENDPOINT = "/v1/search";

/** How long the focused arc takes to draw itself in. Long enough to follow, short enough to ignore. */
const ARC_SECONDS = 0.9;

/**
 * What the caption says when there is no pair to name, which is not one thing but three.
 *
 * A page that has not searched, a search that found nothing, and a list to pick from are different
 * states, and the difference matters most in the middle one: a reader who has just watched a
 * two-minute scan finish with nothing on the map should be told that, not shown a hint.
 */
const NOTHING_SEARCHED_YET = "No pair to draw yet: press search and the leading find appears as the bold arc.";
const NOTHING_TO_DRAW = "No pair to draw: this search found nothing to put on the map.";
const PICK_A_ROW = "The bold arc is the leading find. Press a row to move it.";

// Filled in by `wireUpPage()`, which runs only where there is a page. Importing this module from
// Node - which is how the marking of a name is checked - must not need a document.
let form = null;
let datasetMenu = null;
let databaseLine = null;
let estimateLine = null;
let statusText = null;
let workingIndicator = null;
let progressLine = null;
let messageLine = null;
let resultsTable = null;
let resultRows = null;
let startButton = null;
let stopButton = null;
let mapCanvas = null;
let mapContext = null;
let arcCaption = null;

/** The search whose stream this page is still reading, if any, and the `started` event it answered with. */
let running = null;
let echo = null;

/**
 * The newest thing this server has said about the running search.
 *
 * Kept for the sentence that refuses a second search: a refusal is only useful if it describes the
 * search in the way - how far it has got, and how long it has been taking - so it needs the same
 * numbers the status line is living off rather than another look at the server.
 */
let latest = null;

/**
 * The price currently on screen and the parameters it was quoted for.
 *
 * `costFor` is what makes the sentence safe: it is the parameter string the server answered, so
 * the page can tell "the price of the search about to run" from "the price of the one before it",
 * and can leave a reply on the floor when a newer change has overtaken it.
 */
let cost = null;
let costFor = "";

/**
 * The parameters a price is being asked for right now, and that request.
 *
 * Two of them, because a person typing a number fires an event per keystroke: `costAsked` stops the
 * same count being asked for twice, and `costPending` lets a pressed Search wait for the price that
 * is already on its way instead of opening a stream one frame before the number appears.
 */
let costAsked = "";
let costPending = null;

/** What the map is showing: the running list, which row of it is bold, and how far that arc is drawn. */
let finds = [];
let focus = 0;
let coast = [];
let sharing = 1;
let frame = null;

/** A refusal the server already explained: the sentence is its own, so nothing is added to it. */
class SearchRefusal extends Error {}

function setStatus(words) {
  statusText.textContent = words;
}

function setMessage(words) {
  messageLine.textContent = words;
}

/** The working indicator is the difference between a slow search and a hung one, so it is explicit. */
function setWorking(searching) {
  workingIndicator.hidden = !searching;
  stopButton.hidden = !searching;
  // The search control goes disabled rather than being relabelled "Restart": one search runs at a
  // time, so a press now would mean throwing away the search being watched, and a button should not
  // invite that. Stop is the way out, and it is the control that appears instead.
  //
  // Disabling it is not the whole refusal, and cannot be: Enter in a text field submits a form
  // whether or not the button can be pressed, so `start` says the refusal in words as well.
  startButton.disabled = searching;
  document.body.classList.toggle("searching", searching);
}

/** The parameters as sent: empty fields are left out rather than sent as a blank population floor. */
function parametersToSend() {
  const sent = new URLSearchParams();
  for (const [name, field] of new FormData(form)) {
    const text = String(field).trim();
    if (text !== "") {
      sent.set(name, text);
    }
  }
  return sent;
}

async function fetchJSON(url) {
  const response = await fetch(url);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new SearchRefusal(refusalWords(response.status, payload));
  }
  return response.json();
}

/**
 * Fill the dataset dropdown from the server.
 *
 * The choices, their wording and their costs all come from `/v1/datasets`, which lists only what
 * this place database can actually be searched as; a database with nothing in it is reported as
 * the import to run, on the line above the form, rather than as a dropdown with one dead option.
 */
async function fillDatasetChoices() {
  try {
    const listing = await fetchJSON(DATASETS_ENDPOINT);
    databaseLine.textContent = databaseWords(listing);
    const choices = datasetChoices(listing);
    datasetMenu.replaceChildren(
      ...choices.map((choice) => {
        const option = document.createElement("option");
        option.value = choice.value;
        option.textContent = `${choice.label} - ${choice.detail}`;
        return option;
      }),
    );
    datasetMenu.disabled = choices.length === 0;
  } catch (error) {
    datasetMenu.disabled = true;
    databaseLine.textContent = messageFor(error);
  }
}

function messageFor(error) {
  if (error instanceof SearchRefusal) {
    return error.message;
  }
  return `Could not reach this server: ${error}`;
}

/**
 * Ask this server what the parameters on screen would cost, and put the answer where the decision
 * is made.
 *
 * The estimate is the one number a person can decide with before a two-minute scan, so it is asked
 * for as the parameters are chosen rather than only when Search is pressed: by the time the button
 * is pressed the price should already have been read. A reply is dropped if the parameters have
 * moved on while it was in flight, because a price for the wrong search is worse than none.
 *
 * The count behind `candidate_pairs` is not free, so a parameter string that already has a price on
 * screen is not asked about twice.
 */
async function quoteCost(sent) {
  try {
    const priced = await fetchJSON(`${ESTIMATE_ENDPOINT}?${sent}`);
    if (sent !== costAsked) {
      return;
    }
    cost = priced;
    costFor = sent;
    estimateLine.textContent = costWords(priced);
  } catch (error) {
    if (sent !== costAsked) {
      return;
    }
    cost = null;
    costFor = "";
    estimateLine.textContent = `No price to show: ${messageFor(error)}`;
  } finally {
    if (sent === costAsked) {
      costAsked = "";
      costPending = null;
    }
  }
}

/**
 * Make sure the price of these exact parameters is the sentence on screen.
 *
 * The caller names the parameters being priced rather than letting this read the form, because the
 * form is editable while a search runs: the string a search was started with is the one its price
 * has to be, even if a keystroke lands while the answer is on its way back.
 */
async function showCost(sent) {
  if (cost !== null && costFor === sent) {
    estimateLine.textContent = costWords(cost);
    return;
  }
  if (costPending !== null && costAsked === sent) {
    await costPending;
    return;
  }
  costAsked = sent;
  estimateLine.textContent = "Asking this server what that search would cost...";
  costPending = quoteCost(sent);
  await costPending;
}

/** The parameters are only worth costing again while the page is deciding what to search for. */
function parametersChanged() {
  if (running === null) {
    void showCost(String(parametersToSend()));
  }
}

function clearRows() {
  finds = [];
  focus = 0;
  resultRows.replaceChildren();
  resultsTable.hidden = true;
  showCaption();
  drawMap();
}

/**
 * Paint the map: the coastline, then the finds, then the focused pair's ends.
 *
 * The canvas is sized to the box the stylesheet gave it, at the two-to-one ratio a plate carree
 * map needs, and only when it has actually changed - resizing the window should redraw, and a
 * `results` event every few hundred milliseconds should not be seen by the compositor at all.
 */
function drawMap() {
  if (mapContext === null) {
    return;
  }
  const width = Math.max(1, mapCanvas.clientWidth || mapCanvas.width);
  const height = Math.round(width / 2);
  if (mapCanvas.width !== width || mapCanvas.height !== height) {
    mapCanvas.width = width;
    mapCanvas.height = height;
  }
  paint(mapContext, {
    width,
    height,
    paths: coast,
    scene: sceneOf({ rows: finds, focus, share: sharing }),
  });
}

/** An arc that draws itself in is motion, and a person who has asked for none gets the finished arc. */
function movingQuietly() {
  return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * Redraw with the focused arc drawing itself in, one frame at a time.
 *
 * Frames rather than a timer, so the drawing keeps step with the screen and stops when the tab is
 * hidden; and at most one animation runs, because a better list arriving mid-draw restarts it on
 * the new lead rather than starting a second arc alongside it.
 */
function drawInFocus() {
  if (mapContext === null) {
    return;
  }
  if (frame !== null) {
    cancelAnimationFrame(frame);
    frame = null;
  }
  if (movingQuietly() || typeof requestAnimationFrame !== "function") {
    sharing = 1;
    drawMap();
    return;
  }
  const started = performance.now();
  const step = (now) => {
    const elapsed = (now - started) / 1000;
    sharing = elapsed >= ARC_SECONDS ? 1 : elapsed / ARC_SECONDS;
    drawMap();
    frame = sharing < 1 ? requestAnimationFrame(step) : null;
  };
  sharing = 0;
  frame = requestAnimationFrame(step);
}

function showCaption() {
  const words = focusWords(finds[focus] ?? null);
  if (words !== "") {
    arcCaption.textContent = words;
    return;
  }
  if (finds.length > 0) {
    arcCaption.textContent = PICK_A_ROW;
    return;
  }
  // `echo` is the `started` event, so it says whether a search has been run at all on this page.
  arcCaption.textContent = echo === null ? NOTHING_SEARCHED_YET : NOTHING_TO_DRAW;
}

/**
 * Make one find the bold one.
 *
 * The row is picked by the reader; the map only moves the bold arc and prints that pair's
 * coordinates. Nothing here changes the order of the list, which is the search's to decide.
 */
function focusOn(index) {
  if (index < 0 || index >= finds.length) {
    return;
  }
  focus = index;
  markFocus();
  showCaption();
  drawInFocus();
}

function markFocus() {
  for (const [index, row] of Array.from(resultRows.children).entries()) {
    row.classList.toggle("selected", index === focus);
  }
}

/** The row a click or a keypress belongs to, whichever element inside it it landed on. */
function rowChosen(event) {
  const row = event.target?.closest?.("tr");
  if (row && resultRows.contains(row)) {
    focusOn(Number(row.dataset.index));
  }
}

/**
 * Draw one name, with the differing characters marked.
 *
 * The name is drawn in the runs the presentation module cut, each one marked from the API's own
 * differing positions, so the highlight is the search's finding and not the page's opinion. Text
 * goes in as text: a place name is data, never markup.
 *
 * The document is a parameter because this is checked with Node, which has no page to make an
 * element; the default keeps the caller above honest with one argument.
 */
export function nameCell(cell, doc = document) {
  const td = doc.createElement("td");
  td.className = "name";
  td.title = `${cell.display} (${cell.geonameid}) at ${cell.coordinates}`;
  for (const part of cell.parts) {
    const span = doc.createElement("span");
    span.className = part.differing ? "differing" : "shared";
    span.textContent = part.text;
    td.append(span);
  }
  return td;
}

function textCell(words, doc = document) {
  const td = doc.createElement("td");
  td.textContent = words;
  return td;
}

/**
 * Draw one row of the best-so-far list.
 *
 * The order of the cells is the order of the columns in the page, and nothing more belongs here:
 * the list arrives in the core's own order and is drawn as it arrives.
 */
export function rowFor(row, doc = document) {
  const [first, second] = rowCells(row);
  const tr = doc.createElement("tr");
  tr.dataset.pair = `${first.geonameid}/${second.geonameid}`;
  tr.tabIndex = 0;
  tr.append(
    nameCell(first, doc),
    textCell(first.country, doc),
    textCell(first.population, doc),
    nameCell(second, doc),
    textCell(second.country, doc),
    textCell(second.population, doc),
    textCell(first.distance, doc),
    textCell(first.difference, doc),
  );
  return tr;
}

/**
 * Replace the list rather than merge into it.
 *
 * Each `results` event carries the whole best-so-far list in the core's own order, so the page
 * has nothing to combine and no order to form a view about. `seen_pairs` says how many pairs the
 * list is a window onto, and `improvements` counts the ones the coalescing interval folded away.
 *
 * The map is redrawn from the same event as the table, and the new lead's arc draws itself in: a
 * list that improves eleven times is eleven arcs, which is the scan's progress made visible.
 */
function showList(event) {
  finds = event.results;
  focus = 0;
  // The index is where a find sits in the list, which is a fact about the table being filled now
  // rather than about the row, so it is written as the rows go in.
  resultRows.replaceChildren(
    ...finds.map((row, index) => {
      const tr = rowFor(row);
      tr.dataset.index = String(index);
      return tr;
    }),
  );
  resultsTable.hidden = finds.length === 0;
  setStatus(listWords(event));
  markFocus();
  showCaption();
  drawInFocus();
}

/** One line of the stream, routed by the kind of event it is. */
function handle(event) {
  if (event.type === "started") {
    echo = event;
    latest = event;
    estimateLine.textContent = costWords(event);
    setStatus(searchingWords(event));
    return;
  }
  if (event.type === "progress") {
    latest = event;
    progressLine.textContent = progressWords(event);
    return;
  }
  if (event.type === "results") {
    latest = event;
    showList(event);
    return;
  }
  if (event.type === "done") {
    const foundNothing = event.pairs === 0 && echo !== null;
    if (foundNothing) {
      // Nothing matched, so nothing is drawn either: an arc left over from the search before this
      // one would be a claim about a pair this search never found.
      clearRows();
    }
    setStatus(foundNothing ? "Search finished with nothing to show" : "Search finished");
    setMessage(foundNothing ? emptyWords(echo, event) : finishedWords(event));
    return;
  }
  if (event.type === "error") {
    setStatus("Search failed");
    setMessage(event.message);
  }
}

/**
 * Read a search stream while it happens.
 *
 * `parseStream` takes the unfinished tail of the previous read and hands back the tail of this one,
 * so a record split across two reads is still one record; each finished line is drawn before the
 * next read is awaited, which is what keeps the list assembling during the scan instead of after it.
 */
async function watch(url, signal) {
  const response = await fetch(url, { signal });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new SearchRefusal(refusalWords(response.status, payload));
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  for (;;) {
    const read = await reader.read();
    if (read.done) {
      return;
    }
    const lines = parseStream(pending, decoder.decode(read.value, { stream: true }));
    pending = lines.rest;
    for (const event of lines.events) {
      handle(event);
    }
  }
}

/**
 * Hang up on the search this page is reading, if there is one.
 *
 * Closing the connection is how this API is told to stop, and it is the only way it is told: the
 * server notices nobody is reading and cancels the scan rather than finishing it for nobody.
 */
function abandon() {
  if (running === null) {
    return false;
  }
  const controller = running;
  running = null;
  controller.abort();
  setWorking(false);
  return true;
}

/**
 * Start a search - or refuse, if one is already running here.
 *
 * This server runs one search at a time, and the page runs one search per tab, so a second press
 * cannot be honoured by queueing it. It used to be honoured by hanging up on the first search, which
 * looked like a restart and was a cancellation: the reader lost the list and the scan they had been
 * watching, with nothing saying so. A press now says why it did nothing and what to do about it.
 *
 * The page never reloads, so changing the parameters and searching again is one press after this one
 * has ended - and the scan that was running is finished or stopped by then, not orphaned.
 */
export async function start(event) {
  event?.preventDefault?.();
  if (running !== null) {
    setMessage(runningWords(echo, latest));
    return;
  }
  const controller = new AbortController();
  running = controller;

  echo = null;
  latest = null;
  // After the echo is cleared, so the map's caption says "no search yet" rather than claiming this
  // one has already found nothing.
  clearRows();
  setMessage("");
  progressLine.textContent = "";
  setWorking(true);
  const sent = String(parametersToSend());
  // The price is shown before the stream opens, but it is not asked for twice: the same sentence is
  // already on screen if the parameters have not moved since it was quoted. One parameter string
  // pays for this search and runs it, so a keystroke cannot split the two.
  await showCost(sent);

  try {
    await watch(`${SEARCH_ENDPOINT}?${sent}`, controller.signal);
    if (messageLine.textContent === "") {
      setStatus("The server closed the stream before saying it had finished");
    }
  } catch (error) {
    if (running !== controller) {
      return;
    }
    setStatus("Search not started");
    setMessage(messageFor(error));
    progressLine.textContent = "";
  } finally {
    if (running === controller) {
      running = null;
      setWorking(false);
      // The parameters may have moved while the search was running, and the price on screen is meant
      // to be the price of the search about to run, so it is worth another look now the page is idle.
      parametersChanged();
    }
  }
}

/**
 * Hang up on the running search, and say what is left on screen.
 *
 * The list is what had been found: stopping early is usually how a person reads a scan that is
 * already good enough, so emptying it would throw away the reason for stopping.
 */
export function stop(event) {
  event?.preventDefault?.();
  const found = echo;
  const asFarAsItGot = latest;
  if (abandon()) {
    setStatus("Stopped");
    setMessage(stoppedWords(found, asFarAsItGot));
  } else {
    // Nothing is running, so the status keeps saying what the last search did: overwriting "Search
    // finished" with a word about idleness would destroy the answer that is already there.
    setMessage("There is no search running here to stop.");
  }
}

/**
 * Load the coastline from this server.
 *
 * The outline is data, committed in the package, so there is no library to fetch and nothing from
 * another machine at run time. If it does not arrive, the map is still a map: a graticule and the
 * arcs, which are the finding. The arcs do not depend on the coastline, so a failure here is
 * reported and otherwise left alone rather than stopping a search.
 */
async function fillCoastline() {
  try {
    const outline = await loadOutline();
    coast = landPaths(outline.rings ?? []);
    drawMap();
  } catch (error) {
    console.warn("no coastline to draw, so the map is a graticule:", error);
  }
}

function wireUpPage() {
  form = document.getElementById("search-form");
  datasetMenu = document.getElementById("dataset");
  databaseLine = document.getElementById("database-line");
  estimateLine = document.getElementById("estimate-line");
  statusText = document.getElementById("status-text");
  workingIndicator = document.getElementById("working-indicator");
  progressLine = document.getElementById("progress-line");
  messageLine = document.getElementById("message-line");
  resultsTable = document.getElementById("results-table");
  resultRows = document.getElementById("result-rows");
  startButton = document.getElementById("start-button");
  stopButton = document.getElementById("stop-button");
  mapCanvas = document.getElementById("world-map");
  arcCaption = document.getElementById("arc-caption");
  mapContext = mapCanvas.getContext("2d");

  form.addEventListener("submit", (event) => {
    void start(event);
  });
  // One listener for the whole form: every field on it is a parameter, and a parameter is worth a
  // price the moment it is chosen. Both events, because typing is one and choosing from a menu is
  // the other; a repeat of a parameter string already priced is answered from `costFor`.
  form.addEventListener("input", parametersChanged);
  form.addEventListener("change", parametersChanged);
  stopButton.addEventListener("click", stop);

  // One pair of listeners for a table whose rows are replaced wholesale several times a second.
  resultRows.addEventListener("click", rowChosen);
  resultRows.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      rowChosen(event);
    }
  });
  window.addEventListener("resize", () => drawMap());

  showCaption();
  void fillCoastline();
  // The dataset choices first, because the dataset is one of the parameters the price is quoted
  // for: a price asked before the menu is filled is a price for the wrong search.
  void fillDatasetChoices().then(() => showCost(String(parametersToSend())));
}

// The page wires itself up when there is one. The functions above are what the tests call, and
// they are reached for an element only once this has run.
if (typeof document !== "undefined") {
  wireUpPage();
}
