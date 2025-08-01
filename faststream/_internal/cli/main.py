import logging
import sys
import warnings
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import anyio
import typer

from faststream import FastStream
from faststream.__about__ import __version__
from faststream._internal._compat import IS_WINDOWS, json_loads
from faststream._internal.application import Application
from faststream.asgi import AsgiFastStream
from faststream.exceptions import INSTALL_WATCHFILES, SetupError, StartupValidationError

from .docs import docs_app
from .options import (
    APP_ARGUMENT,
    APP_DIR_OPTION,
    FACTORY_OPTION,
    RELOAD_EXTENSIONS_OPTION,
    RELOAD_FLAG,
)
from .utils.imports import import_from_string
from .utils.logs import (
    LogFiles,
    LogLevels,
    get_log_level,
    set_log_config,
    set_log_level,
)
from .utils.parser import parse_cli_args

if TYPE_CHECKING:
    from faststream._internal.basic_types import SettingField
    from faststream._internal.broker import BrokerUsecase

cli = typer.Typer(pretty_exceptions_short=True)
cli.add_typer(docs_app, name="docs", help="Documentations commands")


def version_callback(version: bool) -> None:
    """Callback function for displaying version information."""
    if version:
        import platform

        typer.echo(
            f"Running FastStream {__version__} with {platform.python_implementation()} "
            f"{platform.python_version()} on {platform.system()}",
        )

        raise typer.Exit


@cli.callback()
def main(
    version: bool | None = typer.Option(
        False,
        "-v",
        "--version",
        callback=version_callback,
        is_eager=True,
        help="Show current platform, python and FastStream version.",
    ),
) -> None:
    """Generate, run and manage FastStream apps to greater development experience."""


@cli.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def run(
    ctx: typer.Context,
    app: str = APP_ARGUMENT,
    workers: int = typer.Option(
        1,
        "-w",
        "--workers",
        show_default=False,
        help="Run [workers] applications with process spawning.",
        envvar="FASTSTREAM_WORKERS",
    ),
    app_dir: str = APP_DIR_OPTION,
    is_factory: bool = FACTORY_OPTION,
    reload: bool = RELOAD_FLAG,
    watch_extensions: list[str] = RELOAD_EXTENSIONS_OPTION,
    log_level: LogLevels = typer.Option(
        LogLevels.notset,
        "-l",
        "--log-level",
        case_sensitive=False,
        help="Set selected level for FastStream and brokers logger objects.",
        envvar="FASTSTREAM_LOG_LEVEL",
        show_default=False,
    ),
    log_config: Path | None = typer.Option(
        None,
        "--log-config",
        help=(
            "Set file to configure logging. Support "
            f"{', '.join(f'`{x.value}`' for x in LogFiles)} extensions."  # noqa: B008
        ),
        show_default=False,
    ),
) -> None:
    """Run [MODULE:APP] FastStream application."""
    if watch_extensions and not reload:
        typer.echo(
            "Extra reload extensions has no effect without `--reload` flag."
            "\nProbably, you forgot it?",
        )

    app, extra = parse_cli_args(app, *ctx.args)
    casted_log_level = get_log_level(log_level)

    if app_dir:  # pragma: no branch
        sys.path.insert(0, app_dir)

    # Should be imported after sys.path changes
    module_path, app_obj = import_from_string(app, is_factory=is_factory)
    app_obj = cast("Application", app_obj)

    args = (app, extra, is_factory, log_config, casted_log_level)

    if reload and workers > 1:
        msg = "You can't use reload option with multiprocessing"
        raise SetupError(msg)
    if workers <= 1:
        extra["worker_id"] = None

    if reload:
        try:
            from faststream._internal.cli.supervisors.watchfiles import WatchReloader
        except ImportError:
            warnings.warn(INSTALL_WATCHFILES, category=ImportWarning, stacklevel=1)
            _run(*args)

        else:
            reload_dirs = []
            if module_path:
                reload_dirs.append(str(module_path))
            if app_dir != ".":
                reload_dirs.append(app_dir)

            WatchReloader(
                target=_run,
                args=args,
                reload_dirs=reload_dirs,
                extra_extensions=watch_extensions,
            ).run()

    elif workers > 1:
        if isinstance(app_obj, FastStream):
            from faststream._internal.cli.supervisors.multiprocess import Multiprocess

            Multiprocess(
                target=_run,
                args=(*args, logging.DEBUG),
                workers=workers,
            ).run()

        elif isinstance(app_obj, AsgiFastStream):
            from faststream._internal.cli.supervisors.asgi_multiprocess import (
                ASGIMultiprocess,
            )

            ASGIMultiprocess(
                target=app,
                args=args,
                workers=workers,
            ).run()

        else:
            msg = f"Unexpected app type, expected FastStream or AsgiFastStream, got: {type(app_obj)}."
            raise typer.BadParameter(msg)

    else:
        _run_imported_app(
            app_obj,
            extra_options=extra,
            log_level=casted_log_level,
            log_config=log_config,
        )


def _run(
    # NOTE: we should pass `str` due FastStream is not picklable
    app: str,
    extra_options: dict[str, "SettingField"],
    is_factory: bool,
    log_config: Path | None,
    log_level: int = logging.NOTSET,
    app_level: int = logging.INFO,  # option for reloader only
) -> None:
    """Runs the specified application."""
    _, app_obj = import_from_string(app, is_factory=is_factory)
    app_obj = cast("Application", app_obj)
    _run_imported_app(
        app_obj,
        extra_options=extra_options,
        log_level=log_level,
        app_level=app_level,
        log_config=log_config,
    )


def _run_imported_app(
    app_obj: "Application",
    extra_options: dict[str, "SettingField"],
    log_config: Path | None,
    log_level: int = logging.NOTSET,
    app_level: int = logging.INFO,  # option for reloader only
) -> None:
    if not isinstance(app_obj, Application):
        msg = f'Imported object "{app_obj}" must be "Application" type.'
        raise typer.BadParameter(
            msg,
        )

    if log_level > 0:
        set_log_level(log_level, app_obj)

    if log_config is not None:
        set_log_config(log_config)

    if not IS_WINDOWS:  # pragma: no cover
        with suppress(ImportError):
            import uvloop

            uvloop.install()

    try:
        anyio.run(
            app_obj.run,
            app_level,
            extra_options,
        )

    except StartupValidationError as startup_exc:
        from faststream._internal.cli.utils.errors import draw_startup_errors

        draw_startup_errors(startup_exc)
        sys.exit(1)


@cli.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def publish(
    ctx: typer.Context,
    app: str = APP_ARGUMENT,
    app_dir: str = APP_DIR_OPTION,
    message: str = typer.Argument(
        ...,
        help="JSON Message string to publish.",
        show_default=False,
    ),
    rpc: bool = typer.Option(
        False,
        help="Enable RPC mode and system output.",
    ),
    is_factory: bool = FACTORY_OPTION,
) -> None:
    """Publish a message using the specified broker in a FastStream application.

    This command publishes a message to a broker configured in a FastStream app instance.
    It supports various brokers and can handle extra arguments specific to each broker type.
    These are parsed and passed to the broker's publish method.
    """
    app, extra = parse_cli_args(app, *ctx.args)

    if app_dir:  # pragma: no branch
        sys.path.insert(0, app_dir)

    publish_extra: dict[str, Any] = extra.copy()
    if "timeout" in publish_extra:
        publish_extra["timeout"] = float(publish_extra["timeout"])

    try:
        _, app_obj = import_from_string(app, is_factory=is_factory)

        assert isinstance(app_obj, FastStream), app_obj

        if not app_obj.broker:
            msg = "Broker instance not found in the app."
            raise ValueError(msg)

        result = anyio.run(publish_message, app_obj.broker, rpc, message, publish_extra)

        if rpc:
            typer.echo(result)

    except Exception as e:
        typer.echo(f"Publish error: {e}")
        sys.exit(1)


async def publish_message(
    broker: "BrokerUsecase[Any, Any]",
    rpc: bool,
    message: str,
    extra: dict[str, Any],
) -> Any:
    with suppress(Exception):
        message = json_loads(message)

    try:
        async with broker:
            if rpc:
                return await broker.request(message, **extra)  # type: ignore[call-arg]
            return await broker.publish(message, **extra)  # type: ignore[call-arg]

    except Exception as e:
        typer.echo(f"Error when broker was publishing: {e!r}")
        sys.exit(1)
