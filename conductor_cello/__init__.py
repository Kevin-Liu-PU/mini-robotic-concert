"""Conductor-driven open-A bowing on the UR5e: the robot execution side.

One moveL per bow, constant speed inside a bow, everything re-planned at the
bow change from the Conductor clock. Motion is ur_rtde moveL through taught
points. See README.md.
"""

__version__ = "0.1.0"
