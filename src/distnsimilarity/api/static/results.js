/**
 * The browser adapter's presentation half: search events in, words and table cells out.
 *
 * Every function here is pure, and that is the reason it is a module of its own rather than
 * lines inside `app.js`: Node can import it, so "the differing characters stand out", "the
 * price is said before the search runs" and "a search that found nothing says so with its
 * parameters" are assertions about returned text rather than about a browser nobody has
 * installed. `tests/test_browser_app.py` runs these functions directly; where Node is missing
 * those tests skip and everything else in the suite still runs.
 *
 * Nothing here decides anything the API has already decided. The best-so-far list arrives in
 * the order the core ranked it, a row arrives with its positions already worked out, and a
 * dataset choice arrives with its label already worded. This module formats, and never
 * measures, compares, sorts or counts: a second copy of a rule in the page is a second answer
 * to the same question, and the two start disagreeing about which pairs are pairs.
 */

const COUNTS = new Intl.NumberFormat("en-US");
const DISTANCES = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 3,
  maximumFractionDigits: 3,
});

/** A count the way a person reads it: 139850045 becomes 139,850,045. */
export function formatNumber(value) {
  if (value === null || value === undefined) {
    return "nothing";
  }
  return COUNTS.format(value);
}

/** A great-circle distance: three decimals, which is metres, and the same separators as a count. */
export function formatDistance(kilometres) {
  if (kilometres === null || kilometres === undefined) {
    return "no distance";
  }
  return DISTANCES.format(kilometres);
}

/**
 * A stretch of seconds, spoken. Nothing is promised to a decimal place the search itself does
 * not have: under a minute is said in seconds, over it in minutes and seconds.
 */
export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) {
    return null;
  }
  if (seconds < 1) {
    return "less than a second";
  }
  if (seconds < 60) {
    return `${trim(seconds.toFixed(1))} s`;
  }
  const whole = Math.round(seconds);
  const minutes = Math.floor(whole / 60);
  const left = whole % 60;
  return left === 0 ? `${minutes} min` : `${minutes} min ${left} s`;
}

function trim(text) {
  return text.endsWith(".0") ? text.slice(0, -2) : text;
}

/** How alike the two names are, said as a count of positions: "1 differing character". */
export function differenceWords(count) {
  const shown = formatNumber(count);
  return count === 1 ? `${shown} differing character` : `${shown} differing characters`;
}

/**
 * What a search is going to cost, in the words used to decide whether to run it.
 *
 * `candidate_pairs` is exact and is the number a person decides with; the seconds are only
 * quoted once something has been measured on this machine, and then only with the `basis` the
 * server gave, because a guessed time is a promise nobody made. Works for a `/v1/estimate`
 * answer and for a `started` event alike: both carry the same three things.
 */
export function costWords(event) {
  const estimate = event.estimate ?? {};
  // The caller's own spelling is only worth quoting when the server had to translate it: "5000"
  // and 5000 are the one choice, and saying it twice is noise where a price should be plain.
  const asked =
    event.requested_dataset && String(event.requested_dataset) !== String(event.dataset)
      ? ` (asked for as ${event.requested_dataset})`
      : "";
  // A floor of nothing is a fact about the search too: "every place in the database" and "floor
  // of 0" are the same search, and the second reads like a value somebody forgot to set.
  const floor = event.population_floor
    ? `at a population floor of ${formatNumber(event.population_floor)}`
    : "with no population floor";
  const scope = [
    `${event.dataset_label}${asked}`,
    floor,
    `over ${formatNumber(event.places)} places`,
  ].join(", ");
  // Which character-difference value is in effect is one of the two things being weighed here - the
  // other is the floor - and a price quoted for the wrong one is a price for a different search.
  const askedFor = event.n === undefined || event.n === null ? "" : `${differenceWords(event.n)}, `;
  if (estimate.seconds === null || estimate.seconds === undefined) {
    const basis = estimate.basis ?? "not measured on this machine yet";
    return `${scope}: ${askedFor}${formatNumber(event.candidate_pairs)} pairs to compare, and ${basis}, so no time to promise`;
  }
  const pace =
    estimate.comparisons_per_second === null || estimate.comparisons_per_second === undefined
      ? ""
      : ` (${formatNumber(Math.round(estimate.comparisons_per_second))} comparisons a second)`;
  return `${scope}: ${askedFor}${formatNumber(event.candidate_pairs)} pairs to compare, about ${formatDuration(estimate.seconds)}${pace} - ${estimate.basis}`;
}

/**
 * How far the scan has got, in comparisons, and how much of it is left.
 *
 * The remaining time is this search's own pace read forward, which says nothing at all until
 * the search is far enough in to have one; "no pace yet" is the answer, not a blank, because an
 * empty line next to a two-minute scan is exactly what a hung one looks like.
 */
export function progressWords(event) {
  const share =
    event.share_compared === null || event.share_compared === undefined
      ? null
      : Math.round(event.share_compared * 1000) / 10;
  const counted = `compared ${formatNumber(event.pairs_compared)} of ${formatNumber(event.candidate_pairs)} pairs (${share === null ? "0" : share}% of them) across ${formatNumber(event.places)} places`;
  const found = `${formatNumber(event.pairs)} pairs found, ${formatNumber(event.improvements)} improvements to the list`;
  const remaining = formatDuration(event.estimated_seconds_remaining);
  if (remaining === null) {
    return `${counted}. ${found}. no pace yet, so no idea how much is left`;
  }
  // The estimate is a guess, so it says "about" - unless the phrase is already a hedge, in which
  // case "about less than a second" is two of them stacked on top of each other.
  const left = remaining.startsWith("less than") ? remaining : `about ${remaining}`;
  return `${counted}. ${found}. ${left} left`;
}

/**
 * The two halves of one row of the best-so-far list.
 *
 * `parts` is the name in runs, each marked with whether the API said that position differs, and
 * it is the only way the page knows: the indexes come from the search, so the highlight can
 * never disagree with the pair it is drawn on. The distance and the character difference belong
 * to the pair rather than to either place, and are given to both halves so a row reads the same
 * whichever side is being looked at.
 */
export function rowCells(row) {
  const positions = row.differing_positions ?? [];
  const distance = formatDistance(row.distance_km);
  const difference = differenceWords(row.character_difference);
  return [
    cellsFor(row.first, positions, distance, difference),
    cellsFor(row.second, positions, distance, difference),
  ];
}

function cellsFor(place, positions, distance, difference) {
  const differing = new Set(positions);
  const parts = [];
  for (const [index, character] of Array.from(place.asciiname).entries()) {
    const mark = differing.has(index);
    const previous = parts[parts.length - 1];
    if (previous !== undefined && previous.differing === mark) {
      previous.text += character;
    } else {
      parts.push({ text: character, differing: mark });
    }
  }
  return {
    name: place.asciiname,
    display: place.display_name,
    country: place.country_code,
    population: formatNumber(place.population),
    coordinates: `${place.latitude.toFixed(4)}, ${place.longitude.toFixed(4)}`,
    geonameid: place.geonameid,
    distance,
    difference,
    parts,
  };
}

/**
 * The sentence for a search that compared everything and matched nothing.
 *
 * It has to name the parameters, because "no results" over every place in the database and "no
 * results" over 5,000-people towns are two different findings, and the person who chose one
 * needs to know which one came back empty.
 */
export function emptyWords(echo, done) {
  const parameters = [
    `exactly ${differenceWords(echo.n)}`,
    echo.dataset_label,
  ];
  if (echo.min_population !== null && echo.min_population !== undefined) {
    parameters.push(`a population floor of ${formatNumber(echo.min_population)}`);
  }
  parameters.push(`${echo.direction} distance first`);
  parameters.push(`a best-so-far list of ${formatNumber(echo.top_k)}`);
  const looked =
    done && done.pairs_compared !== undefined
      ? ` ${formatNumber(done.pairs_compared)} pairs compared${
          done.elapsed_seconds ? ` in ${formatDuration(done.elapsed_seconds)}` : ""
        }.`
      : "";
  return `No pair of places matched: ${parameters.join(", ")}.${looked}`;
}

/**
 * What a search that is no longer running did.
 *
 * A cancelled search says it was cancelled, and why: the list on screen is real but it is what
 * was found before the stop, and calling that "finished" would let a person believe they had
 * seen the whole answer.
 */
export function finishedWords(done) {
  const counted = `${formatNumber(done.pairs)} pairs found from ${formatNumber(
    done.pairs_compared,
  )} compared in ${formatDuration(done.elapsed_seconds)}`;
  if (done.cancelled) {
    const reason = done.cancel_reason ? ` (${done.cancel_reason})` : "";
    return `Search cancelled${reason}: ${counted}. The list is what it found before it stopped.`;
  }
  if (done.error) {
    const retry = done.retryable ? " This search can be run again." : "";
    return `Search failed: ${done.error}. ${counted}.${retry}`;
  }
  const completed = done.completed ? "" : " It did not report itself as complete.";
  return `Finished: ${counted}.${completed}`;
}

/**
 * What the list on screen currently is: a window onto how many pairs, and how often it tightened.
 *
 * `seen_pairs` is how many pairs the list is a window onto and `improvements` counts the pairs
 * that displaced something off it, which is the difference between "the list is still moving" and
 * "the list has settled" - the thing a person watching a long scan is trying to judge.
 */
export function listWords(event) {
  const found = `${formatNumber(event.seen_pairs)} ${plural(event.seen_pairs, "pair", "pairs")} found`;
  const tightened = `the list improved ${formatNumber(event.improvements)} ${plural(event.improvements, "time", "times")}`;
  return `Searching - ${found}, ${tightened}`;
}

function plural(count, one, many) {
  return count === 1 ? one : many;
}

/**
 * What the page is doing right now, naming the search that is doing it.
 *
 * The `search_id` belongs in the sentence because it is how the same search is asked about from
 * somewhere else - another tab, the cancellation endpoint, a log line - and because a status that
 * says "working" without saying on what is barely a status.
 */
export function searchingWords(event) {
  const parts = [
    `Searching ${event.dataset_label}`,
    `for names differing by ${formatNumber(event.n)}`,
    `${event.direction} distance first`,
  ];
  if (event.search_id) {
    parts.push(`(search ${event.search_id})`);
  }
  return parts.join(" ");
}

/**
 * Which search this is, in as few words as still identify it.
 *
 * The character difference and the dataset are what the reader chose; the `search_id` is what
 * somebody else would need to talk about the same search. When the server has not answered with a
 * `started` yet there is nothing to name, and the page says so rather than inventing a description.
 */
function whichWords(started) {
  const which = started ?? {};
  const parts = [];
  if (which.n !== undefined && which.n !== null) {
    parts.push(differenceWords(which.n));
  }
  if (which.dataset_label) {
    parts.push(`over ${which.dataset_label}`);
  }
  if (which.search_id) {
    parts.push(`(search ${which.search_id})`);
  }
  return parts.length === 0 ? "the search this page started, which the server has not described yet" : parts.join(" ");
}

/**
 * How far a search had got, as far as this page knows.
 *
 * Only a `progress` event carries `candidate_pairs`, so a list on its own can say what has been
 * compared without claiming to know what it was out of - which is the truth rather than a rounding
 * of it, and is the case for a search stopped in its first second.
 */
function asFarAsWords(stats) {
  if (!stats || stats.pairs_compared === undefined || stats.pairs_compared === null) {
    return "nothing compared yet";
  }
  const outOf =
    stats.candidate_pairs === undefined || stats.candidate_pairs === null
      ? ""
      : ` of ${formatNumber(stats.candidate_pairs)}`;
  const share =
    stats.share_compared === undefined || stats.share_compared === null
      ? ""
      : ` (${Math.round(stats.share_compared * 1000) / 10}% of them)`;
  const took = formatDuration(stats.elapsed_seconds);
  return `has compared ${formatNumber(stats.pairs_compared)}${outOf} pairs${share}${
    took === null ? "" : `, ${took} in`
  }`;
}

/**
 * Why a second search was not started, and what to do about it.
 *
 * One search runs at a time on a machine, which the server enforces and the page has to remember
 * for itself: the browser could always have started a second stream by hanging up on the first, and
 * used to. The sentence names the running search, says there is no queue to be in, and points at the
 * control that ends it - because a refusal without a way out is a page that looks broken.
 */
export function runningWords(started, latest) {
  return `A search is already running here: ${whichWords(started)}, which ${asFarAsWords(latest)}. One search runs at a time on this machine, so there is nothing to queue behind: press Stop to hang up on it, then search again.`;
}

/**
 * What a stopped search leaves behind.
 *
 * The list stays, in the order the search left it, because reading what has been found so far is
 * usually why a person stops early; the sentence says the scan is over so a still list is not read
 * as a hung one.
 */
export function stoppedWords(started, latest) {
  return `Stopped: ${whichWords(started)}, which ${asFarAsWords(latest)}. The list on screen is what it had found, in the order the search left it, and nothing more is coming.`;
}

/**
 * A refusal, in the server's own sentence.
 *
 * The page adds no diagnosis of its own: the message from the API already names the search in
 * the way, or the dataset that does not exist, or the import to run. The status code is kept in
 * the sentence because it is the part a person pastes to somebody else.
 */
export function refusalWords(status, payload) {
  const message =
    payload && payload.message
      ? payload.message
      : `the server answered ${status} without a sentence`;
  return `${status}: ${message}`;
}

/**
 * Read one chunk of a search stream, carrying the unfinished tail in and out.
 *
 * `pending` is what the previous read left half-written (empty string for the first read) and
 * `rest` is what this one leaves; the caller keeps it and hands it back with the next chunk. A
 * record split across two network reads is one record, and the caller cannot get that wrong by
 * accident: there is exactly one answer per read, so there is no second element to pick by mistake
 * and quietly throw the read away. The list can only assemble during the scan if the client reads
 * as the bytes arrive rather than waiting for the body to end, and this is where that happens.
 */
export function parseStream(pending, chunk) {
  const lines = `${pending ?? ""}${chunk ?? ""}`.split("\n");
  const rest = lines.pop() ?? "";
  const events = [];
  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed !== "") {
      events.push(JSON.parse(trimmed));
    }
  }
  return { events, rest };
}

/**
 * The dataset dropdown, in the server's words and the server's order.
 *
 * A dataset is a population floor, and only the choices this database can honour are listed, so
 * a dropdown built from a file's name would offer searches the server would refuse. The
 * whole-database choice sends no `dataset` at all, which is what "every place in the database"
 * means on the wire.
 */
export function datasetChoices(listing) {
  const choices = listing.datasets ?? [];
  return choices.map((choice) => ({
    value: choice.threshold === null || choice.threshold === undefined ? "" : String(choice.threshold),
    label: choice.label,
    detail: `${formatNumber(choice.places)} places, ${formatNumber(choice.candidate_pairs)} pairs`,
  }));
}

/**
 * Which places this server searches, said before anything is searched.
 *
 * An empty place database is not an empty dropdown: it is the import command to run, named with
 * the path that is empty, because the alternative is a person watching a spinner over nothing.
 */
export function databaseWords(listing) {
  const database = listing.database ?? {};
  if (!database.imported) {
    const where = database.present ? " holds no places" : " does not exist";
    return `The place database at ${database.path}${where}. Run distnsimilarity-import to build it, then press search again.`;
  }
  return `${formatNumber(database.places)} places in ${database.path}, searched as one place database.`;
}

/**
 * The focused pair, named, with both coordinates, to sit beside the map.
 *
 * The coordinates are the API's own, printed to three decimal places: about three hundred metres
 * at the equator, which is finer than the coastline on the map next to it and far finer than a
 * place name means, so it says "here, in the town" rather than pretending to a survey. Both places
 * are given, because the finding is the pair and one end on its own is a fact about a town.
 */
function coordinatesOf(place) {
  return `${Number(place.latitude).toFixed(3)}, ${Number(place.longitude).toFixed(3)}`;
}

function namedPlace(place) {
  const country = place.country_code ? ` (${place.country_code})` : "";
  return `${place.display_name ?? place.asciiname}${country} ${coordinatesOf(place)}`;
}

export function focusWords(row) {
  if (!row) {
    return "";
  }
  return `${namedPlace(row.first)} - ${namedPlace(row.second)}`;
}

/**
 * One place out of a name lookup, written so two places of the same name are not the same sentence.
 *
 * `Springfield` belongs to eleven places in a cities1000 cache, in several countries, and the distance
 * a sweep reports depends entirely on which one was meant - so the country, the population and the
 * coordinates are not decoration, they are what makes the choice possible. The `geonameid` is kept
 * as the option's value rather than written into its words: it is what goes back to the API, and a
 * reader does not choose a town by a number.
 */
export function placeChoice(place) {
  const country = place.country_code ? ` (${place.country_code})` : "";
  return `${place.display_name ?? place.asciiname}${country} - population ${formatNumber(
    place.population,
  )} at ${coordinatesOf(place)}`;
}

/**
 * What a lookup found, and what the reader still has to do with it.
 *
 * The count is the server's `match_count`, never a length this page worked out. The instruction is
 * part of the sentence even when there is only one match: this app does not choose a place for a
 * person (ADR 0011), and a page that quietly selected the single row would be answering a question
 * the reader had not finished asking.
 */
export function foundPlacesWords(lookup) {
  const found = lookup.match_count ?? 0;
  return `${formatNumber(found)} ${plural(found, "place", "places")} ${plural(
    found,
    "is",
    "are",
  )} called ${lookup.name}: choose which one you mean, then search.`;
}

/**
 * The name typed is not a name this database holds.
 *
 * This is a lookup that failed, and it has to read as one: the same page has another sentence for a
 * search that ran and matched nothing, and collapsing the two would leave a person re-running a
 * search that was never the problem. The typed name is quoted because it is the thing to go back and
 * check the spelling of.
 */
export function noPlaceNamedWords(lookup) {
  return `No place in this database is called \`${lookup.name}\`.`;
}

/**
 * Why the page will not start a sweep that has no place to start it from.
 *
 * The server would refuse this, but the page knows it already, so the reader gets a sentence about
 * the control that is missing rather than a status code - and the field they need to fill in is
 * already on screen above it.
 */
export function noPlaceChosenWords() {
  return "Choose which place you mean first: type a name and press Find places.";
}

/**
 * The place a sweep is measured from, named as fully as the lookup named it.
 *
 * Every distance on the map and in the list is from this one town, which is not visible from a row
 * of pairs; the echo is the API's own `origin` - the place behind the id that was sent, not the id.
 */
export function originWords(started) {
  return `Every distance is measured from ${placeChoice(started.origin)}.`;
}

/**
 * What each value of a swept range has found, in the order the server sent them.
 *
 * The list is drawn as it arrives: one entry per value of the range, including the values that found
 * nothing, because "2: nothing" is a finding and a tally that skipped it would make a wide range look
 * like a narrow one. Nothing here counts, sorts or drops anything - the counts come from the same
 * walk as the rows beside them, and a second count in the page is a second answer to the same
 * question. `excluded` is the server's word for the value the reader asked to leave out, so it is
 * said rather than hidden.
 */
export function tallyWords(event) {
  const counts = event.differences ?? [];
  if (counts.length === 0) {
    // Everywhere mode asks about one value, and the price line already names it; a sweep's range is
    // the only thing with a shape worth drawing.
    return "";
  }
  return counts
    .map((entry) => {
      const found = entry.pairs === 0 ? "nothing" : `${formatNumber(entry.pairs)} pairs`;
      const leftOut = entry.excluded ? " (identical names left out)" : "";
      return `at ${formatNumber(entry.character_difference)} differing characters: ${found}${leftOut}`;
    })
    .join(" - ");
}

/**
 * A sweep that ran over its whole range and matched nothing in it.
 *
 * It says which place was searched from and which range was swept, because a from-a-place sweep that
 * found nothing and a lookup that found no such place are two different dead ends - and the person
 * who has to tell them apart is the one deciding whether to retype the name or widen the range.
 */
export function emptySweepWords(echo, done) {
  const swept = [
    `${placeChoice(echo.origin)} swept from 0 up to ${differenceWords(limitOf(echo))}`,
    `over ${echo.dataset_label}`,
  ];
  if (echo.min_population !== null && echo.min_population !== undefined) {
    swept.push(`a population floor of ${formatNumber(echo.min_population)}`);
  }
  const compared =
    done && done.pairs_compared !== undefined
      ? ` ${formatNumber(done.pairs_compared)} ${plural(
          done.pairs_compared,
          "pair",
          "pairs",
        )} compared${done.elapsed_seconds ? ` in ${formatDuration(done.elapsed_seconds)}` : ""}.`
      : "";
  return `No pair of places matched: ${swept.join(", ")}.${compared}`;
}

/**
 * The top of the range this echo swept.
 *
 * `difference_limit` is what was asked for and may be absent; the number of entries in `differences`
 * is what was actually swept, and the server always sends every value of the range, so the last one
 * is the limit. Either way it is the server's number, not one this page worked out.
 */
function limitOf(echo) {
  const swept = echo.differences ?? [];
  const last = swept[swept.length - 1];
  if (last !== undefined && last.character_difference !== undefined) {
    return last.character_difference;
  }
  return echo.difference_limit;
}
