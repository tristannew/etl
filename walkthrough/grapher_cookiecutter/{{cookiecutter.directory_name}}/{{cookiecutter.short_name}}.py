"""Load a garden dataset and create a grapher dataset."""

from owid import catalog

from etl.helpers import PathFinder

# Get paths and naming conventions for current step.
paths = PathFinder(__file__)


def run(dest_dir: str) -> None:
    #
    # Load inputs.
    #
    # Load garden dataset.
    ds_garden = paths.load_dependency("{{cookiecutter.short_name}}")

    # Read table from garden dataset.
    tb_garden = ds_garden["{{cookiecutter.short_name}}"]

    #
    # Process data.
    #

    #
    # Save outputs.
    #
    # Create a new grapher dataset with the same metadata as the garden dataset.
    ds_grapher = catalog.Dataset.create_empty(dest_dir, ds_garden.metadata)

    # Add table of processed data to the new dataset.
    ds_grapher.add(tb_garden)

    # Save changes in the new grapher dataset.
    ds_grapher.save()
