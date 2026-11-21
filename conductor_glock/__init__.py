"""Glockenspiel side of the Conductor-driven duet: eight solenoids follow the Conductor's beat.

The instrument, its firmware and its serial protocol come from the team's
Aria-Solenoid-Studio package (eight solenoids on a Purdue Proton RP2350B over
USB serial). This package only adds the part that was missing there: striking
the notes at the Conductor's beat positions instead of at a fixed tempo.
"""

__version__ = "0.1.0"
