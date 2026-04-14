"""TUI background actions — install, selfmod, evolve.

Extracted from tui.py. These were methods on CliveApp; now they take
`app` as an explicit parameter. The two methods decorated with
`@work(thread=True)` stay as thin delegates in tui.py (the textual
work decorator only applies to App methods).
"""

import subprocess
import threading

from textual.widgets import RichLog


def install_missing(app) -> None:
    if not app._missing_cmds:
        app.query_one("#output", RichLog).write(
            "[#6b7280]Nothing to install.[/]"
        )
        return

    brew_pkgs = []
    pip_pkgs = []
    for cmd in app._missing_cmds:
        install = cmd.get("install", "")
        if install.startswith("brew install "):
            brew_pkgs.append(install.split("brew install ", 1)[1])
        elif install.startswith("pip install "):
            pip_pkgs.append(install.split("pip install ", 1)[1])

    if not brew_pkgs and not pip_pkgs:
        app.query_one("#output", RichLog).write(
            "[#6b7280]No auto-installable packages.[/]"
        )
        return

    app._do_install(brew_pkgs, pip_pkgs)


def do_install(app, brew_pkgs: list, pip_pkgs: list) -> None:
    """Body for the @work-decorated _do_install method on CliveApp."""
    out = app.query_one("#output", RichLog)

    if brew_pkgs:
        argv = ["brew", "install"] + brew_pkgs
        app.call_from_thread(
            out.write, f"[#d97706]$[/] {' '.join(argv)}"
        )
        run_subprocess(app, argv, out)

    if pip_pkgs:
        argv = ["pip3", "install"] + pip_pkgs
        app.call_from_thread(
            out.write, f"[#d97706]$[/] {' '.join(argv)}"
        )
        run_subprocess(app, argv, out)

    app.call_from_thread(out.write, "[#22c55e]✓ Install complete[/]")
    app.call_from_thread(app._resolve_profile)


def run_subprocess(app, argv: list[str], out: RichLog) -> None:
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError:
        app.call_from_thread(
            out.write, f"[#ef4444]✗ Command not found: {argv[0]}[/]"
        )
        return

    if proc.stdout:
        for line in proc.stdout:
            app.call_from_thread(out.write, line.rstrip())
    proc.wait()
    if proc.returncode != 0:
        app.call_from_thread(
            out.write, f"[#ef4444]✗ Exit code: {proc.returncode}[/]"
        )


def run_selfmod(app, goal: str) -> None:
    from selfmod import is_enabled
    out = app.query_one("#output", RichLog)
    if not is_enabled():
        out.write(
            "[#f59e0b]⚠ Self-modification is disabled.[/]\n"
            "[#6b7280]Set CLIVE_EXPERIMENTAL_SELFMOD=1 in .env to enable.[/]"
        )
        return
    app._execute_selfmod(goal)


def execute_selfmod(app, goal: str) -> None:
    """Body for the @work-decorated _execute_selfmod method on CliveApp."""
    out = app.query_one("#output", RichLog)
    from selfmod.pipeline import run_pipeline

    def on_status(stage: str, msg: str) -> None:
        icon = {
            "analyzing": "◐",
            "proposing": "◐",
            "reviewing": "◑",
            "auditing": "◒",
            "gate": "◓",
            "applying": "●",
            "complete": "✓",
        }.get(stage, "·")
        color = "#22c55e" if stage == "complete" else "#d97706"
        app.call_from_thread(
            out.write, f"  [{color}]{icon}[/] [#6b7280]{stage}:[/] {msg}"
        )

    app.call_from_thread(out.write, "")
    app.call_from_thread(
        out.write, "[#d97706]Self-modification pipeline[/]"
    )

    result = run_pipeline(goal, on_status=on_status)

    app.call_from_thread(out.write, "")
    if result.success:
        app.call_from_thread(
            out.write,
            f"[#22c55e]✓ Applied:[/] {result.message}"
        )
        app.call_from_thread(
            out.write,
            f"[#6b7280]  Snapshot: {result.snapshot_tag} · "
            f"Tokens: {result.tokens['prompt'] + result.tokens['completion']:,}[/]"
        )
        app.call_from_thread(
            out.write,
            "[#6b7280]  Use /undo to roll back.[/]"
        )
    else:
        app.call_from_thread(
            out.write,
            f"[#ef4444]✗ {result.stage}:[/] {result.message}"
        )
    app.call_from_thread(out.write, "")


def undo_selfmod(app) -> None:
    out = app.query_one("#output", RichLog)
    try:
        from selfmod.workspace import rollback, list_snapshots
        snaps = list_snapshots()
        if not snaps:
            out.write("[#6b7280]No selfmod snapshots to undo.[/]")
            return
        tag = rollback()
        out.write(f"[#22c55e]✓[/] Rolled back to [#c9c9d6]{tag}[/]")
    except Exception as e:
        out.write(f"[#ef4444]✗ Undo failed: {e}[/]")


def run_evolve(app, driver: str, out: RichLog) -> None:
    """Run driver evolution in background thread."""
    def _worker():
        try:
            from evolve import evolve_driver
            result = evolve_driver(driver, dry_run=False)
            if result["improved"]:
                app.call_from_thread(out.write, f"[green]✓ {driver} driver improved: {result['baseline_score']:.3f} → {result['final_score']:.3f}[/green]")
            else:
                app.call_from_thread(out.write, f"[yellow]No improvement found for {driver} (baseline: {result['baseline_score']:.3f})[/yellow]")
        except Exception as e:
            app.call_from_thread(out.write, f"[red]Evolution error: {e}[/red]")
    threading.Thread(target=_worker, daemon=True).start()
    out.write(f"[dim]Evolution running in background for {driver}...[/dim]")
