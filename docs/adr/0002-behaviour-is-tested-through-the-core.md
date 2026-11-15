# Behaviour is tested through the core, never through an adapter

The core is the single test seam: matching, distance, place reading and scanning are
importable and callable on their own, and the tests call them directly. The terminal, the
search API and the browser are adapters that present its answers, and the obvious route of
testing behaviour through the command line is deliberately not taken - the command line is
one adapter of several, and rules tested there tend to get re-implemented there.

## Consequences

An adapter is only ever tested for what belongs to it: argument parsing and file writing for
the terminal, the streaming contract for the API. Anything else is a sign that a rule has
leaked out of the core.
