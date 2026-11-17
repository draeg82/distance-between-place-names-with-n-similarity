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
 * start, restart or stop a search: hanging up the connection is how this API is told to stop,
 * so `AbortController` is the stop control and pressing Search again is a restart.
 */

import {
  costWords,
  databaseWords,
  datasetChoices,
  emptyWords,
  finishedWords,
  listWords,
  parseStream,
  progressWords,
  refusalWords,
  rowCells,
  searchingWords,
} from "./results.js";

const DATASETS_ENDPOINT = "/v1/datasets";
const ESTIMATE_ENDPOINT = "/v1/estimate";
const SEARCH_ENDPOINT = "/v1/search";

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

/** The search whose stream this page is still reading, if any, and the `started` event it answered with. */
let running = null;
let echo = null;

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
  startButton.textContent = searching ? "Restart" : "Search";
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

function clearRows() {
  resultRows.replaceChildren();
  resultsTable.hidden = true;
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
 */
function showList(event) {
  resultRows.replaceChildren(...event.results.map((row) => rowFor(row)));
  resultsTable.hidden = event.results.length === 0;
  setStatus(listWords(event));
}

/** One line of the stream, routed by the kind of event it is. */
function handle(event) {
  if (event.type === "started") {
    echo = event;
    estimateLine.textContent = costWords(event);
    setStatus(searchingWords(event));
    return;
  }
  if (event.type === "progress") {
    progressLine.textContent = progressWords(event);
    return;
  }
  if (event.type === "results") {
    showList(event);
    return;
  }
  if (event.type === "done") {
    const foundNothing = event.pairs === 0 && echo !== null;
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
 * `parseStream` carries the unfinished tail between reads, so a record split across two chunks is
 * still one record; each finished line is drawn before the next read is awaited, which is what
 * keeps the list assembling during the scan instead of after it.
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
    const [chunk] = parseStream(pending, decoder.decode(read.value, { stream: true }));
    pending = chunk.rest;
    for (const event of chunk.events) {
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

async function start(event) {
  event.preventDefault();
  // One search runs per server, so pressing Search while one runs means hanging up on the old
  // one first: that stops its scan here rather than leaving it running and being refused.
  abandon();
  const controller = new AbortController();
  running = controller;

  clearRows();
  setMessage("");
  progressLine.textContent = "";
  echo = null;
  setWorking(true);
  const sent = parametersToSend();
  estimateLine.textContent = "Asking this server what that search would cost...";

  try {
    estimateLine.textContent = costWords(await fetchJSON(`${ESTIMATE_ENDPOINT}?${sent}`));
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
    }
  }
}

function stop(event) {
  event.preventDefault();
  if (abandon()) {
    setStatus("Stopped");
    setMessage("Search stopped. The list on screen is what it had found.");
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

  form.addEventListener("submit", (event) => {
    void start(event);
  });
  stopButton.addEventListener("click", stop);

  void fillDatasetChoices();
}

// The page wires itself up when there is one. The functions above are what the tests call, and
// they are reached for an element only once this has run.
if (typeof document !== "undefined") {
  wireUpPage();
}
