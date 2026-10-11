"""Explicit synthetic hand geometry for the camera-free demonstration only."""
from types import SimpleNamespace
from .tracking import Hand


def demo_hand(x, y, label="Right"):
    # Landmark 8 is exactly (x,y). Wrist and palm remain separate positions.
    offsets=[(.035,.20),(-.01,.17),(-.04,.13),(-.065,.11),(-.09,.10),
             (0,.12),(0,.075),(0,.035),(0,0),
             (.05,.115),(.05,.07),(.05,.04),(.05,.015),
             (.09,.13),(.095,.09),(.095,.065),(.095,.045),
             (.125,.15),(.135,.12),(.14,.10),(.145,.08)]
    points=[SimpleNamespace(x=x+dx,y=y+dy) for dx,dy in offsets]
    return Hand(points[0].x,points[0].y,label,1,points)
