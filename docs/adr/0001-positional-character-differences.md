# Positional character differences, not edit distance

Two names could be compared by how they line up position by position, or by the insertions
and deletions that would turn one into the other. We keep the original tool's positional
rule: equal lengths are a precondition, and the character difference is a count of positions
where the two names hold different characters. It is inherited behaviour that every later
result is verified against — `Aba`/`Abba` is one edit apart and matches at no threshold,
while `Xabc`/`abcX` is two edits apart and matches at four.

## Consequences

Names that "read similarly" but differ in length can never be a pair, however few edits
apart they are; that is the rule, not a gap to fix. Switching to approximate matching would
change every result the tool has ever produced.
