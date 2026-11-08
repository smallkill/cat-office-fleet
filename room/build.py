"""Build the homepage: room/room-live-v7.tpl.html -> static/index.html
(the template must contain exactly one __FLEET_TOKEN__ placeholder; the server injects the token).
Run: python3 room/build.py
"""
import pathlib
root = pathlib.Path(__file__).resolve().parent.parent
t = (root / "room" / "room-live-v7.tpl.html").read_text(encoding="utf-8")
assert t.count("__FLEET_TOKEN__") == 1, "exactly one token placeholder required"
(root / "static" / "index.html").write_text(t, encoding="utf-8")
print("wrote static/index.html", len(t))
