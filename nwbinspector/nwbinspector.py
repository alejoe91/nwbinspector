"""Primary functions for inspecting NWBFiles."""
import os
import importlib
import traceback
from pathlib import Path
from collections import OrderedDict, Iterable
import json
from enum import Enum
from typing import Optional

import click
import pynwb
from natsort import natsorted
import yaml

from . import available_checks
from .inspector_tools import (
    organize_check_results,
    format_organized_results_output,
    print_to_console,
    save_report,
)
from .register_checks import InspectorMessage, Importance
from .utils import FilePathType, PathType, OptionalListOfStrings


class InspectorOutputJSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, InspectorMessage):
            return o.__dict__
        if isinstance(o, Enum):
            return o.name
        else:
            return super().default(o)


@click.command()
@click.argument("path")
@click.option("-m", "--modules", help="Modules to import prior to reading the file(s).")
@click.option("--no-color", help="Disable coloration for console display of output.", is_flag=True)
@click.option(
    "--report-file-path",
    default=None,
    help="Save path for the report file.",
    type=click.Path(writable=True),
)
@click.option("-o", "--overwrite", help="Overwrite an existing report file at the location.", is_flag=True)
@click.option("-i", "--ignore", help="Comma-separated names of checks to skip.")
@click.option("-s", "--select", help="Comma-separated names of checks to run")
@click.option(
    "-t",
    "--threshold",
    default="BEST_PRACTICE_SUGGESTION",
    type=click.Choice(["CRITICAL", "BEST_PRACTICE_VIOLATION", "BEST_PRACTICE_SUGGESTION"]),
    help="Ignores tests with an assigned importance below this threshold.",
)
@click.option("-c", "--config-path", help="path of config .yaml file that overwrites importance of checks.")
@click.option("-j", "--json-file-path", help="Write json output to this location.")
def inspect_all_cli(
    path: str,
    modules: Optional[str] = None,
    no_color: bool = False,
    report_file_path: str = None,
    overwrite: bool = False,
    ignore: Optional[str] = None,
    select: Optional[str] = None,
    threshold: str = "BEST_PRACTICE_SUGGESTION",
    config_path: Optional[str] = None,
    json_file_path: str = None,
):
    """Primary CLI usage."""
    if config_path is not None:
        with open(file=config_path, mode="r") as stream:
            config = yaml.load(stream, yaml.Loader)
        with open(file=Path(__file__).parent / "config.schema.json", mode="r") as fp:
            schema = json.load(fp=fp)
        jsonschema.validate(config, schema)
    else:
        config = None

    organized_results = inspect_all(
        path,
        modules=modules,
        config=config,
        ignore=ignore if ignore is None else ignore.split(","),
        select=select if select is None else select.split(","),
        importance_threshold=Importance[threshold],
    )
    if json_file_path is not None:
        with open(json_file_path, "w") as fp:
            json.dump(organized_results, fp, cls=InspectorOutputJSONEncoder)

    if len(organized_results):
        formatted_results = format_organized_results_output(organized_results=organized_results)
        print_to_console(formatted_results=formatted_results, no_color=no_color)
        if report_file_path is not None:
            save_report(report_file_path=report_file_path, formatted_results=formatted_results, overwrite=overwrite)
            print(f"{os.linesep*2}Log file saved at {str(report_file_path.absolute())}!{os.linesep}")


def inspect_all(
    path: PathType,
    modules: OptionalListOfStrings = None,
    ignore: OptionalListOfStrings = None,
    select: OptionalListOfStrings = None,
    config: dict = None,
    importance_threshold: Importance = Importance.BEST_PRACTICE_SUGGESTION,
):
    """Inspect all NWBFiles at the specified path."""
    modules = modules or []
    path = Path(path)

    in_path = Path(path)
    if in_path.is_dir():
        nwbfiles = list(in_path.glob("*.nwb"))
    elif in_path.is_file():
        nwbfiles = [in_path]
    else:
        raise ValueError(f"{in_path} should be a directory or an NWB file.")
    nwbfiles = natsorted(nwbfiles)

    if config is not None:
        checks = configure_checks(config, available_checks)
    else:
        checks = available_checks

    for module in modules:
        importlib.import_module(module)
    organized_results = dict()
    for file_index, nwbfile_path in enumerate(nwbfiles):
        organized_results.update(
            inspect_nwb(
                nwbfile_path=nwbfile_path,
                checks=checks,
                importance_threshold=importance_threshold,
                ignore=ignore,
                select=select,
            )
        )
    return organized_results


def configure_checks(config, checks=available_checks):
    checks_out = []
    for check in checks:
        for importance_name, func_names in config.items():
            if check.__name__ in func_names:
                if importance_name == "SKIP":
                    continue
                check.importance = Importance[importance_name]
        checks_out.append(check)
    return checks_out


def inspect_nwb(
    nwbfile_path: FilePathType,
    checks: list = available_checks,
    importance_threshold: Importance = Importance.BEST_PRACTICE_SUGGESTION,
    ignore: OptionalListOfStrings = None,
    select: OptionalListOfStrings = None,
    driver: str = None,
):
    """
    Inspect a NWBFile object and return suggestions for improvements according to best practices.

    Parameters
    ----------
    nwbfile_path : FilePathType
        Path to the NWBFile.
    checks : list, optional
        list of checks to run
    importance_threshold : string, optional
        Ignores tests with an assigned importance below this threshold.
        Importance has three levels:
            CRITICAL
                - potentially incorrect data
            BEST_PRACTICE_VIOLATION
                - very suboptimal data representation
            BEST_PRACTICE_SUGGESTION
                - improvable data representation
        The default is the lowest level, BEST_PRACTICE_SUGGESTION.
    ignore: list, optional
        Names of functions to skip.
    select: list, optional
    driver: str, optional
        Forwarded to h5py.File(). Set to "ros3" for reading from s3 url.
    """
    if ignore is not None and select is not None:
        raise ValueError("Options 'ignore' and 'select' cannot both be used.")
    if importance_threshold not in Importance:
        raise ValueError(
            f"Indicated importance_threshold ({importance_threshold}) is not a valid importance level! Please choose "
            "from [CRITICAL_IMPORTANCE, BEST_PRACTICE_VIOLATION, BEST_PRACTICE_SUGGESTION]."
        )
    unorganized_results = OrderedDict({importance.name: list() for importance in Importance})
    try:
        with pynwb.NWBHDF5IO(path=str(nwbfile_path), mode="r", load_namespaces=True, driver=driver) as io:
            validation_errors = pynwb.validate(io=io)
            if any(validation_errors):
                for validation_error in validation_errors:
                    message = InspectorMessage(message=validation_error.reason)
                    message.importance = Importance.PYNWB_VALIDATION
                    message.check_function_name = validation_error.name
                    message.location = validation_error.location
                    unorganized_results["PYNWB_VALIDATION"].append(message)
            nwbfile = io.read()
            check_results = list()
            for check_function in checks:
                if (
                    (ignore is not None and check_function.__name__ in ignore)
                    or (select is not None and check_function.__name__ not in select)
                    or (check_function.importance.value < importance_threshold.value)
                ):
                    continue
                for nwbfile_object in nwbfile.objects.values():
                    if issubclass(type(nwbfile_object), check_function.neurodata_type):
                        output = check_function(nwbfile_object)
                        if output is not None:
                            if isinstance(output, Iterable):
                                check_results.extend(output)
                            else:
                                check_results.append(output)
                if any(check_results):
                    unorganized_results.update(organize_check_results(check_results=check_results))
    except Exception as ex:
        message = InspectorMessage(message=traceback.format_exc())
        message.importance = Importance.ERROR
        message.check_function_name = ex
        unorganized_results["ERROR"].append(message)
    organized_result = OrderedDict()
    for importance_level, results in unorganized_results.items():
        if any(results):
            organized_result.update({importance_level: results})
    organized_result = {str(nwbfile_path): organized_result}
    return organized_result


if __name__ == "__main__":
    inspect_all_cli()
