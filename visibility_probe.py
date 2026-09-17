"""A disposable, centered visual confirmation window for the launcher."""

from __future__ import annotations

import tkinter as tk


def main() -> None:
    root = tk.Tk()
    root.title("Grim Dawn 修改器启动确认")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    width, height = 300, 180
    root.update_idletasks()
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    x = (screen_width - width) // 2
    y = (screen_height - height) // 2
    root.geometry(f"{width}x{height}+{x}+{y}")

    tk.Label(root, text="OK!", font=("Segoe UI", 36, "bold")).pack(
        expand=True, fill="both"
    )
    root.mainloop()


if __name__ == "__main__":
    main()
