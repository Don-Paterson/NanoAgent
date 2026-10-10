"""Test target window: logs clicks and entry text so the e2e test can check what the agent did."""
import json
import sys
import tkinter as tk

log_path = sys.argv[1]


def write(ev):
    with open(log_path, "a") as f:
        f.write(json.dumps(ev) + "\n")


root = tk.Tk()
root.title("Nano Target")
root.geometry("1600x1000+0+0")
root.configure(bg="#203040")
entry = tk.Entry(root, font=("DejaVu Sans", 16), width=30)
entry.place(x=400, y=300)
box = tk.Label(root, text="  TARGET  ", bg="orange", font=("DejaVu Sans", 20, "bold"))
box.place(x=900, y=600)
root.bind_all("<Button-1>", lambda e: write({"ev": "click", "x": e.x_root, "y": e.y_root}))
root.bind_all("<Button-3>", lambda e: write({"ev": "rclick", "x": e.x_root, "y": e.y_root}))
root.bind_all("<KeyRelease>", lambda e: write({"ev": "text", "value": entry.get()}))
root.after(200, lambda: write({"ev": "ready"}))
root.mainloop()
