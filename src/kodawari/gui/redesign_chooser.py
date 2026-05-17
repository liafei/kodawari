"""GUI dialog for user's redesign choice decision.

Falls back to CLI prompt if tkinter is unavailable or no display is present.
"""

from __future__ import annotations

import os
from typing import NamedTuple


class DesignChoice(NamedTuple):
    """User's choice from the redesign dialog."""
    action: str  # "skip", "accept", "custom"
    option_index: int | None = None
    custom_text: str = ""


def show_redesign_dialog(options: list[dict]) -> DesignChoice:
    """Show redesign options to user and return their choice.

    Tries tkinter first; falls back to CLI prompt if unavailable.
    Set WORKFLOW_DECIDE_FORCE_CLI=1 to skip the GUI and use CLI prompt directly
    (useful for headless/CI runs and scripted testing).
    """
    if os.environ.get("WORKFLOW_DECIDE_FORCE_CLI", "").strip().lower() in {"1", "true", "yes", "on"}:
        return _show_cli_prompt(options)

    try:
        return _show_tkinter_dialog(options)
    except (ImportError, RuntimeError, OSError):
        # No tkinter or no display — fall back to CLI
        return _show_cli_prompt(options)


def _show_tkinter_dialog(options: list[dict]) -> DesignChoice:
    """Show tkinter modal dialog.

    Raises ImportError if tkinter unavailable, RuntimeError if no display.
    """
    import tkinter as tk
    from tkinter import simpledialog, messagebox

    root = tk.Tk()
    root.withdraw()  # Hide main window

    try:
        # Check if display is available (Unix-like systems)
        if root.winfo_screenwidth() <= 1:
            root.destroy()
            raise RuntimeError("No X11 display available")

        window = tk.Toplevel(root)
        window.title("Redesign Options")
        window.geometry("500x400")

        # Title and instructions
        title_label = tk.Label(window, text="Recovery Exhausted - Select Redesign Option", font=("Arial", 12, "bold"))
        title_label.pack(pady=10)

        instruction_label = tk.Label(
            window,
            text="Choose one:",
            justify=tk.LEFT,
            wraplength=450,
        )
        instruction_label.pack(pady=5)

        # Option buttons frame
        frame = tk.Frame(window)
        frame.pack(pady=10, fill=tk.BOTH, expand=True, padx=10)

        choice = [None]  # Mutable container for the choice

        def select_skip():
            choice[0] = DesignChoice(action="skip")
            window.destroy()
            root.destroy()

        def select_option(idx):
            def _inner():
                choice[0] = DesignChoice(action="accept", option_index=idx)
                window.destroy()
                root.destroy()
            return _inner

        # Skip button
        skip_btn = tk.Button(frame, text="Skip This Task", command=select_skip, width=40, justify=tk.LEFT)
        skip_btn.pack(pady=5)

        # Option buttons
        for idx, opt in enumerate(options):
            text = str(opt.get("title") or f"Option {idx + 1}")
            desc = str(opt.get("description") or "")
            if len(text) > 60:
                text = text[:60] + "..."
            if desc and len(desc) > 50:
                desc = desc[:50] + "..."
            full_text = text
            if desc:
                full_text += "\n" + desc
            btn = tk.Button(
                frame,
                text=full_text,
                command=select_option(idx),
                width=40,
                justify=tk.LEFT,
                anchor=tk.W,
            )
            btn.pack(pady=5)

        # Custom input
        custom_frame = tk.Frame(window)
        custom_frame.pack(pady=10, padx=10, fill=tk.X)
        custom_label = tk.Label(custom_frame, text="Or describe custom approach:")
        custom_label.pack(anchor=tk.W)
        custom_text = tk.Text(custom_frame, height=4, width=50)
        custom_text.pack(fill=tk.BOTH, expand=True)

        def select_custom():
            desc = custom_text.get("1.0", tk.END).strip()
            if not desc:
                messagebox.showwarning("Input Required", "Please enter a custom description")
                return
            choice[0] = DesignChoice(action="custom", custom_text=desc)
            window.destroy()
            root.destroy()

        custom_btn = tk.Button(custom_frame, text="Submit Custom Approach", command=select_custom, width=40)
        custom_btn.pack(pady=5)

        # Wait for user input
        window.transient(root)
        window.grab_set()
        root.wait_window(window)

        if choice[0] is None:
            # User closed dialog without selecting
            choice[0] = DesignChoice(action="skip")

        return choice[0]

    finally:
        try:
            root.destroy()
        except Exception:
            pass


def _show_cli_prompt(options: list[dict]) -> DesignChoice:
    """Show CLI-based prompt when no GUI is available."""
    print("\n" + "=" * 60)
    print("RECOVERY EXHAUSTED — SELECT REDESIGN OPTION")
    print("=" * 60)

    for idx, opt in enumerate(options):
        title = str(opt.get("title") or f"Option {idx + 1}")
        desc = str(opt.get("description") or "")
        print(f"\n{idx + 1}. {title}")
        if desc:
            for line in desc.split("\n"):
                print(f"   {line}")

    print("\n0. Skip this task (continue with next task)")
    print("c. Custom approach (enter description)")
    print("-" * 60)

    while True:
        choice = input("Enter choice (0-{}, c): ".format(len(options))).strip().lower()

        if choice == "0":
            return DesignChoice(action="skip")
        elif choice == "c":
            desc = input("Describe your custom approach: ").strip()
            if desc:
                return DesignChoice(action="custom", custom_text=desc)
            else:
                print("Description cannot be empty.")
                continue
        else:
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(options):
                    return DesignChoice(action="accept", option_index=idx)
                else:
                    print(f"Please enter a valid option (0-{len(options)}, or c).")
                    continue
            except ValueError:
                print(f"Invalid input. Please enter 0-{len(options)}, or c.")
                continue


__all__ = ["DesignChoice", "show_redesign_dialog"]
