# conductor_glock

The glockenspiel side: eight solenoid strikers play the melody when the
conductor's beat position reaches each note.

The instrument and its firmware were built by teammates; this package adds the
part that takes the beat from the conductor. `songs/` holds the two note lists.

## What it does

- Listens where the conductor sends (loopback, port 8766 by default).
- With `--forward PORT`, passes every line on at once to the next receiver (the
  cello) and brings its answers back.
- Strikes a note when the beat position reaches it. Between two packets the
  position is extrapolated at the clock's own rate.
- Never plays a burst: if the clock moves past several notes at once, only the
  newest is played. Nothing is replayed when the clock moves back.
- On a stop, a closed connection or one second of silence, releases all strikers
  and ends.

## Commands

| Command | What it does |
| --- | --- |
| `python -m conductor_glock play --song arirang --dry-run` | Records what would be struck. Opens no hardware. |
| `python -m conductor_glock ports` | Lists serial ports. Opens nothing. |
| `python -m conductor_glock check --go` | Opens the controller, handshake, all strikers off. Strikes nothing. |
| `python -m conductor_glock play --song arirang --forward 8767 --go` | Plays the part and passes the clock on to the cello. |

Without `--go` the controller is never opened. `--lead-ms N` strikes N ms before
the beat, to allow for the travel of the hammer; that travel has not been
measured, so the default is 0. Each run writes a folder under
`conductor_glock/runs/` with every strike and a summary.
