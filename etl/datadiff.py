import difflib
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import pandas as pd
import requests
import rich
import rich_click as click
import structlog
from deepdiff import DeepDiff
from owid.catalog import Dataset, DatasetMeta, LocalCatalog, RemoteCatalog, Table, find
from owid.catalog.catalogs import CHANNEL, OWID_CATALOG_URI
from rich.console import Console

from etl.files import yaml_dump

log = structlog.get_logger()


class DatasetDiff:
    """Compare two datasets and print a summary of the differences."""

    def __init__(
        self, ds_a: Optional[Dataset], ds_b: Optional[Dataset], verbose: bool = False, print: Callable = rich.print
    ):
        """
        :param print: Function to print the diff summary. Defaults to rich.print.
        """
        assert ds_a or ds_b, "At least one Dataset must be provided"
        self.ds_a = ds_a
        self.ds_b = ds_b
        self.p = print
        self.verbose = verbose

    def _diff_datasets(self, ds_a: Optional[Dataset], ds_b: Optional[Dataset]):
        if ds_a and ds_b:
            ds_short_name = ds_a.metadata.short_name
            assert ds_short_name

            new_version = " (new version)" if ds_a.metadata.version != ds_b.metadata.version else ""

            # compare dataset metadata
            diff = DeepDiff(_dataset_metadata_dict(ds_a), _dataset_metadata_dict(ds_b))
            if diff:
                self.p(f"[yellow]~ Dataset [b]{dataset_uri(ds_b)}[/b]{new_version}")
                if self.verbose:
                    self.p(_dict_diff(_dataset_metadata_dict(ds_a), _dataset_metadata_dict(ds_b), tabs=2))
            else:
                self.p(f"[white]= Dataset [b]{dataset_uri(ds_b)}{new_version}[/b]")
        elif ds_a:
            self.p(f"[red]- Dataset [b]{dataset_uri(ds_a)}[/b]")
        elif ds_b:
            self.p(f"[green]+ Dataset [b]{dataset_uri(ds_b)}[/b]")

    def _diff_tables(self, ds_a: Dataset, ds_b: Dataset, table_name: str):
        if table_name not in ds_b.table_names:
            self.p(f"\t[red]- Table [b]{table_name}[/b]")
            for col in ds_a[table_name].columns:
                self.p(f"\t\t[red]- Column [b]{col}[/b]")
        elif table_name not in ds_a.table_names:
            self.p(f"\t[green]+ Table [b]{table_name}[/b]")
            for col in ds_b[table_name].columns:
                self.p(f"\t\t[green]+ Column [b]{col}[/b]")
        else:
            table_a = ds_a[table_name].reset_index()
            table_b = ds_b[table_name].reset_index()

            # compare table metadata
            diff = DeepDiff(_table_metadata_dict(table_a), _table_metadata_dict(table_b))
            if diff:
                self.p(f"\t[yellow]~ Table [b]{table_name}[/b] (changed [u]metadata[/u])")

                if self.verbose:
                    self.p(_dict_diff(_table_metadata_dict(table_a), _table_metadata_dict(table_b), tabs=3))
            else:
                self.p(f"\t[white]= Table [b]{table_name}[/b]")

            # compare columns
            for col in sorted(set(table_a.columns) | set(table_b.columns)):
                if col not in table_a.columns:
                    self.p(f"\t\t[green]+ Column [b]{col}[/b]")
                elif col not in table_b.columns:
                    self.p(f"\t\t[red]- Column [b]{col}[/b]")
                else:
                    col_a = table_a[col]
                    col_b = table_b[col]
                    shape_diff = col_a.shape != col_b.shape
                    if not shape_diff:
                        if col_a.dtype == "category":
                            col_a = col_a.astype("string")
                        if col_b.dtype == "category":
                            col_b = col_b.astype("string")

                        try:
                            pd.testing.assert_series_equal(col_a, col_b, check_dtype=False)
                            data_diff = False
                        except AssertionError:
                            data_diff = True
                    else:
                        data_diff = False

                    col_a_meta = col_a.metadata.to_dict()
                    col_b_meta = col_b.metadata.to_dict()

                    meta_diff = DeepDiff(col_a_meta, col_b_meta)

                    changed = (
                        (["data"] if data_diff else [])
                        + (["metadata"] if meta_diff else [])
                        + (["shape"] if shape_diff else [])
                    )

                    if changed:
                        self.p(f"\t\t[yellow]~ Column [b]{col}[/b] (changed [u]{' & '.join(changed)}[/u])")
                        if self.verbose and meta_diff:
                            self.p(_dict_diff(col_a_meta, col_b_meta, tabs=4))
                    else:
                        # do not print identical columns
                        pass

    def summary(self):
        """Print a summary of the differences between the two datasets."""
        self._diff_datasets(self.ds_a, self.ds_b)

        if self.ds_a and self.ds_b:
            for table_name in set(self.ds_a.table_names) | set(self.ds_b.table_names):
                self._diff_tables(self.ds_a, self.ds_b, table_name)


class RemoteDataset:
    """Dataset from remote catalog with the same interface as Dataset."""

    def __init__(self, dataset_meta: DatasetMeta, table_names: List[str]):
        self.metadata = dataset_meta
        self.table_names = table_names

    def __getitem__(self, name: str) -> Table:
        tables = find(
            table=name,
            namespace=self.metadata.namespace,
            version=self.metadata.version,
            dataset=self.metadata.short_name,
            channels=[self.metadata.channel],  # type: ignore
        )

        tables = tables[tables.channel == self.metadata.channel]  # type: ignore

        return tables.load()


@click.command(help=__doc__)
@click.argument(
    "path-a",
    type=click.Path(),
)
@click.argument(
    "path-b",
    type=click.Path(),
)
@click.option(
    "--channel",
    "-c",
    multiple=True,
    type=click.Choice(CHANNEL.__args__),
    default=["garden", "meadow", "grapher"],
    help="Compare only selected channel (subfolder of data/), compare only meadow, garden and grapher by default",
)
@click.option(
    "--include",
    type=str,
    help="Compare only datasets matching pattern",
)
@click.option(
    "--exclude",
    "-e",
    type=str,
    help="Exclude datasets matching pattern",
)
@click.option(
    "--verbose",
    is_flag=True,
    help="Print detailed differences",
)
def cli(
    path_a: str,
    path_b: str,
    channel: Iterable[CHANNEL],
    include: Optional[str],
    exclude: Optional[str],
    verbose: bool,
) -> None:
    """Compare all datasets from two catalogs (`a` and `b`) and print out summary of their differences. This is
    different from `compare` tool which compares two specific datasets and prints out more detailed output. This
    tool is useful as a quick way to see what has changed in the catalog and whether our updates don't have any
    unexpected side effects.

    It uses **source checksums** to find candidates for comparison. Source checksum includes all files used to
    generate the dataset and should be sufficient to find changed datasets, just note that we're not using
    checksum of the files themselves. So if you change core ETL code or some of the dependencies, e.g. change in
    owid-datautils-py, core ETL code or updating library version, the change won't be detected. In cases like
    these you should increment ETL version which is added to all source checksums (not implemented yet).

    Usage:
        # compare local catalog with remote catalog
        etl-datadiff REMOTE data/ --include maddison

        # compare two local catalogs
        etl-datadiff other-data/ data/ --include maddison
    """
    console = Console(tab_size=2)

    path_to_ds_a = _load_catalog_datasets(path_a, channel, include, exclude)
    path_to_ds_b = _load_catalog_datasets(path_b, channel, include, exclude)

    any_diff = False

    for path in sorted(set(path_to_ds_a.keys()) | set(path_to_ds_b.keys())):
        ds_a = _match_dataset(path_to_ds_a, path)
        ds_b = _match_dataset(path_to_ds_b, path)

        if ds_a and ds_b and ds_a.metadata.source_checksum == ds_b.metadata.source_checksum:
            # skip if they have the same source checksum, note that we're not comparing checksum of actual data
            # to improve performance. Source checksum should be enough
            continue

        lines = []
        differ = DatasetDiff(ds_a, ds_b, print=lambda x: lines.append(x), verbose=verbose)
        differ.summary()

        for line in lines:
            console.print(line)

        if any("~" in line for line in lines):
            any_diff = True

    console.print()
    if any_diff:
        console.print("[red]❌ Found differences[/red]")
    else:
        console.print("[green]✅ No differences found[/green]")
    console.print()

    console.print(
        "[b]Legend[/b]: [green]+New[/green]  [yellow]~Modified[/yellow]  [red]-Removed[/red]  [white]=Identical[/white]  [violet]Details[/violet]"
    )
    console.print(
        "[b]Hint[/b]: Run this locally with [cyan][b]etl-datadiff REMOTE data/ --include yourdataset --verbose[/b][/cyan]"
    )
    console.print(
        "[b]Hint[/b]: Get detailed comparison with [cyan][b]compare --show-values channel namespace version short_name --data-values[/b][/cyan]"
    )
    exit(1 if any_diff else 0)


def _dict_diff(dict_a: Dict[str, Any], dict_b: Dict[str, Any], tabs) -> str:
    """Convert dictionaries into YAML and compare them using difflib. Return colored diff as a string."""
    meta_a = yaml_dump(dict_a)
    meta_b = yaml_dump(dict_b)

    lines = difflib.ndiff(meta_a.splitlines(keepends=True), meta_b.splitlines(keepends=True))  # type: ignore
    # do not print lines that are identical
    lines = [line for line in lines if not line.startswith("  ")]

    # add color
    lines = ["[violet]" + line for line in lines]

    # add tabs
    return "\t" * tabs + "".join(lines).replace("\n", "\n" + "\t" * tabs).rstrip()


def _match_dataset(path_to_ds: Dict[str, Any], path: str) -> Optional[Dataset]:
    """Get dataset from dictionary {path -> dataset}. Return dataset with the same version if available,
    otherwise return older version or None if there is no such dataset."""
    if path in path_to_ds:
        return path_to_ds[path]
    else:
        # find latest matching version
        channel, namespace, version, short_name = path.split("/")

        candidates = []
        for k in path_to_ds.keys():
            if re.match(f"{channel}/{namespace}/.*?/{short_name}", k):
                candidates.append(k)

        if candidates:
            latest_version = max(candidates)
            # make sure we don't compare to newer version
            if latest_version < path:
                return path_to_ds[latest_version]
            else:
                return None
        else:
            return None


def _load_catalog_datasets(
    catalog_path: str, channels: Iterable[CHANNEL], include: Optional[str], exclude: Optional[str]
) -> Dict[str, Any]:
    if catalog_path == "REMOTE":
        assert include, "You have to filter with --include when comparing with remote catalog"
        return _remote_catalog_datasets(channels=channels, include=include, exclude=exclude)
    else:
        return _local_catalog_datasets(catalog_path, channels=channels, include=include, exclude=exclude)


def _table_metadata_dict(tab: Table) -> Dict[str, Any]:
    """Extract metadata from Table object, prune and and return it as a dictionary"""
    d = tab.metadata.to_dict()

    # add columns
    # d["columns"] = {}
    # for col in tab.columns:
    #     d["columns"][col] = tab[col].metadata.to_dict()

    del d["dataset"]
    return d


def _dataset_metadata_dict(ds: Dataset) -> Dict[str, Any]:
    """Extract metadata from Dataset object, prune and and return it as a dictionary"""
    d = ds.metadata.to_dict()
    del d["source_checksum"]
    return d


def _local_catalog_datasets(
    catalog_path: str, channels: Iterable[CHANNEL], include: Optional[str], exclude: Optional[str]
) -> Dict[str, Dataset]:
    """Return a mapping from dataset path to Dataset object of local catalog."""
    lc_a = LocalCatalog(catalog_path, channels=channels)
    datasets = []
    for chan in lc_a.channels:
        channel_datasets = list(lc_a.iter_datasets(chan, include=include))
        # TODO: channel should be in DatasetMeta by default
        for ds in channel_datasets:
            ds.metadata.channel = chan  # type: ignore

        datasets += channel_datasets

    # keep only relative path of dataset
    mapping = {str(Path(ds.path).relative_to(catalog_path)): ds for ds in datasets}

    if exclude:
        re_exclude = re.compile(exclude)
        mapping = {path: ds for path, ds in mapping.items() if not re_exclude.search(path)}

    return mapping


def _remote_catalog_datasets(channels: Iterable[CHANNEL], include: str, exclude: Optional[str]) -> Dict[str, Dataset]:
    """Return a mapping from dataset path to Dataset object of remote catalog."""
    rc = RemoteCatalog(channels=channels)
    frame = rc.frame

    frame["ds_paths"] = frame["path"].map(os.path.dirname)
    ds_paths = frame["ds_paths"]

    if include:
        ds_paths = ds_paths[ds_paths.str.contains(include)]

    if exclude:
        ds_paths = ds_paths[~ds_paths.str.contains(exclude)]

    ds_paths = set(ds_paths)

    if len(ds_paths) >= 10:
        log.warning(f"Fetching {len(ds_paths)} datasets from the remote catalog, this may get slow...")

    mapping = {}
    for path in ds_paths:
        uri = f"{OWID_CATALOG_URI}{path}/index.json"
        ds_meta = DatasetMeta(**requests.get(uri).json())
        # TODO: channel should be in DatasetMeta by default
        ds_meta.channel = path.split("/")[0]  # type: ignore
        table_names = frame.loc[frame["ds_paths"] == path, "table"].tolist()
        mapping[path] = RemoteDataset(ds_meta, table_names)

    return mapping


def dataset_uri(ds: Dataset) -> str:
    # TODO: coule be method in DatasetMeta (after we add channel)
    assert hasattr(ds.metadata, "channel"), "Dataset metadata should have channel attribute"
    return f"{ds.metadata.channel}/{ds.metadata.namespace}/{ds.metadata.version}/{ds.metadata.short_name}"  # type: ignore


if __name__ == "__main__":
    cli()