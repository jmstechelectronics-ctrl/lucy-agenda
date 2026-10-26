#!/usr/bin/env python3
"""Credential handoff window for the one-time TickTick OAuth setup."""

from __future__ import annotations

import subprocess
import threading
import tkinter as tk
from pathlib import Path


HERE = Path(__file__).resolve().parent


class Setup(tk.Tk):
    def __init__(self) -> None:
        super().__init__(className="TickTickWidgetSetup")
        self.title("TickTick Widget Setup")
        self.geometry("560x350+680+330")
        self.configure(bg="#15171c")
        self.resizable(False, False)
        self.attributes("-topmost", True)

        tk.Label(self, text="Connect Lucy’s TickTick widget", bg="#15171c", fg="#f4f5f7",
                 font=("Ubuntu", 18, "bold")).pack(anchor="w", padx=28, pady=(24, 5))
        tk.Label(self, text="The access token will be stored in GNOME Keyring. Your TickTick password is not used.",
                 bg="#15171c", fg="#a9adb7", font=("Ubuntu", 10), wraplength=500,
                 justify="left").pack(anchor="w", padx=28, pady=(0, 18))

        form = tk.Frame(self, bg="#15171c")
        form.pack(fill="x", padx=28)
        tk.Label(form, text="Client ID", bg="#15171c", fg="#f4f5f7",
                 font=("Ubuntu", 10, "bold")).pack(anchor="w")
        self.client_id = tk.Entry(form, bg="#262a33", fg="#f4f5f7", insertbackground="white",
                                  relief="flat", font=("Ubuntu Mono", 11))
        self.client_id.pack(fill="x", ipady=8, pady=(4, 12))
        tk.Label(form, text="Client Secret", bg="#15171c", fg="#f4f5f7",
                 font=("Ubuntu", 10, "bold")).pack(anchor="w")
        self.client_secret = tk.Entry(form, bg="#262a33", fg="#f4f5f7", insertbackground="white",
                                      show="•", relief="flat", font=("Ubuntu Mono", 11))
        self.client_secret.pack(fill="x", ipady=8, pady=(4, 12))

        self.status = tk.Label(self, text="OAuth callback: http://127.0.0.1:8765/callback",
                               bg="#15171c", fg="#66a3ff", font=("Ubuntu", 9))
        self.status.pack(anchor="w", padx=28)
        self.authorize = tk.Button(self, text="Authorize TickTick", command=self.start,
                                   bg="#477ff2", fg="white", activebackground="#5f91f5",
                                   activeforeground="white", relief="flat", bd=0,
                                   font=("Ubuntu", 11, "bold"), cursor="hand2", padx=18, pady=9)
        self.authorize.pack(anchor="e", padx=28, pady=18)

    def start(self) -> None:
        client_id = self.client_id.get().strip()
        client_secret = self.client_secret.get().strip()
        if not client_id or not client_secret:
            self.status.configure(text="Paste both values from TickTick Developer.", fg="#ff6b6b")
            return
        self.authorize.configure(state="disabled")
        self.status.configure(text="Waiting for TickTick authorization in Chrome…", fg="#66a3ff")
        threading.Thread(target=self.worker, args=(client_id, client_secret), daemon=True).start()

    def worker(self, client_id: str, client_secret: str) -> None:
        process = subprocess.run(
            ["/usr/bin/python3", str(HERE / "oauth_setup.py")],
            input=client_id + "\n" + client_secret + "\n",
            text=True, capture_output=True,
        )
        if process.returncode == 0:
            self.after(0, self.success)
        else:
            message = process.stderr.strip().splitlines()[-1] if process.stderr.strip() else "Authorization failed"
            self.after(0, lambda: self.failure(message))

    def success(self) -> None:
        self.client_secret.delete(0, "end")
        self.status.configure(text="Connected. Starting the widget…", fg="#65d39a")
        subprocess.run(["systemctl", "--user", "restart", "lucy-ticktick-widget.service"], check=False)
        self.after(1500, self.destroy)

    def failure(self, message: str) -> None:
        self.status.configure(text=message, fg="#ff6b6b")
        self.authorize.configure(state="normal")


if __name__ == "__main__":
    Setup().mainloop()
